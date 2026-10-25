from datetime import datetime, timedelta, timezone

from rsweb import rockchase as rc
from test_ops import client  # noqa: F401  (fixture)


def iso(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def rig(code, loc, status="idle", **kw):
    return {"device_code": code, "device_type": "mining_rig", "location": loc, "status": status, "tags": ["fleet:rc"],
            "features": ["fracture", "cruise", "stow"], "available_commands": ["travel", "start_mining", "stow"], **kw}


def carrier(code, loc, **kw):
    return {"device_code": code, "device_type": "surge_platform", "location": loc, "status": "idle", "tags": ["fleet:rc"],
            "features": ["surge", "cruise"], "attach_capacity": 4, "available_commands": ["travel", "attach"], **kw}


def hauler(code, loc, **kw):
    return {"device_code": code, "device_type": "transport_hauler", "location": loc, "status": "idle", "tags": ["fleet:rc"],
            "features": ["transport", "cruise"], "cargo_capacity": 100, "cargo_used": 0,
            "available_commands": ["travel", "collect_resources"], **kw}


def rock(des="SOL-OBJ-1", minutes=600, size="medium", pct=None):
    return {"designation": des, "status": "active", "impact_eta": iso(minutes), "impact_target": des.split("-")[0] + "-3",
            "mass_class": size, "resource_sites": [{"designation": des + "-SITE-0",
                                                     "resources_remaining_pct": pct or {"carbon": 100, "rares": 40}}]}


CFG = rc.settings({"chase": {"mode": "roam", "radius": 50, "priority": "rares", "min_hours": 2, "min_size": "medium"}})
POS = {"SOL": {"x": 0, "y": 0, "z": 0}, "NEAR": {"x": 10, "y": 0, "z": 0}, "FAR": {"x": 400, "y": 0, "z": 0}}


def test_targets_meet_the_criteria_home_system_first():
    objs = {"NEAR-OBJ-1": rock("NEAR-OBJ-1", 900), "SOL-OBJ-1": rock("SOL-OBJ-1", 400), "SOL-OBJ-2": rock("SOL-OBJ-2", 900, "small"),
            "FAR-OBJ-1": rock("FAR-OBJ-1", 2000), "SOL-OBJ-3": rock("SOL-OBJ-3", 200),
            "SOL-OBJ-4": rock("SOL-OBJ-4", 900, pct={"carbon": 0, "rares": 0})}
    area = rc.area(CFG, "SOL", {"SOL", "NEAR", "FAR"}, POS)
    assert area == {"SOL", "NEAR"}   # FAR is beyond the radius
    got = [c["rock"] for c in rc.candidates(objs, CFG, area, "SOL-3", POS)]
    # small, too late (3 h 20 m: under 2 h after the trip and the 80 min leave) and dry rocks are out; own system first
    assert got == ["SOL-OBJ-1", "NEAR-OBJ-1"]
    assert rc.candidates(objs, CFG, area, "SOL-3", POS, taken={"SOL-OBJ-1"})[0]["rock"] == "NEAR-OBJ-1"
    assert rc.area({**CFG, "mode": "hub"}, "SOL", {"SOL", "NEAR"}, POS) == {"SOL"}
    assert rc.candidates(objs, CFG, area, "SOL-3", POS)[0]["resource"] == "rares"


def test_leaving_never_after_the_defense_hour_and_shipping_wanted_first():
    assert rc.leave_h({"leave_minutes": 30}) == 1.0 and rc.leave_h(CFG) == 80 / 60
    assert rc.due_to_leave(rock(minutes=75), CFG) and not rc.due_to_leave(rock(minutes=90), CFG)
    assert rc.due_to_leave(None, CFG)
    assert rc.ship_take({"carbon": 80, "rares": 50}, hauler("H", "SOL-1"), ["rares"]) == {"rares": 50, "carbon": 50}
    m = rc.marked([{"designation": "sol-kuiper-001", "mass_class": "large"}, {"designation": "SOL-KUIPER-002",
                                                                               "mass_class": "small"}], CFG, "SOL", "now")
    assert list(m) == ["SOL-KUIPER-001"]
    k = rc.kinds([rig("R", "SOL-1"), carrier("C", "SOL-1"), hauler("H", "SOL-1")])
    assert [d["device_code"] for d in k["rigs"] + k["carriers"] + k["shippers"]] == ["R", "C", "H"]


def _setup(client, devices, objs, chase=None):  # noqa: F811
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "objects", objs)
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices") + devices)
    client.portal.call(e.save_fleets, [{"id": "rc", "name": "Chaser", "role": "rock_chaser", "home": "SOL", "wants": {},
                                        "chase": {"mode": "hub", "priority": "rares", **(chase or {})}}])
    return db, e


def test_engine_chases_a_rock_and_the_mining_rule_leaves_its_rigs_alone(client):  # noqa: F811
    from test_ops import H
    db, e = _setup(client, [rig("RIG1", "SOL-1"), carrier("PLAT", "SOL-1")], {"SOL-OBJ-1": rock()})
    s = client.portal.call(e.settings)
    s["rules"]["asteroid_mining"] = {**s["rules"].get("asteroid_mining", {}), "enabled": True, "priority": "carbon"}
    client.portal.call(e.save_settings, s)
    assert client.portal.call(e.rock_mining_pass) == []   # RIG1 belongs to the rock chaser
    out = client.portal.call(e.rock_chase_pass)
    assert out == ["Chaser: → SOL-OBJ-1 to mine rares"]
    job = next(j for j in client.portal.call(e.jobs) if j["rule"] == "rock_chaser")
    bodies = [st.get("body") for st in job["steps"]]
    assert {"command": "attach", "device": "RIG1"} in bodies and {"command": "travel", "destination": "SOL-OBJ-1"} in bodies
    assert bodies[-1] == {"command": "start_mining", "resource_type": "rares"}
    assert client.portal.call(e.evacuation_targets, [], {}).get("SOL-OBJ-1")   # logistics hauls its pile out
    f = client.portal.call(e.fleets)[0]
    assert f["chase_run"]["target"] == "SOL-OBJ-1" and f["station"] is False
    page = client.get("/fleets/details", headers=H).text
    assert "Rock chaser" in page and "SOL-OBJ-1" in page and 'name="leave_minutes"' in page
    r = client.post("/fleets/rc/chase", data={"action": "settings", "mode": "roam", "leave_minutes": "30", "min_size": "large"},
                    headers={**H, "HX-Request": "true"})
    assert "Saved" in r.text
    c = client.portal.call(e.fleets)[0]["chase"]
    assert c["mode"] == "roam" and c["leave_minutes"] == 60 and c["min_size"] == "large" and c["auto_launch"] is False


def test_engine_leaves_in_time_and_ships_the_pile(client):  # noqa: F811
    from rsweb.chase_engine import ChaseMixin  # noqa: F401
    devs = [rig("RIG1", "SOL-OBJ-1", "mining"), carrier("PLAT", "SOL-OBJ-1"), hauler("HAUL", "SOL-3")]
    db, e = _setup(client, devs, {"SOL-OBJ-1": rock(minutes=75)})
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-OBJ-1", "items": {"rares": 30}}])
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"target": "SOL-OBJ-1", "phase": "mining", "resource": "rares"}
    client.portal.call(e.save_fleets, f)
    out = client.portal.call(e.rock_chase_pass)
    assert any("ships 30 from SOL-OBJ-1" in x for x in out) and any("leaving SOL-OBJ-1" in x for x in out)
    jobs = [j for j in client.portal.call(e.jobs) if j["rule"] == "rock_chaser"]
    leave = next(j for j in jobs if "leave" in j["title"])
    bodies = [st.get("body") for st in leave["steps"]]
    assert bodies[0] == {"command": "deactivate"} and {"command": "attach", "device": "RIG1"} in bodies
    assert any(b and b.get("command") == "travel" and b["destination"] != "SOL-OBJ-1" for b in bodies)
    ship = next(j for j in jobs if "ships" in j["title"])
    assert {"command": "collect_resources", "resources": {"rares": 30}} in [st.get("body") for st in ship["steps"]]


def test_engine_marks_kuiper_objects_and_launches_one(client):  # noqa: F811
    sensor = {"device_code": "SENS", "device_type": "sensor_array", "location": "SOL-KUIPER", "status": "idle",
              "tags": ["fleet:rc"], "features": ["cruise"], "available_commands": ["detect_object"]}
    charge = {"device_code": "VC1", "device_type": "vector_charge", "location": "SOL-KUIPER-001", "status": "idle",
              "tags": ["fleet:rc"], "features": ["cruise"], "available_commands": ["detonate", "travel"]}
    db, e = _setup(client, [rig("RIG1", "SOL-1"), sensor, charge], {}, {"launch_size": "large", "launch_at": "SOL-3",
                                                                       "auto_launch": True})
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"searched": {"SOL": iso(-120)}}
    client.portal.call(e.save_fleets, f)
    out = client.portal.call(e.rock_chase_pass)
    assert "Chaser: marked for launch: SOL-KUIPER-001" in out
    assert any("SOL-KUIPER-001 at SOL-3" in x for x in out)   # auto launch: the charge is already there
