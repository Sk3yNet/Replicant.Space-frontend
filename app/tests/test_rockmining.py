from datetime import datetime, timedelta, timezone

from rsweb import evacuate as ev
from rsweb import rockmining as rm
from test_ops import client  # noqa: F401  (fixture)


def iso(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def rig(code, loc, status="idle", **kw):
    return {"device_code": code, "device_type": "mining_rig", "location": loc, "status": status, "tags": [],
            "features": kw.pop("features", ["fracture", "cruise"]), "available_commands": ["travel", "start_mining"], **kw}


def rock(**kw):
    return {"designation": "A-OBJ-1", "status": "active", "impact_eta": iso(600), "impact_target": "A-3", "mass_class": "medium",
            "resource_sites": [{"designation": "A-OBJ-1-SITE-0",
                                "resources_remaining_pct": {"carbon": 100, "volatiles": 40, "rares": 0}}], **kw}


CFG = {"priority": "rares, volatiles", "keep_pct": 5, "send_idle": True, "min_hours": 1, "scan_rocks": True, "scan_minutes": 30}


def test_priority_order_slots_and_only_fracture_devices():
    assert rm.parse_list("Rares, bogus volatiles,rares") == ["rares", "volatiles"]
    objs = {"A-OBJ-1": rock()}
    devs = [rig("R1", "A-OBJ-1"), rig("R2", "A-1"), rig("R3", "A-2"), rig("R4", "B-1"),
            {"device_code": "MD", "device_type": "mining_drone", "location": "A-1", "status": "idle", "features": ["cruise"]},
            rig("R5", "A-1", features=["fracture"])]
    p = rm.plan(objs, devs, CFG, set(), {})
    # rares are gone, so volatiles (the next priority) first; medium = 2 slots: R1 there + R2; R4 is in another system
    assert p["start"] == [{"code": "R1", "rock": "A-OBJ-1", "resource": "volatiles"}]
    assert [(x["code"], x["resource"]) for x in p["send"]] == [("R2", "volatiles")]
    assert not any(x["code"] in ("MD", "R5") for x in p["send"])
    # volatiles mined out: the rest of a natural rock to dust, in resource order
    objs["A-OBJ-1"]["resource_sites"][0]["resources_remaining_pct"]["volatiles"] = 0
    p = rm.plan(objs, [rig("R1", "A-OBJ-1", "mining")], CFG, set(), {"R1": {"rock": "A-OBJ-1", "resource": "volatiles"}})
    assert p["retarget"] == [{"code": "R1", "rock": "A-OBJ-1", "resource": "carbon"}]


def test_launched_rocks_keep_some_of_each_and_late_rocks_get_no_new_rigs():
    o = rock(launched_by_you=True)
    o["resource_sites"][0]["resources_remaining_pct"] = {"carbon": 100, "volatiles": 5}
    p = rm.plan({"A-OBJ-1": o}, [rig("R1", "A-OBJ-1", "mining")], {**CFG, "launched_resources": "volatiles"}, set(),
                {"R1": {"rock": "A-OBJ-1", "resource": "volatiles"}})
    assert p["stop"] == [{"code": "R1", "rock": "A-OBJ-1", "resource": None}]   # carbon isn't chosen, volatiles at the floor
    late = {"A-OBJ-1": rock(impact_eta=iso(90))}
    assert rm.plan(late, [rig("R2", "A-1")], CFG, set(), {})["send"] == []
    assert rm.plan(late, [rig("R1", "A-OBJ-1")], CFG, set(), {})["start"]   # one already there still works


def test_scan_with_an_idle_survey_drone_not_one_at_a_belt():
    sd = {"device_code": "SD", "device_type": "survey_drone", "location": "A-2", "status": "idle", "tags": [],
          "features": ["cruise"], "available_commands": ["travel", "scan"]}
    belt = {**sd, "device_code": "SB", "location": "A-BELT-1"}
    p = rm.plan({"A-OBJ-1": rock()}, [rig("R1", "A-OBJ-1"), belt, sd], CFG, set(), {})
    assert p["scan"] == [{"code": "SD", "rock": "A-OBJ-1", "here": False}]
    there = {**sd, "location": "A-OBJ-1"}   # it stays and scans every 30 minutes
    assert rm.plan({"A-OBJ-1": rock()}, [rig("R1", "A-OBJ-1"), there], CFG, set(), {}, scanned={"A-OBJ-1": iso(-20)})["scan"] == []
    assert rm.plan({"A-OBJ-1": rock()}, [rig("R1", "A-OBJ-1"), there], CFG, set(), {}, scanned={"A-OBJ-1": iso(-31)})["scan"] \
        == [{"code": "SD", "rock": "A-OBJ-1", "here": True}]
    assert rm.amounts({"resource_sites": [{"resources": {"carbon": 1200, "volatiles": 300}, "resources_remaining_pct": {"carbon": 50}}]}) \
        == {"carbon": 1200.0, "volatiles": 300.0}


def test_rigs_keep_mining_an_off_target_rock_until_the_hour_propulsors_leave():
    objs = {"A-OBJ-1": rock(status="off_target", impact_eta=iso(300))}
    devs = [rig("R1", "A-OBJ-1", "mining"),
            {"device_code": "PR", "device_type": "propulsor_plate", "location": "A-OBJ-1", "status": "diverting", "tags": [],
             "features": ["cruise"], "available_commands": ["travel"]}]
    stars = {"A": {"entry_point": "A-OORT"}}
    assert [m["code"] for m in ev.plan(objs, devs, stars, set())["moves"]] == ["PR"]
    objs["A-OBJ-1"]["impact_eta"] = iso(45)
    assert {m["code"] for m in ev.plan(objs, devs, stars, set())["moves"]} == {"PR", "R1"}


def test_engine_sends_a_rig_and_the_defense_page_shows_it(client):  # noqa: F811
    from test_ops import H
    db, e = client.app.state.db, client.app.state.worker.automations
    o = {**rock(), "designation": "SOL-OBJ-1"}
    client.portal.call(db.kv_set, "objects", {"SOL-OBJ-1": o})
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices") + [rig("RIG1", "SOL-1")])
    s = client.portal.call(e.settings)
    s["rules"]["asteroid_mining"] = {**s["rules"].get("asteroid_mining", {}), **CFG, "enabled": True}
    client.portal.call(e.save_settings, s)
    out = client.portal.call(e.rock_mining_pass)
    assert out == ["RIG1 → SOL-OBJ-1 to mine volatiles"]
    job = next(j for j in client.portal.call(e.jobs) if j["rule"] == "asteroid_mining")
    assert job["steps"][-1]["body"] == {"command": "start_mining", "resource_type": "volatiles"}
    assert client.portal.call(e.evacuation_targets, [], {}).get("SOL-OBJ-1")   # its pile is hauled out while it's mined
    page = client.get("/defence", headers=H).text
    assert "Resources left" in page and "RIG1" in page
    panel = client.get("/automations/rules-panel?ids=asteroid_defence,asteroid_mining", headers=H).text
    assert 'name="priority"' in panel and "rares, volatiles" in panel


def test_the_trip_time_counts():
    o = rock(impact_eta=iso(240))   # 4 h: 3 h before the cutoff
    assert rm.trip_ok(o, 1.5 * 3600, CFG) and not rm.trip_ok(o, 2.5 * 3600, CFG) and rm.trip_ok(o, None, CFG)
