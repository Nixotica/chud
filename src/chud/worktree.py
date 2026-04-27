from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from chud.settings import get_branch_prefix, get_include_slug
from chud.types import SessionState, Worktree

log = logging.getLogger(__name__)


class WorktreeError(RuntimeError):
    pass


_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SLUG_MAX_LEN = 40


def _slugify(text: str, max_len: int = _SLUG_MAX_LEN) -> str:
    """Make ``text`` safe for a git branch / filesystem path segment.

    Lowercases, replaces runs of non-alphanumerics with ``-``, strips leading
    and trailing ``-``, and truncates at the last ``-`` boundary that fits in
    ``max_len`` (falling back to a hard cut). Returns ``""`` if nothing
    survives normalization (e.g. emoji-only input) — callers decide the
    fallback name in that case.
    """
    if not text:
        return ""
    # Take only the first line; slugs from multi-line prompts get noisy fast.
    first_line = text.splitlines()[0]
    normalized = _SLUG_NON_ALNUM.sub("-", first_line.lower()).strip("-")
    if not normalized:
        return ""
    if len(normalized) <= max_len:
        return normalized
    truncated = normalized[:max_len]
    # Prefer to cut on a hyphen boundary so we don't slice a word in half.
    last_hyphen = truncated.rfind("-")
    if last_hyphen >= max_len // 2:
        truncated = truncated[:last_hyphen]
    return truncated.strip("-") or normalized[:max_len]


def is_git_repo(path: Path) -> bool:
    if not path.exists():
        return False
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def repo_toplevel(path: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


class WorktreeManager:
    """Per-session manager for the workspace dir and N attached worktrees."""

    def __init__(self, session: SessionState) -> None:
        self.session = session
        self.session.workspace_dir.mkdir(parents=True, exist_ok=True)

    def attach_repo(self, repo_path: Path) -> Worktree:
        """Add a worktree for `repo_path` under the session's workspace.

        Idempotent: if the repo is already attached, returns the existing Worktree.
        Disambiguates worktree dir name on basename collision across different repos.
        """
        toplevel = repo_toplevel(repo_path)
        repo_key = str(toplevel)

        if repo_key in self.session.attached_repos:
            return self.session.attached_repos[repo_key]

        basename = toplevel.name
        wt_dir_name = basename
        i = 2
        existing_dirs = {wt.worktree_path.name for wt in self.session.attached_repos.values()}
        while wt_dir_name in existing_dirs:
            wt_dir_name = f"{basename}-{i}"
            i += 1

        worktree_path = self.session.workspace_dir / wt_dir_name
        prefix = get_branch_prefix()
        if get_include_slug():
            slug = _slugify(self.session.initial_prompt) or "session"
            branch = f"{prefix}{slug}-{self.session.id}"
        else:
            branch = f"{prefix}{self.session.id}"

        branch_exists = (
            subprocess.run(
                ["git", "-C", str(toplevel), "rev-parse", "--verify", branch],
                capture_output=True,
            ).returncode
            == 0
        )

        # Drop stale "prunable" worktree entries (directory gone, .git/worktrees
        # metadata still present) before adding. Without this, a previous chud
        # session whose workspace was rm'd outside `git worktree remove` would
        # keep the branch "checked out" at a missing path and `git worktree
        # add` would fail with "<branch> is already checked out at <path>".
        subprocess.run(
            ["git", "-C", str(toplevel), "worktree", "prune"],
            capture_output=True,
        )

        cmd = ["git", "-C", str(toplevel), "worktree", "add"]
        if branch_exists:
            cmd += [str(worktree_path), branch]
        else:
            cmd += [str(worktree_path), "-b", branch]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise WorktreeError(f"git worktree add failed for {toplevel}: {result.stderr.strip()}")

        wt = Worktree(repo_path=toplevel, worktree_path=worktree_path, branch=branch)
        self.session.attached_repos[repo_key] = wt
        return wt

    def detach_repo(self, repo_key: str, force: bool = False) -> None:
        wt = self.session.attached_repos.get(repo_key)
        if wt is None:
            return

        cmd = ["git", "-C", str(wt.repo_path), "worktree", "remove", str(wt.worktree_path)]
        if force:
            cmd.append("--force")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 and not force:
            raise WorktreeError(
                f"git worktree remove failed: {result.stderr.strip()} (pass force=True to discard)"
            )

        del self.session.attached_repos[repo_key]

    def discard_empty_branch(self, repo_key: str) -> None:
        """Drop a chud branch that never received a commit.

        For worktrees where the agent did no work (or where a session was
        cancelled), the ``chud/<slug>-<sid>`` branch ref accumulates in the
        origin repo with no commits behind it. ``cleanup_workspace`` removes
        the worktree but leaves the branch ref dangling, so call this when
        you know the branch is empty: it force-detaches the worktree (so
        ``git branch -D`` won't complain that it's checked out), then deletes
        the branch ref. Best-effort on the branch deletion — anything other
        than a "not found" failure is logged but swallowed so a stuck branch
        ref doesn't block session cleanup.
        """
        wt = self.session.attached_repos.get(repo_key)
        if wt is None:
            return
        repo_path = wt.repo_path
        branch = wt.branch
        self.detach_repo(repo_key, force=True)
        result = subprocess.run(
            ["git", "-C", str(repo_path), "branch", "-D", branch],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "not found" not in stderr.lower():
                log.warning("git branch -D %s failed in %s: %s", branch, repo_path, stderr)

    def cleanup_workspace(self) -> None:
        """Remove all attached worktrees and the workspace dir. Destructive."""
        for repo_key in list(self.session.attached_repos):
            self.detach_repo(repo_key, force=True)
        if self.session.workspace_dir.exists():
            shutil.rmtree(self.session.workspace_dir, ignore_errors=True)
