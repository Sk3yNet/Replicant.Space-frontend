from datetime import datetime, timedelta, timezone

from rsweb import evacuate as ev
from rsweb import logistics as lg
from test_logistics import BPS, FLEETS, POS, SURGE, dev, world  # noqa: F401
from test_ops import client  # noqa: F401  (fixture)


def iso(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


STARS = {"A": {"entry_point": "A-OORT"}}


def test_devices_leave_an_object_due_within_the_hour_propulsors_last_grounded_ones_board():
    objs = {"A-OBJ-1": {"impact_eta": iso(50), "impact_target": "A-3"}, "A-OBJ-2": {"impact_eta": iso(300)}}
    devs = [dev("PR", "propulsor_plate", "A-OBJ-1", status="diverting", available_commands=["travel"], features=["cruise"]),
            dev("MD", "mining_drone", "A-OBJ-1", available_commands=["travel"], features=["cruise"]),
            dev("BC", "ftl_beacon", "A-OBJ-1", available_commands=["stow"], features=["stow"]),
            dev("SC", "surge_carrier", "A-OBJ-1", available_commands=["travel", "attach"], features=["cruise", "surge"]),
            dev("FAR", "mining_drone", "A-OBJ-2", available_commands=["travel"], features=["cruise"])]
    p = ev.plan(objs, devs, STARS, set())
    moved = {m["code"]: m["to"] for m in p["moves"]}
    assert moved == {"MD": "A-OORT", "SC": "A-OORT"}                      # PR keeps pushing; FAR's object isn't due
    assert p["boards"] == [{"carrier": "SC", "code": "BC", "mode": "attach", "to": "A-OORT"}]
    objs["A-OBJ-1"]["impact_eta"] = iso(44)   # owner 2026-10-22: one leaving at 18 min was lost; 45 by default
    assert "PR" in {m["code"] for m in ev.plan(objs, devs, STARS, set())["moves"]}
    assert "FAR" in {m["code"] for m in ev.plan(objs, devs, STARS, set(), warned={"A-OBJ-2"})["moves"]}   # the game warned
    objs["A-OBJ-1"]["closed"] = "diverted"
    assert not {m["code"] for m in ev.plan(objs, devs, STARS, set())["moves"]} & {"MD", "PR"}


def test_the_propulsor_margin_is_a_setting():
    objs = {"A-OBJ-1": {"impact_eta": iso(55), "impact_target": "A-3"}}
    devs = [dev("PR", "propulsor_plate", "A-OBJ-1", status="diverting", available_commands=["travel"], features=["cruise"])]
    assert not ev.plan(objs, devs, STARS, set())["moves"]
    assert ev.plan(objs, devs, STARS, set(), diverter_minutes=60)["moves"][0]["deactivate"]


def test_a_pile_at_a_due_object_is_evacuated_first():
    devs = world()
    devs[2]["status"] = "waiting_for_resources"
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"A-OBJ-1": {"carbon": 70}, "A-1": {"carbon": 300}}, pos=POS,
                commandable={"A", "B"}, hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[], trades=[], incoming={},
                replicants={}, drop=lambda s: f"{s}-1", evacuate={"A-OBJ-1": "A-OORT"})
    assert p["requests"][0]["id"] == "evacuate:A-OBJ-1" and p["requests"][0]["tier"] == 0
    run = next(r for r in p["runs"] if r["req"] == "evacuate:A-OBJ-1")
    assert run["to"] == "A-OORT" and run["carry"] == {"carbon": 70}


def test_new_object_events_close_tracked_objects():
    from rsweb import defence as dfn
    assert dfn.closed_as("system.object_diverted") == "diverted" and dfn.closed_as("system.object_impacted") == "impacted"
    assert "system.object_impacted" in dfn.CLOSED_EVENTS


def test_engine_evacuates_on_the_warning(client):  # noqa: F811
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "objects", {"SOL-OBJ-1": {"designation": "SOL-OBJ-1", "impact_eta": iso(40),
                                                            "impact_target": "SOL-3", "status": "active"}})
    devs = client.portal.call(db.kv_get, "devices") + [
        dev("EV1", "mining_drone", "SOL-OBJ-1", tags=["at:sol-obj-1"], available_commands=["travel"], features=["cruise"])]
    client.portal.call(db.kv_set, "devices", devs)
    out = client.portal.call(e.evacuate_pass, True)
    assert out and "EV1" in out[0]
    job = next(j for j in client.portal.call(e.jobs) if j["rule"] == "evacuate")
    bodies = [s["body"] for s in job["steps"]]
    assert bodies[0]["command"] == "travel" and bodies[0]["destination"] != "SOL-OBJ-1"
    assert any((b or {}).get("configuration", {}).get("remove_tags") == ["at:sol-obj-1"] for b in bodies)


def test_propulsors_are_recalled_once_the_object_is_diverted_enough():
    """Owner 2026-10-15: diverted far enough (likelihood 0 / progress 100 %), the propulsors leave before the ETA —
    more thrust is wasted, and anything still there when it swings past is flung off and lost."""
    objs = {"A-OBJ-1": {"impact_eta": iso(600), "impact_target": "A-3", "impact_likelihood": 40, "progress_pct": 70}}
    devs = [dev("PR", "propulsor_plate", "A-OBJ-1", status="diverting", available_commands=["travel"], features=["cruise"])]
    assert ev.plan(objs, devs, STARS, set())["moves"] == []            # 10 h out, still needed
    objs["A-OBJ-1"].update({"impact_likelihood": 0.7, "progress_pct": 99.3})   # live: it never reaches 0 %
    m = ev.plan(objs, devs, STARS, set())["moves"]
    assert [x["code"] for x in m] == ["PR"] and "diverted enough" in m[0]["why"]
    assert m[0]["deactivate"]   # owner 2026-10-18: switched off first, then it travels
    assert ev.plan(objs, devs, STARS, set(), pct=0.5)["moves"] == []   # the rule's setting is stricter
    from rsweb import defence as dfn
    a = dfn.assess(objs["A-OBJ-1"], [], {})
    assert a["verdict"] == "diverted enough" and not a["threat"]


def test_never_evacuates_to_where_the_devices_already_are():
    """Live 2026-10-15: "leaving KELMORNEA-4-L4 for KELMORNEA-4-L4" — the warned location was the system's entry point."""
    stars = {"K": {"entry_point": "K-4-L4"}}
    objs = {"K-4-L4": {"impact_eta": iso(30), "impact_target": "K-4"}}
    devs = [dev("MD", "mining_drone", "K-4-L4", available_commands=["travel"], features=["cruise"])]
    m = ev.plan(objs, devs, stars, set(), warned={"K-4-L4"})["moves"]
    assert [(x["code"], x["to"]) for x in m] == [("MD", "K-OORT")]
    objs["K-4-L4"]["impact_eta"] = iso(-30)   # owner: once the impact time has passed it's over, nothing more is sent
    assert ev.plan(objs, devs, stars, set(), warned={"K-4-L4"})["moves"] == []
    assert ev.safe_spot("K", "K-4", stars, {"planets": [{"designation": "K-4"}, {"designation": "K-5"}]}, here="K-4-L4") == "K-5"


def test_off_target_objects_v360_are_evacuated_not_pushed_and_show_resources_left():
    from rsweb import defence as dfn
    from rsweb import notify
    o = {"designation": "A-OBJ-1", "status": "off_target", "impact_eta": iso(300), "impact_target": "A-3",
         "impact_likelihood": 40, "required_strength": 100, "progress_pct": 50,
         "resource_sites": [{"designation": "A-OBJ-1-SITE-0", "resources_remaining_pct": {"carbon": 72, "rares": 100}}]}
    assert "A-OBJ-1" in ev.due({"A-OBJ-1": o})   # flung out at the ETA: everything leaves
    a = dfn.assess(o, [], {})
    assert a["verdict"] == "diverted" and not a["threat"]
    assert a["resources_left"] == {"carbon": 72.0, "rares": 100.0}
    t = notify.describe({"event": "system.object_impacted", "location": "A-OBJ-1",
                         "payload": {"object_designation": "A-OBJ-1", "impact_target": "A-3", "description": "Fast steep rock impact",
                                     "depleted": True}})
    assert "Fast steep rock impact" in t and "no terraform effect" in t
    assert "off target" in notify.describe({"event": "system.object_off_target", "location": "A-OBJ-1",
                                            "payload": {"object_designation": "A-OBJ-1", "impact_target": "A-3"}})
