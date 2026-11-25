"""Information beacons (game v4.1.0): description, broadcast, comments, scans and the device.commented notification."""
from rsweb import infobeacons as ib
from rsweb import notify
from tests.test_ops import H, HX, add, client, world  # noqa: F401  (client is a fixture)


def _beacon(c, code="IB44C2E1", loc="SOL-3-L4", **kw):
    kw.setdefault("available_commands", ["activate", "comment", "stow", "decommission"])
    add(c, device_code=code, device_type=ib.TYPE, location=loc, status="idle", features=[], **kw)
    return next(d for d in world(c).devices if d["device_code"] == code)


def test_settings_and_comment_bodies_check_the_limits():
    assert ib.settings_body("hi\r\nthere", False) == {"configuration": {"settings": {"description": "hi\nthere", "comments": False}}}
    for f, arg in ((ib.settings_body, "x" * 2001), (ib.comment_body, "x" * 2001), (ib.comment_body, "  ")):
        try:
            f(arg)
            assert False, "no error"
        except ValueError:
            pass
    assert ib.comment_body(" yo ") == {"command": "comment", "message": "yo"}


def test_scan_info_reads_any_shape():
    assert [e["text"] for e in ib.scan_info({"info": ["Hello", {"device_code": "IB1", "description": "Waypoint 7"}]})] \
        == ["Hello", "Waypoint 7"]
    assert ib.scan_info({"info": {"beacons": [{"message": "m", "location": "SOL-3"}]}})[0]["location"] == "SOL-3"
    assert ib.scan_info({"info": {"IB2": {"description": "d"}}})[0]["code"] == "IB2"
    assert ib.scan_info({"star": {}}) == []


def test_owner_sets_description_broadcasts_and_moderates(client):
    d = _beacon(client)
    page = client.get(f"/devices/{d['device_code']}", headers=H).text
    assert "Information beacon" in page and "Start broadcasting" in page and "allow comments" in page
    r = client.post("/info-beacons/IB44C2E1/settings", data={"description": "Waypoint 7-alpha", "comments": "1"}, headers=HX).text
    assert "Waypoint 7-alpha" in r and d["settings"]["description"] == "Waypoint 7-alpha"
    r = client.post("/info-beacons/IB44C2E1/switch", data={"command": "activate"}, headers=HX).text
    assert d["settings"]["active"] and "broadcasting" in r
    r = client.post("/info-beacons/IB44C2E1/comment", data={"message": "Confirmed - I am awesome."}, headers=HX).text
    assert "I am awesome" in r and len(d["comments"]) == 1
    r = client.post("/info-beacons/IB44C2E1/comment", data={"message": "x" * 2001}, headers=HX).text
    assert "at most 2000" in r and len(d["comments"]) == 1
    r = client.post("/info-beacons/IB44C2E1/comments/0/delete", headers=HX).text
    assert d["comments"] == [] and "comment_deleted" in r
    client.post("/info-beacons/IB44C2E1/settings", data={"description": "closed"}, headers=HX)
    assert d["settings"]["comments"] is False
    page = client.get("/info-beacons", headers=H).text
    assert "IB44C2E1" in page and "closed" in page   # the last read copy is kept for the list


def test_others_beacons_from_scans_and_system_page(client):
    from rsweb import others as oth
    s = oth.normalize({})
    oth.record_scan(s, "SOL", [{"device_code": "IBF00001", "device_type": ib.TYPE, "location": "SOL-OORT",
                                "owner_replicant_code": "BBBB0002", "owner_name": "Riker"}], set())
    assert s["info_beacons"]["IBF00001"]["owner_name"] == "Riker"
    client.portal.call(client.app.state.db.kv_set, oth.KV, s)
    assert "Riker" in client.get("/info-beacons", headers=H).text
    assert "IBF00001" in client.get("/systems/SOL", headers=H).text


def test_commented_event_notifies_owner():
    ev = {"event": "device.commented", "device_code": "IB44C2E1", "location": "SOL-3-L4",
          "payload": {"replicant_name": "Explorer-7", "comment": "Confirmed"}}
    n = notify.notification_for(ev)
    assert n["level"] == "info" and n["link"] == "/devices/IB44C2E1"
    assert "Explorer-7" in n["title"] and "Confirmed" in n["title"] and "SOL-3-L4" in n["title"]


def test_due_reads_and_target_steps():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    seen = {"IB1": {"location": "SOL-OORT"}, "IB2": {"location": "SOL-3"}, "IB3": {"location": "SOL-4"},
            "IB4": {"location": "TAU-3"}}
    store = {"IB2": {"read_at": (now - timedelta(hours=2)).isoformat()},
             "IB3": {"read_at": (now - timedelta(hours=30)).isoformat()}}
    assert ib.due_reads(seen, store, "SOL", now) == ["IB1", "IB3"]
    t = {"dest": "SOL-3", "activate": True}
    assert ib.target_step(None, t) == "gone"
    d = {"location": "SOL-3", "tags": ["at:sol-3"], "available_commands": ["deploy"]}
    assert ib.target_step(d, t) == "wait"   # logistics still has it
    d["tags"] = []
    assert ib.target_step(d, t) == "deploy"
    d["available_commands"] = ["activate"]
    assert ib.target_step(d, t) == "activate"
    assert ib.target_step(d, {"dest": "SOL-3"}) == "done"
    assert ib.target_step({**d, "location": "SOL-4"}, {"dest": "SOL", "activate": True}) == "activate"   # a star: anywhere in it
    assert ib.target_step({**d, "location": "SOL-4"}, t) == "wait"


def test_device_scan_reads_new_info_beacons(client):
    from rsweb import others as oth

    class Api:
        calls = []

        async def request(self, method, path, params=None, background=False):
            return {"star": "SOL", "devices": [{"device_code": "IBF00001", "device_type": ib.TYPE, "location": "SOL-OORT",
                                                "owner_replicant_code": "BBBB0002", "owner_name": "Riker"}]}

        async def get(self, path, background=False, **kw):
            self.calls.append(path)
            return {"device_code": "IBF00001", "settings": {"description": "Fuel at SOL-OORT"}, "comments": []}

    db = client.app.state.db
    api = Api()
    snap = client.portal.call(oth.scan_into, db, api, "REP", set())
    assert snap["info_read"] == ["IBF00001"] and api.calls == ["/devices/IBF00001"]
    assert "Fuel at SOL-OORT" in client.get("/info-beacons", headers=H).text
    snap = client.portal.call(oth.scan_into, db, api, "REP", set())
    assert snap["info_read"] == [] and len(api.calls) == 1   # read a moment ago: not again


def test_deploy_location_carries_deploys_and_broadcasts(client):
    from tests.test_ops import eng
    d = _beacon(client, code="IB44C2E2", loc="SOL-KUIPER", available_commands=["deploy", "stow"])
    r = client.post("/info-beacons/IB44C2E2/deploy-at", data={"destination": "sol-3", "activate": "1"}, headers=HX).text
    assert "Deploy location SOL-3 set" in r and "at:sol-3" in [t.lower() for t in d.get("tags", [])], r
    targets = client.portal.call(client.app.state.db.kv_get, ib.TARGETS_KV, {})
    assert targets["IB44C2E2"]["dest"] == "SOL-3" and targets["IB44C2E2"]["activate"]
    assert "→ SOL-3" in client.get("/info-beacons", headers=H).text
    e = eng(client)
    assert client.portal.call(e.info_beacon_pass) == []   # still on its way
    d.update(location="SOL-3", tags=[])                    # a carrier dropped it there; the tags went on arrival
    client.portal.call(client.app.state.worker.sync_devices)
    out = client.portal.call(e.info_beacon_pass)
    assert out and "deploy at SOL-3" in out[0]
    d["available_commands"] = ["activate", "comment", "stow"]
    client.portal.call(client.app.state.worker.sync_devices)
    out = client.portal.call(e.info_beacon_pass)
    assert "activate at SOL-3" in out[0] and d["settings"]["active"]
    d["available_commands"] = ["deactivate", "comment", "stow"]
    client.portal.call(client.app.state.worker.sync_devices)
    out = client.portal.call(e.info_beacon_pass)
    assert "in place at SOL-3 and broadcasting" in out[0]
    assert client.portal.call(client.app.state.db.kv_get, ib.TARGETS_KV, {}) == {}
    r = client.post("/info-beacons/IB44C2E2/deploy-at", data={"destination": "nowhere!"}, headers=HX).text
    assert "isn&#39;t a star" in r
