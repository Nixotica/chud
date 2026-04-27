"""Registry of session-creation options (toggleable behaviors).

Adding a new option is a one-line append to ``SESSION_OPTIONS``; the new-session
modal renders all entries automatically and ``SessionState.options`` will gain
the key on the next persistence round-trip.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Mirror of the inline ``Literal`` on ``ClaudeAgentOptions.effort`` in
# claude-agent-sdk's types.py — the SDK does not export a named alias or a
# tuple of valid values, so we keep our own source of truth here.
EFFORT_VALUES: tuple[str | None, ...] = (None, "low", "medium", "high", "max")
DEFAULT_EFFORT: str | None = None


def normalize_effort(raw: object) -> str | None:
    """Coerce a persisted/raw effort value to a known choice (or ``None``).

    Unknown / legacy / wrong-type values silently fall back to the default so
    that a corrupted ``sessions.json`` or stale config can't crash the app.
    """
    if raw in EFFORT_VALUES:
        return raw  # type: ignore[return-value]
    return DEFAULT_EFFORT


@dataclass(frozen=True)
class SessionOption:
    id: str
    label: str
    description: str
    default: bool


# Option IDs are persisted in sessions.json — renaming is a breaking change.
OPT_MAKE_DRAFT_PR = "make_draft_pr"
OPT_SELF_CLEANUP = "self_cleanup"


SESSION_OPTIONS: tuple[SessionOption, ...] = (
    SessionOption(
        id=OPT_MAKE_DRAFT_PR,
        label="Open draft PR(s) on completion",
        description=(
            "When the session finishes, push each worktree branch and open a "
            "draft pull request via the gh CLI."
        ),
        default=False,
    ),
    SessionOption(
        id=OPT_SELF_CLEANUP,
        label="Self-cleanup on completion",
        description=(
            "When the session finishes, prompt to remove it from the sidebar "
            "and delete its workspace and worktrees."
        ),
        default=False,
    ),
)


def default_options() -> dict[str, bool]:
    """Return a fresh dict mapping every registered option to its default."""
    return {opt.id: opt.default for opt in SESSION_OPTIONS}


def normalize_options(raw: dict[str, Any] | None) -> dict[str, bool]:
    """Merge ``raw`` over the registry defaults.

    Unknown keys in ``raw`` are dropped so that removing an option from the
    registry doesn't carry stale flags forward. Missing keys take the default
    so newly-added options apply to old persisted sessions. The input type is
    ``Any`` because the user config dict mixes option booleans with the
    ``effort`` string; this function coerces to ``bool`` and ignores the rest.
    """
    base = default_options()
    if raw:
        for opt in SESSION_OPTIONS:
            if opt.id in raw:
                base[opt.id] = bool(raw[opt.id])
    return base
