"""Tests for ``chud.gh`` — the issue picker and GitHub client wiring.

The pure ``build_issue_prompt`` formatter is exercised directly. The
``list_issues`` / ``list_issue_numbers_with_open_pr`` async paths are
tested by monkeypatching ``chud.gh._get_client`` and
``chud.gh._resolve_remote_owner_name`` so no real GitHub call is made —
these tests must run on machines without network or auth.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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


def test_is_available_reflects_token_presence(monkeypatch):
    """``is_available`` is True iff ``_get_client`` returns a non-None client.

    The seam is the same one the rest of the module uses, so a test that
    flips it is enough to exercise both branches.
    """
    monkeypatch.setattr(gh_mod, "_get_client", lambda: object())
    assert gh_mod.is_available() is True
    monkeypatch.setattr(gh_mod, "_get_client", lambda: None)
    assert gh_mod.is_available() is False


# --- _resolve_remote_owner_name -----------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:foo/bar.git", ("foo", "bar")),
        ("git@github.com:foo/bar", ("foo", "bar")),
        ("https://github.com/foo/bar.git", ("foo", "bar")),
        ("https://github.com/foo/bar", ("foo", "bar")),
        ("ssh://git@github.com/foo/bar.git", ("foo", "bar")),
        ("https://x:y@github.com/foo/bar.git", ("foo", "bar")),
    ],
)
def test_remote_url_regex_matches_github_shapes(url, expected):
    """The internal URL regex should accept every GitHub remote shape we
    expect to see in the wild."""
    m = gh_mod._GITHUB_REMOTE_RE.match(url)
    assert m is not None
    assert (m.group("owner"), m.group("name")) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/foo/bar.git",  # wrong host
        "git@bitbucket.org:foo/bar.git",
        "not-a-url",
        "",
    ],
)
def test_remote_url_regex_rejects_non_github_shapes(url):
    assert gh_mod._GITHUB_REMOTE_RE.match(url) is None


# --- list_issues ---------------------------------------------------------------


def _fake_issue_obj(
    *,
    number: int,
    title: str = "x",
    html_url: str = "https://github.com/x/y/issues/1",
    body: str = "",
    state: str = "open",
    pull_request: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        number=number,
        title=title,
        html_url=html_url,
        body=body,
        state=state,
        pull_request=pull_request,
    )


def _stub_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    issues: list[Any] | None = None,
    raise_on_call: BaseException | None = None,
) -> dict[str, Any]:
    """Install a fake githubkit client whose issue listing returns ``issues``."""
    seen: dict[str, Any] = {}

    async def fake_list_for_repo(
        owner: str,
        name: str,
        *,
        state: str = "open",
        per_page: int = 30,
    ) -> SimpleNamespace:
        seen["owner"] = owner
        seen["name"] = name
        seen["state"] = state
        seen["per_page"] = per_page
        if raise_on_call is not None:
            raise raise_on_call
        return SimpleNamespace(parsed_data=issues or [])

    fake_client = SimpleNamespace(
        rest=SimpleNamespace(
            issues=SimpleNamespace(async_list_for_repo=fake_list_for_repo),
        ),
    )
    monkeypatch.setattr(gh_mod, "_get_client", lambda: fake_client)
    monkeypatch.setattr(gh_mod, "_resolve_remote_owner_name", lambda _: ("x", "y"))
    return seen


async def test_list_issues_parses_payload(monkeypatch):
    items = [
        _fake_issue_obj(
            number=1,
            title="Tighten retries",
            html_url="https://github.com/x/y/issues/1",
            body="first line\r\nsecond line",
            state="open",
        ),
        _fake_issue_obj(
            number=2,
            title="Add docs",
            html_url="https://github.com/x/y/issues/2",
            body="",
            state="open",
        ),
    ]
    _stub_client(monkeypatch, issues=items)

    issues = await list_issues(Path("/some/repo"))

    assert len(issues) == 2
    assert issues[0].number == 1
    assert issues[0].title == "Tighten retries"
    # CRLF normalized to LF.
    assert issues[0].body == "first line\nsecond line"
    # State coerced to upper-case to match the original CLI shape.
    assert issues[0].state == "OPEN"
    assert issues[1].body == ""


async def test_list_issues_returns_empty_when_no_client(monkeypatch):
    monkeypatch.setattr(gh_mod, "_get_client", lambda: None)
    assert await list_issues(Path("/r")) == []


async def test_list_issues_returns_empty_when_no_owner_name(monkeypatch):
    monkeypatch.setattr(gh_mod, "_get_client", lambda: object())
    monkeypatch.setattr(gh_mod, "_resolve_remote_owner_name", lambda _: None)
    assert await list_issues(Path("/r")) == []


async def test_list_issues_returns_empty_on_request_failure(monkeypatch):
    from githubkit.exception import RequestError

    _stub_client(monkeypatch, raise_on_call=RequestError(Exception("boom")))
    assert await list_issues(Path("/r")) == []


async def test_list_issues_skips_pull_requests(monkeypatch):
    """GitHub's issues endpoint includes pull requests; the picker should
    drop them so PRs don't appear under the "issue" label."""
    items = [
        _fake_issue_obj(number=1, title="Real issue", body="b"),
        _fake_issue_obj(number=2, title="A PR", body="b", pull_request=object()),
    ]
    _stub_client(monkeypatch, issues=items)
    issues = await list_issues(Path("/r"))
    assert len(issues) == 1
    assert issues[0].number == 1


async def test_list_issues_skips_malformed_items(monkeypatch):
    """Items missing required attributes are dropped, well-formed ones survive."""
    items = [
        SimpleNamespace(),  # no fields at all → AttributeError on number access
        _fake_issue_obj(number=7, title="good", body="b"),
    ]
    _stub_client(monkeypatch, issues=items)
    issues = await list_issues(Path("/r"))
    assert len(issues) == 1
    assert issues[0].number == 7


async def test_list_issues_passes_limit_and_owner_name(monkeypatch):
    """Verify the call uses owner/name from the repo and the limit is forwarded."""
    seen = _stub_client(monkeypatch, issues=[])
    await list_issues(Path("/repo/x"), limit=5, timeout=2.5)
    assert seen["owner"] == "x"
    assert seen["name"] == "y"
    assert seen["state"] == "open"
    assert seen["per_page"] == 5


# --- list_issue_numbers_with_open_pr ------------------------------------------


def _stub_graphql(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: dict[str, Any] | None = None,
    raise_on_call: BaseException | None = None,
) -> dict[str, Any]:
    """Install a fake githubkit client whose ``async_graphql`` returns ``payload``."""
    seen: dict[str, Any] = {}

    async def fake_graphql(query: str, variables: dict[str, Any] | None = None) -> Any:
        seen["query"] = query
        seen["variables"] = variables
        if raise_on_call is not None:
            raise raise_on_call
        return payload or {}

    fake_client = SimpleNamespace(async_graphql=fake_graphql)
    monkeypatch.setattr(gh_mod, "_get_client", lambda: fake_client)
    monkeypatch.setattr(gh_mod, "_resolve_remote_owner_name", lambda _: ("x", "y"))
    return seen


async def test_list_issue_numbers_with_open_pr_parses_graphql(monkeypatch):
    """Multiple PRs that reference the same issue coalesce; PRs with no
    closing references are skipped."""
    payload = {
        "repository": {
            "pullRequests": {
                "nodes": [
                    {"closingIssuesReferences": {"nodes": [{"number": 1}, {"number": 2}]}},
                    {"closingIssuesReferences": {"nodes": [{"number": 1}, {"number": 3}]}},
                    {"closingIssuesReferences": {"nodes": []}},
                ],
            },
        },
    }
    seen = _stub_graphql(monkeypatch, payload=payload)
    nums = await gh_mod.list_issue_numbers_with_open_pr(Path("/r"))
    assert nums == {1, 2, 3}
    assert seen["variables"] == {"owner": "x", "name": "y", "first": 100}


async def test_list_issue_numbers_with_open_pr_returns_empty_when_no_client(monkeypatch):
    monkeypatch.setattr(gh_mod, "_get_client", lambda: None)
    assert await gh_mod.list_issue_numbers_with_open_pr(Path("/r")) == set()


async def test_list_issue_numbers_with_open_pr_returns_empty_when_no_owner_name(monkeypatch):
    monkeypatch.setattr(gh_mod, "_get_client", lambda: object())
    monkeypatch.setattr(gh_mod, "_resolve_remote_owner_name", lambda _: None)
    assert await gh_mod.list_issue_numbers_with_open_pr(Path("/r")) == set()


async def test_list_issue_numbers_with_open_pr_returns_empty_on_failure(monkeypatch):
    from githubkit.exception import RequestError

    _stub_graphql(monkeypatch, raise_on_call=RequestError(Exception("boom")))
    assert await gh_mod.list_issue_numbers_with_open_pr(Path("/r")) == set()


async def test_list_issue_numbers_with_open_pr_returns_empty_on_unexpected_payload(monkeypatch):
    """Schema drift: missing keys collapse to empty set rather than crash."""
    _stub_graphql(monkeypatch, payload={"repository": None})
    assert await gh_mod.list_issue_numbers_with_open_pr(Path("/r")) == set()
