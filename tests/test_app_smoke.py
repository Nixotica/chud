"""Render-path smoke tests for the Textual UI.

These tests catch failures that only surface when widgets are actually rendered
(e.g. the SessionRow markup-vs-Visual bug). They use Textual's Pilot to mount
the app and force renders without needing a real terminal.

The pattern is: do something, `await pilot.pause()` to flush, then ask each
widget for its strips so the render pipeline runs end-to-end.
"""

from __future__ import annotations

from pathlib import Path

from chud.app import ChudApp
from chud.types import Event, EventKind, SessionState, SessionStatus, Worktree
from chud.widgets.attach_repo_modal import AttachRepoModal
from chud.widgets.new_session_modal import NewSessionModal
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


async def test_new_session_modal_renders_with_recent_repo_suggestions(monkeypatch):
    """The repo Input is wired to a SuggestFromList of recent repos.

    Force a non-empty recent-repo list and confirm the modal still mounts and
    that the repo Input has a suggester attached with those entries — i.e. the
    autocomplete path doesn't crash with real suggestions in play.
    """
    from textual.suggester import SuggestFromList
    from textual.widgets import Input

    from chud.widgets import new_session_modal as nsm_mod

    recents = ["/tmp/repo-a", "/tmp/repo-b"]
    monkeypatch.setattr(nsm_mod, "_safe_recent_repos", lambda: recents)

    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(NewSessionModal())
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, NewSessionModal)
        repo_input = modal.query_one("#repo", Input)
        assert isinstance(repo_input.suggester, SuggestFromList)
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
        result: list[bool | str] = []
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
        result: list[bool | str] = []
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
        result: list[bool | str] = []
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
        result: list[bool | str] = []
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
        result: list[bool | str] = []
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
