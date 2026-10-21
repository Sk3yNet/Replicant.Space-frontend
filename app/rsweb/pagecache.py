"""Slow page-only computations kept for an hour (owner 2026-10-21: the loadout plan, prospects and destination lists
only need rerunning every hour or two). Fleets › Fleet details and the Fleets overview read them from here instead of
recomputing on every load; the engine plans for itself as before. Any change under /fleets or /loadouts clears it
(main.py), and the pages' Recompute button does too."""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

TTL = 3600.0


def _store(app) -> dict[str, tuple[float, Any]]:
    if not hasattr(app.state, "pagecache"):
        app.state.pagecache = {}
    return app.state.pagecache


async def get(request, key: str, fn: Callable[[], Awaitable[Any]], ttl: float = TTL) -> Any:
    store = _store(request.app)
    hit = store.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = await fn()
    store[key] = (time.time(), v)
    return v


def age_minutes(request, prefix: str = "") -> int | None:
    """Minutes since the oldest kept entry (under a key prefix) was computed; None when nothing is kept."""
    ts = [t for k, (t, _) in _store(request.app).items() if k.startswith(prefix)]
    return int((time.time() - min(ts)) // 60) if ts else None


def clear(app) -> None:
    _store(app).clear()


class ClearOnEdit:
    """ASGI middleware: a change to fleets or loadouts (a POST under /fleets or /loadouts) makes the kept plans stale, so
    they're recomputed on the next view."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method") == "POST" and str(scope.get("path", "")).startswith(("/fleets", "/loadouts")):
            app = scope.get("app")
            if app is not None:
                clear(app)
        await self.app(scope, receive, send)
