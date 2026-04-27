from __future__ import annotations

import contextlib
import json
from typing import Any

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.style import Style
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, RadioButton, RadioSet, Static


def _toggle_button_with_off_glyph(self: Checkbox | RadioButton, inner_off: str) -> Content:
    """Render the toggle button cell, swapping glyph by ``self.value``.

    Textual 8.x's default ``ToggleButton._button`` renders ``BUTTON_INNER``
    in both states and only flips its colour. We want a different character
    per state — ✓ when on, ``inner_off`` when off — so we recreate the
    assembly with a value-dependent inner.
    """
    button_style = self.get_visual_style("toggle--button")
    side_style = Style(
        foreground=button_style.background,
        background=self.background_colors[1],
    )
    inner = self.BUTTON_INNER if self.value else inner_off
    return Content.assemble(
        (self.BUTTON_LEFT, side_style),
        (inner, button_style),
        (self.BUTTON_RIGHT, side_style),
    )


class _CheckMarkBox(Checkbox):
    BUTTON_INNER = "✓"
    BUTTON_INNER_OFF = "X"

    @property
    def _button(self) -> Content:
        return _toggle_button_with_off_glyph(self, self.BUTTON_INNER_OFF)


class _CheckMarkRadio(RadioButton):
    BUTTON_INNER = "✓"
    BUTTON_INNER_OFF = "●"

    @property
    def _button(self) -> Content:
        return _toggle_button_with_off_glyph(self, self.BUTTON_INNER_OFF)


class QuestionModal(ModalScreen[str | None]):
    """Render an AskUserQuestion tool call and collect the user's answer."""

    DEFAULT_CSS = """
    QuestionModal {
        align: center middle;
    }
    QuestionModal > Vertical {
        width: 90%;
        max-width: 120;
        height: 80%;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    QuestionModal #question-title {
        height: 1;
        color: $accent;
    }
    QuestionModal VerticalScroll {
        height: 1fr;
        margin-top: 1;
        margin-bottom: 1;
    }
    QuestionModal .question-header {
        color: $accent;
        margin-top: 1;
    }
    QuestionModal .question-text {
        margin-bottom: 1;
    }
    QuestionModal #other-input {
        height: 3;
        margin-top: 1;
    }
    QuestionModal Input.free-input {
        height: 3;
        margin-bottom: 1;
    }
    QuestionModal Checkbox {
        height: 1;
        margin: 0;
        padding: 0;
        border: none;
        background: transparent;
    }
    QuestionModal Checkbox:focus {
        border: none;
        background: $boost;
    }
    QuestionModal RadioSet {
        border: none;
        padding: 0;
        margin: 0;
        background: transparent;
        height: auto;
        width: 1fr;
    }
    QuestionModal RadioSet:focus {
        background: $boost;
    }
    QuestionModal RadioButton {
        height: 1;
        margin: 0;
        padding: 0;
        border: none;
        background: transparent;
    }
    QuestionModal RadioButton:focus {
        background: $boost;
    }
    QuestionModal Horizontal {
        height: 3;
        align: right middle;
    }
    QuestionModal Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("f2", "submit", "Submit", priority=True),
    ]

    def __init__(self, session_id: str, question_input: dict[str, Any]) -> None:
        super().__init__()
        self.session_id = session_id
        self.question_input = question_input
        # Normalize to a list of dicts; tolerate malformed input by falling
        # back to a single synthetic question whose body is the raw JSON.
        raw_questions = question_input.get("questions")
        if isinstance(raw_questions, list) and raw_questions:
            self.questions: list[dict[str, Any]] = [
                q if isinstance(q, dict) else {"question": str(q)} for q in raw_questions
            ]
        else:
            self.questions = [
                {
                    "question": "(unrecognized AskUserQuestion shape — raw input below)",
                    "options": [],
                    "multiSelect": False,
                    "_raw": json.dumps(question_input, default=str, indent=2),
                }
            ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"[bold]Question from session {self.session_id[:8]}[/bold]",
                id="question-title",
            )
            scroll = VerticalScroll()
            scroll.can_focus = False
            with scroll:
                for idx, q in enumerate(self.questions):
                    header = str(q.get("header") or "").strip()
                    if header:
                        yield Static(f"[bold]{header}[/bold]", classes="question-header")
                    yield Static(
                        str(q.get("question") or "(no question text)"),
                        classes="question-text",
                    )
                    raw = q.get("_raw")
                    if raw:
                        yield Static(f"[dim]{raw}[/dim]")
                    options = q.get("options") or []
                    multi = bool(q.get("multiSelect"))
                    if not options:
                        yield Input(
                            placeholder="Type your answer…",
                            id=f"q{idx}-free",
                            classes="free-input",
                        )
                        continue
                    if multi:
                        for opt_idx, opt in enumerate(options):
                            label = _format_option(opt)
                            yield _CheckMarkBox(label, id=f"q{idx}-opt{opt_idx}")
                    else:
                        with RadioSet(id=f"q{idx}-radio"):
                            for opt_idx, opt in enumerate(options):
                                label = _format_option(opt)
                                yield _CheckMarkRadio(label, id=f"q{idx}-opt{opt_idx}")
            yield Input(
                placeholder="Other / free-text answer (optional)…",
                id="other-input",
            )
            with Horizontal():
                cancel_btn = Button("Cancel (Esc)", id="cancel", variant="error")
                cancel_btn.can_focus = False
                yield cancel_btn
                submit_btn = Button("Submit (F2)", id="submit", variant="success")
                submit_btn.can_focus = False
                yield submit_btn

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self._submit()
        elif event.button.id == "cancel":
            self.dismiss(None)

    @on(Input.Submitted, "#other-input")
    def _on_other_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    @on(RadioSet.Changed)
    def _on_radio_changed(self, event: RadioSet.Changed) -> None:
        """Selecting a single-choice option advances to the next question."""
        idx = self._question_index_of(event.radio_set)
        if idx is None:
            return
        self._advance_from(idx)

    @on(Input.Submitted, "Input.free-input")
    def _on_free_submitted(self, event: Input.Submitted) -> None:
        idx = self._question_index_of(event.input)
        if idx is None:
            self._submit()
            return
        self._advance_from(idx)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        self._submit()

    def on_mount(self) -> None:
        """Place initial focus on the first interactive widget."""
        for idx in range(len(self.questions)):
            target = self._question_target_id(idx)
            if target is None:
                continue
            try:
                self.query_one(target).focus()
                return
            except Exception:
                continue
        with contextlib.suppress(Exception):
            self.query_one("#other-input", Input).focus()

    def _question_target_id(self, idx: int) -> str | None:
        """CSS selector for the first focusable response widget of question ``idx``."""
        if idx < 0 or idx >= len(self.questions):
            return None
        q = self.questions[idx]
        options = q.get("options") or []
        if not options:
            return f"#q{idx}-free"
        multi = bool(q.get("multiSelect"))
        return f"#q{idx}-opt0" if multi else f"#q{idx}-radio"

    def _question_last_target_id(self, idx: int) -> str | None:
        """CSS selector for the *last* focusable response widget of question ``idx``."""
        if idx < 0 or idx >= len(self.questions):
            return None
        q = self.questions[idx]
        options = q.get("options") or []
        if not options:
            return f"#q{idx}-free"
        if bool(q.get("multiSelect")):
            return f"#q{idx}-opt{len(options) - 1}"
        return f"#q{idx}-radio"

    def _question_index_of(self, widget: Widget | None) -> int | None:
        """Walk up from ``widget`` to find the question index encoded in its id."""
        node: Widget | None = widget
        while node is not None:
            wid = node.id or ""
            if wid.startswith("q") and "-" in wid:
                with contextlib.suppress(ValueError):
                    return int(wid[1 : wid.index("-")])
            node = getattr(node, "parent", None)
        return None

    def _advance_from(self, idx: int) -> None:
        """Focus the next question's response widget, or fall back to ``#other-input``."""
        for next_idx in range(idx + 1, len(self.questions)):
            target = self._question_target_id(next_idx)
            if target is None:
                continue
            try:
                self.query_one(target).focus()
                return
            except Exception:
                continue
        with contextlib.suppress(Exception):
            self.query_one("#other-input", Input).focus()

    def _retreat_to(self, idx: int) -> None:
        """Focus the previous question's last response widget, if any."""
        for prev_idx in range(idx - 1, -1, -1):
            target = self._question_last_target_id(prev_idx)
            if target is None:
                continue
            try:
                self.query_one(target).focus()
                return
            except Exception:
                continue

    def on_key(self, event: events.Key) -> None:
        """Keyboard nav inside multi-select Checkbox groups.

        Up/Down moves between checkboxes in the same question; overflowing
        past either end jumps to the adjacent question's response widget.
        Enter is left to Textual (toggles the focused checkbox / selects the
        focused radio button); free-text inputs route advance/submit through
        ``Input.Submitted``.
        """
        focused = self.focused
        if focused is None or isinstance(focused, Input):
            return

        if event.key in ("up", "down") and isinstance(focused, Checkbox):
            wid = focused.id or ""
            if "-opt" not in wid:
                return
            try:
                q_idx = int(wid[1 : wid.index("-")])
                opt_idx = int(wid.split("-opt", 1)[1])
            except ValueError:
                return
            q = self.questions[q_idx]
            count = len(q.get("options") or [])
            event.stop()
            event.prevent_default()
            if event.key == "down":
                if opt_idx + 1 < count:
                    with contextlib.suppress(Exception):
                        self.query_one(f"#q{q_idx}-opt{opt_idx + 1}", Checkbox).focus()
                else:
                    self._advance_from(q_idx)
            else:
                if opt_idx - 1 >= 0:
                    with contextlib.suppress(Exception):
                        self.query_one(f"#q{q_idx}-opt{opt_idx - 1}", Checkbox).focus()
                else:
                    self._retreat_to(q_idx)

    def _submit(self) -> None:
        parts: list[str] = []
        for idx, q in enumerate(self.questions):
            options = q.get("options") or []
            label = q.get("header") or q.get("question") or f"Q{idx + 1}"
            short = str(label).strip().splitlines()[0][:60]
            if not options:
                try:
                    free = self.query_one(f"#q{idx}-free", Input).value.strip()
                except Exception:
                    free = ""
                if free:
                    parts.append(f"{short}: {free}")
                continue
            multi = bool(q.get("multiSelect"))
            chosen_labels: list[str] = []
            if multi:
                for opt_idx, opt in enumerate(options):
                    try:
                        cb = self.query_one(f"#q{idx}-opt{opt_idx}", Checkbox)
                    except Exception:
                        continue
                    if cb.value:
                        chosen_labels.append(_option_label(opt))
            else:
                try:
                    rs = self.query_one(f"#q{idx}-radio", RadioSet)
                except Exception:
                    rs = None
                if rs is not None and rs.pressed_index >= 0:
                    chosen = options[rs.pressed_index]
                    chosen_labels.append(_option_label(chosen))
            answer = ", ".join(chosen_labels) if chosen_labels else "(no selection)"
            parts.append(f"{short}: {answer}")

        try:
            other = self.query_one("#other-input", Input).value.strip()
        except Exception:
            other = ""
        summary = " | ".join(parts) if parts else ""
        if other:
            summary = f"{summary}; other: {other}" if summary else other
        if not summary:
            summary = "(no selection)"
        self.dismiss(summary)


def _option_label(opt: Any) -> str:
    if isinstance(opt, dict):
        return str(opt.get("label") or opt.get("value") or opt)
    return str(opt)


def _format_option(opt: Any) -> str:
    if isinstance(opt, dict):
        label = str(opt.get("label") or opt.get("value") or opt)
        desc = str(opt.get("description") or "").strip()
        if desc:
            return f"{label} [dim]— {desc}[/dim]"
        return label
    return str(opt)
