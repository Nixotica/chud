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

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from chud.gh import _no_prompt_env, _run
from chud.settings import render_pr_body_footer
from chud.types import SessionState

log = logging.getLogger(__name__)


_TITLE_LIMIT = 72

# Re-exported so existing tests that monkeypatch ``chud.pr._run`` keep
# working without churn. Internal callers in this module still resolve
# ``_run`` through the local module dict, which the tests override.
__all__ = [
    "PRResult",
    "_no_prompt_env",
    "_run",
    "publish_draft_prs",
    "pick_title",
    "pick_body",
]


@dataclass
class PRResult:
    repo_label: str
    branch: str
    url: str | None = None
    error: str | None = None
    discarded: bool = False


async def _default_branch(repo_path: Path) -> str:
    """Resolve the remote's default branch; fall back to ``main``."""
    rc, out, _ = await _run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo_path,
    )
    if rc == 0 and "/" in out:
        return out.split("/", 1)[1]
    rc, out, _ = await _run(
        ["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
        cwd=repo_path,
    )
    if rc == 0 and out:
        return out
    return "main"


async def _is_dirty(worktree: Path) -> bool:
    """Return True iff ``worktree`` has any tracked changes or untracked files.

    Uses ``git status --porcelain``, which prints one line per modified or
    untracked path and nothing at all on a clean tree. A non-zero git exit
    is treated as "not dirty" so the caller falls through to the existing
    rev-list step, where the real failure surfaces with a useful message.
    """
    rc, out, _ = await _run(["git", "status", "--porcelain"], cwd=worktree)
    if rc != 0:
        return False
    return bool(out.strip())


async def _auto_commit(worktree: Path, title: str, body: str) -> tuple[bool, str]:
    """Stage and commit every change in ``worktree`` under one chud commit.

    The Claude SDK in ``acceptEdits`` mode edits files but never commits, so
    a session can finish with the agent's work sitting uncommitted. Without
    this helper, ``publish_draft_prs`` would see ``0`` commits ahead of base
    and discard the worktree as "abandoned" — losing the agent's work.

    Returns ``(ok, err_text)``. On failure, ``err_text`` carries the git
    stderr (e.g. "Please tell me who you are" when ``user.email`` is unset,
    or a pre-commit hook's rejection). Author identity is deferred to the
    user's local git config — fabricating a chud bot identity would silently
    make commits the user can't push under their own credentials.
    """
    rc, _, err = await _run(["git", "add", "-A"], cwd=worktree)
    if rc != 0:
        return False, err or "git add failed"
    rc, _, err = await _run(["git", "commit", "-m", title, "-m", body], cwd=worktree)
    if rc != 0:
        return False, err or "git commit failed"
    return True, ""


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


# Backwards-compatible aliases for any callers (and tests) still using the
# original underscore-prefixed names. Safe to remove once no internal user
# remains.
_pick_title = pick_title
_pick_body = pick_body


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

    Returns one ``PRResult`` per attached repo. If ``gh`` isn't on PATH, returns
    a single failure result tagged with an empty repo label.
    """
    if shutil.which("gh") is None:
        return [PRResult(repo_label="", branch="", error="gh CLI not installed")]

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

        # Refresh ``origin/{base}`` before counting commits ahead. Without this,
        # a stale local ref (common when the user hasn't fetched in a while)
        # makes ``rev-list`` over-count, we push a branch whose tip already
        # exists on the remote, and ``gh pr create`` opens an empty-diff PR.
        # Best-effort: a fetch failure (offline, auth) is logged and we fall
        # through to the existing rev-list, where the real failure surfaces.
        rc, _, ferr = await _run(["git", "fetch", "origin", base], cwd=worktree)
        if rc != 0:
            log.warning("git fetch origin %s failed in %s: %s", base, worktree, ferr.strip())

        rc, count, err = await _run(
            ["git", "rev-list", "--count", f"origin/{base}..HEAD"],
            cwd=worktree,
        )
        if rc != 0:
            results.append(
                PRResult(
                    repo_label=label,
                    branch=branch,
                    error=f"could not count commits vs origin/{base}: {err or count}",
                )
            )
            continue
        if count.strip() == "0":
            # We just confirmed clean (no dirty edits) AND no commits ahead
            # of base — this branch is genuinely abandoned. Mark it for
            # silent cleanup rather than emitting a noisy PR_FAILED toast.
            results.append(PRResult(repo_label=label, branch=branch, discarded=True))
            continue

        rc, _, err = await _run(
            ["git", "push", "-u", "origin", branch],
            cwd=worktree,
        )
        if rc != 0:
            results.append(
                PRResult(repo_label=label, branch=branch, error=f"git push failed: {err}")
            )
            continue

        rc, url, err = await _run(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--base",
                base,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=origin,
        )
        if rc != 0:
            results.append(
                PRResult(repo_label=label, branch=branch, error=f"gh pr create failed: {err}")
            )
            continue

        results.append(PRResult(repo_label=label, branch=branch, url=url or None))

    return results
