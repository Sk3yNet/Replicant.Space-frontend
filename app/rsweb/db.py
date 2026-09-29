"""SQLite storage: snapshots, full event history, timers, notifications, visits."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

import aiosqlite

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


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # --- generic helpers -------------------------------------------------
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        cur = await self.conn.execute(sql, tuple(params))
        await self.conn.commit()
        return cur

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        async with self.conn.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    # --- key/value snapshots ---------------------------------------------
    async def kv_set(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), now_iso()),
        )

    async def kv_get(self, key: str, default: Any = None) -> Any:
        row = await self.fetchone("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

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
