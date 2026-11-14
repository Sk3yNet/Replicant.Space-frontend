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


def test_engine_steers_alerts_and_stays_terraforming(client):  # noqa: F811
    db, e = _setup(client, [dict(d) for d in KIT])
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
    assert f["terraform"]["planet"] == "SOL-5" and f["terraform"]["steer"] is False
    assert "a moon" in client.post("/fleets/tf/terraform", data={"action": "settings", "planet": "SOL"}, headers=HX).text


def test_fleets_overview_shows_it_terraforming(client):  # noqa: F811
    _setup(client, [dict(d) for d in KIT])
    r = client.get("/fleets/overview", headers=H)
    assert r.status_code == 200 and "terraforming SOL-4" in r.text
