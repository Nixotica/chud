"""Modal for editing chud's global, persistent settings.

Opened from ``ChudApp`` with the ``s`` key binding. Two concerns live here:

- **Defaults for new sessions** — checkboxes for every entry in
  ``options.SESSION_OPTIONS`` (currently ``make_draft_pr`` + ``self_cleanup``).
  Saving here updates the values that pre-fill the New Session modal.
- **Global preferences** — branch prefix, whether to embed the prompt slug in
  the branch name, and the PR body footer template. These keys live only in
  the settings modal (not duplicated in the New Session modal).

Persistence is delegated to ``settings.save_settings`` and
``state.save_user_config`` — both of which rewrite ``~/.local/share/chud/
config.json`` atomically.
"""

from __future__ import annotations

import contextlib

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea

from chud.options import SESSION_OPTIONS
from chud.settings import (
    KEY_BRANCH_PREFIX,
    KEY_INCLUDE_SLUG,
    KEY_PR_BODY_FOOTER,
    load_settings,
    sanitize_branch_prefix,
    save_settings,
)
from chud.state import load_user_config, save_user_config, user_default_options
from chud.widgets.check_mark_toggles import CheckMarkBox


class SettingsModal(ModalScreen[bool]):
    """Edit persistent chud preferences.

    Returns ``True`` if the user saved, ``False`` if cancelled / dismissed.
    """

    DEFAULT_CSS = """
    SettingsModal {
        align: center middle;
    }
    SettingsModal > Vertical {
        width: 90%;
        max-width: 110;
        height: 90%;
        max-height: 40;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    SettingsModal #title {
        height: 1;
        color: $accent;
    }
    SettingsModal Label.section {
        height: 1;
        color: $accent;
        margin-top: 1;
    }
    SettingsModal Label.field {
        height: 1;
        color: $text-muted;
    }
    SettingsModal Static.hint {
        height: auto;
        color: $text-muted;
    }
    SettingsModal #scroll {
        height: 1fr;
        padding: 0 1;
    }
    SettingsModal Checkbox {
        height: 1;
        margin: 0;
        padding: 0;
        border: none;
        background: transparent;
    }
    SettingsModal Checkbox:focus {
        border: none;
        background: $boost;
    }
    SettingsModal Input, SettingsModal TextArea {
        margin-bottom: 1;
    }
    SettingsModal #branch-preview {
        margin-bottom: 1;
    }
    SettingsModal #footer-template {
        height: 5;
    }
    SettingsModal Horizontal#buttons {
        height: 3;
        align: right middle;
    }
    SettingsModal Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("f2", "save", "Save", priority=True),
    ]

    def compose(self) -> ComposeResult:
        snapshot = load_settings()
        opt_defaults = user_default_options()
        with Vertical():
            yield Static("[bold]chud settings[/bold]", id="title")
            with VerticalScroll(id="scroll"):
                yield Label("Defaults for new sessions", classes="section")
                for opt in SESSION_OPTIONS:
                    yield CheckMarkBox(
                        opt.label,
                        value=opt_defaults[opt.id],
                        id=f"opt-{opt.id}",
                        tooltip=opt.description,
                    )

                yield Label("Branch format", classes="section")
                yield Label("Branch prefix (e.g. `chud/`, `agent/`):", classes="field")
                yield Input(
                    value=snapshot[KEY_BRANCH_PREFIX],
                    placeholder="chud/",
                    id="branch-prefix",
                )
                yield CheckMarkBox(
                    "Include prompt slug in branch name",
                    value=snapshot[KEY_INCLUDE_SLUG],
                    id="include-slug",
                    tooltip=(
                        "When on: branches are <prefix><slug>-<id>. "
                        "When off: branches are <prefix><id>."
                    ),
                )
                yield Static("", id="branch-preview", classes="hint")

                yield Label("PR body footer", classes="section")
                yield Label(
                    "Appended to the PR body. Placeholder: {session_id}.",
                    classes="field",
                )
                yield TextArea(snapshot[KEY_PR_BODY_FOOTER], id="footer-template")

            with Horizontal(id="buttons"):
                cancel_btn = Button("Cancel (Esc)", id="cancel")
                cancel_btn.can_focus = False
                yield cancel_btn
                save_btn = Button("Save (F2)", id="save", variant="success")
                save_btn.can_focus = False
                yield save_btn

    def on_mount(self) -> None:
        self._refresh_branch_preview()
        with contextlib.suppress(Exception):
            self.query_one("#branch-prefix", Input).focus()

    # The branch preview is recomputed on every keystroke / toggle so the user
    # sees the result of their config without having to save and re-open the
    # modal. Both handlers route to the same helper.
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "branch-prefix":
            self._refresh_branch_preview()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "include-slug":
            self._refresh_branch_preview()

    def _refresh_branch_preview(self) -> None:
        try:
            prefix_widget = self.query_one("#branch-prefix", Input)
            include_slug_widget = self.query_one("#include-slug", Checkbox)
            preview_widget = self.query_one("#branch-preview", Static)
        except Exception:
            return
        prefix = sanitize_branch_prefix(prefix_widget.value)
        example = f"{prefix}fix-auth-bug-3f9a2c" if include_slug_widget.value else f"{prefix}3f9a2c"
        preview_widget.update(f"[dim]Preview: {example}[/dim]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(False)
        elif event.button.id == "save":
            self._save()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_save(self) -> None:
        self._save()

    def _save(self) -> None:
        # Per-session option defaults: write through save_user_config so they
        # share the single config.json with the keys settings.save_settings
        # owns. Then save_settings handles its own merge for the global keys.
        cfg = load_user_config()
        for opt in SESSION_OPTIONS:
            try:
                cfg[opt.id] = self.query_one(f"#opt-{opt.id}", Checkbox).value
            except Exception:
                continue
        save_user_config(cfg)

        try:
            prefix = sanitize_branch_prefix(self.query_one("#branch-prefix", Input).value)
        except Exception:
            prefix = ""
        try:
            include_slug = self.query_one("#include-slug", Checkbox).value
        except Exception:
            include_slug = True
        try:
            footer = self.query_one("#footer-template", TextArea).text
        except Exception:
            footer = ""
        save_settings(
            {
                KEY_BRANCH_PREFIX: prefix,
                KEY_INCLUDE_SLUG: bool(include_slug),
                KEY_PR_BODY_FOOTER: footer,
            }
        )

        self.app.notify("Settings saved.")
        self.dismiss(True)
