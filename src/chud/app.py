from __future__ import annotations

import contextlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

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
from chud.widgets.pr_review_modal import PRReviewModal, PRReviewResult
from chud.widgets.question_modal import QuestionModal
from chud.widgets.session_list import SessionListView, SessionRow
from chud.widgets.session_view import SessionView

log = logging.getLogger(__name__)

PromptKind = Literal["plan", "pr_review", "cleanup", "input", "question"]


@dataclass
class PromptRequest:
    """A pending blocking user-input request from one agent session.

    Queued FIFO in ``ChudApp._prompt_queue`` so the first agent to ask for
    input keeps the screen until its prompt is resolved, instead of being
    covered by a later agent's modal.
    """

    session_id: str
    kind: PromptKind
    payload: dict[str, Any] = field(default_factory=dict)


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
        self._open_pr_review_modals: set[str] = set()
        self._open_question_modals: set[str] = set()
        # FIFO queue of blocking user-input requests from agents. The first
        # request is shown until resolved; later requests wait their turn so a
        # newly-arrived modal can't cover one the user hasn't answered yet.
        self._prompt_queue: list[PromptRequest] = []
        self._prompt_active: PromptRequest | None = None

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
        self.query_one(SessionListView).list_view.focus()
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
        self._drop_session_prompts(sid)

    # ------------------------------------------------------------------ list selection

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, SessionRow):
            self._select_session(event.item.session_id)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter on a sidebar row drops focus into the message input."""
        if isinstance(event.item, SessionRow):
            self._select_session(event.item.session_id)
            # Best-effort focus; mirrors the pattern in _show_input_focus.
            with contextlib.suppress(Exception):
                self.query_one(SessionView).input.focus()

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
            return
        # If the active prompt was an "input" request from this session, the
        # user has just answered it — advance the queue.
        if (
            self._prompt_active is not None
            and self._prompt_active.kind == "input"
            and self._prompt_active.session_id == sid
        ):
            self._resolve_active_prompt(self._prompt_active)

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

        # A stop-hook NEEDS_USER_INPUT followed by a ResultMessage DONE leaves
        # an "input" prompt active that nobody can resolve (the agent already
        # finished, so the user has nothing to type). That stranded prompt
        # blocks every later modal in the FIFO queue — including the
        # post-DONE cleanup modal. Drop stale "input" prompts whenever a
        # session leaves AWAITING_USER.
        if event.kind == EventKind.STATUS_CHANGED:
            new_status = event.payload.get("status")
            if new_status != SessionStatus.AWAITING_USER.value:
                self._drop_session_input_prompts(event.session_id)

        if event.kind == EventKind.PLAN_PROPOSED:
            self._enqueue_prompt(
                PromptRequest(
                    session_id=event.session_id,
                    kind="plan",
                    payload={"plan": event.payload.get("plan", "")},
                )
            )
        elif event.kind == EventKind.PR_REVIEW_REQUESTED:
            self._enqueue_prompt(
                PromptRequest(
                    session_id=event.session_id,
                    kind="pr_review",
                    payload={
                        "title": event.payload.get("title", ""),
                        "body": event.payload.get("body", ""),
                        "repos": list(event.payload.get("repos", []) or []),
                    },
                )
            )
        elif event.kind == EventKind.QUESTION_ASKED:
            self._enqueue_prompt(
                PromptRequest(
                    session_id=event.session_id,
                    kind="question",
                    payload={"input": event.payload.get("input", {})},
                )
            )
        elif event.kind == EventKind.CLEANUP_REQUESTED:
            self._enqueue_prompt(
                PromptRequest(session_id=event.session_id, kind="cleanup")
            )
        elif event.kind == EventKind.NEEDS_USER_INPUT:
            self._enqueue_prompt(
                PromptRequest(
                    session_id=event.session_id,
                    kind="input",
                    payload={
                        "message": event.payload.get("message", ""),
                        "reason": event.payload.get("reason", ""),
                    },
                )
            )
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
        elif event.kind == EventKind.WORKTREE_DISCARDED:
            branch = event.payload.get("branch", "")
            repo = event.payload.get("repo", "")
            view = self.query_one(SessionView)
            view.transcript.write(
                f"[dim]· discarded empty branch {branch}"
                + (f" ({repo})" if repo else "")
                + "[/dim]"
            )

    # ------------------------------------------------------------------ prompt queue

    def _enqueue_prompt(self, req: PromptRequest) -> None:
        """Append a blocking user-input request to the FIFO queue.

        Dedups against both the active prompt and anything already queued for
        the same (session, kind) pair so duplicate events (e.g. a re-fired
        notification hook) don't stack the same prompt twice. Triggers the
        next-prompt dispatcher in case nothing is currently shown.
        """
        if (
            self._prompt_active is not None
            and self._prompt_active.session_id == req.session_id
            and self._prompt_active.kind == req.kind
        ):
            return
        if any(
            p.session_id == req.session_id and p.kind == req.kind
            for p in self._prompt_queue
        ):
            return
        self._prompt_queue.append(req)
        self._maybe_show_next_prompt()

    def _maybe_show_next_prompt(self) -> None:
        """If nothing is currently shown, pop the next request and show it."""
        if self._prompt_active is not None:
            return
        # Skip requests for sessions that no longer exist (killed mid-queue).
        while self._prompt_queue:
            req = self._prompt_queue.pop(0)
            if self.manager.sessions.get(req.session_id) is None:
                continue
            self._prompt_active = req
            if req.kind == "plan":
                self._show_plan_modal(req)
            elif req.kind == "pr_review":
                self._show_pr_review_modal(req)
            elif req.kind == "cleanup":
                self._show_cleanup_modal(req)
            elif req.kind == "input":
                self._show_input_focus(req)
            elif req.kind == "question":
                self._show_question_modal(req)
            return

    def _resolve_active_prompt(self, req: PromptRequest) -> None:
        """Mark the active prompt resolved and advance the queue.

        Idempotent: only clears the active slot if `req` is still the one
        showing (a session-kill could have already swapped it out).
        """
        if self._prompt_active is req:
            self._prompt_active = None
        self._maybe_show_next_prompt()

    def _drop_session_prompts(self, session_id: str) -> None:
        """Remove any queued/active prompts for a session that's going away."""
        self._prompt_queue = [
            p for p in self._prompt_queue if p.session_id != session_id
        ]
        if (
            self._prompt_active is not None
            and self._prompt_active.session_id == session_id
        ):
            self._prompt_active = None
            self._maybe_show_next_prompt()

    def _drop_session_input_prompts(self, session_id: str) -> None:
        """Clear stale 'input' prompts when a session leaves AWAITING_USER."""
        self._prompt_queue = [
            p for p in self._prompt_queue
            if not (p.session_id == session_id and p.kind == "input")
        ]
        if (
            self._prompt_active is not None
            and self._prompt_active.session_id == session_id
            and self._prompt_active.kind == "input"
        ):
            self._prompt_active = None
            self._maybe_show_next_prompt()

    # ------------------------------------------------------------------ prompt renderers

    def _show_plan_modal(self, req: PromptRequest) -> None:
        session_id = req.session_id
        plan_text = req.payload.get("plan", "")
        if session_id in self._open_plan_modals:
            # Should be impossible given the queue dedup above, but guard anyway.
            self._resolve_active_prompt(req)
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
                self._resolve_active_prompt(req)

        self.run_worker(show_modal(), exclusive=False)

    def _show_cleanup_modal(self, req: PromptRequest) -> None:
        session_id = req.session_id
        if session_id in self._open_cleanup_modals:
            self._resolve_active_prompt(req)
            return
        sess = self.manager.sessions.get(session_id)
        if sess is None:
            self._resolve_active_prompt(req)
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
                # Drop any other queued prompts (e.g. a stale PLAN_PROPOSED)
                # for the now-killed session so the queue doesn't try to
                # re-show them.
                self._drop_session_prompts(session_id)
            finally:
                self._open_cleanup_modals.discard(session_id)
                self._resolve_active_prompt(req)

        self.run_worker(show_modal(), exclusive=False)

    def _show_pr_review_modal(self, req: PromptRequest) -> None:
        session_id = req.session_id
        if session_id in self._open_pr_review_modals:
            self._resolve_active_prompt(req)
            return
        if self.manager.sessions.get(session_id) is None:
            self._resolve_active_prompt(req)
            return
        self._open_pr_review_modals.add(session_id)
        title = str(req.payload.get("title", "") or "")
        body = str(req.payload.get("body", "") or "")
        repos = list(req.payload.get("repos", []) or [])

        async def show_modal() -> None:
            try:
                result: PRReviewResult | None = await self.push_screen_wait(
                    PRReviewModal(
                        session_id=session_id,
                        title=title,
                        body=body,
                        repos=repos,
                    )
                )
                # ``None`` (e.g. unexpected dismissal) is treated as a reject so
                # the post-PR cleanup prompt — if enabled — still progresses.
                if result is None:
                    accepted = False
                    edited_title: str | None = None
                    edited_body: str | None = None
                else:
                    accepted = result.accepted
                    edited_title = result.title
                    edited_body = result.body
                try:
                    await self.manager.submit_pr_review(
                        session_id,
                        accepted=accepted,
                        title=edited_title,
                        body=edited_body,
                    )
                except Exception as e:
                    log.exception("submit_pr_review failed for %s", session_id)
                    self.notify(f"PR review failed: {e}", severity="error")
            finally:
                self._open_pr_review_modals.discard(session_id)
                self._resolve_active_prompt(req)

        self.run_worker(show_modal(), exclusive=False)

    def _show_question_modal(self, req: PromptRequest) -> None:
        session_id = req.session_id
        question_input = req.payload.get("input", {}) or {}
        if session_id in self._open_question_modals:
            self._resolve_active_prompt(req)
            return
        self._open_question_modals.add(session_id)

        async def show_modal() -> None:
            try:
                result = await self.push_screen_wait(
                    QuestionModal(session_id=session_id, question_input=question_input)
                )
                sess = self.manager.sessions.get(session_id)
                if sess is None:
                    return
                if isinstance(result, str) and result:
                    await sess.answer_question(result)
                else:
                    await sess.answer_question(
                        "(user dismissed the question without answering)"
                    )
            finally:
                self._open_question_modals.discard(session_id)
                self._resolve_active_prompt(req)

        self.run_worker(show_modal(), exclusive=False)

    def _show_input_focus(self, req: PromptRequest) -> None:
        """Auto-select the asking session and focus the input box.

        Unlike the modal kinds, this prompt has no screen to wait on; it
        resolves in ``on_input_submitted`` when the user actually replies.
        """
        if self.manager.sessions.get(req.session_id) is None:
            self._resolve_active_prompt(req)
            return
        self._select_session(req.session_id)
        try:
            self.query_one(SessionView).input.focus()
        except Exception:
            # Focusing is best-effort; the prompt is still considered "shown".
            pass


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
