"""Pre-release dev affordances for chud QA.

`chud --dev <scenario> [--seed PATH]` boots the real TUI and immediately
runs a scripted UI flow on top of it — without driving a real agent.
Today's only scenario, ``question``, pushes ``QuestionModal`` seeded from a
JSON file. The registry shape (``SCENARIOS``) is the seam future scenarios
plug into (mock agents, plan-modal preview, scripted event sequences) so
``app.py`` doesn't grow per-scenario branches.

This module must not ship in a released build.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from chud.app import ChudApp

log = logging.getLogger(__name__)

DevHook = Callable[["ChudApp"], Awaitable[None]]
ScenarioFactory = Callable[[Path | None], DevHook]

_DEFAULT_QUESTION_FIXTURE = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "question_sample.json"
)


def _load_seed(path: Path | None, default: Path) -> dict[str, Any]:
    """Load a JSON object seed from ``path`` (or ``default`` if None)."""
    target = path if path is not None else default
    with target.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"seed JSON must be an object, got {type(data).__name__}")
    return data


def make_question_hook(seed_path: Path | None) -> DevHook:
    """Build a hook that pushes ``QuestionModal`` with ``seed_path`` JSON."""
    payload = _load_seed(seed_path, _DEFAULT_QUESTION_FIXTURE)

    async def hook(app: ChudApp) -> None:
        from chud.widgets.question_modal import QuestionModal

        session_id = "dev-question"
        log.info(
            "dev: pushing QuestionModal with seeded payload (%d questions)",
            len(payload.get("questions", []) or []),
        )
        result = await app.push_screen_wait(
            QuestionModal(session_id=session_id, question_input=payload)
        )
        log.info("dev: QuestionModal returned: %r", result)

    return hook


SCENARIOS: dict[str, ScenarioFactory] = {
    "question": make_question_hook,
}
