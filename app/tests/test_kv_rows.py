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


def test_jobs_and_log_store_one_row_per_item(tmp_path):
    async def go():
        path = str(tmp_path / "t.sqlite")
        db = DB(path)
        await db.open()
        jobs = [{"id": f"j{i}", "status": "running", "steps": ["x" * 2000]} for i in range(50)]
        await db.kv_set("automation_jobs", jobs)
        log = [{"at": "t", "rule": "r", "text": "same"}] * 3 + [{"at": f"t{i}", "rule": "r", "text": "x" * 500} for i in range(200)]
        await db.kv_set("automation_log", log)
        perf.writes.pop("automation_jobs", None)
        perf.writes.pop("automation_log", None)
        jobs = await db.kv_get("automation_jobs")
        jobs[7]["status"] = "done"
        await db.kv_set("automation_jobs", jobs)
        log = (await db.kv_get("automation_log"))[1:] + [{"at": "new", "rule": "r", "text": "y"}]
        await db.kv_set("automation_log", log)
        assert perf.writes["automation_jobs"][1] < 8000      # one job and the order, not fifty jobs
        assert perf.writes["automation_log"][1] < 10000       # one line and the order, not 100 KB
        await db.close()
        db = DB(path)
        await db.open()
        assert (await db.kv_get("automation_jobs"))[7]["status"] == "done"
        got = await db.kv_get("automation_log")
        assert got == log and got[0] == got[1] == {"at": "t", "rule": "r", "text": "same"}
        await db.close()
    asyncio.run(go())


def test_star_catalog_stored_by_star_keeps_its_order_and_cache(tmp_path):
    async def go():
        path = str(tmp_path / "t.sqlite")
        db = DB(path)
        await db.open()
        cat = {"stars": [{"designation": f"S{i}", "x": i, "planets": [{"n": 1}]} for i in range(3000)],
               "sources": {"catalogue": 3000}}
        await db.kv_set("stars", cat)
        perf.writes.pop("stars", None)
        c = await db.kv_get("stars")
        c["stars"].append({"designation": "NEW", "x": -1})
        c["stars"][5]["entry_point"] = "S5-1-L4"
        await db.kv_set("stars", c)
        assert perf.writes["stars"][1] < 500             # two stars, not three thousand
        c2 = await db.kv_get("stars")
        c2["stars"][0]["x"] = 99                          # a caller's copy is its own
        assert (await db.kv_get("stars"))["stars"][0]["x"] == 0
        await db.close()
        db = DB(path)
        await db.open()
        got = await db.kv_get("stars")
        assert [s["designation"] for s in got["stars"][:2]] == ["S0", "S1"] and got["stars"][-1]["designation"] == "NEW"
        assert got["stars"][5]["entry_point"] == "S5-1-L4" and got["sources"] == {"catalogue": 3000}
        got["stars"][1]["x"] = 7                          # each star dict is the caller's own (CACHED contract)
        assert (await db.kv_get("stars"))["stars"][1]["x"] == 1
        dup = {"stars": [{"designation": "A"}, {"designation": "A"}]}   # duplicate names: stored whole
        await db.kv_set("stars", dup)
        await db.close()
        db = DB(path)
        await db.open()
        assert await db.kv_get("stars") == dup
        await db.close()
    asyncio.run(go())


def test_system_columns_follow_every_survey_write_and_older_tables_are_filled(tmp_path):
    import json
    import sqlite3

    from rsweb import web_core
    path = str(tmp_path / "s.sqlite")
    old = sqlite3.connect(path)   # a systems table from before 1.541.0: no columns besides the survey
    old.execute("CREATE TABLE systems (star TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL)")
    old.execute("INSERT INTO systems VALUES('OLD', ?, '2026-11-24T00:00:00+00:00')",
                (json.dumps({"star": {"radius_solar": 1.5}, "system_tags": ["hazardous_radiation"]}),))
    old.commit()
    old.close()

    async def go():
        db = DB(path)
        await db.open()
        row = await db.fetchone("SELECT radius, tags, shops, megas FROM systems WHERE star='OLD'")
        assert (row["radius"], json.loads(row["tags"]), row["shops"], row["megas"]) == (1.5, ["hazardous_radiation"], 0, 0)
        scan = {"star": {}, "shops": [{"controller_code": "C1"}],
                "system_objects": [{"object_type": "megastructure", "location": "NEW-OBJ-1"}]}
        await db.execute("INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                         ("NEW", json.dumps(scan), "2026-11-24T00:00:00+00:00"))
        row = await db.fetchone("SELECT radius, tags, shops, megas FROM systems WHERE star='NEW'")
        assert (row["radius"], row["tags"], row["shops"], row["megas"]) == (None, "[]", 1, 1)
        await db.execute("UPDATE systems SET data=? WHERE star='NEW'", (json.dumps({"system_tags": ["resource_rich"]}),))
        row = await db.fetchone("SELECT tags, shops, megas FROM systems WHERE star='NEW'")
        assert (json.loads(row["tags"]), row["shops"], row["megas"]) == (["resource_rich"], 0, 0)
        await db.execute("INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES('BAD', 'not json', 'x')")
        assert (await db.fetchone("SELECT tags FROM systems WHERE star='BAD'"))["tags"] == "[]"
        # the map's summary is kept for a while: a new survey shows on the map at the next refresh
        web_core._map_scans.clear()
        scanned, radii, hazards, tags = await web_core.map_scan_summary(db)
        assert {"OLD", "NEW"} <= scanned and radii == {"OLD": 1.5} and hazards == {"OLD": ["hazardous_radiation"]}
        await db.execute("INSERT INTO systems(star, data, updated_at) VALUES('LATER', '{}', '2026-11-24T01:00:00+00:00')")
        assert "LATER" not in (await web_core.map_scan_summary(db))[0]
        web_core._map_scans["at"] -= web_core.MAP_SCAN_SECONDS
        assert "LATER" in (await web_core.map_scan_summary(db))[0]
        await db.close()
    asyncio.run(go())


def test_merge_and_row_patches_write_only_their_entries_and_match_a_full_write(tmp_path):
    """1.560.0: a scan adds one surveyed entry, an event changes one device, without the whole value."""
    async def go():
        path = str(tmp_path / "t.sqlite")
        db = DB(path)
        await db.open()
        await db.kv_set("surveyed", {f"S-{i}": "t0" for i in range(3000)})
        await db.kv_get("surveyed")
        perf.writes.pop("surveyed", None)
        await db.kv_merge("surveyed", {"S-5": "t1", "NEW-1": "t1"})
        await db.kv_merge("surveyed", {"S-6": "t9", "NEW-2": "t2"}, keep=True)
        assert perf.writes["surveyed"][1] < 100
        s = await db.kv_get("surveyed")
        assert len(s) == 3002 and s["S-5"] == "t1" and s["S-6"] == "t0" and s["NEW-2"] == "t2"
        assert list(s)[-2:] == ["NEW-1", "NEW-2"]

        devs = [{"device_code": f"D{i:04d}", "status": "idle", "tags": []} for i in range(2000)]
        devs[3]["_unconfirmed"] = {"at": "x"}
        await db.kv_set("devices", devs)
        await db.kv_get("devices")
        perf.writes.pop("devices", None)
        await db.kv_patch_rows("devices", [{"device_code": "D0003", "status": "travelling", "-_unconfirmed": 1},
                                           {"device_code": "NEW9", "status": "idle"}])
        assert perf.writes["devices"][1] < 30000
        assert (await db.kv_row("devices", "D0003")) == {"device_code": "D0003", "status": "travelling", "tags": []}
        d = await db.kv_get("devices")
        assert len(d) == 2001 and d[-1]["device_code"] == "NEW9" and d[3]["status"] == "travelling"
        ver = db.kv_version("devices")
        await db.kv_patch_rows("devices", [{"device_code": "D0003", "status": "travelling"}])   # no change
        assert db.kv_version("devices") == ver
        await db.close()

        db = DB(path)
        await db.open()
        assert len(await db.kv_get("surveyed")) == 3002
        d = await db.kv_get("devices")
        assert len(d) == 2001 and d[-1]["device_code"] == "NEW9" and "_unconfirmed" not in d[3]
        await db.close()
    asyncio.run(go())
