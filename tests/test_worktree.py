from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from chud.types import SessionState
from chud.worktree import (
    WorktreeError,
    WorktreeManager,
    _slugify,
    detect_cwd_repo,
    is_git_repo,
)


@pytest.fixture(autouse=True)
def _isolated_worktrees_root(tmp_path_factory, monkeypatch):
    """Redirect ``worktrees_root()`` away from the real user data dir."""
    root = tmp_path_factory.mktemp("wt-root")
    monkeypatch.setattr("chud.worktree.worktrees_root", lambda: root)
    return root


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


def _init_repo_with_origin(repo: Path, bare: Path) -> None:
    """Init ``repo`` as a clone-of-``bare`` style setup with one commit on main.

    The bare repo at ``bare`` plays the role of a remote (``origin``); the
    local ``repo`` has it configured as ``origin``, has a local ``main``
    pushed, and ``origin/HEAD`` set so ``default_base_branch`` resolves
    cleanly. The result is the closest analog to a real cloned repo we can
    build without network.
    """
    bare.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    _init_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(bare)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "-u", "origin", "main"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-head", "origin", "main"],
        check=True,
        capture_output=True,
    )


def _commit(repo: Path, filename: str, contents: str, message: str) -> str:
    (repo / filename).write_text(contents)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message], check=True, capture_output=True
    )
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def test_is_git_repo(tmp_path: Path):
    assert not is_git_repo(tmp_path)
    _init_repo(tmp_path)
    assert is_git_repo(tmp_path)


def test_detect_cwd_repo_outside_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When CWD is not in a git repo, detect_cwd_repo() returns None — this
    is what tells the new-session flow to start a session unattached and let
    the agent attach repos via mcp__chud__attach_repo."""
    monkeypatch.chdir(tmp_path)
    assert detect_cwd_repo() is None


def test_detect_cwd_repo_inside_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When CWD is the toplevel of a git repo, detect_cwd_repo() returns
    that toplevel so the new-session flow can auto-attach it."""
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    monkeypatch.chdir(repo)
    detected = detect_cwd_repo()
    assert detected is not None
    assert detected.resolve() == repo.resolve()


def test_detect_cwd_repo_inside_repo_subdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """detect_cwd_repo() walks up from a subdirectory to the toplevel."""
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    detected = detect_cwd_repo()
    assert detected is not None
    assert detected.resolve() == repo.resolve()


def test_attach_repo_creates_worktree(tmp_path: Path, _isolated_worktrees_root: Path):
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(id="sess1", initial_prompt="add foo")
    mgr = WorktreeManager(session)

    wt = mgr.attach_repo(repo)

    assert wt.worktree_path == _isolated_worktrees_root / "sess1-myrepo"
    assert wt.worktree_path.exists()
    assert (wt.worktree_path / "README").read_text() == "hi"
    assert wt.branch == "chud/add-foo-sess1"


def test_attach_repo_idempotent(tmp_path: Path):
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(id="sess1", initial_prompt="add foo")
    mgr = WorktreeManager(session)

    wt1 = mgr.attach_repo(repo)
    wt2 = mgr.attach_repo(repo)

    assert wt1 == wt2


def test_attach_two_repos_same_basename_disambiguated(tmp_path: Path):
    a = tmp_path / "org-a" / "api"
    b = tmp_path / "org-b" / "api"
    _init_repo(a)
    _init_repo(b)
    session = SessionState(id="sess1", initial_prompt="add foo")
    mgr = WorktreeManager(session)

    wt_a = mgr.attach_repo(a)
    wt_b = mgr.attach_repo(b)

    assert wt_a.worktree_path.name == "sess1-api"
    assert wt_b.worktree_path.name == "sess1-api-2"
    assert wt_a.worktree_path.exists()
    assert wt_b.worktree_path.exists()


def test_attach_repo_branch_falls_back_when_prompt_empty(tmp_path: Path):
    """Empty prompt → branch uses the ``session`` literal as the slug."""
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(id="sess1", initial_prompt="")
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
    session = SessionState(id="sess1", initial_prompt="add foo")
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
        initial_prompt="Fix bug: 🚀 in API!",
    )
    mgr = WorktreeManager(session)

    wt = mgr.attach_repo(repo)

    assert wt.branch == "chud/fix-bug-in-api-sess1"


def test_detach_repo_removes_worktree(tmp_path: Path):
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(id="sess1", initial_prompt="add foo")
    mgr = WorktreeManager(session)
    wt = mgr.attach_repo(repo)
    assert wt.worktree_path.exists()

    mgr.detach_repo(str(repo))
    assert not wt.worktree_path.exists()
    assert str(repo) not in session.attached_repos


def test_detach_repo_with_dirty_worktree_requires_force(tmp_path: Path):
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(id="sess1", initial_prompt="add foo")
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
    session = SessionState(id="sess1", initial_prompt="add foo")
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


def test_discard_empty_branch_no_op_for_unknown_repo():
    """Discarding a repo that isn't attached is a silent no-op."""
    session = SessionState(id="sess1", initial_prompt="add foo")
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


def test_attach_repo_forks_from_origin_default_not_local_head(tmp_path: Path):
    """Reproducer for PR #51 contamination.

    Setup: a clone-of-origin where origin/main is at commit A, but the
    parent repo's local HEAD has been advanced to commit B (simulating a
    user who switched branches or a concurrent chud session that left WIP
    on top). The new chud branch must fork from ``origin/main`` (commit A),
    *not* the parent repo's current ``HEAD`` (commit B), so commit B's
    contents do not leak into the new branch.
    """
    repo = tmp_path / "myrepo"
    bare = tmp_path / "myrepo-bare.git"
    _init_repo_with_origin(repo, bare)
    a_oid = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    # Advance the parent repo's local HEAD past origin/main with a "WIP"
    # commit that hasn't been pushed.
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "wip-other-session"],
        check=True,
        capture_output=True,
    )
    b_oid = _commit(repo, "leaked.txt", "do not leak me", "wip from another session")
    assert a_oid != b_oid

    session = SessionState(id="sess1", initial_prompt="add foo")
    mgr = WorktreeManager(session)
    wt = mgr.attach_repo(repo)

    head = subprocess.run(
        ["git", "-C", str(wt.worktree_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == a_oid, "new chud branch must fork from origin/main, not local HEAD"
    assert not (wt.worktree_path / "leaked.txt").exists()


def test_attach_repo_records_start_head(tmp_path: Path):
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(id="sess1", initial_prompt="add foo")
    mgr = WorktreeManager(session)

    wt = mgr.attach_repo(repo)

    expected = subprocess.run(
        ["git", "-C", str(wt.worktree_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert wt.start_head is not None
    assert wt.start_head == expected
    assert len(wt.start_head) == 40  # full sha, not abbreviated


def test_attach_repo_falls_back_to_local_main_without_origin(tmp_path: Path):
    """Repo with no ``origin`` remote forks from local ``main``."""
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(id="sess1", initial_prompt="add foo")
    mgr = WorktreeManager(session)

    wt = mgr.attach_repo(repo)

    main_tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert wt.start_head == main_tip


def test_attach_repo_raises_when_no_base_branch_exists(tmp_path: Path):
    """Repo whose default-branch resolution lands on a non-existent ref → hard fail."""
    repo = tmp_path / "weirdrepo"
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", str(repo), "init", "-b", "develop"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True
    )
    (repo / "README").write_text("hi")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True
    )
    # No origin, no local "main" or "master" — only "develop" exists.
    # default_base_branch falls back to "main", which doesn't exist anywhere.
    session = SessionState(id="sess1", initial_prompt="add foo")
    mgr = WorktreeManager(session)

    with pytest.raises(WorktreeError, match="could not resolve a base ref"):
        mgr.attach_repo(repo)


def test_attach_repo_reattach_unchanged(tmp_path: Path):
    """Re-attaching the same repo yields the same Worktree, including ``start_head``."""
    repo = tmp_path / "myrepo"
    _init_repo(repo)
    session = SessionState(id="sess1", initial_prompt="add foo")
    mgr = WorktreeManager(session)

    wt1 = mgr.attach_repo(repo)
    wt2 = mgr.attach_repo(repo)

    assert wt1 is wt2
    assert wt1.start_head == wt2.start_head
    assert wt1.start_head is not None
