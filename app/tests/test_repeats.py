"""Repeating console commands (Console › Repeating): sent every N minutes until stopped, finds counted."""
from rsweb import repeats as rp

from test_ops import H, HX, REP, client, eng  # noqa: F401  (fixture)


def _items(c):
    return c.portal.call(c.app.state.db.kv_get, rp.KV, [])


def test_repeat_sends_now_counts_finds_in_its_system_and_keeps_going(client):  # noqa: F811
    r = client.post("/console/repeat", data={"method": "POST", "path": f"/replicants/{REP}/scan", "body": "",
                                             "every_minutes": "5"}, headers=HX)
    assert r.headers.get("HX-Refresh") == "true"
    rep = _items(client)[0]
    assert rep["runs"] == 1 and rep["last_result"] == "ok" and rep["star"] == "SOL" and rep["enabled"]
    db = client.app.state.db
    client.portal.call(db.insert_event, {"id": "d1", "event": "device.discovered", "star": "SOL", "location": "SOL-BELT-1",
                                         "payload": {"device_type": "guidance_chip", "device_code": "ABCD1234"}})
    client.portal.call(db.insert_event, {"id": "d2", "event": "salvage.discovered", "location": "ABOTEIN-BELT-1",
                                         "payload": {"name": "wreck"}})
    client.portal.call(eng(client).run_due_repeats)
    rep = _items(client)[0]
    assert rep["finds"] == 1 and "guidance chip ABCD1234" in rep["last_find"] and rep["enabled"]
    assert rep["runs"] == 1   # not due yet
    page = client.get("/console", headers=H).text
    assert "Repeating" in page and "guidance chip ABCD1234" in page and "Stop" in page
    client.post(f"/console/repeats/{rep['id']}/run", headers=HX)
    assert _items(client)[0]["runs"] == 2
    client.post(f"/console/repeats/{rep['id']}/toggle", headers=HX)
    assert not _items(client)[0]["enabled"]
    client.post(f"/console/repeats/{rep['id']}/delete", headers=HX)
    assert _items(client) == []


def test_repeat_stops_at_first_find_when_asked_and_after_failed_sends(client):  # noqa: F811
    client.post("/console/repeat", data={"method": "POST", "path": f"/replicants/{REP}/scan", "body": "",
                                         "every_minutes": "5", "stop_on_find": "on"}, headers=HX)
    client.portal.call(client.app.state.db.insert_event, {"id": "d1", "event": "device.discovered", "location": "SOL-BELT-1",
                                                          "payload": {"device_type": "guidance_chip"}})
    client.portal.call(eng(client).run_due_repeats)
    assert not _items(client)[0]["enabled"]
    client.post("/console/repeat", data={"method": "POST", "path": "/devices/NOPE0000", "body": '{"command": "scan"}'}, headers=HX)
    bad = _items(client)[1]
    assert bad["errors"] == 1 and bad["last_result"].startswith("error")
    for _ in range(2):
        client.post(f"/console/repeats/{bad['id']}/run", headers=HX)
    assert not _items(client)[1]["enabled"]


def test_repeat_refuses_get(client):  # noqa: F811
    r = client.post("/console/repeat", data={"method": "GET", "path": "/accounts/me"}, headers=HX)
    assert "can repeat" in r.text and _items(client) == []
