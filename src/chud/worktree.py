from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from chud.types import SessionState, Worktree


class WorktreeError(RuntimeError):
    pass


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

        # disambiguate dir name if another repo with same basename is already attached
        basename = toplevel.name
        wt_dir_name = basename
        i = 2
        existing_dirs = {wt.worktree_path.name for wt in self.session.attached_repos.values()}
        while wt_dir_name in existing_dirs:
            wt_dir_name = f"{basename}-{i}"
            i += 1

        worktree_path = self.session.workspace_dir / wt_dir_name
        branch = f"chud/{self.session.id}"

        # if branch already exists in target repo (rare; same session attaching a repo
        # we've previously detached and re-attached), reuse it
        branch_exists = (
            subprocess.run(
                ["git", "-C", str(toplevel), "rev-parse", "--verify", branch],
                capture_output=True,
            ).returncode
            == 0
        )

        cmd = ["git", "-C", str(toplevel), "worktree", "add"]
        if branch_exists:
            cmd += [str(worktree_path), branch]
        else:
            cmd += [str(worktree_path), "-b", branch]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise WorktreeError(
                f"git worktree add failed for {toplevel}: {result.stderr.strip()}"
            )

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
                f"git worktree remove failed: {result.stderr.strip()} "
                f"(pass force=True to discard)"
            )

        del self.session.attached_repos[repo_key]

    def cleanup_workspace(self) -> None:
        """Remove all attached worktrees and the workspace dir. Destructive."""
        for repo_key in list(self.session.attached_repos):
            self.detach_repo(repo_key, force=True)
        if self.session.workspace_dir.exists():
            shutil.rmtree(self.session.workspace_dir, ignore_errors=True)
