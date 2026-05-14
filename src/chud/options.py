"""Registry of session-creation options (toggleable behaviors).

Adding a new option is a one-line append to ``SESSION_OPTIONS``; the new-session
modal renders all entries automatically and ``SessionState.options`` will gain
the key on the next persistence round-trip.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args

# Single source of truth for effort strings. Mirrors the inline ``Literal`` on
# ``ClaudeAgentOptions.effort`` in claude-agent-sdk's types.py — the SDK does
# not export a named alias.
EffortLevel = Literal["low", "medium", "high", "max"]
EFFORT_VALUES: tuple[EffortLevel, ...] = get_args(EffortLevel)


def normalize_effort(raw: object) -> EffortLevel | None:
    """Coerce a persisted/raw effort value to a known choice (or ``None``).

    Unknown / legacy / wrong-type values silently fall back to ``None`` so
    that a corrupted ``sessions.json`` or stale config can't crash the app.
    """
    if raw in EFFORT_VALUES:
        return raw  # type: ignore[return-value]
    return None


# Permission-mode policy for a chud session.
#
# - ``full_auto``: current behavior — after plan approval the SDK runs in
#   ``acceptEdits`` mode and only plan/AskUserQuestion interrupt.
# - ``default``: SDK runs in ``default`` mode after plan approval — the
#   user's ``~/.claude/settings.json`` allow/deny rules apply, and chud
#   surfaces a modal for any tool the SDK still asks about.
# - ``low_perms``: SDK runs in ``acceptEdits`` after plan approval, but
#   chud's PreToolUse hook unconditionally surfaces a modal for every
#   Edit/Write/NotebookEdit/Bash call (read-only tools still pass).
RunMode = Literal["full_auto", "default", "low_perms"]
RUN_MODE_VALUES: tuple[RunMode, ...] = get_args(RunMode)
RUN_MODE_DEFAULT: RunMode = "full_auto"

# Tool names that low-perms requires explicit user approval for. Read-only
# tools are deliberately omitted so the agent can still explore the worktree.
LOW_PERMS_GATED_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "NotebookEdit", "Bash"})

# (label, value) pairs for the run-mode Select in the new-session + settings
# modals. Kept in this module so the two widgets stay in sync.
RUN_MODE_CHOICES: tuple[tuple[str, RunMode], ...] = (
    ("Full auto", "full_auto"),
    ("Default (uses ~/.claude settings)", "default"),
    ("Low-perms (prompt for each edit)", "low_perms"),
)


def normalize_run_mode(raw: object) -> RunMode:
    """Coerce a persisted/raw run_mode value to a known choice.

    Unknown / legacy / wrong-type values fall back to ``RUN_MODE_DEFAULT``
    so a corrupted config can't crash the app and so the runtime branch
    in ``session.py`` stays a simple ``==`` comparison.
    """
    if raw in RUN_MODE_VALUES:
        return raw  # type: ignore[return-value]
    return RUN_MODE_DEFAULT


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
