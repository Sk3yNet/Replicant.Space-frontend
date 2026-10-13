"""Where the time goes: per-request timing (database queries, saved values decoded, game API calls), engine stage
timings, engine lock holds, and a storage report. Measures only; nothing here changes behavior.

A request's numbers are gathered through a context variable, so the database and API wrappers just call note_*().
Each page answer carries a Server-Timing header (browser dev tools → Network → Timing) and requests over SLOW_MS
are logged. The diagnostics page and the snapshot show the slowest recent requests and stages."""
from __future__ import annotations

import contextvars
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("rsweb.perf")

SLOW_MS = 1000
RECENT = 500                       # requests remembered (in memory, since the server started)
_cur: contextvars.ContextVar[dict | None] = contextvars.ContextVar("rsweb_perf", default=None)
requests: deque = deque(maxlen=RECENT)
stages: dict[str, dict] = {}       # engine stage -> {n, last_ms, max_ms, total_ms}
holds: dict[str, dict] = {}        # engine lock holder -> {n, last_ms, max_ms, total_ms}
waits: dict[str, float] = {"n": 0, "max_ms": 0.0, "total_ms": 0.0}   # requests waiting on the engine lock


def start() -> dict:
    m = {"db_n": 0, "db_ms": 0.0, "kv": {}, "api_n": 0, "api_ms": 0.0, "lock_ms": 0.0}
    _cur.set(m)
    return m


def note_query(ms: float) -> None:
    m = _cur.get()
    if m is not None:
        m["db_n"] += 1
        m["db_ms"] += ms


def note_kv(key: str, size: int, ms: float) -> None:
    m = _cur.get()
    if m is not None:
        k = m["kv"].setdefault(key, [0, 0, 0.0])
        k[0] += 1
        k[1] += size
        k[2] += ms


def note_api(ms: float) -> None:
    m = _cur.get()
    if m is not None:
        m["api_n"] += 1
        m["api_ms"] += ms


def note_lock_wait(ms: float) -> None:
    m = _cur.get()
    if m is not None:
        m["lock_ms"] += ms
        waits["n"] += 1
        waits["total_ms"] += ms
        waits["max_ms"] = max(waits["max_ms"], ms)


def _acc(d: dict, name: str, ms: float) -> None:
    s = d.setdefault(name, {"n": 0, "last_ms": 0.0, "max_ms": 0.0, "total_ms": 0.0})
    s["n"] += 1
    s["last_ms"] = round(ms, 1)
    s["max_ms"] = round(max(s["max_ms"], ms), 1)
    s["total_ms"] += ms


def note_stage(name: str, ms: float) -> None:
    _acc(stages, name, ms)


def note_hold(holder: str, ms: float) -> None:
    _acc(holds, holder or "?", ms)


def finish(m: dict, method: str, path: str, status: int, total_ms: float) -> dict:
    _cur.set(None)
    kv = sorted(m["kv"].items(), key=lambda kv: -kv[1][2])
    r = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "method": method, "path": path, "status": status,
         "ms": round(total_ms, 1), "db_n": m["db_n"], "db_ms": round(m["db_ms"], 1), "api_n": m["api_n"],
         "api_ms": round(m["api_ms"], 1), "lock_ms": round(m["lock_ms"], 1),
         "kv_kb": round(sum(v[1] for _, v in kv) / 1024, 1),
         "kv": [{"key": k, "reads": v[0], "kb": round(v[1] / 1024, 1), "ms": round(v[2], 1)} for k, v in kv[:8]]}
    requests.append(r)
    if total_ms >= SLOW_MS:
        log.warning("slow request %s %s: %.0f ms (db %d queries %.0f ms, api %d calls %.0f ms, engine lock %.0f ms, "
                    "%.0f KB of saved values)", method, path, total_ms, r["db_n"], r["db_ms"], r["api_n"], r["api_ms"],
                    r["lock_ms"], r["kv_kb"])
    return r


def by_path() -> list[dict]:
    """Recent requests grouped by path (ids collapsed), slowest average first."""
    import re
    g: dict[str, list] = {}
    for r in requests:
        key = f'{r["method"]} ' + re.sub(r"/[0-9A-Fa-f]{6,}(?=/|$)|/[A-Z0-9]+(?:-[A-Z0-9]+)+(?=/|$)", "/{id}", r["path"])
        g.setdefault(key, []).append(r)
    out = [{"path": k, "n": len(v), "avg_ms": round(sum(x["ms"] for x in v) / len(v), 1), "max_ms": max(x["ms"] for x in v),
            "avg_db_n": round(sum(x["db_n"] for x in v) / len(v), 1), "avg_kv_kb": round(sum(x["kv_kb"] for x in v) / len(v), 1)}
           for k, v in g.items()]
    return sorted(out, key=lambda x: -x["avg_ms"])


def _table(d: dict) -> list[dict]:
    return sorted(({"name": k, "n": v["n"], "last_ms": v["last_ms"], "max_ms": v["max_ms"],
                    "avg_ms": round(v["total_ms"] / max(v["n"], 1), 1)} for k, v in d.items()), key=lambda x: -x["avg_ms"])


async def storage(db) -> dict:
    """Rows per table, the database file size, and the largest saved values."""
    tables = {}
    for t in ("events", "inventory_history", "systems", "actions", "notifications", "timers", "kv"):
        try:
            tables[t] = (await db.fetchone(f"SELECT COUNT(*) AS n FROM {t}"))["n"]
        except Exception:
            tables[t] = None
    files = {}
    for suffix in ("", "-wal"):
        try:
            files["db" + suffix] = round(os.path.getsize(db.path + suffix) / 1048576, 1)
        except OSError:
            pass
    big = await db.fetchall("SELECT key, length(value) AS bytes, updated_at FROM kv ORDER BY bytes DESC LIMIT 15")
    oldest = await db.fetchone("SELECT MIN(received_at) AS t FROM events")
    return {"tables": tables, "files_mb": files, "oldest_event": (oldest or {}).get("t"),
            "kv_largest": [{"key": r["key"], "kb": round(r["bytes"] / 1024, 1), "updated_at": r["updated_at"]} for r in big]}


async def report(db) -> dict:
    slow = sorted(requests, key=lambda r: -r["ms"])[:20]
    return {"slow_requests": slow, "by_path": by_path()[:25], "stages": _table(stages), "lock_holds": _table(holds),
            "lock_waits": {"n": waits["n"], "max_ms": round(waits["max_ms"], 1),
                           "avg_ms": round(waits["total_ms"] / max(waits["n"], 1), 1)},
            "requests_seen": len(requests), "storage": await storage(db)}


class Middleware:
    """Pure ASGI (so streamed answers like the live event feed pass straight through, untimed)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path") or ""
        if scope["type"] != "http" or path.startswith(("/static", "/live/stream")):
            return await self.app(scope, receive, send)
        m = start()
        t0 = time.perf_counter()
        status = {"code": 0}

        async def _send(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]
                ctype = dict(msg.get("headers") or []).get(b"content-type", b"")
                if not ctype.startswith(b"text/event-stream"):
                    r = dict(m, ms=(time.perf_counter() - t0) * 1000)
                    st = (f'total;dur={r["ms"]:.1f}, db;desc="{m["db_n"]} queries";dur={m["db_ms"]:.1f}, '
                          f'api;desc="{m["api_n"]} calls";dur={m["api_ms"]:.1f}')
                    msg = dict(msg, headers=list(msg.get("headers") or []) + [(b"server-timing", st.encode())])
            await send(msg)

        try:
            await self.app(scope, receive, _send)
        finally:
            finish(m, scope.get("method") or "", path, status["code"], (time.perf_counter() - t0) * 1000)
