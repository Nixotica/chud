from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Input, ListView

from chud import state as state_mod
from chud.manager import SessionManager
from chud.types import Event, EventKind, SessionStatus
from chud.widgets.attach_repo_modal import AttachRepoModal
from chud.widgets.cleanup_confirmation_modal import CleanupConfirmationModal
from chud.widgets.new_session_modal import NewSessionModal, NewSessionResult
from chud.widgets.plan_modal import PlanApprovalModal
from chud.widgets.session_list import SessionListView, SessionRow
from chud.widgets.session_view import SessionView

log = logging.getLogger(__name__)


class ChudApp(App[None]):
    """Multi-agent Claude Code TUI."""

    TITLE = "chud"
    SUB_TITLE = "multi-agent Claude Code orchestrator"

    BINDINGS = [
        ("n", "new_session", "New session"),
        ("a", "attach_repo", "Attach repo"),
        ("k", "kill_session", "Kill session"),
        ("q", "quit", "Quit"),
    ]

    CSS = """
    Horizontal#main {
        height: 1fr;
    }
    SessionView {
        width: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.manager = SessionManager()
        self._event_log: dict[str, list[Event]] = defaultdict(list)
        self._selected_session_id: str | None = None
        self._open_plan_modals: set[str] = set()
        self._open_cleanup_modals: set[str] = set()

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            yield SessionListView()
            yield SessionView()
        yield Footer()

    async def on_mount(self) -> None:
        self.manager.set_focus(True)
        self.query_one(SessionView).show_session(None)
        # subscribe and drain events in a background worker
        self.run_worker(self._event_pump(), exclusive=False, name="event-pump")

    async def on_unmount(self) -> None:
        await self.manager.shutdown()

    # ------------------------------------------------------------------ focus

    def on_app_focus(self) -> None:
        self.manager.set_focus(True)

    def on_app_blur(self) -> None:
        self.manager.set_focus(False)

    # ------------------------------------------------------------------ actions

    def action_new_session(self) -> None:
        self.run_worker(self._new_session_flow(), exclusive=False)

    async def _new_session_flow(self) -> None:
        result: NewSessionResult | None = await self.push_screen_wait(NewSessionModal())
        if result is None:
            return
        try:
            sess = await self.manager.create_session(
                prompt=result.prompt,
                repo_path=result.repo_path,
                options=result.options,
            )
        except Exception as e:
            log.exception("create_session failed")
            self.notify(f"Failed to start session: {e}", severity="error")
            return
        self.query_one(SessionListView).add_session(sess.state)
        self._select_session(sess.state.id)

    def action_attach_repo(self) -> None:
        self.run_worker(self._attach_repo_flow(), exclusive=False)

    async def _attach_repo_flow(self) -> None:
        sid = self._selected_session_id
        if sid is None:
            self.notify("No session selected.", severity="warning")
            return
        repo: Path | None = await self.push_screen_wait(AttachRepoModal())
        if repo is None:
            return
        try:
            await self.manager.attach_repo(sid, repo)
        except Exception as e:
            self.notify(f"attach_repo failed: {e}", severity="error")

    async def action_kill_session(self) -> None:
        sid = self._selected_session_id
        if sid is None:
            return
        await self.manager.kill_session(sid)
        self.query_one(SessionListView).remove_session(sid)
        self._event_log.pop(sid, None)
        self._selected_session_id = None
        self.query_one(SessionView).show_session(None)

    # ------------------------------------------------------------------ list selection

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, SessionRow):
            self._select_session(event.item.session_id)

    def _select_session(self, sid: str) -> None:
        self._selected_session_id = sid
        sess = self.manager.sessions.get(sid)
        view = self.query_one(SessionView)
        if sess is None:
            view.show_session(None)
            return
        view.show_session(sess.state)
        for ev in self._event_log.get(sid, []):
            view.render_event(ev)

    # ------------------------------------------------------------------ input box

    @on(Input.Submitted, "#input")
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        sid = self._selected_session_id
        if sid is None:
            self.notify("No session selected.", severity="warning")
            return
        sess = self.manager.sessions.get(sid)
        if sess is None:
            return
        if sess.state.status == SessionStatus.AWAITING_PLAN_APPROVAL:
            # Bypassing the plan modal would inject an out-of-order user message
            # mid-tool-call; the agent's prompt-injection guard would drop it.
            self.notify("Use the plan modal (a/r/Enter) to respond.", severity="warning")
            return
        try:
            await sess.send_message(text)
        except Exception as e:
            self.notify(f"send_message failed: {e}", severity="error")

    # ------------------------------------------------------------------ event pump

    async def _event_pump(self) -> None:
        queue = self.manager.subscribe()
        try:
            while True:
                event = await queue.get()
                self._on_event(event)
        finally:
            self.manager.unsubscribe(queue)

    def _on_event(self, event: Event) -> None:
        self._event_log[event.session_id].append(event)

        sess = self.manager.sessions.get(event.session_id)
        if sess is not None:
            self.query_one(SessionListView).update_session(sess.state)

        if event.session_id == self._selected_session_id:
            self.query_one(SessionView).render_event(event)
            if sess is not None:
                self.query_one(SessionView).update_header(sess.state)

        if event.kind == EventKind.PLAN_PROPOSED:
            self._open_plan_modal(event.session_id, event.payload.get("plan", ""))
        elif event.kind == EventKind.CLEANUP_REQUESTED:
            self._open_cleanup_modal(event.session_id)
        elif event.kind == EventKind.PR_PUBLISHED:
            url = event.payload.get("url", "")
            repo = event.payload.get("repo", "")
            self.notify(f"Draft PR opened ({repo}): {url}")
            view = self.query_one(SessionView)
            view.transcript.write(
                f"[bold green]+ draft PR:[/bold green] {repo} → {url}"
            )
        elif event.kind == EventKind.PR_FAILED:
            err = event.payload.get("error", "")
            repo = event.payload.get("repo", "")
            self.notify(f"PR failed ({repo}): {err}", severity="error")
            view = self.query_one(SessionView)
            view.transcript.write(
                f"[bold red]! PR failed:[/bold red] {repo or '(session)'}: {err}"
            )

    def _open_plan_modal(self, session_id: str, plan_text: str) -> None:
        if session_id in self._open_plan_modals:
            return
        self._open_plan_modals.add(session_id)

        async def show_modal() -> None:
            try:
                result = await self.push_screen_wait(
                    PlanApprovalModal(session_id=session_id, plan_text=plan_text)
                )
                sess = self.manager.sessions.get(session_id)
                if sess is None:
                    return
                if result is True:
                    await sess.approve_plan()
                elif isinstance(result, str) and result:
                    await sess.reject_plan(reason=result)
                else:
                    await sess.reject_plan()
            finally:
                self._open_plan_modals.discard(session_id)

        self.run_worker(show_modal(), exclusive=False)

    def _open_cleanup_modal(self, session_id: str) -> None:
        if session_id in self._open_cleanup_modals:
            return
        sess = self.manager.sessions.get(session_id)
        if sess is None:
            return
        self._open_cleanup_modals.add(session_id)
        state = sess.state

        async def show_modal() -> None:
            try:
                confirmed = await self.push_screen_wait(
                    CleanupConfirmationModal(state=state)
                )
                if not confirmed:
                    return
                await self.manager.kill_session(session_id, cleanup_workspace=True)
                self.query_one(SessionListView).remove_session(session_id)
                self._event_log.pop(session_id, None)
                if self._selected_session_id == session_id:
                    self._selected_session_id = None
                    self.query_one(SessionView).show_session(None)
            finally:
                self._open_cleanup_modals.discard(session_id)

        self.run_worker(show_modal(), exclusive=False)


def main() -> int:
    log_path = state_mod.data_root() / "chud.log"
    logging.basicConfig(
        level=logging.INFO,
        filename=log_path,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ChudApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
