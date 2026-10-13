from rsweb import logistics as lg


def cargo(run):
    return [c["code"] for c in run["cargos"]]
from test_ops import H, HX, client, eng  # noqa: F401  (fixture)

SURGE = {"features": ["surge"], "available_commands": ["travel", "collect_resources", "deposit_resources"]}
POS = {"A": {"x": 0, "y": 0, "z": 0}, "B": {"x": 5, "y": 0, "z": 0}, "Z": {"x": 60, "y": 0, "z": 0}}
BPS = {"mining_drone": {"device_type": "mining_drone", "resources": {"carbon": 40}}}
FLEETS = [{"id": "cab", "name": "Cab", "role": "logistics", "home": "A"},
          {"id": "home", "name": "Home", "role": "mining", "home": "A", "station": True, "materials": "self"}]


def dev(code, t, loc, **kw):
    return {"device_code": code, "device_type": t, "location": loc, "status": kw.pop("status", "idle"),
            "tags": kw.pop("tags", ["fleet:cab"]), **kw}


def world(extra=()):
    devs = [dev("F1", "cargo_freighter", "A-1", cargo_capacity=100, **SURGE),
            dev("HV", "heaven_vessel", "A-1", **SURGE),
            dev("AF", "autofactory", "B-2", tags=[], status="waiting_for_resources",
                print_queue=[{"device_type": "mining_drone", "quantity": 1}],
                printing={"device_type": "mining_drone"}),
            *extra]
    return devs


def run_plan(devs, inv, contracts=(), commandable=frozenset({"A", "B"}), prefs=None):
    return lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory=inv, pos=POS, commandable=set(commandable),
                   hosts={"HV": "R1"}, busy=set(), reach=100, contracts=list(contracts), trades=[], incoming={},
                   replicants={"R1": {"location": "A-1"}}, drop=lambda s: f"{s}-1", prefs=prefs)


def test_prints_come_first_and_home_prints_keep_their_reserve():
    devs = world([dev("AH", "autofactory", "A-1", tags=[], status="idle",
                      print_queue=[{"device_type": "mining_drone", "quantity": 5}])])
    inv = {"A-1": {"carbon": 230}}       # A's own queue keeps 200: 30 to spare
    p = run_plan(devs, inv)
    assert p["requests"][0]["id"] == "print:B-2" and p["requests"][0]["tier"] == 1 and p["requests"][0]["waiting"]
    assert p["reserve"]["A"] == {"carbon": 200}
    run = next(r for r in p["runs"] if r["req"] == "print:B-2")
    # 30 spare, plus 50 of A's reserve: A still has 3 prints paid (30 min) against B's 2 (20 min) — equally busy
    assert cargo(run) == ["F1"] and run["carry"] == {"carbon": 80} and not run["escort"]
    steps = lg.run_steps(run, {d["device_code"]: d for d in devs})
    assert [s["body"]["command"] for s in steps if s["body"]] == ["collect_resources", "travel", "deposit_resources"]


def test_outside_the_network_a_replicant_flies_alongside_fulfills_and_both_come_back():
    devs = world()
    devs[2]["print_queue"], devs[2]["status"], devs[2]["printing"] = [], "idle", None
    inv = {"A-1": {"carbon": 500}}
    c = {"designation": "EV1", "location": "Z-3", "title": "Help", "price": {"carbon": 50}}
    p = run_plan(devs, inv, [c])
    run = next(r for r in p["runs"] if r["req"] == "contract:EV1")
    assert cargo(run) == ["F1"] and run["escort"] == "HV" and run["replicant"] == "R1" and run["fulfil"]
    assert run["back"] == "A-1"            # waits in the network afterwards (its fleet's home)
    steps = lg.run_steps(run, {d["device_code"]: d for d in devs})
    descs = [s["desc"] for s in steps]
    i_dep = next(i for i, d in enumerate(descs) if "deposit" in d)
    assert any("HV → Z-3" in d for d in descs[:i_dep]) and "fulfill" in descs[i_dep + 1]
    assert descs.index("F1 → Z-3") < descs.index("HV → Z-3")          # cargo sent first, while the escort is with it
    assert any("→ A-1" in d for d in descs[i_dep + 1:])
    # no replicant logistics device free: nothing goes
    p2 = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory=inv, pos=POS, commandable={"A", "B"}, hosts={"HV": "R1"},
                 busy={"HV"}, reach=100, contracts=[c], trades=[], incoming={}, replicants={}, drop=lambda s: f"{s}-1")
    assert not [r for r in p2["runs"] if r["req"] == "contract:EV1"]
    assert any(u["req"] == "contract:EV1" and "outside an active relay network" in u["why"] for u in p2["unmet"])


def test_stray_piles_return_to_the_gather_point_last_and_skip_works():
    devs = world()
    devs[2]["print_queue"], devs[2]["status"], devs[2]["printing"] = [], "idle", None
    inv = {"B-BELT": {"silicates": 80}}
    p = run_plan(devs, inv)
    r = next(x for x in p["requests"] if x["kind"] == "return")
    assert r["tier"] == 4 and r["to"] == "A-1"
    assert any(x["req"] == "return:B-BELT" and cargo(x) == ["F1"] for x in p["runs"])
    p = run_plan(devs, inv, prefs={"skip": ["return:B-BELT"]})
    assert not any(x["req"] == "return:B-BELT" for x in p["runs"])


def test_logistics_page_and_trade_approval(client):  # noqa: F811
    assert client.post("/shop/approve", data={"controller": "C1", "trade_code": "T1", "on": "on"},
                       headers=HX).status_code == 200
    ok = client.portal.call(client.app.state.db.kv_get, "trade_approvals")
    assert "C1:T1" in ok["trades"]
    client.post("/shop/approve", data={"controller": "C1", "trade_code": "T1"}, headers=HX)
    assert "C1:T1" not in client.portal.call(client.app.state.db.kv_get, "trade_approvals")["trades"]
    client.post("/fleets/logistics/run", headers=HX)
    page = client.get("/fleets/logistics", headers=H).text
    assert "Logistics" in page and "No logistics fleet yet" in page


def test_logistics_pass_sends_a_job_for_a_waiting_print(client):  # noqa: F811
    db, e = client.app.state.db, eng(client)
    cat = client.portal.call(db.kv_get, "stars") or {"stars": []}
    cat["stars"] += [{"designation": d, "position": p} for d, p in POS.items()]
    client.portal.call(db.kv_set, "stars", cat)
    client.portal.call(db.kv_set, "blueprints", list(BPS.values()))
    client.portal.call(db.kv_set, "devices", world() + [dev("RL", "ftl_relay", "A-4-L4", tags=[], status="relaying")])
    client.portal.call(db.kv_set, "inventory", [{"location": "A-1", "items": {"carbon": 300}}])
    client.portal.call(db.kv_set, "replicants", {"R1": {"location": "A-1", "hosted_device_code": "HV"}})
    items = client.portal.call(e.fleets)
    client.portal.call(e.save_fleets, items + [{"id": "cab", "name": "Cab", "role": "logistics", "home": "A", "wants": {}}])
    out = client.portal.call(e.logistics_pass, True)
    assert out and "prints at B-2" in out[0]
    job = next(j for j in client.portal.call(e.jobs) if j["rule"] == "logistics")
    assert job["meta"]["logistics"]["to"] == "B-2" and job["meta"]["logistics"]["carry"] == {"carbon": 80}
    page = client.get("/fleets/logistics", headers=H).text
    assert "prints at B-2" in page and "F1" in page
    fpage = client.get("/fleets", headers=H)
    assert fpage.status_code == 200 and "Cab" in fpage.text
    r = client.post("/fleets/cab/mission", data={"targets": "B"}, headers=HX)
    assert "takes no missions" in r.text or r.status_code in (404, 405)


def test_a_hauler_is_towed_for_a_long_range_run():
    devs = [dev("HL", "transport_hauler", "A-1", cargo_capacity=80, available_commands=["travel", "collect_resources"]),
            dev("PL", "surge_platform", "A-1", **SURGE),
            dev("AF", "autofactory", "Z-2", tags=[], status="waiting_for_resources",
                print_queue=[{"device_type": "mining_drone", "quantity": 1}])]
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"A-1": {"carbon": 300}}, pos=POS, commandable={"A", "B", "Z"},
                hosts={}, busy=set(), reach=100, contracts=[], trades=[], incoming={}, replicants={}, drop=lambda s: f"{s}-1")
    run = next(r for r in p["runs"] if r["req"] == "print:Z-2")
    assert cargo(run) == ["HL"] and run["ride"] == "PL" and run["carry"] == {"carbon": 40}
    cmds = [(s["path"].split("/")[-1], (s["body"] or {}).get("command")) for s in lg.run_steps(run, {d["device_code"]: d for d in devs})
            if s["body"]]
    assert cmds == [("HL", "collect_resources"), ("PL", "attach"), ("PL", "travel"), ("PL", "detach"), ("HL", "deposit_resources")]


def test_a_contract_is_approved_alone_or_pre_approved_by_its_civilization():
    import asyncio
    from rsweb import gameevents as gev
    e = {"designation": "EV9", "location": "Z-3", "species": "Glorp"}
    run = lambda ap: asyncio.run(gev.approved(None, e, {}, ap))  # noqa: E731
    assert not run({}) and run({"contract:EV9": {}}) and run({"species:glorp": {}}) and not run({"contract:EV8": {}})


def test_trade_fleets_retire_into_logistics_fleets_once_their_run_ends(client):  # noqa: F811
    e = eng(client)
    client.portal.call(e.save_fleets, [
        {"id": "t", "name": "T", "role": "trade", "home": "SOL", "wants": {}, "auto_contracts": True},
        {"id": "u", "name": "U", "role": "trade", "home": "SOL", "wants": {}, "mission": {"status": "running", "trade": {"x": 1}}}])
    assert client.portal.call(e.auto_deals_pass) == ["T: trade fleet is now a logistics fleet (trade fleets are retired)"]
    roles = {f["id"]: f["role"] for f in client.portal.call(e.fleets)}
    assert roles == {"t": "logistics", "u": "trade"}                          # U finishes its run first
    assert "trade" not in __import__("rsweb.fleets", fromlist=["ROLES"]).ROLES


def test_a_fleet_carrier_with_a_nested_matrix_escorts_and_counts_its_replicant():
    from rsweb import relaynet
    devs = [dev("FC", "mobile_fleet", "A-1", **SURGE),
            dev("MC", "matrix_container", None, attached_to_device_code="FC"),
            dev("RM", "replicant_matrix", None, stowed_in_device_code="MC"),
            dev("F1", "cargo_freighter", "A-1", cargo_capacity=100, **SURGE)]
    assert lg.riding_hosts(devs, {"MC": "R1"}) == {"MC": "R1", "FC": "R1"}
    assert relaynet.replicant_stars({"R1": {"hosted_device_code": "MC"}}, devs) == {"A"}
    c = {"designation": "EV1", "location": "Z-3", "title": "Help", "price": {"carbon": 50}}
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"A-1": {"carbon": 500}}, pos=POS, commandable={"A", "B"},
                hosts={"MC": "R1"}, busy=set(), reach=100, contracts=[c], trades=[], incoming={}, replicants={},
                drop=lambda s: f"{s}-1")
    run = next(r for r in p["runs"] if r["req"] == "contract:EV1")
    assert cargo(run) == ["F1"] and not run["ride"] and run["escort"] == "FC" and run["replicant"] == "R1" and run["fulfil"]
    steps = lg.run_steps(run, {d["device_code"]: d for d in devs})
    assert any(s["path"] == "/devices/FC" and (s["body"] or {}).get("command") == "travel" for s in steps)   # flies itself


def test_in_system_deliveries_go_to_a_free_ami_transport_controller():
    devs = world()
    devs[2]["print_queue"], devs[2]["status"], devs[2]["printing"] = [], "idle", None
    c = {"designation": "EV2", "location": "B-3", "title": "Local", "price": {"carbon": 50}}
    ctrl = dev("TC", "ami_transport_controller", "B-1", tags=[])
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"B-BELT": {"carbon": 500}}, pos=POS, commandable={"A", "B"},
                hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[c], trades=[], incoming={}, replicants={},
                drop=lambda s: f"{s}-1", ami=[ctrl])
    run = next(r for r in p["runs"] if r["req"] == "contract:EV2")
    assert run["ami"] == "TC" and run["carry"] == {"carbon": 50} and not run["cargos"]
    st = lg.run_steps(run, {})
    assert st[0]["body"]["directive"] == "delivery" and st[0]["body"]["configuration"]["route"] == {"collect": "B-BELT", "deliver": "B-3"}
    assert st[1]["body"] == {"command": "launch"}


def test_a_big_load_goes_as_one_convoy_with_one_escort_smallest_fitting_ship_first():
    devs = [dev("F1", "cargo_freighter", "A-1", cargo_capacity=100, **SURGE),
            dev("F2", "cargo_freighter", "A-1", cargo_capacity=100, **SURGE),
            dev("F9", "cargo_freighter", "A-1", cargo_capacity=500, **SURGE),
            dev("HV", "heaven_vessel", "A-1", **SURGE)]
    c = {"designation": "EV1", "location": "Z-3", "title": "Big", "price": {"carbon": 180}}
    p = run_plan(devs, {"A-1": {"carbon": 1000}}, [c])
    run = next(r for r in p["runs"] if r["req"] == "contract:EV1")
    assert cargo(run) == ["F9"] and run["escort"] == "HV"            # one ship that takes it all
    c["price"] = {"carbon": 800}
    p = run_plan(devs, {"A-1": {"carbon": 1000}}, [c])
    run = next(r for r in p["runs"] if r["req"] == "contract:EV1")
    assert sorted(cargo(run)) == ["F1", "F2", "F9"] and run["escort"] == "HV" and run["carry"] == {"carbon": 700}
    steps = lg.run_steps(run, {d["device_code"]: d for d in devs})
    sends = [s["desc"] for s in steps if s["body"] and s["body"].get("command") == "travel" or "/replicants/" in s["path"]]
    z = [x for x in sends if "→ Z-3" in x]
    assert len(z) == 4 and z[-1].startswith("HV → Z-3")             # all together, escort last
    assert any("HV → A-1" in x for x in sends) and any("F9 → A-1" in x for x in sends)   # and back to wait in the network


def test_haulers_ride_a_free_carrier_freighters_fly_themselves_and_loaded_holds_go_first():
    hl = dict(cargo_capacity=80, available_commands=["travel", "collect_resources"])
    devs = [dev("H1", "transport_hauler", "A-1", **hl), dev("H2", "transport_hauler", "A-1", **hl),
            dev("F1", "cargo_freighter", "A-2", cargo_capacity=60, cargo={"carbon": 60}, cargo_used=60, **SURGE),
            dev("FC", "mobile_fleet", "A-2", **SURGE)]
    c = {"designation": "EV1", "location": "B-3", "title": "Near", "price": {"carbon": 150}}
    p = run_plan(devs, {"A-1": {"carbon": 1000}}, [c])
    run = next(r for r in p["runs"] if r["req"] == "contract:EV1")
    by = {x["code"]: x for x in run["cargos"]}
    assert list(by)[0] == "F1" and by["F1"]["carry"] == {"carbon": 60} and not by["F1"]["legs"] and not by["F1"]["rides"]
    assert run["ride"] == "FC" and by["H1"]["rides"] and run["carry"] == {"carbon": 150}   # freighters can't be attached
    cmds = [(s["path"].split("/")[-1], s["body"].get("command")) for s in lg.run_steps(run, {d["device_code"]: d for d in devs})
            if s["body"]]
    assert ("FC", "attach") in cmds and ("FC", "detach") in cmds and ("F1", "travel") in cmds
    assert ("F1", "collect_resources") not in cmds and ("F1", "deposit_resources") in cmds

def test_a_moved_fleets_old_stockpile_follows_it_to_the_new_home():
    devs = world()
    devs[2]["print_queue"], devs[2]["status"], devs[2]["printing"] = [], "idle", None
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"B-BELT": {"carbon": 90}}, pos=POS, commandable={"A", "B"},
                hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[], trades=[], incoming={}, replicants={},
                drop=lambda s: f"{s}-1", moved={"B": "Z"})
    r = next(x for x in p["requests"] if x["id"] == "return:B-BELT")
    assert r["to"] == "Z-1" and "new home" in r["label"]


def test_relay_range_comes_from_the_blueprint():
    from rsweb import relaynet
    pos = {"A": {"x": 0, "y": 0, "z": 0}, "B": {"x": 12, "y": 0, "z": 0}}
    devs = [dev("R1", "ftl_relay", "A-4-L4", status="relaying"), dev("R2", "ftl_relay", "B-4-L4", status="relaying")]
    assert len(relaynet.networks(devs, pos)["networks"]) == 2
    net = relaynet.networks(devs, pos, {"ftl_relay": {"relay_range_ly": 15}})
    assert len(net["networks"]) == 1 and net["hop"] == 15


def _rescue_plan(devs, dark=None, away=frozenset(), hosts=None):
    pos = {**POS, "Y": {"x": 40, "y": 0, "z": 0}}
    return lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={}, pos=pos, commandable={"A", "B"},
                   hosts=hosts if hosts is not None else {"HV": "R1"}, busy=set(), reach=100, contracts=[], trades=[],
                   incoming={}, replicants={}, drop=lambda s: f"{s}-1", dark_nets=dark or [], away_fleets=set(away))


def test_a_stranded_device_that_can_surge_is_commanded_home_from_its_dark_network():
    devs = [dev("HV", "heaven_vessel", "A-1", **SURGE),
            dev("SV", "surge_platform", "Z-5", tags=["fleet:home"], **SURGE),
            dev("RL", "ftl_relay", "Z-4-L4", tags=[], status="relaying"),             # placed: stays
            dev("MX", "mining_drone", "Y-2", tags=["fleet:mission"])]                    # its fleet is out on purpose
    p = _rescue_plan(devs, dark=[{"Z", "Y"}], away={"fleet:mission"})
    run = next(r for r in p["runs"] if r["req"] == "rescue:SV")
    assert run["mode"] == "self" and run["escort"] == "HV" and run["point"] == "Y-1" and run["to"] == "A-1"   # nearest dark spot
    assert not any(r["req"] in ("rescue:RL", "rescue:MX") for r in p["runs"])
    steps = lg.run_steps(run, {d["device_code"]: d for d in devs})
    sends = [x["desc"] for x in steps if x["body"]]
    assert sends[0].startswith("HV → Y-1") and sends[1].startswith("SV → A-1") and sends[-1].startswith("HV → A-1")


def test_a_stranded_drone_is_picked_up_by_a_carrier_with_a_replicant_along():
    devs = [dev("HV", "heaven_vessel", "A-1", **SURGE),
            dev("PL", "surge_platform", "A-1", **SURGE),
            dev("TD", "transport_drone", "Z-5", tags=[], in_control_range=False, available_commands=["travel"])]
    p = _rescue_plan(devs)
    run = next(r for r in p["runs"] if r["req"] == "rescue:TD")
    assert run["mode"] == "attach" and run["ride"] == "PL" and run["escort"] == "HV" and run["to"] == "A-1"
    cmds = [(x["path"].split("/")[-1], x["body"].get("command")) for x in lg.run_steps(run, {d["device_code"]: d for d in devs})
            if x["body"]]
    assert cmds == [("PL", "travel"), ("travel", None), ("PL", "attach"), ("PL", "travel"), ("travel", None), ("PL", "detach")]   # HV goes by its replicant


def test_home_piles_are_consolidated_at_the_drop_point_unless_the_consolidate_rule_has_the_system():
    devs = world()
    devs[2]["print_queue"], devs[2]["status"], devs[2]["printing"] = [], "idle", None
    inv = {"A-1": {"carbon": 10}, "A-BELT": {"carbon": 70}}
    kw = dict(fleets=FLEETS, devices=devs, bps=BPS, inventory=inv, pos=POS, commandable={"A", "B"}, hosts={"HV": "R1"},
              busy=set(), reach=100, contracts=[], trades=[], incoming={}, replicants={}, drop=lambda s: f"{s}-1")
    p = lg.plan(**kw)
    run = next(r for r in p["runs"] if r["req"] == "consolidate:A-BELT")
    assert cargo(run) == ["F1"] and run["to"] == "A-1" and run["carry"] == {"carbon": 70}
    p = lg.plan(**kw, ami=[dev("TC", "ami_transport_controller", "A-2", tags=[])])
    assert next(r for r in p["runs"] if r["req"] == "consolidate:A-BELT")["ami"] == "TC"
    p = lg.plan(**kw, no_consolidate={"A"})
    assert not any(r["id"].startswith("consolidate:") for r in p["requests"])


def test_piles_beyond_the_network_are_used_with_an_escort_and_unreturnable_piles_are_shown():
    devs = world()
    devs[2]["print_queue"], devs[2]["status"], devs[2]["printing"] = [], "idle", None
    c = {"designation": "EV3", "location": "B-3", "title": "Far stock", "price": {"carbon": 50}}
    fleets = [f for f in FLEETS if f["id"] == "cab"]   # no gather point
    kw = dict(fleets=fleets, devices=devs, bps=BPS, inventory={"Z-BELT": {"carbon": 90}}, pos=POS, commandable={"A", "B"},
              hosts={"HV": "R1"}, incoming={}, replicants={}, drop=lambda s: f"{s}-1", contracts=[c], trades=[], reach=100)
    p = lg.plan(**kw, busy=set())
    run = next(r for r in p["runs"] if r["req"] == "contract:EV3")
    assert cargo(run) == ["F1"] and run["escort"] == "HV" and run["escort_pickup"]
    descs = [x["desc"] for x in lg.run_steps(run, {d["device_code"]: d for d in devs}) if x["body"]]
    assert descs.index("HV → Z-BELT (pick up)") < next(i for i, d in enumerate(descs) if "load" in d)
    p = lg.plan(**kw, busy={"HV"})                       # no replicant free: the far pile isn't touched
    assert not any(r["req"] == "contract:EV3" for r in p["runs"])
    p = lg.plan(**{**kw, "contracts": []}, busy=set())
    assert any(u["req"] == "return:Z-BELT" and "no gather point" in u["why"] for u in p["unmet"])


def test_a_to_tag_naming_a_location_becomes_system_plus_pin():
    from rsweb import loadouts as lo
    stars = {"SOL", "FALQUORYX"}
    assert lo.place_tag_fix({"tags": ["to:sol-3"]}, stars, set()) == (["to:sol", "at:sol-3"], ["to:sol-3"])
    assert lo.place_tag_fix({"tags": ["to:sol"]}, stars, set()) is None
    assert lo.place_tag_fix({"tags": ["to:sol-printing-hub"]}, stars, {"sol-printing-hub"}) is None   # a fleet id
    assert lo.place_tag_fix({"tags": ["to:sol-3", "at:sol-4"]}, stars, set()) == (["to:sol"], ["to:sol-3"])   # its pin wins


def test_engine_rewrites_location_to_tags(client):  # noqa: F811
    db, e = client.app.state.db, client.app.state.worker.automations
    devs = client.portal.call(db.kv_get, "devices") + [dev("TT", "mining_drone", "SOL-BELT-1", tags=["to:sol-3"])]
    client.portal.call(db.kv_set, "devices", devs)
    out = client.portal.call(e.place_tag_pass)
    assert out == ["TT: to:sol-3 read as to:sol + at:sol-3"]
    job = next(j for j in client.portal.call(e.jobs) if j["rule"] == "tags")
    conf = job["steps"][0]["body"]["configuration"]
    assert conf["add_tags"] == ["to:sol", "at:sol-3"] and conf["remove_tags"] == ["to:sol-3"]


def test_a_device_stowed_in_a_carrier_is_delivered_by_that_carrier_from_where_it_is():
    from rsweb import loadouts as lo
    from test_core import _lo_world
    cfg, devices, bps, inv, stars = _lo_world()
    devices.append({"device_code": "BC", "device_type": "ftl_beacon", "location": None, "status": "stowed",
                    "stowed_in_device_code": "CAR", "tags": ["to:bbb"], "features": ["stow"], "available_commands": ["deploy"]})
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {"CAR": ["BC"]})
    dl = next(x for x in p["deliveries"] if "BC" in x["devices"])
    assert dl["carrier"] == "CAR" and dl["from"] == "AAA" and not dl.get("fetch_from")
    steps = lo.delivery_steps(dl, p["by_code"], stars, True)
    descs = [x["desc"] for x in steps]
    assert not any("stow BC" in x for x in descs) and not any(x.startswith("CAR →  ") for x in descs)
    assert any("CAR → BBB-5-L4" in x for x in descs)


def test_a_device_riding_another_carrier_is_released_for_the_one_delivering_it():
    from rsweb import loadouts as lo
    by = {"MD": {"device_code": "MD", "device_type": "mining_drone", "location": None, "attached_to_device_code": "PL",
                 "tags": ["to:bbb"], "available_commands": ["travel", "stow"], "features": ["cruise", "stow"]},
          "PL": {"device_code": "PL", "device_type": "surge_plate", "location": "AAA-OORT"},
          "BX": {"device_code": "BX", "device_type": "ftl_beacon", "location": None, "stowed_in_device_code": "HV2",
                 "tags": ["to:bbb"], "available_commands": ["deploy"]},
          "HV2": {"device_code": "HV2", "device_type": "cargo_vessel", "location": "AAA-OORT"},
          "CAR": {"device_code": "CAR", "device_type": "surge_carrier", "location": "AAA-OORT"}}
    stars = {"BBB": {"entry_point": "BBB-5-L4"}}
    dl = {"carrier": "CAR", "carrier_loc": "AAA-OORT", "from": "AAA", "to": "BBB", "devices": ["MD", "BX"], "mode": "stow"}
    descs = [s["desc"] for s in lo.delivery_steps(dl, by, stars, True)]
    assert descs.index("PL: detach MD (for CAR)") < descs.index("stow MD in CAR")
    assert descs.index("BX: deploy out of HV2") < descs.index("stow BX in CAR")


def test_a_print_draws_on_its_own_systems_reserve_but_others_dont():
    devs = world([dev("AH", "autofactory", "A-1", tags=[], status="waiting_for_resources",
                      print_queue=[{"device_type": "mining_drone", "quantity": 4}])])
    devs[2]["print_queue"], devs[2]["status"], devs[2]["printing"] = [], "idle", None
    c = {"designation": "EVX", "location": "B-3", "title": "Wants carbon", "price": {"carbon": 100}}
    p = run_plan(devs, {"A-BELT": {"carbon": 200}}, [c])
    run = next(r for r in p["runs"] if r["req"] == "print:A-1")
    assert run["carry"] == {"carbon": 100} and run["to"] == "A-1"          # A's prints use A's stock (F1 holds 100)
    assert not any(r["req"] == "contract:EVX" for r in p["runs"])            # the reserve doesn't leave A


def test_blueprints_page_filters_by_name_and_feature(client):  # noqa: F811
    db = client.app.state.db
    client.portal.call(db.kv_set, "blueprints", [{"device_type": "cargo_freighter", "features": ["surge", "transport"]},
                                                 {"device_type": "mining_drone", "features": ["mine", "cruise"]}])
    page = client.get("/blueprints?feature=mine", headers=H).text
    assert 'id="bp-q"' in page and "<option selected>mine</option>" in page and 'data-features="surge transport"' in page


def test_hubs_share_reserves_to_stay_equally_busy():
    pos = {"FAL": {"x": 0, "y": 0, "z": 0}, "SOL": {"x": 500, "y": 0, "z": 0}}
    fleets = [{"id": "cab", "name": "Cab", "role": "logistics", "home": "FAL"},
              {"id": "hub", "role": "mining", "home": "FAL", "station": True, "materials": "self"},
              {"id": "sol", "role": "mining", "home": "SOL", "station": True, "materials": "self"}]
    bps = {"mining_drone": {"device_type": "mining_drone", "resources": {"carbon": 40}, "print_time": 600}}

    def world_(fal_q, sol_q):
        return [dev("F1", "cargo_freighter", "FAL-1", cargo_capacity=2000, **SURGE), dev("HV", "heaven_vessel", "FAL-1", **SURGE),
                dev("AF", "autofactory", "FAL-1", tags=[], status="idle", print_queue=[{"device_type": "mining_drone", "quantity": fal_q}]),
                dev("AS", "autofactory", "SOL-2", tags=[], status="waiting_for_resources",
                    printing={"device_type": "mining_drone"}, print_queue=[{"device_type": "mining_drone", "quantity": sol_q}])]

    def sent(fal_q, sol_q, carbon):
        p = lg.plan(fleets=fleets, devices=world_(fal_q, sol_q), bps=bps, inventory={"FAL-1": {"carbon": carbon}}, pos=pos,
                    commandable={"FAL", "SOL"}, hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[], trades=[],
                    incoming={}, replicants={}, drop=lambda s: f"{s}-1")
        return sum(r["carry"].get("carbon", 0) for r in p["runs"] if r["req"] == "print:SOL-2")
    assert sent(30, 5, 1000) == 240        # FAL keeps 19 prints' worth (3.2 h), more than SOL's whole queue (6 prints, 1 h)
    assert sent(30, 30, 1000) == 480       # both queues long: 12 prints each (2 h), the odd one stays home
    assert sent(10, 30, 400) == 200        # FAL's own queue takes all 400; even split: 5 prints (50 min) each
