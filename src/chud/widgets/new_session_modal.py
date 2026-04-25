from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, TextArea


@dataclass
class NewSessionResult:
    repo_path: Path | None
    prompt: str


class NewSessionModal(ModalScreen[NewSessionResult | None]):
    """Prompt user for an optional repo path + an initial prompt for a new session."""

    DEFAULT_CSS = """
    NewSessionModal {
        align: center middle;
    }
    NewSessionModal > Vertical {
        width: 90%;
        max-width: 100;
        height: 24;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    NewSessionModal Label {
        height: 1;
        color: $text-muted;
    }
    NewSessionModal Input {
        margin-bottom: 1;
    }
    NewSessionModal TextArea {
        height: 10;
        margin-bottom: 1;
    }
    NewSessionModal Horizontal {
        height: 3;
        align: right middle;
    }
    NewSessionModal Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[bold]New session[/bold]")
            yield Label("Repo path (optional, leave blank for none):")
            yield Input(placeholder="/path/to/repo", id="repo")
            yield Label("Initial prompt:")
            yield TextArea("", id="prompt")
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Start (Ctrl+Enter)", id="start", variant="success")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "start":
            self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_mount(self) -> None:
        self.query_one("#repo", Input).focus()

    def _submit(self) -> None:
        repo_str = self.query_one("#repo", Input).value.strip()
        prompt = self.query_one("#prompt", TextArea).text.strip()
        if not prompt:
            return
        repo_path = Path(repo_str).expanduser() if repo_str else None
        self.dismiss(NewSessionResult(repo_path=repo_path, prompt=prompt))
