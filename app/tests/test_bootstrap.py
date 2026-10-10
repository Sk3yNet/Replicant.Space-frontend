import copy

from rsweb import bootstrap as bt
from test_ops import H, HX, client, eng  # noqa: F401  (fixture)

BPS = {
    "survey_drone": {"device_type": "survey_drone", "print_time": 300,
                     "resources": {"carbon": 10, "conductive": 30, "rares": 5, "silicates": 15, "structural": 60}},
    "mining_drone": {"device_type": "mining_drone", "print_time": 600,
                     "resources": {"carbon": 25, "conductive": 50, "silicates": 25, "structural": 100}},
    "autofactory": {"device_type": "autofactory", "print_time": 2400,
                    "resources": {"carbon": 150, "conductive": 400, "rares": 80, "silicates": 200, "structural": 800, "volatiles": 50}},
}
STRUCT = {"asteroid_belt": {"belts": [{"designation": "A-BELT-1", "density": "dense",
                                       "resources": {"structural": "rich", "conductive": "high", "carbon": "moderate",
                                                     "silicates": "moderate", "rares": "scarce", "volatiles": "scarce"}}]}}
RARES = {"asteroid_belt": {"belts": [{"designation": "B-BELT-1", "density": "dense",
                                      "resources": {"structural": "moderate", "conductive": "moderate", "carbon": "low",
                                                    "silicates": "low", "rares": "rich", "volatiles": "high"}}]}}


def scan_for(star, base):
    s = copy.deepcopy(base)
    s["asteroid_belt"]["belts"][0]["designation"] = f"{star}-BELT-1"
    return s


def world(devices, scans=None, inventory=None, warded=(), pos=None):
    pos = pos or {"HOME": {"x": 0, "y": 0, "z": 0}, "A": {"x": 3, "y": 0, "z": 0}, "B": {"x": 0, "y": 4, "z": 0},
                  "C": {"x": 6, "y": 0, "z": 0}, "D": {"x": 0, "y": 9, "z": 0}, "FAR": {"x": 40, "y": 0, "z": 0}}
    return {"devices": devices, "scans": scans or {}, "inventory": inventory or {}, "bps": BPS, "pos": pos,
            "warded": set(warded), "homes": set(), "reports": {}}


def fleet(stage="home", **kw):
    b = bt.new_state("V1", "R1", "HOME")
    b.update(stage=stage, **kw)
    return {"id": "p", "name": "Pioneer", "role": "bootstrap", "home": "HOME", "boot": b}


def vessel(loc, status="stationary"):
    return {"device_code": "V1", "device_type": "heaven_vessel", "location": loc, "status": status, "tags": ["boot:p"]}


def drone(code, t, loc=None, status="idle", aboard=False):
    d = {"device_code": code, "device_type": t, "status": "stowed" if aboard else status, "tags": ["boot:p"],
         "location": None if aboard else loc}
    if aboard:
        d["stowed_in_device_code"] = "V1"
    return d


def test_rares_rich_belts_score_high():
    from rsweb import prospects as pr
    a = pr.score("A", STRUCT, None, [], 3.0, True)
    b = pr.score("B", RARES, None, [], 3.0, True)
    assert b["parts"]["richness"] > a["parts"]["richness"] and "good rares" in b["reasons"]


def test_home_deploys_mines_and_prints():
    scans = {"HOME": scan_for("HOME", STRUCT)}
    f = fleet()
    devs = [vessel("HOME-BELT-1")] + [drone(f"M{i}", "mining_drone", aboard=True) for i in range(3)]
    ev = bt.evaluate(f, world(devs, scans))
    assert [a["kind"] for a in ev["actions"]][:3] == ["deploy"] * 3
    # deployed and idle: they start mining; no survey drone yet and no rares → the vessel mines rares
    devs = [vessel("HOME-BELT-1")] + [drone(f"M{i}", "mining_drone", "HOME-BELT-1") for i in range(3)]
    ev = bt.evaluate(f, world(devs, scans))
    kinds = [a["kind"] for a in ev["actions"]]
    assert kinds.count("start_mining") == 3 and {"kind": "vessel_mine", "resource": "rares"} in ev["actions"]
    assert "survey drone" in ev["next"]
    # stock covers it: print
    inv = {"HOME-BELT-1": {"carbon": 50, "conductive": 50, "rares": 6, "silicates": 50, "structural": 100}}
    ev = bt.evaluate(f, world(devs, scans, inv))
    assert {"kind": "print", "device_type": "survey_drone"} in ev["actions"]
    # the vessel first goes to the belt
    ev = bt.evaluate(f, world([vessel("HOME-3-L4")] + devs[1:], scans))
    assert ev["actions"] == [{"kind": "travel", "device": "V1", "to": "HOME-BELT-1"}]


def test_warded_home_goes_straight_to_survey_then_hub_choice():
    f = fleet()
    devs = [vessel("HOME-BELT-1")] + [drone(f"M{i}", "mining_drone", aboard=True) for i in range(3)]
    ev = bt.evaluate(f, world(devs, {}, warded={"HOME"}))
    assert f["boot"]["stage"] == "survey" and ev["actions"][0] == {"kind": "travel", "device": "V1", "to": "A", "visit": "A"}
    # everything within 10 ly scanned (FAR is out of range): the hub decision, rares-rich first
    scans = {x: scan_for(x, RARES if x == "B" else STRUCT) for x in ("A", "B", "C", "D")}
    ev = bt.evaluate(f, world([vessel("D-BELT-1")] + devs[1:], scans, warded={"HOME"}))
    assert f["boot"]["stage"] == "hub_choice" and ev["decision"]["kind"] == "hub"
    assert ev["decision"]["options"][0]["star"] == "B" and not ev["actions"]


def test_move_fetches_drones_then_flies_to_the_hub():
    scans = {x: scan_for(x, STRUCT) for x in ("HOME", "A")}
    f = fleet("move", hub="A")
    devs = [vessel("A-BELT-1")] + [drone(f"M{i}", "mining_drone", "HOME-BELT-1") for i in range(2)]
    ev = bt.evaluate(f, world(devs, scans))
    assert ev["actions"] == [{"kind": "travel", "device": "V1", "to": "HOME-BELT-1"}]
    devs[0] = vessel("HOME-BELT-1")
    ev = bt.evaluate(f, world(devs, scans))
    assert [a["kind"] for a in ev["actions"]] == ["stow", "stow"]
    devs = [vessel("HOME-BELT-1")] + [drone(f"M{i}", "mining_drone", aboard=True) for i in range(2)]
    ev = bt.evaluate(f, world(devs, scans))
    assert ev["actions"] == [{"kind": "travel", "device": "V1", "to": "A"}]
    devs[0] = vessel("A-5-L4")
    ev = bt.evaluate(f, world(devs, scans))
    assert f["boot"]["stage"] == "hub_compound" and ev["actions"][0]["to"] == "A-BELT-1"


def test_hub_autofactory_then_outposts_keep_the_relay_chain():
    scans = {x: scan_for(x, STRUCT) for x in ("A", "B", "C", "D")}
    f = fleet("hub_compound", hub="A")
    devs = [vessel("A-BELT-1"), drone("AF", "autofactory", "A-BELT-1")]
    ev = bt.evaluate(f, world(devs, scans))
    assert ev["actions"] == [{"kind": "make_hub", "factory": "AF"}]
    f = fleet("outposts", hub="A")
    f["boot"]["settings"]["min_score"] = 0
    ev = bt.evaluate(f, world(devs, scans))
    opts = {o["star"] for o in ev["decision"]["options"]}
    assert ev["decision"]["kind"] == "outpost" and opts == {"B", "C"}       # D is 9.5 ly from A: beyond a relay's 7.5
    f = fleet("outposts", hub="A", outposts=[{"star": "B", "fleet": "x"}, {"star": "C", "fleet": "y"}])
    f["boot"]["settings"]["min_score"] = 0
    ev = bt.evaluate(f, world(devs, scans))
    assert ev["decision"]["kind"] == "outpost" and ev["decision"]["options"][0]["star"] == "D"   # 5 ly from B: in the chain


def test_bootstrap_fleet_create_decide_and_card(client):
    db = client.app.state.db
    reps = client.portal.call(db.kv_get, "replicants")
    code = next(iter(reps))
    r = client.post("/fleets/bootstrap", data={"name": "Pioneer", "replicant": code}, headers=HX)
    assert r.status_code == 200
    e = eng(client)
    f = next(x for x in client.portal.call(e.fleets) if x["role"] == "bootstrap")
    assert f["boot"]["vessel"] and f["boot"]["stage"] == "home"
    page = client.get("/fleets", headers=H).text
    assert "Pioneer" in page and "Costs" in page and "Gate:" in page
    client.portal.call(e.bootstrap_pass)
    f = next(x for x in client.portal.call(e.fleets) if x["role"] == "bootstrap")
    assert f["boot"].get("status", {}).get("stage")
    # a decision
    items = client.portal.call(e.fleets)
    b = next(x for x in items if x["role"] == "bootstrap")["boot"]
    b.update(stage="hub_choice", decision={"kind": "hub", "options": [{"star": "SOL", "score": 50}]})
    client.portal.call(e.save_fleets, items)
    r = client.post(f"/fleets/{f['id']}/bootstrap", data={"action": "decide", "kind": "hub", "pick": "SOL"}, headers=HX)
    assert "hub: SOL" in r.text
    f = next(x for x in client.portal.call(e.fleets) if x["role"] == "bootstrap")
    assert f["boot"]["hub"] == "SOL" and f["boot"]["stage"] == "move" and not f["boot"]["decision"]


def _setup(client, stage, devices, **boot):
    e, db = eng(client), client.app.state.db
    client.get("/systems/SOL", headers=H)   # SOL's scan stored
    b = bt.new_state("V1", "77F75255", "SOL")
    b.update(stage=stage, **boot)
    client.portal.call(e.save_fleets, [{"id": "p", "name": "Pioneer", "role": "bootstrap", "home": "SOL", "boot": b}])
    client.portal.call(db.kv_set, "devices", devices)
    return e


def test_pass_turns_actions_into_one_job(client):
    devs = [vessel("SOL-BELT-1")] + [drone(f"M{i}", "mining_drone", aboard=True) for i in range(3)]
    e = _setup(client, "home", devs)
    client.portal.call(e.bootstrap_pass)
    jobs = [j for j in client.portal.call(e.jobs) if (j.get("meta") or {}).get("bootstrap") == "p"]
    assert len(jobs) == 1 and [s["body"]["command"] for s in jobs[0]["steps"][:3]] == ["deploy"] * 3
    all_jobs = client.portal.call(e.jobs)
    for j in all_jobs:
        if (j.get("meta") or {}).get("bootstrap") == "p":
            j["status"] = "waiting"
    client.portal.call(e.save_jobs, all_jobs)
    client.portal.call(e.bootstrap_pass)    # its job still runs: nothing more
    assert len([j for j in client.portal.call(e.jobs) if (j.get("meta") or {}).get("bootstrap") == "p"]) == 1


def test_autofactory_makes_the_hub_fleet(client):
    devs = [vessel("SOL-BELT-1"), drone("AF", "autofactory", "SOL-BELT-1"), drone("M1", "mining_drone", "SOL-BELT-1", "mining (carbon)")]
    e = _setup(client, "hub_compound", devs, hub="SOL")
    client.portal.call(e.bootstrap_pass)
    fleets = client.portal.call(e.fleets)
    hub = next(f for f in fleets if f.get("parent") == "p")
    boot = next(f for f in fleets if f["id"] == "p")["boot"]
    assert hub["materials"] == "self" and hub["station"] and hub["family"] == "p" and hub["home"] == "SOL"
    assert hub["wants"]["autofactory"] == 1 and hub["wants"]["ftl_relay"] == 1 and boot["stage"] == "hub"


def test_ward_when_others_mine_there(client):
    from rsweb import others as oth
    devs = [vessel("SOL-BELT-1")]
    e = _setup(client, "hub", devs, hub="SOL", children={"hub": "p-hub"})
    fleets = client.portal.call(e.fleets) + [{"id": "p-hub", "name": "Pioneer · Hub", "role": "mining", "home": "SOL",
                                              "station": True, "wants": {"mining_drone": 2}, "family": "p", "parent": "p"}]
    client.portal.call(e.save_fleets, fleets)
    s = oth.normalize({})
    oth.record_scan(s, "SOL", [{"device_code": "X1", "device_type": "mining_drone", "location": "SOL-BELT-1",
                                "owner_replicant_code": "OTHER"}], {"77F75255"})
    client.portal.call(client.app.state.db.kv_set, oth.KV, s)
    client.portal.call(e.bootstrap_pass)
    hub = next(f for f in client.portal.call(e.fleets) if f["id"] == "p-hub")
    assert hub["wants"].get("system_ward") == 1


def test_loadouts_keep_a_bootstrap_self_reliant():
    from rsweb import loadouts as lo
    from test_core import _lo_world
    cfg, devices, bps, inv, stars = _lo_world()
    # BBB belongs to a bootstrap: it may not take CCC's ordinary spare; its own spare isn't lent to AAA either
    cfg = {**cfg, "fleets": [{"id": "bb", "name": "BB", "role": "mining", "home": "BBB", "station": True,
                              "wants": {"ami_mining_controller": 1}, "family": "p"}]}
    devices = [d for d in devices if d["device_code"] != "BS"]
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    assert (p.get("assign") or {}).get("CC") != "fleet:bb"          # the ordinary spare isn't sent to the bootstrap's fleet
    assert not any(pr.get("fleet") == "bb" and pr["factory"] == "AF" for pr in p["prints"])   # nor printed on others' factory


def test_controllers_in_the_kits_and_their_directives(client):
    assert bt.HUB_KIT["ami_mining_controller"] == 1 and bt.OUTPOST_KIT["ami_survey_controller"] == 1
    r = bt.ratios({"autofactory": 1}, BPS)
    assert abs(sum(r.values()) - 1) < 0.02 and r["structural"] > r["rares"] > 0
    devs = [vessel("SOL-BELT-1"),
            {"device_code": "MC", "device_type": "ami_mining_controller", "location": "SOL-BELT-1", "status": "coordinating",
             "tags": ["boot:p", "fleet:p-hub"]},
            {"device_code": "SC", "device_type": "ami_survey_controller", "location": "SOL-BELT-1", "status": "idle",
             "tags": ["boot:p", "fleet:p-hub"]}]
    e = _setup(client, "hub", devs, hub="SOL", children={"hub": "p-hub"})
    fleets = client.portal.call(e.fleets) + [{"id": "p-hub", "name": "Pioneer · Hub", "role": "mining", "home": "SOL",
                                              "station": True, "wants": {"ami_mining_controller": 1, "ami_survey_controller": 1,
                                                                         "mining_drone": 2}, "family": "p", "parent": "p"}]
    client.portal.call(e.save_fleets, fleets)
    client.portal.call(e.bootstrap_pass)
    job = next(j for j in client.portal.call(e.jobs) if j["title"].endswith("controller directives"))
    bodies = {s["path"]: s["body"] for s in job["steps"]}
    assert bodies["/devices/SC"]["directive"] == "belt_search"
    assert bodies["/devices/MC"]["directive"] == "maintain_ratios" and set(bodies["/devices/MC"]["configuration"]) <= set(bt.RESOURCES)


def test_controllers_on_salvage_or_resting_are_left_to_the_salvage_rule(client):
    devs = [vessel("SOL-BELT-1"),
            {"device_code": "MC", "device_type": "ami_mining_controller", "location": "SOL-BELT-1", "status": "coordinating",
             "tags": ["boot:p", "fleet:p-hub"], "ami_directive": {"name": "gather_salvage"}},
            {"device_code": "MR", "device_type": "ami_mining_controller", "location": "SOL-BELT-1", "status": "idle",
             "tags": ["boot:p", "fleet:p-hub"]}]
    e = _setup(client, "hub", devs, hub="SOL", children={"hub": "p-hub"})
    client.portal.call(client.app.state.db.kv_set, "rested", {"MR": {"belt": "SOL-BELT-1"}})
    fleets = client.portal.call(e.fleets) + [{"id": "p-hub", "name": "Pioneer · Hub", "role": "mining", "home": "SOL",
                                              "station": True, "wants": {"ami_mining_controller": 2}, "family": "p", "parent": "p"}]
    client.portal.call(e.save_fleets, fleets)
    client.portal.call(e.bootstrap_pass)
    assert not [j for j in client.portal.call(e.jobs) if j["title"].endswith("controller directives")]
    log = " ".join(x["text"] for x in next(f for f in client.portal.call(e.fleets) if f["id"] == "p")["boot"]["log"])
    assert "MC (SOL): on salvage" in log and "MR (SOL): resting" in log


def test_convert_a_fleet_to_a_bootstrap(client):
    db, e = client.app.state.db, eng(client)
    reps = client.portal.call(db.kv_get, "replicants")
    code = next(iter(reps))
    vessel_code = reps[code]["hosted_device_code"]
    devices = client.portal.call(db.kv_get, "devices")
    for d in devices:
        if d["device_code"] == vessel_code or d.get("device_type") == "mining_drone":
            d["tags"] = list(d.get("tags") or []) + ["fleet:old"]
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(e.save_fleets, [{"id": "old", "name": "Old", "role": "mining", "home": "SOL", "station": True,
                                        "wants": {"mining_drone": 3}},
                                       {"id": "x", "name": "X", "role": "mining", "home": "ABOTEIN", "materials": "old"}])
    r = client.post("/fleets/old/to-bootstrap", headers=HX)
    assert r.status_code == 200 and "err" not in r.text
    fleets = client.portal.call(e.fleets)
    f = next(x for x in fleets if x["id"] == "old")
    assert f["role"] == "bootstrap" and not f["station"] and f["boot"]["vessel"] == vessel_code and f["boot"]["replicant"] == code
    assert next(x for x in fleets if x["id"] == "x")["materials"] == ""
    job = next(j for j in client.portal.call(e.jobs) if "converted to a bootstrap" in j["title"])
    assert all(s["body"]["configuration"]["add_tags"] == ["boot:old"] for s in job["steps"])
    # a fleet without a replicant's vessel can't be converted
    client.portal.call(e.save_fleets, fleets + [{"id": "bare", "name": "Bare", "role": "mining", "home": "SOL"}])
    assert "heaven vessel hosting a replicant" in client.post("/fleets/bare/to-bootstrap", headers=HX).text


def _range_world():
    def dev(code, t, loc, **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": "idle", "operational_capacity": 100.0,
                "features": kw.pop("features", ["cruise", "stow"]), "available_commands": kw.pop("cmds", ["travel", "stow", "deploy"]), **kw}
    stars = {"AAA": {"designation": "AAA", "position": {"x": 0, "y": 0, "z": 0}},
             "BBB": {"designation": "BBB", "position": {"x": 3, "y": 0, "z": 0}},
             "FAR": {"designation": "FAR", "position": {"x": 500, "y": 0, "z": 0}}}
    bps = [{"device_type": "mining_drone", "resources": {"structural": 100}, "print_time": 180},
           {"device_type": "ami_mining_controller", "resources": {"structural": 100}, "print_time": 600}]
    return dev, stars, bps


def test_borrowed_carriers_stay_within_range_own_ones_may_cross():
    from rsweb import loadouts as lo
    dev, stars, bps = _range_world()
    devices = [dev("M1", "mining_drone", "BBB-BELT-1", tags=["fleet:aaa"]),
               dev("CAR", "surge_carrier", "FAR-OORT", features=["surge", "cruise"], cmds=["travel", "deploy"], attach_capacity=9)]
    cfg = {"fleets": [{"id": "aaa", "name": "A", "role": "mining", "home": "AAA", "station": True, "wants": {"mining_drone": 1}}],
           "settings": {"max_supply_ly": 100}}
    p = lo.plan(cfg, devices, bps, {}, stars, {}, set(), [], {})
    assert not p["deliveries"] and any("within 100 ly" in u["why"] for u in p["unmet"])   # 500 ly away, borrowed: no
    devices[1]["tags"] = ["fleet:aaa"]                                                  # the fleet's own: may come
    cfg["fleets"][0]["wants"]["surge_carrier"] = 1
    p = lo.plan(cfg, devices, bps, {}, stars, {}, set(), [], {})
    assert any(d["carrier"] == "CAR" and "M1" in d["devices"] for d in p["deliveries"])


def test_supplied_from_prints_at_any_distance():
    from rsweb import loadouts as lo
    dev, stars, bps = _range_world()
    af = dev("AF", "autofactory", "FAR-3-L4", features=["print"], cmds=["enqueue_print"], tags=["fleet:hub"])
    cfg = {"fleets": [{"id": "hub", "name": "Hub", "role": "mining", "home": "FAR", "station": True, "wants": {"autofactory": 1}},
                      {"id": "bb", "name": "B", "role": "mining", "home": "BBB", "station": True, "wants": {"ami_mining_controller": 1}}],
           "settings": {"max_supply_ly": 100, "print_missing": True, "need_stock": False}}
    inv = {"FAR-3-L4": {"structural": 1000.0}}
    p = lo.plan(cfg, [af], bps, inv, stars, {}, set(), [], {})
    assert not any(pr.get("fleet") == "bb" for pr in p["prints"])                      # 500 ly: out of range
    cfg["fleets"][1]["supplied_from"] = "hub"
    p = lo.plan(cfg, [af], bps, inv, stars, {}, set(), [], {})
    assert any(pr.get("fleet") == "bb" and pr["factory"] == "AF" for pr in p["prints"])


def test_back_to_a_normal_fleet(client):
    devs = [vessel("SOL-BELT-1"), drone("M1", "mining_drone", "ABOTEIN-BELT-1"), drone("M2", "mining_drone", "ABOTEIN-BELT-1"),
            drone("S1", "survey_drone", "SOL-BELT-1")]
    e = _setup(client, "home", devs)
    r = client.post("/fleets/p/bootstrap", data={"action": "normal"}, headers=HX)
    assert r.status_code == 200
    f = next(x for x in client.portal.call(e.fleets) if x["id"] == "p")
    assert f["role"] == "mining" and f["station"] and f["home"] == "ABOTEIN" and "boot" not in f
    assert f["wants"] == {"mining_drone": 2, "survey_drone": 1}
    job = next(j for j in client.portal.call(e.jobs) if "back to a normal fleet" in j["title"])
    assert all(s["body"]["configuration"] == {"add_tags": ["fleet:p"], "remove_tags": ["boot:p"]} for s in job["steps"])
