"""Publish draft pull requests for a finished session's worktrees.

Triggered by ``SessionManager`` when a session reaches DONE and the
``make_draft_pr`` option is set. Each attached worktree gets its own PR
against its origin's default branch. Failures are reported per-repo via
``PRResult`` (and surfaced to the UI as ``PR_FAILED`` events) so a single
broken push doesn't sink the whole batch.

Title and body prefer the agent's approved plan (a ``# Heading`` for the
title, a ``## Context`` section for the body) over the raw initial prompt,
so PRs read as summaries of *what was done* rather than verbatim user input.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
from git import Repo
from git.exc import GitCommandError, HookExecutionError
from githubkit.exception import GitHubException

from chud.gh import _get_client, _resolve_remote_owner_name
from chud.settings import render_pr_body_footer
from chud.types import SessionState

log = logging.getLogger(__name__)


_TITLE_LIMIT = 72


@dataclass
class PRResult:
    repo_label: str
    branch: str
    url: str | None = None
    error: str | None = None
    discarded: bool = False


async def _default_branch(repo_path: Path) -> str:
    """Resolve the remote's default branch via the GitHub REST API.

    Falls back to ``"main"`` on any failure (no auth, no GitHub remote,
    network error). Mirrors the role of ``worktree.default_base_branch``
    on the publish path.
    """
    client = _get_client()
    if client is None:
        return "main"
    owner_name = _resolve_remote_owner_name(repo_path)
    if owner_name is None:
        return "main"
    owner, name = owner_name
    try:
        resp = await client.rest.repos.async_get(owner, name)
    except (GitHubException, httpx.HTTPError) as e:
        log.warning("repos.get default-branch lookup failed for %s/%s: %s", owner, name, e)
        return "main"
    return str(getattr(resp.parsed_data, "default_branch", "main") or "main")


def _is_dirty_sync(worktree: Path) -> bool:
    """Return True iff ``worktree`` has any tracked changes or untracked files.

    Mirrors the ``git status --porcelain`` semantics that the original
    subprocess version checked.
    """
    try:
        return Repo(worktree).is_dirty(untracked_files=True)
    except Exception:
        # Treat any GitPython failure the same as the legacy "non-zero exit
        # = not dirty" branch — caller falls through to the rev-list step
        # where the real failure surfaces with a useful message.
        return False


async def _is_dirty(worktree: Path) -> bool:
    return await asyncio.to_thread(_is_dirty_sync, worktree)


def _auto_commit_sync(worktree: Path, title: str, body: str) -> tuple[bool, str]:
    """Stage and commit every change under one chud commit.

    Returns ``(ok, err_text)``. On failure, ``err_text`` carries the git
    error message (e.g. "Please tell me who you are" when ``user.email``
    is unset, or a pre-commit hook's rejection).
    """
    try:
        repo = Repo(worktree)
        repo.git.add(A=True)
        repo.index.commit(f"{title}\n\n{body}")
    except (HookExecutionError, GitCommandError) as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)
    return True, ""


async def _auto_commit(worktree: Path, title: str, body: str) -> tuple[bool, str]:
    """Stage and commit every change in ``worktree`` under one chud commit.

    The Claude SDK in ``acceptEdits`` mode edits files but never commits, so
    a session can finish with the agent's work sitting uncommitted. Without
    this helper, ``publish_draft_prs`` would see ``0`` commits ahead of base
    and discard the worktree as "abandoned" — losing the agent's work.

    Author identity is deferred to the user's local git config —
    fabricating a chud bot identity would silently make commits the user
    can't push under their own credentials.
    """
    return await asyncio.to_thread(_auto_commit_sync, worktree, title, body)


# Match a top-level "# heading" line (single hash, not ## or more). DOTALL is
# unnecessary because we only consume up to the newline.
_H1_RE = re.compile(r"^[ \t]*#[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)
# Match a "## Context" heading and capture everything until the next "## " or EOF.
_CONTEXT_RE = re.compile(
    r"^[ \t]*##[ \t]+context[ \t]*$\s*(?P<body>.+?)(?=^[ \t]*##[ \t]+|\Z)",
    re.MULTILINE | re.IGNORECASE | re.DOTALL,
)


def _extract_plan_title(plan_text: str) -> str | None:
    """Return the first ``# Heading`` line of ``plan_text``, stripped, or None.

    Skips ``##``/``###``/etc. — only the top-level title counts.
    """
    if not plan_text:
        return None
    m = _H1_RE.search(plan_text)
    if not m:
        return None
    title = m.group("title").strip()
    return title or None


def _extract_plan_context(plan_text: str) -> str | None:
    """Return the body of the first ``## Context`` section, stripped, or None."""
    if not plan_text:
        return None
    m = _CONTEXT_RE.search(plan_text)
    if not m:
        return None
    body = m.group("body").strip()
    return body or None


def _truncate_title(title: str) -> str:
    if len(title) > _TITLE_LIMIT:
        return title[: _TITLE_LIMIT - 1] + "…"
    return title


def _title_from_prompt(prompt: str) -> str:
    """Fallback title derivation when no approved plan is available."""
    text = prompt.strip()
    if not text:
        return "chud session"
    first = text.splitlines()[0].strip() or "chud session"
    return _truncate_title(first)


def _body_from_prompt(session_id: str, prompt: str) -> str:
    """Fallback body when no approved plan is available.

    The lead line is the user-configurable PR body footer template (default
    matches the legacy ``*Draft PR opened by chud session ...*`` blurb), so
    a custom template applies here too.
    """
    footer = render_pr_body_footer(session_id)
    return f"{footer}\n\nInitial prompt:\n\n```\n{prompt}\n```\n"


def pick_title(state: SessionState) -> str:
    """Prefer the approved plan's H1 heading; fall back to the prompt."""
    if state.approved_plan:
        plan_title = _extract_plan_title(state.approved_plan)
        if plan_title:
            return _truncate_title(plan_title)
    return _title_from_prompt(state.initial_prompt)


def pick_body(state: SessionState) -> str:
    """Prefer the plan's ``## Context`` section as the body lead, with a
    small footer pointing back to the chud session id. Fall back to the
    prompt-only body when no plan is available.

    When the session is linked to a GitHub issue, prepend a ``Closes #N``
    line so GitHub auto-populates the Development sidebar — this is the
    signal the new-session picker reads back via
    ``Issue.closing_pr_numbers`` to filter the issue out of future picker
    opens. Users can still strip the line in the PR-review modal if they
    don't want the auto-close behavior.
    """
    body = _pick_body_inner(state)
    if state.issue_number is not None:
        return f"Closes #{state.issue_number}\n\n{body}"
    return body


def _pick_body_inner(state: SessionState) -> str:
    if state.approved_plan:
        context = _extract_plan_context(state.approved_plan)
        if context:
            footer = render_pr_body_footer(state.id)
            return f"{context}\n\n---\n{footer}\n"
    return _body_from_prompt(state.id, state.initial_prompt)


def _count_commits_sync(worktree: Path, rev_range: str) -> int:
    """Count commits in ``rev_range`` inside ``worktree``.

    Returns ``-1`` on git failure so the caller can distinguish "0 commits"
    (legitimate empty branch) from "couldn't compute" (real error).
    """
    try:
        return sum(1 for _ in Repo(worktree).iter_commits(rev_range))
    except GitCommandError:
        return -1


def _fetch_origin_sync(worktree: Path, ref: str) -> tuple[bool, str]:
    """Best-effort ``git fetch origin <ref>`` from ``worktree``."""
    try:
        Repo(worktree).remotes.origin.fetch(ref)
    except GitCommandError as e:
        return False, str(e)
    return True, ""


def _push_branch_sync(worktree: Path, branch: str) -> tuple[bool, str]:
    """Push ``branch`` to ``origin`` with upstream tracking set.

    GitPython's ``PushInfo.flags`` is a bitfield; ``ERROR``/``REJECTED``/
    ``REMOTE_REJECTED`` all signal a real push failure. Surface the summary
    text so the UI gets the same kind of stderr blurb the CLI used to
    provide.
    """
    try:
        repo = Repo(worktree)
        push_infos = repo.remotes.origin.push(refspec=f"{branch}:{branch}", set_upstream=True)
    except GitCommandError as e:
        return False, str(e)
    for info in push_infos:
        error_flags = info.ERROR | info.REJECTED | info.REMOTE_REJECTED
        if info.flags & error_flags:
            summary = (info.summary or "push rejected").strip()
            return False, summary
    return True, ""


async def publish_draft_prs(
    state: SessionState,
    title: str | None = None,
    body: str | None = None,
) -> list[PRResult]:
    """For each attached worktree: push the chud branch and open a draft PR.

    ``title`` / ``body`` override the plan-derived defaults when supplied
    (e.g. after the user edits them in the PR-review modal). Both are applied
    verbatim to every attached repo's PR; per-repo overrides are not modeled
    yet.

    Returns one ``PRResult`` per attached repo. If no GitHub auth is
    available, returns a single failure result tagged with an empty repo
    label.
    """
    client = _get_client()
    if client is None:
        return [PRResult(repo_label="", branch="", error="no GitHub auth available")]

    title = title if title is not None else pick_title(state)
    body = body if body is not None else pick_body(state)
    results: list[PRResult] = []

    for label, wt in state.attached_repos.items():
        branch = wt.branch
        worktree = Path(wt.worktree_path)
        origin = Path(wt.repo_path)

        base = await _default_branch(origin)

        if await _is_dirty(worktree):
            ok, err = await _auto_commit(worktree, title, body)
            if not ok:
                results.append(
                    PRResult(
                        repo_label=label,
                        branch=branch,
                        error=f"auto-commit failed: {err}",
                    )
                )
                continue

        # Session-authorship gate. ``start_head`` is the worktree HEAD
        # captured by ``WorktreeManager.attach_repo`` *before* the agent
        # runs, so ``rev-list start_head..HEAD`` counts only commits that
        # this session produced. If a contaminated branch base means the
        # branch carries other sessions' commits but none of our own, the
        # ``origin/<base>..HEAD`` check below would still see those foreign
        # commits and proceed to publish a misleading PR — this gate stops
        # that. Skipped for legacy worktrees persisted before the field
        # existed (``start_head is None``); in that case the existing
        # ``origin/<base>..HEAD`` check is the only line of defense.
        if wt.start_head:
            session_count = await asyncio.to_thread(
                _count_commits_sync, worktree, f"{wt.start_head}..HEAD"
            )
            if session_count == 0:
                results.append(PRResult(repo_label=label, branch=branch, discarded=True))
                continue

        # Refresh ``origin/{base}`` before counting commits ahead. Without this,
        # a stale local ref (common when the user hasn't fetched in a while)
        # makes ``rev-list`` over-count, we push a branch whose tip already
        # exists on the remote, and ``pulls.create`` opens an empty-diff PR.
        # Best-effort: a fetch failure (offline, auth) is logged and we fall
        # through to the existing rev-list, where the real failure surfaces.
        ok, ferr = await asyncio.to_thread(_fetch_origin_sync, worktree, base)
        if not ok:
            log.warning("git fetch origin %s failed in %s: %s", base, worktree, ferr)

        ahead = await asyncio.to_thread(_count_commits_sync, worktree, f"origin/{base}..HEAD")
        if ahead < 0:
            results.append(
                PRResult(
                    repo_label=label,
                    branch=branch,
                    error=f"could not count commits vs origin/{base}",
                )
            )
            continue
        if ahead == 0:
            # Clean (no dirty edits) AND no commits ahead of base — this
            # branch is genuinely abandoned. Mark it for silent cleanup
            # rather than emitting a noisy PR_FAILED toast.
            results.append(PRResult(repo_label=label, branch=branch, discarded=True))
            continue

        ok, perr = await asyncio.to_thread(_push_branch_sync, worktree, branch)
        if not ok:
            results.append(
                PRResult(repo_label=label, branch=branch, error=f"git push failed: {perr}")
            )
            continue

        owner_name = _resolve_remote_owner_name(origin)
        if owner_name is None:
            results.append(
                PRResult(
                    repo_label=label,
                    branch=branch,
                    error="origin is not a recognized GitHub remote",
                )
            )
            continue
        owner, name = owner_name

        try:
            resp = await client.rest.pulls.async_create(
                owner,
                name,
                title=title,
                body=body,
                head=branch,
                base=base,
                draft=True,
            )
        except (GitHubException, httpx.HTTPError) as e:
            results.append(
                PRResult(repo_label=label, branch=branch, error=f"pulls.create failed: {e}")
            )
            continue

        url = getattr(resp.parsed_data, "html_url", None)
        results.append(PRResult(repo_label=label, branch=branch, url=str(url) if url else None))

    return results
