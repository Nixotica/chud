"""Shared toggle widgets that swap their inner glyph by ``value``.

Textual 8.x's default ``ToggleButton._button`` renders ``BUTTON_INNER`` in both
the on and off states and only flips its colour. In TUIs that's nearly
indistinguishable, so we recreate the assembly with a value-dependent inner
character — ✓ when on, a class-defined ``BUTTON_INNER_OFF`` (X for checkboxes,
● for radio buttons) when off.

Both ``QuestionModal`` (AskUserQuestion answers) and ``NewSessionModal``
(session creation flags) consume these so the on/off glyph treatment is
consistent across modals and lives in exactly one place.
"""

from __future__ import annotations

from textual.content import Content
from textual.style import Style
from textual.widgets import Checkbox, RadioButton


def toggle_button_with_off_glyph(self: Checkbox | RadioButton) -> Content:
    """Render the toggle button cell, swapping glyph by ``self.value``.

    Textual 8.x's default ``ToggleButton._button`` renders ``BUTTON_INNER``
    in both states and only flips its colour. We want a different character
    per state — ✓ when on, ``BUTTON_INNER_OFF`` when off — so we recreate
    the assembly with a value-dependent inner.
    """
    button_style = self.get_visual_style("toggle--button")
    side_style = Style(
        foreground=button_style.background,
        background=self.background_colors[1],
    )
    inner = self.BUTTON_INNER if self.value else self.BUTTON_INNER_OFF
    return Content.assemble(
        (self.BUTTON_LEFT, side_style),
        (inner, button_style),
        (self.BUTTON_RIGHT, side_style),
    )


class CheckMarkBox(Checkbox):
    """Checkbox that renders ✓ when checked and X when unchecked."""

    BUTTON_INNER = "✓"
    BUTTON_INNER_OFF = "X"

    @property
    def _button(self) -> Content:
        return toggle_button_with_off_glyph(self)


class CheckMarkRadio(RadioButton):
    """RadioButton that renders ✓ when selected and ● when unselected."""

    BUTTON_INNER = "✓"
    BUTTON_INNER_OFF = "●"

    @property
    def _button(self) -> Content:
        return toggle_button_with_off_glyph(self)
