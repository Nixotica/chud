from __future__ import annotations

from pathlib import Path

import pytest

from chud import pr as pr_mod
from chud.types import SessionState, Worktree


def _state_with_one_repo(approved_plan: str | None = None) -> SessionState:
    s = SessionState(
        id="sess1",
        workspace_dir=Path("/tmp/chud-ws/sess1"),
        initial_prompt="add a foo",
        approved_plan=approved_plan,
    )
    s.attached_repos["myrepo"] = Worktree(
        repo_path=Path("/tmp/myrepo"),
        worktree_path=Path("/tmp/chud-ws/sess1/myrepo"),
        branch="chud/add-a-foo-sess1",
    )
    return s


# --- fallback path: prompt-only ----------------------------------------------


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


def test_pick_title_falls_back_to_prompt_when_no_plan():
    s = _state_with_one_repo()
    assert pr_mod._pick_title(s) == "add a foo"


def test_pick_body_falls_back_to_prompt_when_no_plan():
    s = _state_with_one_repo()
    body = pr_mod._pick_body(s)
    assert "sess1" in body
    assert "add a foo" in body


# --- plan-driven path --------------------------------------------------------


def test_extract_plan_title_finds_h1():
    plan = "# Add auth feature\n\n## Context\n\nWhy.\n"
    assert pr_mod._extract_plan_title(plan) == "Add auth feature"


def test_extract_plan_title_ignores_h2_and_deeper():
    plan = "## Context\n\nReason.\n\n# Real Title\n\n## Approach\n"
    assert pr_mod._extract_plan_title(plan) == "Real Title"


def test_extract_plan_title_returns_none_when_missing():
    assert pr_mod._extract_plan_title("no headings here") is None
    assert pr_mod._extract_plan_title("") is None


def test_extract_plan_context_captures_section_body():
    plan = (
        "# Title\n\n"
        "## Context\n\n"
        "First paragraph.\n\nSecond paragraph.\n\n"
        "## Approach\n\nDo X.\n"
    )
    body = pr_mod._extract_plan_context(plan)
    assert body is not None
    assert body.startswith("First paragraph.")
    assert "Second paragraph." in body
    assert "Do X." not in body
    assert "## Approach" not in body


def test_extract_plan_context_case_insensitive_heading():
    plan = "# T\n\n## context\n\nlowercase heading body.\n\n## Approach\n"
    assert pr_mod._extract_plan_context(plan) == "lowercase heading body."


def test_extract_plan_context_returns_none_when_missing():
    plan = "# Title\n\n## Approach\n\nDo X.\n"
    assert pr_mod._extract_plan_context(plan) is None


def test_pick_title_uses_plan_heading_when_present():
    s = _state_with_one_repo(approved_plan="# Add auth feature\n\n## Context\n\nWhy.\n")
    assert pr_mod._pick_title(s) == "Add auth feature"


def test_pick_title_truncates_long_plan_heading():
    long_title = "# " + "x" * 200 + "\n"
    s = _state_with_one_repo(approved_plan=long_title)
    out = pr_mod._pick_title(s)
    assert len(out) <= 72
    assert out.endswith("…")


def test_pick_title_falls_back_when_plan_has_no_h1():
    s = _state_with_one_repo(approved_plan="## Context\n\nNo H1 here.\n")
    assert pr_mod._pick_title(s) == "add a foo"


def test_pick_body_uses_plan_context_when_present():
    plan = "# Title\n\n## Context\n\nBecause reasons.\n\n## Approach\n\nDo X.\n"
    s = _state_with_one_repo(approved_plan=plan)
    body = pr_mod._pick_body(s)
    assert body.startswith("Because reasons.")
    assert "## Approach" not in body
    assert "Do X." not in body
    # Footer references the chud session id.
    assert "sess1" in body
    # Verbatim prompt should NOT lead the body anymore.
    assert not body.startswith("Draft PR opened by chud session")


def test_pick_body_falls_back_when_plan_missing_context():
    s = _state_with_one_repo(approved_plan="# Title\n\n## Approach\n\nDo X.\n")
    body = pr_mod._pick_body(s)
    # Falls back to the legacy boilerplate body.
    assert body.startswith("Draft PR opened by chud session")
    assert "add a foo" in body


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
    assert results[0].branch == "chud/add-a-foo-sess1"


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
