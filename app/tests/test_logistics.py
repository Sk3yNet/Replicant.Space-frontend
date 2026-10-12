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
    assert cargo(run) == ["F1"] and run["carry"] == {"carbon": 30} and not run["escort"]
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
    assert cargo(run) == ["F1"] and run["ride"] == "FC" and not run["escort"] and run["replicant"] == "R1" and run["fulfil"]
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


def test_freighters_ride_a_free_fleet_carrier_on_a_long_trip():
    devs = [dev("F1", "cargo_freighter", "A-1", cargo_capacity=100, **SURGE),
            dev("F2", "cargo_freighter", "A-1", cargo_capacity=100, **SURGE),
            dev("FC", "mobile_fleet", "A-2", **SURGE)]
    c = {"designation": "EV1", "location": "B-3", "title": "Near", "price": {"carbon": 150}}
    p = run_plan(devs, {"A-1": {"carbon": 1000}}, [c])
    run = next(r for r in p["runs"] if r["req"] == "contract:EV1")
    assert run["ride"] == "FC" and all(x["rides"] for x in run["cargos"]) and len(run["cargos"]) == 2
    cmds = [(s["path"].split("/")[-1], s["body"].get("command")) for s in lg.run_steps(run, {d["device_code"]: d for d in devs})
            if s["body"]]
    assert ("FC", "attach") in cmds and cmds.count(("FC", "detach")) == 2 and ("FC", "travel") in cmds
    assert cmds.index(("FC", "attach")) < cmds.index(("FC", "detach"))


def test_a_moved_fleets_old_stockpile_follows_it_to_the_new_home():
    devs = world()
    devs[2]["print_queue"], devs[2]["status"], devs[2]["printing"] = [], "idle", None
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"B-BELT": {"carbon": 90}}, pos=POS, commandable={"A", "B"},
                hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[], trades=[], incoming={}, replicants={},
                drop=lambda s: f"{s}-1", moved={"B": "Z"})
    r = next(x for x in p["requests"] if x["id"] == "return:B-BELT")
    assert r["to"] == "Z-1" and "new home" in r["label"]
