from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea

from chud.options import SESSION_OPTIONS, default_options


@dataclass
class NewSessionResult:
    repo_path: Path | None
    prompt: str
    options: dict[str, bool] = field(default_factory=default_options)


def _detect_cwd_repo() -> str:
    """If CWD is inside a git repo, return its toplevel path; else empty string."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=Path.cwd(),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


class NewSessionModal(ModalScreen[NewSessionResult | None]):
    """Prompt user for an optional repo path + an initial prompt for a new session."""

    DEFAULT_CSS = """
    NewSessionModal {
        align: center middle;
    }
    NewSessionModal > Vertical {
        width: 90%;
        max-width: 100;
        height: 32;
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
        height: 8;
        margin-bottom: 1;
    }
    NewSessionModal #options-group {
        height: auto;
        max-height: 8;
        margin-bottom: 1;
        border: round $primary-background;
        padding: 0 1;
    }
    NewSessionModal #options-group Label {
        color: $accent;
    }
    NewSessionModal #options-group Checkbox {
        height: 1;
        margin: 0;
        padding: 0;
        border: none;
        background: transparent;
    }
    NewSessionModal #options-group Checkbox:focus {
        border: none;
        background: $boost;
    }
    NewSessionModal Horizontal#buttons {
        height: 3;
        align: right middle;
    }
    NewSessionModal Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("f2", "start", "Start", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[bold]New session[/bold]")
            yield Label("Repo path (optional, leave blank for none):")
            yield Input(value=_detect_cwd_repo(), placeholder="/path/to/repo", id="repo")
            yield Label("Initial prompt:")
            yield TextArea("", id="prompt")
            with VerticalScroll(id="options-group"):
                yield Label("Options")
                for opt in SESSION_OPTIONS:
                    yield Checkbox(
                        opt.label,
                        value=opt.default,
                        id=f"opt-{opt.id}",
                        tooltip=opt.description,
                    )
            with Horizontal(id="buttons"):
                yield Button("Cancel (Esc)", id="cancel")
                yield Button("Start (F2)", id="start", variant="success")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "start":
            self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_start(self) -> None:
        self._submit()

    def on_mount(self) -> None:
        self.query_one("#repo", Input).focus()

    def _submit(self) -> None:
        repo_str = self.query_one("#repo", Input).value.strip()
        prompt = self.query_one("#prompt", TextArea).text.strip()
        if not prompt:
            return
        repo_path = Path(repo_str).expanduser() if repo_str else None
        options = {
            opt.id: self.query_one(f"#opt-{opt.id}", Checkbox).value
            for opt in SESSION_OPTIONS
        }
        self.dismiss(
            NewSessionResult(repo_path=repo_path, prompt=prompt, options=options)
        )
