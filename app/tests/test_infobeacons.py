"""Information beacons (game v4.1.0): description, broadcast, comments, scans and the device.commented notification."""
from rsweb import infobeacons as ib
from rsweb import notify
from tests.test_ops import H, HX, add, client, world  # noqa: F401  (client is a fixture)


def _beacon(c, code="IB44C2E1", loc="SOL-3-L4", **kw):
    add(c, device_code=code, device_type=ib.TYPE, location=loc, status="idle", features=[],
        available_commands=["activate", "comment", "stow", "decommission"], **kw)
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
