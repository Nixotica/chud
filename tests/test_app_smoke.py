"""Render-path smoke tests for the Textual UI.

These tests catch failures that only surface when widgets are actually rendered
(e.g. the SessionRow markup-vs-Visual bug). They use Textual's Pilot to mount
the app and force renders without needing a real terminal.

The pattern is: do something, `await pilot.pause()` to flush, then ask each
widget for its strips so the render pipeline runs end-to-end.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from textual.widgets import Select

from chud import gh as gh_mod
from chud.app import ChudApp
from chud.gh import Issue
from chud.types import Event, EventKind, SessionState, SessionStatus, Worktree
from chud.widgets.attach_repo_modal import AttachRepoModal
from chud.widgets.cleanup_confirmation_modal import CleanupConfirmationModal
from chud.widgets.new_session_modal import (
    ACTIVE_CHUD_ICON,
    NewSessionModal,
    NewSessionResult,
)
from chud.widgets.plan_modal import PlanApprovalModal
from chud.widgets.session_list import SessionListView, SessionRow
from chud.widgets.session_view import SessionView


def _force_render(widget) -> None:
    """Drive the render pipeline for `widget` and all visible descendants.

    Calling render_lines on the root forces a full layout + render pass; if any
    widget's render returns something the strip pipeline can't consume, this
    raises here.
    """
    if not widget.region:
        return
    widget.render_lines(widget.region.translate(-widget.region.offset))


async def test_app_mount_and_empty_render():
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        for w in (app.query_one(SessionListView), app.query_one(SessionView)):
            _force_render(w)


async def test_session_list_renders_in_every_status():
    """Catch the original SessionRow render bug for every status enum value."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        slv = app.query_one(SessionListView)
        for i, status in enumerate(SessionStatus):
            st = SessionState(
                id=f"sess{i:04d}",
                workspace_dir=Path("/tmp"),
                status=status,
                initial_prompt=f"prompt for {status.value}",
            )
            slv.add_session(st)
        await pilot.pause()

        rows = list(app.query(SessionRow))
        assert len(rows) == len(SessionStatus)
        for row in rows:
            _force_render(row)


async def test_session_list_update_renders_after_status_change():
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        slv = app.query_one(SessionListView)
        st = SessionState(id="sess1", workspace_dir=Path("/tmp"), initial_prompt="hi")
        slv.add_session(st)
        await pilot.pause()

        st.status = SessionStatus.EXECUTING
        st.attached_repos["r"] = Worktree(
            repo_path=Path("/tmp/repo"),
            worktree_path=Path("/tmp/wt"),
            branch="chud/sess1",
        )
        slv.update_session(st)
        await pilot.pause()

        row = app.query_one(SessionRow)
        _force_render(row)


async def test_session_list_long_prompt_truncates_without_error():
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        slv = app.query_one(SessionListView)
        st = SessionState(
            id="long",
            workspace_dir=Path("/tmp"),
            initial_prompt="x" * 500 + "\nsecond line should be ignored",
        )
        slv.add_session(st)
        await pilot.pause()
        _force_render(app.query_one(SessionRow))


async def test_session_view_renders_every_event_kind():
    """Drive every EventKind through SessionView.render_event then re-render."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        view = app.query_one(SessionView)
        st = SessionState(id="sv1", workspace_dir=Path("/tmp"), initial_prompt="p")
        st.attached_repos["r"] = Worktree(
            repo_path=Path("/tmp/repo"),
            worktree_path=Path("/tmp/wt"),
            branch="chud/sv1",
        )
        view.show_session(st)
        await pilot.pause()

        events = [
            Event("sv1", EventKind.STATUS_CHANGED, {"status": "executing"}),
            Event("sv1", EventKind.TRANSCRIPT_APPENDED, {"role": "assistant", "text": "hi"}),
            Event(
                "sv1",
                EventKind.TRANSCRIPT_APPENDED,
                {
                    "role": "assistant",
                    "tool_use": {"name": "Bash", "input": {"command": "ls"}, "id": "t1"},
                },
            ),
            Event("sv1", EventKind.TRANSCRIPT_APPENDED, {"role": "user", "raw": "u"}),
            Event("sv1", EventKind.PLAN_PROPOSED, {"plan": "do the thing"}),
            Event("sv1", EventKind.NEEDS_USER_INPUT, {"reason": "stop_hook"}),
            Event(
                "sv1",
                EventKind.NEEDS_USER_INPUT,
                {"reason": "notification", "message": "agent has a question"},
            ),
            Event(
                "sv1",
                EventKind.REPO_ATTACHED,
                {"repo": "/tmp/repo", "worktree": "/tmp/wt"},
            ),
            Event("sv1", EventKind.UNKNOWN_MESSAGE, {"type": "Mystery", "raw": "{...}"}),
            Event("sv1", EventKind.ERROR, {"error": "boom"}),
        ]
        for ev in events:
            view.render_event(ev)
        await pilot.pause()
        _force_render(view)


async def test_session_view_escapes_bracket_payloads():
    """Raw SDK payloads contain '[ToolResultBlock(...)]' which Rich would parse as
    markup tags and crash with a MarkupError. Render_event must escape them."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        view = app.query_one(SessionView)
        view.show_session(SessionState(id="x", workspace_dir=Path("/tmp")))
        await pilot.pause()

        # Each of these payloads contains content that would break a markup parser
        # if passed through unescaped.
        evil = (
            "{'content': [ToolResultBlock(tool_use_id='toolu_01abc', "
            "content='[/some] unbalanced [tag]')], 'role': 'user'}"
        )
        events = [
            Event("x", EventKind.TRANSCRIPT_APPENDED, {"role": "user", "raw": evil}),
            Event("x", EventKind.TRANSCRIPT_APPENDED, {"role": "assistant", "text": evil}),
            Event(
                "x",
                EventKind.TRANSCRIPT_APPENDED,
                {
                    "role": "assistant",
                    "tool_use": {
                        "name": "Bash[evil]",
                        "input": {"command": "echo '[unclosed'"},
                        "id": "t1",
                    },
                },
            ),
            Event("x", EventKind.STATUS_CHANGED, {"status": "[weird]"}),
            Event("x", EventKind.NEEDS_USER_INPUT, {"message": evil}),
            Event(
                "x",
                EventKind.REPO_ATTACHED,
                {"repo": "/tmp/[a]repo", "worktree": "/tmp/wt[/]"},
            ),
            Event("x", EventKind.UNKNOWN_MESSAGE, {"type": "[Mystery]", "raw": evil}),
            Event("x", EventKind.ERROR, {"error": evil}),
        ]
        for ev in events:
            view.render_event(ev)
        await pilot.pause()
        _force_render(view)


async def test_session_view_escape_moves_focus_off_input():
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        view = app.query_one(SessionView)
        st = SessionState(id="esc1", workspace_dir=Path("/tmp"), initial_prompt="p")
        view.show_session(st)
        await pilot.pause()
        view.input.focus()
        await pilot.pause()
        assert view.input.has_focus
        await pilot.press("escape")
        await pilot.pause()
        assert not view.input.has_focus
        assert view.transcript.has_focus


async def test_session_view_escape_is_noop_when_input_disabled():
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        view = app.query_one(SessionView)
        view.show_session(None)
        await pilot.pause()
        assert view.input.disabled
        view.action_focus_transcript()
        await pilot.pause()


async def test_session_view_show_session_clears_and_disables_input():
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        view = app.query_one(SessionView)
        st = SessionState(id="x", workspace_dir=Path("/tmp"), initial_prompt="p")
        view.show_session(st)
        await pilot.pause()
        assert not view.input.disabled
        view.show_session(None)
        await pilot.pause()
        assert view.input.disabled
        _force_render(view)


async def test_new_session_modal_mounts_and_renders():
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(NewSessionModal())
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, NewSessionModal)
        _force_render(modal)


async def test_attach_repo_modal_mounts_and_renders():
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(AttachRepoModal())
        await pilot.pause()
        _force_render(app.screen)


async def test_plan_modal_mounts_and_renders_long_plan():
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        plan = "# Plan\n\n" + "\n".join(f"- step {i}" for i in range(40))
        app.push_screen(PlanApprovalModal(session_id="abc12345", plan_text=plan))
        await pilot.pause()
        _force_render(app.screen)


async def test_plan_modal_approve_via_a_key():
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        result: list[bool | str | None] = []
        app.push_screen(
            PlanApprovalModal(session_id="abc12345", plan_text="# plan"),
            callback=lambda v: result.append(v),
        )
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert result == [True]


async def test_plan_modal_reject_via_r_key():
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        result: list[bool | str | None] = []
        app.push_screen(
            PlanApprovalModal(session_id="abc12345", plan_text="# plan"),
            callback=lambda v: result.append(v),
        )
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert result == [False]


async def test_plan_modal_reject_via_escape():
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        result: list[bool | str | None] = []
        app.push_screen(
            PlanApprovalModal(session_id="abc12345", plan_text="# plan"),
            callback=lambda v: result.append(v),
        )
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert result == [False]


async def test_plan_modal_respond_with_typed_message():
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        result: list[bool | str | None] = []
        app.push_screen(
            PlanApprovalModal(session_id="abc12345", plan_text="# plan"),
            callback=lambda v: result.append(v),
        )
        await pilot.pause()
        # First Enter reveals and focuses the response input.
        await pilot.press("enter")
        await pilot.pause()
        # Type a reason. These letters must land in the input (Input has focus
        # and consumes printable characters before the screen bindings fire),
        # not trigger approve/reject — that's the whole point of can_focus=False
        # on the buttons combined with input-focus precedence.
        for ch in "use pathlib":
            await pilot.press(ch if ch != " " else "space")
        await pilot.pause()
        # Second Enter submits via Input.Submitted → on_respond_submitted.
        await pilot.press("enter")
        await pilot.pause()
        assert result == ["use pathlib"]


async def test_plan_modal_respond_empty_collapses_to_reject():
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        result: list[bool | str | None] = []
        app.push_screen(
            PlanApprovalModal(session_id="abc12345", plan_text="# plan"),
            callback=lambda v: result.append(v),
        )
        await pilot.pause()
        await pilot.press("enter")  # reveal + focus
        await pilot.pause()
        await pilot.press("enter")  # submit empty
        await pilot.pause()
        assert result == [False]


def _cleanup_modal_text(modal: CleanupConfirmationModal) -> str:
    """Concatenate every Static's rendered text inside the modal.

    Uses ``Static.render()`` (markup → plain Text) rather than the raw markup
    attribute so the assertions match what the user actually sees.
    """
    from textual.widgets import Static as _Static

    parts: list[str] = []
    for static in modal.query(_Static):
        parts.append(str(static.render()))
    return "\n".join(parts)


async def test_cleanup_modal_unpublished_shows_destructive_copy():
    """No PRs published → keep the existing red/ominous warning copy."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        state = SessionState(
            id="sess1234",
            workspace_dir=Path("/tmp/ws"),
            initial_prompt="do thing",
        )
        modal = CleanupConfirmationModal(state=state)
        app.push_screen(modal)
        await pilot.pause()
        text = _cleanup_modal_text(modal)
        assert "permanently delete" in text
        assert "will be lost" in text
        # And no draft-PR success line.
        assert "Draft PRs opened" not in text


async def test_cleanup_modal_published_swaps_to_success_copy():
    """Successful PRs in payload → calmer copy with the URL, no loss warning."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        state = SessionState(
            id="sess5678",
            workspace_dir=Path("/tmp/ws"),
            initial_prompt="do thing",
        )
        modal = CleanupConfirmationModal(
            state=state,
            published_prs=[
                {"repo": "alpha", "url": "https://github.com/x/y/pull/1"},
                {"repo": "beta", "url": "https://github.com/a/b/pull/2"},
            ],
        )
        app.push_screen(modal)
        await pilot.pause()
        text = _cleanup_modal_text(modal)
        assert "Work is pushed" in text
        assert "Draft PRs opened" in text
        assert "https://github.com/x/y/pull/1" in text
        assert "https://github.com/a/b/pull/2" in text
        assert "permanently delete" not in text
        assert "will be lost" not in text


# --- New-session modal: GitHub issue picker ----------------------------------


def _sample_issue(number: int = 7, title: str = "t") -> Issue:
    return Issue(
        number=number,
        title=title,
        url=f"https://github.com/x/y/issues/{number}",
        body="ctx body",
        state="OPEN",
    )


async def test_new_session_modal_with_issues_renders_picker():
    """Picker Select is mounted and rendered when issues are provided."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(NewSessionModal(issues=[_sample_issue()]))
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, NewSessionModal)
        # Query the modal screen specifically — the picker lives there, not on
        # the app's default screen.
        select = modal.query_one("#issue", Select)
        assert select is not None
        _force_render(modal)


async def test_new_session_modal_without_issues_hides_picker():
    """``issues=None`` ⇒ no Select#issue in the DOM."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(NewSessionModal(issues=None))
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, NewSessionModal)
        assert len(modal.query("#issue")) == 0
        _force_render(modal)


async def test_new_session_modal_with_empty_issues_hides_picker():
    """``issues=[]`` is treated identically to ``None``."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(NewSessionModal(issues=[]))
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, NewSessionModal)
        assert len(modal.query("#issue")) == 0
        _force_render(modal)


async def test_new_session_modal_picker_populates_prompt():
    """Selecting an issue from the picker pre-fills the prompt textarea with
    the issue's title + URL + body + ``---`` separator."""
    from textual.widgets import TextArea

    issue = _sample_issue(number=42, title="Picker test")
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(NewSessionModal(issues=[issue]))
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, NewSessionModal)
        select = modal.query_one("#issue", Select)
        select.value = str(issue.number)
        await pilot.pause()
        text = modal.query_one("#prompt", TextArea).text
        assert "GitHub issue #42: Picker test" in text
        assert "https://github.com/x/y/issues/42" in text
        assert "ctx body" in text
        assert "\n---\n" in text


async def test_new_session_flow_filters_issues_with_open_pr(monkeypatch):
    """End-to-end: an issue listed in ``_issues_with_pr_cache`` (populated
    by the background ``gh.list_issue_numbers_with_open_pr`` refresh) is
    dropped from the picker regardless of who or what made the PR."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr("chud.app.detect_cwd_repo", lambda: Path("/fake/repo"))

        keep = _sample_issue(number=10, title="open work")
        drop = _sample_issue(number=11, title="already in PR")
        app._issues_cache = [keep, drop]
        app._issues_with_pr_cache = {11}
        app._launch_repo = Path("/fake/repo")

        captured: dict[str, Any] = {}

        async def fake_push_screen_wait(modal: Any) -> NewSessionResult | None:
            assert isinstance(modal, NewSessionModal)
            captured["issues"] = list(modal._issues)
            return None

        monkeypatch.setattr(app, "push_screen_wait", fake_push_screen_wait)

        await app._new_session_flow()
        seen_numbers = [i.number for i in captured["issues"]]
        assert 10 in seen_numbers
        assert 11 not in seen_numbers


async def test_app_active_sessions_by_issue_skips_terminal_states():
    """``ChudApp._active_sessions_by_issue`` only counts sessions whose status
    is non-terminal (DONE/ERRORED are excluded — those chuds aren't 'working'
    anymore)."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()

        def _stub(sid: str, issue_num: int | None, status: SessionStatus) -> Any:
            state = SessionState(
                id=sid,
                workspace_dir=Path(f"/tmp/{sid}"),
                issue_number=issue_num,
            )
            state.status = status
            return SimpleNamespace(state=state)

        app.manager.sessions = {  # type: ignore[assignment]
            "a": _stub("a", 7, SessionStatus.EXECUTING),
            "b": _stub("b", 7, SessionStatus.AWAITING_USER),
            "c": _stub("c", 7, SessionStatus.DONE),
            "d": _stub("d", 9, SessionStatus.ERRORED),
            "e": _stub("e", None, SessionStatus.EXECUTING),
        }
        try:
            out = app._active_sessions_by_issue()
            assert sorted(out.keys()) == [7]
            assert sorted(out[7]) == ["a", "b"]
        finally:
            # Clear before teardown — the manager's shutdown iterates sessions
            # and calls .stop() on each, which our SimpleNamespace stubs lack.
            app.manager.sessions = {}


async def test_new_session_modal_marks_issues_with_active_chud():
    """Issues that already have an active chud session linked to them get a
    ``[chud working]`` prefix in the picker label so the user knows before
    spawning a duplicate."""
    one = _sample_issue(number=1, title="solo")
    two = _sample_issue(number=2, title="busy")
    three = _sample_issue(number=3, title="crowded")
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = NewSessionModal(
            issues=[one, two, three],
            active_sessions_by_issue={2: ["sess-A"], 3: ["sess-A", "sess-B"]},
        )
        labels = {i.number: modal._format_issue_label(i) for i in [one, two, three]}
        assert labels[1] == "#1 — solo"
        assert labels[2] == f"{ACTIVE_CHUD_ICON} #2 — busy"
        assert labels[3] == f"{ACTIVE_CHUD_ICON}×2 #3 — crowded"
        # Drive the modal through compose() to make sure the helper actually
        # feeds Select.
        app.push_screen(modal)
        await pilot.pause()
        select = app.screen.query_one("#issue", Select)
        rendered = {pair[1]: pair[0] for pair in select._options}  # type: ignore[attr-defined]
        assert rendered[str(2)] == f"{ACTIVE_CHUD_ICON} #2 — busy"
        assert rendered[str(3)] == f"{ACTIVE_CHUD_ICON}×2 #3 — crowded"


async def test_new_session_modal_picker_does_not_clobber_user_edits():
    """If the user has already typed something the picker must not overwrite
    it on issue selection."""
    from textual.widgets import TextArea

    issue = _sample_issue(number=8, title="Don't overwrite me")
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(NewSessionModal(issues=[issue]))
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, NewSessionModal)
        prompt_area = modal.query_one("#prompt", TextArea)
        prompt_area.text = "user typed this first"
        select = modal.query_one("#issue", Select)
        select.value = str(issue.number)
        await pilot.pause()
        assert modal.query_one("#prompt", TextArea).text == "user typed this first"


# --- New-session flow: issue → templated prompt ------------------------------


async def test_new_session_flow_passes_modal_prompt_through_for_issue(monkeypatch):
    """The modal now pre-populates the prompt with the issue content (see
    NewSessionModal.on_select_changed) and returns the merged text in
    ``NewSessionResult.prompt``. The flow must pass it through verbatim — no
    second prepend, otherwise the issue head would appear twice."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()

        # Pretend we're inside a repo and gh is happy.
        monkeypatch.setattr("chud.app.detect_cwd_repo", lambda: Path("/fake/repo"))
        monkeypatch.setattr(gh_mod, "is_available", lambda: True)
        issue = _sample_issue(number=7, title="Tighten retries")

        # Seed the cache directly — the flow reads from the background-refreshed
        # cache rather than awaiting a fresh fetch on each `n` press.
        app._issues_cache = [issue]
        app._launch_repo = Path("/fake/repo")

        captured: dict[str, Any] = {}

        async def fake_create_session(
            prompt: str,
            repo_path: Path | None = None,
            launch_cwd: Path | None = None,
            options: dict[str, bool] | None = None,
            effort: str | None = None,
            **_kw: Any,
        ) -> Any:
            captured["prompt"] = prompt
            captured["repo_path"] = repo_path
            state = SessionState(
                id="sessISSUE",
                workspace_dir=Path("/tmp/ws"),
                initial_prompt=prompt,
            )
            return SimpleNamespace(state=state)

        monkeypatch.setattr(app.manager, "create_session", fake_create_session)

        # Modal stub returns what a real modal would produce when the user
        # picks the issue and then types extra context: the issue head + body
        # + "---" + their additions, all already in `prompt`.
        merged_prompt = gh_mod.build_issue_prompt(issue, "please fix it")

        async def fake_push_screen_wait(modal: Any) -> NewSessionResult:
            assert isinstance(modal, NewSessionModal)
            assert modal._has_issue_picker
            return NewSessionResult(
                prompt=merged_prompt,
                options={},
                effort=None,
                issue=issue,
            )

        monkeypatch.setattr(app, "push_screen_wait", fake_push_screen_wait)

        await app._new_session_flow()

        prompt = captured["prompt"]
        assert prompt == merged_prompt, "flow must not re-prepend issue content"
        # Sanity: the merged text contains the expected issue pieces.
        assert "GitHub issue #7: Tighten retries" in prompt
        assert "https://github.com/x/y/issues/7" in prompt
        assert "ctx body" in prompt
        assert "\n---\n" in prompt
        assert prompt.rstrip().endswith("please fix it")
        assert captured["repo_path"] == Path("/fake/repo")


async def test_new_session_flow_no_issue_passes_prompt_verbatim(monkeypatch):
    """If the user doesn't pick an issue, the prompt reaches create_session unchanged."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()

        monkeypatch.setattr("chud.app.detect_cwd_repo", lambda: Path("/fake/repo"))
        monkeypatch.setattr(gh_mod, "is_available", lambda: True)

        async def fake_list_issues(_repo, **_kw):
            return [_sample_issue()]

        monkeypatch.setattr(gh_mod, "list_issues", fake_list_issues)

        captured: dict[str, Any] = {}

        async def fake_create_session(
            prompt: str,
            repo_path: Path | None = None,
            launch_cwd: Path | None = None,
            options: dict[str, bool] | None = None,
            effort: str | None = None,
            **_kw: Any,
        ) -> Any:
            captured["prompt"] = prompt
            return SimpleNamespace(
                state=SessionState(
                    id="sessNO",
                    workspace_dir=Path("/tmp/ws"),
                    initial_prompt=prompt,
                )
            )

        monkeypatch.setattr(app.manager, "create_session", fake_create_session)

        async def fake_push_screen_wait(_modal: Any) -> NewSessionResult:
            return NewSessionResult(
                prompt="just do the thing",
                options={},
                effort=None,
                issue=None,
            )

        monkeypatch.setattr(app, "push_screen_wait", fake_push_screen_wait)

        await app._new_session_flow()

        assert captured["prompt"] == "just do the thing"


async def test_new_session_flow_no_repo_skips_gh(monkeypatch):
    """When CWD isn't a repo, ``list_issues`` is never called and modal gets ``None``."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()

        monkeypatch.setattr("chud.app.detect_cwd_repo", lambda: None)

        list_calls: list[Path] = []

        async def fake_list_issues(repo, **_kw):
            list_calls.append(repo)
            return []

        monkeypatch.setattr(gh_mod, "list_issues", fake_list_issues)
        # Don't even let is_available short-circuit hide the call — the
        # detect-cwd-repo branch should bail before reaching this.
        monkeypatch.setattr(gh_mod, "is_available", lambda: True)

        modals_seen: list[NewSessionModal] = []

        async def fake_push_screen_wait(modal: Any) -> NewSessionResult | None:
            assert isinstance(modal, NewSessionModal)
            modals_seen.append(modal)
            # Cancel the flow so we don't have to stub create_session.
            return None

        monkeypatch.setattr(app, "push_screen_wait", fake_push_screen_wait)

        await app._new_session_flow()

        assert list_calls == []
        assert len(modals_seen) == 1
        # The modal received no issues ⇒ picker hidden.
        assert not modals_seen[0]._has_issue_picker


async def test_new_session_flow_gh_unavailable_skips_list(monkeypatch):
    """Repo detected but ``gh`` missing ⇒ list_issues not called, picker hidden."""
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()

        monkeypatch.setattr("chud.app.detect_cwd_repo", lambda: Path("/fake/repo"))
        monkeypatch.setattr(gh_mod, "is_available", lambda: False)

        list_calls: list[Path] = []

        async def fake_list_issues(repo, **_kw):
            list_calls.append(repo)
            return []

        monkeypatch.setattr(gh_mod, "list_issues", fake_list_issues)

        modals_seen: list[NewSessionModal] = []

        async def fake_push_screen_wait(modal: Any) -> NewSessionResult | None:
            assert isinstance(modal, NewSessionModal)
            modals_seen.append(modal)
            return None

        monkeypatch.setattr(app, "push_screen_wait", fake_push_screen_wait)

        await app._new_session_flow()

        assert list_calls == []
        assert len(modals_seen) == 1
        assert not modals_seen[0]._has_issue_picker
