from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from chud.types import SessionState
from chud.worktree import WorktreeError, WorktreeManager, _slugify, is_git_repo


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
    session = SessionState(id="sess1", workspace_dir=workspace, initial_prompt="add foo")
    mgr = WorktreeManager(session)

    wt = mgr.attach_repo(repo)

    assert wt.worktree_path == workspace / "myrepo"
    assert wt.worktree_path.exists()
    assert (wt.worktree_path / "README").read_text() == "hi"
    assert wt.branch == "chud/add-foo-sess1"


def test_attach_repo_idempotent(tmp_path: Path):
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(
        id="sess1", workspace_dir=tmp_path / "ws", initial_prompt="add foo"
    )
    mgr = WorktreeManager(session)

    wt1 = mgr.attach_repo(repo)
    wt2 = mgr.attach_repo(repo)

    assert wt1 == wt2


def test_attach_two_repos_same_basename_disambiguated(tmp_path: Path):
    a = tmp_path / "org-a" / "api"
    b = tmp_path / "org-b" / "api"
    _init_repo(a)
    _init_repo(b)
    session = SessionState(
        id="sess1", workspace_dir=tmp_path / "ws", initial_prompt="add foo"
    )
    mgr = WorktreeManager(session)

    wt_a = mgr.attach_repo(a)
    wt_b = mgr.attach_repo(b)

    assert wt_a.worktree_path.name == "api"
    assert wt_b.worktree_path.name == "api-2"
    assert wt_a.worktree_path.exists()
    assert wt_b.worktree_path.exists()


def test_attach_repo_branch_falls_back_when_prompt_empty(tmp_path: Path):
    """Empty prompt → branch uses the ``session`` literal as the slug."""
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(id="sess1", workspace_dir=tmp_path / "ws", initial_prompt="")
    mgr = WorktreeManager(session)

    wt = mgr.attach_repo(repo)

    assert wt.branch == "chud/session-sess1"


def test_attach_repo_branch_uses_configured_prefix(tmp_path: Path, monkeypatch):
    """A custom branch_prefix setting flows into the new branch name."""
    import chud.worktree as wt_mod

    monkeypatch.setattr(wt_mod, "get_branch_prefix", lambda: "agent/")
    monkeypatch.setattr(wt_mod, "get_include_slug", lambda: True)

    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(
        id="sess1", workspace_dir=tmp_path / "ws", initial_prompt="add foo"
    )
    mgr = WorktreeManager(session)

    wt = mgr.attach_repo(repo)

    assert wt.branch == "agent/add-foo-sess1"


def test_attach_repo_branch_omits_slug_when_disabled(tmp_path: Path, monkeypatch):
    """include_slug=False yields ``<prefix><id>`` (no slug between)."""
    import chud.worktree as wt_mod

    monkeypatch.setattr(wt_mod, "get_branch_prefix", lambda: "chud/")
    monkeypatch.setattr(wt_mod, "get_include_slug", lambda: False)

    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(
        id="sess1",
        workspace_dir=tmp_path / "ws",
        initial_prompt="some prompt that would normally slug",
    )
    mgr = WorktreeManager(session)

    wt = mgr.attach_repo(repo)

    assert wt.branch == "chud/sess1"


def test_attach_repo_branch_handles_unicode_and_punctuation(tmp_path: Path):
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(
        id="sess1",
        workspace_dir=tmp_path / "ws",
        initial_prompt="Fix bug: 🚀 in API!",
    )
    mgr = WorktreeManager(session)

    wt = mgr.attach_repo(repo)

    assert wt.branch == "chud/fix-bug-in-api-sess1"


def test_detach_repo_removes_worktree(tmp_path: Path):
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(
        id="sess1", workspace_dir=tmp_path / "ws", initial_prompt="add foo"
    )
    mgr = WorktreeManager(session)
    wt = mgr.attach_repo(repo)
    assert wt.worktree_path.exists()

    mgr.detach_repo(str(repo))
    assert not wt.worktree_path.exists()
    assert str(repo) not in session.attached_repos


def test_detach_repo_with_dirty_worktree_requires_force(tmp_path: Path):
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(
        id="sess1", workspace_dir=tmp_path / "ws", initial_prompt="add foo"
    )
    mgr = WorktreeManager(session)
    wt = mgr.attach_repo(repo)
    (wt.worktree_path / "dirty.txt").write_text("uncommitted")

    with pytest.raises(WorktreeError):
        mgr.detach_repo(str(repo))

    mgr.detach_repo(str(repo), force=True)
    assert not wt.worktree_path.exists()


def test_discard_empty_branch_removes_worktree_and_branch_ref(tmp_path: Path):
    """Empty branch → worktree gone + ``chud/...`` branch ref deleted in origin."""
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(
        id="sess1", workspace_dir=tmp_path / "ws", initial_prompt="add foo"
    )
    mgr = WorktreeManager(session)
    wt = mgr.attach_repo(repo)
    assert wt.worktree_path.exists()
    # Sanity: the branch was just created in the origin.
    list_before = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", wt.branch],
        capture_output=True,
        text=True,
        check=True,
    )
    assert wt.branch in list_before.stdout

    mgr.discard_empty_branch(str(repo))

    # (a) worktree path gone
    assert not wt.worktree_path.exists()
    # (b) branch ref gone from origin
    list_after = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", wt.branch],
        capture_output=True,
        text=True,
        check=True,
    )
    assert wt.branch not in list_after.stdout
    # (c) attached_repos entry removed
    assert str(repo) not in session.attached_repos


def test_discard_empty_branch_no_op_for_unknown_repo(tmp_path: Path):
    """Discarding a repo that isn't attached is a silent no-op."""
    session = SessionState(
        id="sess1", workspace_dir=tmp_path / "ws", initial_prompt="add foo"
    )
    mgr = WorktreeManager(session)
    # Should not raise.
    mgr.discard_empty_branch("/does/not/exist")


def test_slugify_basic():
    assert _slugify("Add auth feature") == "add-auth-feature"


def test_slugify_strips_punctuation_and_unicode():
    assert _slugify("Fix bug: 🚀 in API!") == "fix-bug-in-api"


def test_slugify_returns_empty_for_emoji_only():
    assert _slugify("🚀🚀🚀") == ""


def test_slugify_only_uses_first_line():
    assert _slugify("first line\nsecond line should be ignored") == "first-line"


def test_slugify_truncates_at_hyphen_boundary():
    out = _slugify("one two three four five six seven eight nine ten eleven twelve")
    # max_len default is 40; result should not exceed it and should not end on a partial word.
    assert len(out) <= 40
    assert not out.endswith("-")
    # Should end on a complete word boundary, not mid-word.
    assert all(part for part in out.split("-"))
