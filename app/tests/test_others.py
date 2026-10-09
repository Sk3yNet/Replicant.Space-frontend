from datetime import datetime, timedelta, timezone

from rsweb import others as oth
from test_ops import H, HX, REP, add, client, eng, iso, world  # noqa: F401  (fixture)

POS = {"FALQUORYX": {"x": -460.3, "y": -214.8, "z": 4.9}, "OTHILETH": {"x": -458.9, "y": -223.8, "z": 3.3},
       "SOL": {"x": 0, "y": 0, "z": 0}}


def test_fixed_types():
    assert oth.is_fixed("ftl_beacon") and oth.is_fixed("system_ward") and oth.is_fixed("autofactory")
    assert not oth.is_fixed("mining_drone") and not oth.is_fixed("cargo_vessel") and not oth.is_fixed("surge_plate")


def test_snapshot_replaced_departures_and_prune():
    s = oth.normalize({})
    t0 = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    scan = [{"device_code": "B1", "device_type": "ftl_beacon", "location": "SOL-3", "owner_replicant_code": "X", "owner_name": "helga"},
            {"device_code": "D1", "device_type": "survey_drone", "location": "SOL-3", "owner_replicant_code": "X"},
            {"device_code": "M1", "device_type": "ftl_beacon", "location": "SOL-4", "owner_replicant_code": REP}]
    oth.record_scan(s, "SOL", scan, {REP}, now=t0)
    assert [d["code"] for d in s["stars"]["SOL"]["devices"]] == ["B1"]       # fixed, not ours
    left = [{"travel_type": "departure", "star": "SOL", "device_code": "B1", "logged_at": iso(t0 + timedelta(minutes=5))}]
    assert oth.apply_departures(s, left) == 1 and s["stars"]["SOL"]["devices"] == []
    oth.prune(s, now=t0 + timedelta(days=8))
    assert "SOL" not in s["stars"]


def test_traffic_arrow_guesses_the_other_end():
    now = datetime.now(timezone.utc)
    rows = [{"replicant_code": "X", "travel_type": "arrival", "star": "FALQUORYX", "device_type": "cargo_vessel",
             "logged_at": iso(now - timedelta(minutes=5)), "vector": "0.15,-0.97,-0.17"},
            {"replicant_code": "X", "travel_type": "arrival", "star": "FALQUORYX", "device_type": "cargo_vessel",
             "logged_at": iso(now - timedelta(hours=3)), "vector": "0.15,-0.97,-0.17"},          # too old
            {"replicant_code": REP, "travel_type": "arrival", "star": "FALQUORYX",
             "logged_at": iso(now), "vector": "0.15,-0.97,-0.17"}]                               # ours
    out = oth.map_traffic(rows, {REP}, POS, {"X": {"name": "helga"}})
    assert len(out) == 1 and out[0]["way"] == "in" and out[0]["who"] == "helga" and out[0]["guess"] == "OTHILETH"


def test_arrival_scans_system_and_map_shows_it(client):
    w = world(client)
    w.foreign_devices = [{"device_code": "B1", "device_type": "ftl_beacon", "location": "SOL-3",
                          "owner_replicant_code": "F00D0001", "owner_name": "helga"},
                         {"device_code": "D1", "device_type": "mining_drone", "location": "SOL-BELT-1",
                          "owner_replicant_code": "F00D0001", "owner_name": "helga"}]
    client.portal.call(client.app.state.worker.sync_catalogue)
    out = client.portal.call(eng(client).sync_traffic)
    assert "SOL" in out["others"]["scanned"]
    out = client.portal.call(eng(client).sync_traffic)
    assert out["others"]["scanned"] == []                                     # same system: not again
    m = client.get("/api/map.json?part=overlay", headers=H).json()
    sol = next(o for o in m["others"] if o["star"] == "SOL")
    assert sol["n"] == 1 and sol["owners"][0]["name"] == "helga"
    page = client.get("/systems/SOL", headers=H).text
    assert "Other players here" in page and "helga" in page and "other-player" in page
    assert "1× mining drone" in page                                          # drones: totals (and the full scan below)
    assert "Full scan" in page and "SOL-BELT-1" in page and "Raw JSON" in page


def test_trail_reset_keeps_target(client):
    db = client.app.state.db
    client.portal.call(db.kv_set, "trail", {"target_name": "Bill", "target_code": "B1LL0001",
                                            "beacons": {"BB1": {"star": "SOL"}}, "audit": {"BB1": [{"id": 1}]},
                                            "read_at": {"BB1": "x"}, "seen": ["1"]})
    r = client.post("/trail/reset", headers=HX)
    assert "Trail cleared" in r.text
    t = client.portal.call(db.kv_get, "trail")
    assert t["target_code"] == "B1LL0001" and t["beacons"] == {} and t["audit"] == {} and t["seen"] == []


def test_traffic_poll_setting(client):
    r = client.post("/traffic/settings", data={"poll_minutes": "3"}, headers=HX)
    assert "every 3 minutes" in r.text
    assert client.portal.call(client.app.state.worker.traffic_interval) == 180


def test_drone_totals_and_belt_count():
    s = oth.normalize({})
    scan = [{"device_code": f"M{i}", "device_type": "mining_drone", "location": "SOL-BELT-1-SITE-1", "owner_replicant_code": "X",
             "owner_name": "helga", "status": "mining (carbon)"} for i in range(3)] + \
           [{"device_code": "S1", "device_type": "survey_drone", "location": "SOL-BELT-1", "owner_replicant_code": "X", "owner_name": "helga"},
            {"device_code": "M9", "device_type": "mining_drone", "location": "SOL-BELT-2", "owner_replicant_code": "Y", "owner_name": "bob"}]
    oth.record_scan(s, "SOL", scan, {REP})
    snap = s["stars"]["SOL"]
    assert snap["devices"] == []                                           # drones aren't fixed devices
    tot = oth.drone_totals(snap)
    assert tot[0]["name"] == "helga" and tot[0]["types"] == {"mining_drone": 3, "survey_drone": 1}
    assert oth.drones_at(snap, "SOL-BELT-1") == 3 and oth.drones_at(snap, "SOL-BELT-2") == 1
