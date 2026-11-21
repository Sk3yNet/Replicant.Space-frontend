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
    assert "SOL-OBJ-1" not in client.portal.call(e.evacuation_targets, [], {})   # logistics never goes to a rock
    f = client.portal.call(e.fleets)[0]
    assert f["chase_run"]["target"] == "SOL-OBJ-1" and f["station"] is False
    page = client.get("/fleets/details?all=1", headers=H).text
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


def test_sensor_array_goes_to_the_kuiper_belt_before_detecting(client):  # noqa: F811
    """Joe 2026-11-16: a sensor array detects only from the Kuiper belt (Chaser_1's sat at RENGRELOR-3-L4)."""
    sensor = {"device_code": "SENS", "device_type": "sensor_array", "location": "SOL-3-L4", "status": "idle",
              "tags": ["fleet:rc"], "features": ["cruise"], "available_commands": ["detect_object", "travel"]}
    db, e = _setup(client, [rig("RIG1", "SOL-1"), sensor], {})
    out = client.portal.call(e.rock_chase_pass)
    assert "Chaser: SENS → SOL-KUIPER to search" in out and not any("searches" in x for x in out)
    job = next(j for j in client.portal.call(e.jobs) if j["rule"] == "rock_chaser")
    assert {"command": "travel", "destination": "SOL-KUIPER"} in [st.get("body") for st in job["steps"]]


def test_card_changes_queue_while_the_engine_is_busy(client):  # noqa: F811
    """Live 2026-11-16: Chaser_1's switch to hub mode never stuck: a tick holds the lock ~110 s and the save waited on it."""
    from test_ops import H
    db, e = _setup(client, [rig("RIG1", "SOL-1")], {}, {"mode": "roam"})
    client.portal.call(e.lock.acquire)
    try:
        r = client.post("/fleets/rc/chase", data={"action": "settings", "mode": "hub"}, headers={**H, "HX-Request": "true"})
    finally:
        client.portal.call(e.lock.release)
    assert "Saved" in r.text and "queued" in r.text
    assert client.portal.call(e.fleets)[0]["chase"]["mode"] == "roam"
    assert "home system only" in client.get("/fleets/details?all=1", headers=H).text   # the card shows it already
    client.portal.call(e.rock_chase_pass)
    assert client.portal.call(e.fleets)[0]["chase"]["mode"] == "hub" and not client.portal.call(db.kv_get, "chase_pending")
    r = client.post("/fleets/rc/chase", data={"action": "settings", "mode": "roam"}, headers={**H, "HX-Request": "true"})
    assert "queued" not in r.text and client.portal.call(e.fleets)[0]["chase"]["mode"] == "roam"


def test_chase_waits_for_a_fresh_device_list_after_arriving(client):  # noqa: F811
    """Live 2026-11-16 (Chaser_1, RENGRELOR-OBJ-1): a minute after the arrival job, the cached list still showed the rigs
    aboard and idle, so the pass sent them again; the repeat failed on "Already mining" and the rock would be skipped."""
    db, e = _setup(client, [rig("RIG1", "SOL-OBJ-1", attached_to_device_code="PLAT"), carrier("PLAT", "SOL-OBJ-1")],
                   {"SOL-OBJ-1": rock()})
    job = client.portal.call(e.create_job, "rock_chaser", "Chaser: chase SOL-OBJ-1 (rares)", "PLAT",
                             [{"desc": "x", "method": "POST", "path": "/devices/PLAT", "body": {"command": "travel"}, "wait": [],
                               "status": "done", "started_at": iso(-1)}])
    job["status"] = "done"
    client.portal.call(e._update, job)
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"target": "SOL-OBJ-1", "phase": "moving", "resource": "rares", "job": job["id"], "searched": {"SOL": iso(0)}}
    client.portal.call(e.save_fleets, f)
    st = client.portal.call(db.kv_get, "rock_mining", {}) or {}
    st["RIG1"] = {"rock": "SOL-OBJ-1", "resource": "rares", "sent": True}
    client.portal.call(db.kv_set, "rock_mining", st)
    assert not any("→ SOL-OBJ-1" in x for x in client.portal.call(e.rock_chase_pass))
    assert client.portal.call(e.fleets)[0]["chase_run"]["phase"] == "mining"
    assert len([j for j in client.portal.call(e.jobs) if j["rule"] == "rock_chaser"]) == 1
    # a repeat that only met "Already mining" keeps the rock
    job["status"] = "failed"
    job["steps"][0].update({"status": "skipped", "error": "Already mining", "started_at": iso(-10)})
    client.portal.call(e._update, job)
    client.portal.call(e.rock_chase_pass)
    assert client.portal.call(e.fleets)[0]["chase_run"].get("target") == "SOL-OBJ-1"


def test_rigs_mining_a_dropped_rock_take_it_back(client):  # noqa: F811
    db, e = _setup(client, [rig("RIG1", "SOL-OBJ-1", "mining (rares)")], {"SOL-OBJ-1": rock()})
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"phase": "idle", "skip": {"SOL-OBJ-1": iso(-5)}, "searched": {"SOL": iso(0)}}
    client.portal.call(e.save_fleets, f)
    client.portal.call(e.rock_chase_pass)
    run = client.portal.call(e.fleets)[0]["chase_run"]
    assert run["target"] == "SOL-OBJ-1" and run["phase"] == "mining" and "SOL-OBJ-1" not in run["skip"]


def test_ships_to_the_nearest_permanent_spot_never_the_impact_target(client):  # noqa: F811
    """Joe 2026-11-16: drop at a permanent spot with the shortest round trip, never at the impact target."""
    scan = {"planets": [{"designation": "SOL-3", "moon_count": 1}, {"designation": "SOL-4"}],
            "asteroid_belt": {"belts": [{"designation": "SOL-BELT-1"}]}}
    c = rc.drop_candidates("SOL", "SOL-3", "SOL-4-L5", scan, {"SOL-OBJ-1"})
    assert c[0] == "SOL-4-L5" and "SOL-BELT-1" in c and not any(x.startswith("SOL-3") for x in c)
    assert rc.near_target("SOL-3-L4", "SOL-3") and not rc.near_target("SOL-30", "SOL-3")
    fr = {"device_code": "FRT", "device_type": "cargo_freighter", "location": "SOL-OBJ-1", "status": "idle", "tags": ["fleet:rc"],
          "features": ["cruise", "transport"], "cargo_capacity": 500, "available_commands": ["travel", "collect_resources"]}
    db, e = _setup(client, [rig("RIG1", "SOL-OBJ-1", "mining (rares)"), fr], {"SOL-OBJ-1": rock()}, {"drop": "SOL-3"})
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-OBJ-1", "items": {"rares": 300}}])
    import json
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("SOL", json.dumps(scan), "2026-11-16T00:00:00+00:00"))
    secs = {"SOL-BELT-1": 40.0, "SOL-4": 300.0}

    async def trip_seconds(code, dest):
        return secs.get(dest, 900.0)
    e.trip_seconds = trip_seconds
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"target": "SOL-OBJ-1", "phase": "mining", "resource": "rares", "arrived": "SOL-OBJ-1",
                         "searched": {"SOL": iso(0)}}
    f[0]["chase"]["drop"] = ""
    client.portal.call(e.save_fleets, f)
    out = client.portal.call(e.rock_chase_pass)
    assert any("ships" in x and "to SOL-BELT-1" in x for x in out), out
    # a fleet controller ferrying from SOL-4-L5: its freighter is left alone and the drop is where the ferry collects
    jobs = [j for j in client.portal.call(e.jobs) if j["rule"] == "rock_chaser"]
    for j in jobs:
        j["status"] = "done"
        client.portal.call(e._update, j)
    devs = client.portal.call(db.kv_get, "devices")
    devs.append({**fr, "device_code": "FER", "controller_device_code": "CTL"})
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(db.insert_event, {"id": "dg1", "event": "ami.transport.digest", "device_code": "CTL",
                                         "payload": {"directive": "ferry", "report": {"collect": "SOL-4-L5",
                                                                                      "deliver": "FAR-BELT-1"}}})
    out = client.portal.call(e.rock_chase_pass)
    assert any("FRT ships" in x and "to SOL-4-L5" in x for x in out) and not any("FER" in x for x in out), out
    # a drop set on the card at the impact target is ignored
    from rsweb.chase_engine import ChaseMixin
    assert ChaseMixin._chase_drop(e, {"drop": "SOL-3"}, "SOL-OBJ-1", {"objs": {"SOL-OBJ-1": rock()}, "stars": {}, "scans": {}},
                                  {"drops": {"SOL-OBJ-1": "SOL-BELT-1"}}) == "SOL-BELT-1"


def test_fleet_controller_shuttles_rock_to_drop_while_mining_then_hands_back(client):  # noqa: F811
    """Joe 2026-11-16: the AMI controller runs every carrier: rock → best drop while mining, nearest hub when idle."""
    ctl = {"device_code": "CTL", "device_type": "ami_transport_controller", "location": "SOL-4-L5", "status": "idle",
           "tags": ["fleet:rc"], "features": ["ami"], "available_commands": ["adopt", "set_directive", "launch"]}
    fr = {"device_code": "FRT", "device_type": "cargo_freighter", "location": "SOL-4-L5", "status": "idle", "tags": ["fleet:rc"],
          "features": ["cruise", "transport"], "cargo_capacity": 500, "available_commands": ["travel"]}
    db, e = _setup(client, [rig("RIG1", "SOL-OBJ-1", "mining (rares)"), fr, ctl], {"SOL-OBJ-1": rock()}, {"drop": "SOL-4-L5"})
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"target": "SOL-OBJ-1", "phase": "mining", "resource": "rares", "arrived": "SOL-OBJ-1",
                         "searched": {"SOL": iso(0)}}
    client.portal.call(e.save_fleets, f)
    client.portal.call(db.kv_set, "loadout_ferries", {"CTL": {"configuration": {"collect": "SOL-4-L5", "deliver": "X"}}})
    out = client.portal.call(e.rock_chase_pass)
    assert "Chaser: CTL shuttles SOL-OBJ-1 → SOL-4-L5" in out, out
    job = next(j for j in client.portal.call(e.jobs) if "shuttles" in j["title"])
    bodies = [st.get("body") for st in job["steps"]]
    assert {"command": "adopt", "devices": ["FRT"]} in bodies
    assert {"command": "set_directive", "directive": "shuttle", "configuration": {"collect": "SOL-OBJ-1", "deliver": "SOL-4-L5"}} in bodies
    assert "CTL" not in (client.portal.call(db.kv_get, "loadout_ferries") or {})
    job["status"] = "done"
    client.portal.call(e._update, job)
    f = client.portal.call(e.fleets)
    f[0]["chase_run"].update({"target": None, "phase": "idle", "arrived": None})
    client.portal.call(e.save_fleets, f)
    client.portal.call(db.kv_set, "objects", {})   # the rock is gone
    devs = client.portal.call(db.kv_get, "devices")
    for d in devs:
        if d["device_code"] == "FRT":
            d.update({"location": "SOL-OBJ-1", "controller_device_code": "CTL", "status": "collecting"})
    client.portal.call(db.kv_set, "devices", devs)
    out = client.portal.call(e.rock_chase_pass)
    assert any("shuttle cleared" in x for x in out), out
    job = next(j for j in client.portal.call(e.jobs) if "supply ferry" in j["title"])
    assert {"command": "travel", "destination": "SOL-4-L5"} in [st.get("body") for st in job["steps"]]   # nobody waits at the rock
    assert "ami" not in client.portal.call(e.fleets)[0]["chase_run"]


def test_engine_starts_its_own_rigs_and_scan_at_the_rock(client):  # noqa: F811
    # snapshot 2026-11-06: the fleet is never stationed, so reserved() hid its own rigs and drone from the plan
    drone = {"device_code": "SURV", "device_type": "belt_surveyor", "location": "SOL-OBJ-1", "status": "idle",
             "tags": ["fleet:rc"], "features": ["cruise", "survey"], "available_commands": ["scan", "travel"]}
    db, e = _setup(client, [rig("RIG1", "SOL-OBJ-1"), carrier("PLAT", "SOL-OBJ-1"), drone], {"SOL-OBJ-1": rock()})
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"target": "SOL-OBJ-1", "phase": "mining", "resource": "rares", "searched": {"SOL": iso(0)}}
    client.portal.call(e.save_fleets, f)
    out = client.portal.call(e.rock_chase_pass)
    assert any(x.startswith("Chaser: RIG1 at SOL-OBJ-1: start rares") for x in out)   # the mock has no RIG1
    assert "Chaser: SURV scans SOL-OBJ-1" in out


def test_idle_chaser_gathers_members_from_other_systems_at_home(client):  # noqa: F811
    # live 2026-11-14 (Chaser_1): half the fleet sat in another system and nothing ever brought it home
    prop = {"device_code": "PROP", "device_type": "propulsor", "location": "NEAR-1-L4", "status": "idle", "tags": ["fleet:rc"],
            "features": ["cruise", "stow"], "available_commands": ["travel", "stow"]}
    fr = {"device_code": "FRT", "device_type": "cargo_freighter", "location": "NEAR-1-L4", "status": "idle",
          "tags": ["fleet:rc"], "features": ["surge", "cruise", "transport"], "cargo_capacity": 500,
          "available_commands": ["travel"]}
    devs = [rig("RIG1", "SOL-1"), rig("RIG2", "NEAR-BELT-1"), prop, fr, rig("RIG3", "FAR-BELT-1"),
            carrier("PLAT", "SOL-1"), carrier("PLAT2", "NEAR-BELT-1", attach_capacity=1)]
    db, e = _setup(client, devs, {})
    far = {"SOL": {"x": 0, "y": 0, "z": 0}, "NEAR": {"x": 10, "y": 0, "z": 0}, "FAR": {"x": 4000, "y": 0, "z": 0}}
    orig = e.star_lookup

    async def lookup(kind, *a, **k):
        return far if kind == "pos" else await orig(kind, *a, **k)

    async def commandable(devices, *a, **k):
        return {"SOL", "NEAR", "FAR"}
    e.star_lookup, e.commandable = lookup, commandable
    try:
        out = client.portal.call(e.rock_chase_pass)
    finally:
        e.__dict__.pop("star_lookup", None), e.__dict__.pop("commandable", None)
    jobs = [j for j in client.portal.call(e.jobs) if j["rule"] == "rock_chaser"]
    by = {j["device"]: [st.get("body") for st in j["steps"]] for j in jobs}
    # PLAT2 (in NEAR, one attach point) takes RIG2; PLAT flies over from home for the propulsor
    assert {"command": "attach", "device": "RIG2"} in by["PLAT2"]
    assert {"command": "detach", "device": "RIG2"} in by["PLAT2"]
    assert {"command": "attach", "device": "PROP"} in by["PLAT"]
    assert by["FRT"][0]["command"] == "travel" and by["FRT"][0]["destination"].startswith("SOL")
    assert "RIG3" not in str(by) and "RIG1" not in str(by)            # beyond the sector range; home already
    f = client.portal.call(e.fleets)[0]
    assert "can't fetch: RIG3" in f["chase_run"]["note"] and any("gathers" in x or "home" in x for x in out)


def test_every_next_target_exists_after_its_form():
    """Live 2026-11-16: the rock chaser Save (and Launch, Call off) targeted "next .chase-out" with no such element after
    them, so htmx never sent the request and Chaser_1's settings never saved."""
    import glob
    import os
    import re
    root = os.path.join(os.path.dirname(__file__), "..", "rsweb", "templates")
    bad = []
    for p in glob.glob(os.path.join(root, "**", "*.html"), recursive=True):
        s = open(p).read()
        for m in re.finditer(r'hx-target="next \.([\w-]+)"', s):
            if not re.search(r'class="[^"]*\b' + re.escape(m.group(1)) + r'\b', s[m.end():]):
                bad.append(f"{os.path.basename(p)}:{s[:m.start()].count(chr(10)) + 1} {m.group(1)}")
    assert not bad, bad


def test_launch_choice_follows_what_the_stockpile_is_low_on():
    """Joe 2026-11-19: launch the Kuiper objects that hold what the nearest stockpile is low on."""
    need = rc.need_order({"structural": 900, "conductive": 500, "rares": 400, "silicates": 50, "volatiles": 20})
    assert need[:3] == ["carbon", "volatiles", "silicates"]
    marks = {"SOL-KUIPER-1": {"star": "SOL", "composition": "metallic", "mass_class": "large"},
             "SOL-KUIPER-2": {"star": "SOL", "composition": "ice", "mass_class": "medium"},
             "SOL-KUIPER-3": {"star": "SOL", "composition": "rock", "mass_class": "large"},
             "VEGA-KUIPER-1": {"star": "VEGA", "composition": "ice", "mass_class": "large"}}
    des, why = rc.launch_choice(marks, "SOL", need)
    assert des == "SOL-KUIPER-2" and "carbon" in why
    marks["SOL-KUIPER-4"] = {"star": "SOL", "composition": "carbonaceous", "mass_class": "large"}
    assert rc.launch_choice(marks, "SOL", need)[0] == "SOL-KUIPER-4"   # same need (carbon): the bigger one holds more
    marks.pop("SOL-KUIPER-2"), marks.pop("SOL-KUIPER-4")
    assert rc.launch_choice(marks, "SOL", need)[0] == "SOL-KUIPER-3"   # rock holds silicates (game docs)
    marks["SOL-KUIPER-3"]["composition"] = "mixed"
    assert rc.launch_choice(marks, "SOL", need)[0] == "SOL-KUIPER-3"   # mixed: three random, may hold anything
    marks["SOL-KUIPER-3"]["composition"] = "glassy"
    des, why = rc.launch_choice(marks, "SOL", need, {"glassy": ["structural"]})
    assert des is None and "SOL-KUIPER-1 (metallic)" in why


def test_learn_yields_from_tracked_rocks():
    known = {}
    objs = {"SOL-OBJ-1": {"composition": "Glassy", "resource_sites": [{"resources_remaining_pct": {"silicates": 0, "structural": 80}}]},
            "SOL-OBJ-3": {"composition": "mixed", "resource_sites": [{"resources_remaining_pct": {"rares": 9}}]},
            "SOL-OBJ-2": {"composition": "", "resource_sites": [{"resources_remaining_pct": {"rares": 9}}]}}
    assert rc.learn_yields(objs, known) and known == {"glassy": ["silicates", "structural"]}
    assert not rc.learn_yields(objs, known)


def test_follow_need_mines_the_scarcest_then_everything_else():
    from rsweb import rockmining as rm
    cfg = {**rc.DEFAULTS, "priority": ", ".join(rc.need_order({"rares": 500, "carbon": 0}))}
    o = {"launched_by_you": True, "resource_sites": [{"resources_remaining_pct": {"carbon": 0, "conductive": 100, "rares": 0}}]}
    assert rm.pick(o, cfg) == "conductive"   # the wanted ones are gone: the rigs take the rest (Joe: when idle)


def test_spent_vector_charge_is_replaced_from_the_spares(client):  # noqa: F811
    """Live 2026-11-19: Chaser_1 used its only vector charge at 04:28 and none came back (the loadout pass skips
    unstationed fleets). A spare is now sent by logistics to where the carrier waits, and joins on arrival."""
    spare = {"device_code": "VCS", "device_type": "vector_charge", "location": "SOL-BELT-1", "status": "idle",
             "tags": ["spare"], "features": ["cruise", "stow"], "available_commands": ["travel"]}
    db, e = _setup(client, [rig("RIG1", "SOL-3-L4"), carrier("PLAT", "SOL-3-L4"), spare], {})
    f = client.portal.call(e.fleets)
    f[0]["wants"] = {"mining_rig": 1, "surge_platform": 1, "vector_charge": 1}
    f[0]["chase_run"] = {"searched": {"SOL": iso(0)}}
    client.portal.call(e.save_fleets, f)
    out = client.portal.call(e.rock_chase_pass)
    run = client.portal.call(e.fleets)[0]["chase_run"]
    spot = run["fetch_at"]   # the home drop: logistics never unloads at the Kuiper belt
    assert any(f"VCS (vector_charge) is free: logistics brings it to {spot}" in x for x in out), out
    assert "VCS" in run["fetch"] and "KUIPER" not in spot
    job = next(j for j in client.portal.call(e.jobs) if f"members for {spot}" in j["title"])
    for j in [job]:
        j["status"] = "done"
        client.portal.call(e._update, j)
    devs = client.portal.call(db.kv_get, "devices")
    for d in devs:
        if d["device_code"] == "VCS":
            d.update({"location": spot, "tags": [f"at:{spot.lower()}"]})
    client.portal.call(db.kv_set, "devices", devs)
    out = client.portal.call(e.rock_chase_pass)
    assert any(f"VCS (vector_charge) arrived at {spot} and joins" in x for x in out)
    assert not client.portal.call(e.fleets)[0]["chase_run"].get("fetch")


def test_auto_launch_waits_while_its_last_rock_flies(client):  # noqa: F811
    charge = {"device_code": "VC1", "device_type": "vector_charge", "location": "SOL-KUIPER", "status": "idle",
              "tags": ["fleet:rc"], "features": ["cruise"], "available_commands": ["detonate", "travel"]}
    db, e = _setup(client, [rig("RIG1", "SOL-OBJ-9"), charge], {"SOL-OBJ-9": rock("SOL-OBJ-9")},
                   {"launch_at": "SOL-3", "auto_launch": True})
    client.portal.call(db.kv_set, rc.LAUNCHED_KV, {"SOL-OBJ-9": {"by": "fleet Chaser", "status": "launched"}})
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"searched": {"SOL": iso(0)}, "read": {"SOL": iso(0)},
                         "marked": {"SOL-KUIPER-1": {"star": "SOL", "composition": "ice", "mass_class": "medium", "found_at": iso(0)}}}
    client.portal.call(e.save_fleets, f)
    out = client.portal.call(e.rock_chase_pass)
    assert not any("launched" in x for x in out)
    run = client.portal.call(e.fleets)[0]["chase_run"]
    assert "SOL-OBJ-9" in run["launch_note"] and not run.get("launch")


def test_idle_member_leaves_a_rock_the_fleet_is_not_working(client):  # noqa: F811
    """Joe 2026-11-19: freighter 2CBD7A06 sat idle at RENGRELOR-OBJ-1 hours before the impact."""
    fr = {"device_code": "FR1", "device_type": "cargo_freighter", "location": "SOL-OBJ-1", "status": "idle",
          "tags": ["fleet:rc"], "features": ["transport", "cruise"], "cargo_capacity": 500,
          "available_commands": ["travel"]}
    db, e = _setup(client, [rig("RIG1", "SOL-3-L4"), fr], {"SOL-OBJ-1": rock(pct={"rares": 0})})
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"phase": "idle", "drops": {"SOL-OBJ-1": "SOL-3-L4"}, "searched": {"SOL": iso(0)}}
    client.portal.call(e.save_fleets, f)
    out = client.portal.call(e.rock_chase_pass)
    assert "Chaser: FR1 leaves SOL-OBJ-1 for SOL-3-L4: nothing waits at a rock" in out


def test_rock_leaver_loads_the_pile_and_the_card_shows_each_step(client):  # noqa: F811
    from test_ops import H
    fr = {"device_code": "FR1", "device_type": "cargo_freighter", "location": "SOL-OBJ-1", "status": "idle",
          "tags": ["fleet:rc"], "features": ["transport", "cruise"], "cargo_capacity": 500, "cargo_used": 0,
          "available_commands": ["travel", "collect_resources"]}
    db, e = _setup(client, [rig("RIG1", "SOL-3-L4"), fr], {"SOL-OBJ-1": rock(pct={"rares": 0})}, {"launch_at": "SOL-3"})
    inv = client.portal.call(db.kv_get, "inventory") or []
    client.portal.call(db.kv_set, "inventory", inv + [{"location": "SOL-OBJ-1", "items": {"rares": 40}}])
    client.portal.call(db.kv_set, rc.LAUNCHED_KV, {"SOL-OBJ-1": {"by": "fleet Chaser", "status": "launched"}})
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"phase": "idle", "drops": {"SOL-OBJ-1": "SOL-3-L4"}, "searched": {"SOL": iso(0)}}
    client.portal.call(e.save_fleets, f)
    client.portal.call(e.rock_chase_pass)
    job = next(j for j in client.portal.call(e.jobs) if "FR1 off SOL-OBJ-1" in j["title"])
    bodies = [st.get("body") for st in job["steps"]]
    assert bodies[0] == {"command": "collect_resources", "resources": {"rares": 40}}
    assert {"command": "deposit_resources"} in bodies
    run = client.portal.call(e.fleets)[0]["chase_run"]
    assert run["inbound"][0]["rock"] == "SOL-OBJ-1"
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "4 Inbound" in page and "SOL-OBJ-1 → SOL-3" in page and "7 Resupply" in page


def test_rigs_leave_leftovers_for_a_rock_with_a_wanted_resource(client):  # noqa: F811
    """Joe 2026-11-19: less wanted resources are mined only when nothing better is to be had."""
    db, e = _setup(client, [rig("RIG1", "SOL-OBJ-1", "mining (structural)"), carrier("PLAT", "SOL-OBJ-1")],
                   {"SOL-OBJ-1": rock(pct={"structural": 100, "rares": 0}),
                    "SOL-OBJ-2": rock("SOL-OBJ-2", pct={"rares": 100})})
    inv = [{"location": "SOL-BELT-1", "items": {r: 1000 for r in ("structural", "conductive", "silicates")}}]
    client.portal.call(db.kv_set, "inventory", inv)
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"target": "SOL-OBJ-1", "phase": "mining", "arrived": "SOL-OBJ-1", "searched": {"SOL": iso(0)}}
    client.portal.call(e.save_fleets, f)
    assert client.portal.call(e._chase_better, client.portal.call(e.fleets)[0], {**rc.DEFAULTS, "mode": "hub",
                              "priority": ", ".join(rc.need_order(inv[0]["items"]))},
                              {}, {"objs": client.portal.call(db.kv_get, "objects"), "cover": set(), "pos": {},
                                   "fleets": [], "full": {}}, rock(pct={"structural": 100}), "SOL-OBJ-1",
                              rc.need_order(inv[0]["items"]))
    out = client.portal.call(e.rock_chase_pass)
    assert any("SOL-OBJ-2" in x for x in out), out


def _scan(client, db):
    import json
    scan = {"planets": [{"designation": "SOL-3"}], "asteroid_belt": {"belts": [{"designation": "SOL-BELT-1"}]},
            "outer_system": {"kuiper": {"designation": "SOL-KUIPER", "distance_au": 40}}}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("SOL", json.dumps(scan), iso(0)))


def test_idle_chaser_waits_at_the_kuiper_belt(client):  # noqa: F811
    """Joe 2026-11-20: the sensor array sat attached to a carrier at RENGRELOR-3-L4 for a day (no search since 11-19
    03:17); everything should wait at the Kuiper belt, close to a rock when it launches."""
    sensor = {"device_code": "SENS", "device_type": "sensor_array", "location": "SOL-3-L4", "status": "idle",
              "tags": ["fleet:rc"], "features": ["cruise"], "attached_to_device_code": "PLAT",
              "available_commands": ["detect_object", "travel"]}
    fr = {"device_code": "FR1", "device_type": "cargo_freighter", "location": "SOL-3-L4", "status": "idle",
          "tags": ["fleet:rc"], "features": ["transport", "cruise"], "cargo_capacity": 500, "available_commands": ["travel"]}
    db, e = _setup(client, [rig("RIG1", "SOL-3-L4", attached_to_device_code="PLAT"), carrier("PLAT", "SOL-3-L4"), sensor, fr], {})
    _scan(client, db)
    out = client.portal.call(e.rock_chase_pass)
    assert "Chaser: PLAT brings 2 member(s) home to SOL-KUIPER" in out, out
    job = next(j for j in client.portal.call(e.jobs) if "gathers" in j["title"])
    bodies = [st.get("body") for st in job["steps"]]
    assert {"command": "travel", "destination": "SOL-KUIPER"} in bodies
    assert not any("FR1" in x for x in out)   # haulers stay at the drop for the supply ferry


def test_propulsors_slow_a_rock_the_fleet_launched(client):  # noqa: F811
    prop = {"device_code": "P1", "device_type": "propulsor", "location": "SOL-OBJ-1", "status": "idle", "tags": ["fleet:rc"],
            "features": ["cruise"], "available_commands": ["activate", "travel"]}
    db, e = _setup(client, [rig("RIG1", "SOL-OBJ-1", "mining (rares)"), prop], {"SOL-OBJ-1": rock(minutes=600)})
    client.portal.call(db.kv_set, rc.LAUNCHED_KV, {"SOL-OBJ-1": {"by": "fleet Chaser", "status": "launched"}})
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"target": "SOL-OBJ-1", "phase": "mining", "arrived": "SOL-OBJ-1", "searched": {"SOL": iso(0)}}
    client.portal.call(e.save_fleets, f)
    out = client.portal.call(e.rock_chase_pass)
    assert "Chaser: P1 pushes SOL-OBJ-1 (decrease)" in out, out
    job = next(j for j in client.portal.call(e.jobs) if "slows" in j["title"])
    assert job["steps"][0]["method"] == "PATCH" and job["steps"][0]["body"] == {"configuration": {"settings": {"direction": "decrease"}}}
    # the ETA moved earlier 15 min after: the other direction
    f = client.portal.call(e.fleets)
    slow = f[0]["chase_run"]["slow"]
    slow["on_at"] = iso(-15)
    slow["eta_on"] = iso(700)
    client.portal.call(e.save_fleets, f)
    job["status"] = "done"
    client.portal.call(e._update, job)
    out = client.portal.call(e.rock_chase_pass)
    assert any("switching to increase" in x for x in out) and "Chaser: P1 pushes SOL-OBJ-1 (increase)" in out, out


def test_propulsors_stop_pushing_a_rock_the_fleet_left(client):  # noqa: F811
    """Live 2026-11-20: RENGRELOR-OBJ-2 was mined out (status depleted) and the fleet went idle, but its four propulsors
    kept pushing it."""
    prop = {"device_code": "P1", "device_type": "propulsor", "location": "SOL-OBJ-1", "status": "diverting",
            "tags": ["fleet:rc"], "features": ["cruise"], "available_commands": ["deactivate", "travel"]}
    db, e = _setup(client, [rig("RIG1", "SOL-3-L4"), prop], {"SOL-OBJ-1": rock(pct={"rares": 0})})
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"phase": "idle", "drops": {"SOL-OBJ-1": "SOL-3-L4"}, "searched": {"SOL": iso(0)}}
    client.portal.call(e.save_fleets, f)
    out = client.portal.call(e.rock_chase_pass)
    assert "Chaser: P1 leaves SOL-OBJ-1 for SOL-3-L4: nothing waits at a rock" in out, out
    job = next(j for j in client.portal.call(e.jobs) if "P1 off SOL-OBJ-1" in j["title"])
    assert [st["body"] for st in job["steps"]][0] == {"command": "deactivate"}


def test_propulsors_keep_pushing_while_the_pile_is_hauled(client):  # noqa: F811
    """Joe 2026-11-20: time for the stockpile to be moved as well as the mining."""
    prop = {"device_code": "P1", "device_type": "propulsor", "location": "SOL-OBJ-1", "status": "diverting",
            "tags": ["fleet:rc"], "features": ["cruise"], "available_commands": ["deactivate", "travel"]}
    db, e = _setup(client, [rig("RIG1", "SOL-3-L4"), prop], {"SOL-OBJ-1": rock(pct={"rares": 0})})
    inv = client.portal.call(db.kv_get, "inventory") or []
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-BELT-1", "items": {"structural": 900, "conductive": 900,
                                                                                    "silicates": 900}},
                                                {"location": "SOL-OBJ-1", "items": {"rares": 40}}])
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"phase": "idle", "drops": {"SOL-OBJ-1": "SOL-3-L4"}, "searched": {"SOL": iso(0)}}
    client.portal.call(e.save_fleets, f)
    out = client.portal.call(e.rock_chase_pass)
    assert not any("P1 leaves" in x for x in out), out
    # only less wanted material left in the pile: they go
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-BELT-1", "items": {"structural": 900, "conductive": 900,
                                                                                    "silicates": 900}},
                                                {"location": "SOL-OBJ-1", "items": {"structural": 40}}])
    out = client.portal.call(e.rock_chase_pass)
    assert any("P1 leaves" in x for x in out), out


def test_propulsors_stop_once_priority_materials_are_picked_up(client):  # noqa: F811
    prop = {"device_code": "P1", "device_type": "propulsor", "location": "SOL-OBJ-1", "status": "diverting",
            "tags": ["fleet:rc"], "features": ["cruise"], "available_commands": ["deactivate", "travel"]}
    db, e = _setup(client, [rig("RIG1", "SOL-OBJ-1", "mining (structural)"), prop],
                   {"SOL-OBJ-1": rock(minutes=600, pct={"structural": 80, "rares": 0})})
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-BELT-1", "items": {"structural": 900, "conductive": 900,
                                                                                    "silicates": 900}}])
    client.portal.call(db.kv_set, rc.LAUNCHED_KV, {"SOL-OBJ-1": {"by": "fleet Chaser", "status": "launched"}})
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"target": "SOL-OBJ-1", "phase": "mining", "arrived": "SOL-OBJ-1", "searched": {"SOL": iso(0)},
                         "drops": {"SOL-OBJ-1": "SOL-3-L4"},
                         "slow": {"rock": "SOL-OBJ-1", "dir": "decrease", "set": {"P1": "decrease"}}}
    client.portal.call(e.save_fleets, f)
    out = client.portal.call(e.rock_chase_pass)
    assert "Chaser: P1 stops slowing SOL-OBJ-1: priority materials all picked up" in out, out


def test_fleet_freighters_keep_clearing_the_pile_after_the_rigs_left(client):  # noqa: F811
    """Joe 2026-11-21: the chaser moves a worked rock's pile until the evacuation hour; logistics only after that."""
    ctl = {"device_code": "CTL", "device_type": "ami_transport_controller", "location": "SOL-4-L5", "status": "idle",
           "tags": ["fleet:rc"], "features": ["ami"], "available_commands": ["adopt", "set_directive", "launch"]}
    fr = {"device_code": "FRT", "device_type": "cargo_freighter", "location": "SOL-4-L5", "status": "idle", "tags": ["fleet:rc"],
          "features": ["cruise", "transport"], "cargo_capacity": 500, "available_commands": ["travel"]}
    db, e = _setup(client, [rig("RIG1", "SOL-KUIPER"), fr, ctl], {"SOL-OBJ-1": rock(minutes=240, pct={"carbon": 0})},
                   {"drop": "SOL-4-L5"})   # mined down: the rigs left
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-OBJ-1", "items": {"volatiles": 621}}])
    from rsweb.chase_engine import ChaseMixin
    run = {"phase": "idle", "arrived": "SOL-OBJ-1"}
    inv = {"SOL-OBJ-1": {"volatiles": 621}}
    assert ChaseMixin._chase_leftover(e, run, {"objs": {"SOL-OBJ-1": rock(minutes=240)}, "inv": inv}) == "SOL-OBJ-1"
    assert ChaseMixin._chase_leftover(e, run, {"objs": {"SOL-OBJ-1": rock(minutes=50)}, "inv": inv}) is None   # the evacuation hour
    assert ChaseMixin._chase_leftover(e, run, {"objs": {"SOL-OBJ-1": rock(minutes=240)}, "inv": {}}) is None   # nothing left
    f = client.portal.call(e.fleets)
    f[0]["chase_run"] = {"phase": "idle", "arrived": "SOL-OBJ-1", "searched": {"SOL": iso(0)}}
    client.portal.call(e.save_fleets, f)
    out = client.portal.call(e.rock_chase_pass)
    assert "Chaser: CTL shuttles SOL-OBJ-1 → SOL-4-L5" in out, out


def test_an_earlier_rocks_pile_is_cleared_before_the_new_ones(client):  # noqa: F811
    """Joe 2026-11-21: Chaser_1 moved to RENGRELOR-OBJ-5 and OBJ-4's pile was lost at its impact."""
    from rsweb.chase_engine import ChaseMixin
    e = client.app.state.worker.automations
    run = {"target": "SOL-OBJ-1", "arrived": "SOL-OBJ-1", "phase": "mining"}
    ChaseMixin._remember(e, run, {"state": {}}, {"rigs": []}, {"rock": "SOL-OBJ-2", "resource": "carbon", "star": "SOL",
                                                               "mine_h": 3}, set())
    assert run["left"] == ["SOL-OBJ-1"] and run["target"] == "SOL-OBJ-2"
    objs = {"SOL-OBJ-1": rock(minutes=240), "SOL-OBJ-2": rock("SOL-OBJ-2", 600)}
    inv = {"SOL-OBJ-1": {"volatiles": 600}, "SOL-OBJ-2": {"carbon": 90}}
    assert ChaseMixin._chase_leftover(e, run, {"objs": objs, "inv": inv}, "SOL-OBJ-2") == "SOL-OBJ-1"
    assert ChaseMixin._chase_leftover(e, run, {"objs": objs, "inv": {"SOL-OBJ-2": {"carbon": 90}}}, "SOL-OBJ-2") is None
    assert "left" not in run   # emptied: the shuttle goes back to the rock being mined
