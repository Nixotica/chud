from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from chud.types import SessionState
from chud.worktree import WorktreeError, WorktreeManager, is_git_repo


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "t"], check=True, capture_output=True
    )
    (path / "README").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True
    )


def test_is_git_repo(tmp_path: Path):
    assert not is_git_repo(tmp_path)
    _init_repo(tmp_path)
    assert is_git_repo(tmp_path)


def test_attach_repo_creates_worktree(tmp_path: Path):
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    workspace = tmp_path / "ws"
    session = SessionState(id="sess1", workspace_dir=workspace)
    mgr = WorktreeManager(session)

    wt = mgr.attach_repo(repo)

    assert wt.worktree_path == workspace / "myrepo"
    assert wt.worktree_path.exists()
    assert (wt.worktree_path / "README").read_text() == "hi"
    assert wt.branch == "chud/sess1"


def test_attach_repo_idempotent(tmp_path: Path):
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(id="sess1", workspace_dir=tmp_path / "ws")
    mgr = WorktreeManager(session)

    wt1 = mgr.attach_repo(repo)
    wt2 = mgr.attach_repo(repo)

    assert wt1 == wt2


def test_attach_two_repos_same_basename_disambiguated(tmp_path: Path):
    a = tmp_path / "org-a" / "api"
    b = tmp_path / "org-b" / "api"
    _init_repo(a)
    _init_repo(b)
    session = SessionState(id="sess1", workspace_dir=tmp_path / "ws")
    mgr = WorktreeManager(session)

    wt_a = mgr.attach_repo(a)
    wt_b = mgr.attach_repo(b)

    assert wt_a.worktree_path.name == "api"
    assert wt_b.worktree_path.name == "api-2"
    assert wt_a.worktree_path.exists()
    assert wt_b.worktree_path.exists()


def test_detach_repo_removes_worktree(tmp_path: Path):
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(id="sess1", workspace_dir=tmp_path / "ws")
    mgr = WorktreeManager(session)
    wt = mgr.attach_repo(repo)
    assert wt.worktree_path.exists()

    mgr.detach_repo(str(repo))
    assert not wt.worktree_path.exists()
    assert str(repo) not in session.attached_repos


def test_detach_repo_with_dirty_worktree_requires_force(tmp_path: Path):
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(id="sess1", workspace_dir=tmp_path / "ws")
    mgr = WorktreeManager(session)
    wt = mgr.attach_repo(repo)
    (wt.worktree_path / "dirty.txt").write_text("uncommitted")

    with pytest.raises(WorktreeError):
        mgr.detach_repo(str(repo))

    mgr.detach_repo(str(repo), force=True)
    assert not wt.worktree_path.exists()
