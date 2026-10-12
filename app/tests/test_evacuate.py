from datetime import datetime, timedelta, timezone

from rsweb import evacuate as ev
from rsweb import logistics as lg
from test_logistics import BPS, FLEETS, POS, SURGE, dev, world  # noqa: F401
from test_ops import client  # noqa: F401  (fixture)


def iso(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


STARS = {"A": {"entry_point": "A-OORT"}}


def test_devices_leave_an_object_due_within_the_hour_propulsors_last_grounded_ones_board():
    objs = {"A-OBJ-1": {"impact_eta": iso(45), "impact_target": "A-3"}, "A-OBJ-2": {"impact_eta": iso(300)}}
    devs = [dev("PR", "propulsor_plate", "A-OBJ-1", status="diverting", available_commands=["travel"], features=["cruise"]),
            dev("MD", "mining_drone", "A-OBJ-1", available_commands=["travel"], features=["cruise"]),
            dev("BC", "ftl_beacon", "A-OBJ-1", available_commands=["stow"], features=["stow"]),
            dev("SC", "surge_carrier", "A-OBJ-1", available_commands=["travel", "attach"], features=["cruise", "surge"]),
            dev("FAR", "mining_drone", "A-OBJ-2", available_commands=["travel"], features=["cruise"])]
    p = ev.plan(objs, devs, STARS, set())
    moved = {m["code"]: m["to"] for m in p["moves"]}
    assert moved == {"MD": "A-OORT", "SC": "A-OORT"}                      # PR keeps pushing; FAR's object isn't due
    assert p["boards"] == [{"carrier": "SC", "code": "BC", "mode": "attach", "to": "A-OORT"}]
    objs["A-OBJ-1"]["impact_eta"] = iso(20)
    assert "PR" in {m["code"] for m in ev.plan(objs, devs, STARS, set())["moves"]}
    assert "FAR" in {m["code"] for m in ev.plan(objs, devs, STARS, set(), warned={"A-OBJ-2"})["moves"]}   # the game warned
    objs["A-OBJ-1"]["closed"] = "diverted"
    assert not {m["code"] for m in ev.plan(objs, devs, STARS, set())["moves"]} & {"MD", "PR"}


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
