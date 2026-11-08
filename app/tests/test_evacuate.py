from datetime import datetime, timedelta, timezone

from rsweb import evacuate as ev
from rsweb import logistics as lg
from test_logistics import BPS, FLEETS, POS, SURGE, dev, world  # noqa: F401
from test_ops import HX, client  # noqa: F401  (fixture)


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


def test_mined_out_and_overdue_rocks_are_still_evacuated():
    # 2026-11-08: SOLARES-OBJ-6 (status depleted) struck 18.5 h after its ETA with 46 devices there
    devs = [dev("MD", "mining_rig", "A-OBJ-1", available_commands=["travel"], features=["cruise"])]
    objs = {"A-OBJ-1": {"impact_eta": iso(50), "impact_target": "A-3", "status": "depleted"}}
    assert [m["code"] for m in ev.plan(objs, devs, STARS, set())["moves"]] == ["MD"]
    objs = {"A-OBJ-1": {"impact_eta": iso(-600), "impact_target": "A-3", "status": "depleted", "overdue": True}}
    m = ev.plan(objs, devs, STARS, set())["moves"]
    assert m and m[0]["why"] == "past its impact time and still incoming"
    objs["A-OBJ-1"]["closed"] = "impacted"
    assert not ev.plan(objs, devs, STARS, set())["moves"]


def test_a_rock_past_its_eta_is_overdue_until_the_game_reports_it(client):  # noqa: F811
    from rsweb import defence as dfn
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "objects", {
        "SOL-OBJ-6": {"designation": "SOL-OBJ-6", "impact_eta": iso(-120), "impact_target": "SOL-3", "status": "depleted",
                      "closed": dfn.CONCLUDED, "closed_at": iso(-120)},
        "SOL-OBJ-1": {"designation": "SOL-OBJ-1", "impact_eta": iso(-60 * 50), "status": "active"},
        "SOL-3-L4": {"designation": "SOL-3-L4", "impact_eta": iso(-60), "source": "impact_warning"}})
    objs = client.portal.call(e.defence_objects)
    assert objs["SOL-OBJ-6"].get("overdue") and not objs["SOL-OBJ-6"].get("closed")
    assert objs["SOL-OBJ-1"]["closed"] == dfn.CONCLUDED          # past OVERDUE_H
    assert objs["SOL-3-L4"]["closed"] == dfn.CONCLUDED           # a warned place, not a rock
    assert client.portal.call(e.overdue_rocks) == {"SOL-OBJ-6"}
    assert client.portal.call(e.safe_piles, {"SOL-OBJ-6": {"carbon": 5}, "SOL-1": {"carbon": 1}}) == {"SOL-1": {"carbon": 1}}
    a = dfn.assess(objs["SOL-OBJ-6"], [], [])
    assert a["verdict"] == dfn.OVERDUE and not a["threat"]


def test_outage_announcement_and_schedule():
    from rsweb import season
    start, mins = season.parse_announcement("Monday, 9th November 2026 Start Time: 20:00 UTC Expected duration: Up to 1 hour")
    assert start.isoformat() == "2026-11-09T20:00:00+00:00" and mins == 60
    assert season.parse_announcement("November 9, 2026 at 20:00 EST for 90 minutes") is None   # never a guessed zone
    assert season.parse_announcement("2026-11-09 20:00 for 90 minutes")[1] == 90
    w = {"id": "o1", "start": start.isoformat(), "minutes": 60, "lead": 30, "after": 15, "state": "pending"}
    at = lambda h, m: datetime(2026, 11, 9, h, m, tzinfo=timezone.utc)  # noqa: E731
    assert season.outage_step([w], None, at(19, 0))[1] is None
    wins, act, _ = season.outage_step([w], None, at(19, 31))
    assert act == "pause" and wins[0]["state"] == "holding"
    assert season.outage_step(wins, {"window": "o1"}, at(21, 0))[1] is None
    wins2, act, _ = season.outage_step(wins, {"window": "o1"}, at(21, 16))
    assert act == "resume" and wins2[0]["state"] == "done"
    assert season.outage_step(wins, {"by": "season change"}, at(21, 16))[1] is None   # a new season stays paused
    assert season.outage_step([w], {"by": "you"}, at(19, 31))[1] is None   # paused by hand: left alone


def test_outage_page_and_engine(client):  # noqa: F811
    from rsweb import season
    db, e = client.app.state.db, client.app.state.worker.automations
    r = client.post("/outage/add", data={"text": "Monday, 9th November 2026 Start Time: 20:00 UTC Expected duration: Up to 1 hour"}, headers=HX)
    assert r.status_code == 200
    wins = client.portal.call(db.kv_get, season.OUTAGE_KV)
    assert wins[0]["start"] == "2026-11-09T20:00:00+00:00"
    assert "Scheduled game outages" in client.get("/partials/story", headers=HX).text
    soon = datetime.now(timezone.utc) + timedelta(minutes=10)
    client.portal.call(db.kv_set, season.OUTAGE_KV, [{"id": "x", "start": soon.isoformat(), "minutes": 60, "lead": 30, "after": 15}])
    client.portal.call(e.outage_pass)
    assert client.portal.call(db.kv_get, season.HOLD_KV)["window"] == "x"
