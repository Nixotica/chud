"""Modal that lets the user review (and edit) a draft PR before it's opened.

Surfaced after a session reaches DONE when both
``OPT_MAKE_DRAFT_PR`` is enabled and a ``PR_REVIEW_REQUESTED`` event arrives.
The modal returns a ``PRReviewResult`` describing whether the user accepted
the PR (and the possibly-edited title/body) or rejected it.

If both PR-review and self-cleanup options are on, this modal always runs
*before* the cleanup confirmation — see ``SessionManager.submit_pr_review``
for the ordering guarantee.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, TextArea


@dataclass
class PRReviewResult:
    accepted: bool
    title: str
    body: str


class PRReviewModal(ModalScreen[PRReviewResult | None]):
    """Show the proposed draft-PR title/body and gate publishing on approval.

    Returns:
        - ``PRReviewResult(accepted=True, title=..., body=...)`` on accept.
        - ``PRReviewResult(accepted=False, title=..., body=...)`` on reject.
        - ``None`` only if the modal is dismissed without a button (treated by
          callers as a reject so the post-PR cleanup prompt can still progress).
    """

    DEFAULT_CSS = """
    PRReviewModal {
        align: center middle;
    }
    PRReviewModal > Vertical {
        width: 90%;
        max-width: 120;
        height: 80%;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    PRReviewModal #pr-title-static {
        height: 1;
        color: $accent;
    }
    PRReviewModal Label {
        height: 1;
        color: $text-muted;
        margin-top: 1;
    }
    PRReviewModal #repos {
        height: auto;
        max-height: 5;
        margin-bottom: 1;
        color: $text-muted;
    }
    PRReviewModal #title-input {
        margin-bottom: 1;
    }
    PRReviewModal TextArea {
        height: 1fr;
        min-height: 6;
        margin-bottom: 1;
    }
    PRReviewModal Horizontal#buttons {
        height: 3;
        align: right middle;
    }
    PRReviewModal Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        ("escape", "reject", "Reject"),
    ]

    def __init__(
        self,
        session_id: str,
        title: str,
        body: str,
        repos: Iterable[str] = (),
    ) -> None:
        super().__init__()
        self.session_id = session_id
        self._initial_title = title
        self._initial_body = body
        self._repos = list(repos)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"[bold]Open draft PR(s) for session {self.session_id[:8]}?[/bold]",
                id="pr-title-static",
            )
            with VerticalScroll(id="repos"):
                if self._repos:
                    for label in self._repos:
                        yield Static(f"  • [yellow]{label}[/yellow]")
                else:
                    yield Static("  [dim](no attached repos)[/dim]")
            yield Label("Title")
            yield Input(value=self._initial_title, id="title-input")
            yield Label("Body")
            yield TextArea(self._initial_body, id="body-input")
            with Horizontal(id="buttons"):
                # Same trick as PlanApprovalModal: keep buttons mouse-clickable
                # but never let them grab keyboard focus, so editing the
                # Input/TextArea above stays unobstructed.
                reject_btn = Button("Reject (skip PR)", id="reject", variant="error")
                reject_btn.can_focus = False
                yield reject_btn
                accept_btn = Button("Accept (open PR)", id="accept", variant="success")
                accept_btn.can_focus = False
                yield accept_btn

    def on_mount(self) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#title-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "accept":
            self._dismiss(accepted=True)
        elif event.button.id == "reject":
            self._dismiss(accepted=False)

    def action_reject(self) -> None:
        self._dismiss(accepted=False)

    def _dismiss(self, *, accepted: bool) -> None:
        try:
            title = self.query_one("#title-input", Input).value
        except Exception:
            title = self._initial_title
        try:
            body = self.query_one("#body-input", TextArea).text
        except Exception:
            body = self._initial_body
        self.dismiss(PRReviewResult(accepted=accepted, title=title, body=body))
