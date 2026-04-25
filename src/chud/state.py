from __future__ import annotations

import json
from pathlib import Path

from platformdirs import user_data_dir

from chud.types import SessionState


def data_root() -> Path:
    root = Path(user_data_dir("chud", appauthor=False))
    root.mkdir(parents=True, exist_ok=True)
    return root


def workspaces_root() -> Path:
    root = data_root() / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    return root


def sessions_file() -> Path:
    return data_root() / "sessions.json"


def load_all() -> dict[str, SessionState]:
    path = sessions_file()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {sid: SessionState.from_dict(d) for sid, d in raw.items()}


def save_all(sessions: dict[str, SessionState]) -> None:
    path = sessions_file()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({sid: s.to_dict() for sid, s in sessions.items()}, indent=2))
    tmp.replace(path)
