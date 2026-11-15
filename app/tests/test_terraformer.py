import json
from datetime import datetime, timezone

from rsweb import terraformer as tr
from test_ops import client  # noqa: F401  (fixture)

H = {"X-Auth-Request-Email": "joe@example.com"}
HX = {**H, "HX-Request": "true"}
PLANET = "SOL-4"


def dev(code, t, loc=PLANET, status="idle", **kw):
    return {"device_code": code, "device_type": t, "location": loc, "status": status, "tags": ["fleet:tf"],
            "features": ["cruise", "stow"], "available_commands": ["activate", "deactivate", "stow", "deploy"], **kw}


def report(**vals):
    base = {"temperature": 285, "pressure": 1.0, "oxygen": 21, "toxicity": 2, "hydrosphere": 55, "biosphere": 50,
            "tectonic": 15}
    base.update(vals)
    return {"body": PLANET, "attributes": {a: {"value": v, "delta": 0} for a, v in base.items()}}


CFG = tr.settings({})
KIT = [dev("MON", "terraform_monitor", status="monitoring"), dev("REG", "atmospheric_regulator"),
       dev("MIR", "orbital_mirror"), dev("SHA", "orbital_shade"), dev("LAN", "thermal_lance"), dev("CRY", "cryo_disperser"),
       dev("ATM", "atmo_processor"), dev("GAS", "gas_separator"), dev("FIL", "filtration_array"),
       dev("AQU", "aquifer_tap"), dev("BIO", "bio_seeder")]


def test_cold_world_warms_gently_or_hard_and_never_both_ways():
    p = tr.plan(report(temperature=262), tr.HUMANS, KIT, CFG)
    s = p["set"]
    assert s["MIR"]["on"] and not s["SHA"]["on"] and not s["CRY"]["on"]
    assert not s["LAN"]["on"]   # 23 K from the middle: the gentle mirrors do it
    p = tr.plan(report(temperature=275.5), tr.HUMANS, KIT, CFG)
    assert p["set"]["MIR"]["on"] is False   # inside the band: nothing to do


def test_lance_only_for_big_gaps_and_regulator_locks_held_values():
    p = tr.plan(report(temperature=250), tr.HUMANS, KIT, CFG)
    assert p["set"]["LAN"]["on"] and p["set"]["MIR"]["strength"] == 1.0
    assert not p["regulate"] and not p["set"]["REG"]["on"]
    p = tr.plan(report(temperature=280, oxygen=12), tr.HUMANS, KIT, CFG)
    assert p["regulate"] and p["set"]["REG"]["on"]
    assert p["set"]["GAS"] == {"on": True, "strength": 1.0, "direction": "increase"}
    assert not p["set"]["MIR"]["on"] and not p["set"]["ATM"]["on"]
    assert p["set"]["MON"]["on"]


def test_reversible_devices_get_a_direction_and_bio_waits_for_preconditions():
    p = tr.plan(report(pressure=2.0, biosphere=10, temperature=150), tr.HUMANS, KIT, CFG)
    assert p["set"]["ATM"]["direction"] == "decrease" and p["set"]["ATM"]["on"]
    assert not p["set"]["BIO"]["on"] and "preconditions" in p["why"]["bio_seeder"]
    p = tr.plan(report(biosphere=10), tr.HUMANS, KIT, CFG)
    assert p["set"]["BIO"]["on"]


def test_push_stops_once_the_projection_reaches_the_middle():
    st = report(temperature=280)
    st["attributes"]["temperature"]["delta"] = 1.0
    assert "temperature" not in tr.wants(st, tr.HUMANS, 15, {"temperature": 1})   # 280 + 6 ≥ 285
    st["attributes"]["temperature"]["delta"] = 0.1
    assert tr.wants(st, tr.HUMANS, 15, {"temperature": 1})["temperature"]["dir"] == 1   # keeps going inside the band


def test_commands_only_for_changes():
    devs = [dev("MIR", "orbital_mirror", status="terraforming", configuration={"settings": {"strength": 1.0}}),
            dev("SHA", "orbital_shade", status="terraforming"), dev("ATM", "atmo_processor")]
    sets = {"MIR": {"on": True, "strength": 1.0, "direction": None}, "SHA": {"on": False, "strength": None, "direction": None},
            "ATM": {"on": True, "strength": 0.5, "direction": "increase"}}
    cmds = tr.commands(sets, devs)
    assert cmds == [("ATM", "settings", {"configuration": {"settings": {"strength": 0.5, "direction": "increase"}}}),
                    ("ATM", "activate", {"command": "activate"}), ("SHA", "deactivate", {"command": "deactivate"})]
    assert tr.commands(sets, devs, {"ATM": tr.key(sets["ATM"]), "SHA": tr.key(sets["SHA"])}) == []


def _setup(client, devices, terraform=None, wants=None):  # noqa: F811
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices") + devices)
    client.portal.call(e.save_fleets, [{"id": "tf", "name": "Greening", "role": "terraformer", "home": "SOL",
                                        "wants": wants or {}, "terraform": {"planet": PLANET, **(terraform or {})}}])
    return db, e


def _report_event(client, st, seq=99950):  # noqa: F811
    db = client.app.state.db
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    client.portal.call(db.execute, "INSERT INTO events(id, seq, event, device_code, location, payload, created_at, received_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", (f"ev-{seq}", seq, "terraform.status", "MON", PLANET, json.dumps(st), now, now))


def test_engine_waits_for_a_replicant_then_flies_everyone_to_the_planet(client):  # noqa: F811
    vessel = {"device_code": "VES", "device_type": "cargo_vessel", "location": "SOL-3", "status": "idle", "tags": ["fleet:tf"],
              "features": ["surge", "cruise"], "stow_capacity": 50, "attach_capacity": 3, "available_commands": ["travel", "attach"]}
    devs = [vessel, dev("MON", "terraform_monitor", "SOL-3"), dev("MIR", "orbital_mirror", "SOL-3")]
    db, e = _setup(client, devs)
    assert client.portal.call(e.terraformer_pass) == []
    f = client.portal.call(e.fleets)[0]
    assert f["tf_run"]["phase"] == "gathering" and "replicant" in f["tf_run"]["note"] and f["pick_matrix"] is True
    matrix = {"device_code": "MAT", "device_type": "replicant_matrix", "location": "SOL-3", "status": "stowed",
              "stowed_in_device_code": "VES", "tags": []}
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices") + [matrix])
    out = client.portal.call(e.terraformer_pass)
    assert out == [f"Greening: → {PLANET}"]
    job = next(j for j in client.portal.call(e.jobs) if j["rule"] == "terraformer")
    bodies = [st.get("body") for st in job["steps"]]
    assert {"command": "stow", "target": "VES"} in bodies and {"command": "travel", "destination": PLANET} in bodies
    assert bodies[-1] == {"command": "deploy"} and job["meta"]["fleet"] == "tf"
    f = client.portal.call(e.fleets)[0]
    assert f["tf_run"]["phase"] == "moving" and f["tf_run"]["departed"] and f["station"] is False


def test_steering_is_off_by_default_only_the_monitor_is_switched_on(client):  # noqa: F811
    kit = [dict(d) for d in KIT]
    kit[0]["status"] = "idle"
    db, e = _setup(client, kit)
    _report_event(client, report(temperature=262, pressure=12))
    client.portal.call(e.terraformer_pass)
    job = next(j for j in client.portal.call(e.jobs) if j["rule"] == "terraformer")
    assert [(s["path"], s["body"]) for s in job["steps"]] == [("/devices/MON", {"command": "activate"})]
    rows = client.portal.call(db.fetchall, "SELECT level FROM notifications WHERE title LIKE '%Greenhouse%'")
    assert [r["level"] for r in rows] == ["error"]   # you steer it: you must act
    assert "you steer it" in client.portal.call(e.fleets)[0]["tf_run"]["note"]


def test_engine_steers_alerts_and_stays_terraforming(client):  # noqa: F811
    db, e = _setup(client, [dict(d) for d in KIT], {"steer": True})
    st = report(temperature=262, pressure=12)
    _report_event(client, st)
    out = client.portal.call(e.terraformer_pass)
    assert any("disaster" in x for x in out) and any(x.startswith("Greening: steering SOL-4") for x in out)
    job = next(j for j in client.portal.call(e.jobs) if j["rule"] == "terraformer")
    bodies = [(s["method"], s["path"], s.get("body")) for s in job["steps"]]
    assert ("POST", "/devices/MIR", {"command": "activate"}) in bodies
    assert ("PATCH", "/devices/ATM", {"configuration": {"settings": {"direction": "decrease"}}}) in bodies   # full strength already
    assert not any(p == "/devices/SHA" for _m, p, _b in bodies)
    rows = client.portal.call(db.fetchall, "SELECT level, title FROM notifications")
    assert [r["level"] for r in rows if "Greenhouse runaway" in r["title"]] == ["warning"]
    client.portal.call(e.terraformer_pass)   # the same disaster again: no second notification
    rows = client.portal.call(db.fetchall, "SELECT title FROM notifications WHERE title LIKE '%Greenhouse%'")
    assert len(rows) == 1
    # every value in range: it holds the world and still reports terraforming
    _report_event(client, report(), seq=99951)
    client.portal.call(e.terraformer_pass)
    f = client.portal.call(e.fleets)[0]
    assert f["tf_run"]["phase"] == "terraforming" and "holding it" in f["tf_run"]["note"]
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Terraformer" in page and "terraforming" in page and "New mission" not in page
    r = client.post("/fleets/tf/terraform", data={"action": "settings", "planet": "SOL-5", "species": "humans",
                                                  "margin_pct": "10"}, headers=HX)
    assert "Assigned to SOL-5" in r.text
    f = client.portal.call(e.fleets)[0]
    assert f["terraform"]["planet"] == "SOL-5" and f["terraform"]["steer"] is False   # unticked
    assert "a moon" in client.post("/fleets/tf/terraform", data={"action": "settings", "planet": "SOL"}, headers=HX).text


def test_fleets_overview_shows_it_terraforming(client):  # noqa: F811
    _setup(client, [dict(d) for d in KIT])
    r = client.get("/fleets/overview", headers=H)
    assert r.status_code == 200 and "terraforming SOL-4" in r.text


def test_the_fleet_takes_its_own_matrix_container_along(client):  # noqa: F811
    # Joe 2026-11-15: Terraformer 1's matrix container (Sk3y-34) sat at SOLARES-4-L4 while the fleet left
    carrier = {"device_code": "MOB", "device_type": "mobile_fleet", "location": "SOL-3", "status": "idle", "tags": ["fleet:tf"],
               "features": ["surge", "cruise"], "attach_capacity": 36, "available_commands": ["travel", "attach"]}
    cont = {"device_code": "CONT", "device_type": "matrix_container", "location": "SOL-3-L4", "status": "idle",
            "tags": ["fleet:tf"], "features": ["cradle", "cruise"], "available_commands": ["travel"],
            "hosting_replicant": {"name": "Sk3y-34", "replicant_code": "2B1F014B"}}
    db, e = _setup(client, [carrier, cont, dev("MON", "terraform_monitor", "SOL-3")])
    assert client.portal.call(e.terraformer_pass) == [f"Greening: → {PLANET}"]
    job = next(j for j in client.portal.call(e.jobs) if j["rule"] == "terraformer")
    bodies = [st.get("body") for st in job["steps"]]
    assert {"command": "attach", "device": "CONT"} in bodies
    assert {"command": "detach", "device": "CONT"} not in bodies   # it stays aboard at the planet


def test_return_home_boards_everyone_and_flies_back(client):  # noqa: F811
    # Joe 2026-11-15: Terraformer 1 back to SOLARES for a closer planet
    carrier = {"device_code": "MOB", "device_type": "mobile_fleet", "location": "FAR-4", "status": "idle", "tags": ["fleet:tf"],
               "features": ["surge", "cruise"], "attach_capacity": 36, "available_commands": ["travel", "attach"]}
    devs = [carrier, dev("MON", "terraform_monitor", "FAR-4", status="monitoring"), dev("MIR", "orbital_mirror", "FAR-4")]
    db, e = _setup(client, devs, {"planet": "FAR-4"})
    r = client.post("/fleets/tf/terraform", data={"action": "home"}, headers=HX)
    assert "Returning to SOL" in r.text
    client.portal.call(e.terraformer_pass)
    job = next(j for j in client.portal.call(e.jobs) if j["rule"] == "terraformer")
    bodies = [st.get("body") for st in job["steps"]]
    assert bodies[0] == {"command": "deactivate"} and job["steps"][0]["path"] == "/devices/MON"
    assert {"command": "stow", "target": "MOB"} in bodies or {"command": "attach", "device": "MON"} in bodies
    assert any(b and b.get("command") == "travel" and b["destination"].startswith("SOL") for b in bodies)
    assert not any(b and b.get("command") in ("deploy", "detach") for b in bodies)
    f = client.portal.call(e.fleets)[0]
    assert f["tf_run"]["phase"] == "returning" and f["terraform"]["planet"] == ""
    # home with everyone aboard: idle, waiting for a planet
    ds = client.portal.call(db.kv_get, "devices")
    for d in ds:
        if d["device_code"] == "MOB":
            d["location"] = "SOL-3"
        elif d["device_code"] in ("MON", "MIR"):
            d.update(location=None, stowed_in_device_code="MOB")
    client.portal.call(db.kv_set, "devices", ds)
    client.portal.call(e.cancel, job["id"])
    client.portal.call(e.terraformer_pass)
    f = client.portal.call(e.fleets)[0]
    assert f["tf_run"]["phase"] == "idle" and "back in SOL" in f["tf_run"]["note"] and not f["tf_run"].get("home_trip")


def test_a_trip_to_the_old_planet_ends_when_reassigned(client):  # noqa: F811
    # Joe 2026-11-15: Terraformer 1 kept flying to MAZARAR-2 after he set ALAYEBASAN-4
    from rsweb.automations import step
    carrier = {"device_code": "MOB", "device_type": "mobile_fleet", "location": "SOL-3", "status": "idle", "tags": ["fleet:tf"],
               "features": ["surge", "cruise"], "attach_capacity": 36, "available_commands": ["travel", "attach"]}
    db, e = _setup(client, [carrier, dev("MON", "terraform_monitor", "SOL-3")])
    old = client.portal.call(e.create_job, "terraformer", "Greening: to OLD-2", "MOB",
                             [step("MOB → OLD-2", "/devices/MOB", {"command": "travel", "destination": "OLD-2"})],
                             {"devices": ["MOB"], "fleet": "tf"})
    old = next(j for j in client.portal.call(e.jobs) if j["id"] == old["id"])
    client.portal.call(e._update, {**old, "status": "waiting"})   # still under way
    f = client.portal.call(e.fleets)
    f[0]["tf_run"] = {"planet": PLANET, "job": old["id"], "phase": "moving"}
    client.portal.call(e.save_fleets, f)
    client.portal.call(e.terraformer_pass)
    jobs = {j["id"]: j for j in client.portal.call(e.jobs)}
    assert jobs[old["id"]]["status"] == "cancelled"
    run = client.portal.call(e.fleets)[0]["tf_run"]
    assert run.get("job") != old["id"]


def test_page_lists_only_places_with_a_running_monitor(client):  # noqa: F811
    db = client.app.state.db
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices") + [
        dev("REGSOL", "atmospheric_regulator", loc="SOL-3-1"),
        dev("MONOFF", "terraform_monitor", loc="SOLARES-4-L4"), dev("LANCE", "thermal_lance", loc="SOLARES-4-L4"),
        dev("REGW", "atmospheric_regulator"), dev("MON", "terraform_monitor", status="monitoring",
                                                  terraform_status={"body": PLANET, **report(temperature=280)})])
    t = client.get("/terraforming", headers={"X-Auth-Request-Email": "joe@example.com"}).text
    assert "REGSOL" not in t and "SOL-3-1" not in t   # no monitor there: left off
    assert "MONOFF" in t and "LANCE" not in t and "its 1 other terraform device(s)" in t   # monitor off: it alone
    assert "REGW" in t and PLANET in t
    import time
    client.portal.call(db.kv_set, "terraform_pending", {"MONOFF": {"on": True, "at": time.time()}})   # just switched on
    out = client.get("/terraforming/world/SOLARES-4-L4", headers={"X-Auth-Request-Email": "joe@example.com", "HX-Request": "true"}).text
    assert "LANCE" in out and "Monitor off" not in out   # switched on: the rest shows


def _at_work(client, devs, terraform=None, wants=None):  # noqa: F811
    db, e = _setup(client, devs, terraform, wants)
    f = client.portal.call(e.fleets)
    f[0]["tf_run"] = {"planet": PLANET, "departed": "2026-11-15T04:00:00+00:00", "phase": "terraforming"}
    client.portal.call(e.save_fleets, f)
    return db, e


def _jobs(client, e, title):  # noqa: F811
    return [j for j in client.portal.call(e.jobs) if title in j["title"]]


def test_a_broken_monitor_isnt_switched_on_again_until_repaired(client):  # noqa: F811
    # live 2026-11-15: Terraformer 1's monitor E13168A9 at 17 %, "activate" refused every 6 minutes
    kit = [dict(d) for d in KIT]
    kit[0].update(status="idle", operational_capacity=17.0)
    db, e = _at_work(client, kit)
    client.portal.call(e.terraformer_pass)
    job = _jobs(client, e, "steer")[0]
    job["steps"][0].update(status="skipped", error="Device non-operational (capacity 17%). Requires repair.")
    client.portal.call(e._update, {**job, "status": "done"})
    client.portal.call(e.terraformer_pass)
    assert len(_jobs(client, e, "steer")) == 1   # not ordered again
    run = client.portal.call(e.fleets)[0]["tf_run"]
    assert run["broken"] == {"MON": 17.0} and "MON" in run["upkeep"] and "no service bot" in run["upkeep"]
    rows = client.portal.call(db.fetchall, "SELECT level, title FROM notifications WHERE title LIKE '%out of order%'")
    assert [r["level"] for r in rows] == ["error"]
    ds = client.portal.call(db.kv_get, "devices")
    next(d for d in ds if d["device_code"] == "MON")["operational_capacity"] = 100.0   # repaired
    client.portal.call(db.kv_set, "devices", ds)
    client.portal.call(e.terraformer_pass)
    assert len(_jobs(client, e, "steer")) == 2 and "broken" not in client.portal.call(e.fleets)[0]["tf_run"]


def test_the_service_bot_patrols_and_repaired_devices_come_back_on(client):  # noqa: F811
    carrier = {"device_code": "MOB", "device_type": "mobile_fleet", "location": PLANET, "status": "idle", "tags": ["fleet:tf"],
               "features": ["surge", "cruise"], "attach_capacity": 36, "available_commands": ["travel", "attach"]}
    bot = {"device_code": "BOT", "device_type": "service_bot", "location": None, "status": "stowed", "tags": ["fleet:tf"],
           "stowed_in_device_code": "MOB", "features": ["cruise", "repair", "stow", "ami"],
           "available_commands": ["deploy", "set_directive", "repair"]}
    devs = [carrier, bot, dev("MON", "terraform_monitor", status="monitoring"),
            dev("SHA", "orbital_shade", status="terraforming", operational_capacity=30.0)]
    db, e = _at_work(client, devs, wants={"service_bot": 1})
    client.portal.call(e.terraformer_pass)
    job = _jobs(client, e, "upkeep")[0]
    assert [s.get("body") for s in job["steps"]] == [{"command": "deploy"},
                                                     {"command": "set_directive", "directive": "patrol"}]
    client.portal.call(e.cancel, job["id"])
    # on patrol, repairing the shade (switched off by the game)
    ds = client.portal.call(db.kv_get, "devices")
    for d in ds:
        if d["device_code"] == "BOT":
            d.update(location=PLANET, stowed_in_device_code=None, status="repairing (SHA)",
                     ami_directive={"name": "patrol"})
        elif d["device_code"] == "SHA":
            d["status"] = "idle"
    client.portal.call(db.kv_set, "devices", ds)
    client.portal.call(e.terraformer_pass)
    run = client.portal.call(e.fleets)[0]["tf_run"]
    assert run["fixing"] == ["SHA"] and "SHA" in run["repair_off"] and "repairing SHA" in run["upkeep"]
    assert len(_jobs(client, e, "upkeep")) == 1
    for d in ds:
        if d["device_code"] == "BOT":
            d["status"] = "idle"
        elif d["device_code"] == "SHA":
            d["operational_capacity"] = 100.0
    client.portal.call(db.kv_set, "devices", ds)
    client.portal.call(e.terraformer_pass)
    job = _jobs(client, e, "upkeep")[-1]
    assert [(s["path"], s.get("body")) for s in job["steps"]] == [("/devices/SHA", {"command": "activate"})]


def test_a_member_added_after_departure_is_brought_by_logistics(client):  # noqa: F811
    # Joe 2026-11-15: a service bot added to Terraformer 1's loadout while it worked at ALAYEBASAN-4
    spare = {"device_code": "SB1", "device_type": "service_bot", "location": "SOL-3", "status": "coordinating",
             "tags": ["spare"], "features": ["cruise", "repair", "stow", "ami"], "available_commands": ["deploy"]}
    db, e = _at_work(client, [dev("MON", "terraform_monitor", status="monitoring"), spare], wants={"service_bot": 1})
    out = client.portal.call(e.terraformer_pass)
    assert any("SB1 (service_bot) is a spare" in x for x in out)
    job = _jobs(client, e, "upkeep")[0]
    cfg = job["steps"][0]["body"]["configuration"]
    assert cfg["add_tags"] == [f"at:{PLANET.lower()}"] and "spare" in cfg["remove_tags"]
    client.portal.call(e.cancel, job["id"])
    client.portal.call(e.terraformer_pass)
    assert len(_jobs(client, e, "upkeep")) == 1   # not sent for twice
    assert "on the way by logistics: SB1" in client.portal.call(e.fleets)[0]["tf_run"]["upkeep"]
    ds = client.portal.call(db.kv_get, "devices")
    next(d for d in ds if d["device_code"] == "SB1").update(location=PLANET, tags=[f"at:{PLANET.lower()}"], status="idle")
    client.portal.call(db.kv_set, "devices", ds)
    client.portal.call(e.terraformer_pass)
    job = _jobs(client, e, "upkeep")[-1]
    assert job["steps"][0]["body"]["configuration"] == {"add_tags": ["fleet:tf"], "remove_tags": [f"at:{PLANET.lower()}"]}
    assert "fetch" not in client.portal.call(e.fleets)[0]["tf_run"]


def test_old_pins_come_off_members_at_work(client):  # noqa: F811
    reg = dev("REG", "atmospheric_regulator", tags=["at:sol-belt-1", "fleet:tf"])
    db, e = _at_work(client, [dev("MON", "terraform_monitor", status="monitoring"), reg])
    client.portal.call(e.terraformer_pass)
    job = _jobs(client, e, "upkeep")[0]
    assert job["steps"][0]["body"] == {"configuration": {"remove_tags": ["at:sol-belt-1"]}}
