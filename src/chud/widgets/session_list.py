from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Label, ListItem, ListView

from chud.types import SessionState, SessionStatus

STATUS_GLYPH: dict[SessionStatus, str] = {
    SessionStatus.NEW: "·",
    SessionStatus.PLANNING: "◌",
    SessionStatus.AWAITING_PLAN_APPROVAL: "▶",
    SessionStatus.EXECUTING: "●",
    SessionStatus.AWAITING_USER: "?",
    SessionStatus.DONE: "✓",
    SessionStatus.ERRORED: "✗",
}

STATUS_STYLE: dict[SessionStatus, str] = {
    SessionStatus.NEW: "dim",
    SessionStatus.PLANNING: "yellow",
    SessionStatus.AWAITING_PLAN_APPROVAL: "bold magenta",
    SessionStatus.EXECUTING: "green",
    SessionStatus.AWAITING_USER: "bold red",
    SessionStatus.DONE: "blue",
    SessionStatus.ERRORED: "bold red",
}


def _render_text(state: SessionState) -> Text:
    glyph = STATUS_GLYPH[state.status]
    style = STATUS_STYLE[state.status]
    prompt = state.initial_prompt.strip().splitlines()[0] if state.initial_prompt else "(no prompt)"
    prompt = prompt[:32] + ("…" if len(prompt) > 32 else "")
    repos = len(state.attached_repos)
    repo_chip = f" [{repos}r]" if repos else ""
    text = Text()
    text.append(glyph, style=style)
    text.append(f" {state.id[:6]}{repo_chip}  {prompt}")
    return text


class SessionRow(ListItem):
    """One row in the session list, identified by session id."""

    def __init__(self, state: SessionState) -> None:
        self._label = Label(_render_text(state))
        super().__init__(self._label, id=f"session-{state.id}")
        self.session_id = state.id
        self.session_state = state

    def update_state(self, state: SessionState) -> None:
        self.session_state = state
        self._label.update(_render_text(state))


class SessionListView(VerticalScroll):
    """Left pane: list of all sessions."""

    DEFAULT_CSS = """
    SessionListView {
        width: 36;
        border-right: solid $accent;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def action_cursor_down(self) -> None:
        self.list_view.action_cursor_down()

    def action_cursor_up(self) -> None:
        self.list_view.action_cursor_up()

    def compose(self) -> ComposeResult:
        yield ListView(id="session-list")

    @property
    def list_view(self) -> ListView:
        return self.query_one("#session-list", ListView)

    def add_session(self, state: SessionState) -> None:
        if self.query(f"#session-{state.id}"):
            return
        row = SessionRow(state)
        self.list_view.append(row)

    def update_session(self, state: SessionState) -> None:
        try:
            row = self.query_one(f"#session-{state.id}", SessionRow)
        except Exception:
            self.add_session(state)
            return
        row.update_state(state)

    def remove_session(self, session_id: str) -> None:
        rows = self.query(f"#session-{session_id}")
        for row in rows:
            row.remove()

    def selected_session_id(self) -> str | None:
        item = self.list_view.highlighted_child
        if isinstance(item, SessionRow):
            return item.session_id
        return None
