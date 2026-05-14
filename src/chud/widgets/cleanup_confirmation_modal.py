from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static

from chud.markup import TextDanger, TextHeading, TextMuted, TextPath, TextSuccess
from chud.types import SessionState
from chud.widgets._scrollable_modal import ScrollableModalScreen


class CleanupConfirmationModal(ScrollableModalScreen[bool]):
    """Confirm cleanup of a finished session.

    Two visual modes, selected by ``published_prs``:

    - **Empty** (no PRs published — session opted out, user rejected the PR
      review, or every gh push failed): destructive red copy warning that
      unpushed commits will be lost.
    - **Non-empty**: the work is safely on the remote and linked from at
      least one draft PR; render a calmer success-flavored confirmation
      that lists the PR URL(s).

    Returns True on confirm, False on cancel/escape. The caller is responsible
    for actually invoking ``manager.kill_session(..., cleanup_worktrees=True)``
    and removing the row from the sidebar.
    """

    DEFAULT_CSS = """
    CleanupConfirmationModal {
        align: center middle;
    }
    CleanupConfirmationModal > Vertical {
        width: 90%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: round $accent;
        padding: 1 2;
    }
    CleanupConfirmationModal #title {
        height: 1;
        color: $accent;
    }
    CleanupConfirmationModal VerticalScroll {
        height: auto;
        max-height: 14;
        margin-top: 1;
        margin-bottom: 1;
    }
    CleanupConfirmationModal Horizontal {
        height: 3;
        align: right middle;
    }
    CleanupConfirmationModal Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("y", "confirm", "Confirm"),
        ("n", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        state: SessionState,
        published_prs: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self.state = state
        self.published_prs = list(published_prs or [])

    def scroll_container(self) -> VerticalScroll | None:
        try:
            return self.query_one(VerticalScroll)
        except Exception:
            return None

    def compose(self) -> ComposeResult:
        with Vertical():
            if self.published_prs:
                yield from self._compose_published()
            else:
                yield from self._compose_unpublished()
            with Horizontal():
                yield Button("Cancel (n)", id="cancel")
                if self.published_prs:
                    yield Button("Confirm (y)", id="confirm", variant="success")
                else:
                    yield Button("Confirm (y)", id="confirm", variant="error")

    def _compose_published(self) -> ComposeResult:
        yield Static(
            TextHeading(f"Clean up session {self.state.id[:8]}? Work is pushed."),
            id="title",
        )
        with VerticalScroll():
            yield Static(TextSuccess("Draft PRs opened:"))
            for pr in self.published_prs:
                repo = pr.get("repo") or "(repo)"
                url = pr.get("url") or ""
                yield Static(f"  • [cyan]{repo}[/cyan] → [green]{url}[/green]")
            yield Static("\nRemoving the worktrees:")
            if self.state.attached_repos:
                for wt in self.state.attached_repos.values():
                    yield Static(
                        f"  • worktree:  {TextPath(str(wt.worktree_path))}  "
                        f"{TextMuted(f'(branch {wt.branch})')}"
                    )
            else:
                yield Static(f"  {TextMuted('(no attached repos)')}")

    def _compose_unpublished(self) -> ComposeResult:
        yield Static(
            TextHeading(f"Clean up session {self.state.id[:8]}?"),
            id="title",
        )
        with VerticalScroll():
            yield Static(f"This will {TextDanger('permanently delete')} every attached worktree:")
            if self.state.attached_repos:
                for wt in self.state.attached_repos.values():
                    yield Static(
                        f"  • worktree:  {TextPath(str(wt.worktree_path))}  "
                        f"{TextMuted(f'(branch {wt.branch})')}"
                    )
            else:
                yield Static(f"  {TextMuted('(no attached repos)')}")
            yield Static(
                "\nUnpushed commits on the chud branch will be lost unless "
                "you also enabled the draft-PR option."
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.dismiss(True)
        elif event.button.id == "cancel":
            self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
