import asyncio

from rsweb import perf
from rsweb.db import DB


def test_large_dictionaries_write_only_changed_entries_and_survive_a_restart(tmp_path):
    async def go():
        path = str(tmp_path / "t.sqlite")
        db = DB(path)
        await db.open()
        # a value saved whole by an older version is read as before and moved to rows on its first write
        await db.execute("INSERT INTO kv(key, value, updated_at) VALUES('surveyed', '{\"A-1\": \"t0\"}', 'x')")
        assert await db.kv_get("surveyed") == {"A-1": "t0"}
        big = {f"S-{i}": "2026-11-23T00:00:00+00:00" for i in range(5000)}
        await db.kv_set("surveyed", {**await db.kv_get("surveyed"), **big})
        perf.writes.pop("surveyed", None)
        v = await db.kv_get("surveyed")
        v["S-9"] = "new"
        await db.kv_set("surveyed", v)
        assert perf.writes["surveyed"][1] < 100          # one entry, not the whole dictionary
        v["S-9"] = "changed by the caller only"           # a caller's copy is its own
        others = {"stars": {"FOO": {"devices": [1, 2]}, "BAR": {"devices": []}}, "rep_at": {"a": "FOO"}, "lockers": {}}
        await db.kv_set("others", others)
        o = await db.kv_get("others")
        o["stars"]["FOO"]["devices"].append(3)
        del o["stars"]["BAR"]
        await db.kv_set("others", o)
        await db.close()

        db = DB(path)
        await db.open()
        s = await db.kv_get("surveyed")
        assert len(s) == 5001 and s["S-9"] == "new" and s["A-1"] == "t0"
        assert await db.kv_get("others") == {"stars": {"FOO": {"devices": [1, 2, 3]}}, "rep_at": {"a": "FOO"}, "lockers": {}}
        await db.execute("DELETE FROM kv WHERE key='others'")   # the mark row gone: the next write puts it back
        await db.kv_set("others", {"stars": {}, "rep_at": {}})
        await db.kv_set("surveyed", [1, 2])                     # not a dictionary: stored whole again
        await db.close()

        db = DB(path)
        await db.open()
        assert await db.kv_get("others") == {"stars": {}, "rep_at": {}}
        assert await db.kv_get("surveyed") == [1, 2]
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM kv_rows WHERE key='surveyed'"))["n"] == 0
        await db.close()
    asyncio.run(go())


def test_write_meter_reports_bytes_per_key():
    perf.note_write("meter-test", 2 * 1048576)
    perf.note_write("meter-test", 0)
    row = next(r for r in perf.writes_report(50)["rows"] if r["key"] == "meter-test")
    assert row["n"] == 2 and row["mb"] == 2.0
