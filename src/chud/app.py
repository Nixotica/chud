from __future__ import annotations

import contextlib
import logging
import os
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, ScrollableContainer
from textual.widget import Widget
from textual.widgets import Footer, Header, Input, ListView, RichLog

from chud import gh as gh_mod
from chud import state as state_mod
from chud.gh import Issue
from chud.manager import SessionManager
from chud.markup import TextError, TextMuted, TextSuccess
from chud.types import Event, EventKind, SessionStatus
from chud.widgets.cleanup_confirmation_modal import CleanupConfirmationModal
from chud.widgets.new_session_modal import NewSessionModal, NewSessionResult
from chud.widgets.permission_modal import PermissionDecision, PermissionModal
from chud.widgets.plan_modal import PlanApprovalModal, PlanDecision
from chud.widgets.pr_review_modal import PRReviewModal, PRReviewResult
from chud.widgets.question_modal import QuestionModal
from chud.widgets.session_list import SessionListView, SessionRow
from chud.widgets.session_view import SessionView
from chud.widgets.settings_modal import SettingsModal
from chud.worktree import detect_cwd_repo

log = logging.getLogger(__name__)

PromptKind = Literal["plan", "pr_review", "cleanup", "input", "question", "permission"]

DevHook = Callable[["ChudApp"], Awaitable[None]]


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
    SUB_TITLE = "multi-agent Claude HUD"

    BINDINGS = [
        ("n", "new_session", "New session"),
        ("x", "kill_session", "Kill session"),
        ("s", "settings", "Settings"),
        ("q", "quit", "Quit"),
        # Vim-style scroll on the focused pane (transcript / session list).
        # Textual won't deliver these to the app while a text input owns focus,
        # so typing `j`/`k` into the message box still inserts the literal
        # character.
        ("j", "scroll_focus_down", "Scroll down"),
        ("k", "scroll_focus_up", "Scroll up"),
    ]

    CSS = """
    Horizontal#main {
        height: 1fr;
    }
    SessionView {
        width: 1fr;
    }
    """

    def __init__(self, dev_hook: DevHook | None = None) -> None:
        super().__init__()
        self._dev_hook = dev_hook
        self.manager = SessionManager()
        self._event_log: dict[str, list[Event]] = defaultdict(list)
        self._selected_session_id: str | None = None
        self._open_plan_modals: set[str] = set()
        self._open_cleanup_modals: set[str] = set()
        self._open_pr_review_modals: set[str] = set()
        self._open_question_modals: set[str] = set()
        self._open_permission_modals: set[str] = set()
        # FIFO queue of blocking user-input requests from agents. The first
        # request is shown until resolved; later requests wait their turn so a
        # newly-arrived modal can't cover one the user hasn't answered yet.
        self._prompt_queue: list[PromptRequest] = []
        self._prompt_active: PromptRequest | None = None
        # Number of user-initiated modals currently on screen (NewSession,
        # Settings). While > 0, session-driven prompts stay queued so they
        # can't pop over a modal the user is actively typing into.
        self._user_modal_depth: int = 0
        # Launch-time repo + cached open issues for the new-session modal.
        # Populated in on_mount() and refreshed in the background so pressing
        # `n` doesn't pay for a GitHub API round-trip each time.
        # ``_issues_cache`` mirrors ``_fetch_issues_for_modal``'s contract:
        # ``None`` means "hide picker" (no auth / no repo / first refresh
        # in-flight / no open issues), non-empty list means ready.
        # ``_issues_with_pr_cache`` carries the set of issue numbers that
        # have an open PR linked (closing-keyword or Development-sidebar);
        # populated by ``gh.list_issue_numbers_with_open_pr`` in the same
        # background refresh and used to filter the picker.
        self._launch_repo: Path | None = None
        self._issues_cache: list[Issue] | None = None
        self._issues_with_pr_cache: set[int] = set()
        self._issues_timer: Any = None  # textual.timer.Timer; loose typing.

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
        if self._dev_hook is not None:
            self.run_worker(self._dev_hook(self), exclusive=False, name="dev-hook")
        # Detect launch repo once + start background issue refresh so the
        # new-session picker is ready when the user presses `n`. Skip when
        # gh isn't installed (no point polling) or chud was launched outside
        # any git repo (nothing to fetch).
        self._launch_repo = detect_cwd_repo()
        if self._launch_repo is not None and gh_mod.is_available():
            self.run_worker(self._refresh_issues_cache(), exclusive=False, name="issues-init")
            # Periodic refresh so a fresh issue created during the session
            # appears within a couple of minutes. Open-issue lists don't churn
            # often, so 120s is a generous floor that keeps the cost trivial.
            self._issues_timer = self.set_interval(120.0, self._schedule_issues_refresh)

    def _schedule_issues_refresh(self) -> None:
        """Timer callback — schedule a fresh fetch as a background worker."""
        self.run_worker(self._refresh_issues_cache(), exclusive=False, name="issues-refresh")

    def _active_sessions_by_issue(self) -> dict[int, list[str]]:
        """Map of GitHub issue number → session ids of *active* chud sessions
        currently linked to that issue.

        "Active" means status is anything other than DONE/ERRORED — i.e. a
        chud is still working on it. Used by ``NewSessionModal`` to mark
        issues in the picker dropdown so the user knows another session is
        already on it before they spin up a duplicate.
        """
        result: dict[int, list[str]] = {}
        terminal = {SessionStatus.DONE, SessionStatus.ERRORED}
        for sid, sess in self.manager.sessions.items():
            issue_num = sess.state.issue_number
            if issue_num is None or sess.state.status in terminal:
                continue
            result.setdefault(issue_num, []).append(sid)
        return result

    async def _refresh_issues_cache(self) -> None:
        """Fetch the launch repo's open issues + PR-linked issue numbers and
        update both caches.

        On any failure (logged inside the gh helpers) we keep the previous
        cached value rather than blanking the picker — a stale list is
        better than no list at the moment the user presses `n`.
        """
        if self._launch_repo is None:
            return
        self._issues_cache = await self._fetch_issues_for_modal(self._launch_repo)
        # Only fetch the PR-linked set if we actually have issues (otherwise
        # there's nothing to filter and the GraphQL round-trip is wasted).
        if self._issues_cache:
            self._issues_with_pr_cache = await gh_mod.list_issue_numbers_with_open_pr(
                self._launch_repo
            )
        else:
            self._issues_with_pr_cache = set()

    async def on_unmount(self) -> None:
        if self._issues_timer is not None:
            with contextlib.suppress(Exception):
                self._issues_timer.stop()
            self._issues_timer = None
        await self.manager.shutdown()

    # ------------------------------------------------------------------ focus

    def on_app_focus(self) -> None:
        self.manager.set_focus(True)

    def on_app_blur(self) -> None:
        self.manager.set_focus(False)

    # ------------------------------------------------------------------ actions

    def action_new_session(self) -> None:
        self.run_worker(self._new_session_flow(), exclusive=False)

    async def _fetch_issues_for_modal(self, repo_path: Path) -> list[Issue] | None:
        """Best-effort issue list for the new-session picker; ``None`` to hide.

        Returns ``None`` when ``gh`` isn't installed *or* when the call
        succeeded but the repo has no open issues — both collapse to the
        same hide-the-picker branch in the modal. ``gh.list_issues`` already
        swallows network/auth/JSON failures internally.
        """
        if not gh_mod.is_available():
            return None
        try:
            issues = await gh_mod.list_issues(repo_path)
        except Exception:
            log.exception("gh.list_issues raised in %s", repo_path)
            return None
        return issues or None

    async def _new_session_flow(self) -> None:
        # Detect the cwd repo synchronously (it's a quick `git rev-parse`).
        # For the issue picker, consume the background-refreshed cache —
        # awaiting `gh issue list` here was the source of the `n`-press lag.
        # If the cache hasn't loaded yet (very first `n` within ~hundreds of
        # ms of startup, or chud launched outside a repo) the picker is just
        # hidden for this open and the modal still appears instantly.
        detected = detect_cwd_repo()
        issues = self._issues_cache if detected is not None else None
        if issues and self._issues_with_pr_cache:
            # Drop issues that already have an open PR linked to them
            # (closing-keyword reference or Development-sidebar link) — the
            # user presumably doesn't want to spawn a duplicate chud on
            # something already in review, regardless of who or what made
            # the PR. The set comes from the same background refresh that
            # populated ``_issues_cache``.
            issues = [i for i in issues if i.number not in self._issues_with_pr_cache]
        active_by_issue = self._active_sessions_by_issue() if issues else {}

        self._user_modal_depth += 1
        try:
            result: NewSessionResult | None = await self.push_screen_wait(
                NewSessionModal(issues=issues, active_sessions_by_issue=active_by_issue)
            )
        finally:
            self._user_modal_depth -= 1
            self._maybe_show_next_prompt()
        if result is None:
            return
        if result.issue is not None:
            # The modal already pre-populated the prompt with the issue's
            # head + body + "---" separator (see NewSessionModal.on_select_changed),
            # so result.prompt is already the merged text — no second prepend.
            log.info("new session linked to issue #%d", result.issue.number)
        final_prompt = result.prompt
        launch = None if detected is not None else Path.cwd()
        try:
            sess = await self.manager.create_session(
                prompt=final_prompt,
                repo_path=detected,
                launch_cwd=launch,
                options=result.options,
                effort=result.effort,
                issue_number=result.issue.number if result.issue is not None else None,
                run_mode=result.run_mode,
            )
        except Exception as e:
            log.exception("create_session failed")
            self.notify(f"Failed to start session: {e}", severity="error")
            return
        self.query_one(SessionListView).add_session(sess.state)
        self._select_session(sess.state.id)

    def action_settings(self) -> None:
        self.run_worker(self._settings_flow(), exclusive=False)

    async def _settings_flow(self) -> None:
        # Treat the settings modal like NewSession/AttachRepo: bump the user
        # modal depth so any session-driven prompt (PLAN, CLEANUP, etc.) waits
        # in the FIFO queue instead of popping over the modal mid-edit.
        self._user_modal_depth += 1
        try:
            await self.push_screen_wait(SettingsModal())
        finally:
            self._user_modal_depth -= 1
            self._maybe_show_next_prompt()

    def action_scroll_focus_down(self) -> None:
        self._scroll_focused(down=True)

    def action_scroll_focus_up(self) -> None:
        self._scroll_focused(down=False)

    def _scroll_focused(self, *, down: bool) -> None:
        """Scroll the nearest scrollable ancestor of ``self.focused``.

        For ``ListView`` we move the highlight (which auto-scrolls) so
        ``j``/``k`` matches the existing arrow-key behaviour. For other
        scrollables we just nudge ``scroll_y`` by one line.
        """
        target: Widget | None = self.focused
        while target is not None:
            if isinstance(target, ListView):
                if down:
                    target.action_cursor_down()
                else:
                    target.action_cursor_up()
                return
            if isinstance(target, RichLog | ScrollableContainer):
                if down:
                    target.scroll_down()
                else:
                    target.scroll_up()
                return
            target = target.parent if isinstance(target.parent, Widget) else None

    async def action_kill_session(self) -> None:
        sid = self._selected_session_id
        if sid is None:
            return
        await self._kill_session_and_cleanup(sid)

    async def _kill_session_and_cleanup(self, sid: str) -> None:
        """Kill ``sid`` and reset all UI/state slots that referenced it.

        Shared by the ``x`` keybinding, the cleanup-modal flow, and the new
        Kill buttons on the plan / permission modals so they can't drift apart.
        """
        await self.manager.kill_session(sid)
        self.query_one(SessionListView).remove_session(sid)
        self._event_log.pop(sid, None)
        if self._selected_session_id == sid:
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
        elif event.kind == EventKind.PERMISSION_REQUESTED:
            # The session emits a fresh dict; copy here is the only one needed
            # to isolate the modal's view of tool_input from the SDK round-trip.
            self._enqueue_prompt(
                PromptRequest(
                    session_id=event.session_id,
                    kind="permission",
                    payload={
                        "tool_name": event.payload.get("tool_name", ""),
                        "tool_input": dict(event.payload.get("tool_input", {})),
                    },
                )
            )
        elif event.kind == EventKind.CLEANUP_REQUESTED:
            self._enqueue_prompt(
                PromptRequest(
                    session_id=event.session_id,
                    kind="cleanup",
                    payload={
                        "published_prs": list(event.payload.get("published_prs", []) or []),
                    },
                )
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
            view.transcript.write(f"{TextSuccess('+ draft PR:')} {repo} → {url}")
            # Refresh the issues + PR-link caches now so the next `n` press
            # filters out the issue this PR just attached to, instead of
            # waiting for the 120s background tick. GitHub may need a moment
            # to register the Development-sidebar link, so this is best-
            # effort — the periodic refresh will catch any lag.
            if self._launch_repo is not None:
                self.run_worker(
                    self._refresh_issues_cache(), exclusive=False, name="issues-pr-refresh"
                )
        elif event.kind == EventKind.PR_FAILED:
            err = event.payload.get("error", "")
            repo = event.payload.get("repo", "")
            self.notify(f"PR failed ({repo}): {err}", severity="error")
            view = self.query_one(SessionView)
            view.transcript.write(f"{TextError('! PR failed:')} {repo or '(session)'}: {err}")
        elif event.kind == EventKind.WORKTREE_DISCARDED:
            branch = event.payload.get("branch", "")
            repo = event.payload.get("repo", "")
            view = self.query_one(SessionView)
            view.transcript.write(
                TextMuted(f"· discarded empty branch {branch}" + (f" ({repo})" if repo else ""))
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
        if any(p.session_id == req.session_id and p.kind == req.kind for p in self._prompt_queue):
            return
        self._prompt_queue.append(req)
        self._maybe_show_next_prompt()

    def _maybe_show_next_prompt(self) -> None:
        """If nothing is currently shown, pop the next request and show it."""
        if self._prompt_active is not None:
            return
        if self._user_modal_depth > 0:
            # A user-initiated modal (NewSession/Settings) is on screen;
            # don't pop a session-driven prompt over it. The flow that closes
            # the user modal calls back into us once it's gone.
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
            elif req.kind == "permission":
                self._show_permission_modal(req)
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
        self._prompt_queue = [p for p in self._prompt_queue if p.session_id != session_id]
        if self._prompt_active is not None and self._prompt_active.session_id == session_id:
            self._prompt_active = None
            self._maybe_show_next_prompt()

    def _drop_session_input_prompts(self, session_id: str) -> None:
        """Clear stale 'input' prompts when a session leaves AWAITING_USER."""
        self._prompt_queue = [
            p for p in self._prompt_queue if not (p.session_id == session_id and p.kind == "input")
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
                if isinstance(result, PlanDecision) and result.action == "kill":
                    # Kill resolves the session's pending plan future via
                    # AgentSession.stop() before tearing the session down, so
                    # the SDK doesn't see a stranded control request.
                    await self._kill_session_and_cleanup(session_id)
                    return
                sess = self.manager.sessions.get(session_id)
                if sess is None:
                    return
                if isinstance(result, PlanDecision) and result.action == "approve":
                    await sess.approve_plan()
                elif isinstance(result, PlanDecision) and result.reason:
                    await sess.reject_plan(reason=result.reason)
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
        published_prs = list(req.payload.get("published_prs", []) or [])

        async def show_modal() -> None:
            try:
                confirmed = await self.push_screen_wait(
                    CleanupConfirmationModal(state=state, published_prs=published_prs)
                )
                if not confirmed:
                    return
                # Cleanup variant — kill_session(cleanup_worktrees=True) tears
                # down the worktree as well as the in-memory session. We then
                # mirror the same UI/state reset the shared helper does, since
                # the helper doesn't accept the cleanup flag.
                await self.manager.kill_session(session_id, cleanup_worktrees=True)
                self.query_one(SessionListView).remove_session(session_id)
                self._event_log.pop(session_id, None)
                if self._selected_session_id == session_id:
                    self._selected_session_id = None
                    self.query_one(SessionView).show_session(None)
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
                    await sess.answer_question("(user dismissed the question without answering)")
            finally:
                self._open_question_modals.discard(session_id)
                self._resolve_active_prompt(req)

        self.run_worker(show_modal(), exclusive=False)

    def _show_permission_modal(self, req: PromptRequest) -> None:
        session_id = req.session_id
        tool_name = str(req.payload.get("tool_name", ""))
        # No defensive copy: the dict was already isolated when the event
        # was enqueued in ``_on_event``.
        tool_input = req.payload.get("tool_input", {})
        if not isinstance(tool_input, dict):
            tool_input = {}
        if session_id in self._open_permission_modals:
            self._resolve_active_prompt(req)
            return
        self._open_permission_modals.add(session_id)

        async def show_modal() -> None:
            try:
                result = await self.push_screen_wait(
                    PermissionModal(
                        session_id=session_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                    )
                )
                if isinstance(result, PermissionDecision) and result.action == "kill":
                    # AgentSession.stop() resolves the pending permission
                    # future with a Deny so the SDK doesn't hang on the
                    # in-flight tool call.
                    await self._kill_session_and_cleanup(session_id)
                    return
                sess = self.manager.sessions.get(session_id)
                if sess is None:
                    return
                # ``None`` (unexpected dismissal) falls through to a plain
                # deny so the agent gets a deterministic answer either way.
                if isinstance(result, PermissionDecision) and result.action == "approve":
                    await sess.approve_tool()
                elif isinstance(result, PermissionDecision) and result.reason:
                    await sess.deny_tool(reason=result.reason)
                else:
                    await sess.deny_tool()
            finally:
                self._open_permission_modals.discard(session_id)
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
        with contextlib.suppress(Exception):
            self.query_one(SessionView).input.focus()


def _muzzle_credential_prompts() -> None:
    """Stop sub-``git`` invocations from blocking on a credential prompt.

    The TUI runs in raw mode, so any tool that tries to read a password
    from the controlling TTY hangs the screen. Setting these env vars at
    startup makes ``git push`` / ``git fetch`` / ssh fail fast with an
    auth error instead — that surfaces as a ``PR_FAILED`` toast or a
    log-warn fetch fallthrough, both of which the UI handles gracefully.
    """
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    os.environ.setdefault("GIT_ASKPASS", "/bin/true")
    os.environ.setdefault("SSH_ASKPASS", "/bin/true")
    os.environ.setdefault("SSH_ASKPASS_REQUIRE", "never")


def main() -> int:
    import argparse

    from chud import __version__
    from chud.dev import SCENARIOS

    _muzzle_credential_prompts()

    parser = argparse.ArgumentParser(prog="chud")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--dev",
        choices=sorted(SCENARIOS.keys()),
        default=None,
        help="(pre-release) launch the TUI with a dev scenario on top",
    )
    parser.add_argument(
        "--seed",
        type=Path,
        default=None,
        help="(pre-release) JSON file overriding the default seed for --dev",
    )
    args = parser.parse_args()

    log_path = state_mod.data_root() / "chud.log"
    logging.basicConfig(
        level=logging.INFO,
        filename=log_path,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    dev_hook = SCENARIOS[args.dev](args.seed) if args.dev else None
    ChudApp(dev_hook=dev_hook).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
