from __future__ import annotations

import json
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, RadioButton, RadioSet, Static

from chud.markup import TextHeading, TextMuted


class QuestionModal(ModalScreen[str | None]):
    """Render an AskUserQuestion tool call and collect the user's answer.
    """

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
    QuestionModal Horizontal {
        height: 3;
        align: right middle;
    }
    QuestionModal Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
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
                TextHeading(f"Question from session {self.session_id[:8]}"),
                id="question-title",
            )
            with VerticalScroll():
                for idx, q in enumerate(self.questions):
                    header = str(q.get("header") or "").strip()
                    if header:
                        yield Static(TextHeading(header), classes="question-header")
                    yield Static(
                        str(q.get("question") or "(no question text)"),
                        classes="question-text",
                    )
                    raw = q.get("_raw")
                    if raw:
                        yield Static(TextMuted(str(raw)))
                    options = q.get("options") or []
                    multi = bool(q.get("multiSelect"))
                    if not options:
                        continue
                    if multi:
                        for opt_idx, opt in enumerate(options):
                            label = _format_option(opt)
                            yield Checkbox(label, id=f"q{idx}-opt{opt_idx}")
                    else:
                        with RadioSet(id=f"q{idx}-radio"):
                            for opt_idx, opt in enumerate(options):
                                label = _format_option(opt)
                                yield RadioButton(label, id=f"q{idx}-opt{opt_idx}")
            yield Input(
                placeholder="Other / free-text answer (optional)…",
                id="other-input",
            )
            with Horizontal():
                cancel_btn = Button("Cancel (Esc)", id="cancel", variant="error")
                cancel_btn.can_focus = False
                yield cancel_btn
                submit_btn = Button("Submit", id="submit", variant="success")
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

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        parts: list[str] = []
        for idx, q in enumerate(self.questions):
            options = q.get("options") or []
            if not options:
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
            label = q.get("header") or q.get("question") or f"Q{idx + 1}"
            short = str(label).strip().splitlines()[0][:60]
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
            return f"{label} {TextMuted(f'— {desc}')}"
        return label
    return str(opt)
