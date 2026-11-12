"""Keeping the database from growing forever (owner 2026-11-12: a month of events is enough, and older records can be
totals instead of exact values).

Once a day:
- events older than EVENT_DAYS are deleted, except the kinds the app reads back without a time limit (KEEP: scans,
  prospects, BobNet posts, location events, deals, hubs, relays, directives, asteroids, depleted sites, lost devices,
  blueprint unlocks, AMI adoptions) and the newest event of each kind per device (its last digest, trip, print ...);
- stockpile history older than THIN_DAYS keeps each day's last reading;
- backlog metrics older than THIN_DAYS keep one average per day (they are dropped after 30 days by metrics.py).
Deleting runs in batches, so a large first clean-up never holds the database long; it carries on at the next call.
The file itself doesn't shrink (SQLite reuses the freed pages)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

EVENT_DAYS = 30
THIN_DAYS = 7
BATCH = 4000
MAX_BATCHES = 15          # per call
STATE_KV = "history_pruned"

KEEP_EVENTS = ("scan.completed", "prospect.completed", "bobnet.new", "mining.resource_depleted", "site.depleted",
               "salvage.depleted", "salvage.discovered", "system.impact_warning", "device.decommissioned",
               "device.destroyed", "device.lost", "device.transferred", "device.owner_changed", "device.changed_owner",
               "device.discovered")
KEEP_PREFIXES = ("event.", "megastructure.", "swoop.", "hub.", "system.object_", "diversion.", "blueprint.", "trade.",
                 "contract.", "relay.", "directive.", "season.")
KEEP_SQL = ("(event IN (%s) OR %s OR (event LIKE 'ami.%%' AND event NOT LIKE '%%.digest'))"
            % (",".join("?" * len(KEEP_EVENTS)), " OR ".join("event LIKE ?" for _ in KEEP_PREFIXES)))
KEEP_ARGS = (*KEEP_EVENTS, *(p + "%" for p in KEEP_PREFIXES))


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


async def prune_events(db, now: datetime | None = None, max_batches: int = MAX_BATCHES) -> tuple[int, bool]:
    """(events deleted, finished). Not finished: more old events remain for the next call."""
    cutoff = _iso((now or datetime.now(timezone.utc)) - timedelta(days=EVENT_DAYS))
    await db.execute("CREATE TEMP TABLE IF NOT EXISTS prune_keep(seq INTEGER PRIMARY KEY)")
    await db.execute("DELETE FROM temp.prune_keep")
    await db.execute("INSERT OR IGNORE INTO temp.prune_keep SELECT MAX(seq) FROM events GROUP BY device_code, event")
    done = 0
    for _ in range(max_batches):
        cur = await db.execute(
            "DELETE FROM events WHERE seq IN (SELECT seq FROM events WHERE received_at < ? "
            f"AND NOT {KEEP_SQL} AND seq NOT IN (SELECT seq FROM temp.prune_keep) LIMIT ?)", (cutoff, *KEEP_ARGS, BATCH))
        n = cur.rowcount or 0
        done += n
        if n < BATCH:
            return done, True
        await asyncio.sleep(0.05)   # let pages and the engine at the database between batches
    return done, False


async def thin_history(db, now: datetime | None = None) -> None:
    """Stockpile history and backlog metrics older than THIN_DAYS: one value per day (whole UTC days only, so a day is
    never summed up twice from a part of it)."""
    day = ((now or datetime.now(timezone.utc)) - timedelta(days=THIN_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
    cut = _iso(day)
    await db.execute("DELETE FROM inventory_history WHERE ts < ? AND ts NOT IN "
                     "(SELECT MAX(ts) FROM inventory_history WHERE ts < ? GROUP BY substr(ts, 1, 10), resource)", (cut, cut))
    noon = "T12:00:00+00:00"
    await db.execute("INSERT OR REPLACE INTO metric_history(ts, metric, value) "
                     "SELECT substr(ts, 1, 10) || ?, metric, AVG(value) FROM metric_history WHERE ts < ? "
                     "GROUP BY substr(ts, 1, 10), metric", (noon, cut))
    await db.execute("DELETE FROM metric_history WHERE ts < ? AND substr(ts, 11) != ?", (cut, noon))


async def daily(db, now: datetime | None = None) -> int | None:
    """Run the clean-up when a day has passed since the last finished one (or one is unfinished). Returns the events
    deleted, None when it wasn't due."""
    now = now or datetime.now(timezone.utc)
    st = await db.kv_get(STATE_KV, {}) or {}
    try:
        last = datetime.fromisoformat(st["at"]) if st.get("at") else None
    except (TypeError, ValueError):
        last = None
    if last and not st.get("more") and (now - last).total_seconds() < 86400:
        return None
    if not st.get("more"):
        await thin_history(db, now)
    n, finished = await prune_events(db, now)
    await db.kv_set(STATE_KV, {"at": _iso(now), "more": not finished, "deleted": n + (st.get("deleted") or 0 if st.get("more") else 0)})
    return n
