"""Core tests: loadouts, carriers, stowing and spares (split out of test_core.py by test name)."""
from corelib import *  # noqa: F401,F403


def test_carrier_plan_orders_steps():
    from rsweb.carrier import plan
    rows = [
        {"code": "A", "carried": True, "can_launch": True, "can_stow": False, "can_recall": False, "same_loc": True, "mobile": True, "location": "X-1"},
        {"code": "B", "carried": False, "can_launch": False, "can_stow": True, "can_recall": True, "same_loc": True, "mobile": True, "location": "X-1"},
        {"code": "C", "carried": False, "can_launch": False, "can_stow": True, "can_recall": True, "same_loc": False, "mobile": True, "location": "X-2"},
        {"code": "D", "carried": False, "can_launch": False, "can_stow": True, "can_recall": False, "same_loc": False, "mobile": False, "location": "X-3"},
        {"code": "E", "carried": False, "can_launch": False, "can_stow": False, "can_recall": True, "same_loc": False, "mobile": True, "location": "X-4"},
    ]
    steps = plan("V", "X-1", rows, {"A"}, {"B", "C", "D"}, {"C", "E"}, "/devices/V", return_after=True)
    descs = [s["desc"] for s in steps]
    assert descs == ["launch A", "recall E", "stow B in V", "C → X-1 (to be picked up)", "wait for C to reach X-1",
                     "stow C in V", "vessel → X-3 (pick-up)", "stow D in V", "vessel → X-1 (return)"]
    assert steps[4]["method"] == "WAIT" and steps[4]["seq0_from"] == 3


def test_carrier_card_and_pickup_with_vessel_move(client):
    world = client.app.state.api.http._transport.app.state.world
    world.move_seconds = 0.03
    db = client.app.state.db   # D8C2A140 starts out scanning: a scanning drone would wait for its scan first
    next(d for d in world.devices if d["device_code"] == "D8C2A140")["status"] = "idle"
    cached = client.portal.call(db.kv_get, "devices")
    next(d for d in cached if d["device_code"] == "D8C2A140")["status"] = "idle"
    client.portal.call(db.kv_set, "devices", cached)
    client._seen = len(world.events)
    page = client.get("/devices/11ADA230", headers=H).text
    assert "Carrier · devices in SOL" in page
    assert 'name="launch" value="SV000001"' in page            # carried → launch
    assert 'name="stow" value="2AC61213"' in page              # mining drone here → stow
    assert 'name="stow" value="SP000001"' in page and "vessel will go and pick it up" in page  # immobile, elsewhere
    assert 'name="stow" value="D8C2A140"' in page and "will fly here" in page                 # mobile, elsewhere
    r = client.post("/devices/11ADA230/carrier", data={"stow": ["2AC61213", "D8C2A140", "SP000001"],
                                                       "launch": ["SV000001"], "return_after": "on"}, headers=HX)
    assert "follow on Automations" in r.text, r.text
    eng = client.app.state.worker.automations

    async def finished():
        return all(j["status"] not in ("running", "waiting") for j in await eng.jobs())
    _pump(client, finished)
    job = client.portal.call(eng.jobs)[-1]
    assert job["status"] == "done", [(s["desc"], s["status"], s["error"]) for s in job["steps"]]
    assert all(s["status"] == "done" for s in job["steps"]), [(s["desc"], s["status"], s["error"]) for s in job["steps"]]
    stowed = {d["device_code"] for d in world.devices if d["status"] == "stowed"}
    assert {"2AC61213", "D8C2A140", "SP000001"} <= stowed and "SV000001" not in stowed
    host = next(d for d in world.devices if d["device_code"] == "11ADA230")
    assert host["location"] == "SOL-BELT-1"  # went to SOL-3 for the surge plate and came back


def test_carrier_rejects_over_capacity(client):
    world = client.app.state.api.http._transport.app.state.world
    next(d for d in world.devices if d["device_code"] == "11ADA230")["stow_capacity"] = 2
    r = client.post("/devices/11ADA230/carrier", data={"stow": ["2AC61213", "D8C2A140"]}, headers=HX)
    assert "holds 2" in r.text


def test_tree_page_renders_with_stowed_and_commands(client):
    client.portal.call(client.app.state.worker.sync_devices)  # also builds the stowed map
    page = client.get("/tree", headers=H).text
    assert "Expand all" in page and "Collapse all" in page and "Systems only" in page
    assert 'id="ts-SOL"' in page and 'id="t-11ADA230"' in page
    # devices sit under a type group, and nothing starts open
    grp = page[page.index('id="tg-SOL-mining_drone"'):]
    assert "mining drone" in grp[:400] and 'id="t-' in grp[:6000]
    assert "<details open" not in page and " open>" not in page
    # the vessel's stowed devices are nested inside its node
    vessel = page[page.index('id="t-11ADA230"'):]
    assert "Stowed in 11ADA230" in vessel and 'id="t-SV000001"' in vessel.split('id="t-2AC61210"')[0]
    # commands panel with the device's own commands, results go to its own box
    assert 'hx-get="/devices/2AC61212/command-form"' in page and 'id="tres-2AC61212"' in page
    r = client.get("/devices/TR000001/command-form?command=collect_resources&rid=tres-TR000001", headers=HX)
    assert 'hx-target="#tres-TR000001"' in r.text


def test_loadout_plan_spares_prints_and_carriers():
    from rsweb import loadouts as lo
    cfg, devices, bps, inv, stars = _lo_world()
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    a, b = _rep(p, "AAA"), _rep(p, "BBB")
    assert {r["type"]: (r["have"], r["short"], r["surplus"]) for r in a["rows"]} == {
        "mining_drone": (4, 0, 2), "ami_mining_controller": (1, 0, 0), "survey_drone": (0, 2, 0)}
    # the two idle drones are the extras, and they go to BBB (which has none)
    assert sorted(p["moves"][c] for c in ("A1", "A3")) == ["BBB", "BBB"]
    assert p["moves"]["CC"] == "BBB"           # spare controller from the unphased system
    assert "CK" not in p["moves"]              # ignored tag: never moved
    dl = p["deliveries"]
    assert len(dl) == 1 and dl[0]["carrier"] == "CAR" and sorted(dl[0]["devices"]) == ["A1", "A3"]
    assert any("carrier in CCC" in u["why"] for u in p["unmet"])   # nothing can carry CC yet
    # survey drones: 2 short in AAA and in BBB, stock covers 2 prints at AF in total
    assert sum(pr["n"] for pr in p["prints"]) == 2 and all(pr["factory"] == "AF" for pr in p["prints"])
    assert any(u["type"] == "survey_drone" for u in p["unmet"])
    steps = lo.delivery_steps(dl[0], p["by_code"], stars, True, assign=p["assign"])
    bodies = [(s["path"], s["body"]) for s in steps]
    # they join BBB's stationed fleet as they leave (so they count as its incoming), and keep the tag on arrival
    assert ("/devices/A1", {"configuration": {"add_tags": ["to:bbb", "fleet:bbb-home"]}}) in bodies
    assert ("/devices/CAR", {"command": "attach", "device": "A1"}) in bodies   # the carrier attaches the cargo
    assert ("/devices/CAR", {"command": "travel", "destination": "BBB-5-L4"}) in bodies
    assert ("/devices/CAR", {"command": "detach", "device": "A3"}) in bodies
    board = next(st for st in steps if st["body"] == {"command": "attach", "device": "A1"})
    assert board["critical"]          # no boarding → the carrier doesn't fly off without it
    assert ("/devices/A3", {"configuration": {"add_tags": ["fleet:bbb-home"], "remove_tags": ["to:bbb"]}}) in bodies
    assert p["assign"]["A2"] == "fleet:aaa-home" and "fleet:aaa-home" in p["tag_add"]["A2"]   # fleetless at home: joins
    assert bodies[-1] == ("/devices/CAR", {"command": "travel", "destination": "AAA-OORT"})
    pr = lo.print_steps(p["prints"][0])[0]["body"]
    assert pr["command"] == "enqueue_print" and pr["tags"][0].startswith("to:") and pr["tags"][1].startswith("fleet:")
    assert "HV" not in p["by_code"]            # the replicant's vessel is never counted or used


def test_loadout_incoming_and_arrivals_and_unspare():
    from rsweb import loadouts as lo
    cfg, devices, bps, inv, stars = _lo_world()
    # A1 and A3 already on their way; a survey drone was ordered for BBB; BS arrived tagged to:bbb
    for d in devices:
        if d["device_code"] in ("A1", "A3"):
            d["tags"] = ["to:bbb"]
        if d["device_code"] == "BS":
            d["tags"] = ["to:bbb", "spare"]
    orders = [{"star": "BBB", "device_type": "survey_drone", "factory": "AF", "at": "2026-01-01T00:00:00+00:00"}]
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), orders, {})
    b = {r["type"]: r for r in _rep(p, "BBB")["rows"]}
    assert b["mining_drone"]["incoming"] == 2 and b["mining_drone"]["short"] == 0
    assert b["survey_drone"]["incoming"] == 1 and b["survey_drone"]["short"] == 0
    assert "BS" in p["arrived"]
    steps = lo.arrived_steps("BS", p["by_code"]["BS"], {}, join=p["assign"]["BS"])
    assert steps[-1]["body"] == {"configuration": {"add_tags": ["fleet:bbb-home"], "remove_tags": ["spare", "to:bbb"]}}
    assert "BS" not in {st["path"][9:] for st in lo.tag_steps(p)}   # the arrival step sets its tags
    # AAA now has exactly 2 drones locally (A2, A4) — nothing more is marked spare there
    a = {r["type"]: r for r in _rep(p, "AAA")["rows"]}
    assert a["mining_drone"]["have"] == 2 and a["mining_drone"]["surplus"] == 0
    # a spare in a system that becomes short loses the tag
    for d in devices:
        if d["device_code"] == "A2":
            d["tags"] = ["spare"]
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), orders, {})
    assert p["tag_remove"].get("A2") == ["spare"]


def test_loadouts_page_and_apply_against_mock(client):
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    home = client.get("/", headers=H).text
    assert 'href="/fleets"' in home and 'href="/loadouts"' not in home   # nav: one Fleets tab
    assert client.get("/loadouts", headers=H, follow_redirects=False).headers["location"] == "/fleets"
    r = client.post("/loadouts/phases", data={"new_phase": "Mining hub"}, headers=HX)
    assert r.headers.get("HX-Refresh")
    cfg = client.portal.call(client.app.state.db.kv_get, "loadouts")
    pid = cfg["phases"][0]["id"]
    client.post("/loadouts/phases", data={f"name:{pid}": "Mining hub", f"order:{pid}": "1",
                                          f"want:{pid}:mining_drone": "2", f"want:{pid}:survey_drone": ""}, headers=HX)
    client.post("/fleets", data={"name": "Sol home", "role": "mining", "home": "sol", "template": pid, "station": "on"}, headers=HX)
    client.post("/loadouts/settings", data={"ignore_tags": "keep, Reserve", "print_missing": "on", "need_stock": "on"}, headers=HX)
    cfg = client.portal.call(client.app.state.db.kv_get, "loadouts")
    assert cfg["phases"][0]["wants"] == {"mining_drone": 2} and cfg["ignore_tags"] == ["keep", "reserve"]
    assert "fleets" not in cfg   # the fleets live in their own store
    fleets = client.portal.call(client.app.state.db.kv_get, "fleets")
    assert fleets[0]["home"] == "SOL" and fleets[0]["station"] and fleets[0]["template"] == pid
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Mining hub" in page and "2 spare" in page and "as spare" in page and "Tags &amp; controllers check" in page
    r = client.post("/loadouts/apply", data={"fleet": "sol-home"}, headers=HX)
    assert "Applied to sol-home" in r.text
    spares = [d["device_code"] for d in world.devices if "spare" in (d.get("tags") or [])]
    assert len(spares) == 2 and all(c.startswith("2AC6121") for c in spares)
    kept = [d for d in world.devices if d["device_type"] == "mining_drone" and "spare" not in (d.get("tags") or [])]
    assert len(kept) == 2 and all("fleet:sol-home" in d["tags"] for d in kept)   # the rest join the stationed fleet
    # survey drones aren't in the template: untouched (no fleet, not spare)
    assert not any(set(d.get("tags") or []) & {"spare", "fleet:sol-home"} for d in world.devices if d["device_type"] == "survey_drone")
    assert "◆ Sol home" in client.get("/tree", headers=H).text



def test_loadout_home_tags_keep_devices_counted_while_away():
    from rsweb import loadouts as lo
    cfg, devices, bps, inv, stars = _lo_world()
    cfg["phases"][0]["wants"]["surge_carrier"] = 1
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    # first pass: everything counted for a stationed fleet joins it (fleet tag)
    assert "fleet:aaa-home" in p["tag_add"]["CAR"] and "fleet:aaa-home" in p["tag_add"]["AC"]
    assert "fleet:bbb-home" in p["tag_add"]["BS"]
    assert any("fleet:aaa-home" in line for line in lo.describe(p))
    # tag them, then send the carrier off to BBB on a delivery
    for d in devices:
        if d["device_code"] in p["tag_add"] and d["device_code"] not in p["moves"]:
            d["tags"] = sorted(set(d.get("tags") or []) | {t for t in p["tag_add"][d["device_code"]] if t.startswith("fleet:")})
        if d["device_code"] == "CAR":
            d["location"], d["status"] = "BBB-5-L4", "idle"
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    a = {r["type"]: r for r in _rep(p, "AAA")["rows"]}
    assert a["surge_carrier"]["have"] == 1 and a["surge_carrier"]["short"] == 0 and a["surge_carrier"]["away"] == ["CAR"]
    b = {r["type"]: r for r in _rep(p, "BBB")["rows"]}
    assert "surge_carrier" not in b or b["surge_carrier"]["have"] == 0   # not counted (or made spare) where it's visiting
    assert "spare" not in p["tag_add"].get("CAR", [])


def test_spare_devices_drop_their_home():
    from rsweb import loadouts as lo
    cfg, devices, bps, inv, stars = _lo_world()
    cfg["systems"] = {"AAA": "outpost"}            # nobody is short, so AAA's extras just become spare
    for d in devices:
        if d["device_code"] in ("A1", "A2", "A3", "A4"):
            d["tags"] = ["home:aaa"]
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    extras = [c for c, t in p["tag_add"].items() if "spare" in t]
    assert sorted(extras) == ["A1", "A3"]
    assert all("home:aaa" in p["tag_remove"][c] for c in extras)
    assert "home:aaa" not in p["tag_add"].get("A1", [])
    # next pass: the spares (no home) aren't counted for AAA, aren't re-homed, and aren't made spare again
    for d in devices:
        if d["device_code"] in extras:
            d["tags"] = ["spare"]
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    row = next(r for r in _rep(p, "AAA")["rows"] if r["type"] == "mining_drone")
    assert row["have"] == 2 and row["surplus"] == 0 and sorted(row["spares_here"]) == ["A1", "A3"]
    assert not any(c in p["tag_add"] for c in extras)


def test_belt_surveyors_are_their_own_loadout_item():
    from rsweb import loadouts as lo
    devices = [{"device_code": "BS1", "device_type": "belt_surveyor", "location": "FAL-BELT-1", "status": "tracking",
                "tags": ["fleet:m"]},
               {"device_code": "SD1", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "idle", "tags": ["fleet:m"]},
               {"device_code": "SD2", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "idle", "tags": ["fleet:m"]}]
    cfg = {"fleets": [{"id": "m", "name": "M", "role": "mining", "home": "FAL", "station": True,
                       "wants": {"survey_drone": 2, "belt_surveyor": 1}}]}
    p = lo.plan(cfg, devices, [], {}, {}, {}, set(), [], {})
    assert not any("spare" in p["tag_add"].get(c, []) for c in ("SD1", "SD2", "BS1"))


def test_log_fixes_ferry_sticky_and_moving_carrier():
    from rsweb import loadouts as lo
    stars = {"AEM": {"position": {"x": 0, "y": 0, "z": 0}}, "FAL": {"position": {"x": 1, "y": 0, "z": 0}}}
    ctrl = {"device_code": "0E158313", "device_type": "ami_transport_controller", "location": "AEM-BELT-1", "status": "coordinating",
            "available_commands": ["set_directive"], "tags": []}
    devices = [ctrl, {"device_code": "D1", "device_type": "transport_drone", "location": "AEM-6-7", "controller_device_code": "0E158313"}]
    cfg = {"roles": {"AEM": "source", "FAL": "destination"}}
    cur = {"0E158313": {"directive": "ferry", "configuration": {"collect": "AEM-6-7", "deliver": "FAL-BELT-1"}, "finished": False}}
    # the biggest stockpile is now elsewhere in AEM, but the running ferry's pick-up still has stock: leave it alone
    inv = {"AEM-6-7": {"structural": 50}, "AEM-BELT-1": {"structural": 900}}
    assert lo.material_routes(cfg, devices, inv, stars, set(), cur, {"D1": "0E158313"})[0] == []
    # its pick-up ran dry → re-pointed at the stock that's left
    inv = {"AEM-6-7": {}, "AEM-BELT-1": {"structural": 900}}
    r = lo.material_routes(cfg, devices, inv, stars, set(), cur, {"D1": "0E158313"})[0][0]
    assert r["collect"] == "AEM-BELT-1" and r["resend"]
    # a surge plate that is itself bound for another system is not used as a carrier in the same pass
    plate = lambda c, **kw: {"device_code": c, "device_type": "surge_plate", "location": "FAL-1-L4", "status": "idle",  # noqa: E731
                             "attach_capacity": 1, "features": ["surge", "attach"], "available_commands": ["attach", "travel"], **kw}
    devs = [plate("2B30CEB4", tags=["to:ith"]), plate("FREE", tags=["home:fal"]),
            {"device_code": "EDE70565", "device_type": "transport_drone", "location": "FAL-1-L4", "status": "idle",
             "available_commands": ["travel"], "tags": ["to:aem"]}]
    p = lo.plan({"phases": [], "systems": {}}, devs, [], {}, {**stars, "ITH": {}}, {}, set(), [], {})
    assert [d["carrier"] for d in p["deliveries"]] == ["FREE"]
    assert ("2B30CEB4", "ITH") in p["self_moves"]


def test_tag_hygiene_for_spare_devices():
    from rsweb import loadouts as lo
    devices = [  # from the player's list
        {"device_code": "1DE93245", "device_type": "transport_hauler", "location": "FALQUORYX-1", "status": "idle",
         "tags": ["home:falquoryx", "spare"]},
        {"device_code": "28C47D66", "device_type": "surge_plate", "location": "FALQUORYX-1-L4", "status": "idle",
         "tags": ["spare", "taxi"], "taxi_mode": "taxi"},
        {"device_code": "512FE0F9", "device_type": "mining_drone", "location": "AEMEROTH-5-L4", "status": "idle",
         "controller_device_code": "84EE1EF1", "tags": ["spare"]},
        {"device_code": "6B208B47", "device_type": "transport_drone", "location": "FALQUORYX-BELT-1", "status": "idle", "tags": ["spare"]},
    ]
    p = lo.plan({"phases": [], "systems": {}}, devices, [], {}, {}, {}, set(), [], {})
    assert p["tag_remove"]["1DE93245"] == ["home:falquoryx"]           # spare → no home, even with no phase for haulers
    assert p["tag_remove"]["28C47D66"] == ["spare"]                    # taxi plates serve a ferry
    assert p["tag_remove"]["512FE0F9"] == ["spare"]                    # working for a controller
    assert "6B208B47" not in p["tag_remove"]                           # a free spare stays spare


def test_fleet_devices_are_left_alone_by_loadouts_and_ami():
    from rsweb import loadouts as lo
    from rsweb.ami_schedule import adoptable
    fleet, devices = _fleet_world()
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"mining_drone": 0}}], "systems": {"AEM": "p"}}
    p = lo.plan(cfg, devices, [], {}, {}, {}, set(), [], {})
    assert set(p["tag_add"]) == {"LOCAL001"}                     # only the non-fleet drone is touched
    local_ctrl = {"device_code": "XC", "device_type": "ami_mining_controller", "location": "AEM-5-L4", "tags": []}
    assert adoptable(devices, local_ctrl, {}) == ["LOCAL001"]
    fleet_ctrl = next(d for d in devices if d["device_code"] == "MC000001")
    assert adoptable(devices, fleet_ctrl, {}) == ["MD000001"]     # its own fleet's idle drone at its location


def test_fleet_gather_collects_strays_and_recruits_spares():
    from rsweb import fleets as fl
    fleet, devices = _fleet_world()
    fleet["wants"]["mining_drone"] = 5     # has 3 (MD1, MD2, LOST) → 2 short
    devices += [
        {"device_code": "SPARE01", "device_type": "mining_drone", "location": "FAL-2", "status": "idle", "tags": ["spare"]},
        {"device_code": "SPARE02", "device_type": "mining_drone", "location": "ITH-BELT-1", "status": "idle", "tags": ["spare"]},
        {"device_code": "SPARE03", "device_type": "mining_drone", "location": "AEM-3", "status": "idle", "tags": ["spare"]},
        {"device_code": "BUSY01", "device_type": "mining_drone", "location": "AEM-3", "status": "idle", "tags": ["spare"],
         "controller_device_code": "X"},
        {"device_code": "SPARE04", "device_type": "survey_drone", "location": "AEM-3", "status": "idle", "tags": ["spare"]},  # not needed
    ]
    stars = {"AEM": {"position": {"x": 0, "y": 0, "z": 0}}, "FAL": {"position": {"x": 2, "y": 0, "z": 0}, "entry_point": "FAL-1-L4"},
             "ITH": {"position": {"x": 9, "y": 0, "z": 0}}}
    plan = fl.gather_plan(fleet, devices, stars, set())
    # nearest spares first: SPARE03 is in AEM with the carrier (boards next assemble/at travel), SPARE01 in FAL
    assert [d["device_code"] for d in plan["recruit"]] == ["SPARE03", "SPARE01"]
    assert plan["carrier"] == "MF000001"
    assert [(s_, sorted(d["device_code"] for d in ds)) for s_, ds in plan["tour"]] == [("AEM", ["SPARE03"]), ("FAL", ["LOST0001", "SPARE01"])]
    bodies = [(st["path"], st["body"]) for st in fl.gather_steps(fleet, plan, stars)]
    assert ("/devices/SPARE01", {"configuration": {"add_tags": ["fleet:prospector-1"], "remove_tags": ["spare"]}}) in bodies
    assert ("/devices/MF000001", {"command": "travel", "destination": "FAL-1-L4"}) in bodies
    assert ("/devices/LOST0001", {"command": "travel", "destination": "FAL-1-L4"}) in bodies
    assert ("/devices/MF000001", {"command": "attach", "device": "SPARE01"}) in bodies
    # SPARE03 boards right where the carrier is (no carrier trip), before it sets off
    i_local = bodies.index(("/devices/MF000001", {"command": "attach", "device": "SPARE03"}))
    assert ("/devices/SPARE03", {"command": "travel", "destination": "AEM-5-L4"}) in bodies
    assert i_local < bodies.index(("/devices/MF000001", {"command": "travel", "destination": "FAL-1-L4"}))


def test_fleet_loadout_lines_add_and_remove(client):
    eng = client.app.state.worker.automations
    client.portal.call(eng.save_fleets, [{"id": "p1", "name": "P1", "role": "mining", "home": "SOL", "wants": {}}])
    world = client.app.state.api.http._transport.app.state.world
    world.devices[1]["tags"] = ["fleet:p1"]
    dtype, code = world.devices[1]["device_type"], world.devices[1]["device_code"]
    client.portal.call(client.app.state.worker.sync_devices)
    # picking a type adds it straight away with qty 1 (no qty sent)
    r = client.post("/fleets/p1/want", data={"type": "ftl_relay_x"}, headers=HX)
    assert r.headers.get("HX-Refresh") and client.portal.call(eng.fleets)[0]["wants"] == {"ftl_relay_x": 1}
    client.post("/fleets/p1/want", data={"type": "ftl_relay_x", "qty": "3"}, headers=HX)
    client.post("/fleets/p1/want", data={"type": dtype, "qty": "1"}, headers=HX)
    page = client.get("/fleets/details?all=1", headers=H).text
    assert 'loadout-form' in page and "✕" in page
    # Save (name/role/home) no longer touches the loadout
    client.post("/fleets/p1/edit", data={"name": "P1b", "role": "mining", "home": "SOL"}, headers=HX)
    assert client.portal.call(eng.fleets)[0]["wants"] == {"ftl_relay_x": 3, dtype: 1}
    assert client.portal.call(eng.fleets)[0]["id"] == "p1b"                  # renamed: the id (and tag) follow the name
    # qty 0 removes the line entirely and releases that type's members
    client.post("/fleets/p1b/want", data={"type": dtype, "qty": "0"}, headers=HX)
    assert client.portal.call(eng.fleets)[0]["wants"] == {"ftl_relay_x": 3}
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "fleets" and "retag" not in j["title"]][-1]
    assert job["steps"][0]["path"] == f"/devices/{code}" and job["steps"][0]["body"] == {"configuration": {"remove_tags": ["fleet:p1b"]}}
    # sent while the game still has the old tag: the old one goes too, or the device would rejoin at the next sync
    from rsweb.fleets import with_renamed_tags
    assert with_renamed_tags("PATCH", f"/devices/{code}", job["steps"][0]["body"], {"p1": "p1b"}) == \
        {"configuration": {"remove_tags": ["fleet:p1b", "fleet:p1"]}}
    import time
    for _ in range(50):
        if "fleet:p1" not in (world.devices[1].get("tags") or []):
            break
        time.sleep(0.1)
    assert not any(t.startswith("fleet:p1") for t in world.devices[1].get("tags") or [])


def test_fleet_loadout_editor_lines(client):
    eng = client.app.state.worker.automations
    client.portal.call(eng.save_fleets, [{"id": "p1", "name": "P1", "role": "mining", "home": "SOL", "wants": {}}])
    world = client.app.state.api.http._transport.app.state.world
    world.devices[1]["tags"] = ["fleet:p1"]
    dtype, code = world.devices[1]["device_type"], world.devices[1]["device_code"]
    client.portal.call(client.app.state.worker.sync_devices)
    base = {"lines": "1", "name": "P1", "role": "mining", "home": "SOL", "autosave": "1"}
    # several lines; a blank line and a qty-0 line are ignored
    r = client.post("/fleets/p1/edit", data={**base, "type": [dtype, "survey_drone", "", "relay_x"], "qty": ["2", "1", "", "0"]}, headers=HX)
    assert "Loadout saved" in r.text and not r.headers.get("HX-Refresh")
    assert client.portal.call(eng.fleets)[0]["wants"] == {dtype: 2, "survey_drone": 1}
    page = client.get("/fleets/details?all=1", headers=H).text
    assert page.count('class="lnew"') == 1 and 'name="lines"' in page
    # removing a type with members (release) drops the line and releases them
    client.post("/fleets/p1/edit", data={**base, "type": [dtype, "survey_drone"], "qty": ["0", "1"], "release": dtype}, headers=HX)
    assert client.portal.call(eng.fleets)[0]["wants"] == {"survey_drone": 1}
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "fleets"][-1]
    assert job["steps"][0]["path"] == f"/devices/{code}"
    # Save without autosave refreshes the page; everything removed → empty loadout
    r = client.post("/fleets/p1/edit", data={"lines": "1", "name": "P1", "role": "mining", "home": "SOL"}, headers=HX)
    assert r.headers.get("HX-Refresh") and client.portal.call(eng.fleets)[0]["wants"] == {}


def test_fleet_attach_points():
    from rsweb import fleets as fl
    fleet, devices = _fleet_world()
    bps = {"surge_platform": {"device_type": "surge_platform", "features": ["surge", "attach"], "attach_capacity": 4},
           "transport_hauler": {"device_type": "transport_hauler", "features": ["cruise", "transport"]}}
    pt = fl.attach_points(fleet, devices, bps)
    # members: one mobile fleet (36); riders = 3 controllers + 3 mining drones + 1 survey drone; the freighter flies itself
    # (the test devices list no stow command, so they all need attach points)
    assert (pt["now"]["available"], pt["now"]["needed"], pt["now"]["short"], pt["now"]["attached"]) == (36, 7, 0, 1)
    assert pt["now"]["attach"] == {"available": 36, "needed": 7}
    assert pt["plan"]["attach"]["available"] == 36 and pt["plan"]["needed"] == 8      # 4 mining drones wanted (3 have)
    # swap the mobile fleet for one platform and add 10 haulers → short
    fleet["wants"] = {"surge_platform": 1, "transport_hauler": 10}
    devices = [d for d in devices if d["device_type"] != "mobile_fleet"]
    pt = fl.attach_points(fleet, devices, bps)
    assert pt["now"]["available"] == 0 and pt["now"]["short"] == 7
    assert pt["plan"]["attach"]["available"] == 4 and pt["plan"]["needed"] == 17 and pt["plan"]["short"] == 13
    assert pt["plan"]["fix"].startswith("2 surge carrier(s), 4 surge platform(s)")
    assert fl.type_profile("cargo_freighter", bps, devices) == {"carrier": 0, "hold": 0, "flies": True, "stowable": False}


def test_cargo_vessel_counts_hold_and_attach():
    from rsweb import fleets as fl
    T = ["fleet:hauler"]
    def dev(code, t, **kw):
        return {"device_code": code, "device_type": t, "location": "FAL-1-L4", "status": "idle", "tags": T, **kw}
    stow_cmds = ["travel", "stow", "deploy"]
    devices = [dev("CV1", "cargo_vessel", features=["surge", "cruise", "stow", "attach"], stow_capacity=50, attach_capacity=3,
                   available_commands=["travel", "attach", "detach"])]
    devices += [dev(f"MD{i}", "mining_drone", features=["cruise", "mine", "stow"], available_commands=stow_cmds) for i in range(6)]
    devices += [dev(f"TD{i}", "transport_drone", features=["cruise", "transport"], available_commands=["travel"]) for i in range(2)]
    fleet = {"id": "hauler", "name": "Hauler", "role": "mining", "home": "FAL",
             "wants": {"cargo_vessel": 1, "mining_drone": 6, "transport_drone": 4}}
    assert fl.is_carrier(devices[0]) and fl.hold(devices[0]) == 50 and fl.capacity(devices[0]) == 3
    pt = fl.attach_points(fleet, devices, {})
    assert pt["now"]["hold"] == {"available": 50, "needed": 6, "used": 6} and pt["now"]["attach"] == {"available": 3, "needed": 2}
    assert pt["now"]["short"] == 0 and pt["now"]["available"] == 53
    # loadout: 4 transport drones need attach points, only 3 → short 1 (the hold can't take them)
    assert pt["plan"]["attach"] == {"available": 3, "needed": 4} and pt["plan"]["short"] == 1
    assert pt["plan"]["fix"] == "1 surge carrier(s), 1 surge platform(s) or 1 cargo vessel(s)"
    prof = fl.type_profile("cargo_vessel", {}, [])   # from known sizes, no device or blueprint
    assert prof["carrier"] == 3 and prof["hold"] == 50 and not prof["flies"]
    # assemble: transport drones attach, mining drones stow themselves into the vessel
    steps, problems = fl.assemble_steps(fleet, devices)
    bodies = [(s["path"], s["body"]) for s in steps]
    assert problems == []
    assert ("/devices/CV1", {"command": "attach", "device": "TD0"}) in bodies and ("/devices/CV1", {"command": "attach", "device": "TD1"}) in bodies
    assert sum(1 for p, b in bodies if b and b.get("command") == "stow" and b.get("target") == "CV1") == 6
    # unload: deploy the stowed, detach the attached
    for d in devices:
        if d["device_type"] == "mining_drone":
            d["stowed_in_device_code"] = "CV1"
        elif d["device_type"] == "transport_drone":
            d["attached_to_device_code"] = "CV1"
    cmds = sorted(((s["body"] or {}).get("command"), s["path"]) for s in fl.unload_steps(fleet, devices))
    assert cmds.count(("deploy", "/devices/MD0")) == 1 and ("detach", "/devices/CV1") in cmds
    # only the carrier flies; stowed and attached members ride along
    assert {s["path"] for s in fl.travel_steps(fleet, devices, "AEM", {}) if s["method"] != "WAIT"} == {"/devices/CV1"}


def test_loadout_pass_hands_off_and_activates(client):
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.db.kv_set, "devices", _arrival_world())
    lines = client.portal.call(eng.apply_loadouts, None, True)
    assert "MD1 joins controller MC1 (flies ITH-2-L4 → ITH-BELT-1)" in lines
    assert any(l.startswith("patrol MT1") for l in lines)
    jobs = client.portal.call(eng.jobs)
    assert any(j["title"] == "loadouts: patrol MT1" for j in jobs)
    assert any(j["title"] == "loadouts: MC1 adopts MD1" for j in jobs)
    assert client.portal.call(client.app.state.db.kv_get, "loadout_woken", {}) == {"MT1": "ITH"}


def test_snapshot_capture_with_stowed_devices(client):
    import asyncio
    world = client.app.state.api.http._transport.app.state.world
    for d in world.devices:
        if d["device_type"] == "mining_drone":
            d.update(location=None, status="stowed", stowed_in_device_code="2AC61200")
            break
    db = client.app.state.db
    client.post("/diagnostics/snapshot", data={"stars": ""}, headers=HX)
    for _ in range(100):
        st = client.portal.call(db.kv_get, "snapshot_status", {})
        if st.get("state") != "running":
            break
        client.portal.call(asyncio.sleep, 0.05)
    assert st["state"] == "done", st
    snap = client.portal.call(db.kv_get, "snapshot_last", None)
    assert not any("diagnosis failed" in h for h in snap["diagnosis"]["headline"])
    assert any(r["state"] == "stowed" for r in snap["diagnosis"]["drones"])
    assert "Mining drones" in client.get("/diagnostics", headers=H).text


def test_stowable_follows_live_device_data():
    from rsweb import fleets as fl
    snap = _live()
    from rsweb.snapshot import devices_of
    by_type = {}
    for d in devices_of(snap):
        by_type.setdefault(d["device_type"], d)
    for t in ("mining_drone", "survey_drone", "ami_mining_controller", "ami_transport_controller"):
        assert fl.stowable(by_type[t]), t
    for t in ("transport_drone", "transport_hauler", "cargo_freighter"):
        if t in by_type:
            assert not fl.stowable(by_type[t]), t


def test_loadout_reduction_releases_spares_from_their_controller():
    from rsweb import loadouts as lo
    devs = [{"device_code": "MC1", "device_type": "ami_mining_controller", "location": "FAL-BELT-1", "status": "coordinating",
             "tags": ["home:fal"], "features": ["ami"]}]
    devs += [{"device_code": f"MD{i}", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "mining (carbon)",
              "controller_device_code": "MC1", "tags": ["home:fal"]} for i in range(4)]
    cfg = {"phases": [{"id": "p", "name": "P", "wants": {"mining_drone": 2}}], "systems": {"FAL": "p"}}
    p = lo.plan(cfg, devs, [], {}, {"FAL": {}}, {}, set(), [], {})
    assert len(p["made_spare"]) == 2
    for code in p["made_spare"]:
        assert lo.SPARE in p["tag_add"][code] and "home:fal" in p["tag_remove"][code]
    assert sorted(p["releases"]["MC1"]) == p["made_spare"]
    assert any("no longer in the loadout" in l for l in lo.describe(p))
    # audit before the pass: nothing wrong yet (tags not applied); after tags applied but before release: fixed by the pass
    for d in devs:
        if d["device_code"] in p["made_spare"]:
            d["tags"] = ["spare"]
    p2 = lo.plan(cfg, devs, [], {}, {"FAL": {}}, {}, set(), [], {})
    assert sorted(p2["releases"]["MC1"]) == p["made_spare"]            # still wanted gone: released, spare kept
    assert not any(lo.SPARE in (p2["tag_remove"].get(c) or []) for c in p["made_spare"])
    issues = lo.audit(cfg, devs, {"FAL": {}}, p2)
    spare_issues = [a for a in issues if "spare but still run by" in a["issue"]]
    assert len(spare_issues) == 2 and all(a["fixed"] and a["how"] == "released by its controller" for a in spare_issues)
    # released: no longer flip-flops, nothing left to fix
    for d in devs:
        if d["device_code"] in p["made_spare"]:
            d.pop("controller_device_code")
    p3 = lo.plan(cfg, devs, [], {}, {"FAL": {}}, {}, set(), [], {})
    assert not p3["releases"] and not any(lo.SPARE in (p3["tag_remove"].get(c) or []) for c in p["made_spare"])
    assert [a for a in lo.audit(cfg, devs, {"FAL": {}}, p3) if not a["fixed"]] == []


def test_loadouts_page_shows_the_check(client):
    landing = client.get("/fleets", headers=H).text   # landing: status (loaded after) and templates; the slow parts on details
    assert 'hx-get="/fleets/overview"' in landing and 'id="templates"' in landing and "Tags &amp;" not in landing
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Tags &amp; controllers check" in page


def test_loadouts_hold_miners_for_mined_out_systems_and_working_spares():
    from rsweb import loadouts as lo
    cfg = {"phases": [{"id": "big", "name": "Mine", "wants": {"mining_drone": 4}}, {"id": "small", "name": "S", "wants": {"mining_drone": 1}}],
           "systems": {"AEM": "big", "FAL": "small"}}
    devs = [{"device_code": f"F{i}", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "mining (carbon)",
             "tags": ["home:fal"]} for i in range(3)]
    stars = {"AEM": {}, "FAL": {}}
    # AEM's belts have no open sites: nothing is sent or printed for it
    p = lo.plan(cfg, devs, [], {}, stars, {}, set(), [], {}, None, {"AEM": 0, "FAL": 5})
    assert not p["moves"] and not p["prints"]
    assert any("no open mining sites" in u["why"] for u in p["unmet"] if u["star"] == "AEM")
    # AEM has sites, FAL's spares are still mining: they wait (no "Cannot cruise while mining"), and nothing is printed instead
    for d in devs[1:]:
        d["tags"] = ["spare"]
    p = lo.plan(cfg, devs, [], {}, stars, {}, set(), [], {}, None, {"AEM": 3, "FAL": 5})
    assert not p["moves"]
    assert any("still working — sent once idle" in u["why"] for u in p["unmet"])
    assert sum(pr["n"] for pr in p["prints"] if pr["device_type"] == "mining_drone") <= 2   # only the 2 not covered by spares
    # once idle they go
    for d in devs[1:]:
        d["status"] = "idle"
    p = lo.plan(cfg, devs, [], {}, stars, {}, set(), [], {}, None, {"AEM": 3, "FAL": 5})
    assert sorted(c for c, dest in p["moves"].items() if dest == "AEM") == ["F1", "F2"]


def test_loadouts_fill_surveyors_before_miners():
    from rsweb import loadouts as lo
    cfg = {"phases": [{"id": "m", "name": "Mine", "wants": {"mining_drone": 8, "survey_drone": 10}}],
           "systems": {"AEM": "m", "ITH": "m"}, "settings": {"need_stock": False}}
    fac = {"device_code": "AF", "device_type": "autofactory", "location": "FAL-BELT-1", "status": "idle",
           "available_commands": ["enqueue_print"], "print_queue": [{"device_type": "x"}] * 7}   # 3 slots free
    bps = [{"device_type": "mining_drone", "resources": {"structural": 1}, "queue_size": 10},
           {"device_type": "survey_drone", "resources": {"structural": 1}}, {"device_type": "autofactory", "queue_size": 10}]
    p = lo.plan(cfg, [fac], bps, {"FAL-BELT-1": {"structural": 1000}}, {"AEM": {}, "ITH": {}, "FAL": {}}, {}, set(), [], {})
    assert [pr["device_type"] for pr in p["prints"]] == ["survey_drone"] * len(p["prints"])
    assert sum(pr["n"] for pr in p["prints"]) == 3
    assert lo.fill_rank("ami_mining_controller") < lo.fill_rank("survey_drone") < lo.fill_rank("mining_drone") < lo.fill_rank("transport_drone")


def test_loadout_pass_switches_a_relaying_relay_off_before_its_pin(client):
    # live 2026-10-29 (1.308.1): the pass itself crashed with NameError 'devices' on this path
    eng = client.app.state.worker.automations
    relay = {"device_code": "B2B8B2EA", "device_type": "ftl_relay", "location": "OTHILETH-1-L4", "status": "relaying",
             "in_control_range": True, "available_commands": ["deactivate", "travel"], "tags": ["at:othileth-1"]}
    other = {"device_code": "4A8A3109", "device_type": "ftl_relay", "location": "OTHILETH-1-L4", "status": "relaying",
             "tags": []}
    client.portal.call(client.app.state.db.kv_set, "devices", [relay, other])
    client.portal.call(eng.apply_loadouts, None, True)
    job = next(j for j in client.portal.call(eng.jobs) if j["title"] == "loadouts: B2B8B2EA → OTHILETH-1 (pinned)")
    assert job["steps"][0]["body"] == {"command": "deactivate"}


def test_large_devices_learned_from_a_refused_attach_only_stow(client):
    # live 2026-10-29: a rescue sent a carrier to attach vector charge 42B4755C, twice: "Cannot attach a large device"
    from rsweb import fleets as fl
    eng = client.app.state.worker.automations
    charge = {"device_code": "42B4755C", "device_type": "vector_charge", "location": "ALKALAROPS-KUIPER",
              "features": [], "available_commands": ["travel"]}
    client.portal.call(client.app.state.db.kv_set, "devices", [charge])
    assert fl.take_seat({"hold": 1, "attach": 2}, charge) == "stow"   # stows first (owner 2026-10-29)
    fl.NO_ATTACH_TYPES.discard("vector_charge")
    try:
        client.portal.call(eng.learn_no_attach, "42B4755C")
        assert fl.take_seat({"hold": 0, "attach": 2}, charge) is None
        assert fl.take_seat({"hold": 1, "attach": 2}, charge) == "stow"
        assert "vector_charge" in client.portal.call(client.app.state.db.kv_get, "no_attach_types", {})
        platform = {"device_code": "P1", "device_type": "surge_platform", "attach_capacity": 2}
        assert fl.cargo_carrier(platform, [platform, charge], False, fl.attachable(charge)) is None
    finally:
        fl.NO_ATTACH_TYPES.discard("vector_charge")


def test_blueprint_stow_feature_makes_a_type_stowable():
    from rsweb import fleets as fl
    d = {"device_code": "X1", "device_type": "gravity_lens", "features": ["focus"], "available_commands": ["travel"]}
    assert not fl.stowable(d)
    try:
        fl.learn_stowable([{"device_type": "gravity_lens", "features": ["focus", "stow"]}])
        assert fl.stowable(d)
    finally:
        fl.STOWABLE_TYPES.discard("gravity_lens")


def test_a_trip_refused_for_distance_is_lifted_by_a_carrier(client):
    """Live 2026-10-30: survey controller 27925A12 at ISOLITHOR-1, sent to ISOLITHOR-BELT-1 (≈639 AU, over the 30 AU
    cruise limit), was refused every pass and nothing fetched it. The refusal tags it `at:` + `lift`; the loadouts pass
    then sends a carrier, which drops it there and takes both tags off."""
    from rsweb import loadouts as lo
    eng = client.app.state.worker.automations
    ctrl = {"device_code": "SC1", "device_type": "ami_survey_controller", "location": "SOL-OORT", "status": "idle",
            "features": ["ami", "cruise", "stow"], "available_commands": ["travel", "stow", "deploy"], "tags": ["fleet:x"]}

    async def devices():
        return [ctrl]
    sent = []

    async def send(method, path, body, label):
        sent.append((method, path, body))
        return True, {}, None
    eng.devices, eng.send = devices, send
    st = {"path": "/devices/SC1", "body": {"command": "travel", "destination": "SOL-BELT-1"}}
    note = client.portal.call(eng._lift, st)
    assert sent == [("PATCH", "/devices/SC1", {"configuration": {"add_tags": ["at:sol-belt-1", "lift"]}})] and "carrier" in note
    ctrl["tags"] += ["at:sol-belt-1", "lift"]
    assert "already" in client.portal.call(eng._lift, st) and len(sent) == 1
    cv = {"device_code": "CV1", "device_type": "cargo_vessel", "location": "SOL-3-L4", "status": "idle",
          "features": ["surge", "transport"], "available_commands": ["travel", "stow"], "stow_capacity": 9, "tags": []}
    stars = {"SOL": {"entry_point": "SOL-3-L4"}}
    cfg = {"phases": [], "systems": {}, "fleets": [{"id": "x", "name": "X", "role": "explore", "home": "SOL", "station": True,
                                                    "wants": {}}]}
    p = lo.plan(cfg, [ctrl, cv], [], {}, stars, {}, set(), [], {})
    dl = p["deliveries"]
    assert len(dl) == 1 and dl[0]["devices"] == ["SC1"] and not p.get("pins")
    bodies = [s["body"] for s in lo.delivery_steps(dl[0], p["by_code"], stars, False)]
    assert {"command": "travel", "destination": "SOL-3-L4"} not in bodies   # the carrier picks it up, it doesn't fly over
    assert {"command": "travel", "destination": "SOL-OORT"} in bodies and {"command": "travel", "destination": "SOL-BELT-1"} in bodies
    removed = [t for b in bodies if b and "configuration" in b for t in b["configuration"].get("remove_tags") or []]
    assert "lift" in removed and "at:sol-belt-1" in removed


def test_loadout_surplus_keeps_the_home_tagged_device():
    """Live 2026-10-05: FALQUORYX wants 1 autofactory and has 3; the one tagged home:falquoryx was being made spare."""
    from rsweb import loadouts as lo
    cfg, devices, bps, inv, stars = _lo_world()
    cfg["phases"][0]["wants"]["autofactory"] = 1
    for code in ("AF0", "AF1"):
        devices.append({**next(d for d in devices if d["device_code"] == "AF"), "device_code": code})
    next(d for d in devices if d["device_code"] == "AF1")["tags"] = ["home:aaa"]
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    row = next(r for r in _rep(p, "AAA")["rows"] if r["type"] == "autofactory")
    assert row["surplus"] == 2 and "AF1" not in row["spare"]


def test_loadout_plan_places_relays_and_controllers():
    from rsweb import loadouts as lo
    from rsweb import placement as pl
    cfg, devices, bps, inv, stars = _lo_world()
    devices.append({"device_code": "RL", "device_type": "ftl_relay", "location": "AAA-BELT-1", "status": "idle",
                    "features": ["cruise", "relay"], "available_commands": ["travel", "activate"], "operational_capacity": 100.0})
    devices.append({"device_code": "RL2", "device_type": "ftl_relay", "location": "BBB-3", "status": "relaying",
                    "features": ["cruise", "relay"], "available_commands": ["travel", "activate"], "operational_capacity": 100.0})
    devices.append({"device_code": "MC2", "device_type": "ami_mining_controller", "location": "BBB-5-L4", "status": "idle",
                    "features": ["cruise", "ami"], "available_commands": ["travel"], "operational_capacity": 100.0})
    geo = {"AAA": pl.geography("AAA", devices, None, "AAA-OORT"),
           "BBB": pl.geography("BBB", devices, {"asteroid_belt": {"belts": [{"designation": "BBB-BELT-1"}]}}, "BBB-5-L4")}
    geo["AAA"]["lagrange"] = ["AAA-3-L4"]
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {}, geo=geo)
    places = dict(p["places"])
    assert places.get("RL") == "AAA-3-L4"
    assert "RL2" not in places and any(m["code"] == "RL2" for m in p["misplaced"])   # relaying: only reported
    assert places.get("MC2") == "BBB-BELT-1"
    assert "AC" not in places                                                      # already at the belt
    assert any("RL" in line and "AAA-3-L4" in line for line in lo.describe(p))


def test_slingshot_stowed_with_the_replicant_counts_as_here(client):
    """1.208.1 (live 2026-10-22: slingshot 7DBF642C and replicant 8EC42FBA's matrix in the same hold of 939E7168):
    a stowed slingshot has no location of its own; it is where its carrier is. Linking works there; firing needs it
    deployed (1.209.1: the game answers 400 "Slingshot must be deployed"), so the page says so instead."""
    from rsweb.mock import REP
    world = client.app.state.api.http._transport.app.state.world
    vs = {"device_code": "VS000001", "device_type": "cargo_vessel", "location": "SOL-BELT-1", "status": "idle",
          "features": ["stow"], "operational_capacity": 100.0, "replicant_code": REP, "available_commands": ["travel"]}
    sl = {"device_code": "SL000003", "device_type": "ftl_slingshot", "location": None, "status": "stowed",
          "stowed_in_device_code": "VS000001", "features": ["slingshot", "stow"], "operational_capacity": 100.0,
          "linked_device": None, "replicant_code": REP, "available_commands": ["deploy"]}
    mx = {"device_code": "MX000003", "device_type": "empty_replicant_matrix", "location": None, "status": "stowed",
          "stowed_in_device_code": "VS000001", "features": ["matrix"], "operational_capacity": 100.0,
          "replicant_code": REP, "available_commands": ["deploy"]}
    world.devices += [vs, sl, mx]
    client.portal.call(client.app.state.worker.sync_devices)
    r = client.post("/slingshots/SL000003/link", data={"matrix": "MX000003"}, headers=HX)
    assert "must be together" not in r.text and sl["linked_device"] == "MX000003"
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get(f"/replicants/{REP}", headers=H).text
    assert '<option value="SL000003">' not in page and "SL000003 is stowed in" in page
    r = client.post(f"/replicants/{REP}/slingshot", data={"slingshot": "SL000003"}, headers=HX)
    assert "deploy it first" in r.text and not world.teleports   # live: 400 "Slingshot must be deployed"
    sl.update(location="SOL-BELT-1", status="idle", stowed_in_device_code=None)
    client.portal.call(client.app.state.worker.sync_devices)
    assert '<option value="SL000003">' in client.get(f"/replicants/{REP}", headers=H).text
    client.post(f"/replicants/{REP}/slingshot", data={"slingshot": "SL000003"}, headers=HX)
    assert world.teleports == [(REP, "SL000003")]


def test_loadout_prints_spread_over_a_systems_autofactories():
    from rsweb import loadouts as lo
    cfg, devices, bps, inv, stars = _lo_world()
    devices.append({**next(d for d in devices if d["device_code"] == "AF"), "device_code": "AF2"})
    devices.append({**next(d for d in devices if d["device_code"] == "AF"), "device_code": "AF3",
                    "status": "printing (ami_mining_controller)", "printing": {"device_type": "ami_mining_controller"}})
    inv = {"AAA-3-L4": {"structural": 1000.0}}
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    per = Counter()
    for pr in p["prints"]:
        assert pr["device_type"] == "survey_drone"
        per[pr["factory"]] += pr["n"]
    # 3 survey drones short (2 in AAA, 1 in BBB): shared by AF and AF2; AF3 is 600 s into a controller, so it gets none
    assert sum(per.values()) == 3 and sorted((per["AF"], per["AF2"])) == [1, 2] and not per["AF3"]
    # one print per factory step, each tagged for its system
    assert {pr["star"] for pr in p["prints"]} == {"AAA", "BBB"}


def test_fleet_fill_takes_nearest_spares_and_comes_back():
    from rsweb import fleets as fl
    fleet, devices = _fleet_world()
    devices += [{"device_code": "SPARE001", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "idle",
                 "tags": ["spare"], "available_commands": ["travel"]},
                {"device_code": "SPARE002", "device_type": "mining_drone", "location": "FAR-BELT-1", "status": "idle",
                 "tags": ["spare"], "available_commands": ["travel"]},
                {"device_code": "BUSY0001", "device_type": "mining_drone", "location": "AEM-5-L4", "status": "mining (carbon)",
                 "tags": ["spare"], "available_commands": ["travel"]}]
    stars = {"AEM": {"position": {"x": 0, "y": 0, "z": 0}, "entry_point": "AEM-5-L4"},
             "FAL": {"position": {"x": 1, "y": 0, "z": 0}, "entry_point": "FAL-4-L4"},
             "FAR": {"position": {"x": 9, "y": 0, "z": 0}, "entry_point": "FAR-4-L4"}}
    steps, recruits, notes = fl.fill_plan(fleet, devices, stars, set())
    assert [d["device_code"] for d in recruits] == ["SPARE001"]           # 1 short: the nearest idle spare
    bodies = [(s["path"], s["body"]) for s in steps]
    assert ("/devices/SPARE001", {"configuration": {"add_tags": ["fleet:prospector-1"], "remove_tags": ["spare"]}}) in bodies
    travels = [b["destination"] for p, b in bodies if p == "/devices/MF000001" and b and b.get("command") == "travel"]
    assert travels == ["FAL-4-L4", "AEM-5-L4"]                             # picks up (and the stranded LOST0001), comes back

def test_mission_carrier_unloads_inside_the_system_not_at_the_kuiper_belt():
    """Live 2026-10-06: the Surveyors' carrier arrived at LORQELYR-KUIPER (the entry point) and unloaded there."""
    from rsweb import fleets as fl, placement as pl
    devices = [
        {"device_code": "HV", "device_type": "heaven_vessel", "location": "LOR-KUIPER", "status": "idle", "features": ["surge"],
         "stow_capacity": 10, "tags": ["fleet:s"]},
        {"device_code": "SC", "device_type": "ami_survey_controller", "location": None, "stowed_in_device_code": "HV",
         "status": "stowed", "tags": ["fleet:s"]},
    ]
    fleet = {"id": "s", "name": "Surveyors", "role": "explore", "home": "FAL", "wants": {}}
    geo = pl.geography("LOR", devices, {"planets": [{"designation": "LOR-2", "orbital_distance_au": 2},
                                                    {"designation": "LOR-1", "orbital_distance_au": 1}]}, "LOR-KUIPER")
    assert fl.deploy_spot(geo) == "LOR-1-L4"
    descs = [s["desc"] for s in fl.unload_steps(fleet, devices, fl.deploy_spot(geo))]
    assert descs == ["HV → LOR-1-L4 (to unload inside the system)", "wait for HV at LOR-1-L4", "deploy SC from HV"]
    assert fl.deploy_spot(pl.geography("LOR", [], None, "LOR-KUIPER")) is None     # nothing known: unload where it is


def test_spare_at_home_replaces_a_queued_print_and_busy_spares_are_waited_for():
    """Snapshot 2026-11-11 01:08: Delta Printing Hub (wants 1 system hub) had one queued on E76D3831 while spare hubs
    13A4BCE6 (relaying) and 5B9A394D sat at ISOLITHOR-1-L4. A pass ordered it while the spares were busy; the order then
    counted as incoming, so the spares were never used and a third hub was on its way."""
    from rsweb import loadouts as lo
    hub = lambda c, st: {"device_code": c, "device_type": "system_hub", "location": "ISO-1-L4", "status": st,  # noqa: E731
                         "tags": ["spare"], "features": ["cruise", "relay", "modular"]}
    af = {"device_code": "AF1", "device_type": "autofactory", "location": "ISO-BELT-1", "status": "idle", "tags": ["fleet:hub"],
          "available_commands": ["enqueue_print"], "print_queue": [{"device_type": "system_hub", "tags": ["fleet:hub", "to:iso"]}]}
    fleets = [{"id": "hub", "name": "hub", "home": "ISO", "station": True, "wants": {"system_hub": 1}}]
    cfg = {"phases": [], "fleets": fleets, "fleets_migrated": True}
    bps = [{"device_type": "system_hub", "resources": {"structural": 50}, "print_time": 600}]
    inv = {"ISO-BELT-1": {"structural": 1000}}
    stars = {"ISO": {"position": {"x": 0, "y": 0, "z": 0}}}
    devices = [af, hub("H1", "inactive"), hub("H2", "relaying")]
    order = {"star": "ISO", "fleet": "hub", "device_type": "system_hub", "factory": "AF1", "at": "2026-11-10T22:15:19+00:00"}
    p = lo.plan(cfg, devices, bps, inv, stars, {}, set(), [order], {})
    assert p["prints"] == []
    assert [(c["factory"], c["spare"]) for c in p["cancel_orders"]] == [("AF1", "H2")]   # the working hub first
    assert p["assign"] == {"H2": "fleet:hub"} and "H2" in p["arrived"]
    assert any("comes off the queue" in line for line in lo.describe(p))
    # no order yet and both spares on a job: no print, they're waited for
    af["print_queue"] = []
    p = lo.plan(cfg, devices, bps, inv, stars, {}, {"H1", "H2"}, [], {})
    assert p["prints"] == [] and p["cancel_orders"] == []
    assert any(u["type"] == "system_hub" and "busy right now" in u["why"] for u in p["unmet"])
    # with neither spare there, it prints
    p = lo.plan(cfg, [af], bps, inv, stars, {}, set(), [], {})
    assert [pr["device_type"] for pr in p["prints"]] == ["system_hub"]
    # snapshot 2026-11-12 20:22: the fleet has its hub (13A4BCE6) and a second one is still ordered: it comes off
    member = {**hub("H2", "relaying"), "tags": ["fleet:hub"]}
    p = lo.plan(cfg, [af, member], bps, inv, stars, {}, set(), [order, {**order, "at": "2026-11-12T00:00:32+00:00"}], {})
    assert p["prints"] == [] and [(c["at"], c["spare"]) for c in p["cancel_orders"]] == [
        ("2026-11-12T00:00:32+00:00", None), ("2026-11-10T22:15:19+00:00", None)]
    assert any("has its count without" in line for line in lo.describe(p))
    # one still needed (no hub yet) stays
    p = lo.plan(cfg, [af], bps, inv, stars, {}, set(), [order], {})
    assert p["cancel_orders"] == [] and p["prints"] == []


def test_cancel_loadout_print_takes_the_newest_matching_print_off(client):
    from functools import partial
    eng = client.app.state.worker.automations
    for body in ({"device_type": "vector_charge"}, {"device_type": "system_hub", "tags": ["fleet:hub", "to:sol"]},
                 {"device_type": "system_hub", "tags": ["fleet:other"]}, {"device_type": "system_hub", "tags": ["fleet:hub"]}):
        client.portal.call(partial(eng.api.request, "POST", "/devices/AF00BEEF", json_body={"command": "enqueue_print", **body}))
    q0 = client.portal.call(eng.api.request, "GET", "/devices/AF00BEEF")["print_queue"]
    orders = [{"star": "SOL", "fleet": "hub", "device_type": "system_hub", "factory": "AF00BEEF", "at": "2026-11-10T22:00:00+00:00"},
              {"star": "SOL", "fleet": "hub", "device_type": "system_hub", "factory": "AF00BEEF", "at": "2026-11-10T22:15:00+00:00"}]
    c = {"factory": "AF00BEEF", "device_type": "system_hub", "fleet": "hub", "star": "SOL", "spare": "H1"}
    left = client.portal.call(eng.cancel_loadout_print, c, orders)
    assert left == orders[:1]
    q = client.portal.call(eng.api.request, "GET", "/devices/AF00BEEF")["print_queue"]
    assert len(q) == len(q0) - 1
    assert [x.get("tags") for x in q if x["device_type"] == "system_hub"] == [x.get("tags") for x in q0 if x["device_type"] == "system_hub"][:-1]


def test_fleet_carrier_fetches_before_going_home_and_old_home_tags_follow_the_fleet():
    """Live 2026-10-07: Miner 1's only carrier was sent home every pass (it was away fetching an observatory), so the 31
    devices waiting in KELMORNEA were never collected; the observatory was still tagged to:kelmornea after the move."""
    from rsweb import loadouts as lo
    D = lambda code, t, loc, **kw: {"device_code": code, "device_type": t, "location": loc, "status": "idle",  # noqa: E731
                                    "replicant_code": "R1", **kw}
    devices = [D("MF", "mobile_fleet", "FAL-1-L4", features=["surge", "attach"], attach_capacity=36, tags=["fleet:m1"]),
               D("MD1", "mining_drone", "KEL-BELT-1", tags=["fleet:m1"]),
               D("MD2", "mining_drone", "KEL-BELT-1", tags=["fleet:m1"]),
               D("OBS", "galactic_observatory", "FAL-1-L4", features=["cruise", "modular"], tags=["fleet:m1", "to:kel"])]
    stars = {s: {"position": {"x": i, "y": 0, "z": 0}, "entry_point": f"{s}-1-L4"} for i, s in enumerate(("FAL", "KEL", "LAR"))}
    fleets = [{"id": "m1", "name": "Miner 1", "home": "LAR", "station": True, "wants": {}}]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert [(dl["carrier"], dl["from"], sorted(dl["devices"]), dl.get("stay")) for dl in p["deliveries"]] == \
        [("MF", "KEL", ["MD1", "MD2"], True)]                       # the biggest batch first, and it stays home after
    assert "MF" not in p["returning"] and p["tag_add"]["OBS"] == ["to:lar"] and p["tag_remove"]["OBS"] == ["to:kel"]
    steps = lo.delivery_steps(p["deliveries"][0], p["by_code"], stars, True)
    assert "return" not in steps[-1]["desc"]
    # nothing to carry: the carrier goes home on its own
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices[:1], [], {}, stars, {}, set(), [], {})
    assert p["self_moves"] == [("MF", "LAR")] and p["returning"] == ["MF"]


def test_large_devices_compact_on_their_own_before_a_carrier_comes(client):
    """Observatories take over 2 h to compact: that starts as soon as the move is planned, without a carrier waiting."""
    from rsweb import loadouts as lo
    obs = {"device_code": "OBS", "device_type": "galactic_observatory", "location": "FAL-1-L4", "status": "idle",
           "features": ["cruise", "modular"], "available_commands": ["compact", "unfurl", "travel"], "tags": ["fleet:m1"]}
    mf = {"device_code": "MF", "device_type": "mobile_fleet", "location": "FAL-1-L4", "status": "idle",
          "features": ["surge", "attach"], "attach_capacity": 36, "tags": ["fleet:m1"]}
    stars = {s: {"position": {"x": i, "y": 0, "z": 0}, "entry_point": f"{s}-1-L4"} for i, s in enumerate(("FAL", "LAR"))}
    fleets = [{"id": "m1", "name": "Miner 1", "home": "LAR", "station": True, "wants": {}}]
    cfg = {"phases": [], "fleets": fleets, "fleets_migrated": True}
    p = lo.plan(cfg, [obs, mf], [], {}, stars, {}, set(), [], {})
    assert p["compact"] == [("OBS", "LAR")] and not p["deliveries"]
    assert any("OBS is being compacted" in u["why"] for u in p["unmet"])
    obs["status"] = "compacting"                                                   # under way: no second job, no carrier
    p = lo.plan(cfg, [obs, mf], [], {}, stars, {}, set(), [], {})
    assert p["compact"] == [] and not p["deliveries"]
    obs["status"] = "compacted"                                                    # done: now the carrier comes
    p = lo.plan(cfg, [obs, mf], [], {}, stars, {}, set(), [], {})
    assert [(dl["carrier"], dl["devices"]) for dl in p["deliveries"]] == [("MF", ["OBS"])]
    # the engine's compaction job: one compact step, waiting long enough for an observatory (≈30 % of 8 h + 30 min)
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.db.kv_set, "blueprints", [{"device_type": "galactic_observatory", "print_time": 28800}])
    obs["status"] = "idle"
    assert client.portal.call(eng.start_compactions, {"compact": [("OBS", "LAR")], "by_code": {"OBS": obs}}) == 1
    job = next(j for j in client.portal.call(eng.jobs) if "compact OBS" in j["title"])
    assert [s["body"] for s in job["steps"]] == [{"command": "compact"}] and job["steps"][0]["timeout"] >= 28800 * 0.3 + 1800
    assert "≈2.4 h" in job["title"]
    # prints bound for another system come out flat-packed
    st = lo.print_steps({"factory": "AF", "factory_star": "FAL", "device_type": "galactic_observatory", "n": 1, "star": "LAR",
                         "fleet": "m1"})[0]
    assert st["body"]["flatpack"] is True and "flat-packed" in st["desc"]
    assert "flatpack" not in lo.print_steps({"factory": "AF", "factory_star": "FAL", "device_type": "galactic_observatory",
                                             "n": 1, "star": "FAL"})[0]["body"]


def test_deploy_is_refused_while_the_carrier_is_travelling(client):
    """Live 2026-10-06: slingshot E28DBE58 deployed mid-surge came out between systems, with no location."""
    world = client.app.state.api.http._transport.app.state.world
    vessel = next(d for d in world.devices if "vessel" in d["device_type"])
    cargo = next(d for d in world.devices if d["device_type"] == "survey_drone")
    vessel.update({"status": "travelling", "travel": {"destination": "ABOTEIN-1-L4", "arrives_at": "2099-01-01T00:00:00+00:00"}})
    cargo.update({"status": "stowed", "location": None, "stowed_in_device_code": vessel["device_code"]})
    client.portal.call(client.app.state.worker.sync_devices)
    r = client.post(f"/devices/{cargo['device_code']}/command", data={"command": "deploy"}, headers=HX)
    assert "is traveling to ABOTEIN-1-L4" in r.text and "between systems" in r.text


def test_folded_devices_are_remembered_so_the_carrier_comes(client):
    """Live 2026-10-07: observatory B3AEBF60 was compacted but its status didn't say so; every loadout pass made another
    compact job ('Device is already compacted') and no carrier was ever sent."""
    from rsweb import loadouts as lo
    from rsweb.modular import FOLDED_KV, remember
    obs = {"device_code": "OBS", "device_type": "galactic_observatory", "location": "FAL-1-L4", "status": "idle",
           "features": ["cruise", "modular"], "available_commands": ["compact", "unfurl", "travel"], "tags": ["fleet:m1"]}
    mf = {"device_code": "MF", "device_type": "mobile_fleet", "location": "FAL-1-L4", "status": "idle",
          "features": ["surge", "attach"], "attach_capacity": 36, "tags": ["fleet:m1"]}
    stars = {s: {"position": {"x": i, "y": 0, "z": 0}, "entry_point": f"{s}-1-L4"} for i, s in enumerate(("FAL", "LAR"))}
    cfg = {"phases": [], "fleets": [{"id": "m1", "name": "Miner 1", "home": "LAR", "station": True, "wants": {}}],
           "fleets_migrated": True}
    obs["folded"] = True                                    # what the device sync adds for a device we know is folded
    p = lo.plan(cfg, [obs, mf], [], {}, stars, {}, set(), [], {})
    assert p["compact"] == [] and [(dl["carrier"], dl["devices"]) for dl in p["deliveries"]] == [("MF", ["OBS"])]
    # the mark: set by a refused compact / device.compacted / a compacted print, carried by the device sync, cleared on unfurl
    db, worker = client.app.state.db, client.app.state.worker
    world = client.app.state.api.http._transport.app.state.world
    code = world.devices[0]["device_code"]
    client.portal.call(remember, db, code, True, "2026-10-07T00:00:00+00:00")
    client.portal.call(worker.sync_devices)
    assert next(d for d in client.portal.call(db.kv_get, "devices") if d["device_code"] == code).get("folded") is True
    client.portal.call(worker.apply_timers, {"event": "device.unfurling", "device_code": code, "payload": {}})
    assert code not in client.portal.call(db.kv_get, FOLDED_KV)
    client.portal.call(worker.sync_devices)
    assert "folded" not in next(d for d in client.portal.call(db.kv_get, "devices") if d["device_code"] == code)
    client.portal.call(worker.apply_timers, {"event": "print.completed", "device_code": "AF",
                                             "payload": {"new_device_code": "NEW1", "compacted": True}})
    assert "NEW1" in client.portal.call(db.kv_get, FOLDED_KV)


def test_tree_files_carried_devices_under_their_carriers_system():
    """Live 2026-10-07: 13 stowed devices (no location of their own) showed as an empty '?' system."""
    from rsweb.tree import build_tree
    devs = [{"device_code": "HV", "device_type": "heaven_vessel", "location": "FAL-BELT-1", "status": "idle"},
            {"device_code": "S1", "device_type": "survey_drone", "location": None, "status": "stowed", "stowed_in_device_code": "HV"},
            {"device_code": "L1", "device_type": "ftl_slingshot", "location": None, "status": "idle"}]
    systems = {s["star"]: s for s in build_tree(devs, {}, {}, {"HV"})}
    assert systems["FAL"]["counts"]["devices"] == 2 and systems["FAL"]["nodes"][0]["children"][0]["d"]["device_code"] == "S1"
    lost = systems["?"]
    assert lost["counts"]["devices"] == 1 and "no location" in (lost["nodes"] + lost["unknown"])[0]["nowhere"]


def test_replicant_vessel_in_a_fleet_counts_toward_its_loadout():
    """Live 2026-10-08: the SOL fleet wants 1 heaven_vessel and has one (hosting a replicant); a second was printed."""
    from rsweb import loadouts as lo
    devices = [{"device_code": "E1029CB0", "device_type": "heaven_vessel", "location": "SOL-3-1", "status": "idle",
                "tags": ["fleet:sol-sector"], "operational_capacity": 100.0},
               {"device_code": "AF1", "device_type": "autofactory", "location": "SOL-3-L4", "status": "idle",
                "available_commands": ["enqueue_print"], "operational_capacity": 100.0}]
    bps = [{"device_type": "heaven_vessel", "resources": {"structural": 10}, "print_time": 60}]
    cfg = lo.normalize({"phases": [], "fleets_migrated": True, "settings": {},
                        "fleets": [{"id": "sol-sector", "name": "Sol Sector", "role": "explore", "home": "SOL", "station": True,
                                    "wants": {"heaven_vessel": 1}}]})
    p = lo.plan(cfg, devices, bps, {"SOL-3-L4": {"structural": 999}}, {}, {"E1029CB0": "D9351B81"}, set(), [], {})
    assert not p["prints"]
    row = next(r for r in p["report"]["sol-sector"]["rows"] if r["type"] == "heaven_vessel")
    assert row["have"] == 1 and row["short"] == 0 and row["riders"] == 1
    assert "E1029CB0" not in str(p.get("retag") or "") and "E1029CB0" not in (p.get("moves") or {})
    cfg["fleets"][0]["wants"] = {"heaven_vessel": 2}                 # wanting two: one more is printed
    p = lo.plan(lo.normalize(cfg), devices, bps, {"SOL-3-L4": {"structural": 999}}, {}, {"E1029CB0": "D9351B81"}, set(), [], {})
    assert p["prints"] and p["prints"][0]["n"] == 1


def test_loadouts_leave_contract_devices_alone():
    from rsweb import loadouts as lo
    cfg, devices, bps, inv, stars = _lo_world()
    devices.append({"device_code": "CS", "device_type": "survey_drone", "location": "BBB-2", "status": "idle",
                    "tags": ["contract:bbb-evt-1", "at:bbb-2"], "available_commands": ["travel"]})
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    b = {r["type"]: r for r in _rep(p, "BBB")["rows"]}
    assert b["survey_drone"]["have"] == 1                       # BS only: the contract's drone isn't the fleet's
    assert "CS" not in p["tag_add"] and "CS" not in p["moves"]  # neither joined nor made spare nor gathered


def test_bulk_check_detach_while_carrier_travels():
    from rsweb.web_bulk import bulk_check
    devices = [{"device_code": "P1", "device_type": "surge_plate", "available_commands": ["detach"], "status": "surging",
                "travel": {"destination": "BBB", "arrives_at": "2099-01-01T00:00:00+00:00"}},
               {"device_code": "P2", "device_type": "surge_plate", "available_commands": ["detach"], "status": "idle"}]
    ok, skip = bulk_check(["P1", "P2", "GONE"], "detach", devices, set(), {})
    assert ok == ["P2"] and {c for c, _ in skip} == {"P1", "GONE"}
    assert "between systems" in dict(skip)["P1"]


def test_placed_beacons_are_never_spare():
    """Live 2026-10-08: 13 working beacons (dropped by the Surveyors) carried an old spare tag — fair game for
    shortfalls and the spare depot. A fleet's placed beacon loses the tag; a fleetless one keeps it (the redundant-beacon
    pass sets it) but is never moved by the loadouts pass."""
    from rsweb import loadouts as lo
    B = lambda code, loc, tags, status="monitoring", **kw: {"device_code": code, "device_type": "ftl_beacon",  # noqa: E731
                                                           "location": loc, "status": status, "tags": tags,
                                                           "available_commands": ["deploy", "stow"], **kw}
    devices = [B("FAR", "ZALDANAL-1-L4", ["spare"]),                       # dropped in a surveyed system
               B("HOME", "AAA-5", ["fleet:hub", "spare"]),                 # the fleet's own, at its home
               B("ABOARD", None, ["fleet:hub"], status="stowed", stowed_in_device_code="V1")]
    cfg = lo.normalize({"phases": [], "fleets_migrated": True,
                        "fleets": [{"id": "hub", "name": "Hub", "role": "mining", "home": "AAA", "station": True,
                                    "wants": {"ftl_beacon": 1}},
                                   {"id": "bbb", "name": "B", "role": "mining", "home": "BBB", "station": True,
                                    "wants": {"ftl_beacon": 1}}]})
    stars = {"AAA": {"position": {"x": 0, "y": 0, "z": 0}}, "BBB": {"position": {"x": 1, "y": 0, "z": 0}},
             "ZALDANAL": {"position": {"x": 2, "y": 0, "z": 0}}}
    p = lo.plan(cfg, devices, [], {}, stars, {}, set(), [], {})
    assert "spare" in p["tag_remove"]["HOME"]
    assert "FAR" not in p["tag_remove"]       # a fleetless one keeps it (redundant: the civ beacon pass may move it)
    assert "FAR" not in p["moves"] and "HOME" not in p["moves"]          # BBB's shortfall isn't filled from them
    assert "spare" not in p["tag_add"].get("HOME", []) and "spare" not in p["tag_add"].get("ABOARD", [])
    hub = {r["type"]: r for r in _rep(p, "AAA")["rows"]}["ftl_beacon"]
    assert hub["have"] == 2                                              # the one at home and the one aboard count


def test_observatory_pass_stands_aside_while_loadouts_moves_it(client):
    """Live 2026-10-09: Miner 2's observatory was unfurled to prospect while loadouts was compacting it for ITHVALAI."""
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": "HOMEA", "position": {"x": 0, "y": 5, "z": 0}}]})
    obs = {"device_code": "OBS00002", "device_type": "galactic_observatory", "location": "HOMEA-6-L4", "status": "compacted",
           "tags": ["fleet:m2"], "available_commands": ["prospect", "compact", "unfurl"]}
    client.portal.call(db.kv_set, "devices", [obs])
    client.portal.call(eng.save_fleets, [{"id": "m2", "name": "Miner 2", "role": "mining", "home": "HOMEA", "wants": {}}])
    from rsweb.db import now_iso
    client.portal.call(db.kv_set, "loadout_moves", {"at": now_iso(), "moves": {"OBS00002": "ITHVALAI"}})
    assert client.portal.call(eng.observatory_pass) == []                    # no unfurl, no compact of its own
    assert "moving it to ITHVALAI" in client.portal.call(db.kv_get, "observatory_runs")["OBS00002"]["note"]
    client.portal.call(db.kv_set, "loadout_moves", {"at": now_iso(), "moves": {}})
    assert client.portal.call(eng.observatory_pass) == ["OBS00002: unfurling"]   # staying: back to prospecting


def test_loadout_prints_never_go_to_a_vessel():
    """Live 2026-10-09: a replenishment print went to an idle heaven vessel (it lists enqueue_print) instead of the
    system's autofactory."""
    from rsweb import loadouts as lo
    D = lambda code, t, **kw: {"device_code": code, "device_type": t, "location": "AAA-3-L4", "status": "idle", **kw}  # noqa: E731
    devices = [D("AF1", "autofactory", available_commands=["enqueue_print"], print_queue=[{"device_type": "x"}] * 3),
               D("HV2", "heaven_vessel", available_commands=["enqueue_print", "travel"])]
    cfg = lo.normalize({"phases": [], "fleets_migrated": True, "settings": {"need_stock": False},
                        "fleets": [{"id": "f", "name": "F", "role": "mining", "home": "AAA", "station": True,
                                    "wants": {"mining_drone": 2}}]})
    bps = [{"device_type": "mining_drone", "resources": {"structural": 10}, "print_time": 60},
           {"device_type": "autofactory", "queue_size": 10}]
    p = lo.plan(cfg, devices, bps, {"AAA-3-L4": {"structural": 100}}, {"AAA": {"position": {"x": 0, "y": 0, "z": 0}}},
                {}, set(), [], {})
    assert p["prints"] and all(pr["factory"] == "AF1" for pr in p["prints"])


def test_device_list_shows_a_stowed_device_where_its_carrier_is(client):
    db = client.app.state.db
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": "CARRY001", "device_type": "cargo_vessel", "location": "SOL-3-L4", "status": "idle"},
                {"device_code": "RIDER001", "device_type": "survey_drone", "location": None, "status": "stowed",
                 "stowed_in_device_code": "CARRY001"}]
    client.portal.call(db.kv_set, "devices", devices)
    page = client.get("/fleet?q=RIDER001", headers=H).text
    assert "SOL-3-L4" in page and 'in <a href="/devices/CARRY001">CARRY001</a>' in page
    assert "RIDER001" in client.get("/fleet?star=SOL&q=RIDER001", headers=H).text      # the system filter finds it


def test_relocation_only_when_the_carriers_fit_everyone(client):
    eng, db = client.app.state.worker.automations, client.app.state.db
    rider = lambda c: {"device_code": c, "device_type": "survey_drone", "location": "AAA-1", "status": "idle",  # noqa: E731
                       "tags": ["fleet:m"], "features": ["cruise", "stow"]}
    ves = {"device_code": "V", "device_type": "cargo_vessel", "location": "AAA-1", "status": "idle", "tags": ["fleet:m"],
           "features": ["surge", "cruise"], "stow_capacity": 2}
    f = {"id": "m", "name": "M", "role": "mining", "home": "BBB", "station": True, "wants": {}}

    async def commandable(devices, stars=None, without=None):
        return {"AAA", "BBB"}
    eng.commandable = commandable
    how = client.portal.call(eng.start_relocation, dict(f), "AAA", "BBB", [ves, rider("D1"), rider("D2")])
    assert "loadout pass" in how                         # a mining fleet never assembles (owner 2026-10-21): piecemeal
    f = {**f, "role": "explore"}
    how = client.portal.call(eng.start_relocation, dict(f), "AAA", "BBB", [ves, rider("D1"), rider("D2")])
    assert "relocation mission" in how                                         # 2 riders, 2 hold slots: together
    f2 = dict(f)
    how = client.portal.call(eng.start_relocation, f2, "AAA", "BBB", [ves, rider("D1"), rider("D2"), rider("D3"), rider("D4"), rider("D5"), rider("D6")])
    assert "piecemeal" in how and "mission" not in f2                         # too many riders: the loadout pass carries them


def test_attach_already_attached(client):
    from rsweb.automations import step
    eng, db = client.app.state.worker.automations, client.app.state.db
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": c, "device_type": "cargo_vessel", "location": "SOL-3", "status": "idle", "tags": t}
                for c, t in (("CAR00001", ["fleet:m3"]), ("CAR00002", ["fleet:m3"]), ("CAR00003", ["fleet:other"]))]
    devices.append({"device_code": "OBS0000A", "device_type": "observatory", "location": "SOL-3", "status": "attached"})
    client.portal.call(db.kv_set, "devices", devices)
    sent = []

    async def fake_send(method, path, body, why):
        sent.append((path, body.get("command")))
        if body.get("command") == "attach" and len([s for s in sent if s[1] == "attach"]) == 1:
            return False, None, "Target device is already attached to a carrier"
        return True, {}, None
    orig = eng.send
    eng.send = fake_send
    try:
        for on, carrier in (("CAR00001", "CAR00001"), ("CAR00001", "CAR00002"), ("CAR00003", "CAR00002"), (None, "CAR00002")):
            cached = client.portal.call(db.kv_get, "devices")
            next(d for d in cached if d["device_code"] == "OBS0000A")["attached_to_device_code"] = on
            client.portal.call(db.kv_set, "devices", cached)
            sent.clear()
            job = client.portal.call(eng.create_job, "fleets", "attach test", carrier,
                                     [step(f"{carrier}: attach OBS0000A", f"/devices/{carrier}",
                                           {"command": "attach", "device": "OBS0000A"}, critical=True)], {"devices": []}, True)
            j = next(x for x in client.portal.call(eng.jobs) if x["id"] == job["id"])
            assert j["status"] != "failed" and j["steps"][0]["status"] == "done", (on, carrier)
            if on in ("CAR00001",):   # this carrier, or another of the fleet: done
                assert sent == [(f"/devices/{carrier}", "attach")], (on, carrier)
            elif on == "CAR00003":    # a carrier outside the fleet: it detaches it, then attach again
                assert sent[:2] == [(f"/devices/{carrier}", "attach"), ("/devices/CAR00003", "detach")], (on, carrier, sent)
                assert [s["desc"].split(" ")[0] for s in j["steps"]][:2] == [f"{carrier}:", "CAR00003:"]
            else:                     # no carrier named: deploy it, then attach again
                assert sent == [(f"/devices/{carrier}", "attach"), ("/devices/OBS0000A", "deploy")], (on, carrier, sent)  # then waits
                assert [s["desc"].split(" ")[0] for s in j["steps"]] == [f"{carrier}:", "deploy", f"{carrier}:"]
    finally:
        eng.send = orig


def test_a_drone_aboard_a_carrier_at_the_destination_has_arrived(client):
    db, e = client.app.state.db, client.app.state.worker.automations
    devs = client.portal.call(db.kv_get, "devices") or []
    devs += [{"device_code": "HV000009", "device_type": "heaven_vessel", "location": "SOL-4-L4", "status": "idle"},
             {"device_code": "SD000009", "device_type": "survey_drone", "location": None, "status": "stowed",
              "stowed_in_device_code": "HV000009"}]
    client.portal.call(db.kv_set, "devices", devs)
    st = {"wait": ["travel.arrived"], "match": {"destination": "SOL-4-L4"}, "wait_device": "SD000009"}
    assert client.portal.call(e._arrived_now, st, {})


def test_loadout_carrier_room_counts_what_the_game_lists_aboard():
    """Live 2026-10-19: 89D9AB0D (4 attach points) had two haulers attached; the plan gave it 3 more."""
    from rsweb import loadouts as lo
    c = {"device_code": "C", "device_type": "surge_platform", "attach_capacity": 4, "attach_used": 4,
         "attached_devices": [{"device_code": x} for x in ("H1", "H2", "A", "B")]}
    assert lo._free(c, {}, {}) == 0
    assert lo._free(c, {}, {}, ["A", "B", "X"]) == 2          # the seats A and B already hold: X doesn't fit
    c2 = {"device_code": "V", "device_type": "cargo_vessel", "stow_capacity": 10, "attach_capacity": 2,
          "attached_devices": [{"device_code": "H1"}], "stowed_devices": [{"device_code": "S1"}]}
    assert lo._free(c2, {}, {}) == 9                          # a hold: only what's stowed takes its seats


def test_loadout_plan_fills_a_carrier_hold_with_relays_for_several_systems():
    """Owner 2026-10-20: a carrier takes as many relays as its free stowage holds, nearest drop first."""
    from rsweb import loadouts as lo
    def dev(code, t, loc, **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": "idle", "operational_capacity": 100.0,
                "features": kw.pop("features", ["cruise", "stow"]), "available_commands": ["travel", "stow", "deploy"], **kw}
    devices = [dev(f"R{t}", "ftl_relay", "HUB-1", tags=[f"to:{t.lower()}"]) for t in ("AAA", "BBB", "CCC")]
    devices.append(dev("HV", "heaven_vessel", "HUB-1", features=["surge", "cruise"], stow_capacity=3,
                       hosting_replicant={"replicant_code": "R1"}, tags=["fleet:logistics"]))
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in
             (("HUB", 0), ("AAA", 5), ("BBB", 9), ("CCC", 2))}
    cfg = {"phases": [], "systems": {}, "fleets": [{"id": "logistics", "name": "Logistics", "role": "logistics",
                                                    "home": "HUB", "wants": {}}]}
    p = lo.plan(cfg, devices, [], {}, stars, {"HV": "R1"}, set(), [], {}, commandable={"HUB"})
    dl = [d for d in p["deliveries"] if d["carrier"] == "HV"]
    assert len(dl) == 1 and sorted(dl[0]["devices"]) == ["RAAA", "RBBB", "RCCC"]   # 2 slots left after the first: all go
    assert dl[0]["to"] == "AAA" and dl[0]["stops"] == ["CCC", "BBB"]               # then nearest first: CCC 3 ly, BBB 4 ly
    devices[-1]["stow_capacity"] = 2   # room for two: the third waits for the next run
    p = lo.plan(cfg, devices, [], {}, stars, {"HV": "R1"}, set(), [], {}, commandable={"HUB"})
    assert sum(len(d["devices"]) for d in p["deliveries"] if d["carrier"] == "HV") == 2
    devices[-1].update(stow_capacity=3, replicant_code="R1")
    for d in devices[:3]:
        d["replicant_code"] = "FAB"   # printed by another replicant's factory (live 2026-10-21): handed over as they board
    p = lo.plan(cfg, devices, [], {}, stars, {"HV": "R1"}, set(), [], {}, commandable={"HUB"})
    assert sorted(x for d in p["deliveries"] if d["carrier"] == "HV" for x in d["devices"]) == ["RAAA", "RBBB", "RCCC"]


def test_a_stowable_device_can_be_attached_to_a_carrier_here(client):
    """Live 2026-10-20: a mobile fleet attached an FTL relay; the relay's own commands don't list attach."""
    world = client.app.state.api.http._transport.app.state.world
    sp = next(d for d in world.devices if d["device_type"] == "surge_plate")
    sp["available_commands"] = sp["available_commands"] + ["attach", "detach"]
    rl = next(d for d in world.devices if d["device_type"] == "ftl_relay")
    rl["location"] = sp["location"]
    client.portal.call(client.app.state.worker.sync_devices)
    assert '<option value="attach"' in client.get(f"/devices/{rl['device_code']}", headers=H).text
    form = client.get(f"/devices/{rl['device_code']}/command-form?command=attach", headers=HX).text
    assert "attach me to" in form and f'value="{sp["device_code"]}"' in form
    sent = client.get(f"/devices/{sp['device_code']}/command-form?command=attach", headers=HX).text
    assert f'value="{rl["device_code"]}"' in sent


def test_survey_carrier_ferries_members_too_far_to_cruise():
    """Live 2026-10-21: 769B37CC stalled at BELZHORII-KUIPER, ≈190 AU from BELZHORII-1; the fleet's carrier takes it."""
    from rsweb import fleets as fl
    fleet = {"id": "s", "name": "S", "role": "explore", "home": "AAA"}
    devs = [{"device_code": "HV", "device_type": "heaven_vessel", "location": "BBB-KUIPER", "status": "idle",
             "features": ["surge"], "stow_capacity": 10, "tags": ["fleet:s"]},
            {"device_code": "SC", "device_type": "ami_survey_controller", "location": "BBB-KUIPER", "status": "idle",
             "features": ["stow"], "tags": ["fleet:s"]},
            {"device_code": "SD", "device_type": "survey_drone", "location": "BBB-KUIPER", "status": "idle",
             "features": ["stow"], "tags": ["fleet:s"]}]
    steps, problems = fl.explore_work_steps(fleet, devs, "BBB-1", {"BBB-1": 1.0, "BBB-KUIPER": 190.0}, 30)
    assert not problems
    cmds = [(s_["path"], (s_["body"] or {}).get("command")) for s_ in steps]
    assert cmds[:5] == [("/devices/SC", "stow"), ("/devices/SD", "stow"), ("/devices/HV", "travel"),
                        ("/devices/SC", "deploy"), ("/devices/SD", "deploy")]
    assert steps[2]["body"]["destination"] == "BBB-1"
    assert not any(c == "travel" and p != "/devices/HV" for p, c in cmds)
    # no fleet carrier in the system: say so instead of sending them on the long cruise
    devs[0]["location"] = "CCC-1"
    steps, problems = fl.explore_work_steps(fleet, devs, "BBB-1", {"BBB-1": 1.0, "BBB-KUIPER": 190.0}, 30)
    assert not steps and "no fleet carrier" in problems[0]


def test_an_extra_relay_where_one_already_relays_is_made_spare():
    """Owner 2026-10-21: a second relay delivered to a system with a working one becomes a spare."""
    from rsweb import loadouts as lo
    def dev(code, status, **kw):
        return {"device_code": code, "device_type": "ftl_relay", "location": "AAA-1-L4", "status": status,
                "operational_capacity": 100.0, "features": ["stow"], "available_commands": ["travel", "stow", "deploy"], **kw}
    devices = [dev("R1", "relaying"), dev("R2", "inactive"), dev("R3", "inactive", tags=["to:bbb"])]
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in (("AAA", 0), ("BBB", 5))}
    p = lo.plan({"phases": [], "systems": {}, "fleets": []}, devices, [], {}, stars, {}, set(), [], {})
    assert "spare" in p["tag_add"].get("R2", set()) and "spare" not in p["tag_add"].get("R3", set())
    assert "spare" not in p["tag_add"].get("R1", set())


def test_beacon_fetch_skips_a_carrier_with_a_full_hold():
    """Live 2026-10-23: matrix container 10D7491E (1 slot, holding its replicant's matrix, towed) was picked every pass."""
    from rsweb import traffic as tr
    devs = [{"device_code": "MC", "device_type": "matrix_container", "location": "AAA-6-L4", "status": "idle",
             "features": ["cruise", "cradle"], "stow_capacity": 1, "stow_used": 1, "attached_to_device_code": "SP"},
            {"device_code": "B1", "device_type": "ftl_beacon", "location": "AAA-6-L4", "status": "monitoring",
             "tags": ["spare"], "available_commands": ["stow", "deploy"]}]
    p = tr.placement("AAA-3", devs, {}, {}, use_replicant_vessel=True)
    assert p["kind"] not in ("fetch", "carry")
    # owner 2026-10-23: the container rides on surge platform SP; the beacon goes on the platform's attach points
    devs.append({"device_code": "SP", "device_type": "surge_platform", "location": "AAA-6-L4", "status": "idle",
                 "features": ["surge", "cruise", "attach"], "attached_devices": [{"device_code": "MC"}], "attach_used": 1})
    devs[0]["hosting_replicant"] = {"replicant_code": "REP"}
    reps = {"REP": {"name": "Sk3y_17", "hosted_device_code": "MC"}}
    p = tr.placement("AAA-3", devs, reps, {}, use_replicant_vessel=True)
    assert p["kind"] == "fetch" and p["vessel"] == "SP" and p["carrier"] == "SP" and p["mode"] == "attach"
    assert "Sk3y_17" in p["text"]   # the platform with a populated container counts as a replicant aboard
    steps = tr.placement_steps(p, "AAA-3")
    assert steps[0]["path"] == "/devices/SP" and steps[0]["body"] == {"command": "attach", "device": "B1"}
    assert steps[-1]["body"] == {"command": "detach", "device": "B1"}
    assert tr.placement("AAA-3", devs, reps, {}, use_replicant_vessel=False)["kind"] not in ("fetch", "carry")
    devs.append({"device_code": "HV", "device_type": "heaven_vessel", "location": "AAA-1", "status": "idle",
                 "features": ["cruise", "surge"], "stow_capacity": 10, "stow_used": 2})
    p = tr.placement("AAA-3", devs, reps, {}, use_replicant_vessel=True)
    assert p["kind"] == "fetch" and p["vessel"] == "HV" and p["mode"] == "stow"   # no replicant moved: first


def test_a_hold_carrier_takes_beacons_for_several_systems_on_one_run():
    """Live 2026-10-23: 17 ordered beacons waited for a carrier each; they ride multi-drop runs like relays."""
    from rsweb import loadouts as lo
    def dev(code, t, loc, **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": "monitoring", "operational_capacity": 100.0,
                "features": kw.pop("features", ["stow", "comms"]), "available_commands": ["travel", "stow", "deploy"], **kw}
    devices = [dev(f"B{t}", "ftl_beacon", "HUB-BELT-1", tags=[f"to:{t.lower()}"]) for t in ("AAA", "BBB")]
    devices.append(dev("RCCC", "ftl_relay", "HUB-BELT-1", status="inactive", features=["cruise", "relay", "stow"],
                       tags=["to:ccc"]))
    devices.append(dev("HV", "heaven_vessel", "HUB-1", status="idle", features=["surge", "cruise"], stow_capacity=3,
                       hosting_replicant={"replicant_code": "R1"}, tags=["fleet:logistics"]))
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in
             (("HUB", 0), ("AAA", 5), ("BBB", 9), ("CCC", 2))}
    cfg = {"phases": [], "systems": {}, "fleets": [{"id": "logistics", "name": "Logistics", "role": "logistics",
                                                    "home": "HUB", "wants": {}}]}
    p = lo.plan(cfg, devices, [], {}, stars, {"HV": "R1"}, set(), [], {}, commandable={"HUB", "AAA", "BBB", "CCC"})
    dl = [d for d in p["deliveries"] if d["carrier"] == "HV"]
    assert len(dl) == 1 and sorted(dl[0]["devices"]) == ["BAAA", "BBBB", "RCCC"], (p["deliveries"], p["unmet"])


def test_a_surge_plate_attaches_a_beacon_it_flies_to():
    """Owner 2026-10-23: platforms and plates take cargo on their attach points, beacons included."""
    from rsweb import loadouts as lo
    def dev(code, t, loc, **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": kw.pop("status", "idle"),
                "operational_capacity": 100.0, "features": kw.pop("features", ["stow", "comms"]),
                "available_commands": kw.pop("available_commands", ["stow", "deploy"]), **kw}
    devices = [dev("BAAA", "ftl_beacon", "HUB-BELT-1", status="monitoring", tags=["to:aaa"]),
               dev("SP", "surge_plate", "HUB-BELT-1", features=["surge", "cruise", "attach"],
                   available_commands=["travel", "attach", "detach"], tags=["fleet:logistics"])]
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in (("HUB", 0), ("AAA", 5))}
    cfg = {"phases": [], "systems": {}, "fleets": [{"id": "logistics", "name": "Logistics", "role": "logistics",
                                                    "home": "HUB", "wants": {}}]}
    p = lo.plan(cfg, devices, [], {}, stars, {}, set(), [], {}, commandable={"HUB", "AAA"})
    dl = [d for d in p["deliveries"] if d["carrier"] == "SP"]
    assert dl and dl[0]["devices"] == ["BAAA"] and dl[0]["mode"] == "attach", (p["deliveries"], p["unmet"])
    descs = [s["desc"] for s in lo.delivery_steps(dl[0], {d["device_code"]: d for d in devices}, stars, False)]
    assert "SP: attach BAAA" in descs and not any(d.startswith("stow BAAA") for d in descs)


def test_a_hub_short_of_stock_takes_a_partners_extra_spares_before_printing(client):
    """Owner 2026-10-28: SOLARES printed relays for its stock while ~95 spare relays sat at FALQUORYX. The stock pass
    sends a partner hub's spares beyond its own stock over (gather + to:) and prints only what's still missing."""
    db, eng = client.app.state.db, client.app.state.worker.automations
    sent = []

    async def fake_send(method, path, body, label):
        sent.append((path, body))
        return True, {}, None
    eng.send = fake_send
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": "SOL", "position": {"x": 0, "y": 0, "z": 0}},
                                                      {"designation": "FAL", "position": {"x": 500, "y": 0, "z": 0}}]})
    client.portal.call(db.kv_set, "devices", [
        {"device_code": "F1", "device_type": "ftl_relay", "location": "FAL-BELT-1", "status": "inactive", "tags": ["spare", "stock"]},
        {"device_code": "F2", "device_type": "ftl_relay", "location": "FAL-BELT-1", "status": "inactive", "tags": ["spare"]},
        {"device_code": "F3", "device_type": "ftl_relay", "location": "FAL-BELT-1", "status": "inactive", "tags": ["spare"]}])
    client.portal.call(eng.save_fleets, [
        {"id": f, "name": f, "role": "mining", "home": h, "station": True, "materials": "self", "stock": {"ftl_relay": n}}
        for f, h, n in (("sol", "SOL", 3), ("fal", "FAL", 1))])
    out = client.portal.call(eng.stock_pass)
    assert sent == [(f"/devices/{c}", {"configuration": {"add_tags": ["gather", "stock", "to:sol"]}}) for c in ("F2", "F3")]
    assert sum("sent to keep in stock at SOL" in x for x in out) == 2


def test_stow_into_offers_attach_carriers():
    """Stow into lists carriers here with room: holds first, else attach points (up the chain); full ones are left out."""
    devs = [{"device_code": "ME", "device_type": "mining_drone", "location": "X-BELT-1", "features": ["stow"]},
            {"device_code": "HV", "device_type": "heaven_vessel", "location": "X-BELT-1", "stow_capacity": 10, "stow_used": 10},
            {"device_code": "PL", "device_type": "surge_platform", "location": "X-BELT-1", "attach_capacity": 4},
            {"device_code": "CV", "device_type": "cargo_vessel", "location": "X-BELT-1", "stow_capacity": 50},
            {"device_code": "FAR", "device_type": "cargo_vessel", "location": "Y-BELT-1", "stow_capacity": 50}]
    from rsweb import commands as c
    f = c.COMMANDS["stow"][0]
    opts = {o["value"]: o["label"] for o in c.device_options({"devices": devs, "here": "X-BELT-1"}, f, "ME")}
    assert set(opts) == {"PL", "CV"} and "attach (4 free)" in opts["PL"] and "stow (50 free)" in opts["CV"]


def test_stow_into_offers_matrix_containers_for_a_matrix():
    """A replicant matrix's Stow into lists matrix containers with an empty cradle, loose or riding on a carrier here;
    full ones are left out, and other cargo never sees a cradle."""
    devs = [{"device_code": "MX", "device_type": "replicant_matrix", "location": "X-BELT-1", "features": ["stow"]},
            {"device_code": "C1", "device_type": "matrix_container", "location": "X-BELT-1", "features": ["cruise", "cradle"],
             "stow_capacity": 1},
            {"device_code": "C2", "device_type": "matrix_container", "location": "X-BELT-1", "features": ["cruise", "cradle"],
             "stow_capacity": 1, "attached_to_device_code": "PL"},
            {"device_code": "C3", "device_type": "matrix_container", "location": "X-BELT-1", "features": ["cruise", "cradle"],
             "stow_capacity": 1, "stow_used": 1, "hosting_replicant": {"replicant_code": "R1"}},
            {"device_code": "PL", "device_type": "surge_platform", "location": "X-BELT-1", "attach_capacity": 4,
             "attach_used": 1},
            {"device_code": "ME", "device_type": "mining_drone", "location": "X-BELT-1", "features": ["stow"]}]
    from rsweb import commands as c
    f = c.COMMANDS["stow"][0]
    opts = {o["value"]: o["label"] for o in c.device_options({"devices": devs, "here": "X-BELT-1"}, f, "MX")}
    assert {"C1", "C2"} <= set(opts) and "C3" not in opts and "stow (1 free)" in opts["C1"]
    devs[0]["device_type"] = "empty_replicant_matrix"   # an empty matrix (208A0CC4, live 2026-11-11)
    opts = {o["value"] for o in c.device_options({"devices": devs, "here": "X-BELT-1"}, f, "MX")}
    assert {"C1", "C2"} <= opts
    opts = {o["value"] for o in c.device_options({"devices": devs, "here": "X-BELT-1"}, f, "ME")}
    assert not opts & {"C1", "C2", "C3"}


def test_stow_form_for_a_stowed_matrix_lists_containers_where_its_vessel_is(client):
    """A matrix stowed in a vessel has no location of its own: Stow into looks where the vessel is (SOLARES-BELT-1,
    live 2026-11-11: two empty containers there were missing)."""
    db = client.app.state.db
    cached = client.portal.call(db.kv_get, "devices")
    cached += [{"device_code": "HV0000A1", "device_type": "heaven_vessel", "location": "SOLARES-BELT-1", "status": "idle",
                "stow_capacity": 10, "stow_used": 1},
               {"device_code": "MX0000A1", "device_type": "replicant_matrix", "location": None, "status": "stowed",
                "stowed_in_device_code": "HV0000A1", "features": ["stow", "matrix"],
                "available_commands": ["deploy", "stow"]},
               {"device_code": "MC0000A1", "device_type": "matrix_container", "location": "SOLARES-BELT-1", "status": "idle",
                "stow_capacity": 1, "stow_used": 0, "features": ["cruise", "cradle"]}]
    client.portal.call(db.kv_set, "devices", cached)
    form = client.get("/devices/MX0000A1/command-form?command=stow", headers=HX).text
    assert 'value="MC0000A1"' in form


def test_stow_into_attach_carrier_sends_attach(client):
    # survey drone D8C2A140 and surge plate SP000001 are both at SOL-3: the plate has no hold, so it attaches the drone
    r = client.post("/devices/D8C2A140/command", data={"command": "stow", "f.target": "SP000001"}, headers=HX)
    assert r.status_code == 200
    row = client.portal.call(client.app.state.db.fetchone, "SELECT path, body FROM actions ORDER BY id DESC LIMIT 1")
    assert row["path"] == "/devices/SP000001" and '"attach"' in row["body"] and "D8C2A140" in row["body"]
