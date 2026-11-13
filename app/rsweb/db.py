"""SQLite storage: snapshots, full event history, timers, notifications, visits."""
from __future__ import annotations

import hashlib
import json
import marshal
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
-- The device list, one row per device (kv_set("devices") writes only the devices that changed; the kv row "devices"
-- holds their order). See DB._rows_write.
CREATE TABLE IF NOT EXISTS device_rows (
    key TEXT NOT NULL,
    code TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (key, code)
);

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
ROW_KEYS = {"devices"}   # lists of devices stored one row each (device_rows) instead of one JSON value
ROWS_MARK = "__device_rows__"
PACK_MAX = 2_000_000   # saved values up to this many characters of JSON are also kept decoded in memory (kv_get)


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
        self._packed: dict[str, bytes] = {}   # key -> the decoded value, marshaled (see kv_get)
        self._ver: dict[str, int] = {}        # key -> writes seen, so a read racing a write isn't kept
        self._digest: dict[str, bytes] = {}   # key -> digest of the JSON last written (see _kv_write)
        self._rows: dict[str, dict] = {}      # ROW_KEYS key -> {"order": [codes], "raw": {code: bytes}, "obj": {code: bytes}}
        self.gen: dict[str, int] = {"systems": 0, "scans": 0, "all": 0}   # bumped on writes; memo keys for readers of those rows
        self.star_gen: dict[str, int] = {}    # star -> bumped when an event, a location detail or a scan of it is stored
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
        head = sql.lstrip()[:40].upper()
        if "SYSTEMS" in head:
            self.gen["systems"] += 1
            for v in (params[:1] + params[-1:]):   # the star is the first value of an insert, the last of an update
                self.touch_star(v)
        if head.startswith("DELETE") and ("EVENTS" in head or " KV" in head or "SYSTEMS" in head):
            self.gen["all"] += 1    # rows gone: every per-star memo starts over
        if "EVENTS" in head:
            self.gen["scans"] += 1
        cur = await self.conn.execute(sql, tuple(params))
        await self.conn.commit()
        if "SYSTEMS" in head:
            self.gen["systems"] += 1
        if "EVENTS" in head:
            self.gen["scans"] += 1
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

    def touch_star(self, place: Any) -> None:
        """Something stored about this system (or a place in it) changed: targets.system_resources reads it again."""
        if isinstance(place, str) and place and len(place) < 80:
            star = place.split("-")[0].upper()
            self.star_gen[star] = self.star_gen.get(star, 0) + 1

    def kv_version(self, key: str) -> tuple[int, int]:
        """Changes with every write of `key` through this database (for memos of values worked out from it)."""
        return id(self), self._ver.get(key, 0)

    async def _kv_write(self, key: str, value: Any) -> None:
        if key.startswith("loc:"):
            self.touch_star(key[4:])
        self._cache.pop(key, None)
        self._packed.pop(key, None)
        self._ver[key] = self._ver.get(key, 0) + 1
        for k in [k for k in self._derived if k[0] == key]:
            del self._derived[k]
        if key in ROW_KEYS and await self._rows_write(key, value):
            self._ver[key] = self._ver.get(key, 0) + 1
            return
        text = json.dumps(value)
        digest = hashlib.blake2b(text.encode(), digest_size=16).digest()
        try:
            if self._digest.get(key) == digest:
                # the same value as last written (passes save their state every tick whether it changed or not, some
                # of it megabytes): only the timestamp moves. A row deleted meanwhile is written in full.
                cur = await self.execute("UPDATE kv SET updated_at=? WHERE key=?", (now_iso(), key))
                if cur.rowcount:
                    return
            self._digest.pop(key, None)
            await self.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, text, now_iso()),
            )
            self._digest[key] = digest
        finally:
            self._ver[key] = self._ver.get(key, 0) + 1

    # --- device rows ---------------------------------------------------------------------------------------------
    # The device list is a megabyte of JSON and something about one device changes with nearly every game event.
    # Stored as one value it was encoded, hashed and rewritten whole each time, and decoded whole on the next read
    # (about a quarter of the server's busy time, 2026-11-13). Stored one row per device, a write compares each
    # device with what was last written and stores only the changed ones; a read hands out copies from memory.
    # kv_get / kv_set("devices") behave as before for every caller.
    async def _rows_load(self, key: str) -> dict | None:
        """The rows of `key` from the database, or None when it is (still) stored as one value."""
        ver = self._ver.get(key, 0)
        row = await self.fetchone("SELECT value FROM kv WHERE key=?", (key,))
        try:
            mark = json.loads(row["value"]) if row else None
        except ValueError:
            mark = None
        if not (isinstance(mark, dict) and mark.get(ROWS_MARK)):
            return None
        data = {r["code"]: r["data"] for r in await self.fetchall("SELECT code, data FROM device_rows WHERE key=?", (key,))}
        order = [c for c in mark.get("order") or [] if c in data]
        st = {"order": order, "raw": {}, "obj": {c: marshal.dumps(json.loads(data[c])) for c in order}}
        if key in self._rows:        # written while this was read
            return self._rows[key]
        if self._ver.get(key, 0) == ver:
            self._rows[key] = st
        return st

    async def _rows_write(self, key: str, value: Any) -> bool:
        """Store a list of devices by row. False: not such a list (stored as one value, as any other key)."""
        codes = [d.get("device_code") if isinstance(d, dict) else None for d in value] if isinstance(value, list) else None
        if not codes or not all(isinstance(c, str) and c for c in codes) or len(set(codes)) != len(codes):
            if self._rows.pop(key, None) is not None or await self._rows_load(key) is not None:
                self._rows.pop(key, None)
                await self.conn.execute("DELETE FROM device_rows WHERE key=?", (key,))
                await self.conn.commit()
            return False
        old = self._rows.get(key) or await self._rows_load(key) or {"order": [], "raw": {}, "obj": {}}
        raw, obj, changed = dict(old["raw"]), dict(old["obj"]), []
        try:
            for c, d in zip(codes, value):
                b = marshal.dumps(d, 2)      # version 2: the same content gives the same bytes
                if raw.get(c) != b or c not in obj:
                    text = json.dumps(d)
                    o = marshal.dumps(json.loads(text))   # what a reader gets: as JSON gives it back
                    if obj.get(c) != o:
                        changed.append((key, c, text))
                    raw[c], obj[c] = b, o
        except ValueError:
            return False
        have = set(codes)
        gone = [c for c in obj if c not in have]
        for c in gone:
            raw.pop(c, None)
            obj.pop(c, None)
        first = not old["order"] and not old["obj"]
        self._rows[key] = {"order": codes, "raw": raw, "obj": obj}
        t0 = time.perf_counter()
        try:
            if changed:
                await self.conn.executemany(
                    "INSERT INTO device_rows(key, code, data) VALUES(?,?,?) "
                    "ON CONFLICT(key, code) DO UPDATE SET data=excluded.data", changed)
            if gone:
                await self.conn.executemany("DELETE FROM device_rows WHERE key=? AND code=?", [(key, c) for c in gone])
            if changed or gone or first or codes != old["order"]:
                await self.conn.execute(
                    "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, json.dumps({ROWS_MARK: True, "order": codes}), now_iso()))
            else:
                await self.conn.execute("UPDATE kv SET updated_at=? WHERE key=?", (now_iso(), key))
            await self.conn.commit()
        except Exception:
            self._rows.pop(key, None)    # memory no longer matches the database: read it again next time
            try:
                await self.conn.rollback()
            except Exception:
                pass
            raise
        self._digest.pop(key, None)
        perf.note_query((time.perf_counter() - t0) * 1000)
        return True

    async def kv_get(self, key: str, default: Any = None) -> Any:
        t0 = time.perf_counter()
        if key in self._cache:
            value = _copy(self._cache[key])
            perf.note_kv(key, 0, (time.perf_counter() - t0) * 1000)
            return value
        if key in ROW_KEYS:
            st = self._rows.get(key) or await self._rows_load(key)
            if st is not None:
                value = [marshal.loads(st["obj"][c]) for c in st["order"]]
                perf.note_kv(key, 0, (time.perf_counter() - t0) * 1000)
                return value
        packed = self._packed.get(key)
        if packed is not None:
            # every caller gets its own copy to change, as a fresh decode gave it, at about half the cost and without
            # a trip to the database
            value = marshal.loads(packed)
            perf.note_kv(key, 0, (time.perf_counter() - t0) * 1000)
            return value
        ver = self._ver.get(key, 0)
        row = await self.fetchone("SELECT value FROM kv WHERE key=?", (key,))
        if not row:
            return default
        value = json.loads(row["value"])
        if self._ver.get(key, 0) == ver:   # no write since the read began
            if key in CACHED:
                self._cache[key] = value
                value = _copy(value)
            elif len(row["value"]) <= PACK_MAX:
                try:
                    self._packed[key] = marshal.dumps(value)
                except ValueError:
                    pass
        elif key in CACHED:
            value = _copy(value)
        perf.note_kv(key, len(row["value"]), (time.perf_counter() - t0) * 1000)   # read + decode
        return value

    async def kv_derived(self, key: str, name: str, fn: Callable[[Any], Any], default: Any = None) -> Any:
        """fn(value of `key`), worked out once and shared until the key is next written. Callers must not change the
        result. For the hot readers of the star catalog (tens of thousands of stars, read every pass): copying it on each
        read and rebuilding the same lookups was most of an engine pass (2026-10-21)."""
        if key in ROW_KEYS and (key, name) not in self._derived:
            ver = self._ver.get(key, 0)
            out = fn(await self.kv_get(key, default))
            if self._ver.get(key, 0) != ver:
                return out
            self._derived[(key, name)] = out
        if (key, name) not in self._derived:
            ver = self._ver.get(key, 0)
            row = await self.fetchone("SELECT value FROM kv WHERE key=?", (key,)) if key not in self._cache else None
            src = self._cache[key] if key in self._cache else (json.loads(row["value"]) if row else default)
            if self._ver.get(key, 0) != ver:   # written while it was read: answer, but keep nothing
                return fn(src)
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
        self.touch_star(ev.get("star"))
        self.touch_star(ev.get("location"))
        if ev.get("event") == "scan.completed":
            self.gen["scans"] += 1
        return cur.rowcount > 0


def row_event(row: dict) -> dict:
    """DB row -> event dict with parsed payload."""
    out = dict(row)
    try:
        out["payload"] = json.loads(row.get("payload") or "{}")
    except (TypeError, ValueError):
        out["payload"] = {}
    return out
