import asyncio
from datetime import datetime, timedelta, timezone

from rsweb import history
from rsweb.db import DB

NOW = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)


def _iso(days, h=0):
    return (NOW - timedelta(days=days, hours=h)).isoformat(timespec="seconds")


async def _ev(db, i, event, days, device="AAAA0001"):
    await db.insert_event({"id": f"e{i}", "event": event, "device_code": device, "created_at": _iso(days)})
    await db.execute("UPDATE events SET received_at=? WHERE id=?", (_iso(days), f"e{i}"))


def test_old_events_go_but_kept_kinds_and_each_devices_newest_stay(tmp_path):
    async def go():
        db = DB(str(tmp_path / "t.sqlite"))
        await db.open()
        await _ev(db, 1, "travel.arrived", 40)
        await _ev(db, 2, "travel.arrived", 35)             # this device's newest arrival: stays
        await _ev(db, 3, "scan.completed", 40)             # read back for ever
        await _ev(db, 4, "ami.survey.digest", 40, "BBBB0002")
        await _ev(db, 10, "ami.survey.digest", 9, "BBBB0002")   # digests keep a week
        await _ev(db, 11, "ami.survey.digest", 3, "BBBB0002")
        await _ev(db, 5, "ami.survey.digest", 1, "BBBB0002")
        await _ev(db, 6, "ami.device_adopted", 40, "BBBB0002")
        await _ev(db, 7, "event.completed", 40)
        await _ev(db, 9, "print.completed", 45)
        await _ev(db, 8, "print.completed", 10)            # inside the month
        assert await history.prune_events(db, NOW) == (4, True)
        left = {r["id"] for r in await db.fetchall("SELECT id FROM events")}
        assert left == {"e2", "e3", "e5", "e6", "e7", "e8", "e11"}
        assert await history.prune_events(db, NOW) == (0, True)
        await db.close()
    asyncio.run(go())


def test_history_older_than_a_week_becomes_one_value_a_day(tmp_path):
    async def go():
        db = DB(str(tmp_path / "t.sqlite"))
        await db.open()
        for days in (10, 2):
            for h, qty in ((1, 5.0), (2, 7.0), (3, 9.0)):
                await db.execute("INSERT INTO inventory_history(ts, resource, qty) VALUES(?,?,?)", (_iso(days, -h), "iron", qty))
                await db.execute("INSERT INTO metric_history(ts, metric, value) VALUES(?,?,?)", (_iso(days, -h), "req_open", qty))
        for _ in range(2):   # a second run changes nothing
            await history.thin_history(db, NOW)
            inv = await db.fetchall("SELECT ts, qty FROM inventory_history ORDER BY ts")
            assert [r["qty"] for r in inv] == [9.0, 5.0, 7.0, 9.0]          # the old day's last reading, the recent day whole
            met = await db.fetchall("SELECT ts, value FROM metric_history ORDER BY ts")
            assert [r["value"] for r in met] == [7.0, 5.0, 7.0, 9.0]        # the old day's average
            assert met[0]["ts"] == "2026-11-02T12:00:00+00:00"
        await db.close()
    asyncio.run(go())


def test_daily_runs_once_a_day(tmp_path):
    async def go():
        db = DB(str(tmp_path / "t.sqlite"))
        await db.open()
        await _ev(db, 1, "travel.arrived", 40)
        await _ev(db, 2, "travel.arrived", 39)
        assert await history.daily(db, NOW) == 1
        assert await history.daily(db, NOW + timedelta(hours=3)) is None
        assert await history.daily(db, NOW + timedelta(hours=25)) == 0
        await db.close()
    asyncio.run(go())


def test_records_a_season_reset_wiped_go_at_once(tmp_path):
    async def go():
        from rsweb import season
        db = DB(str(tmp_path / "t.sqlite"))
        await db.open()
        await db.kv_set(season.RESET_KV, {"at": _iso(3), "number": 4})
        await _ev(db, 1, "scan.completed", 5)              # before the reset: wiped
        await _ev(db, 2, "event.discovered", 4)
        await _ev(db, 3, "system.object_detected", 4)
        await _ev(db, 4, "scan.completed", 1)              # after it: stays
        await _ev(db, 5, "bobnet.new", 5)                  # not wiped by a season
        await _ev(db, 6, "eventful.other", 5)              # only the event. prefix
        assert await history.prune_wiped(db) == (3, True)
        assert {r["id"] for r in await db.fetchall("SELECT id FROM events")} == {"e4", "e5", "e6"}
        await db.close()
    asyncio.run(go())


def test_compaction_runs_once_and_shrinks_the_file(tmp_path):
    async def go():
        import os
        db = DB(str(tmp_path / "t.sqlite"))
        await db.open()
        for i in range(400):
            await db.kv_set(f"big{i}", "x" * 20000)
        await db.execute("DELETE FROM kv WHERE key LIKE 'big%'")
        await db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = os.path.getsize(db.path)
        rec = await history.compact_once(db)
        assert rec["state"] == "done" and os.path.getsize(db.path) < before / 4
        assert await history.compact_once(db) is None
        await db.kv_set("after", 1)
        assert await db.kv_get("after") == 1
        await db.close()
    asyncio.run(go())


def test_compaction_tries_again_after_a_locked_database(tmp_path):
    async def go():
        from datetime import datetime, timedelta, timezone
        db = DB(str(tmp_path / "t.sqlite"))
        await db.open()
        for i in range(400):
            await db.kv_set(f"big{i}", "x" * 20000)
        await db.execute("DELETE FROM kv WHERE key LIKE 'big%'")
        old = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        await db.kv_set(history.COMPACT_KV, {"at": old, "state": "failed", "error": "database table is locked"})
        rec = await history.compact_once(db)
        assert rec["state"] == "done" and rec["tries"] == 2 and "error" not in rec
        await db.kv_set(history.COMPACT_KV, {"at": old, "state": "failed", "tries": history.COMPACT_TRIES})
        assert await history.compact_once(db) is None        # given up
        await db.kv_set(history.COMPACT_KV, {"at": datetime.now(timezone.utc).isoformat(), "state": "failed"})
        assert await history.compact_once(db) is None        # not within half an hour
        await db.close()
    asyncio.run(go())
