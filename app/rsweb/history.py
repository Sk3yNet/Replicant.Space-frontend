"""Keeping the database from growing forever (owner 2026-11-12: a month of events is enough, and older records can be
totals instead of exact values).

Once a day:
- AMI digests, experience gains and scan / search starts older than SHORT_DAYS are deleted (each device's newest stays);
- events older than EVENT_DAYS are deleted, except the kinds the app reads back without a time limit (KEEP: scans,
  prospects, BobNet posts, location events, deals, hubs, relays, directives, asteroids, depleted sites, lost devices,
  blueprint unlocks, AMI adoptions) and the newest event of each kind per device (its last digest, trip, print ...);
- stockpile history older than THIN_DAYS keeps each day's last reading;
- backlog metrics older than THIN_DAYS keep one average per day (they are dropped after 30 days by metrics.py).
Deleting runs in batches, so a large first clean-up never holds the database long; it carries on at the next call.
Events of kinds a season reset wipes (WIPED_*) go as soon as they predate the reset.
The file itself doesn't shrink (SQLite reuses the freed pages)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

EVENT_DAYS = 30
THIN_DAYS = 7
BATCH = 4000
MAX_BATCHES = 15          # per call
STATE_KV = "history_pruned"
RULES = 3                 # raise when the rules change, so the clean-up runs again without waiting a day

KEEP_EVENTS = ("scan.completed", "prospect.completed", "bobnet.new", "mining.resource_depleted", "site.depleted",
               "salvage.depleted", "salvage.discovered", "system.impact_warning", "device.decommissioned",
               "device.destroyed", "device.lost", "device.transferred", "device.owner_changed", "device.changed_owner",
               "device.discovered")
KEEP_PREFIXES = ("event.", "megastructure.", "swoop.", "hub.", "system.object_", "diversion.", "blueprint.", "trade.",
                 "contract.", "relay.", "directive.", "season.")
KEEP_SQL = ("(event IN (%s) OR %s OR (event LIKE 'ami.%%' AND event NOT LIKE '%%.digest'))"
            % (",".join("?" * len(KEEP_EVENTS)), " OR ".join("event LIKE ?" for _ in KEEP_PREFIXES)))
KEEP_ARGS = (*KEEP_EVENTS, *(p + "%" for p in KEEP_PREFIXES))


# High-volume kinds nothing reads back beyond a visit's digest: kept SHORT_DAYS (the 2026-11-13 snapshot: AMI digests
# were 230,000 of 539,000 events, experience gains 83,000). Each device's newest one still stays.
SHORT_DAYS = 7
SHORT_SQL = "(event LIKE 'ami.%.digest' OR event IN ('experience.gained', 'scan.started', 'search.started'))"
SHORT_ARGS: tuple = ()


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


async def prune_events(db, now: datetime | None = None, max_batches: int = MAX_BATCHES) -> tuple[int, bool]:
    """(events deleted, finished). Not finished: more old events remain for the next call."""
    cutoff = _iso((now or datetime.now(timezone.utc)) - timedelta(days=EVENT_DAYS))
    await db.execute("CREATE TEMP TABLE IF NOT EXISTS prune_keep(seq INTEGER PRIMARY KEY)")
    await db.execute("DELETE FROM temp.prune_keep")
    await db.execute("INSERT OR IGNORE INTO temp.prune_keep SELECT MAX(seq) FROM events GROUP BY device_code, event")
    short = _iso((now or datetime.now(timezone.utc)) - timedelta(days=SHORT_DAYS))
    done, left = 0, max_batches
    for cut, cond, args in ((cutoff, f"NOT {KEEP_SQL}", KEEP_ARGS), (short, SHORT_SQL, SHORT_ARGS)):
        while True:
            if left <= 0:
                return done, False
            left -= 1
            cur = await db.execute(
                f"DELETE FROM events WHERE seq IN (SELECT seq FROM events WHERE received_at < ? AND {cond} "
                "AND seq NOT IN (SELECT seq FROM temp.prune_keep) LIMIT ?)", (cut, *args, BATCH))
            n = cur.rowcount or 0
            done += n
            if n < BATCH:
                break
            await asyncio.sleep(0.05)   # let pages and the engine at the database between batches
    return done, True


# What a season reset wipes or ends (owner 2026-11-13: those records can go too; game docs and season.py: systems become
# unexplored, so scans, salvage and site finds start over; contracts and location events end; asteroids drift away).
# The readers of these kinds already skip anything dated before the reset.
WIPED_EVENTS = ("scan.completed", "salvage.discovered", "salvage.depleted", "site.depleted", "system.impact_warning")
WIPED_PREFIXES = ("event.", "contract.", "system.object_", "diversion.")


async def prune_wiped(db, max_batches: int = MAX_BATCHES) -> tuple[int, bool]:
    """Delete the events of the kinds above that were stored before the last season reset. (deleted, finished)."""
    from . import season
    reset = await season.reset_at(db)
    if not reset:
        return 0, True
    where = "(event IN (%s) OR %s)" % (",".join("?" * len(WIPED_EVENTS)),
                                        " OR ".join("(event >= ? AND event < ?)" for _ in WIPED_PREFIXES))
    args = [*WIPED_EVENTS]
    for p in WIPED_PREFIXES:
        args += [p, p[:-1] + chr(ord(p[-1]) + 1)]
    done = 0
    for _ in range(max_batches):
        cur = await db.execute(f"DELETE FROM events WHERE seq IN (SELECT seq FROM events WHERE {where} "
                               "AND received_at < ? LIMIT ?)", (*args, str(reset), BATCH))
        n = cur.rowcount or 0
        done += n
        if n < BATCH:
            return done, True
        await asyncio.sleep(0.05)
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


COMPACT_KV = "history_compacted"
COMPACT_TRIES = 8


async def compact_once(db) -> dict | None:
    """Give the space the clean-up freed back to the disk, once (owner 2026-11-13): SQLite reuses freed pages but never
    shrinks the file by itself. VACUUM rewrites the database, holding it for about a minute at this size; it is tried
    up to COMPACT_TRIES times, half an hour apart at least (each attempt is recorded first, so a restart in the middle
    counts as one)."""
    import asyncio
    import os
    last = await db.kv_get(COMPACT_KV, None) or {}
    tries = int(last.get("tries") or 1) if last else 0
    if last.get("state") in ("done", "not needed") or tries >= COMPACT_TRIES:
        return None
    try:
        if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last["at"])).total_seconds() < 1800:
            return None
    except (KeyError, TypeError, ValueError):
        pass
    free = (await db.fetchone("PRAGMA freelist_count") or {}).get("freelist_count") or 0
    pages = (await db.fetchone("PRAGMA page_count") or {}).get("page_count") or 1
    before = round(os.path.getsize(db.path) / 1048576, 1) if os.path.exists(db.path) else None
    rec = {"at": _iso(datetime.now(timezone.utc)), "before_mb": before, "free_pct": round(100 * free / pages),
           "tries": tries + 1}
    await db.kv_set(COMPACT_KV, {**rec, "state": "started"})
    if free / pages < 0.2:
        rec["state"] = "not needed"
    else:
        # VACUUM is refused ("database table is locked", live 2026-11-13) while any other statement on the connection
        # is half read, which under load is most of the time: a few tries seconds apart, and again on later days
        for i in range(6):
            try:
                await db.conn.commit()
                await db.conn.execute("VACUUM")
                await db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                rec.update(state="done", after_mb=round(os.path.getsize(db.path) / 1048576, 1))
                rec.pop("error", None)
                break
            except Exception as e:   # no room on the disk, or the database busy: left as it is
                rec.update(state="failed", error=str(e)[:200])
                if "lock" not in str(e).lower():
                    break
                await asyncio.sleep(2)
    await db.kv_set(COMPACT_KV, rec)
    return rec


async def daily(db, now: datetime | None = None) -> int | None:
    """Run the clean-up when a day has passed since the last finished one (or one is unfinished). Returns the events
    deleted, None when it wasn't due."""
    now = now or datetime.now(timezone.utc)
    st = await db.kv_get(STATE_KV, {}) or {}
    try:
        last = datetime.fromisoformat(st["at"]) if st.get("at") else None
    except (TypeError, ValueError):
        last = None
    if last and not st.get("more") and st.get("rules") == RULES and (now - last).total_seconds() < 86400:
        await compact_once(db)   # after a finished clean-up
        return None
    if not st.get("more"):
        await thin_history(db, now)
    w, wiped_done = await prune_wiped(db)
    n, finished = await prune_events(db, now)
    n, finished = n + w, finished and wiped_done
    if finished:
        try:   # the write-ahead log only shrinks when it is emptied on purpose (it had grown to 139 MB, 2026-11-13)
            await db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
    await db.kv_set(STATE_KV, {"at": _iso(now), "more": not finished, "rules": RULES, "deleted": n + (st.get("deleted") or 0 if st.get("more") else 0)})
    return n
