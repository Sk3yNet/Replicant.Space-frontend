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
