"""Tests for ``chud.gh`` — the issue picker and shared subprocess plumbing.

The pure ``build_issue_prompt`` formatter is exercised directly. The
``list_issues`` async path is tested by monkeypatching ``chud.gh._run`` so
no real ``gh`` subprocess is spawned — these tests must run on machines
without GitHub CLI installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from chud import gh as gh_mod
from chud.gh import Issue, build_issue_prompt, list_issues

# --- build_issue_prompt --------------------------------------------------------


def _issue(
    *,
    number: int = 42,
    title: str = "Fix race in worker",
    url: str = "https://github.com/x/y/issues/42",
    body: str = "Repro: kick off two workers and watch the dispatch loop.",
    state: str = "OPEN",
) -> Issue:
    return Issue(number=number, title=title, url=url, body=body, state=state)


def test_build_issue_prompt_includes_all_sections():
    out = build_issue_prompt(_issue(), "please fix")
    # Header is the first non-empty block.
    assert out.startswith("GitHub issue #42: Fix race in worker\n")
    assert "https://github.com/x/y/issues/42" in out
    # Body and user prompt are separated by ``---``.
    assert "Repro:" in out
    assert "\n---\n" in out
    # User-typed prompt closes the message.
    assert out.rstrip().endswith("please fix")


def test_build_issue_prompt_truncates_long_body():
    # Use 'Q' — guaranteed not to appear anywhere in the issue/url/prompt
    # boilerplate so we can count body chars unambiguously.
    long_body = "Q" * 9000
    out = build_issue_prompt(_issue(body=long_body), "fix it")
    assert "…(truncated)…" in out
    # Exactly 8000 body chars survive — the rest are dropped.
    assert out.count("Q") == 8000
    # The marker appears immediately after the truncated body.
    marker_idx = out.index("…(truncated)…")
    assert out[marker_idx - 3 : marker_idx] == "Q\n\n"


def test_build_issue_prompt_omits_blank_body():
    out = build_issue_prompt(_issue(body="   \n\n  "), "do the work")
    # No "Repro" sentinel, no leading blank-line-then-marker before ---.
    assert "Repro" not in out
    # Header → blank line → ---. Body block must not appear.
    lines = out.split("\n")
    # The very next non-empty block after the URL should be "---".
    assert "---" in lines
    sep_idx = lines.index("---")
    # Everything from the URL to "---" should be just empty padding (one blank line).
    assert all(line == "" for line in lines[2:sep_idx])
    assert "do the work" in out


def test_build_issue_prompt_strips_user_prompt_whitespace():
    out = build_issue_prompt(_issue(body="b"), "  spaced prompt  \n")
    assert out.endswith("spaced prompt")


def test_build_issue_prompt_handles_crlf_in_body():
    # CRLF normalization happens at parse time in ``list_issues``, but the
    # prompt builder should also pass through clean LF bodies unchanged.
    out = build_issue_prompt(_issue(body="line1\nline2"), "x")
    assert "line1\nline2" in out


# --- is_available --------------------------------------------------------------


def test_is_available_reflects_path(monkeypatch):
    monkeypatch.setattr(
        gh_mod.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None
    )
    assert gh_mod.is_available() is True
    monkeypatch.setattr(gh_mod.shutil, "which", lambda _name: None)
    assert gh_mod.is_available() is False


# --- list_issues ---------------------------------------------------------------


def _stub_run(monkeypatch: pytest.MonkeyPatch, *, rc: int, stdout: str, stderr: str = "") -> None:
    """Replace ``gh._run`` with a deterministic async stub."""

    async def fake_run(
        cmd: list[str],
        cwd: Path | None = None,
        *,
        timeout: float = 0.0,
    ) -> tuple[int, str, str]:
        return rc, stdout, stderr

    monkeypatch.setattr(gh_mod, "_run", fake_run)


async def test_list_issues_parses_gh_json(monkeypatch):
    payload: list[dict[str, Any]] = [
        {
            "number": 1,
            "title": "Tighten retries",
            "url": "https://github.com/x/y/issues/1",
            "body": "first line\r\nsecond line",
            "state": "OPEN",
        },
        {
            "number": 2,
            "title": "Add docs",
            "url": "https://github.com/x/y/issues/2",
            "body": "",
            "state": "OPEN",
        },
    ]
    _stub_run(monkeypatch, rc=0, stdout=json.dumps(payload))

    issues = await list_issues(Path("/some/repo"))

    assert len(issues) == 2
    assert issues[0].number == 1
    assert issues[0].title == "Tighten retries"
    # CRLF normalized to LF.
    assert issues[0].body == "first line\nsecond line"
    assert issues[1].body == ""


async def test_list_issues_returns_empty_on_nonzero_exit(monkeypatch):
    _stub_run(monkeypatch, rc=1, stdout="", stderr="auth required")
    assert await list_issues(Path("/r")) == []


async def test_list_issues_returns_empty_on_invalid_json(monkeypatch):
    _stub_run(monkeypatch, rc=0, stdout="not json")
    assert await list_issues(Path("/r")) == []


async def test_list_issues_returns_empty_on_non_list_json(monkeypatch):
    _stub_run(monkeypatch, rc=0, stdout=json.dumps({"unexpected": "object"}))
    assert await list_issues(Path("/r")) == []


async def test_list_issues_returns_empty_on_empty_stdout(monkeypatch):
    _stub_run(monkeypatch, rc=0, stdout="")
    assert await list_issues(Path("/r")) == []


async def test_list_issues_skips_malformed_items(monkeypatch):
    """Items missing required keys are dropped, well-formed ones survive."""
    payload = [
        {"number": "not-an-int", "title": "x", "url": "u", "body": "", "state": "OPEN"},
        {"number": 7, "title": "good", "url": "u", "body": "b", "state": "OPEN"},
        {"missing_keys": True},
    ]
    _stub_run(monkeypatch, rc=0, stdout=json.dumps(payload))
    issues = await list_issues(Path("/r"))
    assert len(issues) == 1
    assert issues[0].number == 7


async def test_list_issues_passes_limit_and_cwd(monkeypatch):
    """Verify the gh invocation includes --limit and is run with cwd=repo_path."""
    seen: dict[str, Any] = {}

    async def fake_run(
        cmd: list[str],
        cwd: Path | None = None,
        *,
        timeout: float = 0.0,
    ) -> tuple[int, str, str]:
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        seen["timeout"] = timeout
        return 0, "[]", ""

    monkeypatch.setattr(gh_mod, "_run", fake_run)
    await list_issues(Path("/repo/x"), limit=5, timeout=2.5)

    assert seen["cwd"] == Path("/repo/x")
    assert seen["timeout"] == 2.5
    cmd = seen["cmd"]
    assert cmd[0] == "gh"
    assert "--limit" in cmd
    assert cmd[cmd.index("--limit") + 1] == "5"
    assert "--state" in cmd
    assert cmd[cmd.index("--state") + 1] == "open"
