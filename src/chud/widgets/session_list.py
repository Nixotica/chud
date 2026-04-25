from __future__ import annotations

from textual.app import ComposeResult
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


class SessionRow(ListItem):
    """One row in the session list, identified by session id."""

    def __init__(self, state: SessionState) -> None:
        super().__init__(id=f"session-{state.id}")
        self.session_id = state.id
        self.session_state = state

    def compose(self) -> ComposeResult:
        yield Label(self._render())

    def update_state(self, state: SessionState) -> None:
        self.session_state = state
        label = self.query_one(Label)
        label.update(self._render())

    def _render(self) -> str:
        glyph = STATUS_GLYPH[self.session_state.status]
        style = STATUS_STYLE[self.session_state.status]
        if self.session_state.initial_prompt:
            prompt = self.session_state.initial_prompt.strip().splitlines()[0]
        else:
            prompt = "(no prompt)"
        prompt = prompt[:32] + ("…" if len(prompt) > 32 else "")
        repos = len(self.session_state.attached_repos)
        repo_chip = f" [{repos}r]" if repos else ""
        return f"[{style}]{glyph}[/{style}] {self.session_state.id[:6]}{repo_chip}  {prompt}"


class SessionListView(VerticalScroll):
    """Left pane: list of all sessions."""

    DEFAULT_CSS = """
    SessionListView {
        width: 36;
        border-right: solid $accent;
        padding: 0 1;
    }
    """

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
        rows = self.query(f"#session-{state.id}")
        if not rows:
            self.add_session(state)
            return
        rows.first(SessionRow).update_state(state)

    def remove_session(self, session_id: str) -> None:
        rows = self.query(f"#session-{session_id}")
        for row in rows:
            row.remove()

    def selected_session_id(self) -> str | None:
        item = self.list_view.highlighted_child
        if isinstance(item, SessionRow):
            return item.session_id
        return None
