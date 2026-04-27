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
import contextlib
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from chud.types import SessionState

log = logging.getLogger(__name__)


_TITLE_LIMIT = 72

# A real GitHub push completes in a few seconds; anything past this is a
# hang, typically a credential prompt that the TUI's raw-mode terminal
# can't satisfy. Without this guard, _publish_prs parks forever and the
# post-DONE CLEANUP_REQUESTED broadcast never fires.
_RUN_TIMEOUT_S = 60.0


@dataclass
class PRResult:
    repo_label: str
    branch: str
    url: str | None = None
    error: str | None = None
    discarded: bool = False


def _no_prompt_env() -> dict[str, str]:
    """Env that forbids git/ssh from waiting on an interactive credential prompt.

    Without these, ``git push`` inheriting the TUI's TTY can either block on
    stdin (raw mode swallows the keypresses git expects) or scribble a
    password prompt onto the screen — both manifest as "TUI got laggy and
    nothing happened." With them set, git/ssh fail fast with an auth error
    that surfaces as a PR_FAILED toast.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "/bin/true")
    env.setdefault("SSH_ASKPASS", "/bin/true")
    env.setdefault("SSH_ASKPASS_REQUIRE", "never")
    return env


async def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a subprocess, returning (returncode, stdout, stderr).

    stdin is wired to /dev/null so git/gh can never read from the parent
    TTY, and a 60s wall-clock timeout kills any process that still hangs
    despite that — preserving the invariant that ``_finish_done`` always
    reaches its CLEANUP_REQUESTED broadcast.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd is not None else None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_no_prompt_env(),
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=_RUN_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.warning("pr._run timeout after %.0fs: %s", _RUN_TIMEOUT_S, cmd)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return (-1, "", f"timed out after {_RUN_TIMEOUT_S:.0f}s")
    return (
        proc.returncode if proc.returncode is not None else -1,
        out_b.decode(errors="replace").strip(),
        err_b.decode(errors="replace").strip(),
    )


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
    """Fallback body when no approved plan is available."""
    return (
        f"Draft PR opened by chud session `{session_id}`.\n\n"
        f"Initial prompt:\n\n```\n{prompt}\n```\n"
    )


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
    """
    if state.approved_plan:
        context = _extract_plan_context(state.approved_plan)
        if context:
            return f"{context}\n\n---\n*Draft PR opened by chud session `{state.id}`.*\n"
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
