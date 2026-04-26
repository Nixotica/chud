"""Publish draft pull requests for a finished session's worktrees.

Triggered by ``SessionManager`` when a session reaches DONE and the
``make_draft_pr`` option is set. Each attached worktree gets its own PR
against its origin's default branch. Failures are reported per-repo via
``PRResult`` (and surfaced to the UI as ``PR_FAILED`` events) so a single
broken push doesn't sink the whole batch.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from chud.types import SessionState

log = logging.getLogger(__name__)


_TITLE_LIMIT = 72


@dataclass
class PRResult:
    repo_label: str
    branch: str
    url: str | None = None
    error: str | None = None


async def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a subprocess, returning (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
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


def _title_from_prompt(prompt: str) -> str:
    text = prompt.strip()
    if not text:
        return "chud session"
    first = text.splitlines()[0].strip() or "chud session"
    if len(first) > _TITLE_LIMIT:
        return first[: _TITLE_LIMIT - 1] + "…"
    return first


def _body_from_prompt(session_id: str, prompt: str) -> str:
    return (
        f"Draft PR opened by chud session `{session_id}`.\n\n"
        f"Initial prompt:\n\n```\n{prompt}\n```\n"
    )


async def publish_draft_prs(state: SessionState) -> list[PRResult]:
    """For each attached worktree: push the chud branch and open a draft PR.

    Returns one ``PRResult`` per attached repo. If ``gh`` isn't on PATH, returns
    a single failure result tagged with an empty repo label.
    """
    if shutil.which("gh") is None:
        return [PRResult(repo_label="", branch="", error="gh CLI not installed")]

    title = _title_from_prompt(state.initial_prompt)
    body = _body_from_prompt(state.id, state.initial_prompt)
    results: list[PRResult] = []

    for label, wt in state.attached_repos.items():
        branch = wt.branch
        worktree = Path(wt.worktree_path)
        origin = Path(wt.repo_path)

        base = await _default_branch(origin)

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
            results.append(
                PRResult(
                    repo_label=label,
                    branch=branch,
                    error=f"no commits on {branch} ahead of origin/{base}",
                )
            )
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
