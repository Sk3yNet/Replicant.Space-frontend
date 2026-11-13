"""Core tests: logistics runs, deliveries and stockpiles (split out of test_core.py by test name)."""
from corelib import *  # noqa: F401,F403


def test_freighter_at_destination_counts_for_its_ferry_controllers_system():
    from rsweb import loadouts as lo
    devices = [{"device_code": "TF", "device_type": "ami_transport_controller", "location": "AEM-BELT-1", "status": "coordinating",
                "ami_directive": {"name": "ferry", "config": {}, "_eval_state": "active:1l:0d"}},
               {"device_code": "57C506F0", "device_type": "cargo_freighter", "location": "FAL-BELT-1", "status": "idle",
                "controller_device_code": "TF", "tags": []}]
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"cargo_freighter": 1}}], "systems": {"AEM": "p", "FAL": "p"}}
    p = lo.plan(cfg, devices, [], {}, {}, {}, set(), [], {})
    aem = {r["type"]: r for r in _rep(p, "AEM")["rows"]}["cargo_freighter"]
    fal = {r["type"]: r for r in _rep(p, "FAL")["rows"]}["cargo_freighter"]
    assert aem["have"] == 1 and aem["away"] == ["57C506F0"] and fal["have"] == 0
    assert "fleet:aem-home" in p["tag_add"]["57C506F0"]



def test_freighter_stranded_under_in_system_controller_is_released():
    """The real case: 57C506F0 sits idle in AEMEROTH, still run by FALQUORYX's consolidate controller DF451241."""
    from rsweb import loadouts as lo
    devices = [{"device_code": "DF451241", "device_type": "ami_transport_controller", "location": "FALQUORYX-BELT-1",
                "status": "coordinating", "ami_directive": {"name": "consolidate", "config": {"deliver": "FALQUORYX-BELT-1"},
                                                            "_eval_state": "idle:no_sources"}},
               {"device_code": "0E158313", "device_type": "ami_transport_controller", "location": "AEMEROTH-BELT-1",
                "status": "coordinating", "ami_directive": {"name": "ferry", "config": {"collect": "AEMEROTH-6-7", "deliver": "FALQUORYX-BELT-1"},
                                                            "_eval_state": "active:1l:0d"}},
               {"device_code": "57C506F0", "device_type": "cargo_freighter", "location": "AEMEROTH-5-L4", "status": "idle",
                "controller_device_code": "DF451241", "tags": ["home:aemeroth"], "cargo_used": 0}]
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"cargo_freighter": 1}}], "systems": {"AEMEROTH": "p", "FALQUORYX": "p"},
           "roles": {"AEMEROTH": "source", "FALQUORYX": "destination"}}
    stars = {"AEMEROTH": {"position": {"x": 0, "y": 0, "z": 0}}, "FALQUORYX": {"position": {"x": 1, "y": 0, "z": 0}}}
    p = lo.plan(cfg, devices, [], {}, stars, {}, set(), [], {})
    assert p["releases"] == {"DF451241": ["57C506F0"]}          # the consolidate controller lets it go
    aem = {r["type"]: r for r in _rep(p, "AEMEROTH")["rows"]}["cargo_freighter"]
    assert aem["have"] == 1                                    # it counts where it is (and is tagged)
    # next pass, released: AEMEROTH's ferry controller takes it on
    devices[2]["controller_device_code"] = None
    routes, _ = lo.material_routes(cfg, devices, {"AEMEROTH-6-7": {"structural": 277}}, stars, set(),
                                   {"0E158313": {"directive": "ferry", "configuration": {"collect": "AEMEROTH-6-7", "deliver": "FALQUORYX-BELT-1"},
                                                 "finished": False}}, {})
    assert routes and routes[0]["controller"] == "0E158313" and [a["code"] for a in routes[0]["adopt"]] == ["57C506F0"]
    assert not routes[0]["resend"]                              # adopt only; its ferry keeps running


def test_unlisted_freighter_still_counts_in_loadouts_and_fleets():
    from rsweb import fleets as fl
    from rsweb import loadouts as lo
    devices = [
        {"device_code": "TC1", "device_type": "ami_transport_controller", "location": "AEM-BELT-1", "status": "coordinating",
         "ami_directive": {"name": "ferry"}, "tags": ["home:aem", "ferry"]},
        {"device_code": "CF1", "device_type": "cargo_freighter", "location": "AEM-6-33", "status": "surging", "unlisted": True,
         "location_stale": True, "controller_device_code": "TC1", "tags": ["home:aem", "fleet:haul"], "features": ["surge", "transport"]},
    ]
    cfg = {"phases": [{"id": "p1", "name": "Outpost", "wants": {"cargo_freighter": 1}}], "systems": {"AEM": "p1"}}
    p = lo.plan(cfg, [devices[0], {**devices[1], "tags": ["home:aem"]}], [], {}, {"AEM": {}}, {}, set(), [], {})
    row = next(r for r in _rep(p, "AEM")["rows"] if r["type"] == "cargo_freighter")
    assert row["have"] == 1 and row["short"] == 0 and p["prints"] == []      # counted, not re-printed
    r = fl.roster({"id": "haul", "wants": {"cargo_freighter": 1}}, devices)
    assert [d["device_code"] for d in r["members"]] == ["CF1"] and r["unlisted"] == {"CF1"}


def test_planner_spreads_over_autofactories_at_the_same_stockpile(client):
    world = client.app.state.api.http._transport.app.state.world
    af = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    world.devices.append({**af, "device_code": "AF00CAFE"})
    world.queues["AF00BEEF"], world.queues["AF00CAFE"] = [], []
    client.portal.call(client.app.state.worker.sync_devices)
    data = {"printer": "device:AF00BEEF", "qty:mining_drone": "3", "qty:survey_drone": "1"}
    r = client.post("/blueprints/plan", data=data, headers=HX)
    assert "Spread over the 2 autofactories at SOL-3-L4" in r.text and 'name="nosplit" checked' not in r.text
    assert "Queue on 2 autofactories" in r.text
    r = client.post("/blueprints/queue-plan", data={**data, "_planned": "1"}, headers=HX)   # spread unless told not to
    job = client.portal.call(client.app.state.worker.automations.jobs)[-1]
    queued = [(s["path"], s["body"]["device_type"], s["body"]["quantity"]) for s in job["steps"]
              if s["body"].get("command") == "enqueue_print"]
    assert sum(q for _, _, q in queued) == 4 and {p for p, _, _ in queued} == {"/devices/AF00BEEF", "/devices/AF00CAFE"}
    # "keep it on one": everything on the chosen printer
    r = client.post("/blueprints/queue-plan", data={**data, "_planned": "1", "nosplit": "on"}, headers=HX)
    job = client.portal.call(client.app.state.worker.automations.jobs)[-1]
    assert {s["path"] for s in job["steps"] if s["body"].get("command") == "enqueue_print"} == {"/devices/AF00BEEF"}
    # Queue a print (Blueprints): 3 of one type are shared too
    world.queues["AF00BEEF"], world.queues["AF00CAFE"] = [], []
    client.portal.call(client.app.state.worker.sync_devices)
    client.post("/blueprints/print", data={"printer": "device:AF00BEEF", "device_type": "mining_drone", "quantity": "3"}, headers=HX)
    assert world.queues["AF00BEEF"] and world.queues["AF00CAFE"]


def test_deliveries_hand_devices_to_the_carriers_owner():
    """Live 2026-10-06: other replicants' carriers failed to attach Miner 1's drones ('Target device belongs to a
    different account'). Since 1.26.0 any carrier will do: the device is handed to the carrier's owner as it boards
    (its fleet's keep-owner setting takes it back). A device hosting a replicant only rides its own replicant's carrier."""
    from rsweb import loadouts as lo
    from rsweb.modular import prepare_moves
    D = lambda code, t, loc, owner, **kw: {"device_code": code, "device_type": t, "location": loc, "status": "idle",  # noqa: E731
                                          "replicant_code": owner, **kw}
    devices = [
        D("MD1", "mining_drone", "ITH-BELT-1", "R1", tags=["fleet:m1"]),
        D("PLATE", "surge_plate", "ITH-BELT-1", "R2", features=["surge", "attach"], attach_capacity=4, tags=[]),
        D("MINE", "mobile_fleet", "KEL-4-L4", "R1", features=["surge", "stow"], stow_capacity=36, tags=["fleet:m1"]),
    ]
    stars = {"ITH": {"position": {"x": 0, "y": 0, "z": 0}}, "KEL": {"position": {"x": 1, "y": 0, "z": 0}, "entry_point": "KEL-4-L4"}}
    fleets = [{"id": "m1", "name": "Miner 1", "home": "KEL", "station": True, "wants": {}}]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert [(dl["carrier"], dl["devices"]) for dl in p["deliveries"]] == [("PLATE", ["MD1"])]   # the one already there
    steps = prepare_moves(lo.delivery_steps(p["deliveries"][0], p["by_code"], stars, True), devices)
    descs = [s["desc"] for s in steps]
    assert descs.index("MD1: hand to R2 (owner of carrier PLATE)") + 1 == descs.index("PLATE: attach MD1")
    # same owner available in the system too: that one is preferred
    devices.append(D("OWN", "surge_plate", "ITH-BELT-1", "R1", features=["surge", "attach"], attach_capacity=1, tags=[]))
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert [dl["carrier"] for dl in p["deliveries"]] == ["OWN"]
    assert not any("hand to" in s["desc"] for s in prepare_moves(lo.delivery_steps(p["deliveries"][0], p["by_code"], stars, True), devices))
    # a vessel hosting a replicant is never handed over: it waits for its own replicant's carrier
    devices = [D("HV", "heaven_vessel", "ITH-1-L4", "R1", tags=["fleet:m1"], hosting_replicant={"code": "R1"}, features=["stow"]),
               D("PLATE", "surge_plate", "ITH-1-L4", "R2", features=["surge", "attach"], attach_capacity=4, tags=[])]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert not p["deliveries"]


def test_deal_pickup_plan_uses_the_nearest_stockpiles():
    from rsweb import fleets as fl
    fleet = {"id": "t", "name": "Traders", "role": "trade", "home": "FAL", "wants": {}}
    devices = [{"device_code": c, "device_type": "cargo_freighter", "location": "FAL-1-L4", "features": ["surge"],
                "cargo_capacity": 300, "cargo_used": 0, "tags": ["fleet:t"]} for c in ("F1", "F2")]
    inv = {"FAL-BELT-1": {"carbon": 1000, "silicates": 50}, "KEL-3": {"silicates": 500}, "SITE-2": {"carbon": 100},
           "FAR-BELT-1": {"silicates": 900}}
    pos = {"FAL": 0, "KEL": 1, "SITE": 2, "FAR": 50}
    need = fl.site_short({"carbon": 450, "silicates": 200}, inv["SITE-2"])
    assert need == {"carbon": 350, "silicates": 200}
    legs, left, problems = fl.pickup_plan(fleet, devices, need, inv, "SITE-2", lambda a, b: abs(pos[a] - pos[b]))
    # KEL is nearest the site: its silicates first; then FAL's carbon; FAR is never needed
    assert [(x["freighter"], x["pile"], x["take"]) for x in legs] == [
        ("F1", "KEL-3", {"silicates": 200}), ("F1", "FAL-BELT-1", {"carbon": 100}), ("F2", "FAL-BELT-1", {"carbon": 250})]
    assert not left and not problems
    steps = fl.pickup_steps(legs, devices)
    assert [s["desc"] for s in steps][:3] == ["F1 → KEL-3 (pick up)", "F1: load 200 silicates at KEL-3", "F1 → FAL-BELT-1 (pick up)"]
    # not enough anywhere: says what's missing
    legs, left, problems = fl.pickup_plan(fleet, devices, {"rares": 10}, inv, "SITE-2", lambda a, b: 0)
    assert not legs and left == {"rares": 10} and "missing" in problems[0]


def test_contract_fleet_delivers_waits_fulfils_and_brings_the_rewards(client):
    from rsweb import fleets as fl
    eng = client.app.state.worker.automations
    db = client.app.state.db
    devices = [{"device_code": "F1", "device_type": "cargo_freighter", "location": "SOL-BELT-1", "features": ["surge"],
                "cargo_capacity": 500, "cargo_used": 0, "tags": ["fleet:t"], "status": "idle"},
               {"device_code": "HV", "device_type": "heaven_vessel", "location": "SOL-3-L4", "features": ["surge"],
                "stow_capacity": 10, "tags": ["fleet:t"], "status": "idle"}]
    fleet = {"id": "t", "name": "Traders", "role": "trade", "home": "SOL", "wants": {}, "station": False, "materials": ""}
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-BELT-1", "items": [{"resource_type": "carbon", "quantity": 900}]}])
    client.portal.call(db.kv_set, "replicants", {"R1": {"name": "Joe", "hosted_device_code": "HV", "location": "SOL-3-L4"}})
    m = {"status": "running", "phase": None, "idx": 0, "targets": ["SOL"], "opts": {}, "log": [],
         "contract": {"designation": "EV-1", "location": "SOL-4", "title": "Help them", "price": {"carbon": 200},
                      "rewards": {"rares": 40}}}
    steps, problems = client.portal.call(eng.fleet_phase_steps, fleet, m, "load", devices)
    assert [s["desc"] for s in steps] == ["F1: load 200 carbon at SOL-BELT-1"] and m["loaded"] == ["F1"] and not problems
    steps, _ = client.portal.call(eng.fleet_phase_steps, fleet, m, "deliver", devices)
    assert [s["desc"] for s in steps] == ["F1 → SOL-4", "F1: deposit at SOL-4", "HV → SOL-4 (brings its replicant)"]
    steps, _ = client.portal.call(eng.fleet_phase_steps, fleet, m, "trade", devices)
    assert steps[0]["path"] == "/locations/SOL-4/events/EV-1" and steps[0]["method"] == "POST"
    devices[0]["location"] = "SOL-4"
    steps, _ = client.portal.call(eng.fleet_phase_steps, fleet, m, "collect", devices)
    assert steps[0]["body"] == {"command": "collect_resources", "resources": {"rares": 40}} and m["drop_star"] == "SOL"
    # a trade executes on the trader's controller
    t = {"status": "running", "log": [], "trade": {"controller": "TC1", "trade_code": "TRD-1", "location": "KEL-2", "price": {}}}
    steps, _ = client.portal.call(eng.fleet_phase_steps, fleet, t, "trade", devices)
    assert steps[0]["path"] == "/devices/TC1/trades/TRD-1"
    # the wait phase holds until the whole price is at the site and the fleet's own replicant (Joe rides on HV) is there
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-BELT-1", "items": [{"resource_type": "carbon", "quantity": 700}]},
                                                {"location": "SOL-4", "items": [{"resource_type": "carbon", "quantity": 200}]}])
    client.portal.call(db.kv_set, "replicants", {"R1": {"name": "Joe", "hosted_device_code": "HV", "location": "SOL-3-L4"}})
    m.update({"phase": "wait", "job": None, "status": "running"})
    client.portal.call(eng.save_fleets, [{**fleet, "mission": m}])
    client.portal.call(eng.run_fleets)
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "t")
    assert f["mission"]["phase"] == "wait" and "waiting for Joe (the fleet's replicant)" in f["mission"]["watch_note"]
    client.portal.call(db.kv_set, "replicants", {"R1": {"name": "Joe", "hosted_device_code": "HV", "location": "SOL-4"}})
    client.portal.call(eng.run_fleets)
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "t")
    assert f["mission"]["phase"] == "trade" and any("Joe at SOL-4: fulfilling" in x["text"] for x in f["mission"]["log"])
    # the Fleets page offers open contracts to a trade fleet, and starting one targets its system
    disc = {"designation": "SOL-3-L4-EVT-009", "location": "SOL-3-L4", "title": "Atmospheric Harvest", "tier": 1,
            "criteria": [{"name": "default", "resources": {"carbon": 150}, "devices": []}], "rewards": {"resources": {"volatiles": 200}}}
    client.portal.call(client.app.state.worker.handle_event, {"id": "4444444444499-0", "event": "event.discovered",
                       "category": "event", "location": "SOL-3-L4", "star": "SOL", "payload": disc,
                       "created_at": "2026-10-07T10:00:00+00:00"})
    client.portal.call(eng.save_fleets, [{**fleet, "mission": None}])
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Atmospheric Harvest @ SOL-3-L4" in page and 'name="contract"' in page
    import json as _json
    client.post("/fleets/t/mission", data={"contract": _json.dumps({"designation": "SOL-3-L4-EVT-009", "location": "SOL-3-L4",
                                                                    "title": "x", "price": {"carbon": 150}, "rewards": {}})},
                headers=HX)
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "t")
    assert f["mission"]["targets"] == ["SOL"] and f["mission"]["contract"]["designation"] == "SOL-3-L4-EVT-009"


def test_supply_links_between_fleets():
    from datetime import datetime, timedelta, timezone
    from rsweb import fleets as fl
    now = datetime.now(timezone.utc)
    fleets = [{"id": "m", "name": "Miners", "role": "mining", "home": "ABC", "materials": "f"},
              {"id": "f", "name": "Factory", "role": "mining", "home": "XYZ", "materials": "self"},
              {"id": "d", "name": "Deliverers", "role": "mining", "home": "ABC",
               "mission": {"status": "running", "phase": "work", "targets": ["KEL-BELT-1"], "idx": 0, "deliver_to": "XYZ-3-L4"}},
              {"id": "t", "name": "Traders", "role": "trade", "home": "XYZ",
               "mission": {"status": "running", "phase": "collect", "contract": {"location": "QRS-2", "designation": "C1"},
                           "drop_star": "ABC"}},
              {"id": "o", "name": "Old", "role": "trade", "home": "XYZ",
               "mission": {"status": "done", "contract": {"location": "NOPE-1"}}}]
    links = {(x["from"], x["to"], x["kind"]): x for x in fl.supply_links(fleets, [])}
    # configured only: planned; missions in progress: active; finished missions draw nothing
    assert links[("ABC", "XYZ", "materials")]["state"] == "planned"
    assert links[("ABC", "XYZ", "materials")]["from_fleet"] == "Miners" and links[("ABC", "XYZ", "materials")]["to_fleet"] == "Factory"
    assert links[("KEL", "XYZ", "materials")]["state"] == "active" and links[("KEL", "XYZ", "materials")]["to_fleet"] == "Factory"
    assert links[("XYZ", "QRS", "trade")]["state"] == "active" and links[("QRS", "ABC", "trade")]["state"] == "active"
    assert not any("NOPE" in k for k in links)
    # a ferry controller running the route: ferrying, with its freighters; something traveling along it: moving
    devices = [{"device_code": "TC", "device_type": "ami_transport_controller", "location": "ABC-2",
                "ami_directive": {"name": "ferry", "config": {"collect": "ABC-2", "deliver": "XYZ-3-L4"}}},
               {"device_code": "F1", "device_type": "cargo_freighter", "controller_device_code": "TC", "location": "ABC-2"},
               {"device_code": "F2", "device_type": "cargo_freighter", "controller_device_code": "TC", "location": "ABC-2"}]
    x = {(l["from"], l["to"]): l for l in fl.supply_links(fleets, devices)}[("ABC", "XYZ")]
    assert x["state"] == "ferrying" and x["freighters"] == 2 and x["in_transit"] == 0
    devices[1]["travel"] = {"origin": "ABC-2", "destination": "XYZ-3-L4", "departed_at": (now - timedelta(minutes=5)).isoformat(),
                            "arrives_at": (now + timedelta(minutes=5)).isoformat()}
    x = {(l["from"], l["to"]): l for l in fl.supply_links(fleets, devices)}[("ABC", "XYZ")]
    assert x["state"] == "moving" and x["in_transit"] == 1
    # a fleet sending to itself or to a fleet in the same system draws no line
    assert fl.supply_links([{"id": "a", "home": "ABC", "materials": "b"}, {"id": "b", "home": "ABC", "materials": "self"}], []) == []


def test_supply_range_keeps_prints_and_spares_near_the_fleet():
    from rsweb import loadouts as lo
    D = lambda code, t, loc, **kw: {"device_code": code, "device_type": t, "location": loc, "status": "idle",  # noqa: E731
                                    "operational_capacity": 100.0, **kw}
    devices = [D("AF1", "autofactory", "FALQ-3-L4", available_commands=["enqueue_print"]),
               D("SP1", "mining_drone", "FALQ-BELT-1", tags=["spare"])]
    bps = [{"device_type": "mining_drone", "resources": {"structural": 10}, "print_time": 60},
           {"device_type": "autofactory", "resources": {"structural": 100}, "print_time": 600, "queue_size": 10}]
    inv = {"FALQ-3-L4": {"structural": 1000}}
    stars = {k: {"position": {"x": x, "y": 0, "z": 0}} for k, x in (("FALQ", 0), ("SOL", 40), ("NEAR", 5))}

    def run(home, reach):
        cfg = lo.normalize({"phases": [], "settings": {"max_supply_ly": reach}, "fleets_migrated": True,
                            "fleets": [{"id": "f1", "name": "F1", "role": "mining", "home": home, "station": True,
                                        "wants": {"mining_drone": 2}}]})
        return lo.plan(cfg, devices, bps, inv, stars, {}, set(), [], {})
    far = run("SOL", 20)
    assert not far["prints"] and not far.get("moves")
    assert any("no autofactory within 20 ly of SOL" in u["why"] for u in far["unmet"])
    near = run("NEAR", 20)
    assert near["prints"] and near["prints"][0]["factory"] == "AF1"
    assert run("SOL", 0)["prints"]                                     # 0 = any distance, as before
    from rsweb.logistics import SECTOR_RANGE_LY
    assert lo.normalize({})["settings"]["max_supply_ly"] == SECTOR_RANGE_LY   # the default: the sector range
    assert lo.normalize({"settings": {"max_supply_ly": 15}})["settings"]["max_supply_ly"] == SECTOR_RANGE_LY   # old defaults
    assert lo.normalize({"settings": {"max_supply_ly": 100, "supply_range_v2": True}})["settings"]["max_supply_ly"] == SECTOR_RANGE_LY
    kept = lo.normalize({"settings": {"max_supply_ly": 15, "supply_range_v2": True, "supply_range_v3": True}})["settings"]["max_supply_ly"]
    assert kept == 15                                                  # chosen after the change: kept


def test_contract_device_delivery_waits_for_print_authorization(client):
    eng, db, w = client.app.state.worker.automations, client.app.state.db, client.app.state.worker
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-BELT-1", "items": [{"resource_type": "carbon", "quantity": 900}]}])
    client.portal.call(db.kv_set, "blueprints", [{"device_type": "ftl_relay", "resources": {"carbon": 40}, "print_time": 600}])
    client.portal.call(w.handle_event, {**_ev(997, "event.discovered", designation="AEM-2-EVT-001", location="AEM-2",
                                                title="Relay Network", criteria=[{"name": "default", "resources": {"carbon": 50},
                                                                                  "devices": [{"device_type": "ftl_relay", "quantity": 2}]}]),
                                        "location": "AEM-2"})
    client.post("/game-events/approve", data={"key": "body:AEM-2", "on": "1"}, headers=HX)   # its species approved
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": "S1", "device_type": "ftl_relay", "location": "SOL-3", "status": "idle", "tags": ["spare"]},
                {"device_code": "F1", "device_type": "ftl_relay", "location": "SOL-3", "status": "idle", "tags": ["fleet:x"]},
                {"device_code": "AF9", "device_type": "autofactory", "location": "SOL-BELT-1", "status": "idle",
                 "available_commands": ["enqueue_print"], "tags": []}]
    client.portal.call(db.kv_set, "devices", devices)
    assert client.portal.call(eng.contract_supply_pass, True) == []    # nothing happens until you ask for it
    client.post("/game-events/AEM-2-EVT-001/supply", data={"action": "start"}, headers=HX)
    jobs = [j for j in client.portal.call(eng.jobs) if j["rule"] == "contracts"]
    assert len(jobs) == 1 and "S1" in jobs[0]["title"]                 # the spare goes; the fleet's relay doesn't
    tags = jobs[0]["steps"][0]["body"]["configuration"]
    assert set(tags["add_tags"]) == {"to:aem", "at:aem-2", "contract:aem-2-evt-001"} and tags["remove_tags"] == ["spare"]
    sup = client.portal.call(db.kv_get, "contract_supply")["AEM-2-EVT-001"]
    row = sup["rows"][0]
    assert row["short"] == 1 and row["print"] == 0 and "authorize" in row["why"]   # one more: no print without a yes
    page = client.get("/game-events", headers=H).text
    assert "Authorize printing" in page and "Stop delivering devices" in page
    # once the tag shows, S1 counts as on its way; authorizing prints the other one, tagged for the contract
    for d in devices:
        if d["device_code"] == "S1":
            d["tags"] = ["to:aem", "at:aem-2", "contract:aem-2-evt-001"]
    client.portal.call(db.kv_set, "devices", devices)
    client.post("/game-events/AEM-2-EVT-001/authorize-prints", headers=HX)
    jobs = [j for j in client.portal.call(eng.jobs) if j["rule"] == "contracts"]
    pj = [j for j in jobs if "print" in j["title"]]
    assert len(pj) == 1 and pj[0]["device"] == "AF9"
    body = pj[0]["steps"][0]["body"]
    assert body["command"] == "enqueue_print" and body["quantity"] == 1
    assert set(body["tags"]) == {"to:aem", "at:aem-2", "contract:aem-2-evt-001"}
    client.portal.call(db.kv_set, "contract_supply_last", None)
    client.portal.call(eng.contract_supply_pass, True)
    jobs = [j for j in client.portal.call(eng.jobs) if j["rule"] == "contracts"]
    assert len(jobs) == 2                                              # the print counts as coming: nothing more
    row = client.portal.call(db.kv_get, "contract_supply")["AEM-2-EVT-001"]["rows"][0]
    assert (row["coming"], row["printing"], row["short"]) == (["S1"], 1, 0)
    # the print never came out (no autofactory holds it): Restart prints forgets it and prints it again
    client.post("/game-events/AEM-2-EVT-001/restart-prints", headers=HX)
    sup = client.portal.call(db.kv_get, "contract_supply")["AEM-2-EVT-001"]
    assert len(sup["orders"]) == 1 and sup["print_ok"]["ftl_relay"] == 1
    assert len([j for j in client.portal.call(eng.jobs) if j["rule"] == "contracts" and "print" in j["title"]]) == 2
    # the contract closes: the leftover relay goes back to spare
    client.portal.call(w.handle_event, {**_ev(998, "event.completed", designation="AEM-2-EVT-001", location="AEM-2"),
                                        "location": "AEM-2"})
    client.portal.call(eng.contract_supply_pass, True)
    rel = [j for j in client.portal.call(eng.jobs) if j["rule"] == "contracts" and "back to spare" in j["title"]]
    assert len(rel) == 1
    cfgx = rel[0]["steps"][0]["body"]["configuration"]
    assert cfgx["add_tags"] == ["spare"] and set(cfgx["remove_tags"]) == {"to:aem", "at:aem-2", "contract:aem-2-evt-001"}
    assert "AEM-2-EVT-001" not in client.portal.call(db.kv_get, "contract_supply")


def test_materials_in_transit_on_logistics_and_dashboard(client):
    """What's in holds: per device (from its last transport.collected), its trip, and totals on the dashboard."""
    from rsweb import cargo
    db = client.app.state.db
    client.portal.call(db.insert_event, {"id": "tc-1", "event": "transport.collected", "device_code": "FR1",
                                          "payload": {"cargo_after": {"structural": 120, "carbon": 30}}})
    devs = [{"device_code": "FR1", "device_type": "cargo_freighter", "location": "SOL-3", "status": "travelling",
             "travel": {"destination": "FALQUORYX-3", "eta_seconds": 3600}},
            {"device_code": "FR2", "device_type": "cargo_freighter", "location": "SOL-3", "status": "idle"}]
    out = client.portal.call(cargo.in_transit, db, devs, [])
    assert [r["code"] for r in out["rows"]] == ["FR1"] and out["rows"][0]["dest"] == "FALQUORYX-3"
    assert out["totals"] == {"structural": 120, "carbon": 30}
    assert "In transit" in client.get("/fleets/logistics", headers=H).text


def test_dashboard_stockpiles_card_charts_each_resource(client):
    """1.338.0: the Stockpiles card draws like the other charts: a chart per resource, its own color, 2d / 7d / 30d."""
    from datetime import datetime, timedelta, timezone
    db = client.app.state.db
    now = datetime.now(timezone.utc)
    for i, (c, r) in enumerate([(100, 5), (140, 7)]):
        ts = (now - timedelta(hours=2 - i)).isoformat(timespec="seconds")
        for res, q in (("carbon", c), ("rares", r)):
            client.portal.call(db.execute, "INSERT INTO inventory_history(ts, resource, qty) VALUES(?,?,?)", (ts, res, q))
    assert 'hx-get="/partials/backlog?card=stock&amp;hours=48"' in client.get("/", headers=H).text \
        or 'hx-get="/partials/backlog?card=stock&hours=48"' in client.get("/", headers=H).text
    page = client.get("/partials/backlog?card=stock&hours=48", headers=HX).text
    assert "<h2>Stockpiles</h2>" in page and "card=stock&amp;hours=168" in page.replace("card=stock&hours", "card=stock&amp;hours")
    import re
    keys = re.findall(r'<div class="small">([a-z]+)[^<]*</div>\s*<div class="small"><span class="bl-key (bl-\d+)', page)
    assert ("carbon", "bl-4") in keys and ("rares", "bl-6") in keys


def test_supplying_fleet_sends_to_the_nearest_receiver(client):
    from rsweb import fleets as fl
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": d, "position": {"x": x, "y": 0, "z": 0}}
                                                      for d, x in (("MINE", 0), ("NEARHUB", 3), ("FARHUB", 40))]})
    client.portal.call(eng.save_fleets, [
        {"id": "far", "name": "Far hub", "role": "mining", "home": "FARHUB", "wants": {}, "materials": "self"},
        {"id": "near", "name": "Near hub", "role": "mining", "home": "NEARHUB", "wants": {}, "materials": "self"},
        {"id": "m", "name": "Miner", "role": "mining", "home": "MINE", "wants": {}, "materials": ""}])
    r = client.post("/fleets/m/station", data={"materials": "supply"}, headers=HX)
    assert r.status_code == 200
    items = client.portal.call(eng.fleets)
    m = next(f for f in items if f["id"] == "m")
    assert m["materials"] == "supply" and fl.materials_target(m, items)["id"] == "near"
    t = client.get("/fleets/overview", headers=HX).text
    assert "supplying the nearest receiver" in t and "to Near hub (NEARHUB), the nearest" in t


def test_only_logistics_carriers_deliver_a_fleets_carrier_only_fetches_its_own():
    """Live 2026-10-18: Miner 0.5's cargo vessel was borrowed to carry compute cores for a simulation, and flew off with
    fleet members aboard while the fleet's move waited. Deliveries are the logistics fleets' work (or a spare carrier's)."""
    from rsweb import loadouts as lo
    D = lambda code, t, loc, **kw: {"device_code": code, "device_type": t, "location": loc, "status": "idle",  # noqa: E731
                                    "replicant_code": "R1", **kw}
    devices = [D("MF", "mobile_fleet", "FAL-1-L4", features=["surge", "attach"], attach_capacity=36, tags=["fleet:m1"]),
               D("CC", "compute_core", "FAL-1-L4", tags=["to:kel"])]
    stars = {s: {"position": {"x": i, "y": 0, "z": 0}, "entry_point": f"{s}-1-L4"} for i, s in enumerate(("FAL", "KEL", "LAR"))}
    fleets = [{"id": "m1", "name": "Miner 1", "home": "LAR", "station": True, "wants": {"mobile_fleet": 1}}]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert not any("CC" in x["devices"] for x in p["deliveries"])
    devices[0]["tags"] = []   # a spare carrier, in no fleet, still delivers
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert [x["carrier"] for x in p["deliveries"] if "CC" in x["devices"]] == ["MF"]


def test_lockers_deliver_and_show_on_the_system_page(client):
    db = client.app.state.db
    page = client.get("/lockers", headers=H).text
    assert "0 of 50 compute core there" in page and "Deliver" in page
    devs = client.portal.call(db.kv_get, "devices") or []
    devs[0] = {**devs[0], "device_type": "compute_core", "tags": ["spare"]}   # a device the mock game knows, as a spare core
    client.portal.call(db.kv_set, "devices", devs)
    r = client.post("/lockers/deliver", data={"location": "TWAFFY-OORT"}, headers=HX).text
    assert "compute core: 1 spare(s) sent" in r, r
    assert "TWAFFY-OORT" in client.get("/systems/TWAFFY", headers=H).text
    # all 50 on site: the row offers Retrieve, which scans from the replicant there and retrieves (2026-10-22: the row
    # kept offering Deliver, which never retrieves)
    world = client.app.state.api.http._transport.app.state.world
    cores = [{"device_code": f"CC{i:06d}", "device_type": "compute_core", "location": "TWAFFY-OORT", "status": "idle",
              "tags": []} for i in range(50)]
    client.portal.call(db.kv_set, "devices", devs + cores)
    r = client.post("/lockers/retrieve_at", data={"location": "TWAFFY-OORT"}, headers=HX).text
    assert "No replicant of yours is at TWAFFY-OORT" in r
    client.portal.call(db.kv_set, "devices", devs + cores + [
        {"device_code": "CV000001", "device_type": "cargo_vessel", "location": "TWAFFY-OORT", "status": "idle",
         "hosting_replicant": {"name": "Percival", "replicant_code": "8EC42FBA"}}])
    page = client.get("/lockers", headers=H).text
    assert "50 of 50 compute core there" in page and "/lockers/retrieve_at" in page
    world.foreign_devices = [{"device_code": "70C835AC", "device_type": "equipment_locker", "location": "TWAFFY-OORT"}]
    try:
        r = client.post("/lockers/retrieve_at", data={"location": "TWAFFY-OORT"}, headers=HX).text
    finally:
        world.foreign_devices = []
    assert "retrieved" in r and "HV2E8F3A" in r, r


def test_relay_rollout_capacity_is_free_hold_on_logistics_vessels_with_a_replicant():
    """Owner 2026-10-20: no hard cap; what each carrier takes stays within its free stowage."""
    from rsweb import relayroll as rr
    from rsweb.loadouts import _free
    devs = [{"device_code": "HV", "device_type": "heaven_vessel", "tags": ["fleet:logistics"], "features": ["surge"],
             "stow_capacity": 10, "hosting_replicant": {"replicant_code": "R1"},
             "stowed_devices": [{"device_code": "M"}, {"device_code": "X"}]},
            {"device_code": "CV", "device_type": "cargo_vessel", "tags": ["fleet:logistics"], "features": ["surge"],
             "stow_capacity": 50},                                                   # no replicant aboard: not an escort
            {"device_code": "MV", "device_type": "heaven_vessel", "tags": ["fleet:miner-1"], "features": ["surge"],
             "stow_capacity": 10, "hosting_replicant": {"replicant_code": "R2"}}]    # not logistics
    assert rr.capacity(devs, {"fleet:logistics"}, lambda d: _free(d, {}, {})) == 8


def test_logistics_replicant_vessels_carry_inside_the_network():
    """Owner 2026-10-20: every vessel in a logistics fleet is there for automation (the old 'use replicant vessels'
    setting is gone); another fleet's replicant vessel isn't borrowed."""
    from rsweb import loadouts as lo
    def dev(c, t, loc, **kw):
        return {"device_code": c, "device_type": t, "location": loc, "status": "idle", "operational_capacity": 100.0,
                "features": kw.pop("features", ["cruise", "stow"]), "available_commands": ["travel", "stow", "deploy"], **kw}
    f = {"id": "m", "name": "M", "role": "mining", "home": "BBB", "station": True, "wants": {"mining_drone": 2}}
    drones = [dev(c, "mining_drone", "AAA-BELT-1", tags=["fleet:m"]) for c in ("D1", "D2")]
    hv = dev("HV", "heaven_vessel", "AAA-1", features=["surge", "cruise"], stow_capacity=10,
             hosting_replicant={"replicant_code": "R1"}, tags=["fleet:logistics"])
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in (("AAA", 0), ("BBB", 5))}
    cfg = {"phases": [], "systems": {}, "fleets": [f, {"id": "logistics", "name": "L", "role": "logistics", "home": "AAA",
                                                        "wants": {}},
                                                   {"id": "s", "name": "S", "role": "explore", "home": "AAA", "wants": {}}]}
    p = lo.plan(cfg, drones + [hv], [], {}, stars, {"HV": "R1"}, set(), [], {}, commandable={"AAA", "BBB"})
    assert [(d["carrier"], d["devices"]) for d in p["deliveries"]] == [("HV", ["D1", "D2"])]
    other = {**hv, "tags": ["fleet:s"]}
    p = lo.plan(cfg, drones + [other], [], {}, stars, {"HV": "R1"}, set(), [], {}, commandable={"AAA", "BBB"})
    assert not p["deliveries"]


def test_an_approved_trade_keeps_logistics_replicant_vessels_out_of_loadouts(client):
    """Owner 2026-10-20: a shop trade must complete before it expires; device runs mustn't tie up its only escorts."""
    from rsweb import logistics as lg
    eng, db = client.app.state.worker.automations, client.app.state.db
    seen = {}

    def fake_plan(*a, **kw):
        seen["busy"] = set(a[6])
        raise RuntimeError("stop")
    import rsweb.loadouts as lo
    real = lo.plan
    lo.plan = fake_plan
    try:
        devices = client.portal.call(db.kv_get, "devices")
        hv = next(d for d in devices if d["device_type"] == "heaven_vessel")
        hv["tags"] = ["fleet:logistics"]
        # a second one, far away: a fleet carrier towing a replicant's matrix container stays free for relay runs
        reps = client.portal.call(db.kv_get, "replicants", {}) or {}
        reps["R9"] = {"hosted_device_code": "MC9", "location": "ALPHA-1"}
        client.portal.call(db.kv_set, "replicants", reps)
        devices += [{"device_code": "MF9", "device_type": "mobile_fleet", "location": "ALPHA-1", "status": "idle",
                     "features": ["surge", "cruise", "attach"], "tags": ["fleet:logistics"]},
                    {"device_code": "MC9", "device_type": "matrix_container", "location": "ALPHA-1", "status": "idle",
                     "features": ["cruise", "cradle"], "attached_to_device_code": "MF9"}]
        client.portal.call(db.kv_set, "devices", devices)
        client.portal.call(eng.save_fleets, [{"id": "logistics", "name": "Logistics", "role": "logistics", "home": "SOL",
                                              "wants": {}}])
        for state, held in (({"requests": []}, False),
                            ({"requests": [{"kind": "trade", "id": "trade:x", "to": hv["location"], "skipped": False}]}, True)):
            client.portal.call(db.kv_set, lg.KV_STATE, state)
            try:
                client.portal.call(eng.loadout_plan)
            except RuntimeError:
                pass
            assert (hv["device_code"] in seen["busy"]) is held and "MF9" not in seen["busy"]
    finally:
        lo.plan = real


def test_logistics_drop_off_keeps_replicants_aboard():
    from rsweb.logistics import stray_aboard
    devs = [{"device_code": "M1", "device_type": "replicant_matrix", "features": ["stow", "matrix"], "stowed_in_device_code": "LV"},
            {"device_code": "C1", "device_type": "matrix_container", "features": ["cruise", "cradle"], "attached_to_device_code": "LV"},
            {"device_code": "M2", "device_type": "replicant_matrix", "stowed_in_device_code": "C1"},
            {"device_code": "P1", "device_type": "odd_pod", "features": [], "stowed_in_device_code": "LV"},
            {"device_code": "M3", "device_type": "empty_replicant_matrix", "stowed_in_device_code": "P1"},
            {"device_code": "D1", "device_type": "survey_drone", "features": ["stow"], "stowed_in_device_code": "LV"}]
    assert stray_aboard({"device_code": "LV"}, devs, set(), set()) == [{"code": "D1", "mode": "stow"}]


def test_relays_bound_for_a_covered_system_or_twice_for_one_are_made_spare_before_delivery():
    """Owner 2026-10-21: an extra relay is marked spare where it waits instead of being delivered."""
    from rsweb import loadouts as lo
    def dev(code, t, loc, **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": kw.pop("status", "inactive"),
                "operational_capacity": 100.0, "features": kw.pop("features", ["cruise", "stow"]),
                "available_commands": ["travel", "stow", "deploy"], **kw}
    devices = [dev("W1", "ftl_relay", "AAA-1-L4", status="relaying"),
               dev("R1", "ftl_relay", "HUB-1", tags=["to:aaa"]),                       # AAA already relays
               dev("R2", "ftl_relay", "HUB-1", tags=["to:bbb"]), dev("R3", "ftl_relay", "HUB-1", tags=["to:bbb"]),
               dev("HV", "heaven_vessel", "HUB-1", features=["surge", "cruise"], stow_capacity=5,
                   hosting_replicant={"replicant_code": "X"}, tags=["fleet:logistics"])]
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in (("HUB", 0), ("AAA", 5), ("BBB", 9))}
    cfg = {"phases": [], "systems": {}, "fleets": [{"id": "logistics", "name": "Logistics", "role": "logistics",
                                                    "home": "HUB", "wants": {}}]}
    p = lo.plan(cfg, devices, [], {}, stars, {"HV": "X"}, set(), [], {}, commandable={"HUB", "AAA"})
    for code, tag in (("R1", "to:aaa"), ("R3", "to:bbb")):
        assert "spare" in p["tag_add"][code] and tag in p["tag_remove"][code]
    sent = {x for d in p["deliveries"] for x in d["devices"]}
    assert "R2" in sent and not sent & {"R1", "R3"}


def test_dashboard_stockpiles_by_location(client):
    """1.328.0: the Dashboard lists every stockpile's current amounts (per location, per resource)."""
    inv = client.portal.call(lambda: client.app.state.db.kv_get("inventory", []))
    piles = [i for i in inv if any(float(v or 0) > 0 for v in (i.get("items") or {}).values())]
    assert piles
    r = client.get("/", headers=H)
    assert r.status_code == 200 and "Stockpiles by location" in r.text and 'data-card="stock-locations"' in r.text
    for p in piles:
        assert f'href="/locations/{p["location"]}"' in r.text
