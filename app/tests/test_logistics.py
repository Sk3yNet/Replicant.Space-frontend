from datetime import datetime, timedelta, timezone

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


def test_a_contract_covered_only_by_deliveries_on_their_way_is_not_fulfilled_yet():
    devs = world()
    c = {"designation": "EV2", "location": "B-2", "title": "Boom", "price": {"carbon": 400}}
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"B-2": {"carbon": 69}}, pos=POS, commandable={"A", "B"},
                hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[c], trades=[], incoming={"B-2": {"carbon": 331}},
                replicants={"R1": {"location": "A-1"}}, drop=lambda s: f"{s}-1")
    assert not [r for r in p["runs"] if r["req"] == "contract:EV2"]
    assert any(u["req"] == "contract:EV2" and "331 carbon on its way" in u["why"] for u in p["unmet"])


def test_a_stocked_contract_already_being_fulfilled_gets_no_second_replicant():
    """Live 2026-10-25: three replicant carriers left for Geothermal Surplus at ELUSORYR-3 within two hours."""
    devs = world()
    c = {"designation": "EV3", "location": "B-2", "title": "Geo", "price": {"carbon": 50}}
    assert any(r["req"] == "contract:EV3" and r["fulfil"] for r in run_plan(devs, {"B-2": {"carbon": 50}}, [c])["runs"])
    p = run_plan(devs, {"B-2": {"carbon": 50}}, [{**c, "fulfilling": True}])
    assert not [r for r in p["runs"] if r["req"] == "contract:EV3"]
    assert any(u["req"] == "contract:EV3" and "on its way to fulfill" in u["why"] for u in p["unmet"])
    assert not any(r["id"] == "return:B-2" for r in p["requests"])   # its price stays put


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
    assert [s.get("need_comms") for s in steps if s["body"]] == [None, "B", None]   # checked again before leaving


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
    # an AMI ferry already empties B: no return trip there
    ferry = {"device_code": "TC000001", "device_type": "ami_transport_controller", "location": "B-2-L4",
             "status": "coordinating", "tags": ["ferry"]}
    p = run_plan(devs + [ferry], inv)
    assert not any(x["id"] == "return:B-BELT" for x in p["requests"])


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
    client.portal.call(db.kv_set, "devices", world() + [dev("RL", "ftl_relay", "A-4-L4", tags=[], status="relaying"),
                                                     dev("RL2", "ftl_relay", "B-4-L4", tags=[], status="relaying")])
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
    fpage = client.get("/fleets/details?all=1", headers=H)
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


def test_a_plate_fetches_a_hauler_from_another_system_for_a_run():
    devs = [dev("HL", "transport_hauler", "B-1", cargo_capacity=80, available_commands=["travel", "collect_resources"]),
            dev("PL", "surge_plate", "A-1", **SURGE),
            dev("AF", "autofactory", "Z-2", tags=[], status="waiting_for_resources",
                print_queue=[{"device_type": "mining_drone", "quantity": 1}])]
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"A-1": {"carbon": 300}}, pos=POS, commandable={"A", "B", "Z"},
                hosts={}, busy=set(), reach=100, contracts=[], trades=[], incoming={}, replicants={}, drop=lambda s: f"{s}-1")
    run = next(r for r in p["runs"] if r["req"] == "print:Z-2")
    assert cargo(run) == ["HL"] and run["ride"] == "PL" and run["carry"] == {"carbon": 40}
    steps = [s for s in lg.run_steps(run, {d["device_code"]: d for d in devs}) if s["body"]]
    cmds = [(s["path"].split("/")[-1], s["body"].get("command"), s["body"].get("destination")) for s in steps]
    assert cmds == [("PL", "travel", "B-1"), ("PL", "attach", None), ("PL", "travel", "A-1"), ("PL", "detach", None),
                    ("HL", "collect_resources", None), ("PL", "attach", None), ("PL", "travel", "Z-2"),
                    ("PL", "detach", None), ("HL", "deposit_resources", None)]


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
    # the engine passes the riding hosts: the carrier still flies by its own travel, not the replicant's (that moves the
    # towed container, which can't surge: live 2026-10-20)
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"A-1": {"carbon": 500}}, pos=POS, commandable={"A", "B"},
                hosts=lg.riding_hosts(devs, {"MC": "R1"}), busy=set(), reach=100, contracts=[c], trades=[], incoming={},
                replicants={}, drop=lambda s: f"{s}-1")
    run = next(r for r in p["runs"] if r["req"] == "contract:EV1")
    assert run["escort"] == "FC" and "FC" not in run["direct"]
    assert not any("/replicants/" in st["path"] for st in lg.run_steps(run, {d["device_code"]: d for d in devs}))
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


def test_ami_delivery_requirement_is_the_level_the_destination_should_hold():
    # live 2026-10-28: SOLARES-4-L4 held 69/158; a requirement of the 57/130 shortfall completed at once, moving nothing
    devs = world()
    devs[2]["print_queue"], devs[2]["status"], devs[2]["printing"] = [], "idle", None
    c = {"designation": "EV2", "location": "B-3", "title": "Local", "price": {"carbon": 50}}
    ctrl = dev("TC", "ami_transport_controller", "B-1", tags=[])
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"B-BELT": {"carbon": 500}, "B-3": {"carbon": 20}}, pos=POS,
                commandable={"A", "B"}, hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[c], trades=[], incoming={},
                replicants={}, drop=lambda s: f"{s}-1", ami=[ctrl])
    run = next(r for r in p["runs"] if r["req"] == "contract:EV2")
    assert run["carry"] == {"carbon": 30}
    assert lg.run_steps(run, {})[0]["body"]["configuration"]["requirement"] == {"carbon": 50}


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


def test_a_device_pinned_where_it_is_is_not_rescued():
    devs = [dev("HV", "heaven_vessel", "A-1", **SURGE),
            dev("PL", "surge_platform", "A-1", **SURGE),
            dev("CC", "compute_core", "Z-5", tags=["at:z-5"], in_control_range=False, available_commands=["travel"]),
            dev("TD", "transport_drone", "Z-5", tags=["at:a-1"], in_control_range=False, available_commands=["travel"])]
    p = _rescue_plan(devs)
    assert not any(r["id"] == "rescue:CC" for r in p["requests"])                       # stocked at Z-5 on purpose
    assert any(r["req"] == "rescue:TD" for r in p["runs"])                              # pinned elsewhere: still stranded


def test_a_bootstrap_device_out_of_range_is_not_rescued():
    devs = [dev("HV", "heaven_vessel", "A-1", **SURGE),
            dev("PL", "surge_platform", "A-1", **SURGE),
            dev("MD", "mining_drone", "Z-5", tags=["boot:b1", "fleet:b1"], in_control_range=False, available_commands=["travel"])]
    p = _rescue_plan(devs)
    assert not any(r["id"] == "rescue:MD" for r in p["requests"])                       # left mining there by its bootstrap


def test_a_reserved_device_out_of_range_is_not_rescued():
    devs = [dev("HV", "heaven_vessel", "A-1", **SURGE),
            dev("PL", "surge_platform", "A-1", **SURGE),
            dev("CT", "compute_core", "Z-5", tags=["contract:c1"], in_control_range=False, available_commands=["travel"]),
            dev("TO", "transport_drone", "Z-5", tags=["to:y"], in_control_range=False, available_commands=["travel"]),
            dev("ZZ", "transport_drone", "Z-5", tags=["to:z"], in_control_range=False, available_commands=["travel"])]
    p = _rescue_plan(devs)
    ids = {r["id"] for r in p["requests"]}
    assert "rescue:CT" not in ids and "rescue:TO" not in ids   # a contract's, or bound for another system
    assert "rescue:ZZ" in ids                                  # to: its own system: nothing holds it there


def test_a_rescue_is_dropped_when_its_device_is_in_range_again_before_it_was_moved():
    job = {"status": "waiting", "idx": 1, "meta": {"logistics": {"req": "rescue:HV2"}},
           "steps": [{"desc": "ESC → DELTA", "path": "/devices/ESC", "body": {"command": "travel", "destination": "DELTA"}},
                     {"desc": "wait for ESC", "path": None, "body": {}},
                     {"desc": "HV2 → BADU-3-L4 (home)", "path": "/devices/HV2", "body": {"command": "travel"}}]}
    esc = dev("ESC", "mobile_fleet", None, status="travelling")
    devs = [esc, dev("HV2", "heaven_vessel", "ZERETHYR-2-L4", in_control_range=True)]
    assert [w for _, w in lg.stale_rescues([job], devs)] == ["HV2 is in command range again at ZERETHYR-2-L4"]
    devs[1]["in_control_range"] = False                                                 # still stranded: carry on
    assert lg.stale_rescues([job], devs) == []
    devs[1].update(in_control_range=True, tags=["boot:b"])
    assert lg.stale_rescues([job], devs)[0][1] == "HV2 belongs to a bootstrap now"
    job["idx"] = 3                                                                       # already flying it home
    assert lg.stale_rescues([job], devs) == []


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

    def world_(fal_q, sol_q, cap=500):
        return [dev("F1", "cargo_freighter", "FAL-1", cargo_capacity=cap, **SURGE), dev("HV", "heaven_vessel", "FAL-1", **SURGE),
                dev("AF", "autofactory", "FAL-1", tags=[], status="idle", print_queue=[{"device_type": "mining_drone", "quantity": fal_q}]),
                dev("AS", "autofactory", "SOL-2", tags=[], status="waiting_for_resources",
                    printing={"device_type": "mining_drone"}, print_queue=[{"device_type": "mining_drone", "quantity": sol_q}])]

    def sent(fal_q, sol_q, carbon, cap=500):
        p = lg.plan(fleets=fleets, devices=world_(fal_q, sol_q, cap), bps=bps, inventory={"FAL-1": {"carbon": carbon}}, pos=pos,
                    commandable={"FAL", "SOL"}, hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[], trades=[],
                    incoming={}, replicants={}, drop=lambda s: f"{s}-1")
        return sum(r["carry"].get("carbon", 0) for r in p["runs"] if r["req"] == "print:SOL-2")
    assert sent(30, 5, 1000) == 240        # FAL keeps 19 prints' worth (3.2 h), more than SOL's whole queue (6 prints, 1 h)
    assert sent(30, 30, 1000) == 480       # both queues long: 12 prints each (2 h), the odd one stays home
    assert sent(10, 30, 400) == 200        # FAL's own queue takes all 400; even split: 5 prints (50 min) each
    # a long haul waits for a fuller hold: 200 is under a quarter of a 2000 hold (more is spared on a later pass)
    assert sent(10, 30, 400, cap=2000) == 0
    assert sent(30, 5, 1000, cap=2000) == 240   # a small load that's all the queue wants still goes
    p = lg.plan(fleets=fleets, devices=world_(10, 30, 2000), bps=bps, inventory={"FAL-1": {"carbon": 400}}, pos=pos,
                commandable={"FAL", "SOL"}, hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[], trades=[],
                incoming={}, replicants={}, drop=lambda s: f"{s}-1")
    assert any("waiting for a fuller load" in u["why"] for u in p["unmet"] if u["req"] == "print:SOL-2")


def test_a_pile_already_being_fetched_is_not_planned_again(client):  # noqa: F811
    db, e = client.app.state.db, eng(client)
    cat = client.portal.call(db.kv_get, "stars") or {"stars": []}
    cat["stars"] += [{"designation": d, "position": p} for d, p in POS.items()]
    client.portal.call(db.kv_set, "stars", cat)
    client.portal.call(db.kv_set, "blueprints", list(BPS.values()))
    devs = [dev("F1", "cargo_freighter", "A-1", cargo_capacity=500, **SURGE), dev("F2", "cargo_freighter", "A-1", cargo_capacity=500, **SURGE),
            dev("RL", "ftl_relay", "A-4-L4", tags=[], status="relaying"), dev("RB", "ftl_relay", "B-4-L4", tags=[], status="relaying")]
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(db.kv_set, "inventory", [{"location": "B-BELT", "items": {"carbon": 200}}])
    client.portal.call(db.kv_set, "replicants", {"R1": {"location": "A-1"}})
    items = client.portal.call(e.fleets)
    client.portal.call(e.save_fleets, items + [{"id": "cab", "name": "Cab", "role": "logistics", "home": "A", "wants": {}},
                                               {"id": "hub", "name": "Hub", "role": "mining", "home": "A", "station": True,
                                                "materials": "self", "wants": {}}])
    jobs = client.portal.call(e.jobs)
    jobs.append({"id": "lg-1", "rule": "logistics", "title": "earlier trip", "device": "F2", "status": "running", "idx": 0,
                 "created_at": "2026-10-13T12:00:00+00:00",
                 "steps": [{"desc": "F2: load 200 carbon at B-BELT", "body": {"command": "collect_resources", "resources": {"carbon": 200}},
                            "status": "pending", "path": "/devices/F2", "method": "POST", "wait": []}],
                 "meta": {"devices": ["F2"], "logistics": {"req": "return:B-BELT", "to": "A-1", "carry": {"carbon": 200},
                                                           "legs": [{"pile": "B-BELT", "take": {"carbon": 200}}]}}})
    client.portal.call(db.kv_set, "automation_jobs", jobs)
    client.portal.call(e.logistics_pass, True)
    assert not [j for j in client.portal.call(e.jobs) if j["rule"] == "logistics" and j["id"] != "lg-1"]   # nothing sent twice


def test_a_pile_a_fleet_mission_is_fetching_is_not_planned_again(client):  # noqa: F811
    db, e = client.app.state.db, eng(client)
    cat = client.portal.call(db.kv_get, "stars") or {"stars": []}
    cat["stars"] += [{"designation": d, "position": p} for d, p in POS.items()]
    client.portal.call(db.kv_set, "stars", cat)
    client.portal.call(db.kv_set, "blueprints", list(BPS.values()))
    devs = [dev("F1", "cargo_freighter", "A-1", cargo_capacity=500, **SURGE), dev("F2", "cargo_freighter", "A-1", cargo_capacity=500, **SURGE),
            dev("RL", "ftl_relay", "A-4-L4", tags=[], status="relaying"), dev("RB", "ftl_relay", "B-4-L4", tags=[], status="relaying")]
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(db.kv_set, "inventory", [{"location": "B-BELT", "items": {"carbon": 200}}])
    client.portal.call(db.kv_set, "replicants", {"R1": {"location": "A-1"}})
    items = client.portal.call(e.fleets)
    client.portal.call(e.save_fleets, items + [{"id": "cab", "name": "Cab", "role": "logistics", "home": "A", "wants": {}},
                                               {"id": "hub", "name": "Hub", "role": "mining", "home": "A", "station": True,
                                                "materials": "self", "wants": {}}])
    jobs = client.portal.call(e.jobs)
    jobs.append({"id": "fl-1", "rule": "fleets", "title": "a fleet mission", "device": "F2", "status": "running", "idx": 0,
                 "created_at": "2026-10-13T12:00:00+00:00",
                 "steps": [{"desc": "F2 → B-BELT", "body": {"command": "travel", "destination": "B-BELT"}, "status": "pending",
                            "path": "/devices/F2", "method": "POST", "wait": []},
                           {"desc": "F2: load 200", "body": {"command": "collect_resources", "resources": {"carbon": 200}},
                            "status": "pending", "path": "/devices/F2", "method": "POST", "wait": []}],
                 "meta": {"devices": ["F2"]}})
    client.portal.call(db.kv_set, "automation_jobs", jobs)
    client.portal.call(e.logistics_pass, True)
    assert not [j for j in client.portal.call(e.jobs) if j["rule"] == "logistics"]   # the mission's load is spoken for


def test_a_replicant_about_to_leave_does_not_cover_its_system(client):  # noqa: F811
    db, e = client.app.state.db, eng(client)
    cat = client.portal.call(db.kv_get, "stars") or {"stars": []}
    cat["stars"] += [{"designation": d, "position": p} for d, p in POS.items()]
    client.portal.call(db.kv_set, "stars", cat)
    client.portal.call(db.kv_set, "replicants", {"R1": {"location": "Z-1"}})
    devs = client.portal.call(e.devices)
    assert "Z" in client.portal.call(e.commandable, devs)
    assert "Z" not in client.portal.call(e.commandable, devs, None, {"R1"})


def test_a_deals_payout_is_loaded_and_brought_to_the_gather_point():
    devs = world()
    devs[2]["print_queue"], devs[2]["status"], devs[2]["printing"] = [], "idle", None
    t = {"key": "C1:T1", "controller": "C1", "trade_code": "T1", "name": "Ore swap", "location": "Z-3", "price": {"carbon": 50},
         "rewards": {"rares": 30}, "reward_devices": {"surge_plate": 1}}
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"A-1": {"carbon": 500}}, pos=POS, commandable={"A", "B"},
                hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[], trades=[t], incoming={}, replicants={},
                drop=lambda s: f"{s}-1")
    run = next(r for r in p["runs"] if r["req"] == "trade:C1:T1")
    assert run["fulfil"] and run["rewards"] == {"rares": 30} and run["home"] == "A-1" and run["reward_devices"] == {"surge_plate": 1}
    descs = [x["desc"] for x in lg.run_steps(run, {d["device_code"]: d for d in devs}) if x["body"] or x["method"] == "POST"]
    i = next(i for i, d in enumerate(descs) if d.startswith("execute"))
    after = descs[i + 1:]
    assert after[0] == "F1: load 30 rares (the payout) at Z-3"
    assert any(d.startswith("F1 → A-1 (with the payout)") for d in after) and any(d.startswith("HV → A-1") for d in after)
    assert after[-1] == "F1: deposit the payout at A-1"


def test_devices_a_deal_paid_out_are_sent_to_the_gather_point(client):  # noqa: F811
    db, e = client.app.state.db, eng(client)
    cat = client.portal.call(db.kv_get, "stars") or {"stars": []}
    cat["stars"] += [{"designation": d, "position": p} for d, p in POS.items()]
    client.portal.call(db.kv_set, "stars", cat)
    from datetime import datetime, timezone
    client.portal.call(db.kv_set, "devices", [dev("SP", "surge_plate", "Z-3", tags=[]), dev("SQ", "surge_plate", "Z-3", tags=["fleet:x"])])
    client.portal.call(db.kv_set, lg.KV_PICKUPS, {"Z-3": {"types": {"surge_plate": 1}, "to": "A",
                                                          "at": datetime.now(timezone.utc).isoformat()}})
    items = client.portal.call(e.fleets)
    client.portal.call(e.save_fleets, items + [{"id": "cab", "name": "Cab", "role": "logistics", "home": "A", "wants": {}}])
    client.portal.call(e.logistics_pass, True)
    jobs = [j for j in client.portal.call(e.jobs) if "payout" in j["title"]]
    assert len(jobs) == 1 and jobs[0]["device"] == "SP"
    assert jobs[0]["steps"][0]["body"]["configuration"]["add_tags"] == ["to:a"]
    assert client.portal.call(db.kv_get, lg.KV_PICKUPS) == {}


def test_idle_logistics_devices_gather_at_the_nearest_printing_hub():
    """Owner's request 2026-10-17: a free logistics device away from every printing hub (a fleet taking materials in)
    flies to the nearest one; one that can't surge is towed there by a free carrier."""
    devs = [dev("F1", "cargo_freighter", "B-1", cargo_capacity=100, **SURGE),
            dev("PL", "carrier_plate", "B-1", attach_capacity=2, **SURGE),
            dev("HL", "hauler", "B-2", available_commands=["collect_resources"])]
    p = run_plan(devs, {})
    by = {r.get("escort") or r.get("rescued"): r for r in p["runs"]}
    assert by["F1"]["kind"] == "reposition" and by["F1"]["to"] == "A-1"
    assert by["HL"]["kind"] == "rescue" and by["HL"]["ride"] == "PL" and by["HL"]["to"] == "A-1"
    p = run_plan([dev("F1", "cargo_freighter", "A-2", cargo_capacity=100, **SURGE)], {})
    assert not p["runs"]                                 # at a hub already: it stays


def test_stock_prints_are_supplied_last():
    devs = [dev("AF", "autofactory", "B-2", tags=[], status="idle",
                print_queue=[{"device_type": "mining_drone", "quantity": 1, "tags": ["spare", "stock"]}])]
    req = next(r for r in lg.requests(devices=devs, bps=BPS, inventory={}, pos=POS, contracts=[], trades=[], incoming={},
                                      fleets=FLEETS, gather={"A": "A-1"}, busy_stars=set())[0] if r["kind"] == "print")
    assert req["tier"] == 4 and "keep in stock" in req["label"]
    devs[0]["print_queue"].append({"device_type": "mining_drone", "quantity": 1})
    req = next(r for r in lg.requests(devices=devs, bps=BPS, inventory={}, pos=POS, contracts=[], trades=[], incoming={},
                                      fleets=FLEETS, gather={"A": "A-1"}, busy_stars=set())[0] if r["kind"] == "print")
    assert req["tier"] == 1


def test_logistics_reaches_every_device_nearest_first():
    devs = world([dev("F2", "cargo_freighter", "W-1", cargo_capacity=100, **SURGE)])
    devs[2]["print_queue"], devs[2]["status"], devs[2]["printing"] = [], "idle", None
    pos = {**POS, "W": {"x": 500, "y": 0, "z": 0}}
    c = {"designation": "EV1", "location": "Z-3", "title": "Help", "price": {"carbon": 50}}

    def plan(busy):
        return lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"A-1": {"carbon": 500}}, pos=pos,
                       commandable={"A", "B", "W"}, hosts={"HV": "R1"}, busy=set(busy), contracts=[c], trades=[],
                       incoming={}, replicants={"R1": {"location": "A-1"}}, drop=lambda s: f"{s}-1")
    assert cargo(next(r for r in plan(())["runs"] if r["req"] == "contract:EV1")) == ["F1"]       # the near one first
    assert cargo(next(r for r in plan({"F1"})["runs"] if r["req"] == "contract:EV1")) == ["F2"]   # 500 ly is no limit


def test_a_system_the_game_says_is_out_of_range_is_not_commandable(client):  # noqa: F811
    from rsweb import relaynet
    devs = [{"device_code": "F1", "location": "RHY", "in_control_range": False},
            {"device_code": "F2", "location": "SOL-3", "in_control_range": False},
            {"device_code": "F3", "location": "SOL-4", "in_control_range": True},
            {"device_code": "F4", "location": None, "in_control_range": False, "stowed_in_device_code": "F3"}]
    assert relaynet.dark_stars(devs) == {"RHY"}
    db, e = client.app.state.db, eng(client)
    client.portal.call(db.kv_set, "replicants", {"R1": {"location": "RHY-1"}})
    assert "RHY" not in client.portal.call(e.commandable, devs)   # the game's word beats the model


def test_idle_carrier_drops_what_isnt_its_work_at_the_receiving_point():
    """Owner 2026-10-20: logistics carriers dump extra cargo and devices unrelated to their work at a receiving fleet or hub."""
    devs = [dev("CV", "cargo_vessel", "B-3", stow_capacity=50, cargo={"carbon": 40}, **SURGE),
            dev("HV", "heaven_vessel", "A-1", **SURGE),
            dev("MX", "matrix_container", None, stowed_in_device_code="CV", tags=[]),               # its escort: stays
            dev("SD", "survey_drone", None, stowed_in_device_code="CV", tags=["fleet:miner-1"]),    # someone else's: off
            dev("TD", "transport_drone", None, attached_to_device_code="CV", tags=["fleet:x"]),     # off (detached)
            dev("BD", "mining_drone", None, stowed_in_device_code="CV", tags=["fleet:x", "to:b"]),  # bound somewhere: stays
            dev("HL", "transport_hauler", None, attached_to_device_code="CV")]                     # a logistics member
    p = run_plan(devs, {})
    run = next(r for r in p["runs"] if r.get("kind") == "dump")
    assert run["escort"] == "CV" and run["to"] == "A-1" and run["carry"] == {"carbon": 40}
    assert run["dump"] == [{"code": "SD", "mode": "stow"}, {"code": "TD", "mode": "attach"}]
    by = {d["device_code"]: d for d in devs}
    descs = [s["desc"] for s in lg.run_steps(run, by)]
    assert descs[0] == "CV → A-1 (drop-off)" and "CV: deposit 40 carbon at A-1" in descs
    assert "deploy SD from CV" in descs and "CV: detach TD" in descs
    assert set(lg.run_devices(run)) == {"CV", "SD", "TD"}


def test_an_approved_trade_in_a_warded_system_is_a_request(client):  # noqa: F811
    """Owner 2026-10-20: an approved trade at SOL (warded) was missing from the requests; a ward's lock stops contracts,
    not shop trades."""
    db, e = client.app.state.db, eng(client)
    cat = client.portal.call(db.kv_get, "stars") or {"stars": []}
    cat["stars"] += [{"designation": d, "position": p, **({"has_ward": True} if d == "B" else {})} for d, p in POS.items()]
    client.portal.call(db.kv_set, "stars", cat)
    client.portal.call(db.kv_set, "devices", world())
    client.portal.call(db.kv_set, "inventory", [{"location": "A-1", "items": {"carbon": 300}}])
    client.portal.call(db.kv_set, "replicants", {"R1": {"location": "A-1", "hosted_device_code": "HV"}})
    client.portal.call(db.kv_set, "traders_cache", {"C1": {"controller_code": "C1", "location": "B-3", "star": "B",
                                                           "read_at": "2026-10-20T00:00:00+00:00",
                                                           "trades": [{"trade_code": "T1", "name": "Widget",
                                                                       "criteria": {"resources": {"carbon": 50}}}]}})
    client.post("/shop/approve", data={"controller": "C1", "trade_code": "T1", "on": "on"}, headers=HX)
    items = client.portal.call(e.fleets)
    client.portal.call(e.save_fleets, items + [{"id": "cab", "name": "Cab", "role": "logistics", "home": "A", "wants": {}}])
    client.portal.call(e.logistics_pass, True)
    st = client.portal.call(db.kv_get, "logistics_state")
    assert any(r["id"] == "trade:C1:T1" for r in st["requests"])


def test_an_approved_trade_for_several_units_pays_and_executes_each(client):  # noqa: F811
    """Owner 2026-10-28: order more than one from a shop. The game's buy call sells one unit, so one delivery pays n
    prices and the trade is executed n times (at most the listed stock)."""
    db, e = client.app.state.db, eng(client)
    cat = client.portal.call(db.kv_get, "stars") or {"stars": []}
    cat["stars"] += [{"designation": d, "position": p} for d, p in POS.items()]
    client.portal.call(db.kv_set, "stars", cat)
    client.portal.call(db.kv_set, "devices", world())
    client.portal.call(db.kv_set, "inventory", [{"location": "A-1", "items": {"carbon": 300}}])
    client.portal.call(db.kv_set, "replicants", {"R1": {"location": "A-1", "hosted_device_code": "HV"}})
    client.portal.call(db.kv_set, "traders_cache", {"C1": {"controller_code": "C1", "location": "B-3", "star": "B",
                                                           "read_at": "2026-10-20T00:00:00+00:00",
                                                           "trades": [{"trade_code": "T1", "name": "Widget", "current_stock": 3,
                                                                       "criteria": {"resources": {"carbon": 50}},
                                                                       "rewards": {"resources": {"rares": 2}}}]}})
    r = client.post("/shop/approve", data={"controller": "C1", "trade_code": "T1", "on": "on", "qty": "5"}, headers=HX)
    assert "5 to buy" in r.text
    assert client.portal.call(db.kv_get, "trade_approvals")["counts"] == {"C1:T1": 5}
    items = client.portal.call(e.fleets)
    client.portal.call(e.save_fleets, items + [{"id": "cab", "name": "Cab", "role": "logistics", "home": "A", "wants": {}}])
    client.portal.call(e.logistics_pass, True)
    req = next(r for r in client.portal.call(db.kv_get, "logistics_state")["requests"] if r["id"] == "trade:C1:T1")
    assert req["price"] == {"carbon": 150} and req["times"] == 3 and req["rewards"] == {"rares": 6}   # capped at stock 3
    run = {"to": "B-3", "legs": [], "cargos": [], "carry": {}, "fulfil": lg._deal(req), "replicant": "R1"}
    execs = [s for s in lg.run_steps(run, {}) if s["path"] == "/devices/C1/trades/T1"]
    assert len(execs) == 3 and "×3" in execs[0]["desc"]
    assert [x["critical"] for x in execs] == [True, False, False]   # a later refusal doesn't stop the job
    client.post("/shop/approve", data={"controller": "C1", "trade_code": "T1", "qty": "0"}, headers=HX)   # 0 = not approved
    ok = client.portal.call(db.kv_get, "trade_approvals")
    assert ok["counts"] == {} and ok["trades"] == {}


def test_an_approval_for_several_units_stays_until_all_are_bought():
    """Owner 2026-10-28: 10 vector charges approved, one or two bought and the approval was gone. The approval now stays
    while the job runs and keeps the units it didn't buy."""
    path = "/devices/C1/trades/T1"
    ok = {"trades": {"C1:T1": "x"}, "counts": {"C1:T1": 10}, "running": {"C1:T1": "j1"}}
    job = {"id": "j1", "status": "waiting", "steps": [{"path": path, "status": "done"}]}
    assert not lg.settle_trades(ok, [job]) and ok["running"] == {"C1:T1": "j1"}
    job["status"] = "failed"
    job["steps"] += [{"path": path, "status": "done"}, {"path": path, "status": "failed"}]
    assert lg.settle_trades(ok, [job])
    assert ok["counts"] == {"C1:T1": 8} and "C1:T1" in ok["trades"] and ok["running"] == {}
    ok["running"]["C1:T1"] = "j2"
    lg.settle_trades(ok, [{"id": "j2", "status": "done", "steps": [{"path": path, "status": "done"}] * 8}])
    assert ok["trades"] == {} and ok["counts"] == {} and "C1:T1" in ok["done"]


def test_a_priority_shows_at_once_and_every_tier_is_accepted(client):  # noqa: F811
    db = client.app.state.db
    client.portal.call(db.kv_set, lg.KV_STATE, {"at": "2026-10-20T10:00:00+00:00", "requests": [
        {"id": "print:A", "kind": "print", "to": "A-1", "need": {"carbon": 5}, "tier": 1, "base_tier": 1, "label": "print A"},
        {"id": "rescue:R1", "kind": "rescue", "to": "Z-1", "need": {}, "tier": 5, "base_tier": 5, "label": "rescue R1"}]})
    r = client.post("/fleets/logistics/prefs", data={"req": "rescue:R1", "tier": "0"}, headers=HX)
    assert "saved" in r.text
    assert client.portal.call(db.kv_get, lg.KV_PREFS)["bump"] == {"rescue:R1": 0}
    st = client.portal.call(db.kv_get, lg.KV_STATE)
    assert [x["id"] for x in st["requests"]] == ["rescue:R1", "print:A"] and st["requests"][0]["tier"] == 0
    page = client.get("/fleets/logistics", headers=H).text
    assert '<option value="0" selected>' in page and "bumped" in page
    client.post("/fleets/logistics/prefs", data={"req": "rescue:R1", "tier": "reset"}, headers=HX)
    assert client.portal.call(db.kv_get, lg.KV_STATE)["requests"][1]["tier"] == 5


def test_an_unmet_reason_says_where_it_loads_where_it_goes_and_whats_in_the_way():
    devs = [dev("F1", "cargo_freighter", "A-1", cargo_capacity=100, **SURGE),
            dev("F2", "cargo_freighter", "Z-1", cargo_capacity=100, **SURGE)]
    c = {"designation": "EV1", "location": "B-3", "title": "Help", "price": {"carbon": 50}}
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"A-1": {"carbon": 500}}, pos=POS, commandable={"A", "B", "Z"},
                hosts={}, busy={"F1"}, reach=20, contracts=[c], trades=[], incoming={}, replicants={},
                drop=lambda s: f"{s}-1")
    why = next(u["why"] for u in p["unmet"] if u["req"] == "contract:EV1")
    assert why == ("no logistics cargo device free to load at A-1 for B-3 (2 cargo devices: 1 on jobs); the nearest free "
                   "one, F2 (cargo freighter): it is 60 ly away, beyond the 20 ly reach")


def test_a_rescue_reason_names_each_replicant_vessel_and_what_keeps_it():
    devs = [dev("HV", "heaven_vessel", "A-1", **SURGE), dev("MC", "matrix_container", "A-1", features=["cruise"]),
            dev("P1", "surge_plate", "Z-1", in_control_range=False, **SURGE)]
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={}, pos=POS, commandable={"A", "B"},
                hosts={"HV": "R1", "MC": "R2"}, busy={"HV"}, reach=100, contracts=[], trades=[], incoming={},
                replicants={}, drop=lambda s: f"{s}-1")
    why = next(u["why"] for u in p["unmet"] if u["req"] == "rescue:P1")
    assert why.endswith("— HV (heaven vessel): on a job; MC (matrix container): can't surge")


def test_a_shop_listing_sends_a_replicant_last_of_all():
    devs = world()
    li = [{"controller": "SH1", "location": "Z-OORT", "label": "get the listing of Shop at Z-OORT"}]
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={}, pos=POS, commandable={"A", "B"}, hosts={"HV": "R1"},
                busy=set(), reach=100, contracts=[], trades=[], incoming={}, replicants={"R1": {"location": "A-1"}},
                drop=lambda s: f"{s}-1", listings=li)
    run = next(r for r in p["runs"] if r["req"] == "listing:SH1")
    assert run["escort"] == "HV" and run["kind"] == "listing"                  # only a device hosting a replicant
    assert [s["desc"] for s in lg.run_steps(run, {d["device_code"]: d for d in devs})][0] == "HV → Z-OORT (to read the shop's trades)"
    req = next(r for r in p["requests"] if r["id"] == "listing:SH1")
    assert req["tier"] == 6
    p2 = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={}, pos=POS, commandable={"A", "B"}, hosts={"HV": "R1"},
                 busy={"HV"}, reach=100, contracts=[], trades=[], incoming={}, replicants={}, drop=lambda s: f"{s}-1",
                 listings=li)
    assert any(u["req"] == "listing:SH1" for u in p2["unmet"])


def test_get_listing_reads_now_or_asks_logistics(client):
    from rsweb import shop
    from rsweb.api import ApiError
    db, e = client.app.state.db, eng(client)
    client.portal.call(db.kv_set, "traders_cache", {"SH1": {"controller_code": "SH1", "location": "Z-OORT", "shop_name": "Shop",
                                                            "read_at": "2026-10-20T00:00:00+00:00", "trades": []}})
    api = client.app.state.api
    calls = {"ok": False}
    orig = api.get

    async def get(path, **kw):
        if path == "/devices/SH1/trades":
            if not calls["ok"]:
                raise ApiError(403, "no replicant in that system")
            return {"trades": [{"name": "t1", "trade_code": "T1"}]}
        return await orig(path, **kw)
    api.get = get
    try:
        r = client.post("/shop/listing", data={"controller": "SH1"}, headers=HX).text
        assert "Asked logistics" in r and "SH1" in client.portal.call(db.kv_get, shop.LISTING_KV)
        assert "listing asked" in client.get("/shop", headers=H).text
        reps = {"R9": {"location": "Z-1"}}                                     # a replicant reached the system
        calls["ok"] = True
        lines, todo = client.portal.call(e.listing_requests, reps)
        assert "1 trades read" in lines[0] and todo == []
        assert client.portal.call(db.kv_get, shop.LISTING_KV) == {}
        assert client.portal.call(shop.known_traders, db)["SH1"]["trades"][0]["trade_code"] == "T1"
    finally:
        api.get = orig


def test_bills_listing_goes_to_logistics_not_his_trail(client):
    """Owner 2026-10-22: following Bill is started by hand on the Trail page only; his shop's listing is a logistics job."""
    from rsweb import shop, trail as tl
    from rsweb.api import ApiError
    db, e = client.app.state.db, eng(client)
    client.portal.call(db.kv_set, tl.KV, {"target_name": "Bill", "target_code": tl.BILL_CODE, "scanner": "REP1"})
    client.portal.call(db.kv_set, "traders_cache", {"BS1": {"controller_code": "BS1", "location": "Z-OORT", "owner_name": "Bill",
                                                            "owner_replicant_code": tl.BILL_CODE,
                                                            "read_at": "2026-10-20T00:00:00+00:00", "trades": []}})
    api = client.app.state.api
    orig = api.get

    async def get(path, **kw):
        if path == "/devices/BS1/trades":
            raise ApiError(403, "no replicant in that system")
        return await orig(path, **kw)
    api.get = get
    try:
        r = client.post("/shop/listing", data={"controller": "BS1"}, headers=HX).text
        assert "Asked logistics" in r
        assert not (client.portal.call(db.kv_get, tl.KV).get("follow") or {}).get("active")   # nobody follows him
        lines, todo = client.portal.call(e.listing_requests, {})
        assert [x["controller"] for x in todo] == ["BS1"]
        client.portal.call(shop.mark_gone, db, "BS1", "Z-OORT", "2099-01-01T00:00:00+00:00")   # he moved it
        r = client.post("/shop/listing", data={"controller": "BS1"}, headers=HX).text
        assert "once the shop is seen again" in r
        assert client.portal.call(e.listing_requests, {})[1] == []            # waits until his shop is seen
    finally:
        api.get = orig

def test_idle_carrier_brings_another_fleets_member_home_not_to_a_drop_off():
    """Owner 2026-10-21: a fleet member aboard a logistics carrier goes to its fleet's home, never a drop-off point."""
    fleets = FLEETS + [{"id": "far", "name": "Far", "role": "mining", "home": "B"}]
    devs = [dev("CV", "cargo_vessel", "A-3", stow_capacity=50, cargo={"carbon": 40}, **SURGE),
            dev("HV", "heaven_vessel", "A-1", **SURGE),
            dev("MD", "mining_drone", None, stowed_in_device_code="CV", tags=["fleet:far"]),
            dev("SD", "survey_drone", None, stowed_in_device_code="CV", tags=["fleet:gone"])]
    p = lg.plan(fleets=fleets, devices=devs, bps=BPS, inventory={}, pos=POS, commandable={"A", "B"},
                hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[], trades=[], incoming={},
                replicants={"R1": {"location": "A-1"}}, drop=lambda s: f"{s}-1")
    run = next(r for r in p["runs"] if r.get("kind") == "dump")
    assert run["to"] == "B-1" and run["dump"] == [{"code": "MD", "mode": "stow"}] and run["carry"] == {}
    assert "home to B" in run["label"]


def test_a_carrier_lets_other_fleets_members_off_before_it_leaves():
    from rsweb import fleets as fl
    fleets = [{"id": "a", "home": "AAA"}, {"id": "b", "home": "BBB"}, {"id": "m", "home": "MMM"}]
    devs = [{"device_code": "CAR", "tags": ["fleet:a"]},
            {"device_code": "OWN", "tags": ["fleet:a"], "stowed_in_device_code": "CAR"},       # its own fleet: stays
            {"device_code": "B1", "tags": ["fleet:b"], "attached_to_device_code": "CAR"},      # carried away: off
            {"device_code": "B2", "tags": ["fleet:b"], "stowed_in_device_code": "CAR"},        # carried away: off
            {"device_code": "M1", "tags": ["fleet:m"], "stowed_in_device_code": "CAR"},        # the job's fleet: stays
            {"device_code": "B3", "tags": ["fleet:b"], "stowed_in_device_code": "CAR"},        # named in the job: stays
            {"device_code": "B4", "tags": ["fleet:b", "to:ccc"], "stowed_in_device_code": "CAR"},  # bound there: stays
            {"device_code": "MX", "device_type": "replicant_matrix", "tags": ["fleet:b"], "stowed_in_device_code": "CAR"},
            {"device_code": "X1", "tags": [], "stowed_in_device_code": "CAR"}]                 # fleetless: not this rule
    job = {"meta": {"fleet": "m"}, "steps": [{"path": "/devices/B3", "body": {"command": "stow", "target": "CAR"}}]}
    assert fl.foreign_riders("CAR", "CCC-2", devs, fleets, job) == [{"code": "B1", "mode": "attach"},
                                                                    {"code": "B2", "mode": "stow"}]
    assert fl.foreign_riders("CAR", "BBB-1", devs, fleets, job) == []   # going to their home: they ride along
    descs = [s["desc"] for s in fl.let_off_steps("CAR", [{"code": "B1", "mode": "attach"}, {"code": "B2", "mode": "stow"}])]
    assert descs == ["CAR: detach B1 (not its fleet's)", "deploy B2 from CAR (not its fleet's)"]


def test_a_rescue_of_more_than_a_day_waits_for_your_ok():
    """Owner 2026-10-22: a heaven vessel was sent from SOLARES to DELTA to rescue a stranded vessel, well over a day of
    travel. A rescue that long waits for Confirm on the Logistics page; the OK holds for two days."""
    pos = {**POS, "F": {"x": 700, "y": 0, "z": 0}}
    devs = [dev("HV", "heaven_vessel", "A-1", **SURGE), dev("P1", "surge_plate", "F-1", in_control_range=False, **SURGE)]

    def go(prefs):
        return lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={}, pos=pos, commandable={"A", "B"},
                       hosts={"HV": "R1"}, busy=set(), contracts=[], trades=[], incoming={}, replicants={},
                       drop=lambda s: f"{s}-1", prefs=prefs)
    p = go({})
    req = next(r for r in p["requests"] if r["id"] == "rescue:P1")
    assert req["confirm"] and req["trip_h"] > 24 and not p["runs"]
    assert "waits for your OK" in next(u["why"] for u in p["unmet"] if u["req"] == "rescue:P1")
    now = datetime.now(timezone.utc)
    assert go({"confirm": {"rescue:P1": now.isoformat()}})["runs"][0]["rescued"] == "P1"
    assert not go({"confirm": {"rescue:P1": (now - timedelta(days=3)).isoformat()}})["runs"]   # an old OK has run out
    pos["F"]["x"] = 60
    assert go({})["runs"]                                                                       # a short one just goes
    pos.pop("F")                                                    # DELTA, 2026-10-22: not in the catalog → it asks
    p = go({})
    assert not p["runs"] and next(r for r in p["requests"] if r["id"] == "rescue:P1")["trip_h"] is None


def test_trades_at_one_location_share_one_delivery():
    devs = world([dev("F2", "cargo_freighter", "A-1", cargo_capacity=100, **SURGE)])
    trades = [{"key": f"SHOP:T{i}", "location": "B-2", "price": {"carbon": 30}, "controller": "SHOP", "trade_code": f"T{i}",
               "name": f"T{i}"} for i in (1, 2, 3)]
    p = lg.plan(fleets=FLEETS, devices=devs, bps={}, inventory={"A-1": {"carbon": 500}, "B-2": {"carbon": 35}}, pos=POS,
                commandable={"A", "B"}, hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[], trades=trades,
                incoming={}, replicants={"R2": {"location": "B-2"}}, drop=lambda s: f"{s}-1")
    # T1 is paid from the 35 there; T2 asks for what T2 and T3 lack (5 left after T1 + 55), T3 comes with it
    runs = {r["req"]: r for r in p["runs"]}
    assert runs["trade:SHOP:T1"]["carry"] == {} and runs["trade:SHOP:T1"]["fulfil"]["trade_code"] == "T1"
    run = runs["trade:SHOP:T2"]
    assert run["carry"] == {"carbon": 55} and run["fulfil"]["trade_code"] == "T2"
    assert [a["trade_code"] for a in run["also"]] == ["T3"] and "trade:SHOP:T3" not in runs
    assert any(u["req"] == "trade:SHOP:T3" and "together with trade T2" in u["why"] for u in p["unmet"])
    steps = lg.run_steps(run, {d["device_code"]: d for d in devs})
    assert [s["path"] for s in steps if s["path"].endswith(("/T2", "/T3"))] == ["/devices/SHOP/trades/T2",
                                                                                 "/devices/SHOP/trades/T3"]


MAINT = {"features": ["cruise", "repair", "stow", "ami"],
         "available_commands": ["travel", "stow", "deploy", "set_directive", "clear_directive"]}
PATROL = {"ami_directive": {"name": "patrol"}}


def _repair_plan(devs, busy=frozenset(), repair_to=None, prefs=None, pos=None):
    return lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={}, pos=pos or POS, commandable={"A", "B", "Z"},
                   hosts={"HV": "R1"}, busy=set(busy), reach=100, contracts=[], trades=[], incoming={}, replicants={},
                   drop=lambda s: f"{s}-1", repair_to=repair_to, prefs=prefs)


def test_a_logistics_repair_drone_is_carried_to_the_most_worn_system_and_patrols_there():
    devs = [dev("HV", "heaven_vessel", "A-1", **SURGE),
            dev("MD", "maintenance_drone", "A-1", operational_capacity=100, **MAINT, **PATROL),
            dev("X1", "mining_drone", "B-2", tags=[], operational_capacity=70),     # B: 30 % missing
            dev("Y1", "mining_drone", "Z-2", tags=[], operational_capacity=50),     # Z: 50 % + 30 %: worst
            dev("Y2", "mining_drone", "Z-3", tags=[], operational_capacity=70)]
    p = _repair_plan(devs)
    run = next(r for r in p["runs"] if r["kind"] == "repair")
    assert run["req"] == "repair:Z" and run["repairer"] == "MD" and run["ride"] == "HV" and run["mode"] == "stow"
    assert run["point"] == "Z-2"                                               # the worst device's spot
    assert any(u["req"] == "repair:B" and "no spare" in u["why"] for u in p["unmet"])
    assert not any(r["kind"] == "reposition" and r.get("escort") == "MD" for r in p["runs"])
    steps = lg.run_steps(run, {d["device_code"]: d for d in devs})
    cmds = [s["body"].get("command", "travel") for s in steps if s["body"]]
    assert cmds == ["clear_directive", "stow", "travel", "deploy", "set_directive"]
    assert "MD" in lg.run_devices(run)
    # one on its way to Z already: B gets it instead
    p2 = _repair_plan(devs, repair_to={"Z": 1})
    assert next(r for r in p2["runs"] if r["kind"] == "repair")["req"] == "repair:B"


def test_a_repair_drone_stays_where_it_is_needed_and_patrols():
    devs = [dev("HV", "heaven_vessel", "A-1", **SURGE),
            dev("MD", "maintenance_drone", "A-1", operational_capacity=100, **MAINT),
            dev("W1", "mining_drone", "A-2", tags=[], operational_capacity=40),
            dev("Y1", "mining_drone", "Z-2", tags=[], operational_capacity=50)]
    p = _repair_plan(devs)
    runs = [r for r in p["runs"] if r["kind"] == "repair"]
    assert len(runs) == 1 and runs[0]["mode"] == "stay" and runs[0]["repairer"] == "MD"
    assert [s["body"]["command"] for s in lg.run_steps(runs[0], {})] == ["set_directive"]
    # a fleetless drone patrols A: ours may go
    devs.append(dev("SB", "service_bot", "A-1", tags=[], **MAINT, **PATROL))
    run = next(r for r in _repair_plan(devs)["runs"] if r["kind"] == "repair")
    assert run["req"] == "repair:Z" and run["repairer"] == "MD"


def test_repair_drones_not_in_the_logistics_fleet_and_far_trips_are_left_alone():
    devs = [dev("HV", "heaven_vessel", "A-1", **SURGE),
            dev("SB", "service_bot", "A-1", tags=["fleet:home"], **MAINT),
            dev("Y1", "mining_drone", "Z-2", tags=[], operational_capacity=50)]
    assert not any(r["kind"] == "repair" for r in _repair_plan(devs)["runs"])
    devs[1]["tags"] = ["fleet:cab"]
    far = {**POS, "Z": {"x": 2000, "y": 0, "z": 0}}                                # about 28 h of surging
    p = _repair_plan(devs, pos=far)
    assert not any(r["kind"] == "repair" for r in p["runs"])
    assert any(u["req"] == "repair:Z" and "waits for your OK" in u["why"] for u in p["unmet"])


def test_repair_patrols_go_only_where_a_patrol_may_be_sent():
    # live 2026-10-22: ERAJAELAE was lit only by a survey fleet's replicant; the patrol arrived after it left
    devs = [dev("HV", "heaven_vessel", "A-1", **SURGE),
            dev("MD", "maintenance_drone", "A-1", operational_capacity=100, **MAINT, **PATROL),
            dev("Y1", "mining_drone", "Z-2", tags=[], operational_capacity=50)]
    assert any(r["req"] == "repair:Z" for r in _repair_plan(devs)["runs"])
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={}, pos=POS, commandable={"A", "B", "Z"},
                hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[], trades=[], incoming={}, replicants={},
                drop=lambda s: f"{s}-1", repair_stars={"A", "B"})
    assert not any(r["kind"] == "repair" and r["req"] for r in p["runs"])
    assert not any(r["id"] == "repair:Z" for r in p["requests"])


def test_a_bootstrap_fleets_home_piles_stay_put():
    devs = world()
    devs[2]["print_queue"], devs[2]["status"], devs[2]["printing"] = [], "idle", None
    inv = {"B-BELT": {"silicates": 80}}
    fleets = FLEETS + [{"id": "delta", "name": "Delta", "role": "bootstrap", "home": "B", "station": False}]
    p = lg.plan(fleets=fleets, devices=devs, bps=BPS, inventory=inv, pos=POS, commandable={"A", "B"}, hosts={},
                busy=set(), contracts=[], trades=[], incoming={}, replicants={}, drop=lambda s: f"{s}-1")
    assert not any(r["id"] == "return:B-BELT" for r in p["requests"])


def test_backlog_counts_piles_holds_and_devices_waiting():
    from rsweb import metrics
    devs = [dev("HL", "transport_hauler", "A-1", cargo={"carbon": 30}),
            dev("D1", "mining_drone", "B-2", tags=["to:a"]),        # on its way to A: waiting
            dev("D2", "mining_drone", "A-2", tags=["to:a"]),        # there already
            dev("D3", "mining_drone", "A-2", tags=["to:home"]),     # to a fleet's id: its home (A), there
            dev("D4", "mining_drone", "A-2", tags=["at:a-3"])]      # pinned elsewhere in the system: waiting
    b = metrics.backlog(inventory={"A-1": {"carbon": 300}, "A-2": {"carbon": 20}, "B-2": {"iron": 5.5}}, receiving={"A-1"},
                        carriers=[devs[0]], devices=devs, strays=[{"code": "S1"}], fleet_homes={"home": "A", "cab": "A"},
                        requests=[{"id": "rescue:R9", "kind": "rescue", "device": "R9"}, {"id": "x", "skipped": True}],
                        unmet=[{"req": "x"}])
    assert b == {"res_away": 25.5, "res_deals": 0, "res_holds": 30, "dev_waiting": 4, "req_open": 1, "req_unmet": 1}


def test_backlog_splits_what_open_deals_still_pay():
    from rsweb import metrics
    b = metrics.backlog(inventory={"C-3": {"carbon": 40, "iron": 7}, "D-1": {"carbon": 9}}, receiving=set(), carriers=[],
                        devices=[], strays=[], fleet_homes={},
                        requests=[{"id": "contract:K1", "kind": "contract", "to": "C-3", "price": {"carbon": 25}},
                                  {"id": "trade:T", "kind": "trade", "to": "C-3", "price": {"carbon": 10}},
                                  {"id": "trade:S", "kind": "trade", "to": "D-1", "price": {"carbon": 5}, "skipped": True}],
                        unmet=[])
    # 35 carbon owed at C-3; the rest (5 carbon, a trade's 7 iron payout) and the skipped trade's pile wait to move
    assert (b["res_deals"], b["res_away"]) == (35, 21)


def test_logistics_pass_samples_the_backlog_for_the_dashboard(client):  # noqa: F811
    db, e = client.app.state.db, eng(client)
    test_logistics_pass_sends_a_job_for_a_waiting_print(client)
    rows = client.portal.call(db.fetchall, "SELECT metric, value FROM metric_history")
    got = {r["metric"] for r in rows}
    assert {"res_away", "res_holds", "dev_waiting", "req_open", "req_unmet", "req_age_max_h", "lead_med_h",
            "carriers_busy_pct", "missions_stalled", "expired_24h", "role:logistics", "cmd_systems", "relay_systems"} <= got
    client.portal.call(e.logistics_pass, True)   # within ten minutes: no second sample
    assert len(client.portal.call(db.fetchall, "SELECT * FROM metric_history")) == len(rows)
    assert "Devices by fleet role" in client.get("/partials/backlog?card=trends", headers=HX).text
    assert 'hx-get="/partials/backlog"' in open(__import__("pathlib").Path(__file__).parents[1] / "rsweb/templates/dashboard.html").read()
    page = client.get("/partials/backlog?hours=48", headers=HX).text
    assert "Resources not in a receiving stockpile" in page and "Devices waiting to be moved" in page and "<polyline" in page


def test_request_ages_lead_times_carriers_roles_and_mining_gains():
    from rsweb import metrics
    t0 = datetime(2026, 10, 22, tzinfo=timezone.utc)
    st = metrics.track(None, {"a", "b"}, t0)
    st = metrics.track(st, {"b", "c"}, t0 + timedelta(hours=3))      # a closed after 3 h
    a = metrics.ages(st, t0 + timedelta(hours=4))
    assert a == {"req_age_max_h": 4.0, "req_age_med_h": 2.5, "lead_med_h": 3.0}
    cs = [{"device_code": "X", "cargo_capacity": 100, "cargo": {"carbon": 50}}, {"device_code": "Y", "cargo_capacity": 100}]
    assert metrics.carrier_use(cs, {"X"}) == {"carriers_busy_pct": 50.0, "hold_fill_pct": 50.0}
    fleets = [{"id": "cab", "role": "logistics"}, {"id": "home", "role": "mining", "mission": {"status": "stalled",
                                                                                                "auto_retry": {"at": "x"}}}]
    devs = [dev("A", "hauler", "A-1"), dev("B", "mining_drone", "A-1", tags=["fleet:home"]), dev("C", "x", "A-1", tags=[])]
    assert metrics.roles(fleets, devs) == {"role:logistics": 1, "role:mining": 1, "role:none": 1}
    assert metrics.missions(fleets) == {"missions_stalled": 1, "missions_retrying": 1}
    g = metrics.pile_gains({"A-1": {"carbon": 10, "rares": 5}}, {"A-1": {"carbon": 25, "rares": 2}, "B-1": {"carbon": 99}}, {"A-1"})
    assert g == {"carbon": 15}


def test_printed_beacon_bound_elsewhere_is_delivered():
    # beacons come out of the printer monitoring (live 2026-10-23): one tagged to:<another system> still gets a carrier
    from rsweb import loadouts as lo
    stars = {"DEP": {"position": {"x": 0, "y": 0, "z": 0}}, "OTH": {"position": {"x": 1, "y": 0, "z": 0}}}
    devices = [
        {"device_code": "CV1", "device_type": "cargo_vessel", "location": "DEP-BELT-1", "status": "idle", "stow_capacity": 50,
         "features": ["surge", "cruise", "attach"], "available_commands": ["travel"]},
        {"device_code": "BK1", "device_type": "ftl_beacon", "location": "DEP-BELT-1", "status": "monitoring", "tags": ["to:oth"],
         "features": ["stow", "audit", "comms"], "available_commands": ["deactivate", "deploy", "stow", "decommission"]},
        {"device_code": "BK2", "device_type": "ftl_beacon", "location": "DEP-3", "status": "monitoring", "tags": ["to:dep"],
         "features": ["stow", "audit", "comms"], "available_commands": ["deactivate", "deploy", "stow", "decommission"]},
    ]
    assert not lo.placed(devices[1]) and lo.placed(devices[2])
    p = lo.plan({"phases": [], "systems": {}, "roles": {}}, devices, [], {}, stars, {}, set(), [], {})
    assert [(d["carrier"], d["devices"], d["to"]) for d in p["deliveries"]] == [("CV1", ["BK1"], "OTH")]


def test_logistics_keeps_an_escort_for_waiting_relays():
    from rsweb import loadouts as lo
    from rsweb import logistics as lg
    p = {"unmet": [{"star": "FAR", "type": "ftl_relay", "n": 3, "escort_from": "SOL", "why": "…"},
                   {"star": "FAR", "type": "mining_drone", "n": 1, "escort_from": "SOL", "why": "…"},
                   {"star": "X", "type": "ftl_relay", "n": 1, "why": "waiting for a surge-capable carrier"}]}
    assert lo.escort_wanted(p) == {"SOL": 3}
    pos = {"SOL": {"x": 0, "y": 0, "z": 0}, "NEAR": {"x": 5, "y": 0, "z": 0}, "FARAWAY": {"x": 500, "y": 0, "z": 0}}
    hv = lambda c, loc, **k: {"device_code": c, "device_type": "heaven_vessel", "location": loc, "status": "idle",  # noqa: E731
                             "stow_capacity": 10, "features": ["surge", "stow"], "available_commands": ["travel"],
                             "tags": ["fleet:log"], **k}
    devices = [hv("H1", "NEAR-3-L4"), hv("H2", "FARAWAY-1-L4"), hv("H3", "SOL-4-L4", status="travelling"),
               {**hv("C1", "SOL-4-L4"), "device_type": "cargo_freighter"}]   # no replicant aboard: no escort
    fleets = [{"id": "log", "role": "logistics", "name": "Logistics"}]
    hosts = {"H1": "R1", "H2": "R2", "H3": "R3"}
    assert lg.keep_escorts({"SOL": 3}, fleets, devices, hosts, set(), {}, pos, 100) == {"H1": "SOL"}
    assert lg.keep_escorts({"SOL": 3}, fleets, devices, hosts, {"H1"}, {}, pos, 100) == {}   # H2 is beyond the supply range


def _category_plan(lanes=None):
    pos = {**POS, "Y": {"x": 40, "y": 0, "z": 0}}
    devs = [dev("F1", "cargo_freighter", "A-1", cargo_capacity=100, **SURGE),
            dev("F2", "cargo_freighter", "A-1", cargo_capacity=100, **SURGE),
            dev("HV", "heaven_vessel", "A-1", **SURGE), dev("HV2", "heaven_vessel", "A-1", **SURGE),
            dev("SV", "survey_drone", "Z-5", tags=[], **SURGE)]
    deals = [{"designation": "E1", "location": "Z-2", "title": "One", "price": {"carbon": 50}},
             {"designation": "E2", "location": "Y-2", "title": "Two", "price": {"carbon": 50}}]
    return lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"A-1": {"carbon": 500}}, pos=pos, commandable={"A", "B"},
                   hosts={"HV": "R1", "HV2": "R2"}, busy=set(), reach=100, contracts=deals, trades=[], incoming={},
                   replicants={"R1": {"location": "A-1"}, "R2": {"location": "A-1"}}, drop=lambda s: f"{s}-1", lanes=lanes)


def test_each_task_category_gets_carriers_so_a_rescue_is_not_starved_by_deals():
    p = _category_plan()
    reqs = {r["id"]: r for r in p["requests"]}
    assert reqs["rescue:SV"]["category"] == "rescues" and reqs["contract:E1"]["category"] == "deals"
    done = {r["req"] for r in p["runs"]}
    assert "rescue:SV" in done                                    # before: both escorts went to the two contracts
    assert done & {"contract:E1", "contract:E2"}                  # the leftover round lends deals the other escort
    assert p["lanes"]["F1"] == p["lanes"]["F2"] == "deals" and "rescues" in p["lanes"].values()
    assert all(not k.startswith("_") for r in p["requests"] for k in r)


def test_a_carrier_keeps_its_category_while_it_has_work():
    p = _category_plan(lanes={"HV2": "rescues", "HV": "deals"})
    assert p["lanes"]["HV2"] == "rescues" and p["lanes"]["HV"] == "deals"
    assert next(r for r in p["runs"] if r["req"] == "rescue:SV")["escort"] == "HV2"
    p = _category_plan(lanes={"HV2": "repairs"})                  # no repair work: it's given a category that has some
    assert p["lanes"]["HV2"] in ("deals", "rescues")


def test_rescues_pause_for_everyone_or_one_fleet_and_a_timed_pause_lapses():
    """Owner 2026-10-26: take a fleet out of rescues for a while, or stop rescues altogether."""
    devs = [dev("HV", "heaven_vessel", "A-1", **SURGE),
            dev("P1", "surge_plate", "B-1", in_control_range=False, tags=["fleet:mine"], **SURGE),
            dev("P2", "surge_plate", "B-2", in_control_range=False, tags=["fleet:cab"], **SURGE)]
    now = datetime.now(timezone.utc)

    def go(raw):
        p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={}, pos=POS, commandable={"A"},
                    hosts={"HV": "R1"}, busy=set(), contracts=[], trades=[], incoming={}, replicants={},
                    drop=lambda s: f"{s}-1", rescue_pause=lg.rescue_pauses(raw))
        return {r["device"]: r for r in p["requests"] if r["kind"] == "rescue"}, {r.get("rescued") for r in p["runs"]}
    reqs, sent = go({"fleets": {"Mine": {"until": (now + timedelta(hours=6)).isoformat()}}})
    assert reqs["P1"]["paused"] == "its fleet" and reqs["P1"]["paused_until"] and "P1" not in sent
    assert not reqs["P2"].get("paused")
    reqs, sent = go({"all": {"until": None}})
    assert all(r["paused"] == "all rescues" for r in reqs.values()) and not sent - {None}
    reqs, _ = go({"all": {"until": (now - timedelta(minutes=1)).isoformat()}})   # lapsed: rescues go again
    assert not any(r.get("paused") for r in reqs.values())


def test_the_rescue_pause_form_pauses_and_resumes(client):  # noqa: F811
    db = client.app.state.db
    assert "6 h" in client.post("/fleets/logistics/rescue-pause", data={"hours": "6"}, headers=HX).text
    assert client.portal.call(db.kv_get, lg.KV_RESCUE_PAUSE)["all"]["until"]
    client.post("/fleets/logistics/rescue-pause", data={"fleet": "cab", "hours": ""}, headers=HX)
    p = lg.rescue_pauses(client.portal.call(db.kv_get, lg.KV_RESCUE_PAUSE))
    assert p["fleets"]["cab"]["until"] is None and p["all"]
    assert "All rescues paused" in client.get("/fleets/logistics", headers=H).text
    client.post("/fleets/logistics/rescue-pause", data={"resume": "1"}, headers=HX)
    client.post("/fleets/logistics/rescue-pause", data={"fleet": "cab", "resume": "1"}, headers=HX)
    assert lg.rescue_pauses(client.portal.call(db.kv_get, lg.KV_RESCUE_PAUSE)) == {"all": None, "fleets": {}}


def test_logistics_page_cancels_old_and_held_jobs(client):  # noqa: F811
    """Every running logistics job gets Cancel on the Logistics page, however old, also for a skipped request;
    a trip held for Confirm says so and offers Confirm."""
    db = client.app.state.db
    old = {"id": "lg-old", "rule": "logistics", "title": "logistics: prints at X-1: C02 carries 3 rares", "status": "waiting",
           "created_at": "2026-10-01T00:00:00+00:00", "idx": 0,
           "meta": {"logistics": {"req": "print:X-1"}},
           "steps": [{"desc": "C02 → X-1", "status": "confirm", "hold": "about 240 h of travel"}]}
    done = [{"id": f"lg-{i}", "rule": "logistics", "title": f"done {i}", "status": "done",
             "created_at": f"2026-10-20T00:{i:02d}:00+00:00", "idx": 1, "steps": [{"desc": "x", "status": "done"}]}
            for i in range(20)]
    client.portal.call(db.kv_set, "automation_jobs", done + [old])
    client.portal.call(db.kv_set, "logistics_state", {"requests": [
        {"id": "print:X-1", "kind": "print", "to": "X-1", "need": {}, "tier": 1, "base_tier": 1, "skipped": True,
         "label": "prints at X-1"}]})
    page = client.get("/fleets/logistics", headers=H).text
    assert "skipped, but" in page and "about 240 h of travel" in page
    assert page.count("/automations/jobs/lg-old/cancel") == 2 and "/automations/jobs/lg-old/confirm" in page
    client.post("/automations/jobs/lg-old/cancel", headers=HX)
    page = client.get("/fleets/logistics", headers=H).text
    assert "/automations/jobs/lg-old/cancel" not in page


def test_a_system_that_can_spare_it_all_goes_before_a_nearer_sliver():
    # live 2026-10-28: three freighters for one contract, two for 19 and 10 units from a nearer belt
    devs = [dev("F1", "cargo_freighter", "A-1", cargo_capacity=500, **SURGE),
            dev("F2", "cargo_freighter", "B-1", cargo_capacity=500, **SURGE),
            dev("HV", "heaven_vessel", "A-1", **SURGE)]
    c = {"designation": "EV1", "location": "Z-3", "title": "Big", "price": {"carbon": 180}}
    p = run_plan(devs, {"A-1": {"carbon": 1000}, "B-BELT": {"carbon": 10}}, [c])
    runs = [r for r in p["runs"] if r["req"] == "contract:EV1"]
    assert len(runs) == 1 and [g["pile"] for cg in runs[0]["cargos"] for g in cg["legs"]] == ["A-1"]
    p = run_plan(devs, {"A-1": {"carbon": 100}, "B-BELT": {"carbon": 100}}, [c])       # nowhere has it all: nearest first
    assert {g["pile"] for r in p["runs"] if r["req"] == "contract:EV1" for cg in r["cargos"] for g in cg["legs"]} >= {"B-BELT"}


def test_a_delivery_brings_a_stray_pile_back_instead_of_flying_home_empty():
    # Joe 2026-10-28: a lower priority run is better than a wasted trip
    devs = [dev("F1", "cargo_freighter", "A-1", cargo_capacity=500, **SURGE),
            dev("HV", "heaven_vessel", "A-1", **SURGE)]
    c = {"designation": "EV1", "location": "B-3", "title": "Local", "price": {"carbon": 50}}
    p = run_plan(devs, {"A-1": {"carbon": 1000}, "B-BELT": {"silicates": 300}}, [c])
    run = next(r for r in p["runs"] if r["req"] == "contract:EV1")
    assert run["backhaul"] == {"pile": "B-BELT", "to": "A-1", "takes": {"F1": {"silicates": 300}}}
    assert not any(r.get("req") == "return:B-BELT" and r.get("cargos") for r in p["runs"])   # not fetched twice
    steps = lg.run_steps(run, {d["device_code"]: d for d in devs})
    tail = [(s["desc"], (s["body"] or {}).get("command")) for s in steps if "way back" in s["desc"] or "deposit at A-1" in s["desc"]]
    assert [x[1] for x in tail] == ["travel", "collect_resources", "travel", "deposit_resources"]
    assert "on the way back 300 silicates from B-BELT → A-1" in lg.describe(run)


def test_the_way_back_never_takes_a_pile_a_request_delivers_to():
    # live 2026-10-28: RILXORAE-3 got 200 silicates and lost the 500 conductive left there for the same contract
    devs = [dev("F1", "cargo_freighter", "A-1", cargo_capacity=500, **SURGE),
            dev("HV", "heaven_vessel", "A-1", **SURGE)]
    c = {"designation": "EV1", "location": "B-3", "title": "Local", "price": {"carbon": 50, "silicates": 300}}
    p = run_plan(devs, {"A-1": {"carbon": 1000}, "B-3": {"silicates": 300}}, [c])
    run = next(r for r in p["runs"] if r["req"] == "contract:EV1")
    assert "backhaul" not in run


def test_a_deal_pile_is_not_a_source_for_other_deliveries():
    """Live 2026-10-28: trade runs to MENKUNT-2-L4 loaded carbon and volatiles at KAMUYIS-4, the pile of a contract there."""
    devs = world()
    c = {"designation": "EV4", "location": "B-2", "title": "Boom", "price": {"carbon": 50}}
    t = {"key": "SHOP:T1", "location": "A-2", "price": {"carbon": 30}, "controller": "SHOP", "trade_code": "T1", "name": "T1"}
    reqs, spare, _ = lg.requests(devices=devs, bps={}, inventory={"B-2": {"carbon": 60}}, pos=POS, contracts=[c],
                                 trades=[t], incoming={}, fleets=FLEETS, gather={}, busy_stars=set())
    assert spare["B-2"] == {"carbon": 10}   # only what the contract leaves over


def test_a_device_out_of_range_counts_at_its_pinned_deal_location():
    from rsweb import gameevents as gev
    d = {"device_type": "propulsor", "location": "ALKALAROPS", "in_control_range": False, "tags": ["at:alkalarops-oort"]}
    assert gev.at_place(d, "ALKALAROPS-OORT")
    assert not gev.at_place({**d, "in_control_range": True}, "ALKALAROPS-OORT")
    assert not gev.at_place({**d, "tags": []}, "ALKALAROPS-OORT")
