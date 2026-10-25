"""SQLite storage: snapshots, full event history, timers, notifications, visits."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import aiosqlite

from . import perf

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Every game event we have ever seen (the game only keeps ~10k).
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    event TEXT NOT NULL,
    category TEXT,
    replicant_code TEXT,
    device_code TEXT,
    device_type TEXT,
    star TEXT,
    location TEXT,
    payload TEXT,
    created_at TEXT,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_received ON events(received_at);
CREATE INDEX IF NOT EXISTS ix_events_category ON events(category);
CREATE INDEX IF NOT EXISTS ix_events_device ON events(device_code);
CREATE INDEX IF NOT EXISTS ix_events_event ON events(event);
-- per-system lookups (targets.system_resources / system_targets): star = ? OR location in [STAR-, STAR.)
CREATE INDEX IF NOT EXISTS ix_events_star_event ON events(star, event);
CREATE INDEX IF NOT EXISTS ix_events_loc_event ON events(location, event);

-- Things in progress with a known finish time (travel, prints, scans ...).
CREATE TABLE IF NOT EXISTS timers (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    device_code TEXT,
    replicant_code TEXT,
    location TEXT,
    started_at TEXT,
    ends_at TEXT NOT NULL,
    source TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,
    level TEXT NOT NULL,          -- alert | done | info
    title TEXT NOT NULL,
    body TEXT,
    link TEXT,
    created_at TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_notif_read ON notifications(read, id);

-- Per-person visit tracking for the "since you were last here" digest.
CREATE TABLE IF NOT EXISTS visitors (
    email TEXT PRIMARY KEY,
    last_seen_at TEXT NOT NULL,
    visit_started_at TEXT NOT NULL,
    baseline_at TEXT NOT NULL,
    digest_dismissed INTEGER NOT NULL DEFAULT 0
);

-- Resource totals over time (for charts and the digest's inventory delta).
CREATE TABLE IF NOT EXISTS inventory_history (
    ts TEXT NOT NULL,
    resource TEXT NOT NULL,
    qty REAL NOT NULL,
    PRIMARY KEY (ts, resource)
);

-- Logistics backlog samples over time (metrics.py, Dashboard › Logistics backlog).
CREATE TABLE IF NOT EXISTS metric_history (
    ts TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (ts, metric)
);

-- Cached system scans / location details.
CREATE TABLE IF NOT EXISTS systems (
    star TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Every command issued through the UI.
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    user TEXT,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    body TEXT,
    status INTEGER,
    response TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Saved values decoded once and kept in memory until the next kv_set (the star catalog is ~13 MB of JSON and read
# several times per page and per tick). Each read gets its own copy of the top level and of each list item, so a
# caller annotating star dicts (the map does) doesn't change the cached catalog.
CACHED = {"stars"}
SPLIT_KEYS = ("devices", "inventory", "stars")   # their VIRT… (simulated) part goes to sim:<key>


def _copy(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: ([dict(x) if isinstance(x, dict) else x for x in w] if isinstance(w, list) else
                    dict(w) if isinstance(w, dict) else w) for k, w in v.items()}
    return v


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.rconn: aiosqlite.Connection | None = None
        self._cache: dict[str, Any] = {}
        self._derived: dict[tuple[str, str], Any] = {}
        self._sim_split: set[str] = set()

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()
        await self.separate_simulated()
        # page requests read through their own connection (WAL lets it read while the engine writes), so a page's
        # reads don't queue behind the engine's on the one connection's worker thread; every write stays on conn
        self.rconn = await aiosqlite.connect(self.path)
        self.rconn.row_factory = aiosqlite.Row
        await self.rconn.execute("PRAGMA query_only=1")

    async def separate_simulated(self) -> None:
        """Move what a simulation left among the real data (before 1.109.0) to its own place: VIRT… systems, stockpile
        keys, devices, stockpiles and stars; its events are kept (jobs wait on them) but marked category simulation."""
        await self.conn.execute("DELETE FROM systems WHERE star LIKE 'VIRT%'")
        await self.conn.execute("DELETE FROM kv WHERE key LIKE 'loc:VIRT%'")
        await self.conn.execute("UPDATE events SET category='simulation' WHERE (star LIKE 'VIRT%' OR location LIKE 'VIRT%') "
                                "AND category != 'simulation'")
        await self.conn.commit()
        for key in SPLIT_KEYS:
            cur = await self.kv_get(key)
            if cur is not None:
                await self.kv_set(key, cur)

    async def close(self) -> None:
        if self.rconn:
            await self.rconn.close()
        if self.conn:
            await self.conn.close()

    def _reader(self) -> aiosqlite.Connection:
        """The connection for a read: the read-only one inside a page request (perf has a request open), else conn."""
        return self.rconn if self.rconn is not None and perf.in_request() else self.conn

    # --- generic helpers -------------------------------------------------
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        params = tuple(params)
        if sql.lstrip().upper().startswith("INSERT OR REPLACE INTO SYSTEMS") and params \
                and str(params[0] or "").upper().startswith("VIRT"):
            # a simulated system's survey: kept apart (sim:systems), never on the map or the systems pages
            virt = await self.kv_get("sim:systems", {}) or {}
            try:
                virt[params[0]] = json.loads(params[1])
            except (TypeError, ValueError, IndexError):
                pass
            await self._kv_write("sim:systems", virt)
            return await self.conn.execute("SELECT 1")
        t0 = time.perf_counter()
        cur = await self.conn.execute(sql, tuple(params))
        await self.conn.commit()
        perf.note_query((time.perf_counter() - t0) * 1000)
        return cur

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        t0 = time.perf_counter()
        async with self._reader().execute(sql, tuple(params)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        perf.note_query((time.perf_counter() - t0) * 1000)
        return rows

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        t0 = time.perf_counter()
        async with self._reader().execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
        perf.note_query((time.perf_counter() - t0) * 1000)
        return dict(row) if row else None

    # --- key/value snapshots ---------------------------------------------
    async def kv_set(self, key: str, value: Any) -> None:
        if key in SPLIT_KEYS:   # simulated devices / stockpiles / stars never mix with the real ones (sim.py)
            from .sim import split_kv
            value, virt = split_kv(key, value)
            if virt or key in self._sim_split:
                self._sim_split.add(key)
                await self._kv_write(f"sim:{key}", virt or [])
        await self._kv_write(key, value)

    async def _kv_write(self, key: str, value: Any) -> None:
        self._cache.pop(key, None)
        for k in [k for k in self._derived if k[0] == key]:
            del self._derived[k]
        await self.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), now_iso()),
        )

    async def kv_get(self, key: str, default: Any = None) -> Any:
        t0 = time.perf_counter()
        if key in self._cache:
            value = _copy(self._cache[key])
            perf.note_kv(key, 0, (time.perf_counter() - t0) * 1000)
            return value
        row = await self.fetchone("SELECT value FROM kv WHERE key=?", (key,))
        if not row:
            return default
        value = json.loads(row["value"])
        if key in CACHED:
            self._cache[key] = value
            value = _copy(value)
        perf.note_kv(key, len(row["value"]), (time.perf_counter() - t0) * 1000)   # read + decode
        return value

    async def kv_derived(self, key: str, name: str, fn: Callable[[Any], Any], default: Any = None) -> Any:
        """fn(value of `key`), worked out once and shared until the key is next written. Callers must not change the
        result. For the hot readers of the star catalog (tens of thousands of stars, read every pass): copying it on each
        read and rebuilding the same lookups was most of an engine pass (2026-10-21)."""
        if (key, name) not in self._derived:
            row = await self.fetchone("SELECT value FROM kv WHERE key=?", (key,)) if key not in self._cache else None
            src = self._cache[key] if key in self._cache else (json.loads(row["value"]) if row else default)
            if key in CACHED and row:
                self._cache[key] = src
            self._derived[(key, name)] = fn(src)
        return self._derived[(key, name)]

    async def kv_updated(self, key: str) -> str | None:
        row = await self.fetchone("SELECT updated_at FROM kv WHERE key=?", (key,))
        return row["updated_at"] if row else None

    # --- events ------------------------------------------------------------
    async def insert_event(self, ev: dict) -> bool:
        """Store an event; returns False if we already had it."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO events(id, event, category, replicant_code, device_code, device_type, "
            "star, location, payload, created_at, received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(ev.get("id")),
                ev.get("event") or "unknown",
                ev.get("category") or (ev.get("event") or "").split(".")[0],
                ev.get("replicant_code"),
                ev.get("device_code"),
                ev.get("device_type"),
                ev.get("star"),
                ev.get("location"),
                json.dumps(ev.get("payload") or {}),
                ev.get("created_at"),
                now_iso(),
            ),
        )
        await self.conn.commit()
        return cur.rowcount > 0


def row_event(row: dict) -> dict:
    """DB row -> event dict with parsed payload."""
    out = dict(row)
    try:
        out["payload"] = json.loads(row.get("payload") or "{}")
    except (TypeError, ValueError):
        out["payload"] = {}
    return out
