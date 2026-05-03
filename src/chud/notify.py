from __future__ import annotations

import logging

from desktop_notifier import DesktopNotifier

log = logging.getLogger(__name__)

_notifier = DesktopNotifier(app_name="chud")


async def desktop_notify(title: str, body: str) -> None:
    """Best-effort OS notification. Silent no-op if no backend is available."""
    try:
        await _notifier.send(title=title, message=body)
    except Exception:
        log.exception("desktop_notify failed")
