"""Core tests: print queues, blueprints, factories and components (split out of test_core.py by test name)."""
from corelib import *  # noqa: F401,F403


def test_printers_mark_every_vessel():
    from rsweb.web import printers
    devs = [{"device_code": "R1", "device_type": "racing_vessel", "features": ["print"]},
            {"device_code": "A1", "device_type": "autofactory", "features": ["print"]}]
    ps = {p["code"]: p["vessel"] for p in printers({"devices": devs, "replicants": {"X": {"name": "X"}}})}
    assert ps == {"R1": True, "A1": False, "X": True}  # the hide vessels toggle covers racing vessels too


def test_blueprints_with_list_shaped_inventory(client):
    r = client.get("/blueprints", headers=H)
    assert r.status_code == 200 and "×" in r.text
    assert "bp-hide-vessels" in r.text and "afford-vessel" in r.text  # vessels marked for the hide vessels toggle
    r = client.post("/blueprints/plan", data={"location": "SOL-BELT-1", "qty:mining_drone": "1"}, headers=HX)
    import re
    have = re.search(r"<td>structural</td><td class=\"num\">[^<]+</td><td class=\"num\">([\d,.]+)</td>", r.text)
    assert have and float(have.group(1).replace(",", "")) >= 1200  # read from the list-shaped mock inventory


def test_new_blueprint_detected_and_announced(client):
    from rsweb import mock as mockmod
    from rsweb.ingest import may_unlock_blueprint
    w = client.app.state.worker
    extra = {"device_type": "ftl_relay", "short_description": "Relays", "print_time": 900, "resources": {"rares": 50}}
    mockmod.BLUEPRINTS.append(extra)
    try:
        added = client.portal.call(w.sync_blueprints)
    finally:
        mockmod.BLUEPRINTS.remove(extra)
    assert added == ["ftl_relay"]
    page = client.get("/notifications", headers=H).text
    assert "New blueprint unlocked: ftl relay" in page
    bp = client.get("/blueprints", headers=H).text
    assert "ftl relay" not in bp or "new" in bp  # page re-syncs; relay gone again from mock is fine
    d = client.get("/digest?hours=1", headers=H).text
    assert "New blueprints" in d and "ftl relay" in d
    # heuristics
    assert may_unlock_blueprint({"event": "device.decommissioned", "category": "device", "payload": {"blueprint_discovered": "x"}})
    assert may_unlock_blueprint({"event": "event.completed", "category": "event", "payload": {}})
    assert not may_unlock_blueprint({"event": "mining.started", "category": "mining", "payload": {"site": "A"}})
    assert not may_unlock_blueprint({"event": "experience.gained", "category": "experience", "payload": {"amount": 5}})


def test_vessel_prints_directly_and_explains_busy(client):
    # heaven vessels have no queue: one print through the replicant endpoint
    r1 = client.post("/blueprints/print", data={"printer": "replicant:77F75255", "device_type": "mining_drone", "quantity": "2"}, headers=HX)
    assert "result ok" in r1.text and "/replicants/77F75255/print" in r1.text and "only 1 of 2" in r1.text
    r2 = client.post("/replicants/77F75255/print", data={"device_type": "mining_drone"}, headers=HX)
    assert "Printer is busy" in r2.text and "don&#39;t have a queue" in r2.text
    page = client.get("/replicants/77F75255", headers=H).text
    assert "Clear queue" not in page
    # the planner refuses more than one device on a vessel
    r3 = client.post("/blueprints/queue-plan", data={"printer": "replicant:77F75255", "qty:mining_drone": "2", "_planned": "1"}, headers=HX)
    assert "no queue" in r3.text


def test_autofactory_print_queue_panel(client):
    import time
    world = client.app.state.api.http._transport.app.state.world
    world.af_print_seconds = 0.5
    world.queues["AF00BEEF"] = []
    world.devices[7]["status"] = "idle"
    for t, n in (("survey_drone", 1), ("ftl_beacon", 2)):
        r = client.post("/devices/AF00BEEF/print-queue", data={"action": "add", "device_type": t, "quantity": str(n)}, headers=HX)
        assert "result ok" in r.text
    async def once():
        return True
    _pump(client, once)
    page = client.get("/devices/AF00BEEF", headers=H).text
    assert "Print queue" in page and "printing" in page and "survey drone" in page.lower()
    # the printing item is shown above the queue, not in it
    panel = client.get("/devices/AF00BEEF/print-queue", headers=HX).text
    assert panel.count("Remove</button>") == 2 and "ftl beacon" in panel.lower()
    assert "all done in" in panel
    # the panel's #1 Remove button sends the game's 0-based index 0, and the right item goes
    assert '"index": 0}' in panel and 'Remove #1 ' in panel
    waiting = [x.get("device_type") for x in world.queues["AF00BEEF"]]
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "remove", "index": "0"}, headers=HX)
    assert "result ok" in r.text and r.text.count("Remove</button>") == 1
    assert '"index": 0' in client.portal.call(client.app.state.db.fetchone, "SELECT body FROM actions ORDER BY id DESC LIMIT 1")["body"]
    assert [x.get("device_type") for x in world.queues["AF00BEEF"]] == waiting[1:]
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "clear"}, headers=HX)
    assert "Queue is empty" in r.text
    # finishes, and the panel says idle
    time.sleep(0.8)
    _pump(client, once)
    assert "Not printing" in client.get("/devices/AF00BEEF/print-queue", headers=HX).text
    # blueprints tab lists the autofactory's queue panel
    assert "/devices/AF00BEEF/print-queue?compact=1" in client.get("/blueprints", headers=H).text


def test_planner_queues_autofactory_and_gathers_shortfall(client):
    world = client.app.state.api.http._transport.app.state.world
    world.queues["AF00BEEF"] = []
    world.devices[7]["status"] = "printing (survey_drone)"  # busy, so new items stay queued
    page = client.get("/blueprints", headers=H).text
    # autofactory listed first as printer
    sel = page[page.index('name="printer"'):]
    assert sel.index("AF00BEEF") < sel.index("(vessel)")
    data = {"printer": "device:AF00BEEF", "qty:mining_drone": "3"}  # autofactory at SOL-3-L4 has no conductive
    r = client.post("/blueprints/plan", data=data, headers=HX)
    assert "Gather the shortfall with" in r.text and "MC91FF22" in r.text
    assert "Move it from SOL-BELT-1 to SOL-3-L4 with" in r.text and "TC000001" in r.text
    r = client.post("/blueprints/queue-plan", data={**data, "_planned": "1", "gather": "on", "deliver": "on",
                                                    "mining": "MC91FF22", "transport": "TC000001"}, headers=HX)
    assert "follow on Automations" in r.text, r.text
    job = client.portal.call(client.app.state.worker.automations.jobs)[-1]
    assert job["status"] == "done", [(s["desc"], s["status"], s["error"]) for s in job["steps"]]
    bodies = [(s["path"], s["body"]) for s in job["steps"]]
    assert bodies[0] == ("/devices/AF00BEEF", {"command": "enqueue_print", "device_type": "mining_drone", "quantity": 3})
    gather = next(b for p, b in bodies if p == "/devices/MC91FF22" and b["command"] == "set_directive")
    assert gather["directive"] == "gather_resources" and gather["configuration"]["conductive"] == 150
    assert "structural" not in gather["configuration"] or gather["configuration"]["structural"] <= 300
    deliver = next(b for p, b in bodies if p == "/devices/TC000001" and b["command"] == "set_directive")
    assert deliver["configuration"]["route"] == {"collect": "SOL-BELT-1", "deliver": "SOL-3-L4"}
    assert [i["device_type"] for i in world.queues["AF00BEEF"]] == ["mining_drone"] * 3
    # unticking gather survives a plan refresh
    r = client.post("/blueprints/plan", data={**data, "_planned": "1"}, headers=HX)
    assert 'name="gather" >' in r.text or 'name="gather" ' in r.text and 'name="gather" checked' not in r.text


def test_print_queue_uses_printing_block(client):
    from rsweb import printqueue
    dev = {"device_code": "3E95BD59", "status": "printing (maintenance_drone)",
           "printing": {"completes_at": "2026-10-01T16:36:43-04:00", "device_type": "maintenance_drone",
                        "started_at": "2026-10-01T16:21:43-04:00", "tags": ["to:ithvalai"]},
           "print_queue": [{"device_type": "surge_plate", "notify": {"device": None}, "tags": ["to:ithvalai"]}]}
    cur = client.portal.call(printqueue.current, client.app.state.db, dev)
    assert cur["device_type"] == "maintenance_drone" and cur["completes_at"].startswith("2026-10-01T16:36") and cur["tags"] == ["to:ithvalai"]
    assert printqueue.items(dev)[0]["tags"] == ["to:ithvalai"]


def test_real_surge_plates_and_queue_space():
    from rsweb import loadouts as lo
    plate = lambda code, **kw: {"device_code": code, "device_type": "surge_plate", "location": "FFF-1-L4", "status": "idle",  # noqa: E731
                                "attach_capacity": 1, "features": ["surge", "cruise", "attach", "stow", "taxi"],
                                "available_commands": ["attach", "detach", "deploy", "stow", "travel"], **kw}
    devices = [
        plate("TAXI0001", taxi_mode="taxi", tags=["taxi"], controller_device_code="0E158313"),   # serving a ferry
        plate("FREE0001", tags=["home:fff"]),
        {"device_code": "TD000001", "device_type": "transport_drone", "location": "FFF-1-L4", "status": "idle",
         "available_commands": ["collect_resources", "deposit_resources", "recall", "travel"], "tags": ["to:aaa"]},
        {"device_code": "AF", "device_type": "autofactory", "location": "FFF-BELT-1", "status": "printing (maintenance_drone)",
         "available_commands": ["enqueue_print"], "printing": {"device_type": "maintenance_drone"},
         "print_queue": [{"device_type": "surge_plate"}] * 9},
    ]
    bps = [{"device_type": "autofactory", "queue_size": 10}, {"device_type": "survey_drone", "resources": {"structural": 1}}]
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"survey_drone": 2}}], "systems": {"AAA": "p"}}
    stars = {"AAA": {"position": {"x": 0, "y": 0, "z": 0}}, "FFF": {"position": {"x": 1, "y": 0, "z": 0}}}
    p = lo.plan(cfg, devices, bps, {"FFF-BELT-1": {"structural": 100}}, stars, {}, set(), [], {})
    # the transport drone (can't be stowed) rides the free plate, not the taxi plate
    assert [(d["carrier"], d["mode"], d["devices"]) for d in p["deliveries"]] == [("FREE0001", "attach", ["TD000001"])]
    # the factory queue is full (9 queued + 1 printing of 10): no print is attempted
    assert p["prints"] == [] and any("queue is full" in u["why"] for u in p["unmet"])


def test_printed_devices_go_home_on_a_surge_platform():
    from rsweb import loadouts as lo
    devices = [
        {"device_code": "AF", "device_type": "autofactory", "location": "FAL-BELT-1", "status": "printing (survey_drone)",
         "available_commands": ["enqueue_print"], "tags": ["fleet:fal"]},
        {"device_code": "PL000001", "device_type": "surge_platform", "location": "FAL-1-L4", "status": "idle", "attach_capacity": 4,
         "features": ["surge", "cruise", "attach", "taxi"], "taxi_mode": "taxi", "available_commands": ["attach", "detach", "travel"],
         "tags": ["fleet:fal"]},
        # printed here for AEM's stationed fleet (a member already, or to:aem by the print) — both must be delivered
        {"device_code": "SD1", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "idle", "tags": ["fleet:aem"],
         "available_commands": ["travel", "scan"]},
        {"device_code": "SD2", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "idle", "tags": ["to:aem"],
         "available_commands": ["travel", "scan"]},
        # away but working for a controller there: left alone
        {"device_code": "MD1", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "idle", "tags": ["fleet:aem"],
         "controller_device_code": "XC"},
    ]
    stars = {"FAL": {"position": {"x": 0, "y": 0, "z": 0}}, "AEM": {"position": {"x": 1, "y": 0, "z": 0}, "entry_point": "AEM-5-L4"}}
    fleets = [{"id": "aem", "name": "Aem", "home": "AEM", "station": True, "wants": {}},
              {"id": "fal", "name": "Fal", "home": "FAL", "station": True, "wants": {}}]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert p["returning"] == ["SD1"] and not p["deliveries"]   # Fal's platform delivers nothing for another fleet
    devices[1]["tags"] = ["fleet:cab"]                          # a logistics fleet's does
    fleets.append({"id": "cab", "name": "Cab", "role": "logistics", "home": "FAL"})
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert [(d["carrier"], d["mode"], sorted(d["devices"]), d["to"]) for d in p["deliveries"]] == [("PL000001", "attach", ["SD1", "SD2"], "AEM")]
    bodies = [s["body"] for s in lo.delivery_steps(p["deliveries"][0], p["by_code"], stars, True)]
    assert {"command": "travel", "destination": "FAL-1-L4"} in bodies                    # drones fly to the platform first
    assert {"command": "attach", "device": "SD1"} in bodies and {"command": "travel", "destination": "AEM-5-L4"} in bodies
    assert any("send it back to AEM" in line for line in lo.describe(p))
    # on a mission, a fleet's devices are the mission's: nothing is sent home
    fleets[0]["mission"] = {"status": "running"}
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert p["returning"] == [] and "SD1" not in p["by_code"]


def test_print_with_a_destination(client):
    world = client.app.state.api.http._transport.app.state.world
    world.queues["AF00BEEF"] = []
    db = client.app.state.db
    here = next(d for d in world.devices if d["device_code"] == "AF00BEEF")["location"].split("-")[0]
    # the location list for a system
    opts = client.get(f"/print-queue/locations?dest_star={here}", headers=HX).text
    assert f"anywhere in {here}" in opts and f'value="{here}-BELT-1"' in opts
    # same system + a location: the game's oncomplete sends it, and it's pinned
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "add", "device_type": "survey_drone", "quantity": "1",
                                                          "dest_star": here, "dest_loc": f"{here}-3"}, headers=HX)
    assert "result ok" in r.text
    body = json.loads(client.portal.call(db.fetchone, "SELECT body FROM actions ORDER BY id DESC LIMIT 1")["body"])
    assert body["oncomplete"] == {"command": "travel", "destination": f"{here}-3"} and body["tags"] == [f"at:{here.lower()}-3"]
    # another system + a typed location: to: and at: tags, an order counted for that system, no oncomplete
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "add", "device_type": "mining_drone", "quantity": "2",
                                                          "dest_star": "ITHVALAI", "dest_text": "ithvalai-belt-1"}, headers=HX)
    body = json.loads(client.portal.call(db.fetchone, "SELECT body FROM actions ORDER BY id DESC LIMIT 1")["body"])
    assert body["tags"] == ["to:ithvalai", "at:ithvalai-belt-1"] and "oncomplete" not in body
    orders = [o for o in client.portal.call(db.kv_get, "loadout_orders", []) if o.get("manual")]
    assert len(orders) == 2 and orders[0]["star"] == "ITHVALAI" and orders[0]["location"] == "ITHVALAI-BELT-1"
    assert all(x.get("tags") for x in world.queues["AF00BEEF"])
    # a location outside the chosen system is refused
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "add", "device_type": "mining_drone", "quantity": "1",
                                                          "dest_star": "ITHVALAI", "dest_text": "AEMEROTH-3"}, headers=HX)
    assert "AEMEROTH-3 isn&#39;t in ITHVALAI" in r.text


def test_new_print_bound_elsewhere_is_dispatched_at_once(client):
    eng = client.app.state.worker.automations
    db = client.app.state.db

    async def enable():
        s = await eng.settings()
        s["rules"]["loadouts"]["enabled"] = True
        await eng.save_settings(s)
    client.portal.call(enable)
    devices = [
        {"device_code": "AF1", "device_type": "autofactory", "location": "FAL-BELT-1", "status": "printing (mining_drone)",
         "available_commands": ["enqueue_print"], "tags": ["home:fal"]},
        {"device_code": "PL1", "device_type": "surge_plate", "location": "FAL-1-L4", "status": "idle", "attach_capacity": 1,
         "features": ["surge", "attach", "taxi"], "taxi_mode": "taxi", "available_commands": ["attach", "detach", "travel"],
         "tags": ["home:fal", "taxi"]},
        {"device_code": "NEW1", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "idle",
         "available_commands": ["travel", "stow", "deploy"], "tags": ["to:ith"]},
        # ITH is commandable: its relay links to the one in FAL (1 ly away), where a replicant is — an active network
        {"device_code": "RLF", "device_type": "ftl_relay", "location": "FAL-1-L4", "status": "relaying", "tags": []},
        {"device_code": "RLI", "device_type": "ftl_relay", "location": "ITH-2-L4", "status": "relaying", "tags": []},
    ]
    client.portal.call(db.kv_set, "devices", devices)
    reps = client.portal.call(db.kv_get, "replicants") or {}
    client.portal.call(db.kv_set, "replicants", {**reps, "RX": {"name": "rx", "location": "FAL-1-L4"}})
    cat = {"stars": [{"designation": "FAL", "position": {"x": 0, "y": 0, "z": 0}, "entry_point": "FAL-1-L4"},
                     {"designation": "ITH", "position": {"x": 1, "y": 0, "z": 0}, "entry_point": "ITH-2-L4"}]}
    client.portal.call(db.kv_set, "stars", cat)
    client.portal.call(client.app.state.worker.handle_event,
                       _ev(950, "print.completed", device_code="AF1", device_type="mining_drone", new_device_code="NEW1",
                           tags=["to:ith"], location="FAL-BELT-1"))
    assert "NEW1" in client.portal.call(db.kv_get, "dispatch_pending", {})
    started = client.portal.call(eng.dispatch_new_prints)
    assert started == ["PL1 carries NEW1 to ITH"]
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "loadouts"][-1]
    assert job["title"].endswith("(just printed)") and job["device"] == "PL1"
    assert client.portal.call(db.kv_get, "dispatch_pending", {}) == {}


def test_split_prints_evenly_by_print_time():
    from rsweb import printqueue as pq
    bps = {"survey_drone": {"print_time": 240}, "mining_drone": {"print_time": 180}, "relay": {"print_time": 600}}
    f = lambda code, **kw: {"device_code": code, "device_type": "autofactory", "status": "idle", **kw}  # noqa: E731
    a, b, c = f("A"), f("B", status="printing (relay)", printing={"device_type": "relay"}), f("C")
    load = {x["device_code"]: pq.load_seconds(x, bps) for x in (a, b, c)}
    assert load == {"A": 0, "B": 600, "C": 0}
    parts = pq.split([a, b, c], "mining_drone", 5, bps, load)
    # A and C take the first four; B, 600 s behind, only gets the 5th once A and C would be past 540 s
    assert [(x["device_code"], k) for x, k in parts] == [("A", 2), ("C", 3)] or \
           [(x["device_code"], k) for x, k in parts] == [("A", 3), ("C", 2)]
    parts = pq.split([a, b, c], "survey_drone", 4, bps, load)        # carries on from the load so far
    assert sum(k for _, k in parts) == 4 and max(load.values()) - min(load.values()) <= 240
    room = {"A": 1, "B": 0, "C": 0}
    assert [(x["device_code"], k) for x, k in pq.split([a, b, c], "relay", 3, bps, {}, room)] == [("A", 1)]
    assert pq.least_loaded([b, c], bps)["device_code"] == "C"


def test_prints_spread_over_factories_across_passes():
    """Live 2026-10-06: three prints of one item all landed on one of three idle autofactories — one print a pass, and
    each pass every factory still looked idle."""
    from rsweb import loadouts as lo
    devices = [{"device_code": c, "device_type": "autofactory", "location": "FAL-BELT-1", "status": "idle", "print_queue": [],
                "available_commands": ["enqueue_print"], "tags": []} for c in ("AF1", "AF2", "AF3")]
    fleets = [{"id": "fal", "name": "Fal", "home": "FAL", "station": True, "wants": {"autofactory": 3, "maintenance_drone": 1}}]
    bps = [{"device_type": "maintenance_drone", "resources": {"structural": 50}, "print_time": 600}]
    inv = {"FAL-BELT-1": {"structural": 1000}}
    orders = []
    for _ in range(3):
        p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, bps, inv, {}, {}, set(), orders, {})
        (pr,) = p["prints"]
        orders.append({"star": "FAL", "fleet": "fal", "device_type": "maintenance_drone", "factory": pr["factory"]})
        fleets[0]["wants"]["maintenance_drone"] += 1          # one more wanted each pass, as when stock allows one at a time
    assert sorted(o["factory"] for o in orders) == ["AF1", "AF2", "AF3"]


def test_print_orders_count_prints_fed_to_a_neighbor_factory():
    """Live 2026-11-09: the virtual queue fed system hubs ordered on E76D3831 to its neighbors at ISOLITHOR-BELT-1; the
    orders were dropped as removed, so loadouts kept ordering another and 5 got queued for a fleet wanting one."""
    from rsweb import loadouts as lo
    from datetime import datetime, timezone
    now = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc).timestamp()
    O = lambda f: {"star": "ISO", "fleet": "hub", "device_type": "system_hub", "factory": f, "at": "2026-11-09T23:00:00+00:00"}  # noqa: E731
    F = lambda c, loc, q: {"device_code": c, "device_type": "autofactory", "location": loc, "status": "idle",  # noqa: E731
                           "print_queue": [{"device_type": "system_hub"}] * q}
    orders = [O("AF1"), O("AF1"), O("AF1")]
    devices = [F("AF1", "ISO-BELT-1", 0), F("AF2", "ISO-BELT-1", 1), F("AF3", "ISO-BELT-1", 1), F("AF4", "ISO-3-L4", 5)]
    assert len(lo.reconcile_orders(orders, devices, now)) == 2   # AF2 and AF3 hold two of them; AF4 is elsewhere


def test_print_orders_removed_from_a_queue_stop_counting_as_incoming(client):
    """Live 2026-10-07: prints taken off a queue by hand kept counting as incoming for 48 h, so the fleet looked complete
    and nothing could be queued to fill its loadout."""
    from rsweb import loadouts as lo
    from datetime import datetime, timezone
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc).timestamp()
    O = lambda t, f, at="2026-10-07T11:00:00+00:00", **kw: {"star": "FAL", "fleet": "hub", "device_type": t, "factory": f, "at": at, **kw}  # noqa: E731
    orders = [O("maintenance_drone", "AF1"), O("maintenance_drone", "AF1"), O("maintenance_drone", "AF1"),
              O("survey_drone", "AF2"), O("survey_drone", "AF2", at="2026-10-07T11:58:00+00:00"),   # fresh: kept
              O("mining_drone", "AF3", device_code="?", printed_at="2026-10-07T11:00:00+00:00"),  # printed, code never came
              O("mining_drone", "GONE")]                                                            # factory not listed: kept
    devices = [{"device_code": "AF1", "device_type": "autofactory", "status": "printing (maintenance_drone)", "print_queue": []},
               {"device_code": "AF2", "device_type": "autofactory", "status": "idle", "print_queue": []},
               {"device_code": "AF3", "device_type": "autofactory", "status": "idle", "print_queue": []}]
    kept = lo.reconcile_orders(orders, devices, now)
    assert [(o["device_type"], o["factory"]) for o in kept] == [("maintenance_drone", "AF1"), ("survey_drone", "AF2"),
                                                                ("mining_drone", "GONE")]
    devices[0]["print_queue"] = [{"device_type": "maintenance_drone", "quantity": 2}]
    assert len([o for o in lo.reconcile_orders(orders, devices, now) if o["factory"] == "AF1"]) == 3
    assert lo.forget_queued(orders, "AF1", "maintenance_drone", 1) == orders[:2] + orders[3:]
    assert [o["factory"] for o in lo.forget_queued(orders, "AF1")] == ["AF2", "AF2", "AF3", "GONE"]
    # the fleet card lists them, and Forget these drops only that fleet's
    db = client.app.state.db
    client.portal.call(db.kv_set, "loadout_orders", orders + [{**O("survey_drone", "AF2"), "fleet": "other"}])
    client.post("/loadouts/orders/clear", data={"fleet": "hub"}, headers=HX)
    assert [o["fleet"] for o in client.portal.call(db.kv_get, "loadout_orders")] == ["other"]


def test_removing_from_a_queue_forgets_its_order(client):
    world = client.app.state.api.http._transport.app.state.world
    world.queues["AF00BEEF"] = [{"device_type": "survey_drone"}, {"device_type": "ftl_beacon"}]
    world.devices[7]["status"] = "printing (mining_drone)"
    db = client.app.state.db
    O = lambda t: {"star": "FAL", "fleet": "hub", "device_type": t, "factory": "AF00BEEF", "at": "2026-10-07T11:00:00+00:00"}  # noqa: E731
    client.portal.call(db.kv_set, "loadout_orders", [O("survey_drone"), O("ftl_beacon"), O("ftl_beacon"),
                                                     {**O("ftl_beacon"), "factory": "OTHER"}])
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "remove", "index": "1"}, headers=HX)   # the ftl beacon
    assert "result ok" in r.text
    left = client.portal.call(db.kv_get, "loadout_orders")
    assert [(o["device_type"], o["factory"]) for o in left] == [("survey_drone", "AF00BEEF"), ("ftl_beacon", "AF00BEEF"),
                                                                ("ftl_beacon", "OTHER")]
    client.post("/devices/AF00BEEF/print-queue", data={"action": "clear"}, headers=HX)
    assert [o["factory"] for o in client.portal.call(db.kv_get, "loadout_orders")] == ["OTHER"]
    # the fleet card lists a stationed fleet's queued prints with a Forget button
    eng = client.app.state.worker.automations
    client.portal.call(eng.save_fleets, [{"id": "hub", "name": "Hub", "role": "mining", "home": "SOL", "station": True,
                                          "wants": {"ftl_beacon": 3}, "materials": "", "mission": None}])
    client.portal.call(db.kv_set, "loadout_orders", [{**O("ftl_beacon"), "at": "2099-01-01T00:00:00+00:00"}])
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Queued prints counted as incoming" in page and "1 queued print(s)" in page and "Forget these" in page


def test_edit_tags_of_a_print_in_progress(client):
    world = client.app.state.api.http._transport.app.state.world
    eng, db, w = client.app.state.worker.automations, client.app.state.db, client.app.state.worker
    client.portal.call(db.kv_set, "loadout_orders", [{"star": "SOL", "fleet": "sol-sector", "device_type": "mining_drone",
                                                       "factory": "AF00BEEF", "at": "2026-10-08T11:55:20+00:00"}])
    r = client.post("/devices/AF00BEEF/print-tags", data={"device_type": "mining_drone", "original": "to:sol, fleet:sol-sector",
                                                          "tags": "fleet:falquoryx-home, keepme"}, headers=HX)
    assert "applied when printed" not in r.text or "will be tagged fleet:falquoryx-home, keepme" in r.text
    assert client.portal.call(db.kv_get, "loadout_orders") == []              # no longer bound for SOL: not incoming there
    edits = client.portal.call(db.kv_get, "print_tag_edits")
    assert edits["AF00BEEF"][0]["to"] == ["fleet:falquoryx-home", "keepme"]
    new = world.devices[2]
    new["tags"] = ["to:sol", "fleet:sol-sector"]                              # as the game prints it
    client.portal.call(w.handle_event, {**_ev(990, "print.completed", device="AF00BEEF", device_type="mining_drone",
                                                new_device_code=new["device_code"], tags=["to:sol", "fleet:sol-sector"]),
                                        "device_type": "autofactory"})
    assert sorted(new["tags"]) == ["fleet:falquoryx-home", "keepme"]
    assert not client.portal.call(db.kv_get, "print_tag_edits")
    # re-aimed at another system instead: the order follows
    client.portal.call(db.kv_set, "loadout_orders", [{"star": "SOL", "fleet": "sol-sector", "device_type": "mining_drone",
                                                       "factory": "AF00BEEF"}])
    client.post("/devices/AF00BEEF/print-tags", data={"device_type": "mining_drone", "original": "to:sol, fleet:sol-sector",
                                                      "tags": "to:lerna, fleet:lerna-miners"}, headers=HX)
    o = client.portal.call(db.kv_get, "loadout_orders")[0]
    assert o["star"] == "LERNA" and o["fleet"] == "lerna-miners"
    world.queues["AF00BEEF"] = [{"device_type": "mining_drone", "tags": ["to:sol", "fleet:sol-sector"]}]   # something queued
    page = client.get("/devices/AF00BEEF/print-queue", headers=HX).text
    assert "/devices/AF00BEEF/print-tags" in page and "applied when printed" in page


def test_enqueue_print_on_a_replicant_vessel_uses_the_replicant_print(client):
    db = client.app.state.db
    reps = client.portal.call(db.kv_get, "replicants")
    code, rep = next(iter(reps.items()))
    host = rep.get("hosted_device_code")
    assert host
    r = client.post(f"/devices/{host}/command", data={"command": "enqueue_print", "f.device_type": "mining_drone",
                                                      "f.quantity": "2"}, headers=HX)
    assert "one device at a time" in r.text
    sent = client.portal.call(db.fetchall, "SELECT path, body FROM actions ORDER BY rowid DESC LIMIT 1")[0]
    assert sent["path"] == f"/replicants/{code}/print" and '"device_type": "mining_drone"' in sent["body"]


def test_large_devices_can_be_printed_compacted(client):
    import json as _j
    db = client.app.state.db
    bps = client.portal.call(db.kv_get, "blueprints") or []
    if not any(b.get("device_type") == "galactic_observatory" for b in bps):
        bps = bps + [{"device_type": "galactic_observatory", "features": ["modular", "prospect"], "resources": {"structural": 10},
                      "print_time": 60}]
        client.portal.call(db.kv_set, "blueprints", bps)
    page = client.get("/blueprints", headers=H).text
    assert 'value="galactic_observatory" data-large=1' in page and 'name="flatpack"' in page
    client.post("/blueprints/print", data={"printer": "device:AF00BEEF", "device_type": "galactic_observatory", "quantity": "1",
                                           "flatpack": "on"}, headers=HX)
    client.post("/blueprints/print", data={"printer": "device:AF00BEEF", "device_type": "mining_drone", "quantity": "1",
                                           "flatpack": "on"}, headers=HX)
    rows = client.portal.call(db.fetchall, "SELECT body FROM actions WHERE path='/devices/AF00BEEF' ORDER BY rowid DESC LIMIT 2")
    bodies = [_j.loads(r["body"]) for r in rows]
    assert {b["device_type"]: b.get("flatpack") for b in bodies} == {"galactic_observatory": True, "mining_drone": None}
    # the planner: large lines go compacted when ticked
    data = {"printer": "device:AF00BEEF", "qty:galactic_observatory": "1", "qty:mining_drone": "1", "_planned": "1",
            "flatpack": "on", "nosplit": "on"}
    assert "print galactic observatory compacted" in client.post("/blueprints/plan", data=data, headers=HX).text
    client.post("/blueprints/queue-plan", data=data, headers=HX)
    job = client.portal.call(client.app.state.worker.automations.jobs)[-1]
    flat = {s["body"]["device_type"]: s["body"].get("flatpack") for s in job["steps"] if s["body"].get("command") == "enqueue_print"}
    assert flat == {"galactic_observatory": True, "mining_drone": None}


def test_print_components_used_and_returned(client):
    from rsweb import lost
    db, w = client.app.state.db, client.app.state.worker
    now = datetime.now(timezone.utc)
    client.portal.call(w.handle_event, _ev(2001, "print.started", device="FAC00001", created=now - timedelta(minutes=5),
                                           device_type="ami_mining_controller", component_device_codes=["USED0001", "BACK0001"]))
    client.portal.call(w.handle_event, _ev(2002, "print.cancelled", device="FAC00001", created=now - timedelta(minutes=2),
                                           device_type="ami_mining_controller", returned_device_codes=["BACK0001"]))
    got = client.portal.call(lost.given_away, db, {"USED0001": ("SOL-BELT-1", "mining_drone"),
                                                   "BACK0001": ("SOL-BELT-1", "mining_drone")})
    assert got == {"USED0001"}
    why = client.portal.call(lost.reasons, db, ["USED0001", "BACK0001"])
    assert why == {"USED0001": "used in a print"}
    body = client.get("/partials/tutorials", headers=HX).text
    assert "On graduation:" in body and "graduate: 100 XP" in body


def test_print_queue_reorder_and_keep_in_stock(client):
    """The game can't reorder a queue: moving a print takes the waiting prints off from its new place down and queues
    them again. A printing hub keeps its stock: one print at a time on an idle factory, tagged spare + stock, and a
    waiting stock print goes behind any other print."""
    from rsweb import printqueue as pq, stock
    db, eng = client.app.state.db, client.app.state.worker.automations
    w = client.app.state.api.http._transport.app.state.world
    af = next(d for d in w.devices if d["device_code"] == "AF00BEEF")
    af["status"] = "printing (mining_drone)"
    w.queues["AF00BEEF"] = [{"device_type": t, "notify": {"device": None}, "tags": tags}
                            for t, tags in (("ftl_relay", ["spare", "stock"]), ("mining_drone", ["fleet:x"]),
                                            ("cargo_freighter", []))]
    q = pq.items({"print_queue": w.queues["AF00BEEF"]})
    t = client.post("/devices/AF00BEEF/print-queue", data={"action": "top", "index": 2}, headers=HX).text
    assert [x["device_type"] for x in w.queues["AF00BEEF"]] == ["cargo_freighter", "ftl_relay", "mining_drone"]
    assert w.queues["AF00BEEF"][2]["tags"] == ["fleet:x"] and "⤒" in t
    assert stock.stock_last(q)[-1]["device_type"] == "ftl_relay"
    # keep in stock: SOL's receiving fleet wants one relay; the factory there is idle and nothing is in stock
    af["status"] = "idle"
    w.queues["AF00BEEF"] = []
    devs = client.portal.call(db.kv_get, "devices")
    for d in devs:
        if d["device_code"] == "AF00BEEF":
            d.update(status="idle", print_queue=[], tags=[])
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(eng.save_fleets, [{"id": "hub", "name": "Hub", "role": "mining", "home": "SOL", "station": True,
                                          "materials": "self", "stock": {"ftl_relay": 1}}])
    out = client.portal.call(eng.stock_pass)
    assert any("ftl_relay to keep in stock" in x for x in out)
    assert client.portal.call(eng.stock_pass) == []            # ordered: not again while the device list catches up
    hub = client.portal.call(eng.fleets)[0]
    devs[-1:] = [devs[-1], {"device_code": "R1", "device_type": "ftl_relay", "location": "SOL-3-L4", "tags": ["spare"]}]
    assert stock.shortfall(hub, devs) == {}                     # a spare in the hub's system counts
    # set in the loadout editor (a Stock column beside Want) …
    client.post("/fleets/hub/edit", data={"name": "Hub", "lines": "1", "autosave": "1", "type": ["mining_drone", "ftl_relay"],
                                          "qty": ["2", ""], "stock": ["", "3"]}, headers=HX)
    hub = client.portal.call(eng.fleets)[0]
    assert hub["wants"] == {"mining_drone": 2} and hub["stock"] == {"ftl_relay": 3}
    assert 'name="stock" value="3"' in client.get("/fleets/details?all=1", headers=H).text
    # … or in a template, which its fleets follow
    client.post("/loadouts/phases", data={"new_phase": "Depot"}, headers=HX)
    pid = next(p["id"] for p in client.portal.call(eng.loadout_cfg)["phases"] if p["name"] == "Depot")
    client.post("/loadouts/phases", data={f"want:{pid}:mining_drone": "1", f"stock:{pid}:system_ward": "2"}, headers=HX)
    client.post("/fleets/hub/edit", data={"name": "Hub", "template": pid}, headers=HX)
    hub = client.portal.call(eng.fleets)[0]
    assert hub["stock"] == {"system_ward": 2} and stock.shortfall(hub, devs) == {"system_ward": 2}


def test_queueing_a_simulation_checks_its_offer_and_preps_at_once(client):
    """Queuing asks the interface right away whether it offers the scenario and at what cost, then runs the prep pass
    (the free training sends a replicant at once) instead of waiting for the next tick."""
    import time
    eng = client.app.state.worker.automations
    t = client.post("/simulations/queue", data={"scenario": "nope"}, headers=HX).text
    assert "Not queued" in t and "offers mining_rush, terraforming_training_1" in t
    assert not client.portal.call(eng.sim_state).get("prep")
    t = client.post("/simulations/queue", data={"scenario": "terraforming_training_1"}, headers=HX).text
    assert "no entry cost" in t
    for _ in range(50):
        prep = client.portal.call(eng.sim_state)["prep"]
        if prep.get("rep_sent"):
            break
        time.sleep(0.05)
    assert prep["cores_needed"] == 0 and prep["rep_sent"]
    assert "Entry cost: <b>free</b>" in client.get("/simulations", headers=H).text


def test_blueprints_show_how_a_device_travels_and_what_it_carries(client):
    from rsweb import fleets as fl
    assert fl.carry_info({"device_type": "mining_drone", "features": ["cruise", "mine", "stow"]})["ways"] == ["stowed", "attached"]
    assert fl.carry_info({"device_type": "transport_hauler", "features": ["cruise", "transport"]})["ways"] == ["attached"]
    assert fl.carry_info({"device_type": "cargo_freighter", "features": ["surge", "cruise", "transport"],
                          "cargo_capacity": 500})["ways"] == ["flies itself"]
    assert fl.carry_info({"device_type": "autofactory", "features": ["print", "modular"]})["ways"] == ["compacted, then attached"]
    cv = fl.carry_info({"device_type": "cargo_vessel", "features": ["surge", "cruise", "attach"], "stow_capacity": 50,
                        "attach_capacity": 3, "cargo_capacity": 200})
    assert cv == {"ways": ["flies itself", "attached"], "hold": 50, "attach": 3, "cargo": 200}
    page = client.get("/blueprints", headers=H).text
    assert "travels: flies itself or attached · hold 50 devices · attaches 3 devices · cargo 200 materials" in page


def test_blueprint_specs_show_set_fields_only():
    from rsweb.web import blueprint_specs
    specs = blueprint_specs({"device_type": "system_hub", "relay_range": 15.0, "current_hubs": 2, "queue_size": 0,
                             "strength": 0, "stow_capacity": 1, "components": {}, "new_field": "x"})
    assert [(l, v) for l, v, _ in specs] == [("relay range", "15 ly"), ("current hubs", "2"), ("new field", "x")]


def test_blueprint_unlocked_source_v361(client):
    from rsweb import mock as mockmod
    from rsweb.notify import describe
    ev = {"event": "blueprint.unlocked", "payload": {"device_type": "hydroponic_bay", "source": "decommission"}}
    assert describe(ev) == "Blueprint unlocked: hydroponic bay (by decommissioning)"
    w = client.app.state.worker
    client.portal.call(w.sync_blueprints)   # a first sync takes everything as known
    client.portal.call(w.db.kv_set, "blueprint_sources", {"hydroponic_bay": "decommission"})
    extra = {"device_type": "hydroponic_bay", "short_description": "Bay", "print_time": 60, "resources": {"carbon": 5}}
    mockmod.BLUEPRINTS.append(extra)
    try:
        assert client.portal.call(w.sync_blueprints) == ["hydroponic_bay"]
    finally:
        mockmod.BLUEPRINTS.remove(extra)
    assert "New blueprint unlocked: hydroponic bay (by decommissioning)" in client.get("/notifications", headers=H).text


def test_a_waiting_factory_says_what_it_waits_for(client):
    """Joe 2026-11-03: the panel names the game's waiting_for (live: {"components": {"ftl_relay": {"have": 0, "need": 1}}})."""
    world = client.app.state.api.http._transport.app.state.world
    af = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    world.queues["AF00BEEF"] = [{"device_type": "deep_space_relay_station", "notify": {"device": None}, "tags": []}]
    af.update(status="waiting_for_resources", waiting_for={"components": {"ftl_relay": {"have": 0, "need": 1}}})
    panel = client.get("/devices/AF00BEEF/print-queue", headers=HX).text
    assert "waiting for components" in panel and "needs ftl relay 0 of 1" in panel.lower()
    assert "deep space relay station" in panel.lower()


def test_automations_never_print_on_vessels_but_your_jobs_may(client):
    """Owner 2026-11-07: automations don't print on vessels; a job you start yourself still does."""
    from rsweb import automations as auto
    from rsweb.mock import HOST
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.worker.sync_devices)
    body = {"command": "enqueue_print", "device_type": "mining_drone", "quantity": 1}
    ok, _, err = client.portal.call(eng.send, "POST", f"/devices/{HOST}", body, "test")
    assert not ok and "don't print on vessels" in err
    ok, _, err = client.portal.call(eng.send, "POST", "/replicants/R1/print", {"device_type": "mining_drone"}, "test")
    assert not ok and "don't print on vessels" in err
    assert client.portal.call(eng.vessel_print, "POST", "/devices/AF00BEEF", body) is False
    job = client.portal.call(eng.create_job, "stock", "t", HOST, [auto.step("p", f"/devices/{HOST}", body)])
    job = next(j for j in client.portal.call(eng.jobs) if j["id"] == job["id"])
    assert "don't print on vessels" in str(job["steps"][0])
    job = client.portal.call(eng.create_job, "chain", "t", HOST, [auto.step("p", f"/devices/{HOST}", body)], {}, True)
    job = next(j for j in client.portal.call(eng.jobs) if j["id"] == job["id"])
    assert "don't print on vessels" not in str(job["steps"][0])


def test_awaited_component_is_collected_before_logistics(client):
    """Joe 2026-11-13: collections for a print stalled on a component go ahead of the logistics pass."""
    eng = client.app.state.worker.automations
    db = client.app.state.db

    async def enable():
        s = await eng.settings()
        s["rules"]["loadouts"]["enabled"] = True
        await eng.save_settings(s)
    client.portal.call(enable)
    devices = [
        {"device_code": "AF1", "device_type": "autofactory", "location": "ITH-BELT-1", "status": "waiting_for_resources",
         "available_commands": ["enqueue_print"], "tags": ["home:ith"],
         "waiting_for": {"components": {"mining_drone": {"have": 0, "need": 1}}}},
        {"device_code": "PL1", "device_type": "surge_plate", "location": "FAL-1-L4", "status": "idle", "attach_capacity": 1,
         "features": ["surge", "attach", "taxi"], "taxi_mode": "taxi", "available_commands": ["attach", "detach", "travel"],
         "tags": ["home:fal", "taxi"]},
        {"device_code": "C1", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "idle",
         "available_commands": ["travel", "stow", "deploy"], "tags": ["component", "to:ith", "at:ith-belt-1"]},
        {"device_code": "RLF", "device_type": "ftl_relay", "location": "FAL-1-L4", "status": "relaying", "tags": []},
        {"device_code": "RLI", "device_type": "ftl_relay", "location": "ITH-2-L4", "status": "relaying", "tags": []},
    ]
    client.portal.call(db.kv_set, "devices", devices)
    reps = client.portal.call(db.kv_get, "replicants") or {}
    client.portal.call(db.kv_set, "replicants", {**reps, "RX": {"name": "rx", "location": "FAL-1-L4"}})
    cat = {"stars": [{"designation": "FAL", "position": {"x": 0, "y": 0, "z": 0}, "entry_point": "FAL-1-L4"},
                     {"designation": "ITH", "position": {"x": 1, "y": 0, "z": 0}, "entry_point": "ITH-2-L4"}]}
    client.portal.call(db.kv_set, "stars", cat)
    started = client.portal.call(eng.priority_deliveries_pass)
    assert started and "C1" in started[0], started
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "loadouts"][-1]
    assert job["title"].endswith("(priority)") and "C1" in job["meta"]["devices"]
    assert client.portal.call(eng.priority_deliveries_pass) == []   # throttled, and C1 is on its way


def _mixed_setup(client, extra=()):
    eng = client.app.state.worker.automations
    db = client.app.state.db

    async def enable():
        s = await eng.settings()
        s["rules"]["loadouts"]["enabled"] = True
        await eng.save_settings(s)
    client.portal.call(enable)
    devices = [
        {"device_code": "MF1", "device_type": "mobile_fleet", "location": "FAL-1-L4", "status": "idle", "attach_capacity": 36,
         "features": ["surge", "attach"], "available_commands": ["attach", "detach", "travel"], "tags": []},
        {"device_code": "S1", "device_type": "ftl_beacon", "location": "FAL-1-L4", "status": "monitoring",
         "available_commands": ["stow"], "tags": ["to:ith"]},
        {"device_code": "T1", "device_type": "gas_separator", "location": "FAL-1-L4", "status": "idle",
         "available_commands": ["travel", "activate"], "tags": ["to:ith"]},
        {"device_code": "H1", "device_type": "hull_plate", "location": "FAL-1-L4", "status": "idle",
         "available_commands": [], "tags": ["to:ith"]},
        {"device_code": "RLF", "device_type": "ftl_relay", "location": "FAL-1-L4", "status": "relaying", "tags": []},
        {"device_code": "RLI", "device_type": "ftl_relay", "location": "ITH-2-L4", "status": "relaying", "tags": []},
        *extra,
    ]
    client.portal.call(db.kv_set, "devices", devices)
    reps = client.portal.call(db.kv_get, "replicants") or {}
    client.portal.call(db.kv_set, "replicants", {**reps, "RX": {"name": "rx", "location": "FAL-1-L4"}})
    cat = {"stars": [{"designation": "FAL", "position": {"x": 0, "y": 0, "z": 0}, "entry_point": "FAL-1-L4"},
                     {"designation": "ITH", "position": {"x": 1, "y": 0, "z": 0}, "entry_point": "ITH-2-L4"}]}
    client.portal.call(db.kv_set, "stars", cat)
    return eng, db


def test_mixed_batch_boards_what_it_can(client):
    """Live 2026-11-13: SOL_Trader's devices (stow-only, fly-only and a hull plate that does neither) never fit a carrier
    whole, so none of them moved."""
    eng, _ = _mixed_setup(client)
    p = client.portal.call(eng.loadout_plan)
    dl = [d for d in p["deliveries"] if d["carrier"] == "MF1"]
    assert dl and set(dl[0]["devices"]) == {"S1", "T1"}, p["deliveries"]
    assert any("neither stows nor flies" in u["why"] and u["n"] == 1 for u in p["unmet"]), p["unmet"]


def test_freed_carrier_goes_to_waiting_devices_before_logistics(client):
    """Joe 2026-11-13: a carrier that comes free takes waiting device moves before the logistics pass grabs it."""
    eng, db = _mixed_setup(client)
    client.portal.call(db.kv_set, "loadouts_last", {"at": "2026-11-13T16:50:00+00:00", "lines": [
        "can't fill 2× ftl_beacon in ITH: waiting for a surge-capable carrier in FAL (or a free one within 1728 ly) to go to ITH"]})
    started = client.portal.call(eng.priority_deliveries_pass)
    assert started and "MF1" in started[0], started
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "loadouts"][-1]
    assert job["title"].endswith("(carrier came free)") and {"S1", "T1"} <= set(job["meta"]["devices"])
    assert client.portal.call(eng.priority_deliveries_pass) == []   # MF1 is busy now, nothing new came free
