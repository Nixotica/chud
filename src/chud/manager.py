from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from pathlib import Path

from chud import pr as pr_mod
from chud import state as state_mod
from chud.notify import desktop_notify
from chud.options import OPT_MAKE_DRAFT_PR, OPT_SELF_CLEANUP, normalize_options
from chud.session import AgentSession
from chud.types import Event, EventKind, SessionState, SessionStatus
from chud.worktree import WorktreeManager, is_git_repo

log = logging.getLogger(__name__)


def _new_session_id() -> str:
    return secrets.token_hex(4)


class SessionManager:
    """Owns all AgentSessions and fans their events to UI subscribers."""

    def __init__(self) -> None:
        self.sessions: dict[str, AgentSession] = {}
        self.worktrees: dict[str, WorktreeManager] = {}
        self._fanout_tasks: dict[str, asyncio.Task[None]] = {}
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._notify_enabled: bool = True
        self._tui_focused: bool = True
        self._done_handled: set[str] = set()
        self._pr_tasks: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ subscribe

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def set_focus(self, focused: bool) -> None:
        self._tui_focused = focused

    # ------------------------------------------------------------------ create

    async def create_session(
        self,
        prompt: str,
        repo_path: Path | None = None,
        model: str | None = None,
        options: dict[str, bool] | None = None,
    ) -> AgentSession:
        sid = _new_session_id()
        workspace = state_mod.workspaces_root() / sid
        st = SessionState(
            id=sid,
            workspace_dir=workspace,
            initial_prompt=prompt,
            options=normalize_options(options),
        )

        wt_mgr = WorktreeManager(st)
        if repo_path is not None:
            if not is_git_repo(repo_path):
                raise ValueError(f"{repo_path} is not a git repo")
            wt = wt_mgr.attach_repo(repo_path)
            await self._broadcast(
                Event(
                    session_id=sid,
                    kind=EventKind.REPO_ATTACHED,
                    payload={"repo": str(wt.repo_path), "worktree": str(wt.worktree_path)},
                )
            )

        sess = AgentSession(st, model=model)
        self.sessions[sid] = sess
        self.worktrees[sid] = wt_mgr

        self._fanout_tasks[sid] = asyncio.create_task(
            self._fan_out(sess), name=f"chud-fanout-{sid}"
        )

        await sess.start(prompt)
        self._persist()
        return sess

    async def attach_repo(self, session_id: str, repo_path: Path) -> None:
        wt_mgr = self.worktrees[session_id]
        if not is_git_repo(repo_path):
            raise ValueError(f"{repo_path} is not a git repo")
        wt = wt_mgr.attach_repo(repo_path)
        self._persist()
        await self._broadcast(
            Event(
                session_id=session_id,
                kind=EventKind.REPO_ATTACHED,
                payload={"repo": str(wt.repo_path), "worktree": str(wt.worktree_path)},
            )
        )

    # ------------------------------------------------------------------ teardown

    async def shutdown(self) -> None:
        for sid in list(self.sessions):
            await self.kill_session(sid)

    async def kill_session(
        self, session_id: str, cleanup_workspace: bool = False
    ) -> None:
        sess = self.sessions.pop(session_id, None)
        task = self._fanout_tasks.pop(session_id, None)
        wt_mgr = self.worktrees.pop(session_id, None)
        done_task = self._pr_tasks.pop(session_id, None)
        self._done_handled.discard(session_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if done_task is not None and not done_task.done():
            done_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await done_task
        if sess is not None:
            await sess.stop()
        if cleanup_workspace and wt_mgr is not None:
            try:
                wt_mgr.cleanup_workspace()
            except Exception:
                log.exception("cleanup_workspace failed for session %s", session_id)
        self._persist()

    # ------------------------------------------------------------------ event fanout

    async def _fan_out(self, sess: AgentSession) -> None:
        try:
            while True:
                event = await sess.events.get()
                await self._handle_event(event, sess)
                await self._broadcast(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("fanout crashed for session %s", sess.state.id)

    async def _broadcast(self, event: Event) -> None:
        for q in list(self._subscribers):
            await q.put(event)

    async def _handle_event(self, event: Event, sess: AgentSession) -> None:
        if event.kind == EventKind.STATUS_CHANGED:
            self._persist()
            status = event.payload.get("status")
            should_notify = (
                status == SessionStatus.DONE.value
                and self._notify_enabled
                and not self._tui_focused
            )
            if should_notify:
                desktop_notify("chud", f"Session {event.session_id[:6]} finished")
            if status == SessionStatus.DONE.value:
                await self._on_session_done(sess)
        elif event.kind == EventKind.NEEDS_USER_INPUT:
            if self._notify_enabled and not self._tui_focused:
                desktop_notify("chud", f"Session {event.session_id[:6]} needs input")
        elif event.kind == EventKind.PLAN_PROPOSED:
            if self._notify_enabled and not self._tui_focused:
                desktop_notify("chud", f"Session {event.session_id[:6]} has a plan to review")
        elif event.kind == EventKind.ERROR:
            self._persist()
            if self._notify_enabled and not self._tui_focused:
                desktop_notify("chud", f"Session {event.session_id[:6]} errored")

    # ------------------------------------------------------------------ DONE side-effects

    async def _on_session_done(self, sess: AgentSession) -> None:
        sid = sess.state.id
        if sid in self._done_handled:
            return
        self._done_handled.add(sid)
        opts = sess.state.options or {}
        make_pr = bool(opts.get(OPT_MAKE_DRAFT_PR))
        do_cleanup = bool(opts.get(OPT_SELF_CLEANUP))
        if not (make_pr or do_cleanup):
            return
        if not make_pr:
            await self._broadcast(
                Event(session_id=sid, kind=EventKind.CLEANUP_REQUESTED, payload={})
            )
            return
        task = asyncio.create_task(
            self._finish_done(sess.state, do_cleanup), name=f"chud-done-{sid}"
        )
        self._pr_tasks[sid] = task
        task.add_done_callback(lambda _t, s=sid: self._pr_tasks.pop(s, None))

    async def _finish_done(self, state: SessionState, request_cleanup: bool) -> None:
        await self._publish_prs(state)
        if request_cleanup:
            await self._broadcast(
                Event(session_id=state.id, kind=EventKind.CLEANUP_REQUESTED, payload={})
            )

    async def _publish_prs(self, state: SessionState) -> None:
        try:
            results = await pr_mod.publish_draft_prs(state)
        except Exception as e:
            log.exception("publish_draft_prs crashed for session %s", state.id)
            await self._broadcast(
                Event(
                    session_id=state.id,
                    kind=EventKind.PR_FAILED,
                    payload={"repo": "", "branch": "", "error": repr(e)},
                )
            )
            return
        wt_mgr = self.worktrees.get(state.id)
        any_discarded_branches = False
        for r in results:
            if r.discarded:
                if wt_mgr is not None and r.repo_label:
                    try:
                        wt_mgr.discard_empty_branch(r.repo_label)
                        any_discarded_branches = True
                    except Exception:
                        log.exception(
                            "discard_empty_branch failed for %s in session %s",
                            r.repo_label,
                            state.id,
                        )
                await self._broadcast(
                    Event(
                        session_id=state.id,
                        kind=EventKind.WORKTREE_DISCARDED,
                        payload={"repo": r.repo_label, "branch": r.branch},
                    )
                )
            elif r.error is None and r.url:
                await self._broadcast(
                    Event(
                        session_id=state.id,
                        kind=EventKind.PR_PUBLISHED,
                        payload={
                            "repo": r.repo_label,
                            "branch": r.branch,
                            "url": r.url,
                        },
                    )
                )
            else:
                await self._broadcast(
                    Event(
                        session_id=state.id,
                        kind=EventKind.PR_FAILED,
                        payload={
                            "repo": r.repo_label,
                            "branch": r.branch,
                            "error": r.error or "unknown error",
                        },
                    )
                )
        if any_discarded_branches:
            self._persist()

    # ------------------------------------------------------------------ persistence

    def _persist(self) -> None:
        try:
            state_mod.save_all({sid: s.state for sid, s in self.sessions.items()})
        except Exception:
            log.exception("failed to persist sessions")
