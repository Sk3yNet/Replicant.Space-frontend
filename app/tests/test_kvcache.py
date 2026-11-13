import asyncio

from rsweb import relaynet
from rsweb.db import DB


def test_saved_values_come_back_as_private_copies_and_follow_writes(tmp_path):
    async def go():
        db = DB(str(tmp_path / "t.sqlite"))
        await db.open()
        await db.kv_set("k", {"a": [1, {"b": 2}], "t": (1, 2)})
        first = await db.kv_get("k")
        assert first == {"a": [1, {"b": 2}], "t": [1, 2]}       # as JSON gives it back
        first["a"][1]["b"] = 99                                  # a caller changing its copy
        assert (await db.kv_get("k"))["a"][1]["b"] == 2          # ... changes nobody else's
        await db.kv_set("k", {"a": []})
        assert await db.kv_get("k") == {"a": []}
        await db.close()
    asyncio.run(go())


def test_unchanged_write_only_moves_the_timestamp_and_survives_a_deleted_row(tmp_path):
    async def go():
        db = DB(str(tmp_path / "t.sqlite"))
        await db.open()
        await db.kv_set("k", {"a": 1})
        await db.execute("UPDATE kv SET updated_at='old' WHERE key='k'")
        await db.kv_set("k", {"a": 1})
        assert await db.kv_updated("k") != "old" and await db.kv_get("k") == {"a": 1}
        await db.execute("DELETE FROM kv WHERE key='k'")
        await db.kv_set("k", {"a": 1})                           # the row is gone: written in full again
        assert await db.kv_get("k") == {"a": 1}
        await db.close()
    asyncio.run(go())


def test_networks_link_across_cells_and_answer_with_fresh_records():
    pos = {"A": {"x": 0, "y": 0, "z": 0}, "B": {"x": 7.4, "y": 0, "z": 0}, "C": {"x": 21, "y": 0, "z": 0},
           "D": {"x": 40, "y": 0, "z": 0}}
    devs = [{"device_code": s, "device_type": "system_hub" if s == "C" else "ftl_relay", "status": "relaying",
             "location": f"{s}-1-L4"} for s in "ABCD"]
    net = relaynet.networks(devs, pos)
    assert [(l["a"], l["b"]) for l in net["links"]] == [("A", "B"), ("B", "C")]   # the hub reaches 15 ly, D is alone
    assert [g["stars"] for g in net["networks"]] == [["A", "B", "C"], ["D"]]
    net["links"][0]["net"] = "changed"
    assert relaynet.networks(devs, pos)["links"][0]["net"] == 0


def test_a_long_retry_after_holds_only_that_endpoint():
    import httpx
    import pytest
    from rsweb.api import ApiError, RSClient
    from rsweb.config import Settings
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/trades"):
            return httpx.Response(429, headers={"Retry-After": "3600"}, json={"error": "hourly limit"})
        return httpx.Response(200, json={"ok": True})

    async def go():
        c = RSClient(Settings(api_token="t", api_base="http://x/v1"), transport=httpx.MockTransport(handler))
        with pytest.raises(ApiError) as e:
            await c.post("/trades", {})
        assert e.value.status == 429 and "3600 s" in e.value.message
        with pytest.raises(ApiError) as e2:
            await c.post("/trades", {})                      # refused here, not sent again
        assert "refused for another" in e2.value.message and calls.count("/v1/trades") == 1
        assert await c.post("/devices/AAAA0001", {"command": "deploy"}) == {"ok": True}   # other commands go on
        assert c.act_bucket.wait_time() <= 0 and c.rate_status()["endpoints_held"][0]["path"] == "/trades"
        await c.close()
    asyncio.run(go())


def test_device_list_is_stored_by_row_and_reads_back_the_same(tmp_path):
    async def go():
        path = str(tmp_path / "t.sqlite")
        db = DB(path)
        await db.open()
        await db.execute("INSERT INTO kv(key, value, updated_at) VALUES('devices', ?, 'old')",
                         ('[{"device_code": "A1", "status": "idle"}, {"device_code": "B2", "status": "idle"}]',))
        assert [d["device_code"] for d in await db.kv_get("devices")] == ["A1", "B2"]   # the old single value still reads
        devs = await db.kv_get("devices")
        devs[1]["status"] = "mining"
        devs.append({"device_code": "C3", "tags": ("x",)})
        await db.kv_set("devices", devs)
        devs[0]["status"] = "changed after saving"                      # the caller's list is its own
        got = await db.kv_get("devices")
        assert got == [{"device_code": "A1", "status": "idle"}, {"device_code": "B2", "status": "mining"},
                       {"device_code": "C3", "tags": ["x"]}]
        got[0]["status"] = "x"
        assert (await db.kv_get("devices"))[0]["status"] == "idle"
        rows = await db.fetchall("SELECT code FROM device_rows ORDER BY code")
        assert [r["code"] for r in rows] == ["A1", "B2", "C3"]
        await db.kv_set("devices", [got[2], got[1]])                    # one gone, order changed
        assert await db.kv_updated("devices") != "old"
        await db.close()
        db2 = DB(path)                                                  # a restart reads the rows back, in order
        await db2.open()
        assert [d["device_code"] for d in await db2.kv_get("devices")] == ["C3", "B2"]
        assert await db2.kv_derived("devices", "n", len) == 2
        await db2.kv_set("devices", [])                                 # not a list of devices: one value again
        assert await db2.kv_get("devices") == [] and not await db2.fetchall("SELECT code FROM device_rows")
        await db2.close()
    asyncio.run(go())


def test_star_catalog_stays_in_memory_across_writes(tmp_path, monkeypatch):
    async def go():
        db = DB(str(tmp_path / "t.sqlite"))
        await db.open()
        await db.kv_set("stars", {"stars": [{"designation": "SOL"}]})
        seen = []
        real = db.fetchone

        async def spy(sql, params=()):
            seen.append(sql)
            return await real(sql, params)
        monkeypatch.setattr(db, "fetchone", spy)
        got = await db.kv_get("stars")
        assert got == {"stars": [{"designation": "SOL"}]} and not seen     # no read, no decode
        got["stars"][0]["explored"] = True                                  # a reader's copy is its own
        assert "explored" not in (await db.kv_get("stars"))["stars"][0]
        await db.kv_set("stars", got)
        assert (await db.kv_get("stars"))["stars"][0]["explored"] and not seen
        await db.close()
        db2 = DB(str(tmp_path / "t.sqlite"))
        await db2.open()
        assert (await db2.kv_get("stars"))["stars"][0]["explored"]          # and it was saved
        await db2.close()
    asyncio.run(go())


def test_device_list_reads_match_after_repeated_reads_and_writes(tmp_path):
    async def go():
        db = DB(str(tmp_path / "t.sqlite"))
        await db.open()
        devs = [{"device_code": f"D{i}", "status": "idle"} for i in range(5)]
        await db.kv_set("devices", devs)
        for _ in range(3):                                  # the third read comes from the single blob
            got = await db.kv_get("devices")
            assert got == devs
        got[0]["status"] = "changed by a reader"
        assert (await db.kv_get("devices"))[0]["status"] == "idle"
        await db.kv_set("devices", devs)                    # unchanged write
        assert await db.kv_get("devices") == devs
        devs[2]["status"] = "mining"
        await db.kv_set("devices", devs[:4])
        for _ in range(3):
            assert await db.kv_get("devices") == devs[:4]
        await db.close()
    asyncio.run(go())
