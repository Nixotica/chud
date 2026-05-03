from __future__ import annotations

import json
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, RichLog, Static

from chud.markup import (
    TextAttached,
    TextError,
    TextHeading,
    TextMuted,
    TextPlanBanner,
    TextPrompt,
    TextStatus,
    TextStatusChange,
    TextToolName,
    escape,
)
from chud.types import Event, EventKind, SessionState


class SessionView(Vertical):
    """Right pane: transcript log + input box."""

    DEFAULT_CSS = """
    SessionView {
        padding: 0 1;
    }
    SessionView #header {
        height: 1;
        color: $accent;
    }
    SessionView #transcript {
        border: solid $primary-background;
        padding: 0 1;
    }
    SessionView #input {
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "focus_transcript", "Focus transcript", show=False),
        Binding("j", "scroll_transcript_down", show=False),
        Binding("k", "scroll_transcript_up", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static("(no session selected)", id="header")
        yield RichLog(id="transcript", wrap=True, markup=True, highlight=True, auto_scroll=True)
        yield Input(placeholder="Send a message…  (Enter to send)", id="input")

    @property
    def transcript(self) -> RichLog:
        return self.query_one("#transcript", RichLog)

    @property
    def header(self) -> Static:
        return self.query_one("#header", Static)

    @property
    def input(self) -> Input:
        return self.query_one("#input", Input)

    def action_scroll_transcript_down(self) -> None:
        self.transcript.scroll_down()

    def action_scroll_transcript_up(self) -> None:
        self.transcript.scroll_up()

    def action_focus_transcript(self) -> None:
        """Move focus from the input back up to the transcript.

        No-op when no session is selected (the input is disabled in that case).
        """
        if self.input.disabled:
            return
        self.transcript.focus()

    def show_session(self, state: SessionState | None) -> None:
        self.transcript.clear()
        if state is None:
            self.header.update("(no session selected)")
            self.input.disabled = True
            return
        self.input.disabled = False
        self._refresh_header(state)

    def update_header(self, state: SessionState) -> None:
        self._refresh_header(state)

    def _refresh_header(self, state: SessionState) -> None:
        repo_names = sorted(wt.repo_path.name for wt in state.attached_repos.values())
        repos = ", ".join(repo_names) or "(no repos)"
        self.header.update(
            f"{TextHeading(state.id[:8])}  status={TextStatus(state.status.value)}  repos: {repos}"
        )

    def render_event(self, event: Event) -> None:
        kind = event.kind
        p = event.payload
        if kind == EventKind.TRANSCRIPT_APPENDED:
            role = escape(str(p.get("role", "?")))
            if "text" in p:
                self.transcript.write(f"{TextHeading(f'{role}:')} {escape(str(p['text']))}")
            elif "tool_use" in p:
                tu = p["tool_use"]
                self.transcript.write(
                    f"{TextMuted(f'{role} → tool:')} "
                    f"{TextToolName(escape(str(tu.get('name'))))} "
                    f"{escape(_short_json(tu.get('input')))}"
                )
            # Other transcript shapes (raw SystemMessage init blobs, UserMessage
            # tool_result echoes) are intentionally not rendered by default.
            # They remain in the in-memory event log and chud.log for debugging
            # and for a future verbose mode.
        elif kind == EventKind.STATUS_CHANGED:
            self.transcript.write(TextStatusChange(f"→ {escape(str(p.get('status')))}"))
        elif kind == EventKind.PLAN_PROPOSED:
            self.transcript.write(TextPlanBanner("── Plan proposed (modal will open) ──"))
        elif kind == EventKind.QUESTION_ASKED:
            qs = (p.get("input") or {}).get("questions") or []
            first = ""
            if qs and isinstance(qs[0], dict):
                first = str(qs[0].get("question") or qs[0].get("header") or "")
            preview = escape(first[:120]) if first else ""
            line = TextPrompt("? agent asked a question")
            if preview:
                line = f"{line}: {preview}"
            self.transcript.write(line)
        elif kind == EventKind.NEEDS_USER_INPUT:
            msg = p.get("message") or p.get("reason", "agent waiting")
            self.transcript.write(TextError(f"? agent needs input: {escape(str(msg))}"))
        elif kind == EventKind.REPO_ATTACHED:
            self.transcript.write(
                f"{TextAttached('+ attached:')} {escape(str(p.get('repo')))} → "
                f"{escape(str(p.get('worktree')))}"
            )
        elif kind == EventKind.UNKNOWN_MESSAGE:
            # Intentionally not rendered — kept in the event log + chud.log only.
            pass
        elif kind == EventKind.ERROR:
            self.transcript.write(f"{TextError('ERROR:')} {escape(str(p.get('error')))}")


def _short_json(obj: Any, limit: int = 120) -> str:
    try:
        s = json.dumps(obj, default=str)
    except Exception:
        s = str(obj)
    if len(s) > limit:
        s = s[:limit] + "…"
    return s
