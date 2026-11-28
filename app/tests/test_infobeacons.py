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


def test_info_beacons_never_count_as_traffic_beacons():
    """Joe 2026-11-25: an information beacon at RADYR-OORT was pinned to the civilization's body like an FTL beacon."""
    from rsweb import traffic
    info = {"device_code": "B9BA1A7B", "device_type": "information_beacon", "features": ["beacon", "stow"],
            "location": "RADYR-OORT", "status": "monitoring", "tags": ["spare"]}
    ftl = {"device_code": "F1", "device_type": "ftl_beacon", "location": "RADYR-3-L4", "status": "monitoring"}
    assert not traffic.is_beacon(info) and traffic.is_beacon(ftl)
    assert [d["device_code"] for d in traffic.beacons([info, ftl])] == ["F1"]
    assert [d["device_code"] for d in traffic.beacons_all([info, ftl])] == ["F1"]


def test_bulk_import_assigns_tags_describes_and_broadcasts(client):
    import json
    from tests.test_ops import eng
    a = _beacon(client, code="IB0000A1", loc="SOL-KUIPER", available_commands=["deploy", "stow"])
    _beacon(client, code="IB0000A2", loc="SOL-KUIPER", available_commands=["deploy", "stow"])
    a3 = _beacon(client, code="IB0000A3", loc="SOL-3-L4", available_commands=["deactivate", "comment", "stow"])
    a3["status"] = "broadcasting"
    client.portal.call(client.app.state.worker.sync_devices)
    text = "LOG 01/36 · CAPT. TEST\n\nHello."
    data = json.dumps({"beacons": [{"dest": "SOL-3", "text": text}, {"dest": "SOL-OORT", "text": "two", "code": "IB0000A2"},
                                   {"dest": "SOL-4", "text": "three"}]})
    r = client.post("/info-beacons/bulk", data={"data": data}, headers=HX).text
    assert "Preview: 2 to place, 1 to print" in r and "printed near SOL" in r and "IB0000A1" in r and "IB0000A3" not in r, r
    assert client.portal.call(client.app.state.db.kv_get, ib.TARGETS_KV, {}) == {}
    r = client.post("/info-beacons/bulk", data={"data": data, "go": "1"}, headers=HX).text
    assert "Imported 2 of 2 to place, 1 print order(s)" in r, r
    t = client.portal.call(client.app.state.db.kv_get, ib.TARGETS_KV, {})
    assert t["IB0000A1"]["text"] == text and t["IB0000A2"]["dest"] == "SOL-OORT" and t["IB0000A1"]["activate"]
    assert "at:sol-3" in [x.lower() for x in a.get("tags", [])]
    again = client.post("/info-beacons/bulk", data={"data": data}, headers=HX).text
    assert "0 to place, 0 to print, 3 already set" in again, again
    e = eng(client)
    a.update(location="SOL-3", tags=[])
    client.portal.call(client.app.state.worker.sync_devices)
    assert "deploy at SOL-3" in client.portal.call(e.info_beacon_pass)[0]
    a["available_commands"] = ["activate", "comment", "stow"]
    client.portal.call(client.app.state.worker.sync_devices)
    out = client.portal.call(e.info_beacon_pass)
    assert "describe at SOL-3" in out[0], out
    assert ib.description(a) == text
    out = client.portal.call(e.info_beacon_pass)
    assert "activate at SOL-3" in out[0], out
    bad = client.post("/info-beacons/bulk", data={"data": '[{"dest": "", "text": "x"}]'}, headers=HX).text
    assert "no star or location code" in bad


def test_bulk_assign_prefers_the_nearest_free_beacon():
    devs = [{"device_code": c, "device_type": ib.TYPE, "location": loc, "available_commands": ["deploy"]}
            for c, loc in (("A", "FAR-OORT"), ("B", "NEAR-3"))]
    dist = {("FAR", "HOME"): 100.0, ("NEAR", "HOME"): 5.0}
    rows = ib.assign([{"n": 1, "dest": "HOME-OORT", "text": "t", "title": "t", "code": None, "activate": True}],
                     devs, {}, {}, lambda a, b: dist[(a, b)])
    assert rows[0]["code"] == "B" and "5 ly" in rows[0]["why"]


def test_bulk_assign_skips_free_beacons_beyond_the_sector_range():
    devs = [{"device_code": "A", "device_type": ib.TYPE, "location": "FAR-OORT", "available_commands": ["deploy"]}]
    e = {"n": 1, "dest": "HOME-OORT", "text": "t", "title": "t", "code": None, "activate": True}
    rows = ib.assign([e], devs, {}, {}, lambda a, b: 30000.0, lambda a, b: True)
    assert rows[0]["status"] == "print" and "within the sector range" in rows[0]["why"] and not rows[0].get("code")
    rows = ib.assign([e], devs, {}, {}, None, None, {"p1": {"dest": "HOME-OORT", "text": "t", "factory": "AF1"}})
    assert rows[0]["status"] == "kept" and "printing on AF1" in rows[0]["why"]


def test_bulk_print_orders_print_near_the_destination_then_hand_over_the_text(client):
    import json
    from tests.test_ops import eng
    db = client.app.state.db
    client.portal.call(db.kv_set, "blueprints", [{"device_type": ib.TYPE, "resources": {}}])
    data = json.dumps([{"dest": "SOL-OORT", "text": "LOG 07", "title": "LOG 07"}])
    r = client.post("/info-beacons/bulk", data={"data": data, "go": "1"}, headers=HX).text
    assert "1 print order(s)" in r, r
    prints = client.portal.call(db.kv_get, ib.PRINTS_KV, {})
    (pid, p), = prints.items()
    e = eng(client)
    out = client.portal.call(e.info_beacon_pass)
    assert out and "printing on AF00BEEF" in out[0], out
    jobs = [j for j in client.portal.call(e.jobs) if (j.get("meta") or {}).get("ib_print") == pid]
    body = jobs[0]["steps"][0]["body"]
    assert body["device_type"] == ib.TYPE and set(body["tags"]) == {"to:sol", "at:sol-oort", "ibp:" + pid}, body
    assert client.portal.call(e.info_beacon_pass) == []   # ordered: not again
    assert "printing on AF00BEEF" in client.get("/info-beacons", headers=H).text
    again = client.post("/info-beacons/bulk", data={"data": data}, headers=HX).text
    assert "0 to print, 1 already set" in again, again
    _beacon(client, code="IBNEW001", loc="SOL-3-L4", available_commands=["deploy", "stow"], tags=["to:sol", "at:sol-oort", "ibp:" + pid])
    out = client.portal.call(e.info_beacon_pass)
    assert "IBNEW001 printed for SOL-OORT" in out[0], out
    t = client.portal.call(db.kv_get, ib.TARGETS_KV, {})
    assert t["IBNEW001"]["text"] == "LOG 07" and t["IBNEW001"]["activate"]
    assert client.portal.call(db.kv_get, ib.PRINTS_KV, {}) == {}


def test_an_idle_beacon_offering_deactivate_is_still_free():
    """Live 2026-11-28: the game lists deactivate on idle, undeployed beacons; none of Joe's 33 counted as free."""
    d = {"device_code": "E060D19D", "device_type": ib.TYPE, "location": "SOLARES-BELT-1", "status": "idle", "tags": [],
         "available_commands": ["change_owner", "deactivate", "decommission", "deploy", "stow"]}
    assert ib.free_beacon(d, {}, {})
    assert not ib.free_beacon({**d, "status": "broadcasting"}, {}, {})
