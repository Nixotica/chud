from __future__ import annotations

from pathlib import Path

import pytest

from chud import pr as pr_mod
from chud.types import SessionState, Worktree


def _state_with_one_repo() -> SessionState:
    s = SessionState(
        id="sess1",
        workspace_dir=Path("/tmp/chud-ws/sess1"),
        initial_prompt="add a foo",
    )
    s.attached_repos["myrepo"] = Worktree(
        repo_path=Path("/tmp/myrepo"),
        worktree_path=Path("/tmp/chud-ws/sess1/myrepo"),
        branch="chud/sess1",
    )
    return s


def test_title_from_prompt_uses_first_line():
    assert pr_mod._title_from_prompt("first line\nsecond line") == "first line"


def test_title_from_prompt_truncates_long_input():
    long = "x" * 200
    out = pr_mod._title_from_prompt(long)
    assert len(out) <= 72
    assert out.endswith("…")


def test_title_from_prompt_handles_empty():
    assert pr_mod._title_from_prompt("") == "chud session"
    assert pr_mod._title_from_prompt("   \n\n  ") == "chud session"


def test_body_includes_session_id_and_prompt():
    body = pr_mod._body_from_prompt("abcdef", "do the thing")
    assert "abcdef" in body
    assert "do the thing" in body


@pytest.mark.asyncio
async def test_publish_draft_prs_when_gh_missing(monkeypatch):
    monkeypatch.setattr(pr_mod.shutil, "which", lambda _: None)
    results = await pr_mod.publish_draft_prs(_state_with_one_repo())
    assert len(results) == 1
    assert results[0].url is None
    assert results[0].error is not None
    assert "gh" in results[0].error.lower()


@pytest.mark.asyncio
async def test_publish_draft_prs_skips_when_no_commits(monkeypatch):
    """rev-list reports 0 commits → skip push, return descriptive error."""
    monkeypatch.setattr(pr_mod.shutil, "which", lambda _: "/usr/bin/gh")

    calls: list[list[str]] = []

    async def fake_run(cmd, cwd=None):
        calls.append(cmd)
        if cmd[:2] == ["git", "symbolic-ref"]:
            return 0, "origin/main", ""
        if cmd[:3] == ["git", "rev-list", "--count"]:
            return 0, "0", ""
        # Anything else means we accidentally tried to push or open a PR.
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(pr_mod, "_run", fake_run)

    results = await pr_mod.publish_draft_prs(_state_with_one_repo())
    assert len(results) == 1
    assert results[0].url is None
    assert results[0].error is not None
    assert "no commits" in results[0].error
    # Verify we never attempted a push or PR creation.
    assert not any(c[:2] == ["git", "push"] for c in calls)
    assert not any(c[:1] == ["gh"] for c in calls)


@pytest.mark.asyncio
async def test_publish_draft_prs_happy_path(monkeypatch):
    monkeypatch.setattr(pr_mod.shutil, "which", lambda _: "/usr/bin/gh")

    async def fake_run(cmd, cwd=None):
        if cmd[:2] == ["git", "symbolic-ref"]:
            return 0, "origin/main", ""
        if cmd[:3] == ["git", "rev-list", "--count"]:
            return 0, "3", ""
        if cmd[:3] == ["git", "push", "-u"]:
            return 0, "", ""
        if cmd[:1] == ["gh"]:
            return 0, "https://github.com/x/y/pull/42", ""
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(pr_mod, "_run", fake_run)

    results = await pr_mod.publish_draft_prs(_state_with_one_repo())
    assert len(results) == 1
    assert results[0].error is None
    assert results[0].url == "https://github.com/x/y/pull/42"
    assert results[0].branch == "chud/sess1"


@pytest.mark.asyncio
async def test_publish_draft_prs_reports_push_failure(monkeypatch):
    monkeypatch.setattr(pr_mod.shutil, "which", lambda _: "/usr/bin/gh")

    async def fake_run(cmd, cwd=None):
        if cmd[:2] == ["git", "symbolic-ref"]:
            return 0, "origin/main", ""
        if cmd[:3] == ["git", "rev-list", "--count"]:
            return 0, "1", ""
        if cmd[:3] == ["git", "push", "-u"]:
            return 1, "", "permission denied"
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(pr_mod, "_run", fake_run)

    results = await pr_mod.publish_draft_prs(_state_with_one_repo())
    assert len(results) == 1
    assert results[0].url is None
    assert "push failed" in (results[0].error or "")


@pytest.mark.asyncio
async def test_default_branch_falls_back_to_main(monkeypatch):
    async def fake_run(cmd, cwd=None):
        # Both lookups fail / return nothing useful → fallback.
        return 1, "", "fatal"

    monkeypatch.setattr(pr_mod, "_run", fake_run)
    assert await pr_mod._default_branch(Path("/tmp/anywhere")) == "main"
