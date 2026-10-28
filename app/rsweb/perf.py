"""Where the time goes: per-request timing (database queries, saved values decoded, game API calls), engine stage
timings, engine lock holds, and a storage report. Measures only; nothing here changes behavior.

A request's numbers are gathered through a context variable, so the database and API wrappers just call note_*().
Each page answer carries a Server-Timing header (browser dev tools → Network → Timing) and requests over SLOW_MS
are logged. The diagnostics page and the snapshot show the slowest recent requests and stages."""
from __future__ import annotations

import contextvars
import logging
import os
import re
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


def in_request() -> bool:
    return _cur.get() is not None


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


READS_WINDOW = 600                  # game reads are reported over the last 10 minutes
reads: deque = deque(maxlen=3000)  # game GETs: (time, source, path pattern)
_CODE_RE = re.compile(r"/[0-9A-F]{8}(?=/|$)")
_PLACE_RE = re.compile(r"/[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*(?=/|$)")


def read_source() -> str:
    """Who is asking the game: a page, a poller (its task name), an engine stage, or other background work."""
    if in_request():
        return "page"
    try:
        import asyncio
        name = asyncio.current_task().get_name()
    except RuntimeError:
        return "other"
    if name == "automations":
        st = _sampler.get("stage_of")
        stage = st() if st else None
        return f"engine: {stage}" if stage else "engine"
    return "background" if name.startswith("Task-") else name


def note_read(path: str) -> None:
    pattern = _PLACE_RE.sub("/{place}", _CODE_RE.sub("/{code}", path.split("?")[0]))
    reads.append((time.time(), read_source(), pattern))


def reads_report(n: int = 30) -> dict:
    """Game reads per minute over the last READS_WINDOW seconds, by source and path, and the busiest minute."""
    now = time.time()
    recent = [r for r in reads if r[0] >= now - READS_WINDOW]
    span = min(READS_WINDOW, now - recent[0][0]) / 60 if recent else 1
    span = max(span, 1)
    by: dict[tuple, int] = {}
    for _, src, pat in recent:
        by[(src, pat)] = by.get((src, pat), 0) + 1
    minutes: dict[int, int] = {}
    for t, _, _ in recent:
        minutes[int(t // 60)] = minutes.get(int(t // 60), 0) + 1
    return {"minutes": round(span, 1), "per_min": round(len(recent) / span, 1), "peak_min": max(minutes.values(), default=0),
            "rows": [{"source": s, "path": p, "n": c, "per_min": round(c / span, 1)}
                     for (s, p), c in sorted(by.items(), key=lambda x: -x[1])[:n]]}


def note_lock_wait(ms: float) -> None:
    m = _cur.get()
    if m is not None:
        m["lock_ms"] += ms
        waits["n"] += 1
        waits["total_ms"] += ms
        waits["max_ms"] = max(waits["max_ms"], ms)


def _acc(d: dict, name: str, ms: float, cpu_ms: float | None = None) -> None:
    s = d.setdefault(name, {"n": 0, "last_ms": 0.0, "max_ms": 0.0, "total_ms": 0.0, "cpu_ms": 0.0})
    s["n"] += 1
    s["last_ms"] = round(ms, 1)
    s["max_ms"] = round(max(s["max_ms"], ms), 1)
    s["total_ms"] += ms
    if cpu_ms is not None:
        s["cpu_ms"] += cpu_ms


def note_stage(name: str, ms: float, cpu_ms: float | None = None) -> None:
    """cpu_ms: the event loop thread's own CPU time in the stage (time.thread_time), i.e. how long it kept pages
    waiting; the rest of ms was spent awaiting the database or the game."""
    _acc(stages, name, ms, cpu_ms)


# event loop lag: a ticker that should wake every LAG_STEP s; how late it wakes is how long pages were held up
LAG_STEP = 0.25
lag: dict = {"max_ms": 0.0, "stalls": 0, "stalled_ms": 0.0, "by_stage": {}}   # stalls: wakes over 1 s late


# sampling profiler: a thread looks at the event loop thread's stack every SAMPLE_S while it's busy, so the snapshot
# shows which code holds the server (by the outermost and innermost rsweb function), without slowing it like cProfile
SAMPLE_S = 0.02
hot: dict = {"samples": 0, "busy": 0, "entry": {}, "leaf": {}, "pair": {}, "func": {}, "inner": {}}
_sampler: dict = {"thread": None}


def _where(f) -> str:
    return f"{os.path.basename(f.f_code.co_filename)}:{f.f_code.co_name}"


def sample_once(main_ident: int, stage_of) -> None:
    import sys
    f = sys._current_frames().get(main_ident)
    hot["samples"] += 1
    if f is None or (f.f_code.co_name == "select" and "selectors" in f.f_code.co_filename):
        return   # idle: the loop waits for something to do
    hot["busy"] += 1
    hot["inner"][_where(f)] = hot["inner"].get(_where(f), 0) + 1   # the innermost frame, library code included (json, re …)
    ours = []
    while f is not None:
        if "rsweb" in f.f_code.co_filename and "perf.py" not in f.f_code.co_filename:
            ours.append(_where(f))
        f = f.f_back
    if not ours:
        ours = ["(outside rsweb)"]
    leaf, entry = ours[0], ours[-1]
    st = stage_of() if stage_of else None
    if st:
        entry = f"{entry} [{st}]"
    for d, k in ((hot["entry"], entry), (hot["leaf"], leaf), (hot["pair"], f"{entry} → {leaf}")):
        d[k] = d.get(k, 0) + 1
    for k in set(ours):   # inclusive: every rsweb function on the stack
        hot["func"][k] = hot["func"].get(k, 0) + 1


def start_sampler(stage_of=None) -> None:
    """Start the sampling thread once (it lives as long as the process)."""
    import threading
    if _sampler["thread"] is not None:
        return
    _sampler["stage_of"] = stage_of
    main = threading.get_ident()   # called from the event loop's thread

    def run() -> None:
        while True:
            time.sleep(SAMPLE_S)
            try:
                sample_once(main, stage_of)
            except Exception:
                pass
    t = threading.Thread(target=run, name="perf-sampler", daemon=True)
    _sampler["thread"] = t
    t.start()


def hot_report(n: int = 25) -> dict:
    def top(d: dict) -> list[dict]:
        busy = max(hot["busy"], 1)
        return [{"where": k, "samples": v, "pct": round(100 * v / busy, 1)} for k, v in sorted(d.items(), key=lambda x: -x[1])[:n]]
    return {"sample_ms": SAMPLE_S * 1000, "samples": hot["samples"], "busy": hot["busy"],
            "busy_pct": round(100 * hot["busy"] / max(hot["samples"], 1), 1),
            "entry": top(hot["entry"]), "leaf": top(hot["leaf"]), "inclusive": top(hot["func"]), "pairs": top(hot["pair"]),
            "innermost": top(hot["inner"])}


async def watch_loop(stage_of) -> None:
    """Measure event loop stalls, credited to the engine stage running then (stage_of() → name or None)."""
    import asyncio
    while True:
        t = time.perf_counter()
        await asyncio.sleep(LAG_STEP)
        late = (time.perf_counter() - t - LAG_STEP) * 1000
        lag["max_ms"] = round(max(lag["max_ms"], late), 1)
        if late >= 1000:
            lag["stalls"] += 1
            lag["stalled_ms"] += late
            st = stage_of() or "(no engine stage)"
            b = lag["by_stage"].setdefault(st, {"n": 0, "max_ms": 0.0, "total_ms": 0.0})
            b["n"] += 1
            b["max_ms"] = round(max(b["max_ms"], late), 1)
            b["total_ms"] = round(b["total_ms"] + late, 1)


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
                    "avg_ms": round(v["total_ms"] / max(v["n"], 1), 1),
                    **({"avg_cpu_ms": round(v["cpu_ms"] / max(v["n"], 1), 1)} if v.get("cpu_ms") else {})}
                   for k, v in d.items()), key=lambda x: -x["avg_ms"])


async def storage(db) -> dict:
    """Rows per table, the database file size, and the largest saved values."""
    tables = {}
    for t in ("events", "inventory_history", "metric_history", "systems", "actions", "notifications", "timers", "kv"):
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
            "loop_lag": {"max_ms": lag["max_ms"], "stalls_over_1s": lag["stalls"], "stalled_s": round(lag["stalled_ms"] / 1000, 1),
                         "by_stage": sorted(({"stage": k, **v} for k, v in lag["by_stage"].items()), key=lambda x: -x["total_ms"])[:15]},
            "hot": hot_report(), "reads": reads_report(), "requests_seen": len(requests), "storage": await storage(db)}


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
