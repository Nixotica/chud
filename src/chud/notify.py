from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger(__name__)

_NOTIFY_SEND = shutil.which("notify-send")


def desktop_notify(title: str, body: str) -> None:
    """Best-effort OS notification. Silent no-op if no backend is available."""
    if _NOTIFY_SEND is None:
        return
    try:
        subprocess.Popen(
            [_NOTIFY_SEND, "--app-name=chud", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        log.exception("desktop_notify failed")
