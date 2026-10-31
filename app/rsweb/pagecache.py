"""Slow page-only computations kept for an hour (owner 2026-10-21: the loadout plan, prospects and destination lists
only need rerunning every hour or two). Fleets › Fleet details and the Fleets overview read them from here instead of
recomputing on every load; the engine plans for itself as before. Any change under /fleets or /loadouts clears it
(main.py), and the pages' Recompute button does too."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

TTL = 3600.0
log = logging.getLogger(__name__)


def _store(app) -> dict[str, tuple[float, Any]]:
    if not hasattr(app.state, "pagecache"):
        app.state.pagecache = {}
        app.state.pagecache_running = {}   # key -> the task working it out now (one per key, shared by every caller)
        app.state.pagecache_gen = 0        # bumped by clear(): a result begun before an edit isn't kept
    return app.state.pagecache


async def get(request, key: str, fn: Callable[[], Awaitable[Any]], ttl: float = TTL) -> Any:
    """The kept value. Older than `ttl`, the old one is served while one background task works out the next
    (2026-10-31: pages waited seconds for an hourly replan); with nothing kept, the caller waits for that task,
    and every page asking at the same time shares it."""
    app = request.app
    store = _store(app)
    hit = store.get(key)
    fresh = hit and time.time() - hit[0] < ttl
    if fresh:
        return hit[1]
    task = app.state.pagecache_running.get(key)
    if task is None:
        gen = app.state.pagecache_gen

        async def work() -> Any:
            try:
                v = await fn()
                if app.state.pagecache_gen == gen:
                    store[key] = (time.time(), v)
                return v
            finally:
                if app.state.pagecache_running.get(key) is task:
                    del app.state.pagecache_running[key]
        task = app.state.pagecache_running[key] = asyncio.ensure_future(work())
        if hit:   # a stale value is there: don't leave a failed refresh unread
            task.add_done_callback(lambda t: t.cancelled() or not t.exception()
                                   or log.warning("page cache refresh of %s failed: %s", key, t.exception()))
    if hit:
        return hit[1]
    return await asyncio.shield(task)   # one page closing doesn't cancel the work the others share


def age_minutes(request, prefix: str = "") -> int | None:
    """Minutes since the oldest kept entry (under a key prefix) was computed; None when nothing is kept."""
    ts = [t for k, (t, _) in _store(request.app).items() if k.startswith(prefix)]
    return int((time.time() - min(ts)) // 60) if ts else None


def clear(app, prefix: str = "") -> None:
    """Forget every kept entry, or only those under a key prefix."""
    store = _store(app)
    app.state.pagecache_gen += 1
    for k in [k for k in store if k.startswith(prefix)]:
        del store[k]
    running = app.state.pagecache_running
    for k in [k for k in running if k.startswith(prefix)]:
        del running[k]   # its result predates the edit: the next page starts over


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
