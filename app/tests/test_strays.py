from datetime import timedelta

from rsweb import fleets as fl
from rsweb.automations import _now
from test_ops import H, client, eng  # noqa: F401  (fixture)

FLEETS = [{"id": "m1", "name": "Miner 1", "role": "mining", "home": "AAA", "station": True},
          {"id": "s1", "name": "Scout", "role": "explore", "home": "BBB", "station": True},
          {"id": "cab", "name": "Cab", "role": "logistics", "home": "AAA"}]


def devs():
    return [{"device_code": "CAR", "device_type": "cargo_vessel", "location": "BBB-1", "status": "idle", "tags": ["fleet:s1"]},
            {"device_code": "MD1", "device_type": "mining_drone", "location": None, "status": "stowed",
             "stowed_in_device_code": "CAR", "tags": ["fleet:m1"]},
            {"device_code": "MD2", "device_type": "mining_drone", "location": "AAA-1", "status": "idle", "tags": ["fleet:m1"]},
            {"device_code": "MD3", "device_type": "mining_drone", "location": "CCC-1", "status": "idle", "tags": ["fleet:m1"]},
            {"device_code": "MD4", "device_type": "mining_drone", "location": "CCC-1", "status": "idle", "tags": ["fleet:m1", "spare"]},
            {"device_code": "MX", "device_type": "replicant_matrix", "location": "CCC-1", "status": "idle", "tags": ["fleet:m1"]}]


def test_strays_include_riders_on_other_carriers_and_skip_spares_and_matrices():
    out = {s["code"]: s for s in fl.strays(FLEETS, devs(), set())}
    assert set(out) == {"MD1", "MD3"}
    assert out["MD1"]["star"] == "BBB" and out["MD1"]["riding"] == "CAR" and out["MD1"]["riding_fleet"] == "s1"
    assert fl.stray_reason(out["MD1"]) == "riding CAR (fleet s1)"
    assert fl.stray_reason(out["MD3"]) == "nothing is bringing it home"


def test_stray_pass_lets_riders_off_alerts_once_and_closes_a_move(client):  # noqa: F811
    db, e = client.app.state.db, eng(client)
    client.portal.call(db.kv_set, "devices", devs())
    client.portal.call(db.kv_set, "fleets", FLEETS)
    client.portal.call(db.kv_set, "replicants", {"R1": {"location": "BBB-1"}})
    client.portal.call(db.kv_set, "home_moves", {"m1": {"from": "CCC", "to": "AAA", "at": (_now() - timedelta(hours=3)).isoformat()}})
    out = client.portal.call(e.stray_pass)
    assert out == ["CAR lets off MD1"]
    jobs = client.portal.call(e.jobs)
    assert any(j["title"] == "Miner 1: CAR lets off MD1 (its fleet is in AAA)" for j in jobs)
    st = client.portal.call(db.kv_get, "fleet_strays", {})
    assert set(st) == {"MD1", "MD3"} and st["MD1"]["let_off"] == 1
    # an hour later: MD3 is reported once, the move once
    st["MD3"]["since"] = (_now() - timedelta(minutes=61)).isoformat()
    client.portal.call(db.kv_set, "fleet_strays", st)
    client.portal.call(e.stray_pass)
    client.portal.call(e.stray_pass)
    log = [x["text"] for x in client.portal.call(db.kv_get, "automation_log", [])]
    assert sum("away from their fleet for an hour" in t and "MD3" in t for t in log) == 1
    assert sum("Miner 1: move to AAA still missing" in t for t in log) == 1
    page = client.get("/fleets", headers=H).text
    assert "Strays" in page and "moving CCC → AAA" in page and "riding CAR (fleet s1)" in page
    # everyone home: the move is complete
    home = [dict(d, location="AAA-1", stowed_in_device_code=None) if d["device_code"] in ("MD1", "MD3") else d for d in devs()]
    client.portal.call(db.kv_set, "devices", home)
    client.portal.call(e.stray_pass)
    log = [x["text"] for x in client.portal.call(db.kv_get, "automation_log", [])]
    assert any("Miner 1: move to AAA complete" in t for t in log) and any("back with their fleet: MD3" in t for t in log)
    assert client.portal.call(db.kv_get, "home_moves", {}) == {}
    r = client.get("/fleets", headers=H)
    assert r.status_code == 200
