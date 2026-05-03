"""Vim-key navigation in the New Session modal's GitHub-issue picker.

Covers the regression where ``j``/``k`` were eaten by Textual's
``SelectOverlay`` type-to-search instead of moving the option highlight,
and the closed-Select case where ``j``/``k`` used to scroll the modal
body underneath the focused widget.
"""

from __future__ import annotations

from chud.app import ChudApp
from chud.gh import Issue
from chud.widgets._vim_select import VimSelect, VimSelectOverlay
from chud.widgets.new_session_modal import NewSessionModal


def _issue(number: int, title: str) -> Issue:
    return Issue(
        number=number,
        title=title,
        url=f"https://github.com/x/y/issues/{number}",
        body="body",
        state="OPEN",
    )


async def test_jk_navigates_open_issue_overlay():
    issues = [_issue(1, "alpha"), _issue(2, "beta"), _issue(3, "gamma")]
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(NewSessionModal(issues=issues))
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, NewSessionModal)

        select = modal.query_one("#issue", VimSelect)
        select.focus()
        await pilot.pause()
        # Open the overlay; first option is highlighted by show_overlay.
        await pilot.press("enter")
        await pilot.pause()

        overlay = modal.query_one(VimSelectOverlay)
        assert overlay.highlighted == 0

        await pilot.press("j")
        await pilot.pause()
        assert overlay.highlighted == 1

        await pilot.press("j")
        await pilot.pause()
        assert overlay.highlighted == 2

        await pilot.press("k")
        await pilot.pause()
        assert overlay.highlighted == 1


async def test_type_to_search_is_disabled():
    """A non-jk printable letter must not move the highlight — type-to-search
    is intentionally turned off for chud's selects."""
    issues = [_issue(1, "alpha"), _issue(2, "beta"), _issue(3, "gamma")]
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(NewSessionModal(issues=issues))
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, NewSessionModal)

        select = modal.query_one("#issue", VimSelect)
        select.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        overlay = modal.query_one(VimSelectOverlay)
        assert overlay.highlighted == 0
        # Pressing a letter that uniquely prefixes option 3 ("gamma") would
        # have jumped to index 2 with type-to-search enabled. We disabled it,
        # so the highlight should stay put.
        await pilot.press("g")
        await pilot.pause()
        assert overlay.highlighted == 0


async def test_jk_on_closed_select_opens_overlay():
    """With the (closed) Select focused, ``j``/``k`` should open the overlay
    (matching how ``down``/``up`` already behave) — not scroll the modal body."""
    issues = [_issue(1, "alpha"), _issue(2, "beta")]
    app = ChudApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(NewSessionModal(issues=issues))
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, NewSessionModal)

        select = modal.query_one("#issue", VimSelect)
        select.focus()
        await pilot.pause()
        assert not select.expanded

        await pilot.press("j")
        await pilot.pause()
        assert select.expanded
