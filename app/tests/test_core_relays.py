"""Core tests: relays, wards, hubs, beacons, the map, systems and prospecting (split out of test_core.py by test name)."""
from corelib import *  # noqa: F401,F403


def test_system_targets_from_scans_details_and_events(client):
    from rsweb.targets import category
    assert [category(c, "SOL") for c in ["SOL", "SOL-3", "SOL-3-1", "SOL-3-L4", "SOL-BELT-1", "SOL-BELT-1-SITE-2",
                                         "SOL-1-3-SAL-1", "SOL-OBJ-2", "SOL-KUIPER"]] == \
        ["star", "planet", "moon", "lagrange", "belt", "site", "salvage", "object", "outer"]
    # refresh = system scan + belt details (resource sites)
    r = client.post("/ami/TC000001/refresh-targets", headers=HX)
    assert r.headers.get("HX-Refresh") == "true", r.text
    # a salvage find reported by the event stream
    client.portal.call(client.app.state.worker.handle_event, {
        "id": "7777777777777-0", "event": "salvage.discovered", "star": "SOL", "location": "SOL-4",
        "payload": {"designation": "SOL-4-1-SAL-1", "location": "SOL-4-1", "name": "Old probe", "salvage_type": "wreck"},
        "created_at": "2026-09-30T12:00:00+00:00"})
    from rsweb.targets import system_targets
    t = client.portal.call(system_targets, client.app.state.db, "SOL")
    codes = {x["code"]: x for x in t["targets"]}
    assert codes["SOL-BELT-1"]["category"] == "belt" and "structural rich" in codes["SOL-BELT-1"]["label"]
    assert "SOL-BELT-1-SITE-1" in codes and codes["SOL-BELT-1-SITE-1"]["category"] == "site"
    assert codes["SOL-4-1-SAL-1"]["category"] == "salvage" and "Old probe" in codes["SOL-4-1-SAL-1"]["label"]
    assert "stock" in codes["SOL-3-L4"]["label"]
    assert t["resources"]["structural"]["level"] == "rich"


def test_system_targets_list_a_scans_system_objects(client):
    import json as _json
    db = client.app.state.db
    scan = {"entry_point": "TWAFFY-11-L4", "planets": [{"designation": "TWAFFY-1"}],
            "system_objects": [{"designation": "DERELICT-RING-001", "location": "TWAFFY-OBJ-1", "title": "Derelict Ring",
                                "object_type": "megastructure", "megastructure_type": "derelict_ring"}]}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("TWAFFY", _json.dumps(scan), "2026-10-18T00:00:00+00:00"))
    from rsweb.targets import system_targets
    codes = {x["code"]: x for x in client.portal.call(system_targets, db, "TWAFFY")["targets"]}
    assert codes["TWAFFY-OBJ-1"]["category"] == "object" and "Derelict Ring" in codes["TWAFFY-OBJ-1"]["label"]


def test_system_targets_show_only_objects_that_still_exist(client):
    """Owner 2026-10-22: the system page shows active asteroids only; past ones stay on the Defense page."""
    import json as _json
    db = client.app.state.db
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("KELX", _json.dumps({"planets": [{"designation": "KELX-1"}]}), "2026-10-18T00:00:00+00:00"))
    for i, loc in enumerate(["KELX-OBJ-1", "KELX-OBJ-2"]):
        client.portal.call(db.insert_event, {"id": f"od{i}", "event": "system.object_detected", "location": loc,
                                              "payload": {"object_designation": loc}})
    client.portal.call(db.kv_set, "objects", {"KELX-OBJ-1": {"designation": "KELX-OBJ-1", "status": "active"},
                                              "KELX-OBJ-2": {"designation": "KELX-OBJ-2", "closed": "impacted"}})
    from rsweb.targets import system_targets
    codes = {x["code"] for x in client.portal.call(system_targets, db, "KELX")["targets"]}
    assert "KELX-OBJ-1" in codes and "KELX-OBJ-2" not in codes


def test_ami_forms_offer_system_targets(client):
    client.post("/ami/TC000001/refresh-targets", headers=HX)
    r = client.get("/ami/TC000001/directive-form?directive=delivery", headers=HX)
    assert '<optgroup label="Stockpiles">' in r.text and '<optgroup label="Resource sites">' in r.text
    assert '<optgroup label="Lagrange points">' in r.text  # deliver-to list
    assert '<option value="SOL-BELT-1-SITE-1"' in r.text and 'name="f.route.collect__custom"' in r.text
    # ferry deliver goes to another system: free text with suggestions, not a system-only list
    r = client.get("/ami/TC000001/directive-form?directive=ferry", headers=HX)
    assert 'name="f.deliver" value="" list=' in r.text
    # mining controller: resource hints from the system's belts
    r = client.get("/ami/MC91FF22/directive-form?directive=gather_resources", headers=HX)
    assert "rich" in r.text and "richest belt level in SOL" in r.text
    r = client.get("/ami/MC91FF22/directive-form?directive=gather_salvage", headers=HX)
    assert '<optgroup label="Planets">' in r.text and '<optgroup label="Resource sites">' not in r.text
    # a -SAL- code picked anyway is sent as its body
    from rsweb.web import directive_body
    assert directive_body({"directive": "gather_salvage", "f.location": "SOL-3-1-SAL-1"})["configuration"]["location"] == "SOL-3-1"
    # the whole AMI page renders with per-controller targets
    page = client.get("/ami", headers=H).text
    assert "known targets in SOL" in page and "Refresh targets" in page
    # typed code overrides the list
    r = client.post("/ami/TC000001/directive", data={"directive": "shuttle", "f.collect": "SOL-BELT-1",
                                                     "f.collect__custom": "sol-belt-1-site-1", "f.deliver": "SOL-3-L4"}, headers=HX)
    assert "result ok" in r.text
    acts = client.get("/account", headers=H).text
    assert "&#34;collect&#34;: &#34;SOL-BELT-1-SITE-1&#34;" in acts


def test_a_gather_evenly_schedule_leans_toward_print_needs(client):
    import json as _j
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("SOL", _j.dumps({"asteroid_belt": {"belts": [{"designation": "SOL-BELT-1",
                                                                      "resources": {"carbon": "rich", "silicates": "low"}}]}}),
                        "2026-10-22T00:00:00+00:00"))

    async def needs(devices):
        return {"silicates": 1.0}
    eng.print_needs = needs
    ctrl = {"device_code": "MC91FF22", "device_type": "ami_mining_controller", "location": "SOL-BELT-1"}
    use = client.portal.call(eng.print_tilt, {"directive": "gather_evenly"}, ctrl, [])
    assert use["directive"] == "maintain_ratios" and use["configuration"]["ratios"]["silicates"] > use["configuration"]["ratios"]["carbon"]
    assert client.portal.call(eng.print_tilt, {"directive": "gather_evenly"}, {**ctrl, "tags": ["boot:x"]}, [])["directive"] \
        == "gather_evenly"


def test_system_resources_and_map_places(client):
    from rsweb.targets import system_resources
    client.portal.call(client.app.state.worker.sync_devices)
    world = client.app.state.api.http._transport.app.state.world
    for ev in [e for e in world.events if e["event"] == "salvage.discovered"]:
        client.portal.call(client.app.state.worker.handle_event, dict(ev))
    page = client.get("/systems/SOL", headers=H).text
    assert "<h2>Resources</h2>" in page and "Derelict hauler" in page and "SOL-3-1-SAL-1" in page
    r = client.post("/systems/SOL/resources/refresh", headers=HX)
    assert "open mining site(s)" in r.text and "salvage body" in r.text and "Could not read" not in r.text
    res = client.portal.call(system_resources, client.app.state.db, "SOL")
    assert res["totals"]["structural"]["sites"] == 5200 and res["totals"]["rares"]["sites"] == 340
    # the body's detail gives % remaining of what was discovered (300 structural, 80 conductive)
    assert res["totals"]["structural"]["salvage"] == pytest.approx(260, abs=0.1)
    assert res["mineable"] == 5540 and res["salvageable"] == pytest.approx(335, abs=0.1)
    sal = next(x for x in res["salvage"] if x["code"] == "SOL-3-1-SAL-1")
    assert sal["body"] == "SOL-3-1" and sal["remaining_pct"] == {"structural": 86.67, "conductive": 93.75}
    page = client.get("/systems/SOL", headers=H).text
    assert 'class="marker place place-site' in page and 'class="marker place place-salvage' in page
    assert "SITE-1 · 5,200" in page or "SITE-1 · 5200" in page
    assert 'class="marker place place-lagrange' in page   # Lagrange points are drawn too
    lst = client.get("/systems", headers=H).text
    assert "Mineable" in lst and ("5,540" in lst or "5540" in lst)
    # a depleted site no longer counts
    client.portal.call(client.app.state.worker.handle_event, _ev(99, "site.depleted", site="SOL-BELT-1-SITE-2", location="SOL-BELT-1"))
    res = client.portal.call(system_resources, client.app.state.db, "SOL")
    assert res["mineable"] == 5200


def test_ferry_controller_kept_out_of_in_system_work():
    from rsweb import production
    from rsweb.ami_schedule import adoptable, targets_of
    devs = [{"device_code": "TF", "device_type": "ami_transport_controller", "location": "A-1", "tags": ["ferry"], "features": ["ami"]},
            {"device_code": "TI", "device_type": "ami_transport_controller", "location": "A-1", "features": ["ami"]},
            {"device_code": "TD", "device_type": "transport_drone", "location": "A-1", "status": "idle"}]
    assert [c["device_code"] for c in production.controllers_in(devs, "A", "transport")] == ["TI"]
    assert [c["device_code"] for c in targets_of({"target": "kind:transport"}, devs)] == ["TI"]
    assert adoptable(devs, devs[0], {}) == [] and adoptable(devs, devs[1], {}) == ["TD"]


def test_print_completed_tags_new_device_for_its_system(client):
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(client.app.state.db.kv_set, "loadout_orders",
                       [{"star": "SOL", "device_type": "survey_drone", "factory": "AF00BEEF", "at": "2026-10-01T00:00:00+00:00"}])
    world.devices.append({"device_code": "NEW00001", "device_type": "survey_drone", "location": "SOL-3-L4", "status": "idle",
                          "features": [], "available_commands": []})
    client.portal.call(client.app.state.worker.handle_event,
                       {"id": "5555555555555-0", "event": "print.completed", "device_code": "AF00BEEF", "device_type": "autofactory",
                        "location": "SOL-3-L4", "payload": {"device_type": "survey_drone", "new_device_code": "NEW00001", "tags": []},
                        "created_at": "2026-10-01T00:00:00+00:00"})
    assert "to:sol" in next(d for d in world.devices if d["device_code"] == "NEW00001")["tags"]
    assert client.portal.call(client.app.state.db.kv_get, "loadout_orders")[0]["device_code"] == "NEW00001"


def test_arrivals_join_their_systems_controller():
    from rsweb.ami_schedule import handoffs, handoff_steps
    hs = handoffs(_arrival_world(), {}, set())
    assert [(h["drone"], h["controller"]) for h in hs] == [("MD1", "MC1"), ("SD1", "SC1")]   # TD1: only a ferry controller there
    md = hs[0]
    bodies = [(s["path"], s["body"]) for s in handoff_steps(md)]
    assert bodies == [("/devices/MD1", {"command": "travel", "destination": "ITH-BELT-1"}),
                      ("/devices/MC1", {"command": "adopt", "devices": ["MD1"]}),
                      ("/devices/MC1", {"command": "launch"})]                                  # MC1 is running a directive
    assert [s["body"]["command"] for s in handoff_steps(hs[1])] == ["travel", "adopt"]          # SC1 is idle: no launch
    assert handoffs(_arrival_world(), {"MD1": "MC9"}, {"SD1"}) == []                           # managed per digests / busy


def test_start_notes_explain_restarts_and_upgrades():
    from rsweb import version as ver
    me = {"run": ver.RUN_ID, "version": ver.VERSION, "fingerprint": ver.FINGERPRINT}
    assert "no earlier run on record" in " ".join(ver.start_notes(me, None))
    crashed = {"run": "r1", "version": "1.3.0", "fingerprint": "abc", "started_at": "2026-10-01T00:00:00",
               "last_seen": "2026-10-01T05:00:00", "stopped_at": None}
    lines = ver.start_notes(me, crashed)
    assert any("did not stop cleanly — last seen 2026-10-01T05:00:00" in l for l in lines)
    assert any(l == f"upgraded v1.3.0 → v{ver.VERSION}" for l in lines)
    assert any(l.strip().startswith("v1.4.0 (") for l in lines) and not any(l.strip().startswith("v1.3.0 (") for l in lines)
    same = {**crashed, "version": ver.VERSION, "fingerprint": "zzz", "stopped_at": "2026-10-01T05:00:00"}
    lines = ver.start_notes(me, same)
    assert any("stopped cleanly" in l for l in lines) and any("code changed (zzz →" in l for l in lines)
    assert ver.age_of({"run": ver.RUN_ID}, []) == "current" and ver.age_of({}, []) == "unversioned"
    assert ver.age_of({"run": "x", "v": ver.VERSION}, []) == "earlier run" and ver.age_of({"run": "x", "v": "0.9"}, []) == "older version"


def test_relaying_relay_goes_to_its_pin():
    # live 2026-10-29: B2B8B2EA, sent to the OTHILETH-1-EVT-007 contract, relayed at OTHILETH-1-L4 and never moved
    from rsweb import loadouts as lo
    relay = {"device_code": "B2B8B2EA", "device_type": "ftl_relay", "location": "OTHILETH-1-L4", "status": "relaying",
             "in_control_range": True, "available_commands": ["deactivate", "travel"],
             "tags": ["at:othileth-1", "contract:othileth-1-evt-007"]}
    other = {"device_code": "4A8A3109", "device_type": "ftl_relay", "location": "OTHILETH-1-L4", "status": "relaying",
             "tags": []}
    cfg = {"phases": [], "systems": {}}
    p = lo.plan(cfg, [relay, other], [], {}, {"OTHILETH": {}}, {}, set(), [], {})
    assert ("B2B8B2EA", "OTHILETH-1") in p["pins"] and p["pins_off"] == ["B2B8B2EA"] and lo.relay_off_to_pin(relay, [relay, other])
    # the system's only relay stays on where it is
    p = lo.plan(cfg, [relay], [], {}, {"OTHILETH": {}}, {}, set(), [], {})
    assert not p["pins"] and not lo.relay_off_to_pin(relay, [relay])


def test_a_working_beacon_pinned_to_a_civ_body_is_carried_there():
    """Live 2026-10-23: beacons left at relays' L4 points in a dozen civilization systems with no vessel there; the civ
    beacon pass pins them (`at:<body>`) and a carrier from elsewhere picks one up and drops it at the body."""
    from rsweb import loadouts as lo
    b = {"device_code": "B1", "device_type": "ftl_beacon", "location": "KAM-5-L4", "status": "monitoring",
         "features": ["stow", "audit"], "available_commands": ["deploy", "stow"], "tags": ["at:kam-3"]}
    cv = {"device_code": "CV1", "device_type": "cargo_vessel", "location": "KAM-OORT", "status": "idle",
          "features": ["surge", "transport"], "available_commands": ["travel", "stow"], "stow_capacity": 9, "tags": []}
    assert not lo.placed(b) and lo.placed({**b, "location": "KAM-3"}) and lo.placed({**b, "tags": []})
    stars = {"KAM": {"entry_point": "KAM-5-L4"}}
    p = lo.plan({"phases": [], "systems": {}}, [b, cv], [], {}, stars, {}, set(), [], {})
    dl = p["deliveries"]
    assert len(dl) == 1 and dl[0]["devices"] == ["B1"], dl
    bodies = [s["body"] for s in lo.delivery_steps(dl[0], p["by_code"], stars, False)]
    assert {"command": "travel", "destination": "KAM-3"} in bodies and {"command": "deploy"} in bodies


def test_relay_is_activated_only_at_a_lagrange_point():
    from rsweb.ami_schedule import wakeups
    devs = [{"device_code": "R1", "device_type": "ftl_relay", "location": "AAA-3-L4", "status": "idle", "tags": ["home:aaa"],
             "available_commands": ["activate", "travel"]},
            {"device_code": "R2", "device_type": "ftl_relay", "location": "AAA-BELT-1", "status": "idle", "tags": ["home:aaa"],
             "available_commands": ["activate", "travel"]},
            {"device_code": "R3", "device_type": "ftl_relay", "location": "AAA-4-L5", "status": "relaying", "tags": [],
             "available_commands": ["activate"]}]
    w = {x["code"]: x for x in wakeups(devs, set(), {})}
    assert w.get("R1", {}).get("activate") and "R2" not in w and "R3" not in w


def test_census_merges_into_the_catalogue_and_lists_unexplored():
    from rsweb import census
    cat = {"stars": [{"designation": "SOL", "position": {"x": 0, "y": 0, "z": 0}},
                     {"designation": "OTHILETH", "position": {"x": -458.9, "y": -223.75, "z": 3.3}}]}
    known = {s["designation"]: s for s in CENSUS["stars"]}
    merged = census.merge(cat, known)
    by = {s["designation"]: s for s in merged["stars"]}
    assert by["ZALDANAL"]["from_census"] and "distance_from_replicant" not in by["ZALDANAL"]   # beyond the catalog: added
    assert by["OTHILETH"]["explored"] is True and by["OTHILETH"]["entry_point"] == "OTHILETH-1-L4"
    assert not by["OTHILETH"].get("from_census") and len(merged["stars"]) == 4
    assert census.seconds_per_ly(known) == pytest.approx(727 / 14.55)
    rows = census.unexplored(merged, {"OTHILETH"}, by["OTHILETH"]["position"], known)
    assert [r["designation"] for r in rows] == ["ZALDANAL", "LORQELYR", "SOL"]     # nearest first; SOL never visited
    assert rows[0]["distance"] == pytest.approx(5.83, abs=0.01) and 280 < rows[0]["eta"] < 300
    opts = census.destination_systems(merged, {"OTHILETH"}, {"OTHILETH"}, "OTHILETH-1-L4")
    assert [(o["value"], o["group"]) for o in opts] == [("OTHILETH", "Your systems"), ("ZALDANAL", "Unexplored"),
                                                        ("LORQELYR", "Unexplored"), ("SOL", "Unexplored")]
    assert "census" in opts[1]["label"]


def test_census_on_arrival_runs_once_per_system(client):
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.worker.sync_devices)
    arrival = {"id": "7777777777777-0", "event": "travel.arrived", "category": "travel", "device_code": "11ADA230",
               "device_type": "heaven_vessel", "location": "SOL-BELT-1", "star": "SOL",
               "payload": {"destination": "SOL-BELT-1", "origin": "ABOTEIN-OORT", "travel_type": "surge"},
               "created_at": iso(datetime.now(timezone.utc))}
    count = lambda: client.portal.call(client.app.state.db.fetchone,  # noqa: E731
                                       "SELECT COUNT(*) n FROM actions WHERE body LIKE '%stellar_census%'")["n"]
    client.portal.call(client.app.state.worker.handle_event, arrival)
    assert count() == 1
    assert client.portal.call(client.app.state.db.kv_get, "census")["SOL"]["device"] == "11ADA230"
    cat = client.portal.call(client.app.state.db.kv_get, "stars")
    assert any(s["designation"] == "ZALDANAL" and s.get("from_census") for s in cat["stars"])
    arrival["id"] = "7777777777777-1"
    client.portal.call(client.app.state.worker.handle_event, arrival)
    assert count() == 1                                           # SOL has had its census
    from rsweb import season                                      # a census from before a season reset is redone
    done = client.portal.call(client.app.state.db.kv_get, "census")
    done["SOL"]["at"] = iso(datetime.now(timezone.utc) - timedelta(hours=2))
    client.portal.call(client.app.state.db.kv_set, "census", done)
    client.portal.call(client.app.state.db.kv_set, season.RESET_KV, {"at": iso(datetime.now(timezone.utc) - timedelta(hours=1))})
    arrival["id"] = "7777777777777-2"
    client.portal.call(client.app.state.worker.handle_event, arrival)
    assert count() == 2
    # the catalog refresh keeps census stars
    client.portal.call(client.app.state.worker.sync_catalogue)
    assert any(s["designation"] == "ZALDANAL" for s in client.portal.call(client.app.state.db.kv_get, "stars")["stars"])
    page = client.get("/stars", headers=H).text
    assert "ZALDANAL" in page and "Census from SOL" in page and 'rules-panel?ids=census_on_arrival"' in page
    assert ">Stars</a>" in page                                   # Map › Stars sub-tab
    assert page.count('id="route-from"') == 1 and "route-from').value" in page   # one replicant dropdown for the list
    r = client.post("/stars/census", data={"device": "11ADA230"}, headers=HX)
    assert "unexplored: ZALDANAL" in r.text and count() == 3


def test_transit_trips_on_the_system_and_galaxy_maps():
    from rsweb import transit
    from rsweb.web import build_system_view
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(seconds=7060), now + timedelta(seconds=1423)   # 83 % of the live recall trip
    drones = [_moving(c, f"OTHDANAX-{o}", "OTHDANAX-OORT", [(f"OTHDANAX-{o}", "OTHDANAX-OORT", "cruise", 8483.6)], start, end)
              for c, o in (("A42C5AB6", "1-2"), ("C3BE3BEF", "1-2"))]
    ship = _moving("HV", "OTHDANAX-2", "FALQUORYX-1-L4",
                   [("OTHDANAX-2", "OTHDANAX-OORT", "cruise", 100), ("OTHDANAX-OORT", "FALQUORYX-1-L4", "surge", 800),
                    ("FALQUORYX-1-L4", "FALQUORYX-1-L4", "cruise", 100)], now - timedelta(seconds=500), now + timedelta(seconds=500),
                   "heaven_vessel")
    stale = _moving("OLD", "OTHDANAX-2", "OTHDANAX-3", [("OTHDANAX-2", "OTHDANAX-3", "cruise", 10)],
                    now - timedelta(hours=2), now - timedelta(hours=1))
    groups = transit.trips(drones + [ship, stale])
    assert [g["label"] for g in groups] == ["HV heaven vessel", "2× survey drone"]      # together; the old trip is over
    d = groups[1]
    assert d["progress"] == pytest.approx(0.832, abs=0.01) and 1400 < d["eta"] <= 1423
    leg = groups[0]["legs"][1]
    assert leg["type"] == "surge" and leg["t1"] - leg["t0"] == pytest.approx(800)
    pos = {"OTHDANAX": {"x": -456.1, "y": -220.2, "z": 1.6}, "FALQUORYX": {"x": -460.3, "y": -214.8, "z": 4.9}}
    gal = transit.galaxy_movers(groups, pos)
    assert len(gal) == 1 and gal[0]["origin"] == "OTHDANAX" and gal[0]["destination"] == "FALQUORYX-1-L4"
    assert [sg["a"] == sg["b"] for sg in gal[0]["segs"]] == [True, False, True]       # cruise legs stay at the star
    scan = {"planets": [{"designation": "OTHDANAX-1", "orbital_distance_au": .1}, {"designation": "OTHDANAX-2", "orbital_distance_au": .3}],
            "outer_system": {"oort": {"designation": "OTHDANAX-OORT", "distance_au": 1800}}}
    view = build_system_view("OTHDANAX", scan, [], [], [], None, groups, pos)
    ways = {m["label"]: m for m in view["movers"]}
    assert ways["2× survey drone"]["way"] == "local" and ways["2× survey drone"]["segs"][0][2:4] == [730, 30]   # to the Oort corner
    out = ways["HV heaven vessel"]
    assert out["way"] == "out" and out["other"] == "FALQUORYX" and len(out["segs"]) == 2   # cruise out, then surge to the rim
    rx, ry = out["segs"][1][2:4]
    assert rx < 380 and ry < 380            # FALQUORYX is to the -x, +y (map up) of OTHDANAX: up and to the left
    arriving = build_system_view("FALQUORYX", {}, [], [], [], None, groups, pos)["movers"][0]
    assert arriving["way"] == "in" and arriving["other"] == "OTHDANAX"


def test_moving_devices_show_on_both_maps(client):
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    client.post("/devices/11ADA230/command", data={"command": "travel", "f.destination": "ABOTEIN-3-L4"}, headers=HX)
    client.portal.call(client.app.state.worker.sync_devices)
    assert any(d.get("travel") for d in world.devices)
    m = client.get("/api/map.json", headers=H).json()
    assert m["moving"] and m["moving"][0]["destination"] == "ABOTEIN-3-L4" and m["moving"][0]["origin"] == "SOL"
    page = client.get("/systems/SOL", headers=H).text
    assert 'class="mover mover-out"' in page and "→ ABOTEIN-3-L4" in page
    assert 'id="opt-moving"' in client.get("/map", headers=H).text


def test_map_names_the_device_each_replicant_rides_in(client):
    # the wallpaper's follow setting tracks a replicant in transit by the ship hosting it
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    rep = client.get("/api/map.json?part=core", headers=H).json()["replicants"][0]["code"]
    next(d for d in world.devices if d["device_code"] == "11ADA230")["hosting_replicant"] = {"replicant_code": rep}
    client.portal.call(client.app.state.worker.sync_devices)
    reps = {r["code"]: r for r in client.get("/api/map.json?part=core", headers=H).json()["replicants"]}
    assert reps[rep]["host"] == "11ADA230"

def test_map_core_loads_your_systems_first(client):
    client.portal.call(client.app.state.worker.sync_devices)
    whole = {s["designation"] for s in client.get("/api/map.json?part=core", headers=H).json()["stars"]}
    mine = {s["designation"] for s in client.get("/api/map.json?part=core&scope=mine", headers=H).json()["stars"]}
    rest = {s["designation"] for s in client.get("/api/map.json?part=core&scope=rest", headers=H).json()["stars"]}
    assert "SOL" in mine and mine | rest == whole and not mine & rest and rest


def test_system_page_lists_your_devices_there(client):
    from rsweb.web import devices_in_system
    devs = [{"device_code": "A", "device_type": "mining_drone", "location": "SOL-BELT-1", "status": "mining (carbon)",
             "tags": ["fleet:m1"], "controller_device_code": "C"},
            {"device_code": "V", "device_type": "cargo_vessel", "location": "SOL-3", "status": "idle"},
            {"device_code": "R", "device_type": "survey_drone", "stowed_in_device_code": "V", "status": "stowed"},
            {"device_code": "X", "device_type": "mining_drone", "location": "FAL-1", "status": "idle"}]
    h = devices_in_system(devs, "SOL")
    assert [d["device_code"] for d in h["rows"]] == ["V", "R", "A"] and h["rows"][1]["_where"] == "aboard V"   # by place
    assert h["rows"][2]["_fleet"] == "m1" and dict((t, n) for t, n, _ in h["summary"])["mining_drone"] == 1
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get("/systems/SOL", headers=H).text
    assert 'id="devices-here"' in page and "Your devices here" in page


def test_prospects_page_scores_stationed_homes_for_review(client):
    eng = client.app.state.worker.automations
    client.portal.call(eng.save_fleets, [{"id": "h", "name": "Homers", "role": "mining", "home": "SOL", "station": True,
                                          "wants": {"mining_drone": 3}}])
    client.portal.call(client.app.state.worker.sync_devices)
    client.get("/systems/SOL", headers=H)
    page = client.get("/systems/prospects", headers=H).text
    row = page[page.index('href="/systems/SOL"'):].split("</tr>")[0]
    assert "your fleet here: Homers" in row and "⚑" in row and ">home<" in row


def test_another_players_hub_counts_as_a_ward():
    from rsweb import wards
    stars = {"stars": [{"designation": "CYGNUS", "has_hub": True}, {"designation": "TARAZEDAR", "has_ward": True},
                       {"designation": "SOL"}]}
    assert wards.foreign(stars, []) == {"CYGNUS", "TARAZEDAR"}
    our_hub = {"device_code": "H", "device_type": "system_hub", "location": "CYGNUS-5-L4", "status": "relaying"}
    assert wards.foreign(stars, [our_hub]) == {"TARAZEDAR"}                         # our own hub doesn't keep us out
    assert wards.foreign(stars, [{**our_hub, "status": "compacted"}]) == {"CYGNUS", "TARAZEDAR"}   # folded up: not ours there


def test_observatory_aims_and_found_stars(client):
    from rsweb.observatory import aim_vector
    here, there = {"x": 10, "y": -2, "z": 4}, {"x": 13, "y": 0, "z": 4}
    assert aim_vector("outward", here) is None and aim_vector("", here) is None   # omit: away from Sol
    assert aim_vector("sol", here) == [-10.0, 2.0, -4.0]
    assert aim_vector("sideways", here) == [0.0, 1.0, 0.0]
    assert aim_vector("star", here, there) == [3.0, 2.0, 0.0]
    import pytest
    with pytest.raises(ValueError):
        aim_vector("sol", {"x": 0, "y": 0, "z": 0})
    with pytest.raises(ValueError):
        aim_vector("star", here, None)
    # the command form offers the aims; the command sends the worked-out direction
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [
        {"designation": "SOL", "position": {"x": 0, "y": 0, "z": 0}}, {"designation": "ABOTEIN", "position": {"x": 3, "y": 4, "z": 0}}]})
    code = world.devices[0]["device_code"]
    form = client.get(f"/devices/{code}/command-form", params={"command": "prospect"}, headers=HX).text
    assert 'name="aim"' in form and "Toward Sol" in form and "ABOTEIN" in form
    client.post(f"/devices/{code}/command", data={"command": "prospect", "aim": "star", "aim_star": "abotein"}, headers=HX)
    row = client.portal.call(client.app.state.db.fetchone, "SELECT body FROM actions WHERE path=? ORDER BY id DESC LIMIT 1",
                             (f"/devices/{code}",))
    import json as _json
    assert _json.loads(row["body"]) == {"command": "prospect", "direction": [3.0, 4.0, 0.0]}
    # stars a prospect finds go on the map
    client.portal.call(client.app.state.worker.handle_event, {
        "id": "p-1", "event": "prospect.completed", "device_code": code, "location": "SOL-3-L4", "created_at": "2026-10-07T12:00:00+00:00",
        "payload": {"origin": "SOL", "stars_generated": 1,
                    "stars": [{"designation": "NEWSTAR", "position": {"x": 80, "y": 1, "z": 2}}]}})
    stars = {s["designation"] for s in client.portal.call(client.app.state.db.kv_get, "stars")["stars"]}
    assert "NEWSTAR" in stars


def test_catalogue_includes_observatory_finds_even_from_before(client):
    """Stars from every stored prospect.completed go on the map (finds from before 1.36 were never merged); a find with
    no position is reported, not plotted at Sol."""
    db = client.app.state.db
    client.portal.call(db.insert_event, {"id": "old-1", "event": "prospect.completed", "device_code": "OBS",
                                          "created_at": "2026-10-01T00:00:00+00:00",
                                          "payload": {"stars_generated": 2, "stars": [
                                              {"designation": "FARSTAR", "position": {"x": 90, "y": 0, "z": 1}}, "NOPOS"]}})
    client.portal.call(client.app.state.worker.sync_catalogue)
    cat = client.portal.call(db.kv_get, "stars")
    names = {s["designation"] for s in cat["stars"]}
    assert "FARSTAR" in names and "NOPOS" not in names
    assert cat["sources"]["observatory"] >= 1 and cat["sources"]["observatory_unplaced"] == ["NOPOS"]
    assert cat["sources"]["catalogue"] > 0
    data = client.get("/api/map.json", headers=H).json()
    assert data["sources"]["observatory"] >= 1
    r = client.post("/map/refresh", headers=HX)
    assert "found by your observatories" in r.text and "without a position" in r.text


def test_ward_or_hub_lock_blocks_contracts_not_trades(client):
    """Species interaction lock: other players can't complete location events where a ward or hub is."""
    import json as _json
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [{"designation": "SOL"},
                                                                        {"designation": "ABOTEIN", "has_hub": True}]})
    client.portal.call(eng.save_fleets, [{"id": "t", "name": "T", "role": "trade", "home": "SOL", "wants": {}}])
    r = client.post("/fleets/t/mission", data={"kind": "contract", "contract": _json.dumps(
        {"designation": "E1", "location": "ABOTEIN-3", "price": {"carbon": 5}})}, headers=HX)
    assert "species interaction lock" in r.text
    r = client.post("/fleets/t/mission", data={"kind": "trade", "trade": _json.dumps(
        {"name": "T1", "trade_code": "X", "location": "ABOTEIN-3", "price": {"carbon": 5}})}, headers=HX)
    assert "species interaction lock" not in r.text and "another player" not in r.text   # trades still go


def test_galaxy_map_loads_in_two_parts(client):
    client.portal.call(client.app.state.worker.sync_devices)
    core = client.get("/api/map.json", params={"part": "core"}, headers=H).json()
    assert core["stars"] and "per_star" not in core and "fleets" not in core and "drones" not in core["stars"][0]
    assert set(core["stars"][0]) <= {"designation", "name", "position", "color", "spectral_type", "region", "estimated_planets",
                                     "entry_point", "has_hub", "has_ward", "has_life", "explored", "from_census",
                                     "from_observatory", "devices", "infra", "reach", "scanned", "radius",
                                     "radius_measured"}
    over = client.get("/api/map.json", params={"part": "overlay"}, headers=H).json()
    assert "stars" not in over and {"per_star", "moving", "fleets", "supply"} <= set(over)
    assert any(v["drones"] for v in over["per_star"].values())
    full = client.get("/api/map.json", headers=H).json()                      # the old one-shot shape still works
    assert "drones" in full["stars"][0] and "fleets" in full


def test_trail_matches_departure_vectors_to_stars():
    from rsweb import trail as tl
    pos = {"HOME": {"x": 0, "y": 0, "z": 0}, "A": {"x": 4, "y": 2, "z": -3}, "B": {"x": -4, "y": 2, "z": 3},
           "FAR": {"x": 40, "y": 20, "z": -30}, "NEAR": {"x": 0.5, "y": 0, "z": 0}}
    # the blog's example: (0,0,0) → (4,2,-3) gives about (0.7, 0.4, -0.6)
    c = tl.candidates("HOME", "0.7,0.4,-0.6", pos)
    assert c[0]["star"] == "A" and c[0]["good"] and c[0]["in_relay"]
    assert c[1]["star"] == "FAR" and not c[1]["in_relay"]                 # same line, further: second
    assert all(x["star"] not in ("B", "NEAR") for x in c)                 # the wrong way
    assert tl.parse_vector({"x": 3, "y": 4, "z": 0}) == [0.6, 0.8, 0.0]
    audit = {"BC1": [{"id": 2, "travel_type": "departure", "replicant_code": "BILL", "location": "HOME-1",
                      "logged_at": "2026-10-08T02:00:00+00:00", "vector": "0.7,0.4,-0.6", "device_code": "V1"},
                     {"id": 1, "travel_type": "arrival", "replicant_code": "BILL", "location": "HOME-1",
                      "logged_at": "2026-10-08T01:00:00+00:00", "vector": None},
                     {"id": 0, "travel_type": "departure", "replicant_code": "SOMEONE", "location": "HOME-1",
                      "logged_at": "2026-10-08T03:00:00+00:00", "vector": "-1,0,0"}]}
    legs = tl.legs(audit, {"BC1": {"star": "HOME"}}, "BILL", pos)
    assert len(legs) == 1 and legs[0]["best"]["star"] == "A"
    assert tl.last_seen(audit, {"BC1": {"star": "HOME"}}, "BILL")["travel_type"] == "departure"


def test_wardhub_hub_shield_projection_and_ward_cap():
    from rsweb import wardhub as wh
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    hub = {"device_code": "HUB1", "device_type": "system_hub", "location": "SOL-5-L4", "status": "active",
           "operational_capacity": 100.0}
    evs = [{"event": "hub.activated", "device_code": "HUB1", "location": "SOL-5-L4", "payload": "{}",
            "created_at": "2026-10-01T00:00:00+00:00"},
           {"event": "hub.maintained", "device_code": "HUB1", "location": "SOL-5-L4",
            "payload": json.dumps({"resources_consumed": {"structural": 50}, "capacity": 80}),
            "created_at": "2026-10-18T00:00:00+00:00"}]
    h = wh.hubs([hub], evs, {"SOL": {"structural": 99}, "SOL-5-L4": {"structural": 20}}, now=now)[0]   # the hub's own spot counts
    assert not h["shielded"] and h["shield_until"].startswith("2026-10-08")
    assert h["capacity"] == 80 and h["projected"] == 60.0 and h["days_left"] == 6.0     # 2 days at −10 %/day
    assert h["short"] == {"structural": 30.0} and h["level"] == "warn"
    # inside the 7 days: shielded, no decay
    h = wh.hubs([hub], evs[:1], {}, now=datetime(2026, 10, 3, tzinfo=timezone.utc))[0]
    assert h["shielded"] and h["projected"] == 100.0 and h["level"] == "ok"
    w = wh.wards([_ward(f"W{i}", f"S{i}-OORT") for i in range(25)] + [_ward("WX", "SOL-OORT", "inactive"), hub,
                                                                     _ward("WH", "SOL-KUIPER")])
    assert w["active"] == 26 and w["free"] == 0 and w["clash"] == ["SOL"]
    assert wh.evicted({"evicted_miners": ["D1", {"device_code": "D2", "owner_name": "Sylphrena"}]}) == [
        {"device_code": "D1"}, {"device_code": "D2", "owner_name": "Sylphrena"}]


def test_wards_page_activate_cap_and_evictions(client):
    world = client.app.state.api.http._transport.app.state.world
    world.devices += [_ward("WA000001", "SOL-OORT", "inactive")] + [_ward(f"WF{i:06d}", f"STAR{i}-OORT") for i in range(24)]
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get("/wards", headers=H).text
    assert "24 of 25 active" in page and "/wards/WA000001/activate" in page
    world.evict_next = [{"device_code": "MD777777", "owner_name": "Sylphrena"}]
    r = client.post("/wards/WA000001/activate", headers=HX)
    assert "Evicted miners: MD777777 (Sylphrena)" in r.text
    notes = client.portal.call(client.app.state.db.fetchall, "SELECT title, link FROM notifications WHERE link='/wards'")
    assert any("MD777777" in n["title"] for n in notes)
    client.portal.call(client.app.state.worker.sync_devices)
    assert "25 of 25 active" in client.get("/wards", headers=H).text
    world.devices.append(_ward("WB000001", "LERNA-OORT", "inactive"))
    client.portal.call(client.app.state.worker.sync_devices)
    r = client.post("/wards/WB000001/activate", headers=HX)
    assert "25 wards are already active" in r.text
    assert client.post("/wards/WB000001/explode", headers=HX).status_code == 404


def test_hub_watch_pass_warns_once(client):
    world = client.app.state.api.http._transport.app.state.world
    world.devices.append({"device_code": "HUB00001", "device_type": "system_hub", "location": "SOL-5-L4", "status": "active",
                          "operational_capacity": 100.0, "available_commands": ["set_welcome_message"]})
    client.portal.call(client.app.state.worker.sync_devices)
    old = datetime.now(timezone.utc) - timedelta(days=12)
    w = client.app.state.worker
    client.portal.call(w.handle_event, {**_ev(901, "hub.activated", device="HUB00001", created=old), "location": "SOL-5-L4"})
    client.portal.call(w.handle_event, {**_ev(902, "hub.maintained", device="HUB00001",
                                              created=datetime.now(timezone.utc) - timedelta(days=4),
                                              resources_consumed={"rares": 500}, capacity=70), "location": "SOL-5-L4"})
    eng = w.automations
    out = client.portal.call(eng.hub_watch_pass)
    assert len(out) == 1 and "capacity about 30 %" in out[0] and "rares" in out[0]
    assert client.portal.call(eng.hub_watch_pass) == []                       # once per state
    page = client.get("/wards", headers=H).text
    assert "HUB00001" in page and "needs maintenance" in page and "down since" in page


def test_review_prospects_transit_and_star_prefixes():
    from rsweb import prospects, targets, transit
    assert prospects.VERDICT.get("learning", 8) == 8 and prospects.VERDICT["consider moving"] == 3
    d = {"device_code": "A", "travel": {"departed_at": "2026-10-08T00:00:00", "arrives_at": "2026-10-08T01:00:00",
                                        "route": [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}]}}
    legs = transit.trip(d, now=0)["legs"]
    assert [round(x["t1"] - x["t0"]) for x in legs] == [1800, 1800]          # not both at the destination at once
    assert targets.in_star("KEL-3", "KEL") and not targets.in_star("KELMORNEA-3", "KEL")


def test_pathing_choose_never_picks_an_excluded_system():
    from rsweb import pathing as pa
    xyz = {"HOME": (0, 0, 0), "GOOD": (5, 0, 0), "BEST_BEHIND": (-5, 0, 0), "OTHERS": (4, 1, 0), "NEXT": (4, -1, 0),
           "TARGET": (3, 0, 1), "WARD": (4, 0, 0), "FAR": (12, 0, 0), "UNSCANNED": (2, 0, 0)}
    relay = [{"device_code": f"R{s}", "device_type": "ftl_relay", "location": f"{s}-5-L4", "status": "relaying"}
             for s in ("HOME", "GOOD", "BEST_BEHIND", "OTHERS", "NEXT", "TARGET", "WARD", "UNSCANNED")]   # not FAR
    fleet = {"id": "me", "name": "Me", "role": "mining", "station": True, "home": "HOME"}
    fleets = [fleet, {"id": "o1", "name": "Other", "role": "mining", "home": "OTHERS"},
              {"id": "o2", "name": "Mover", "role": "mining", "home": "ELSEWHERE", "next_home": "NEXT"},
              {"id": "o3", "name": "Prospector", "role": "mining", "home": "ELSEWHERE2",
               "mission": {"status": "running", "targets": ["TARGET-BELT-1"]}}]
    rows = [{"star": s, "score": sc, "status": st, "distance": 1.0, "reasons": []} for s, sc, st in (
        ("BEST_BEHIND", 99, "ok"), ("OTHERS", 95, "ok"), ("NEXT", 94, "ok"), ("TARGET", 93, "ok"), ("WARD", None, "warded"),
        ("FAR", 92, "ok"), ("UNSCANNED", None, "unscanned"), ("HOME", 98, "ok"), ("GOOD", 50, "ok"))]
    heading = pa.heading_from("custom", None, custom=[1, 0, 0])
    star, why = pa.choose(fleet, fleets, rows, xyz, relay, heading, 60)
    assert star == "GOOD" and why[0] == "score 50"
    assert pa.choose(fleet, fleets, rows, xyz, relay, None, 60)[0] == "BEST_BEHIND"     # no heading: best anywhere served
    none, reasons = pa.choose(fleet, fleets, [r for r in rows if r["star"] != "GOOD"], xyz, relay, heading, 60)
    assert none is None and any("held by another fleet" in r for r in reasons) and any("outside relay" in r for r in reasons)
    # owner 2026-10-19: no automatic assignments outside the network — a relay without a replicant on its network
    # doesn't count, only the commandable systems do
    assert pa.choose(fleet, fleets, rows, xyz, relay, heading, 60, reach={"HOME", "BEST_BEHIND"})[0] is None
    assert pa.choose(fleet, fleets, rows, xyz, relay, None, 60, reach={"HOME", "GOOD"})[0] == "GOOD"
    assert pa.dry("HOME", [], [], {"HOME": "x"}, set(), []) and not pa.dry("HOME", [], [], {}, set(), ["HOME-BELT-1"])
    assert pa.dry("HOME", [], [{"star": "HOME", "verdict": "consider moving"}], {}, set(), ["HOME-BELT-1"])
    assert pa.heading_from("outward", (3, 4, 0)) == [0.6, 0.8, 0.0]


def test_pathing_pass_prospects_then_relocates(client):
    from rsweb import prospects
    eng, db = client.app.state.worker.automations, client.app.state.db

    async def commandable(devices, stars=None, without=None):   # a replicant on the relay network: every system it covers
        return {"HOMEA", "AHEADB", "BEHINDC"}
    eng.commandable = commandable
    stars = {"HOMEA": (0, 0, 0), "AHEADB": (5, 0, 0), "BEHINDC": (-5, 0, 0)}
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": k, "position": dict(zip("xyz", v))} for k, v in stars.items()]})
    belt = {"asteroid_belt": {"belts": [{"designation": "X", "density": "dense", "resources": {"structural": "rich", "rares": "high"}}]}}
    for s in ("AHEADB", "BEHINDC"):
        client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                           (s, json.dumps(belt), "2026-10-08T00:00:00+00:00"))
    devices = [{"device_code": "OBS00001", "device_type": "galactic_observatory", "location": "HOMEA-5-L4", "status": "idle",
                "tags": ["fleet:m1"], "available_commands": ["prospect", "compact", "unfurl"]},
               {"device_code": "REL00001", "device_type": "ftl_relay", "location": "HOMEA-5-L4", "status": "relaying", "tags": []}]
    devices += [{"device_code": f"REL{s}", "device_type": "ftl_relay", "location": f"{s}-5-L4", "status": "relaying", "tags": []}
                for s in ("AHEADB", "BEHINDC")]
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(eng.save_fleets, [{"id": "m1", "name": "Miner 1", "role": "mining", "home": "HOMEA", "station": True, "wants": {}}])
    r = client.post("/fleets/m1/path", data={"heading_kind": "star", "heading_star": "AHEADB", "cone": "60",
                                             "auto_relocate": "on", "auto_prospect": "on"}, headers=HX)
    assert r.status_code == 200
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "m1")
    assert f["heading"]["vector"] == [1.0, 0.0, 0.0] and f["auto_relocate"] and f["auto_prospect"]
    prospects.forget_inputs()
    out = client.portal.call(eng.observatory_pass)
    assert out == ["OBS00001: prospecting from HOMEA, direction 1"]        # along the heading first
    job = next(j for j in client.portal.call(eng.jobs) if j["rule"] == "observatory")
    assert job["steps"][0]["body"] == {"command": "prospect", "direction": [1.0, 0.0, 0.0]}
    # the home runs dry (no belt, no salvage) — after 2 h the fleet picks the system ahead, then moves after the grace
    client.portal.call(db.kv_set, "salvage_state", {"nothing": {"HOMEA": datetime.now(timezone.utc).isoformat()}})
    client.portal.call(eng.pathing_pass, True)
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "m1")
    assert f["depleted_since"] and not f.get("next_home")
    items = client.portal.call(eng.fleets)
    for x in items:
        if x["id"] == "m1":
            x["depleted_since"] = "2026-10-01T00:00:00+00:00"
    client.portal.call(eng.save_fleets, items)
    out = client.portal.call(eng.pathing_pass, True)
    assert out == ["Miner 1: next home AHEADB"]                            # not BEHINDC, though it scores the same
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Next home <b><a href=\"/systems/AHEADB\">AHEADB</a></b>" in page and "/fleets/m1/next-home" in page
    r = client.post("/fleets/m1/next-home", data={"action": "now"}, headers=HX)
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "m1")
    assert f["home"] == "AHEADB" and f["prev_home"] == "HOMEA" and not f.get("next_home")
    notes = client.portal.call(db.fetchall, "SELECT title FROM notifications WHERE link='/fleets'")
    assert not any("moving home: HOMEA → AHEADB" in n["title"] for n in notes)   # announced ahead only (1.381.0)


def test_travel_shows_on_the_map_straight_away(client):
    """The travel command's answer is put on the cached device at once (the device sync is up to a minute away)."""
    db = client.app.state.db
    devices = client.portal.call(db.kv_get, "devices")
    for d in devices:
        if d["device_code"] == "2AC61212":
            d.pop("travel", None)
            d["status"] = "idle"
    client.portal.call(db.kv_set, "devices", devices)
    r = client.post("/devices/2AC61212/command", data={"command": "travel", "f.destination": "ABOTEIN-3"}, headers=HX)
    assert r.status_code == 200
    d = next(x for x in client.portal.call(db.kv_get, "devices") if x["device_code"] == "2AC61212")
    assert d["travel"]["destination"] == "ABOTEIN-3" and d["travel"]["departed_at"] and d["status"] == "travelling"
    assert "attached_devices" not in d["travel"]
    ov = client.get("/api/map.json?part=overlay", headers=H).json()
    assert any("2AC61212" in m["label"] for m in ov["moving"])


def test_prospect_directions():
    from rsweb import prospecting as pr
    import math
    h = [1.0, 0.0, 0.0]
    d = pr.directions(h)
    assert len(d) == 14 and d[0] == h and d[-1] == [-1.0, 0.0, 0.0]
    ang = lambda v: round(math.degrees(math.acos(max(-1, min(1, sum(a * b for a, b in zip(v, h)))))))  # noqa: E731
    assert [ang(v) for v in d[1:7]] == [45] * 6 and [ang(v) for v in d[7:13]] == [90] * 6
    assert pr.base_heading(None, (3, 4, 0)) == [0.6, 0.8, 0.0]               # no heading: outward from Sol


@pytest.mark.parametrize("obs_type", ["galactic_observatory", "parallax_array"])   # a parallax array is an upgraded observatory
def test_observatory_prospects_each_direction_then_compacts(client, obs_type):
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": "HOMEA", "position": {"x": 0, "y": 5, "z": 0}}]})
    obs = {"device_code": "OBS00001", "device_type": obs_type, "location": "HOMEA-5-L4", "status": "idle",
           "tags": ["fleet:ex"], "available_commands": ["prospect", "compact", "unfurl"]}
    client.portal.call(db.kv_set, "devices", [obs])
    client.portal.call(eng.save_fleets, [{"id": "ex", "name": "Explorers", "role": "explore", "home": "HOMEA", "wants": {}}])

    def last_job():
        return [j for j in client.portal.call(eng.jobs) if j["rule"] == "observatory"][-1]

    def finish(stars=None, error=None):
        jobs = client.portal.call(eng.jobs)
        j = jobs[-1]
        j["status"] = "failed" if error else "done"
        if error:
            j["steps"][0].update({"status": "failed", "error": error})
        client.portal.call(eng.save_jobs, jobs)
        if stars is not None:
            client.portal.call(client.app.state.worker.handle_event, {**_ev(990 + len(jobs), "prospect.completed", "OBS00001",
                                                                              datetime(2026, 10, 8, 12, tzinfo=timezone.utc),
                                                                              stars=[{"designation": s, "position": {"x": 1, "y": 1, "z": 1}} for s in stars])})

    assert client.portal.call(eng.observatory_pass) == ["OBS00001: prospecting from HOMEA, direction 1"]
    assert last_job()["steps"][0]["body"]["direction"] == [0.0, 1.0, 0.0]     # outward from Sol: no heading set
    finish(error="No new stars visible from this location")                    # too dense that way: the next direction
    assert client.portal.call(eng.observatory_pass) == ["OBS00001: prospecting from HOMEA, direction 2"]
    finish(stars=["NEWSTAR1"])                                                  # one prospect per star: HOMEA is used up
    out = client.portal.call(eng.observatory_pass)
    assert out == ["OBS00001: compacting (every direction tried in HOMEA)"]
    pf = client.portal.call(db.kv_get, "prospected_from")
    assert pf["HOMEA"]["by"] == "OBS00001" and pf["HOMEA"]["found"] == 1
    assert client.get("/api/map.json?part=core", headers=H).json()["stars"][0]["prospected_from"]["found"] == 1
    assert "done in HOMEA" in client.get("/fleets/details?all=1", headers=H).text


def test_prospect_cones_on_the_galaxy_map(client):
    from rsweb.observatory import live_prospect, reach_estimate
    pos = {"HOME": {"x": 0, "y": 0, "z": 0}}
    finds = [{"origin": "HOME", "direction": [1, 0, 0],
              "stars": [{"designation": f"S{i}", "position": {"x": 10 + i, "y": i, "z": 0}} for i in range(6)]}]
    reach, half, learned = reach_estimate(finds, pos)
    assert learned and reach == 16.0 and 0 < half < 30
    assert reach_estimate([], pos) == (25.0, 30.0, False)                  # nothing learned yet: the guess
    db = client.app.state.db
    live = {"completes_at": "2099-10-08T23:34:17-04:00", "direction": [-0.9068, -0.4214, 0.0094], "eta_seconds": 10284,
            "origin": "SOL", "progress_percent": 76.2, "started_at": "2026-10-08T11:34:17-04:00"}
    assert live_prospect({"device_code": "O", "prospect": live}) == live
    devices = client.portal.call(db.kv_get, "devices")
    devices.append({"device_code": "OBSV0001", "device_type": "galactic_observatory", "location": "SOL-3-L4",
                    "status": "prospecting", "prospect": live, "tags": []})
    client.portal.call(db.kv_set, "devices", devices)
    ov = client.get("/api/map.json?part=overlay", headers=H).json()
    p = ov["prospects"][0]
    assert p["code"] == "OBSV0001" and p["origin"] == "SOL" and p["direction"] == live["direction"]
    assert p["t1"] > p["t0"] and p["reach"] == 25.0 and not p["learned"]
    assert 'id="opt-prospect"' in client.get("/map", headers=H).text


def test_contract_approvals_go_by_location_and_beacon(client):
    """Joe 2026-10-31: one species at several bodies ticked and unticked together; approve per location instead, and
    optionally every location with a beacon of yours deployed there."""
    from rsweb import gameevents as gev
    db = client.app.state.db
    scan = {"planets": [{"designation": "VETH-2", "life_stage": "intelligent", "species": "Veth"},
                        {"designation": "VETH-4", "life_stage": "intelligent", "species": "Veth"},
                        {"designation": "VETH-5", "life_stage": "intelligent", "species": "Orn"}]}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("VETH", json.dumps(scan), "2026-10-09"))
    client.portal.call(db.kv_set, "contract_approvals", {"species:veth": {"at": "x"}})   # saved before 1.321.0
    ok = lambda loc: client.portal.call(gev.approved, db, {"location": loc, "designation": f"{loc}-EVT-001"})
    assert ok("VETH-2") and ok("VETH-4") and not ok("VETH-5")
    assert set(client.portal.call(db.kv_get, "contract_approvals")) == {"body:VETH-2", "body:VETH-4"}   # spread once
    client.post("/game-events/approve", data={"key": "body:VETH-4", "on": ""}, headers=HX)
    assert ok("VETH-2") and not ok("VETH-4")                                   # the other colony keeps its approval
    client.portal.call(db.kv_set, "devices", [{"device_code": "BC1", "device_type": "communication_beacon",
                                               "location": "VETH-5", "status": "monitoring"}])
    client.post("/game-events/approve", data={"key": gev.BEACON_KEY, "on": "1"}, headers=HX)
    assert ok("VETH-5") and not ok("VETH-4")
    page = client.get("/game-events", headers=H).text
    assert "Auto-approve contracts at every location where a beacon" in page and "Veth at VETH-4" in page


def test_advancing_explorer_moves_home_to_farthest_star_ahead(client):
    eng, db = client.app.state.worker.automations, client.app.state.db
    stars = {"HOMEA": (0, 0, 0), "NEAR1": (5, 0, 0), "FAR1": (20, 1, 0), "TOOFAR": (60, 0, 0), "BEHIND": (-10, 0, 0)}
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": k, "position": dict(zip("xyz", v))} for k, v in stars.items()]})
    devices = [{"device_code": "OBSX", "device_type": "galactic_observatory", "location": "HOMEA-3-L4", "status": "idle",
                "tags": ["fleet:ex"], "available_commands": ["prospect", "compact", "unfurl"]},
               {"device_code": "VES1", "device_type": "heaven_vessel", "location": "HOMEA-3-L4", "status": "idle", "tags": ["fleet:ex"],
                "features": ["surge", "cruise", "stow"], "stow_capacity": 10},
               {"device_code": "PLT1", "device_type": "surge_plate", "location": "HOMEA-3-L4", "status": "idle", "tags": ["fleet:ex"],
                "features": ["surge", "attach"], "attach_capacity": 1},   # the observatory can't stow: it attaches
               {"device_code": "RLY1", "device_type": "ftl_relay", "location": "HOMEA-3-L4", "status": "relaying", "tags": ["fleet:ex"]}]
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(db.kv_set, "replicants", {"R1": {"name": "bob", "hosted_device_code": "VES1"}})   # rides along
    client.portal.call(eng.save_fleets, [{"id": "ex", "name": "Pathfinder", "role": "explore", "home": "HOMEA", "wants": {}}])
    r = client.post("/fleets/ex/path", data={"heading_kind": "custom", "heading_vec": "1, 0, 0", "cone": "30",
                                             "advance": "on", "hop_ly": "30"}, headers=HX)
    assert r.status_code == 200
    assert "advance along the heading" in client.get("/fleets/details?all=1", headers=H).text
    client.portal.call(db.kv_set, "observatory_runs", {"OBSX": {"star": "HOMEA", "used": list(range(14)), "idx": 0, "found": 3}})
    out = client.portal.call(eng.observatory_pass)
    assert out == ["Pathfinder: advancing HOMEA → FAR1"]                     # farthest ahead within 30 ly, not NEAR1
    f = client.portal.call(eng.fleets)[0]
    assert f["home"] == "FAR1" and f["prev_home"] == "HOMEA" and f["station"] and f["advanced_from"] == ["HOMEA"]
    m = f["mission"]
    assert m["relocate"] and m["targets"] == ["FAR1"] and m["status"] == "running"   # one move, everyone together
    from rsweb import fleets as fl
    assert fl.next_phase("explore", {**m, "phase": "travel"}) == "deploy"
    assert fl.next_phase("explore", {**m, "phase": "deploy"}) is None               # unloaded at the new home: done
    assert client.portal.call(eng.observatory_pass) == []                          # the mission handles the observatory
    job = [j for j in client.portal.call(eng.jobs) if "leave a relay" in j["title"]][0]
    assert job["steps"][0]["body"]["configuration"] == {"add_tags": ["at:homea-3-l4"], "remove_tags": ["fleet:ex"]}


def test_path_forward_form_shows_the_saved_heading(client):
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": "HOMEA", "position": {"x": 3, "y": 4, "z": 0}},
                                                      {"designation": "TGT", "position": {"x": 9, "y": 4, "z": 0}}]})
    client.portal.call(eng.save_fleets, [{"id": "m1", "name": "Miner 1", "role": "mining", "home": "HOMEA", "station": True, "wants": {}}])
    client.post("/fleets/m1/path", data={"heading_kind": "star", "heading_star": "TGT", "cone": "45"}, headers=HX)
    page = client.get("/fleets/details?all=1", headers=H).text
    assert '<option value="star" selected>' in page and 'name="heading_star" placeholder="star" size="10" list="path-stars" value="TGT"' in page
    items = client.portal.call(eng.fleets)
    items[0]["home"] = "ELSEWHERE"                                           # the fleet moved since
    client.portal.call(eng.save_fleets, items)
    client.post("/fleets/m1/path", data={"heading_kind": "star", "heading_star": "TGT", "cone": "30"}, headers=HX)
    f = client.portal.call(eng.fleets)[0]
    assert f["heading"]["vector"] == [1.0, 0.0, 0.0] and f["cone"] == 30     # unchanged choice: vector kept, cone saved


def test_working_relay_is_switched_off_before_boarding():
    from rsweb import fleets as fl
    f = {"id": "s", "role": "explore", "home": "AAA"}
    devs = [{"device_code": "MF", "device_type": "mobile_fleet", "location": "AAA-3-L4", "status": "idle", "tags": ["fleet:s"],
             "attach_capacity": 36, "features": ["surge"]},
            {"device_code": "RR", "device_type": "ftl_relay", "location": "AAA-3-L4", "status": "relaying", "tags": ["fleet:s"],
             "available_commands": ["deactivate", "travel"]}]
    steps, _ = fl.assemble_steps(f, devs)
    names = [s["name"] if "name" in s else s.get("label", "") for s in steps]
    i = next(i for i, s in enumerate(steps) if (s.get("body") or {}).get("command") == "deactivate")
    j = next(i for i, s in enumerate(steps) if "RR" in str(s.get("body")) and (s.get("body") or {}).get("command") != "deactivate")
    assert i < j, names


def test_console_presets_relay_links_matrix_moves_and_megastructures(client):
    """Console presets with fill-in blanks; a relay's live links; transfer / teleport into an empty matrix;
    megastructure contribution and leaderboard; attach_used counts (v3.5.1)."""
    from rsweb import fleets as fl
    page = client.get("/console", headers=H).text
    assert 'data-p="/devices/{relay}/network"' in page and 'id="c-dev"' in page and "Simulations" in page
    assert 'data-p="/devices/{locker}/retrieve"' in page and "Open an equipment locker" in page   # 1.204.0
    assert 'data-p="/devices/{controller}/trades/{trade}"' in page and "/leaderboards/colony_moon" in page   # 1.206.0
    r = client.get("/map/relays/AAAA1111/network", headers=HX).text
    assert "relaying" in r and "MUROPE" in r
    r = client.post("/replicants/77F75255/move-to", data={"target": "b22a1198", "how": "transfer"}, headers=HX)
    row = client.portal.call(client.app.state.db.fetchone, "SELECT path, body FROM actions ORDER BY id DESC LIMIT 1")
    assert row["path"] == "/replicants/77F75255/transfer" and '"B22A1198"' in row["body"]
    r = client.post("/locations/FLARM-OBJ-1/contribute", data={"devices": ["A473F411"]}, headers=HX)
    row = client.portal.call(client.app.state.db.fetchone, "SELECT path, body FROM actions ORDER BY id DESC LIMIT 1")
    assert row["path"] == "/locations/FLARM-OBJ-1/contribute" and "A473F411" in row["body"]
    assert "Bob-19" in client.get("/megastructures", headers=H).text
    c = {"device_code": "C", "device_type": "surge_carrier", "attach_capacity": 4, "attach_used": 3}
    assert fl.seats(c, [])["attach"] == 1


def test_hubs_link_by_distance_and_hub_links_are_marked(client):
    """Live 2026-10-15: hubs link by distance (15 ly), not across the galaxy. A hub's links are marked; two relays both
    linked to one hub don't show their own link on the map."""
    from rsweb import relaynet
    pos = {"FAL": {"x": 0, "y": 0, "z": 0}, "SOL": {"x": 500, "y": 0, "z": 0}, "NEAR": {"x": 506, "y": 0, "z": 0},
           "NEAR2": {"x": 503, "y": 4, "z": 0}}
    devs = [{"device_code": "H1", "device_type": "system_hub", "location": "FAL-3-L4", "status": "active"},
            {"device_code": "H2", "device_type": "system_hub", "location": "SOL-3-L4", "status": "active"},
            {"device_code": "R1", "device_type": "ftl_relay", "location": "NEAR-2-L4", "status": "relaying"},
            {"device_code": "R2", "device_type": "ftl_relay", "location": "NEAR2-2-L4", "status": "relaying"}]
    rn = relaynet.networks(devs, pos)
    assert len(rn["networks"]) == 2                              # FAL's hub is 500 ly away: its own network
    assert all(l.get("hub") for l in rn["links"] if "SOL" in (l["a"], l["b"]))
    db = client.app.state.db
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": k, "position": v} for k, v in pos.items()]})
    links = client.get("/api/map.json?part=overlay", headers=H).json()["relay_net"]["links"]
    assert {frozenset((l["a"], l["b"])) for l in links} == {frozenset(("SOL", "NEAR")), frozenset(("SOL", "NEAR2"))}


def test_prospector_lets_go_of_an_observatory_still_towed(client, monkeypatch):
    """Snapshot 2026-11-12 17:03: Prospectors 3 and 4 sat folded on their surge platforms at a star they never prospected
    from, nothing running. The pass detaches it there (observatory_pass unfurls it); one still towed at a prospected star moves on
    without a second attach."""
    eng, db = client.app.state.worker.automations, client.app.state.db
    stars = [{"designation": "HOME", "position": {"x": 0, "y": 0, "z": 0}},
             {"designation": "NEAR", "position": {"x": 5, "y": 0, "z": 0}},
             {"designation": "NEXT", "position": {"x": 0, "y": 6, "z": 0}}]
    client.portal.call(db.kv_set, "stars", {"stars": stars})
    obs = {"device_code": "OBS1", "device_type": "parallax_array", "location": "NEAR-2-L4", "status": "compacted",
           "attached_to_device_code": "CAR1", "tags": ["fleet:pro"], "available_commands": ["prospect", "unfurl"]}
    car = {"device_code": "CAR1", "device_type": "surge_platform", "location": "NEAR-2-L4", "status": "idle",
           "features": ["surge"], "attach_capacity": 4, "tags": ["fleet:pro"]}
    client.portal.call(db.kv_set, "devices", [obs, car])
    client.portal.call(db.kv_set, "prospected_from", {"HOME": {"at": "2026-10-15T00:00:00+00:00", "outcome": "3 new star(s)"}})
    client.portal.call(eng.save_fleets, [{"id": "pro", "name": "Prospector 3", "role": "prospector", "home": "HOME", "wants": {}}])
    async def covered(devices, stars=None):
        return {"HOME", "NEAR", "NEXT"}
    monkeypatch.setattr(eng, "commandable", covered)
    out = client.portal.call(eng.prospector_pass)
    assert out == ["Prospector 3: detaching at NEAR"]
    job = [j for j in client.portal.call(eng.jobs) if "put observatory" in j["title"]][-1]
    assert [s["body"]["command"] for s in job["steps"] if s.get("body")][:1] == ["detach"]
    # already prospected from NEAR, still towed: the next move flies on without attaching again
    js = client.portal.call(eng.jobs)
    for j in js:
        j["status"] = "done"
    client.portal.call(eng.save_jobs, js)
    client.portal.call(db.kv_set, "prospected_from", {"HOME": {"at": "2026-10-15T00:00:00+00:00"},
                                                      "NEAR": {"at": "2026-10-16T00:00:00+00:00"}})
    out = client.portal.call(eng.prospector_pass)
    assert out == ["Prospector 3: NEAR → NEXT"]
    job = [j for j in client.portal.call(eng.jobs) if "prospecting tour" in j["title"]][-1]
    assert [s["body"]["command"] for s in job["steps"] if s.get("body")][:2] == ["travel", "detach"]


def test_observatory_moves_on_soon_after_a_prospect_without_a_result(client):
    """Snapshot 2026-11-12 20:11: Prospectors 1, 2 and 5 sat unfurled and idle for hours: a finished prospect whose
    prospect.completed never matched waited 6 h. Now 15 min past its end (1 h from its start when the end is unknown),
    then the star counts as prospected and the observatory packs up."""
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": "FAL", "position": {"x": 0, "y": 0, "z": 0}},
                                                      {"designation": "CYR", "position": {"x": 5, "y": 0, "z": 0}}]})
    obs = {"device_code": "OBS1", "device_type": "parallax_array", "location": "CYR-1-L4", "status": "idle",
           "tags": ["fleet:prospector-1"], "available_commands": ["compact", "prospect", "unfurl"]}
    client.portal.call(db.kv_set, "devices", [obs])
    client.portal.call(eng.save_jobs, [{"id": "obsjob", "rule": "observatory", "title": "x", "status": "done", "steps": [],
                                        "meta": {"devices": ["OBS1"]}}])
    ago = lambda **k: (datetime.now(timezone.utc) - timedelta(**k)).isoformat(timespec="seconds")
    run = {"star": "CYR", "used": [], "idx": 0, "job": "obsjob", "seq0": 0, "at": ago(minutes=50),
           "live": {"completes_at": ago(minutes=5)}}
    client.portal.call(db.kv_set, "observatory_runs", {"OBS1": dict(run)})
    client.portal.call(eng.save_fleets, [{"id": "prospector-1", "name": "Prospector 1", "role": "prospector", "home": "FAL"}])
    assert client.portal.call(eng.observatory_pass) == []                     # 5 min past its end: still waiting
    assert "waiting for its result" in client.portal.call(db.kv_get, "observatory_runs")["OBS1"]["note"]
    run["live"]["completes_at"] = ago(minutes=20)
    client.portal.call(db.kv_set, "observatory_runs", {"OBS1": dict(run)})
    out = client.portal.call(eng.observatory_pass)
    assert out and "compacting" in out[0]
    assert "CYR" in client.portal.call(db.kv_get, "prospected_from")


def test_a_relay_out_of_range_is_a_network_of_its_own():
    """Owner 2026-10-15: a relay the game reports out of range starts its own network (its own color)."""
    from rsweb import relaynet
    pos = {"A": {"x": 0, "y": 0, "z": 0}, "B": {"x": 3, "y": 0, "z": 0}, "K": {"x": 40, "y": 0, "z": 0}}
    devs = [{"device_code": "R1", "device_type": "ftl_relay", "location": "A-1-L4", "status": "relaying"},
            {"device_code": "R2", "device_type": "ftl_relay", "location": "B-1-L4", "status": "relaying"},
            {"device_code": "R3", "device_type": "ftl_relay", "location": "K", "status": "out_of_range"}]
    assert "K" not in relaynet.networks(devs, pos)["of"]         # out of range reads the same switched off
    devs[2]["_on"] = True                                         # … switched on (web.relays_on): its own network
    rn = relaynet.networks(devs, pos)
    assert rn["of"]["A"] == rn["of"]["B"] != rn["of"]["K"]
    assert len({n["color"] for n in rn["networks"]}) == 2


def test_relays_switched_off_get_a_red_ring_unless_their_system_has_a_working_one(client):
    """Owner 2026-10-15: a relay switched off gets a red ring, but not in a system with a working relay of yours."""
    import json as _json
    from types import SimpleNamespace
    from rsweb import web
    db = client.app.state.db
    devs = [{"device_code": "R1", "device_type": "ftl_relay", "location": "A-1-L4", "status": "relaying"},
            {"device_code": "R0", "device_type": "ftl_relay", "location": "A-1-L4", "status": "inactive"},
            {"device_code": "R2", "device_type": "ftl_relay", "location": "B-1-L4", "status": "inactive"},
            {"device_code": "R3", "device_type": "ftl_relay", "location": "K", "status": "out_of_range"},
            {"device_code": "R4", "device_type": "ftl_relay", "location": "M", "status": "out_of_range"}]
    client.portal.call(db.execute, "INSERT INTO actions(at, user, method, path, body, status, response) VALUES(?,?,?,?,?,?,?)",
                       ("2026-10-15T16:00:00+00:00", "you", "POST", "/devices/R3", _json.dumps({"command": "activate"}), 200, "{}"))
    marked = client.portal.call(web.relays_on, db, devs)
    assert [d["device_code"] for d in marked if d.get("_on")] == ["R3"]
    pos = {"A": {"x": 0, "y": 0, "z": 0}, "B": {"x": 30, "y": 0, "z": 0}, "K": {"x": 60, "y": 0, "z": 0},
           "M": {"x": 90, "y": 0, "z": 0}}
    req = SimpleNamespace(app=client.app)
    out = client.portal.call(web.relay_overlay, req, {"devices": devs, "replicants": {}}, pos)
    assert out["off"] == ["B", "M"]   # A has a working relay; K is out of range but on: a network of its own
    # a replicant visits M: the list shows R4 relaying for a while; back out of range it stays on
    w = client.app.state.worker
    client.portal.call(w._note_relay_states, [dict(devs[4], status="relaying")])
    out = client.portal.call(web.relay_overlay, req, {"devices": devs, "replicants": {}}, pos)
    assert out["off"] == ["B"] and any(n["star"] == "M" for n in out["nodes"])


def test_megastructures_for_the_map(client):
    """Owner 2026-10-15: a symbol on the galaxy map where megastructures are."""
    import json as _json
    from rsweb import megas
    db = client.app.state.db
    scan = {"planets": [], "objects": [{"designation": "FLARM-OBJ-1", "location_type": "megastructure"},
                                       {"designation": "FLARM-OBJ-2", "location_type": "object"}]}
    assert set(megas.in_scan(scan)) == {"FLARM-OBJ-1"}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("FLARM", _json.dumps(scan), "2026-10-15T00:00:00+00:00"))
    client.portal.call(db.insert_event, {"id": "m1", "event": "megastructure.contributed", "location": "VEGA-OBJ-4",
                                          "payload": {"accepted_count": 1}})
    got = client.portal.call(megas.known, db)
    assert got["FLARM"] == ["FLARM-OBJ-1"] and got["VEGA"] == ["VEGA-OBJ-4"] and got["MIRFAKA"] == ["MIRFAKA-OBJ-1"]

def test_unescorted_trip_does_not_leave_for_a_dark_system(client):
    from rsweb.automations import step
    eng = client.app.state.worker.automations
    sent = []

    async def fake_send(method, path, body, why):
        sent.append(path)
        return True, {}, None
    orig = eng.send
    eng.send = fake_send
    try:
        st = step("F1 → NOWHERE-3-1", "/devices/F1", {"command": "travel", "destination": "NOWHERE-3-1"}, critical=True)
        st["need_comms"] = "NOWHERE"
        job = client.portal.call(eng.create_job, "logistics", "dark trip", "F1", [st], {"devices": ["F1"]}, True)
        j = next(x for x in client.portal.call(eng.jobs) if x["id"] == job["id"])
        assert j["status"] == "failed" and sent == [] and "NOWHERE is out of comms range" in j["steps"][0]["error"]
    finally:
        eng.send = orig


def test_simulation_run_started_elsewhere_is_taken_over(client):
    eng = client.app.state.worker.automations
    r = client.app.state.api   # start it in the game only, as if the app's record had been lost
    client.portal.call(lambda: r.request("POST", "/devices/F4791EDF/simulate",
                                         json_body={"scenario": "mining_rush", "replicant_code": "77F75255"}))
    eng._sim_adopt_t = 0
    out = client.portal.call(eng.sim_pass)
    assert any("took over Mining Rush #42" in x for x in out), out
    s = client.portal.call(eng.sim_state)
    assert s["active"]["id"] == 42 and s["active"]["replicant"] == "77F75255" and s["active"]["adopted"]
    assert "Runs in the game" in client.get("/simulations", headers=H).text


def test_star_sizes_on_the_maps(client):
    """Stars are drawn by radius: a scan's radius_solar when known, else the typical radius of the spectral class."""
    from rsweb.web import build_system_view, star_radius
    assert star_radius({"spectral_type": "M3V"}) == (0.4, False)
    assert star_radius({"spectral_type": "K1III"}) == (8.0, False)
    assert star_radius({"spectral_type": "G2"}, 1.4) == (1.4, True)
    assert star_radius({}) == (None, False)
    sol = build_system_view("SOL", {"star": {"spectral_type": "G2", "radius_solar": 1.0}}, [], [])
    red = build_system_view("RED", {"star": {"spectral_type": "M5V"}}, [], [])
    assert sol["star_r"] == 12 and 5 <= red["star_r"] < 12 and not red["star_radius"]["measured"]
    db = client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": "BIG", "position": {"x": 1, "y": 0, "z": 0},
                                                       "spectral_type": "B1"},
                                                      {"designation": "ODD", "position": {"x": 9, "y": 0, "z": 0}}]})
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("BIG", json.dumps({"star": {"radius_solar": 6.2}}), "2026-10-17"))
    stars = {s["designation"]: s for s in client.get("/api/map.json?part=core", headers=H).json()["stars"]}
    assert stars["BIG"]["radius"] == 6.2 and stars["BIG"]["radius_measured"] and "radius" not in stars["ODD"]


def test_build_order_spreads_out_over_systems():
    """Survey drones search the belt, a mining controller takes over the drones there and is launched, the vessel goes
    on to the nearest unvisited system with what it printed, and `repeat` loops; a system with no belt is skipped."""
    from rsweb import sim_order as so
    steps, err = so.parse("each system:\nsurvey\ncontroller: gather_evenly\nprint 1 mining_drone\nnext system\nrepeat")
    assert not err and [s_["kind"] for s_ in steps] == ["label", "survey", "controller", "print", "next", "repeat"]
    assert so.parse("controller: gather_resources")[1]                    # needs a configuration: refused
    assert not so.parse(so.EXPAND)[1]
    belt = "VIRTA-BELT-1"
    v = {"device_code": "V", "device_type": "heaven_vessel", "location": belt, "status": "idle"}
    devs = [v, {"device_code": "S1", "device_type": "survey_drone", "location": belt, "status": "idle"},
            {"device_code": "M1", "device_type": "mining_drone", "location": belt, "status": "idle"},
            {"device_code": "C1", "device_type": "ami_mining_controller", "location": belt, "status": "idle"}]
    a = {"order": steps, "stars": ["VIRTA"], "near": ["VIRTA", "VIRTB", "VIRTC"]}
    acts, _ = so.advance(a, devs, v, belt, {}, True, "t")
    assert acts == [{"kind": "search", "device": "S1"}]
    acts, _ = so.advance(a, devs, v, belt, {}, True, "t")
    assert [x["body"]["command"] for x in acts] == ["adopt", "set_directive", "launch"] and acts[0]["body"]["devices"] == ["M1"]
    acts, note = so.advance(a, devs, v, belt, {}, True, "t")
    assert acts == [{"kind": "print", "device_type": "mining_drone"}]
    devs.append({"device_code": "M2", "device_type": "mining_drone", "stowed_in_device_code": "V"})
    acts, _ = so.advance(a, devs, v, belt, {}, True, "t")
    assert acts[0]["kind"] == "travel" and acts[0]["to"] == "VIRTB"
    v2 = {**v, "location": "VIRTB"}                                      # arrived: no belt there, so on again
    a["stars"].append("VIRTB")
    acts, _ = so.advance(a, devs, v2, None, {}, True, "t")
    assert acts[0]["kind"] == "travel" and acts[0]["to"] == "VIRTC" and a["loops"] == 1


def test_relay_rollout_queue_goes_outward_and_chains():
    """Owner 2026-10-19: deliver relays to all systems, nearest the designated systems first."""
    from rsweb import relayroll as rr
    pos = {s: {"x": x, "y": 0, "z": 0} for s, x in
           {"S": 0, "A": 5, "B": 10, "C": 3, "FAR": 40, "D": -6, "OUT": 80}.items()}
    q = rr.queue(["S"], pos, set(), set(), set(), 50, True)
    assert [x["star"] for x in q] == ["S", "C", "A", "D", "B"]   # FAR: no relay within 7.5 ly of it; OUT: past 50 ly
    assert q[1] == {"star": "C", "ly": 3.0, "seed": "S", "kind": "relay"}
    assert [x["star"] for x in rr.queue(["S"], pos, {"S"}, {"C"}, {"D"}, 50, False)] == ["A", "B", "FAR"]
    assert [x["star"] for x in rr.queue(["S"], pos, set(), set(), set(), 50, True, limit=2)] == ["S", "C"]
    assert rr.queue(["NOWHERE"], pos, set(), set(), set(), 50, True) == []


def test_relay_rollout_long_range_stations(client):
    """Owner 2026-10-29: a 10 ly deep space relay station bridges gaps a relay can't (either end in reach links)."""
    from rsweb import relayroll as rr
    pos = {s: {"x": x, "y": 0, "z": 0} for s, x in
           {"S": 0, "A": 5, "B": 10, "W": 19, "V": 25, "X": 34, "FAR": 60}.items()}
    assert [x["star"] for x in rr.queue(["S"], pos, {"S"}, set(), set(), 50, True)] == ["A", "B"]
    q = rr.queue(["S"], pos, {"S"}, set(), set(), 50, True, long_hop=10.0)
    # relays first; W (9 ly from B) only by a station; V is a relay hop from W, X 9 ly from V a station again
    assert [(x["star"], x["kind"]) for x in q] == [("A", "relay"), ("B", "relay"), ("W", "station"), ("V", "relay"),
                                                   ("X", "station")]
    # a station already on order in B reaches W: a relay there links to it
    q = rr.queue(["S"], pos, {"S", "A"}, {"B"}, set(), 50, True, ranges={"B": 10.0}, long_hop=10.0, limit=1)
    assert q == [{"star": "W", "ly": 19.0, "seed": "S", "kind": "relay"}]
    r = client.post("/map/relays/rollout", headers=HX, data={"action": "save", "seeds": "S", "radius": "50",
                                                            "chained": "1", "long": "1"})
    assert r.status_code == 200 and rr.state(client.portal.call(client.app.state.db.kv_get, rr.KV, {}))["long"]
    page = client.get("/map/relays", headers=HX).text
    assert 'name="long" value="1" checked' in page and "deep space relay station" in page


def test_relay_rollout_radius_per_seed(client):
    """Owner 2026-10-24: SOLARES needs a much smaller rollout radius than FALQUORYX; unset seeds use the default."""
    from rsweb import relayroll as rr
    pos = {s: {"x": x, "y": 0, "z": 0} for s, x in
           {"SOL": 0, "N1": 3, "N2": 12, "FAL": 100, "F1": 104, "F2": 140}.items()}
    q = rr.queue(["SOL", "FAL"], pos, {"SOL", "FAL"}, set(), set(), 50, False, radii={"SOL": 5})
    assert [x["star"] for x in q] == ["N1", "F1", "F2"]   # N2 is past SOL's 5 ly, F2 within FAL's default 50
    assert q[-1]["seed"] == "FAL"
    r = client.post("/map/relays/rollout", headers=HX, data={"action": "save", "seeds": "sol fal f1:20", "radius": "50",
                                                            "r_SOL": "5", "r_FAL": "50", "chained": "1"})
    assert r.status_code == 200
    roll = rr.state(client.portal.call(client.app.state.db.kv_get, rr.KV, {}))
    assert roll["seeds"] == ["SOL", "FAL", "F1"] and roll["radii"] == {"SOL": 5.0, "F1": 20.0}
    page = client.get("/map/relays", headers=HX).text
    assert 'name="r_SOL"' in page and 'name="r_FAL"' in page


def test_relay_rollout_pass_keeps_orders_open_and_skips_failures(client, monkeypatch):
    from rsweb import relayroll as rr
    monkeypatch.setattr(rr, "capacity", lambda *a: 2)   # two free hold slots on logistics vessels with a replicant
    from rsweb import ward_orders as wo
    db = client.app.state.db
    eng = client.app.state.worker.automations
    stars = [{"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in {"S": 0, "A": 4, "B": 8, "C": 10}.items()]
    client.portal.call(db.kv_set, "stars", {"stars": stars})
    devs = [{"device_code": "R1", "device_type": "ftl_relay", "location": "S-1-L4", "status": "relaying"}]

    async def devices():
        return devs

    async def order(kind, star, user=""):
        if star == "B":
            return "no spare FTL relay and no autofactory within 100 ly of B"
        orders = await db.kv_get(wo.kv_key("relay"), {}) or {}
        orders[star] = {"at": "2026-10-19T00:00:00+00:00", "how": "print"}
        await db.kv_set(wo.kv_key("relay"), orders)
        return f"printing a relay for {star}"
    eng.devices, eng.order_outpost = devices, order
    r = client.post("/map/relays/rollout", headers=HX, data={"action": "start", "seeds": "s", "radius": "50",
                                                              "chained": "1"})
    assert r.status_code == 200
    st = rr.state(client.portal.call(db.kv_get, rr.KV, {}))
    assert st["on"] and st["seeds"] == ["S"] and sorted(st["ordered"]) == ["A", "C"] and "B" in st["skipped"]
    # A's relay is up, C's order was dropped: both make room, and nothing is left, so the rollout ends
    devs.append({"device_code": "R2", "device_type": "ftl_relay", "location": "A-1-L4", "status": "relaying"})
    client.portal.call(db.kv_set, wo.kv_key("relay"), {})
    client.portal.call(eng.relay_rollout_pass)
    st = rr.state(client.portal.call(db.kv_get, rr.KV, {}))
    assert st["done"] == 1 and sorted(st["skipped"]) == ["B", "C"] and not st["ordered"] and not st["on"]
    page = client.get("/map/relays", headers=H).text
    assert "Relay rollout" in page and "Retry skipped (2)" in page


def test_relay_rollout_pass_orders_station_pairs(client, monkeypatch):
    """Owner 2026-10-29: with long-range stations on, a gap no relay crosses gets a station."""
    from rsweb import relayroll as rr
    monkeypatch.setattr(rr, "capacity", lambda *a: 3)
    from rsweb import ward_orders as wo
    db = client.app.state.db
    eng = client.app.state.worker.automations
    stars = [{"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in {"S": 0, "W": 9}.items()]
    client.portal.call(db.kv_set, "stars", {"stars": stars})
    devs = [{"device_code": "R1", "device_type": "ftl_relay", "location": "S-1-L4", "status": "relaying"}]
    calls = []

    async def devices():
        return devs

    async def order(kind, star, user=""):
        calls.append((kind, star))
        orders = await db.kv_get(wo.kv_key(kind), {}) or {}
        orders[star] = {"at": "2026-10-29T00:00:00+00:00", "how": "print"}
        await db.kv_set(wo.kv_key(kind), orders)
        return f"printing a {kind} for {star}"
    eng.devices, eng.order_outpost = devices, order
    client.portal.call(db.kv_set, rr.KV, {"on": True, "seeds": ["S"], "chained": True})
    client.portal.call(eng.relay_rollout_pass)
    assert calls == [] and not rr.state(client.portal.call(db.kv_get, rr.KV, {}))["on"]   # no relay reaches W
    client.portal.call(db.kv_set, rr.KV, {"on": True, "seeds": ["S"], "chained": True, "long": True})
    client.portal.call(eng.relay_rollout_pass)
    assert calls == [("station", "W")]
    st = rr.state(client.portal.call(db.kv_get, rr.KV, {}))
    assert sorted(st["ordered"]) == ["W"] and st["on"]


def test_relays_route_orders_stations(client, monkeypatch):
    eng = client.app.state.worker.automations
    calls = []

    async def order(kind, star, user=""):
        calls.append((kind, star))
        return "ok"
    monkeypatch.setattr(eng, "order_outpost", order)
    r = client.post("/map/relays/route", headers=HX, data={"stars": "A,G,B", "long": "A,B"})
    assert r.status_code == 200 and calls == [("station", "A"), ("relay", "G"), ("station", "B")]


def test_relay_rollout_pause_cancels_relay_prints_and_prints_no_more(client, monkeypatch):
    """Owner 2026-10-25: pausing the relay rollout cancels every relay print not started yet and keeps new ones (the
    1.241.1 lost-print re-source included) from being created until it starts again."""
    from rsweb import relayroll as rr
    from rsweb import ward_orders as wo
    db = client.app.state.db
    eng = client.app.state.worker.automations
    fab = {"device_code": "F1", "device_type": "autofactory", "location": "S-BELT-1", "status": "printing (ftl_relay)",
           "available_commands": ["enqueue_print", "dequeue_print"], "queue_size": 10,
           "print_queue": [{"device_type": "ftl_relay", "tags": ["to:A"]}, {"device_type": "mining_drone", "tags": []},
                           {"device_type": "ftl_relay", "tags": ["to:B"]}, {"device_type": "ftl_relay", "tags": ["stock"]}]}
    devs = [fab]

    async def devices():
        return devs
    eng.devices = devices
    client.portal.call(db.kv_set, wo.kv_key("relay"), {
        "A": {"at": "2026-10-25T00:00:00+00:00", "how": "print", "factory": "F1"},
        "B": {"at": "2026-10-25T00:00:00+00:00", "how": "print", "factory": "F1"},
        "C": {"at": "2026-10-25T00:00:00+00:00", "how": "print"},                  # lost, waiting for a slot
        "D": {"at": "2026-10-25T00:00:00+00:00", "how": "send", "device": "R9"}})
    job = "wards-print-E"
    client.portal.call(eng.save_jobs, [{"id": job, "rule": "wards", "title": "print a FTL relay on F1 for E", "device": "F1",
                                        "steps": [], "idx": 0, "status": "running", "meta": {"outpost": "E"}}])
    client.portal.call(db.kv_set, rr.KV, {"on": True, "seeds": ["S"], "ordered": {s: "x" for s in "ABCD"}})
    r = client.post("/map/relays/rollout", headers=HX, data={"action": "stop", "seeds": "S", "radius": "50", "chained": "1"})
    assert r.status_code == 200
    st = rr.state(client.portal.call(db.kv_get, rr.KV, {}))
    assert st["paused"] and not st["on"] and list(st["ordered"]) == ["D"] and not st["skipped"]
    assert list(client.portal.call(db.kv_get, wo.kv_key("relay"), {})) == ["D"]
    jobs = client.portal.call(eng.jobs)
    assert next(j for j in jobs if j["id"] == job)["status"] == "cancelled"
    drop = next(j for j in jobs if j["title"].startswith("F1: drop 2 queued"))
    assert [s["body"]["index"] for s in drop["steps"]] == [2, 0]   # highest first, so the indices hold
    # paused: an order (by hand, or a lost print sourced again) finds no spare and prints nothing
    how, msg = client.portal.call(eng._outpost_source, "relay", "E", devs, set(), {})
    assert how is None and "relay prints are paused" in msg
    page = client.get("/map/relays", headers=H).text
    assert "relay prints paused" in page and ">Resume<" in page
    client.post("/map/relays/rollout", headers=HX, data={"action": "start", "seeds": "S", "radius": "50", "chained": "1"})
    assert not rr.state(client.portal.call(db.kv_get, rr.KV, {}))["paused"]


def test_a_relay_run_drops_relays_system_by_system():
    from rsweb import loadouts as lo
    by = {c: {"device_code": c, "device_type": "ftl_relay", "location": None, "stowed_in_device_code": "HV",
              "tags": [f"to:{t.lower()}"]} for c, t in (("R1", "AAA"), ("R2", "BBB"), ("R3", "BBB"))}
    by["HV"] = {"device_code": "HV", "device_type": "heaven_vessel", "location": "HUB-1"}
    dl = {"carrier": "HV", "carrier_loc": "HUB-1", "from": "HUB", "to": "AAA", "devices": ["R1", "R2", "R3"],
          "mode": "stow", "dest_of": {"R2": "BBB", "R3": "BBB"}, "stops": ["BBB"], "stay": True}
    descs = [s["desc"] for s in lo.delivery_steps(dl, by, {}, False)]
    moves = [d for d in descs if d.startswith("HV → ") and "L4/L5" not in d]   # (+ a trip to an L4/L5 point at each)
    assert moves == ["HV → AAA (deliver 1 to AAA)", "HV → BBB (deliver 2 to BBB)"]
    assert descs.index("deploy R1 in AAA") < descs.index("HV → BBB (deliver 2 to BBB)") < descs.index("deploy R3 in BBB")


def test_a_reserved_stock_relay_loses_its_stock_tag(client):
    from rsweb import loadouts as lo
    db, eng = client.app.state.db, client.app.state.worker.automations
    sent = []

    async def fake_send(method, path, body, label):
        sent.append((path, body))
        return True, {}, None
    eng.send = fake_send
    client.portal.call(db.kv_set, "devices", [
        {"device_code": "R1", "device_type": "ftl_relay", "location": "SOL-3", "tags": ["fleet:hub", "stock", "to:ALPHA"]},
        {"device_code": "R2", "device_type": "ftl_relay", "location": "SOL-3", "tags": ["spare", "stock"]},
        {"device_code": "R3", "device_type": "ftl_relay", "location": "SOL-3", "tags": ["spare", "stock", "gather", "to:alpha"]}])
    client.portal.call(eng.stock_pass)
    assert sent == [("/devices/R1", {"configuration": {"remove_tags": ["stock"]}})]   # R2 is still a spare in stock, R3 on its way to a hub's
    st = lo.self_move_steps("R2", "ALPHA", {}, {"device_code": "R2", "tags": ["spare", "stock"], "location": "SOL-3"})
    assert st[0]["body"]["configuration"] == {"add_tags": [lo.to_tag("ALPHA")], "remove_tags": ["spare", "stock"]}


def test_map_hazard_haze(client):
    """The galaxy map hazes systems an incoming object still threatens (the object's system and its target's), and
    drops the haze once the object is closed."""
    db = client.app.state.db
    client.portal.call(db.insert_event, {"id": "hz1", "event": "system.impact_warning", "location": "SOL-OBJ-1",
                                          "created_at": "2026-10-20T10:00:00+00:00",
                                          "payload": {"destination": "SOL-3", "impact_eta": "2099-10-20T11:00:00Z",
                                                      "device_codes": ["D1"]}})
    hz = client.get("/api/map.json?part=overlay", headers=H).json()["hazards"]
    assert list(hz) == ["SOL"] and hz["SOL"]["objects"] == ["SOL-OBJ-1"] and hz["SOL"]["hours_left"] > 0
    client.portal.call(db.insert_event, {"id": "hz2", "event": "system.object_impacted", "location": "SOL-OBJ-1",
                                          "payload": {"impact_target": "SOL-3"}})
    assert client.get("/api/map.json?part=overlay", headers=H).json()["hazards"] == {}


def test_map_hazardous_tag(client):
    """A system whose scan tags it hazardous (system_tags) is hazed on the galaxy map too."""
    import json
    from rsweb.web import hazard_tags
    db = client.app.state.db
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("SOL", json.dumps({"system_tags": ["binary_system", "hazardous"]}), "2026-10-21T00:00:00+00:00"))
    sol = next(s for s in client.get("/api/map.json?part=core", headers=H).json()["stars"] if s["designation"] == "SOL")
    assert sol["hazardous"] == ["hazardous"]
    assert hazard_tags('["radiation_hazard", "binary_system"]') == ["radiation_hazard"] and hazard_tags(None) == []


def test_systems_page_lists_scan_tags(client):
    """The Systems page lists every system tag in the cached scans with the systems carrying it."""
    import json
    db = client.app.state.db
    for star, tags in (("SOL", ["binary_system", "hazardous"]), ("KELM", ["hazardous"])):
        client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                           (star, json.dumps({"system_tags": tags}), "2026-10-21T00:00:00+00:00"))
    body = client.get("/systems", headers=H).text
    assert 'id="system-tags"' in body and 'href="/systems/KELM">KELM</a>' in body
    assert body.index(">hazardous<") < body.index(">binary system<")   # most common first


def test_map_star_tags(client):
    """The galaxy map gets each scanned system's tags (resource_rich and anomaly have their own markers)."""
    import json
    db = client.app.state.db
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("SOL", json.dumps({"system_tags": ["resource_rich", "anomaly"]}), "2026-10-21T00:00:00+00:00"))
    sol = next(s for s in client.get("/api/map.json?part=core", headers=H).json()["stars"] if s["designation"] == "SOL")
    assert sol["tags"] == ["resource_rich", "anomaly"] and "hazardous" not in sol


# --- 1.192.0: star catalog lookups are built once per catalog update ----------------------------------------------------

def test_star_lookups_are_shared_until_the_catalog_changes(client):
    """The engine read and copied the whole star catalog (tens of thousands of stars) several times a pass."""
    eng, db = client.app.state.worker.automations, client.app.state.db

    async def go():
        a = await eng.star_lookup("pos")
        assert a and await eng.star_lookup("pos") is a
        cat = await db.kv_get("stars", {})
        cat["stars"] = [{"designation": "NEWSTAR", "position": {"x": 1, "y": 2, "z": 3}}]
        await db.kv_set("stars", cat)
        b = await eng.star_lookup("pos")
        assert b == {"NEWSTAR": {"x": 1, "y": 2, "z": 3}} and await eng.star_lookup("names") == {"NEWSTAR"}
    client.portal.call(go)


def test_a_new_relay_order_sends_the_relay_already_tagged_there():
    """Live 2026-10-21: MOLURNA's order expired with its relay still at the factory; the new order printed another."""
    from rsweb import ward_orders as wo
    devs = [{"device_code": "R1", "device_type": "ftl_relay", "location": "FAB-BELT-1", "status": "inactive",
             "tags": ["to:molurna"]},
            {"device_code": "R2", "device_type": "ftl_relay", "location": "FAB-BELT-1", "status": "inactive", "tags": ["to:valyra"]}]
    assert wo.bound_to(devs, "MOLURNA", "ftl_relay")["device_code"] == "R1"
    assert wo.bound_to(devs, "TIMIRIS", "ftl_relay") is None


def test_a_new_relay_order_waits_for_a_print_already_queued_for_it():
    from rsweb import ward_orders as wo
    fab = {"device_code": "F1", "device_type": "autofactory", "location": "FAB-BELT-1", "available_commands": ["enqueue_print"],
           "print_queue": [{"device_type": "ftl_relay", "tags": ["to:molurna"]}]}
    assert wo.printing_for([fab], "MOLURNA", "ftl_relay")["device_code"] == "F1"
    assert wo.printing_for([fab], "VALYRA", "ftl_relay") is None
    fab2 = {"device_code": "F2", "device_type": "autofactory", "print_queue": [],
            "printing": {"device_type": "ftl_relay", "tags": ["to:valyra"]}}
    assert wo.printing_for([fab, fab2], "VALYRA", "ftl_relay")["device_code"] == "F2"


def test_printing_hubs_print_for_each_other_within_sector_range():
    """Owner 2026-10-26: FALQUORYX and SOLARES (~500 ly apart) print for each other when one's stockpile can't cover a
    print (SOLARES relays sat waiting for conductive); a hub beyond the sector range (Delta, ISOLITHOR) stays apart."""
    from rsweb import loadouts as lo, stock, ward_orders as wo

    def fac(code, loc, fid, **kw):
        return {"device_code": code, "device_type": "autofactory", "location": loc, "status": "idle", "print_queue": [],
                "available_commands": ["enqueue_print"], "tags": [f"fleet:{fid}"], **kw}
    devices = [fac("SF", "SOL-BELT-1", "sol"), fac("FF", "FAL-BELT-1", "fal"), fac("DF", "DEL-BELT-1", "del")]
    fleets = [{"id": fid, "name": fid, "home": home, "station": True, "materials": "self", "wants": {}}
              for fid, home in (("sol", "SOL"), ("fal", "FAL"), ("del", "DEL"))]
    fleets[0]["wants"] = {"ftl_relay": 1}
    bps = [{"device_type": "ftl_relay", "resources": {"conductive": 120, "structural": 80}, "print_time": 600}]
    stars = {"SOL": {"position": {"x": 0, "y": 0, "z": 0}}, "FAL": {"position": {"x": 500, "y": 0, "z": 0}},
             "DEL": {"position": {"x": -29000, "y": 0, "z": 0}}}
    cfg = {"phases": [], "fleets": fleets, "fleets_migrated": True}
    rich = {"conductive": 3000, "structural": 9000}
    p = lo.plan(cfg, devices, bps, {"SOL-BELT-1": {"structural": 9000}, "FAL-BELT-1": rich}, stars, {}, set(), [], {})
    assert [(pr["factory"], pr["star"]) for pr in p["prints"]] == [("FF", "SOL")]      # the stocked hub prints it
    p = lo.plan(cfg, devices, bps, {"SOL-BELT-1": {"structural": 9000}, "DEL-BELT-1": rich}, stars, {}, set(), [], {})
    assert [pr["factory"] for pr in p["prints"]] == ["SF"]                               # Delta's isn't lent: waits at home
    p = lo.plan(cfg, devices, bps, {"SOL-BELT-1": rich, "FAL-BELT-1": rich}, stars, {}, set(), [], {})
    assert [pr["factory"] for pr in p["prints"]] == ["SF"]                               # stocked at home: home first

    # keep in stock: partners in range, prints bound here count for here (and not for the hub printing them)
    pos = {k: v["position"] for k, v in stars.items()}
    sol, fal = {**fleets[0], "stock": {"ftl_relay": 1}}, {**fleets[1], "stock": {"ftl_relay": 1}}
    assert stock.partners(sol, fleets, pos, 1728) == [fleets[1]]
    devs = [fac("SF", "SOL-BELT-1", "sol"),
            fac("FF", "FAL-BELT-1", "fal", print_queue=[{"device_type": "ftl_relay", "tags": ["spare", "stock", "gather", "to:sol"]}])]
    assert stock.shortfall(sol, devs, [fal]) == {} and stock.shortfall(fal, devs) == {"ftl_relay": 1}
    devs[1]["print_queue"] = []
    devs.append({"device_code": "R1", "device_type": "ftl_relay", "location": "FAL-BELT-1", "tags": ["spare", "gather", "to:sol"]})
    assert stock.shortfall(sol, devs, [fal]) == {} and stock.shortfall(fal, devs) == {"ftl_relay": 1}   # on its way
    # owner 2026-10-28: a partner's spares beyond its own stock go over before anything is printed (SOLARES printed
    # relays while ~95 sat at FALQUORYX); the hub keeps its own stock, stock-tagged ones first
    spare = lambda c, loc, *t, **kw: {"device_code": c, "device_type": "ftl_relay", "location": loc, "status": "inactive",
                                      "tags": ["spare", *t], **kw}
    devs = [spare("A", "FAL-BELT-1"), spare("B", "FAL-1-L4", "stock"), spare("C", "FAL-BELT-1"),
            spare("D", "FAL-BELT-1", "to:sol"), spare("E", "FAL-BELT-1", "at:fal-1-l4"), spare("F", "FAL-BELT-1", status="relaying"),
            spare("G", "SOL-BELT-1")]
    assert [d["device_code"] for d in stock.surplus(fal, "ftl_relay", devs)] == ["A", "C"]
    assert [d["device_code"] for d in stock.surplus(fal, "ftl_relay", devs, {"A"})] == ["C"]
    # the loadout planner carries a spare sent over (gather + to:) as a spare: it stays spare (and off) there
    devs = [fac("SF", "SOL-BELT-1", "sol"), spare("S", "FAL-BELT-1", "stock", "gather", "to:sol")]
    p = lo.plan({**cfg, "fleets": [{**fleets[0], "wants": {}}, fleets[1]]}, devs, bps, {}, stars, {}, set(), [], {})
    assert p["moves"].get("S") == "SOL" and "S" in p["gathering"]
    assert lo.self_move_steps("S", "SOL", {}, devs[1], gathering=True)[0]["body"]["configuration"].get("remove_tags") is None

    # relay / ward orders: a factory whose stockpile covers the print first, ahead of a shorter queue
    inv = {"SOL-BELT-1": {"structural": 9000}, "FAL-BELT-1": rich}
    devs = [fac("SF", "SOL-BELT-1", "sol"), fac("FF", "FAL-BELT-1", "fal", print_queue=[{"device_type": "x"}])]
    have = wo.stocked(devs, {"conductive": 120, "structural": 80}, inv)
    assert have == {"FF"} and wo.factory_near(devs, "SOL", set(), pos, 1728, None, have)["device_code"] == "FF"
    assert wo.factory_near(devs, "SOL", set(), pos, 1728)["device_code"] == "SF"


def test_colony_space_stations_on_the_map(client):
    """Owner 2026-10-27: colony space stations aren't megastructures (the game reports no station object, only the
    overview's storyline), so they get their own map mark, kept once the storyline ends."""
    from rsweb import megas
    db = client.app.state.db
    ov = {"storylines": [{"title": "Terraforming the Colony Worlds", "locations": ["ROSALIATE", "SOL-3"],
                          "summary": "Humanity is living on space stations, but they need planets."},
                         {"title": "Other", "locations": ["FALQUORYX"], "summary": "Mine things."}]}
    assert megas.stations_in(ov) == {"ROSALIATE", "SOL"}
    client.portal.call(megas.remember_stations, db, ov)
    client.portal.call(db.kv_set, "overview", {})
    assert {"ROSALIATE", "SOL"} <= client.portal.call(megas.stations, db)
    stars = {s["designation"]: s for s in client.get("/api/map.json", headers=H).json()["stars"]}
    assert stars["SOL"].get("station") and not stars.get("FALQUORYX", {}).get("station")
    assert 'id="opt-mark-station"' in client.get("/map", headers=H).text


def test_scanned_megastructures_land_on_their_star(client):
    """Joe 2026-10-27: scans list stations as megastructures at STAR-OBJ-n; the designation names no star."""
    import json as _json
    from rsweb import megas
    db = client.app.state.db
    scan = {"system_objects": [
        {"designation": "SPACE-STATION-006", "location": "ARUCALIS-OBJ-1", "object_type": "megastructure",
         "megastructure_type": "space_station"},
        {"designation": "DERELICT-RING-001", "location": "TWAFFY-OBJ-1", "object_type": "megastructure",
         "megastructure_type": "derelict_ring"}]}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("ARUCALIS", _json.dumps(scan), "2026-10-27T00:00:00+00:00"))
    known = client.portal.call(megas.known, db)
    assert known["ARUCALIS"] == ["ARUCALIS-OBJ-1"] and "SPACE" not in known
    assert "ARUCALIS" in client.portal.call(megas.stations, db)
    assert megas.other_than_stations(["ARUCALIS-OBJ-1", "TWAFFY-OBJ-1"]) == ["TWAFFY-OBJ-1"]


def test_system_page_folds_out_system_objects_and_the_full_scan(client):
    """Joe 2026-10-27: the scan's system objects (a space station) and the whole scan, under the bodies."""
    import json as _json
    db = client.app.state.db
    scan = {"planets": [{"designation": "ARUCALIS-3", "type": "terrestrial"}], "system_objects": [
        {"designation": "SPACE-STATION-006", "location": "ARUCALIS-OBJ-1", "object_type": "megastructure",
         "megastructure_type": "space_station", "title": "Space Station", "stage": "Populated Space Station",
         "status": "completed", "progress_percentage": 100.0, "description": "Fully operational.", "contributors": 1,
         "contributors_memorial": [{"name": "creme-292", "replicant_code": "E25D16DF", "contributions": 206, "total_value": 143615}],
         "requirements": {"hab_module": {"complete": True, "current": 85, "remaining": 0, "required": 85}}}]}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("ARUCALIS", _json.dumps(scan), "2026-10-27T00:00:00+00:00"))
    page = client.get("/systems/ARUCALIS", headers=H).text
    assert "Populated Space Station" in page and "hab module" in page and "creme-292" in page
    assert "Full system scan" in page and "SPACE-STATION-006" in page


def test_relay_orders_use_another_hubs_extras_before_a_hubs_own_stock():
    """Owner 2026-10-28 (use up the spare relays at FALQUORYX): a relay / ward order sends the nearest spare in reach,
    but a printing hub's own keep-in-stock spares go last, since taking one only gets it printed again there."""
    from rsweb import stock, ward_orders as wo
    pos = {"SOL": {"x": 0, "y": 0, "z": 0}, "FAL": {"x": 500, "y": 0, "z": 0}, "NEAR": {"x": 5, "y": 0, "z": 0}}
    r = lambda c, loc, *t: {"device_code": c, "device_type": "ftl_relay", "location": loc, "status": "inactive",
                            "tags": ["spare", *t]}
    devs = [r("S1", "SOL-BELT-1", "stock"), r("F1", "FAL-BELT-1", "stock"), r("F2", "FAL-BELT-1"), r("F3", "FAL-1-L4")]
    hubs = [{"id": "sol", "home": "SOL", "stock": {"ftl_relay": 1}}, {"id": "fal", "home": "FAL", "stock": {"ftl_relay": 1}}]
    keep = stock.kept(hubs, "ftl_relay", devs)
    assert keep == {"S1", "F1"}
    assert wo.spare_ward(devs, "NEAR", set(), pos, 1728, "ftl_relay")["device_code"] == "S1"          # nearest
    assert wo.spare_ward(devs, "NEAR", set(), pos, 1728, "ftl_relay", keep)["device_code"] == "F2"    # FAL's extras first
    assert wo.spare_ward(devs[:2], "NEAR", set(), pos, 1728, "ftl_relay", keep)["device_code"] == "S1"  # else still a spare


def test_station_swapped_for_relay_once_a_relay_path_exists(client):
    """Owner 2026-10-29: once a cheaper relay can link a system another way, the long-range station is replaced and
    goes spare."""
    from rsweb import relaynet as rn
    from rsweb import ward_orders as wo
    P = lambda x: {"x": x, "y": 0, "z": 0}  # noqa: E731
    pos = {"A": P(0), "M": P(4), "S": P(8), "B": P(16), "N": P(12)}
    R = lambda c, t, s: {"device_code": c, "device_type": t, "location": f"{s}-4-L4", "status": "relaying"}  # noqa: E731
    devs = [R("R1", "ftl_relay", "A"), R("R2", "ftl_relay", "B"), R("S1", rn.STATION, "S"), R("R3", "ftl_relay", "M")]
    assert rn.swappable_stations(devs, pos) == []   # S–B is 8 ly: only the station's 10 ly links it
    devs.append(R("R4", "ftl_relay", "N"))
    assert rn.swappable_stations(devs, pos) == ["S"]
    db = client.app.state.db
    eng = client.app.state.worker.automations
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": s, "position": p} for s, p in pos.items()]})
    calls = []

    async def devices():
        return devs

    async def order(kind, star, user=""):
        calls.append(("add", kind, star))
        await db.kv_set(wo.kv_key(kind), {star: {"at": "2026-10-29T00:00:00+00:00", "how": "print"}})
        return "printing"

    async def remove(kind, star, user=""):
        calls.append(("remove", kind, star))
        return "deactivating"
    eng.devices, eng.order_outpost, eng.remove_outpost = devices, order, remove
    client.portal.call(eng.station_swap_pass)
    client.portal.call(eng.station_swap_pass)   # the order is open: not asked twice
    assert calls == [("add", "relay", "S")]
    devs.append(R("R5", "ftl_relay", "S"))      # the relay is up beside the station
    client.portal.call(eng.station_swap_pass)
    assert calls[-1] == ("remove", "station", "S")


def test_outpost_orders_with_virtual_queue_never_fill_a_factory(client, monkeypatch):
    """Live 2026-11-03: rollout prints ordered lately counted against the game's queue slots even though they wait in
    the virtual print queue, so after a few dozen every factory looked full and the relay rollout paused (VORWYRYR)."""
    from rsweb import ward_orders as wo
    db = client.app.state.db
    eng = client.app.state.worker.automations
    stars = [{"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in {"F": 0, "A": 5, "B": 9, "C": 12}.items()]
    client.portal.call(db.kv_set, "stars", {"stars": stars})
    devs = [{"device_code": "FA000001", "device_type": "autofactory", "location": "F-BELT-1", "status": "idle",
             "queue_size": 10, "print_queue": [{"device_type": "x"}] * 8, "available_commands": ["enqueue_print"],
             "in_control_range": True}]

    async def devices():
        return devs
    eng.devices = devices
    assert "printing" in client.portal.call(eng.order_outpost, "relay", "A")
    assert "printing" in client.portal.call(eng.order_outpost, "relay", "B")
    assert "no autofactory" in client.portal.call(eng.order_outpost, "relay", "C")   # no virtual queue: 8 + 2 = full
    class VQ:   # the virtual print queue keeps every print
        async def intercept(self, path, body):
            return {"kept": True}

        async def merge(self, path, resp):
            return resp
    monkeypatch.setattr(eng.api, "vq", VQ(), raising=False)
    assert "printing" in client.portal.call(eng.order_outpost, "relay", "C")
    assert {o.get("factory") for o in client.portal.call(db.kv_get, wo.kv_key("relay"), {}).values()} == {"FA000001"}
    print_job = {"status": "running", "device": "X", "steps": [{"path": "/devices/X", "body": {"command": "enqueue_print"}}]}
    travel = {"status": "running", "device": "X", "steps": [{"path": "/devices/X", "body": {"command": "travel"}}]}
    assert wo.print_only([print_job], "X") == {"X"} and not wo.print_only([print_job, travel], "X")


def test_dashboard_lists_station_and_megastructure_systems(client):
    """1.383.0: Dashboard › Stations & megastructures links each system with a station (by name) or megastructure."""
    db = client.app.state.db
    client.portal.call(db.kv_set, "stations", {"at": "x", "stations": [
        {"name": "New Havana Station", "slug": "new_havana", "location": "ALKALAROPS-OBJ-2"}]})
    page = client.get("/", headers=H).text
    assert 'data-card="stations"' in page
    assert '<a href="/systems/ALKALAROPS">ALKALAROPS</a> <span class="muted">New Havana Station' in page
    assert '<a href="/systems/MIRFAKA">MIRFAKA</a> <span class="muted">datacenter' in page
