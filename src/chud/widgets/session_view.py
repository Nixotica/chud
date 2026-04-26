from __future__ import annotations

import json
from typing import Any

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, RichLog, Static

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
            f"[bold]{state.id[:8]}[/bold]  status=[cyan]{state.status.value}[/cyan]  "
            f"repos: {repos}"
        )

    def render_event(self, event: Event) -> None:
        kind = event.kind
        p = event.payload
        if kind == EventKind.TRANSCRIPT_APPENDED:
            role = escape(str(p.get("role", "?")))
            if "text" in p:
                self.transcript.write(f"[bold]{role}:[/bold] {escape(str(p['text']))}")
            elif "tool_use" in p:
                tu = p["tool_use"]
                self.transcript.write(
                    f"[dim]{role} → tool:[/dim] [yellow]{escape(str(tu.get('name')))}[/yellow] "
                    f"{escape(_short_json(tu.get('input')))}"
                )
            # Other transcript shapes (raw SystemMessage init blobs, UserMessage
            # tool_result echoes) are intentionally not rendered by default.
            # They remain in the in-memory event log and chud.log for debugging
            # and for a future verbose mode.
        elif kind == EventKind.STATUS_CHANGED:
            self.transcript.write(
                f"[dim italic]→ {escape(str(p.get('status')))}[/dim italic]"
            )
        elif kind == EventKind.PLAN_PROPOSED:
            self.transcript.write(
                "[bold magenta]── Plan proposed (modal will open) ──[/bold magenta]"
            )
        elif kind == EventKind.NEEDS_USER_INPUT:
            msg = p.get("message") or p.get("reason", "agent waiting")
            self.transcript.write(f"[bold red]? agent needs input: {escape(str(msg))}[/bold red]")
        elif kind == EventKind.REPO_ATTACHED:
            self.transcript.write(
                f"[blue]+ attached:[/blue] {escape(str(p.get('repo')))} → "
                f"{escape(str(p.get('worktree')))}"
            )
        elif kind == EventKind.UNKNOWN_MESSAGE:
            # Intentionally not rendered — kept in the event log + chud.log only.
            pass
        elif kind == EventKind.ERROR:
            self.transcript.write(f"[bold red]ERROR:[/bold red] {escape(str(p.get('error')))}")


def _short_json(obj: Any, limit: int = 120) -> str:
    try:
        s = json.dumps(obj, default=str)
    except Exception:
        s = str(obj)
    if len(s) > limit:
        s = s[:limit] + "…"
    return s
