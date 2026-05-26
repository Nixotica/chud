"""Tests for ``chud.pr`` — title/body extraction and the PR-publish flow.

The pure helpers (``_extract_plan_title``, ``pick_title``, ``pick_body``,
etc.) are exercised directly. The async publish flow is tested by
monkeypatching the GitPython helpers (``_is_dirty``, ``_auto_commit``,
``_count_commits_sync``, ``_fetch_origin_sync``, ``_push_branch_sync``)
and the githubkit client returned by ``_get_client`` so no real git or
network call is made.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chud import gh as gh_mod
from chud import pr as pr_mod
from chud.types import SessionState, Worktree


def _state_with_one_repo(approved_plan: str | None = None) -> SessionState:
    s = SessionState(
        id="sess1",
        initial_prompt="add a foo",
        approved_plan=approved_plan,
    )
    s.attached_repos["myrepo"] = Worktree(
        repo_path=Path("/tmp/myrepo"),
        worktree_path=Path("/tmp/chud-wt/sess1-myrepo"),
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
    assert pr_mod.pick_title(s) == "add a foo"


def test_pick_body_falls_back_to_prompt_when_no_plan():
    s = _state_with_one_repo()
    body = pr_mod.pick_body(s)
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
        "# Title\n\n## Context\n\nFirst paragraph.\n\nSecond paragraph.\n\n## Approach\n\nDo X.\n"
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
    assert pr_mod.pick_title(s) == "Add auth feature"


def test_pick_title_truncates_long_plan_heading():
    long_title = "# " + "x" * 200 + "\n"
    s = _state_with_one_repo(approved_plan=long_title)
    out = pr_mod.pick_title(s)
    assert len(out) <= 72
    assert out.endswith("…")


def test_pick_title_falls_back_when_plan_has_no_h1():
    s = _state_with_one_repo(approved_plan="## Context\n\nNo H1 here.\n")
    assert pr_mod.pick_title(s) == "add a foo"


def test_pick_body_uses_plan_context_when_present():
    plan = "# Title\n\n## Context\n\nBecause reasons.\n\n## Approach\n\nDo X.\n"
    s = _state_with_one_repo(approved_plan=plan)
    body = pr_mod.pick_body(s)
    assert body.startswith("Because reasons.")
    assert "## Approach" not in body
    assert "Do X." not in body
    # Footer references the chud session id.
    assert "sess1" in body
    # Verbatim prompt should NOT lead the body anymore.
    assert not body.startswith("Draft PR opened by chud session")


def test_pick_body_falls_back_when_plan_missing_context():
    s = _state_with_one_repo(approved_plan="# Title\n\n## Approach\n\nDo X.\n")
    body = pr_mod.pick_body(s)
    # Falls back to the legacy boilerplate body — still mentions chud,
    # session id, and the original prompt.
    assert "Draft PR opened by chud session" in body
    assert "sess1" in body
    assert "add a foo" in body


def test_pick_body_uses_configured_footer_template(monkeypatch):
    """A custom ``pr_body_footer`` setting flows into the rendered body."""
    monkeypatch.setattr(pr_mod, "render_pr_body_footer", lambda sid: f"<<chud:{sid}>>")
    plan = "# Title\n\n## Context\n\nReasons.\n\n## Approach\n\nDo X.\n"
    s = _state_with_one_repo(approved_plan=plan)
    body = pr_mod.pick_body(s)
    assert "<<chud:sess1>>" in body
    # Default footer text is gone when a template override is in play.
    assert "*Draft PR opened by chud session" not in body


def test_pick_body_prepends_closes_when_issue_linked():
    """``Closes #N`` lands at the top of the body when the session is linked
    to a GitHub issue. This tells GitHub to wire up the Development sidebar
    so the issue is filtered from the new-session picker on subsequent opens.
    """
    plan = "# Title\n\n## Context\n\nReasons.\n"
    s = _state_with_one_repo(approved_plan=plan)
    s.issue_number = 42
    body = pr_mod.pick_body(s)
    assert body.startswith("Closes #42\n\n")


def test_pick_body_omits_closes_when_no_issue_linked():
    """No issue → no ``Closes`` line; body is unchanged from the legacy shape."""
    plan = "# Title\n\n## Context\n\nReasons.\n"
    s = _state_with_one_repo(approved_plan=plan)
    body = pr_mod.pick_body(s)
    assert not body.startswith("Closes #")
    assert body.startswith("Reasons.")


def test_body_from_prompt_uses_configured_footer_template(monkeypatch):
    monkeypatch.setattr(pr_mod, "render_pr_body_footer", lambda sid: f"FOOTER[{sid}]")
    body = pr_mod._body_from_prompt("xyz", "do the thing")
    assert body.startswith("FOOTER[xyz]")
    assert "do the thing" in body


# --- publish_draft_prs harness -----------------------------------------------


@pytest.fixture
def fake_publish_env(monkeypatch):
    """Stand up a programmable environment for ``publish_draft_prs`` tests.

    Replaces the GitPython helpers and the githubkit client with stubs
    whose return values can be tweaked per-test. Returns a ``state`` mapping
    that test bodies mutate to drive the flow.
    """
    state: dict[str, Any] = {
        "is_dirty": False,
        "auto_commit": (True, ""),
        "fetch": (True, ""),
        "push": (True, ""),
        # rev_range → count. Default: counts ahead of base; session-authorship
        # gate is skipped unless a start_head case is set.
        "counts": {},
        # PR creation
        "pr_url": "https://github.com/x/y/pull/7",
        "pr_raises": None,
        # default-branch lookup
        "default_branch": "main",
        # owner/name resolution
        "owner_name": ("x", "y"),
    }
    calls: dict[str, list[Any]] = {
        "is_dirty": [],
        "auto_commit": [],
        "fetch": [],
        "push": [],
        "counts": [],
        "pr_create": [],
    }

    async def fake_is_dirty(worktree: Path) -> bool:
        calls["is_dirty"].append(worktree)
        return bool(state["is_dirty"])

    async def fake_auto_commit(worktree: Path, title: str, body: str) -> tuple[bool, str]:
        calls["auto_commit"].append((worktree, title, body))
        return state["auto_commit"]

    def fake_fetch(worktree: Path, ref: str) -> tuple[bool, str]:
        calls["fetch"].append((worktree, ref))
        return state["fetch"]

    def fake_push(worktree: Path, branch: str) -> tuple[bool, str]:
        calls["push"].append((worktree, branch))
        return state["push"]

    def fake_count(worktree: Path, rev_range: str) -> int:
        calls["counts"].append((worktree, rev_range))
        return state["counts"].get(rev_range, 1)

    async def fake_default_branch(_: Path) -> str:
        return state["default_branch"]

    async def fake_pulls_create(
        owner: str,
        name: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool,
    ) -> SimpleNamespace:
        calls["pr_create"].append(
            {
                "owner": owner,
                "name": name,
                "title": title,
                "body": body,
                "head": head,
                "base": base,
                "draft": draft,
            }
        )
        if state["pr_raises"] is not None:
            raise state["pr_raises"]
        return SimpleNamespace(parsed_data=SimpleNamespace(html_url=state["pr_url"]))

    fake_client = SimpleNamespace(
        rest=SimpleNamespace(
            pulls=SimpleNamespace(async_create=fake_pulls_create),
        ),
    )

    monkeypatch.setattr(pr_mod, "_is_dirty", fake_is_dirty)
    monkeypatch.setattr(pr_mod, "_auto_commit", fake_auto_commit)
    monkeypatch.setattr(pr_mod, "_fetch_origin_sync", fake_fetch)
    monkeypatch.setattr(pr_mod, "_push_branch_sync", fake_push)
    monkeypatch.setattr(pr_mod, "_count_commits_sync", fake_count)
    monkeypatch.setattr(pr_mod, "_default_branch", fake_default_branch)
    monkeypatch.setattr(pr_mod, "_get_client", lambda: fake_client)
    monkeypatch.setattr(pr_mod, "_resolve_remote_owner_name", lambda _: state["owner_name"])
    # Also reset the gh module's client cache so any stray reads don't leak.
    monkeypatch.setattr(gh_mod, "_get_client", lambda: fake_client)

    return SimpleNamespace(state=state, calls=calls)


async def test_publish_draft_prs_when_no_auth(monkeypatch):
    monkeypatch.setattr(pr_mod, "_get_client", lambda: None)
    results = await pr_mod.publish_draft_prs(_state_with_one_repo())
    assert len(results) == 1
    assert results[0].url is None
    assert results[0].error is not None
    assert "auth" in results[0].error.lower()


async def test_publish_draft_prs_discards_clean_empty_branch(fake_publish_env):
    """Clean worktree + 0 commits → discarded=True, no error, no push/PR."""
    fake_publish_env.state["is_dirty"] = False
    fake_publish_env.state["counts"] = {"origin/main..HEAD": 0}

    results = await pr_mod.publish_draft_prs(_state_with_one_repo())
    assert len(results) == 1
    assert results[0].url is None
    assert results[0].error is None
    assert results[0].discarded is True
    assert fake_publish_env.calls["push"] == []
    assert fake_publish_env.calls["pr_create"] == []
    assert fake_publish_env.calls["auto_commit"] == []


async def test_publish_draft_prs_auto_commits_dirty_worktree(fake_publish_env):
    """Dirty worktree → auto-commit fires, then push + PR open succeed."""
    fake_publish_env.state["is_dirty"] = True
    fake_publish_env.state["counts"] = {"origin/main..HEAD": 1}

    results = await pr_mod.publish_draft_prs(_state_with_one_repo())
    assert len(results) == 1
    assert results[0].error is None
    assert results[0].url == "https://github.com/x/y/pull/7"
    assert results[0].discarded is False

    assert len(fake_publish_env.calls["auto_commit"]) == 1
    # Subject derived from prompt fallback ("add a foo"); not empty.
    _, title, _ = fake_publish_env.calls["auto_commit"][0]
    assert title == "add a foo"


async def test_publish_draft_prs_auto_commit_uses_plan_title(fake_publish_env):
    """When an approved plan exists, its H1 becomes the commit subject."""
    fake_publish_env.state["is_dirty"] = True
    fake_publish_env.state["counts"] = {"origin/main..HEAD": 1}

    state = _state_with_one_repo(approved_plan="# Fix the parser\n\n## Context\n\nIt was wrong.\n")
    results = await pr_mod.publish_draft_prs(state)
    assert results[0].error is None
    _, title, _ = fake_publish_env.calls["auto_commit"][0]
    assert title == "Fix the parser"


async def test_publish_draft_prs_auto_commit_failure_surfaces_as_error(fake_publish_env):
    """A pre-commit hook (or any commit failure) becomes a PR_FAILED error."""
    fake_publish_env.state["is_dirty"] = True
    fake_publish_env.state["auto_commit"] = (False, "pre-commit hook rejected: lint failed")

    results = await pr_mod.publish_draft_prs(_state_with_one_repo())
    assert len(results) == 1
    assert results[0].url is None
    assert results[0].discarded is False
    assert results[0].error is not None
    assert results[0].error.startswith("auto-commit failed:")
    assert "pre-commit hook rejected" in results[0].error


async def test_publish_draft_prs_happy_path(fake_publish_env):
    fake_publish_env.state["is_dirty"] = False
    fake_publish_env.state["counts"] = {"origin/main..HEAD": 3}

    results = await pr_mod.publish_draft_prs(_state_with_one_repo())
    assert len(results) == 1
    assert results[0].error is None
    assert results[0].url == "https://github.com/x/y/pull/7"
    assert results[0].branch == "chud/add-a-foo-sess1"


async def test_publish_draft_prs_reports_push_failure(fake_publish_env):
    fake_publish_env.state["counts"] = {"origin/main..HEAD": 1}
    fake_publish_env.state["push"] = (False, "permission denied")

    results = await pr_mod.publish_draft_prs(_state_with_one_repo())
    assert len(results) == 1
    assert results[0].url is None
    assert "push failed" in (results[0].error or "")


async def test_publish_draft_prs_uses_override_title_and_body(fake_publish_env):
    """Caller-supplied title/body must reach `pulls.create` verbatim."""
    fake_publish_env.state["counts"] = {"origin/main..HEAD": 1}

    results = await pr_mod.publish_draft_prs(
        _state_with_one_repo(),
        title="user-edited title",
        body="user-edited body",
    )
    assert len(results) == 1 and results[0].url == "https://github.com/x/y/pull/7"

    create_calls = fake_publish_env.calls["pr_create"]
    assert len(create_calls) == 1
    assert create_calls[0]["title"] == "user-edited title"
    assert create_calls[0]["body"] == "user-edited body"
    # And the H1/Context defaults should NOT have been used.
    assert "add a foo" not in create_calls[0]["title"]
    assert "add a foo" not in create_calls[0]["body"]


async def test_default_branch_falls_back_to_main(monkeypatch):
    monkeypatch.setattr(pr_mod, "_get_client", lambda: None)
    assert await pr_mod._default_branch(Path("/tmp/anywhere")) == "main"


async def test_publish_fetches_origin_base_before_counting_commits(fake_publish_env):
    """Regression: a stale local ``origin/{base}`` ref used to make rev-list
    over-count and produce empty-diff PRs. ``publish_draft_prs`` must run
    the equivalent of ``git fetch origin {base}`` before the count."""
    fake_publish_env.state["counts"] = {"origin/main..HEAD": 1}

    results = await pr_mod.publish_draft_prs(_state_with_one_repo())
    assert results[0].error is None

    fetch_calls = fake_publish_env.calls["fetch"]
    assert len(fetch_calls) == 1
    assert fetch_calls[0][1] == "main"


async def test_publish_continues_when_fetch_fails(fake_publish_env):
    """A fetch failure (offline, auth) must not block the PR flow — log and
    fall through to the count, which surfaces the real error if any."""
    fake_publish_env.state["fetch"] = (False, "Could not resolve host: github.com")
    fake_publish_env.state["counts"] = {"origin/main..HEAD": 2}

    results = await pr_mod.publish_draft_prs(_state_with_one_repo())
    assert results[0].error is None
    assert results[0].url == "https://github.com/x/y/pull/7"


# --- session-authorship gate (Bug 2 fix) -------------------------------------

_SESSION_START_OID = "abc123abc123abc123abc123abc123abc123abc1"


def _state_with_start_head(start_head: str | None) -> SessionState:
    s = SessionState(id="sess1", initial_prompt="add a foo")
    s.attached_repos["myrepo"] = Worktree(
        repo_path=Path("/tmp/myrepo"),
        worktree_path=Path("/tmp/chud-wt/sess1-myrepo"),
        branch="chud/add-a-foo-sess1",
        start_head=start_head,
    )
    return s


async def test_publish_skips_when_no_session_authored_commits(fake_publish_env):
    """Reproducer for PR #51 contamination at the publish layer.

    Even when the branch has commits ahead of ``origin/main`` (the existing
    abandonment check passes), if none of those commits were authored *by
    this session* (i.e. ``start_head..HEAD`` is empty) the PR must be
    discarded silently — never opened with the session's plan as title."""
    fake_publish_env.state["counts"] = {
        f"{_SESSION_START_OID}..HEAD": 0,
        "origin/main..HEAD": 8,
    }

    results = await pr_mod.publish_draft_prs(_state_with_start_head(_SESSION_START_OID))
    assert len(results) == 1
    assert results[0].discarded is True
    assert results[0].url is None
    assert results[0].error is None
    # No push, no PR open.
    assert fake_publish_env.calls["push"] == []
    assert fake_publish_env.calls["pr_create"] == []


async def test_publish_proceeds_when_session_authored_commits_exist(fake_publish_env):
    """Gate passes (≥1 commit since ``start_head``) → normal publish flow."""
    fake_publish_env.state["counts"] = {
        f"{_SESSION_START_OID}..HEAD": 1,
        "origin/main..HEAD": 1,
    }

    results = await pr_mod.publish_draft_prs(_state_with_start_head(_SESSION_START_OID))
    assert results[0].error is None
    assert results[0].url == "https://github.com/x/y/pull/7"
    assert results[0].discarded is False


async def test_publish_falls_back_to_origin_count_when_start_head_missing(fake_publish_env):
    """Legacy ``Worktree`` (start_head=None) → gate is skipped, existing
    ``origin/<base>..HEAD`` check is the only line of defense."""
    fake_publish_env.state["counts"] = {"origin/main..HEAD": 0}

    results = await pr_mod.publish_draft_prs(_state_with_start_head(None))
    assert results[0].discarded is True
    # Exactly one count call — the origin/main..HEAD check, not the new
    # session-authorship gate (which is skipped when start_head is None).
    count_ranges = [r for _, r in fake_publish_env.calls["counts"]]
    assert count_ranges == ["origin/main..HEAD"]


# --- Worktree.start_head round-trip ------------------------------------------


def test_worktree_round_trip_with_start_head():
    wt = Worktree(
        repo_path=Path("/tmp/r"),
        worktree_path=Path("/tmp/wt"),
        branch="chud/x-sess1",
        start_head="deadbeef" * 5,
    )
    assert Worktree.from_dict(wt.to_dict()) == wt


def test_worktree_from_dict_legacy_without_start_head():
    """Persistence layer must tolerate sessions.json entries written before
    the field existed — they default ``start_head`` to None."""
    legacy = {
        "repo_path": "/tmp/r",
        "worktree_path": "/tmp/wt",
        "branch": "chud/x-sess1",
    }
    wt = Worktree.from_dict(legacy)
    assert wt.start_head is None
    assert wt.branch == "chud/x-sess1"
