"""Core tests: mining rules, sites, salvage and drones (split out of test_core.py by test name)."""
from corelib import *  # noqa: F401,F403


def test_maintenance_drone_gets_patrol(client):
    from rsweb import commands as c
    # blueprint says patrol, even if the device type name doesn't match our built-in kinds
    assert c.directives_for({"device_type": "repair_bot"}, [{"device_type": "repair_bot", "directives": ["patrol"]}]) == ["patrol"]
    assert c.directives_for({"device_type": "maintenance_drone"}, []) == ["patrol"]
    world = client.app.state.api.http._transport.app.state.world
    world.devices.append({"device_code": "MD000001", "device_type": "maintenance_drone", "location": "SOL-BELT-1",
                          "status": "idle", "features": ["cruise", "repair", "ami", "stow"], "replicant_code": "77F75255",
                          "available_commands": ["set_directive", "clear_directive", "launch", "travel"]})
    client.portal.call(client.app.state.worker.sync_devices)
    r = client.get("/devices/MD000001/command-form?command=set_directive", headers=HX)
    assert '<option value="patrol">' in r.text
    r = client.post("/devices/MD000001/command", data={"command": "set_directive", "directive": "patrol"}, headers=HX)
    assert "result ok" in r.text and "directive patrol" in r.text
    assert '<option value="patrol">' in client.get("/ami", headers=H).text


def test_restart_idle_miners(client):
    eng = client.app.state.worker.automations
    client.post("/automations/rules/restart_idle_miners", data={"enabled": "on", "resource": "carbon", "cooldown_minutes": "10"}, headers=HX)
    client.portal.call(eng.tick)
    acts = client.portal.call(client.app.state.db.fetchall, "SELECT path, body FROM actions WHERE user='automation'")
    assert any('"start_mining"' in a["body"] and '"carbon"' in a["body"] and a["path"] == "/devices/2AC61212" for a in acts)
    n = len(acts)
    client.portal.call(eng.tick)  # cooldown: no second attempt
    assert len(client.portal.call(client.app.state.db.fetchall, "SELECT 1 FROM actions WHERE user='automation'")) == n



def test_restart_idle_miners_focus_one_site(client):
    eng = client.app.state.worker.automations
    db = client.app.state.db
    loc = client.portal.call(eng.devices)
    belt = next(d["location"] for d in loc if d["device_code"] == "2AC61212")
    world = client.app.state.api.http._transport.app.state.world
    world.belt_sites = {belt: [
        {"designation": f"{belt}-SITE-1", "resources_remaining_pct": {"carbon": 80, "structural": 10}},
        {"designation": f"{belt}-SITE-2", "resources_remaining_pct": {"carbon": 30, "structural": 100}},
        {"designation": f"{belt}-SITE-3", "resources_remaining_pct": {"carbon": 0, "structural": 5}}]}
    client.post("/automations/rules/restart_idle_miners", data={"enabled": "on", "resource": "carbon", "cooldown_minutes": "10",
                                                                 "focus_sites": "on"}, headers=HX)
    client.portal.call(eng.tick)
    acts = client.portal.call(db.fetchall, "SELECT path, body FROM actions WHERE user='automation' AND path='/devices/2AC61212'")
    assert any(f'"target": "{belt}-SITE-2"' in a["body"] for a in acts), acts


def test_focus_site_picks_the_least_left():
    from rsweb.sites import focus_site
    detail = {"resource_sites": [{"designation": "A-BELT-1-SITE-1", "resources_remaining_pct": {"carbon": 40}},
                                 {"designation": "A-BELT-1-SITE-2", "resources_remaining_pct": {"carbon": 40, "rares": 0}},
                                 {"designation": "A-BELT-1-SITE-3", "resources_remaining_pct": {"carbon": 10, "rares": 90}},
                                 {"designation": "A-1-SAL-1", "site_type": "salvage", "resources_remaining_pct": {"carbon": 1}}]}
    assert focus_site(detail, "carbon") == "A-BELT-1-SITE-3"
    assert focus_site(detail, "carbon", {"A-BELT-1-SITE-3"}) == "A-BELT-1-SITE-1"
    assert focus_site(detail, "volatiles") is None and focus_site(None, "carbon") is None


def test_replicant_travel_chain_launches_carried_drone(client):
    world = client.app.state.api.http._transport.app.state.world
    world.move_seconds = 0.03
    client._seen = len(world.events)
    r = client.post("/replicants/77F75255/travel", data={"destination": "ABOTEIN", "dry_run": "1"}, headers=HX)
    assert "Then, on arrival" in r.text and "SV000001" in r.text  # carried drone offered
    # dry run (the Automations switch) must not block a chain the player asked for
    client.post("/automations/dry-run", data={"dry_run": "on"}, headers=HX)
    r = client.post("/replicants/77F75255/travel", data={
        "destination": "ABOTEIN",
        "then_device_0": "SV000001", "then_command_0": "deploy",
        "then_device_1": "SV000001", "then_command_1": "scan",
        "then_device_2": "__self__", "then_command_2": "system_scan"}, headers=HX)
    assert "follow on Automations" in r.text, r.text
    eng = client.app.state.worker.automations

    async def finished():
        return all(j["status"] not in ("running", "waiting") for j in await eng.jobs())
    _pump(client, finished)
    job = client.portal.call(eng.jobs)[-1]
    # arrival reported at ABOTEIN-3-L4 still counts as arriving "at ABOTEIN"
    assert job["status"] == "done" and all(s["status"] == "done" for s in job["steps"]), \
        [(s["desc"], s["status"], s["error"]) for s in job["steps"]]
    assert [s["path"] for s in job["steps"]] == ["/replicants/77F75255/travel", "/devices/SV000001",
                                                  "/devices/SV000001", "/replicants/77F75255/scan"]


def test_planner_without_mining_controller_warns():
    from rsweb.production import production_steps
    steps = production_steps("AF1", "autofactory AF1", [("mining_drone", 2)], {"rares": 10}, None, None, "X-3-L4", True)
    assert [s["body"]["command"] for s in steps] == ["enqueue_print"]


def test_print_needs_tilt_drones_toward_what_waiting_prints_lack():
    from rsweb import prospects as pr
    short = pr.print_short({"silicates": 100, "carbon": 900}, [{"silicates": 400, "carbon": 50}])
    assert short["silicates"] > 0.8 and short["carbon"] == 0
    scan = {"asteroid_belt": {"belts": [{"designation": "X-BELT-1", "resources": {"silicates": "low", "carbon": "rich"}}]}}
    assert pr.belt_resources(scan, "X-BELT-1") == ["carbon", "silicates"]
    r = pr.print_ratios(["carbon", "silicates"], short)
    assert r["silicates"] > 0.7 and r["carbon"] < 0.3
    assert pr.print_ratios(["carbon"], short) is None        # the belt has none of it: gather_evenly as before


def test_idle_miners_handed_to_mining_controller(client):
    eng = client.app.state.worker.automations
    client.post("/automations/rules/restart_idle_miners", data={"enabled": "on", "prefer_ami": "on", "resource": "same",
                                                                "cooldown_minutes": "10"}, headers=HX)
    client.portal.call(eng.tick)
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "restart_idle_miners"][-1]
    assert job["steps"][0]["body"] == {"command": "adopt", "devices": ["2AC61212"]}
    assert len(job["steps"]) == 1  # controller is coordinating: adopt only, no relaunch
    acts = client.portal.call(client.app.state.db.fetchall, "SELECT body FROM actions WHERE user='automation'")
    assert not any('"start_mining"' in (a["body"] or "") for a in acts)


def test_site_quantity_shapes():
    from rsweb.targets import site_quantity
    assert site_quantity({"resource_type": "carbon", "quantity": 120}) == ({"carbon": 120.0}, 120.0)
    assert site_quantity({"resource": "rares", "remaining": "40"}) == ({"rares": 40.0}, 40.0)
    assert site_quantity({"resources": {"structural": 10, "carbon": 5}}) == ({"structural": 10.0, "carbon": 5.0}, 15.0)
    assert site_quantity({"resources": [{"resource": "silicates", "quantity": 7}]}) == ({"silicates": 7.0}, 7.0)
    assert site_quantity({"resource_type": "carbon"}) == ({}, None)


def test_salvage_rule_switches_ami_when_belt_worked_out(client):
    world, eng = _salvage_setup(client)
    assert client.portal.call(eng.rule_salvage) == []          # sites still have stock: nothing to do
    for i, site in enumerate(("SOL-BELT-1-SITE-1", "SOL-BELT-1-SITE-2")):
        client.portal.call(client.app.state.worker.handle_event, _ev(200 + i, "site.depleted", site=site))
    jobs = [j for j in client.portal.call(eng.jobs) if j["rule"] == "salvage_when_depleted"]
    assert len(jobs) == 1 and jobs[0]["device"] == "MC91FF22"
    body = next(s["body"] for s in jobs[0]["steps"] if (s["body"] or {}).get("command") == "set_directive")
    assert body == {"command": "set_directive", "directive": "gather_salvage",
                    "configuration": {"location": "SOL-3-1", "recall": True}}    # the body, not the -SAL- code; recall brings drones back
    # no drones are sent on their own while the AMI handles the system
    assert not any(j["device"].startswith("2AC6121") for j in jobs)


def test_salvage_rule_sends_drones_without_ami(client):
    world, eng = _salvage_setup(client, drop_controller=True)
    for i, site in enumerate(("SOL-BELT-1-SITE-1", "SOL-BELT-1-SITE-2")):
        client.portal.call(client.app.state.worker.handle_event, _ev(300 + i, "site.depleted", site=site))
    jobs = [j for j in client.portal.call(eng.jobs) if j["rule"] == "salvage_when_depleted"]
    assert [j["device"] for j in jobs] == ["2AC61212"]          # the idle drone at the worked-out belt
    bodies = [s["body"] for s in jobs[0]["steps"]]
    assert bodies[0] == {"command": "travel", "destination": "SOL-3-1"}
    assert bodies[-1] == {"command": "start_mining", "resource_type": "structural"}
    # the idle-miner rule leaves drones at the worked-out belt alone
    acts = client.portal.call(client.app.state.db.fetchall, "SELECT body FROM actions WHERE body LIKE '%start_mining%'")
    client.portal.call(eng.rule_restart_idle_miners)
    after = client.portal.call(client.app.state.db.fetchall, "SELECT body FROM actions WHERE body LIKE '%start_mining%'")
    assert len(after) == len(acts)


def test_salvage_helpers():
    from rsweb import salvage as sv
    res = {"sites": [{"code": "X-BELT-1-SITE-1", "belt": "X-BELT-1", "depleted": True, "total": 50},
                     {"code": "X-BELT-2-SITE-1", "belt": "X-BELT-2", "depleted": False, "total": 10},
                     {"code": "X-BELT-2-SITE-2", "belt": "X-BELT-2", "depleted": False, "total": 0}],
           "salvage": [{"code": "X-1-SAL-1", "depleted": False, "total": 10, "amounts": {"carbon": 10}},
                       {"code": "X-2-SAL-1", "depleted": False, "total": 90, "amounts": {"rares": 60, "carbon": 30}},
                       {"code": "X-3-SAL-1", "depleted": True, "total": 500}]}
    assert sv.worked_out(res) == {"X-BELT-1"}
    assert [s["code"] for s in sv.available_salvage(res)] == ["X-2-SAL-1", "X-1-SAL-1"]
    assert sv.main_resource(sv.available_salvage(res)[0]) == "rares"
    assert sv.at_worked_out_place("X-BELT-1-SITE-1", {"X-BELT-1"}, set())
    assert sv.body_of("AEMEROTH-6-7-SAL-1") == "AEMEROTH-6-7" and sv.body_of("AEMEROTH-6-7") == "AEMEROTH-6-7"


def test_reopen_sites_with_drones_only_and_tracking_drones_stay(client):
    eng = _enable(client, "reopen_sites")
    devices = [{"device_code": "SD1", "device_type": "survey_drone", "location": "FAL-1", "status": "idle"},
               {"device_code": "SD2", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "tracking"}]
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(client.app.state.db.kv_set, "exhausted_places", {"FAL-BELT-1": __import__("rsweb.db", fromlist=["x"]).now_iso()})
    assert client.portal.call(eng.rule_reopen_sites) == ["SD1 → search FAL-BELT-1"]
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "reopen_sites"][-1]
    assert [s["body"] for s in job["steps"]] == [{"command": "travel", "destination": "FAL-BELT-1"}, {"command": "search"}]
    # loadouts never marks a tracking drone spare or moves it
    from rsweb import loadouts as lo
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"survey_drone": 0}}], "systems": {"FAL": "p"}}
    p = lo.plan(cfg, devices, [], {}, {}, {}, set(), [], {})
    assert "spare" in p["tag_add"].get("SD1", []) and "spare" not in p["tag_add"].get("SD2", [])


def test_used_up_salvage_and_closed_sites_are_hidden(client):
    from rsweb.targets import system_resources, system_targets
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    for ev in [e for e in world.events if e["event"] == "salvage.discovered"]:
        client.portal.call(client.app.state.worker.handle_event, dict(ev))
    client.get("/systems/SOL", headers=H)
    client.post("/systems/SOL/resources/refresh", headers=HX)
    db = client.app.state.db
    # salvage used up, one site depleted
    client.portal.call(client.app.state.worker.handle_event, _ev(401, "salvage.depleted", site="SOL-3-1-SAL-1", location="SOL-3-1"))
    client.portal.call(client.app.state.worker.handle_event, _ev(402, "site.depleted", site="SOL-BELT-1-SITE-2"))
    # a site we only know from an old mining event, which the belt's latest detail no longer lists → closed
    client.portal.call(client.app.state.worker.handle_event, _ev(403, "mining.started", site="SOL-BELT-1-SITE-9", resource_type="carbon"))
    res = client.portal.call(system_resources, db, "SOL")
    assert {"SOL-3-1-SAL-1", "SOL-BELT-1-SITE-2", "SOL-BELT-1-SITE-9"} <= res["hidden"]
    assert [x["code"] for x in res["sites_shown"]] == ["SOL-BELT-1-SITE-1"] and res["salvage_shown"] == []
    codes = {t["code"] for t in client.portal.call(system_targets, db, "SOL")["targets"]}
    assert "SOL-3-1-SAL-1" not in codes and "SOL-BELT-1-SITE-2" not in codes and "SOL-BELT-1" in codes
    page = client.get("/systems/SOL", headers=H).text
    assert "SOL-3-1-SAL-1" not in page and "SOL-BELT-1-SITE-9" not in page and "hidden" in page
    # the salvage rule still knows it's used up
    assert any(x["code"] == "SOL-3-1-SAL-1" and x["depleted"] for x in res["salvage"])


def test_refresh_without_a_scan_reads_the_star_and_salvage_bodies(client):
    db = client.app.state.db
    client.portal.call(db.execute, "DELETE FROM systems WHERE star=?", ("SOL",))
    r = client.post("/systems/SOL/resources/refresh", headers=HX)
    assert "read 1 belt(s): 2 open mining site(s)" in r.text
    row = client.portal.call(db.fetchone, "SELECT data FROM systems WHERE star=?", ("SOL",))
    assert row and "asteroid_belt" in row["data"]           # GET /locations/SOL stood in for the missing scan


def test_body_salvage_percentages_and_used_up():
    import asyncio
    from rsweb.targets import system_resources

    class FakeDB:
        def __init__(self, kv, events):
            self.kv, self.events = kv, events
        async def fetchall(self, q, args=()):
            if "FROM events" in q:
                return self.events
            if "FROM kv" in q:
                return [{"key": k, "value": json.dumps(v), "updated_at": "2026-10-02T10:00:00"} for k, v in self.kv.items()]
            return []
        async def fetchone(self, q, args=()):
            return None
        async def kv_get(self, k, default=None):
            return default
    body = {"location": "KELMONENT-1", "location_type": "planet", "resource_sites": [
        {"designation": "KELMONENT-1-SAL-2", "name": "Orbital Debris Field", "site_type": "salvage", "site_index": 1,
         "resources_remaining_pct": {"conductive": 100, "structural": 100}},
        {"designation": "KELMONENT-1-SAL-1", "name": "Ejected Instrument Package", "site_type": "salvage", "site_index": 0,
         "resources_remaining_pct": {"rares": 100, "silicates": 100, "volatiles": 100}}]}
    events = [{"event": "salvage.discovered", "created_at": "2026-10-01T00:00:00", "payload": json.dumps(
                   {"designation": "KELMONENT-1-SAL-2", "resources": {"conductive": 40, "structural": 120}})},
              {"event": "salvage.discovered", "created_at": "2026-10-01T00:00:00", "payload": json.dumps(
                   {"designation": "KELMONENT-1-SAL-3", "resources": {"carbon": 50}})}]   # no longer listed: used up
    res = asyncio.run(system_resources(FakeDB({"loc:KELMONENT-1": body}, events), "KELMONENT"))
    shown = {x["code"]: x for x in res["salvage_shown"]}
    assert set(shown) == {"KELMONENT-1-SAL-1", "KELMONENT-1-SAL-2"} and "KELMONENT-1-SAL-3" in res["hidden"]
    assert shown["KELMONENT-1-SAL-2"]["amounts"] == {"conductive": 40, "structural": 120} and shown["KELMONENT-1-SAL-2"]["total"] == 160
    assert shown["KELMONENT-1-SAL-1"]["amounts"] == {} and shown["KELMONENT-1-SAL-1"]["total"] is None   # % known, amounts not
    assert res["sites_shown"] == []                         # salvage isn't counted as mining sites


def test_diagnostics_snapshot_and_mining_diagnosis(client):
    import asyncio
    db = client.app.state.db
    assert "No snapshot yet" in client.get("/diagnostics", headers=H).text
    r = client.post("/diagnostics/snapshot", data={"stars": ""}, headers=HX)
    assert r.headers.get("HX-Refresh")
    for _ in range(100):
        st = client.portal.call(db.kv_get, "snapshot_status", {})
        if st.get("state") != "running":
            break
        client.portal.call(asyncio.sleep, 0.05)
    assert st["state"] == "done", st
    snap = client.portal.call(db.kv_get, "snapshot_last", None)
    paths = [c["path"] for c in snap["calls"]]
    assert "/devices" in paths and "/inventory" in paths and any(p.startswith("/locations/") for p in paths)
    assert snap["requests"] <= 60 and "diagnosis" in snap and "rules" in snap["app"]
    assert snap["app"]["devices"] and "ward_orders" in snap["app"]   # every device in one list
    page = client.get("/diagnostics", headers=H).text
    assert "Mining diagnosis" in page and "Mining drones" in page
    dl = client.get("/diagnostics/snapshot.json", headers=H)
    assert dl.headers["content-type"].startswith("application/json") and "attachment" in dl.headers["content-disposition"]
    assert "dev" not in json.loads(dl.text).get("token", "")      # no token field at all
    assert '"api_token"' not in dl.text


def test_mining_diagnosis_reasons():
    from rsweb.snapshot import diagnose
    devs = [
        {"device_code": "MC1", "device_type": "ami_mining_controller", "location": "KEL-BELT-1", "features": ["ami"], "status": "coordinating",
         "ami_directive": {"name": "gather_evenly", "_eval_state": "exhausted:[carbon]:KEL-BELT-1"}, "ami_directive_status": "active"},
        {"device_code": "MD1", "device_type": "mining_drone", "location": "KEL-BELT-1", "status": "idle", "controller_device_code": "MC1"},
        {"device_code": "MD2", "device_type": "mining_drone", "location": "KEL-2-L4", "status": "idle"},
        {"device_code": "MD3", "device_type": "mining_drone", "location": "KEL-BELT-1", "status": "mining (carbon)", "controller_device_code": "MC1"},
        {"device_code": "MC2", "device_type": "ami_mining_controller", "location": "ITH-BELT-1", "features": ["ami"], "status": "idle"},
    ]
    snap = {"calls": [{"path": "/devices", "status": 200, "body": {"devices": devs}},
                      {"path": "/locations/KEL-BELT-1", "status": 200, "body": {"location_type": "belt", "resource_sites": []}}],
            "app": {"rules": {"restart_idle_miners": {"enabled": False}, "reopen_sites": {"enabled": True}}, "schedules": [], "jobs": []}}
    d = diagnose(snap)
    rows = {r["code"]: r for r in d["drones"]}
    assert rows["MD3"]["state"] == "mining"
    assert any("no open resource sites" in w for w in rows["MD1"]["why"]) and any("reports exhausted" in w for w in rows["MD1"]["why"])
    assert any("isn't a belt" in w for w in rows["MD2"]["why"]) and any("Restart idle miners" in f or "rule is OFF" in w
                                                                         for f in rows["MD2"]["fix"] for w in rows["MD2"]["why"])
    ctrl = {c["code"]: c for c in d["controllers"]}
    assert any("no directive" in n for n in ctrl["MC2"]["notes"])
    assert "1 of 3 mining drones are mining" in d["headline"] and any("KEL-BELT-1" in h for h in d["headline"])


def test_live_back_to_belt_plan():
    """From the 2026-10-02 snapshot: 0 of 12 drones mining, every controller 'exhausted' though both read belts have
    open sites at 100%. AEMEROTH's drones sat at used-up salvage on AEMEROTH-4-2; FALQUORYX's controller was paused
    with a stale 'exhausted' at its own belt."""
    from rsweb import salvage as sv
    from rsweb.snapshot import devices_of
    snap = _live()
    devices = devices_of(snap)
    belts = {c["path"].split("/")[2]: c["body"] for c in snap["calls"] if c["path"].startswith("/locations/")}
    open_sites = {b: sv.open_site_count(d) for b, d in belts.items()}
    assert open_sites == {"AEMEROTH-BELT-1": 2, "FALQUORYX-BELT-1": 4}
    ctrls = [d for d in devices if d.get("device_type") == "ami_mining_controller" and not any(t.startswith("fleet:") for t in d.get("tags") or [])]
    plans = {p["ctrl"]: p for p in sv.back_to_belt_plan(ctrls, devices, snap["app"]["managed_by"], open_sites,
                                                       {"AEMEROTH": ["AEMEROTH-BELT-1"], "FALQUORYX": ["FALQUORYX-BELT-1"]}, set())}
    aem = plans["84EE1EF1"]
    assert aem["belt"] == "AEMEROTH-BELT-1" and not aem["move_ctrl"] and aem["directive"] == "gather_evenly"
    assert set(aem["away"]) == {"1886BD05", "399FF11C", "607F9110", "EB6DF4A3"} and "AEMEROTH-4-2" in aem["why"]
    fal = plans["F32E05A7"]
    assert fal["belt"] == "FALQUORYX-BELT-1" and fal["away"] == [] and "stale" in fal["why"]
    assert "90DEC78F" not in plans          # ITHVALAI's belt wasn't read: no open sites known → nothing guessed
    steps = sv.back_to_belt_steps(aem)
    cmds = [(s["path"], (s["body"] or {}).get("command")) for s in steps]
    assert cmds[0] == ("/devices/84EE1EF1", "release") and ("/devices/84EE1EF1", "adopt") in cmds
    assert cmds[-2:] == [("/devices/84EE1EF1", "set_directive"), ("/devices/84EE1EF1", "launch")]
    assert sum(1 for p, c in cmds if c == "travel") == 4
    assert [s["body"]["command"] for s in sv.back_to_belt_steps(fal) if s["body"]] == ["set_directive", "launch"]
    # with ITHVALAI's belt open, its controller (at the entry point) flies there with its drones
    plans = {p["ctrl"]: p for p in sv.back_to_belt_plan(ctrls, devices, snap["app"]["managed_by"],
                                                       {**open_sites, "ITHVALAI-BELT-1": 3}, {"ITHVALAI": ["ITHVALAI-BELT-1"]}, set())}
    ith = plans["90DEC78F"]
    assert ith["belt"] == "ITHVALAI-BELT-1" and ith["move_ctrl"] and len(ith["away"]) == 4


def test_live_back_to_belt_rule_and_schedule(client):
    from rsweb.snapshot import devices_of
    eng = client.app.state.worker.automations
    db = client.app.state.db
    snap = _live()
    client.portal.call(db.kv_set, "devices", devices_of(snap))
    for c in snap["calls"]:
        if c["path"].startswith("/locations/"):
            client.portal.call(db.kv_set, f"loc:{c['path'].split('/')[2]}", c["body"])
    client.portal.call(db.kv_set, "belt_reads", {"AEMEROTH-BELT-1": "2099-01-01T00:00:00+00:00",
                                                 "FALQUORYX-BELT-1": "2099-01-01T00:00:00+00:00"})

    async def enable():
        s = await eng.settings()
        s["rules"]["salvage_when_depleted"]["enabled"] = True
        await eng.save_settings(s)
    client.portal.call(enable)
    client.portal.call(eng.save_schedules, snap["app"]["schedules"])
    done = client.portal.call(eng.rule_back_to_belt, {"back_to_belt": True})
    assert sorted(d.split(" ")[0] for d in done) == ["84EE1EF1", "F32E05A7"]
    jobs = {j["device"]: j for j in client.portal.call(eng.jobs) if j["rule"] == "salvage_when_depleted"}
    assert "bringing 4 drone(s)" in jobs["84EE1EF1"]["title"] and "stale" in jobs["F32E05A7"]["title"]
    # second pass: cooling down / busy — nothing new
    assert client.portal.call(eng.rule_back_to_belt, {"back_to_belt": True}) == []
    # the schedule no longer writes off a stale 'exhausted' at a belt that has open sites
    sched = dict(snap["app"]["schedules"][0])
    devs = devices_of(snap)
    for d in devs:
        if d["device_code"] == "F32E05A7":
            d["ami_directive_status"] = "active"
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(db.kv_set, "automation_jobs", [])
    res = client.portal.call(eng.run_schedule, sched)
    by = {r.split(":")[0]: r for r in res}
    assert "exhausted at AEMEROTH-4-2" in by["84EE1EF1"] and "brings them back" in by["84EE1EF1"]
    assert "won't help" not in by["F32E05A7"]


def test_locations_list_hides_sites_only_seen_in_old_events(client):
    from rsweb.targets import system_targets, system_resources
    db = client.app.state.db
    client.portal.call(db.kv_set, "loc:SOL-BELT-1", {"location_type": "belt", "resource_sites": [
        {"designation": "SOL-BELT-1-SITE-7", "resources_remaining_pct": {"carbon": 40, "structural": 0}},
        {"designation": "SOL-BELT-1-SITE-8", "resources_remaining_pct": {"carbon": 0, "structural": 0}}]})
    # an old digest names a site that has since closed
    client.portal.call(client.app.state.worker.handle_event,
                       _ev(901, "ami.mining.digest", location="SOL-BELT-1", report={"site": "SOL-BELT-1-SITE-3"}))
    t = client.portal.call(system_targets, db, "SOL")
    codes = {x["code"] for x in t["targets"]}
    assert "SOL-BELT-1-SITE-7" in codes
    assert "SOL-BELT-1-SITE-3" not in codes and "SOL-BELT-1-SITE-8" not in codes   # closed / all 0 %
    res = client.portal.call(system_resources, db, "SOL")
    s7 = next(x for x in res["sites_shown"] if x["code"] == "SOL-BELT-1-SITE-7")
    assert s7["remaining_pct"] == {"carbon": 40.0, "structural": 0.0}
    assert "carbon 40%" in client.get("/systems/SOL", headers=H).text      # the open site's % left, per resource


def test_live_mined_out_belt_with_searches_running():
    """21:40Z: FALQUORYX-BELT-1 lists no sites (SITE-79 'fully depleted'); 4 survey drones are ~90% through a search."""
    import pathlib
    from rsweb.snapshot import diagnose
    snap = json.loads((pathlib.Path(__file__).parent / "fixtures" / "live_2026-10-02c.json").read_text())
    snap["app"].update(jobs=[], log=[])
    d = diagnose(snap)
    assert "8 of 12 mining drones are mining" in d["headline"]
    h = next(x for x in d["headline"] if x.startswith("belts with no open sites"))
    assert "FALQUORYX-BELT-1 (searching, new sites due 2026-10-02T17:46:10-04:00)" in h
    r = next(x for x in d["drones"] if x["code"] == "01BA3403")
    assert any("4 survey drone(s) searching FALQUORYX-BELT-1, 89.2%+ done" in f for f in r["fix"])
    c = next(x for x in d["controllers"] if x["code"] == "F32E05A7")
    assert not any("survey drones must search" in n for n in c["notes"])


def test_viability_observe_and_assess():
    from rsweb import viability as via
    st: dict = {}
    miners = [{"device_code": f"MD{i}", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "mining (carbon)"} for i in range(4)]
    survey = [{"device_code": f"SD{i}", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "searching",
               "scan": {"target": "FAL-BELT-1", "started_at": f"2026-10-02T20:3{i}:00+00:00",
                        "completes_at": f"2026-10-02T21:4{i}:00+00:00"}} for i in range(4)]
    # 20:05 four fresh sites; searches seen at 20:40; sites gone by 21:25
    via.observe(st, miners, {"FAL-BELT-1": _belt((76, 100), (77, 100), (78, 100), (79, 100))}, "2026-10-02T20:05:00+00:00")
    via.observe(st, miners + survey, {}, "2026-10-02T20:40:00+00:00")
    via.observe(st, miners + survey, {"FAL-BELT-1": _belt((79, 10))}, "2026-10-02T21:05:00+00:00")
    _, notes = via.observe(st, miners + survey, {"FAL-BELT-1": _belt()}, "2026-10-02T21:25:00+00:00")
    assert any("SITE-79 closed after ~80 min" in n for n in notes)
    rec = st["belts"]["FAL-BELT-1"]
    assert len(rec["searches"]) == 4 and rec["searches"][0]["minutes"] == 70.0 and rec["max_index"] == 79
    a = via.assess("FAL-BELT-1", rec)
    assert a["search_min"] == 70.0 and a["site_life_min"] == 60.0      # 76–78 closed at 21:05 (60 min), 79 at 21:25 (80)
    assert a["ratio"] == 1.17 and a["verdict"] == "watch" and a["miners"] == 4 and a["survey_needed"] == 9
    assert a["survey"] == 4 and a["survey_short"] == 5
    # the same search seen twice isn't counted twice
    via.observe(st, survey, {}, "2026-10-02T21:30:00+00:00")
    assert len(st["belts"]["FAL-BELT-1"]["searches"]) == 4


def test_viability_report_suggests_a_cheaper_belt():
    from rsweb import viability as via
    def rec(search, life, idx, miners=4):
        return {"searches": [{"minutes": search}] * 3,
                "sites": {f"S{i}": {"first": "2026-10-02T20:00:00+00:00", "closed": f"2026-10-02T20:{life:02d}:00+00:00"} for i in range(3)},
                "max_index": idx, "miners": miners, "mining": 0, "survey": 4}
    st = {"belts": {"FAL-BELT-1": rec(59, 20, 120), "ITH-BELT-1": rec(5, 40, 3)}}
    stars = {"FAL": {"position": {"x": 0, "y": 0, "z": 0}}, "ITH": {"position": {"x": 3, "y": 4, "z": 0}},
             "AEM": {"position": {"x": 1, "y": 0, "z": 0}}}
    rows = via.report(st, stars, 2.5, {"AEM-BELT-1": 2})
    fal = rows[0]
    assert fal["belt"] == "FAL-BELT-1" and fal["verdict"] == "consider moving" and fal["ratio"] == 2.95
    assert fal["move_to"]["belt"] == "AEM-BELT-1" and fal["move_to"]["ly"] == 1.0     # nearest cheaper: untracked, site #2
    text = via.alert_text(fal)
    assert "2.95× a site's life" in text and "AEM-BELT-1 (1.0 ly)" in text and "~16 survey drones" in text
    assert next(r for r in rows if r["belt"] == "ITH-BELT-1")["verdict"] == "ok"


def test_viability_engine_alerts_once_and_pages_render(client):
    from rsweb import viability as via
    eng = client.app.state.worker.automations
    db = client.app.state.db
    st = {"belts": {"SOL-BELT-1": {"searches": [{"minutes": 90}] * 3, "max_index": 80, "miners": 2, "mining": 0, "survey": 2,
                                   "sites": {f"S{i}": {"first": "2026-10-02T20:00:00+00:00", "closed": "2026-10-02T20:30:00+00:00"}
                                             for i in range(3)}}}}
    client.portal.call(db.kv_set, "viability", st)

    async def enable():
        s = await eng.settings()
        s["rules"]["belt_viability"]["enabled"] = True
        await eng.save_settings(s)
    client.portal.call(enable)
    alerts = client.portal.call(eng.track_viability)
    assert len(alerts) == 1 and "SOL-BELT-1: searches take 3.0× a site's life" in alerts[0]
    assert client.portal.call(eng.track_viability) == []            # once per crossing
    page = client.get("/systems/SOL", headers=H).text
    assert "site life" in page and "consider moving" in page
    assert "Belt viability alerts" in client.get("/automations", headers=H).text


def test_consolidate_falquoryx_leftovers():
    """Live 21:40Z: autofactory at FALQUORYX-BELT-1 waiting for resources; 1,235 units left at FALQUORYX-5 by a contract delivery."""
    from rsweb import consolidate as co
    from rsweb.snapshot import devices_of
    snap = _live_c_inventory()
    devices = [d for d in devices_of(snap) if d["device_code"].startswith(("3E95", "DF45", "41B6", "C582", "374C", "1179"))]
    assert {d["device_code"] for d in devices} >= {"3E95BD59", "DF451241"}
    inv = {"FALQUORYX-BELT-1": [{"quantity": 1665, "resource_type": "carbon"}, {"quantity": 36, "resource_type": "volatiles"}, {"quantity": 6284, "resource_type": "structural"},
                                {"quantity": 68, "resource_type": "rares"}],
           "FALQUORYX-5": [{"quantity": 127, "resource_type": "conductive"}, {"quantity": 58, "resource_type": "rares"},
                           {"quantity": 224, "resource_type": "silicates"}, {"quantity": 615, "resource_type": "structural"},
                           {"quantity": 211, "resource_type": "volatiles"}],
           "FALQUORYX-2": [{"quantity": 40, "resource_type": "carbon"}]}                         # below the minimum
    fac = next(d for d in devices if d["device_code"] == "3E95BD59")
    fac["printing"] = {"device_type": "survey_drone"}
    bps = {"survey_drone": {"device_type": "survey_drone", "resources": {"volatiles": 100, "rares": 80, "structural": 50}}}
    plans = co.plan(devices, inv, bps, set())
    assert len(plans) == 1
    p = plans[0]
    assert p["controller"] == "DF451241" and p["collect"] == "FALQUORYX-5" and p["deliver"] == "FALQUORYX-BELT-1"
    # "deliver until": the home pile plus what's picked up (rares 68, structural 6284, volatiles 36 there already)
    assert p["requirement"] == {"conductive": 127, "rares": 126, "silicates": 224, "structural": 6899, "volatiles": 247}
    assert p["helps"] == {"volatiles": 64, "rares": 12}
    body = co.steps(p)[0]["body"]
    assert body == {"command": "set_directive", "directive": "delivery",
                    "configuration": {"route": {"collect": "FALQUORYX-5", "deliver": "FALQUORYX-BELT-1"}, "requirement": p["requirement"]}}
    assert "has what the autofactory is waiting for: 12 rares, 64 volatiles" in co.describe(p)
    # a contract staged there: left alone; controller busy / no drones: reported, not sent
    assert co.plan(devices, inv, bps, set(), {"FALQUORYX-5"}) == []
    waiting = co.plan(devices, inv, bps, {"DF451241"})
    assert waiting[0]["controller"] is None and "no free in-system transport controller" in co.describe(waiting[0])


def test_consolidate_rule_creates_job(client):
    eng = client.app.state.worker.automations
    db = client.app.state.db
    devices = [
        {"device_code": "AF1", "device_type": "autofactory", "location": "SOL-BELT-1", "status": "waiting_for_resources",
         "available_commands": ["enqueue_print"], "tags": []},
        {"device_code": "TC1", "device_type": "ami_transport_controller", "location": "SOL-BELT-1", "status": "idle",
         "ami_directive": {"name": "delivery", "_eval_state": "completed:delivered"}, "ami_directive_status": "completed", "tags": []},
        {"device_code": "TD1", "device_type": "transport_drone", "location": "SOL-BELT-1", "status": "idle", "controller_device_code": "TC1"},
        {"device_code": "TC2", "device_type": "ami_transport_controller", "location": "SOL-3-L4", "status": "coordinating",
         "ami_directive": {"name": "ferry"}, "tags": ["ferry"]},
    ]
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-BELT-1", "items": {"carbon": 500}},
                                                {"location": "SOL-3", "items": {"structural": 300, "silicates": 50}}])
    done = client.portal.call(eng.rule_consolidate, True)
    assert len(done) == 1 and done[0].startswith("TC1 hauls SOL-3 (350 units) → autofactory at SOL-BELT-1")
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "consolidate"][-1]
    assert job["device"] == "TC1"
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Consolidation at the autofactory" in page


def test_drone_indicators_on_the_maps(client):
    from rsweb.web import drone_summary
    devs = [{"device_code": "M1", "device_type": "mining_drone", "status": "mining (carbon)"},
            {"device_code": "M2", "device_type": "mining_drone", "status": "mining (carbon)"},
            {"device_code": "M3", "device_type": "mining_drone", "status": "idle"},
            {"device_code": "S1", "device_type": "survey_drone", "status": "searching"},
            {"device_code": "T1", "device_type": "transport_hauler", "status": "idle"},
            {"device_code": "AF", "device_type": "autofactory", "status": "idle"}]
    g = {x["kind"]: x for x in drone_summary(devs)}
    assert list(g) == ["mining", "survey", "transport"]
    assert (g["mining"]["n"], g["mining"]["working"], g["mining"]["idle"], g["mining"]["state"]) == (3, 2, 1, "working")
    assert g["survey"]["state"] == "working" and g["transport"]["state"] == "idle"
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get("/systems/SOL", headers=H).text
    assert 'class="drone drone-' in page and "mining drone(s) at SOL-BELT-1" in page and "nodrones" in page
    sol = next(s for s in client.get("/api/map.json", headers=H).json()["stars"] if s["designation"] == "SOL")
    assert any(d["kind"] == "mining" and d["n"] >= 1 for d in sol["drones"])


def test_working_drones_orbit_their_site(client):
    from rsweb.web import drone_orbits
    devs = [{"device_code": "M1", "device_type": "mining_drone", "status": "mining (carbon)"},
            {"device_code": "M2", "device_type": "mining_drone", "status": "mining (carbon)"},
            {"device_code": "M3", "device_type": "mining_drone", "status": "idle"},
            {"device_code": "S1", "device_type": "survey_drone", "status": "tracking"},
            {"device_code": "AF", "device_type": "autofactory", "status": "printing"}]
    o = {x["code"]: x for x in drone_orbits(devs, 10)}
    assert set(o) == {"M1", "M2", "S1"}   # idle drones and non-drones don't orbit
    assert o["M1"]["kind"] == "work" and o["S1"]["kind"] == "survey" and o["S1"]["r"] > o["M1"]["r"] > 10
    assert {o["M1"]["delay"], o["M2"]["delay"]} == {0, -4.5}   # spread evenly around the ring
    from rsweb.web import build_system_view
    belt = [{"device_code": "M1", "device_type": "mining_drone", "status": "mining (carbon)", "location": "SOL-BELT-1"},
            {"device_code": "M2", "device_type": "mining_drone", "status": "mining (carbon)", "location": "SOL-BELT-1"},
            {"device_code": "S1", "device_type": "survey_drone", "status": "tracking", "location": "SOL-BELT-1",
             "tracking_site": "SOL-BELT-1-SITE-2"}]
    scan = {"asteroid_belt": {"belts": [{"designation": "SOL-BELT-1", "inner_radius_au": 2.2, "outer_radius_au": 3.2}]}}
    places = [{"code": "SOL-BELT-1-SITE-2", "category": "site"}]
    view = build_system_view("SOL", scan, belt, [], places, drone_sites={"M1": "SOL-BELT-1-SITE-2", "M2": "SOL-BELT-9-SITE-1"})
    rings = {(g["x"], g["y"]): sorted(o["code"] for o in g["orbits"]) for g in view["orbits"]}
    site = next(pl for pl in view["places"] if pl["code"] == "SOL-BELT-1-SITE-2")
    loc = view["markers"][0]
    assert rings == {(site["x"], site["y"]): ["M1", "S1"], (loc["x"], loc["y"]): ["M2"]}   # M2's site isn't drawn here
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get("/systems/SOL", headers=H).text
    assert 'orbiter orbit-survey' in page and 'class="scan-beam"' in page


def test_tracking_drones_deactivate_right_before_they_move(client):
    """Live 2026-10-07: 'Cannot cruise while tracking a site - deactivate first' on every pass moving Miner 1 and 2."""
    from rsweb import loadouts as lo
    from rsweb.modular import prepare_moves
    sd = {"device_code": "SD1", "device_type": "survey_drone", "location": "KEL-BELT-1", "status": "tracking",
          "features": ["cruise", "survey", "stow"], "available_commands": ["deactivate", "stow", "travel"]}
    md = {"device_code": "MD1", "device_type": "mining_drone", "location": "KEL-BELT-1", "status": "idle", "features": ["stow"]}
    mf = {"device_code": "MF", "device_type": "mobile_fleet", "location": "KEL-4-L4", "status": "idle", "features": ["surge"],
          "attach_capacity": 36}
    by = {d["device_code"]: d for d in (sd, md, mf)}
    dl = {"carrier": "MF", "carrier_loc": "KEL-4-L4", "from": "KEL", "to": "LAR", "devices": ["MD1", "SD1"], "mode": "attach"}
    out = prepare_moves(lo.delivery_steps(dl, by, {"LAR": {"entry_point": "LAR-1-L4"}}, False), [sd, md, mf], {})
    descs = [s["desc"] for s in out]
    d_i = descs.index("SD1: stop tracking its site (deactivate) to move")
    assert d_i + 1 == descs.index("SD1 → KEL-4-L4 (to board MF)")
    assert descs.index("MF: detach SD1 in LAR") + 1 == descs.index("SD1: activate")
    assert not any(d.startswith("MD1: stop tracking") for d in descs)
    # command descriptions: on the form and as the picker's tooltips
    from rsweb.commands import COMMANDS, DESCRIPTIONS
    assert set(COMMANDS) <= set(DESCRIPTIONS)
    client.portal.call(client.app.state.worker.sync_devices)
    r = client.get("/devices/AF00BEEF/command-form?command=compact", headers=HX)
    assert "Fold a large (modular) device" in r.text
    assert 'title="Fold a large (modular) device' in client.get("/devices/AF00BEEF", headers=H).text


def test_galaxy_shows_what_each_system_is_mining(client):
    from rsweb.web import mining_now
    assert mining_now([{"status": "mining (carbon)"}, {"status": "mining (carbon)"}, {"status": "mining (rares)"},
                       {"status": "mining"}, {"status": "idle"}]) == {"carbon": 2, "rares": 1}
    world = client.app.state.api.http._transport.app.state.world
    world.devices[1]["status"] = "mining (volatiles)"
    client.portal.call(client.app.state.worker.sync_devices)
    star = world.devices[1]["location"].split("-")[0]
    stars = client.get("/api/map.json", headers=H).json()["stars"]
    s = next((x for x in stars if x["designation"] == star), None)
    assert s is None or s["mining"].get("volatiles", 0) >= 1


def test_parked_mining_controller_goes_to_the_belt_with_its_drones():
    """Live 2026-10-07: LORSELAN / LARSELAN controllers sat at Lagrange points ('gated:cold_repair', logging
    ami_overheat) with every drone idle, though the system has a belt."""
    from rsweb import salvage as sv
    ctrl = {"device_code": "C1", "device_type": "ami_mining_controller", "location": "LOR-6-L4", "status": "coordinating",
            "ami_directive": {"name": "gather_evenly", "_eval_state": "gated:cold_repair"}, "ami_directive_status": "active"}
    mine = [{"device_code": f"M{i}", "device_type": "mining_drone", "location": "LOR-6-L4", "status": "idle",
             "controller_device_code": "C1"} for i in range(2)]
    stray = {"device_code": "S1", "device_type": "mining_drone", "location": "LOR-2", "status": "idle"}
    busy = {"device_code": "S2", "device_type": "mining_drone", "location": "LOR-2", "status": "idle"}
    devices = [ctrl, *mine, stray, busy]
    assert sv.parked(ctrl)
    assert not sv.parked({**ctrl, "location": "LOR-BELT-1"})                                       # at a belt
    assert not sv.parked({**ctrl, "ami_directive": {"name": "gather_salvage", "_eval_state": "active"}})   # salvaging
    assert not sv.parked({**ctrl, "status": "travelling"})
    plans = sv.back_to_belt_plan([ctrl], devices, {}, {}, {"LOR": ["LOR-BELT-1"]}, set(), None, {"C1", "M0", "M1", "S1"})
    assert len(plans) == 1
    p = plans[0]
    assert p["belt"] == "LOR-BELT-1" and p["move_ctrl"] and p["away"] == ["M0", "M1"] and p["strays"] == ["S1"]
    assert "parked" in p["why"] and p["directive"] == "gather_evenly"
    steps = [(s["path"], s["body"]) for s in sv.back_to_belt_steps(p)]
    assert steps[0] == ("/devices/C1", {"command": "release", "devices": ["M0", "M1"]})   # only its own drones are released
    assert ("/devices/C1", {"command": "travel", "destination": "LOR-BELT-1"}) in steps
    assert ("/devices/S1", {"command": "travel", "destination": "LOR-BELT-1"}) in steps
    assert ("/devices/C1", {"command": "adopt", "devices": ["M0", "M1", "S1"]}) in steps
    assert steps[-1] == ("/devices/C1", {"command": "launch"})
    # no belt in the system: nothing to plan here (salvage takes over)
    assert sv.back_to_belt_plan([ctrl], devices, {}, {}, {"LOR": []}, set(), None, set()) == []
    # live 2026-10-25: drones it holds in another system can't surge back — let go, never flown
    far = {"device_code": "M9", "device_type": "mining_drone", "location": "SOL-BELT-1", "status": "idle",
           "controller_device_code": "C1"}
    p = sv.back_to_belt_plan([ctrl], devices + [far], {}, {}, {"LOR": ["LOR-BELT-1"]}, set(), None, set())[0]
    assert p["away"] == ["M0", "M1"] and p["gone"] == ["M9"]
    steps = [(s["path"], s["body"]) for s in sv.back_to_belt_steps(p)]
    assert ("/devices/C1", {"command": "release", "devices": ["M9"]}) in steps
    assert not any(path == "/devices/M9" for path, _ in steps)


def test_miners_in_a_system_without_a_belt_go_to_salvage(client):
    import json as _json
    world, eng = _salvage_setup(client)
    db = client.app.state.db
    row = client.portal.call(db.fetchone, "SELECT data FROM systems WHERE star='SOL'")
    scan = _json.loads(row["data"])
    scan.pop("asteroid_belt", None)                                      # scanned, and no belt
    client.portal.call(db.execute, "UPDATE systems SET data=? WHERE star='SOL'", (_json.dumps(scan),))
    client.portal.call(eng.rule_salvage)
    jobs = [j for j in client.portal.call(eng.jobs) if j["rule"] == "salvage_when_depleted"]
    assert any(j["device"] == "MC91FF22" and "salvage" in j["title"] for j in jobs)


def test_controller_at_the_belt_brings_its_scattered_drones():
    """The game's docs: a controller running drones at many places multi-tasks, which brings ami_overheat. Live
    2026-10-07: LORSELAN's controller at the belt, its 8 drones idle at LORSELAN-6-L4."""
    from rsweb import salvage as sv
    ctrl = {"device_code": "C1", "device_type": "ami_mining_controller", "location": "LOR-BELT-1", "status": "coordinating",
            "ami_directive": {"name": "gather_evenly", "_eval_state": "gated:cold_repair"}}
    kids = [{"device_code": f"M{i}", "device_type": "mining_drone", "location": "LOR-6-L4", "status": "idle",
             "controller_device_code": "C1"} for i in range(3)]
    assert sv.scattered(ctrl, [ctrl, *kids]) == ["M0", "M1", "M2"]
    p = sv.back_to_belt_plan([ctrl], [ctrl, *kids], {}, {}, {"LOR": ["LOR-BELT-1"]}, set())[0]
    assert p["belt"] == "LOR-BELT-1" and not p["move_ctrl"] and p["away"] == ["M0", "M1", "M2"] and "overheats" in p["why"]
    # left alone: one of them mining, salvage on purpose, drones at a site in the belt, or a controller away from belts
    assert sv.scattered(ctrl, [ctrl, {**kids[0], "status": "mining (carbon)"}, *kids[1:]]) == []
    assert sv.scattered({**ctrl, "ami_directive": {"name": "gather_salvage"}}, [ctrl, *kids]) == []
    assert sv.scattered(ctrl, [ctrl, {**kids[0], "location": "LOR-BELT-1-SITE-2"}]) == []
    assert sv.scattered({**ctrl, "location": "LOR-6-L4"}, [ctrl, *kids]) == []


def test_mining_watch_ends_when_the_salvage_is_used_up():
    """Live 2026-10-07: Prospectors at KELMONENT (no belt) salvaged; the controller reported gather_salvage
    'depleted:complete' and the watch phase waited for 'exhausted' for ever."""
    from rsweb import fleets as fl
    f = {"id": "p", "role": "mining"}
    ctrl = {"device_code": "C", "device_type": "ami_mining_controller", "tags": ["fleet:p"], "location": "KEL-1",
            "ami_directive": {"name": "gather_salvage", "_eval_state": "depleted:complete"}}
    drone = {"device_code": "D", "device_type": "mining_drone", "tags": ["fleet:p"], "location": "KEL-1", "status": "idle"}
    done, why, upd = fl.watch_done(f, {}, [ctrl, drone], "2026-10-07T12:00:00+00:00")
    assert not done and "salvage used up" in why and upd["exhausted_since"]          # the grace period starts
    done, why, _ = fl.watch_done(f, {"exhausted_since": "2026-10-07T11:00:00+00:00"}, [ctrl, drone], "2026-10-07T12:00:00+00:00")
    assert done
    # still mining something: not done, whatever the state says
    assert not fl.watch_done(f, {"exhausted_since": "2026-10-07T11:00:00+00:00"}, [ctrl, {**drone, "status": "mining (carbon)"}],
                             "2026-10-07T12:00:00+00:00")[0]
    # gated / working: not done
    assert not fl.watch_done(f, {}, [{**ctrl, "ami_directive": {"name": "gather_evenly", "_eval_state": "gated:cold_repair"}}, drone],
                             "2026-10-07T12:00:00+00:00")[0]
    # no controller: done once no drone has mined for the grace period
    assert fl.watch_done(f, {"exhausted_since": "2026-10-07T11:00:00+00:00"}, [drone], "2026-10-07T12:00:00+00:00")[0]


def test_another_players_ward_keeps_our_miners_out(client):
    from rsweb import loadouts as lo
    from rsweb import wards
    stars = {"SOL": {"position": {"x": 0, "y": 0, "z": 0}}, "ZAL": {"position": {"x": 5, "y": 0, "z": 0}, "has_ward": True},
             "OUR": {"position": {"x": 9, "y": 0, "z": 0}, "has_ward": True}}
    ours = {"device_code": "W", "device_type": "system_ward", "location": "OUR-1-L4", "status": "warding"}
    assert wards.foreign(stars, [ours]) == {"ZAL"}                                   # our own ward isn't "another player's"
    assert wards.foreign({"stars": [{"designation": "ZAL", "has_ward": True}]}, []) == {"ZAL"}
    # a stationed fleet at a warded home: no mining controllers / drones are sent; the rest still is
    fleets = [{"id": "z", "name": "Z", "home": "ZAL", "station": True,
               "wants": {"mining_drone": 4, "ami_mining_controller": 1, "survey_drone": 2}}]
    cfg = {"phases": [], "fleets": fleets, "fleets_migrated": True}
    p = lo.plan(cfg, [], [], {}, stars, {}, set(), [], {})
    rows = {r["type"]: r for r in p["report"]["z"]["rows"]}
    assert rows["mining_drone"]["short"] == 0 and rows["mining_drone"]["warded"]
    assert rows["ami_mining_controller"]["short"] == 0 and rows["survey_drone"]["short"] == 2
    assert any("ward" in u["why"] for u in p["unmet"]) and p["report"]["z"]["warded"]
    # a mining mission to a warded system can't launch
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [{"designation": "SOL"},
                                                                        {"designation": "ABOTEIN", "has_ward": True}]})
    client.portal.call(eng.save_fleets, [{"id": "m", "name": "M", "role": "mining", "home": "FAL", "wants": {}}])
    r = client.post("/fleets/m/mission", data={"targets": "ABOTEIN"}, headers=HX)
    assert "another player" in r.text and not (client.portal.call(eng.fleets)[0].get("mission") or {}).get("status")
    # a running mining mission whose target became warded stalls before it unloads
    m = {"status": "running", "phase": "travel", "idx": 0, "targets": ["ABOTEIN"], "opts": {}, "log": []}
    steps, problems = client.portal.call(eng.fleet_phase_steps, {"id": "m", "name": "M", "role": "mining", "home": "FAL"},
                                         m, "deploy", [])
    assert not steps and m.get("stall") and "ward" in problems[0]


def test_mining_prospects_score():
    from rsweb import prospects as pr
    rich = {"asteroid_belt": {"belts": [{"designation": "A-BELT-1", "density": "dense",
                                         "resources": {"carbon": "rich", "volatiles": "high", "rares": "low"}}]}}
    poor = {"asteroid_belt": {"belts": [{"designation": "B-BELT-1", "density": "sparse",
                                         "resources": {"carbon": "scarce", "volatiles": "low", "rares": "low"}}]}}
    a = pr.score("A", rich, {"mineable": 6000, "sites": [{"code": "s"}] * 5}, [{"belt": "A-BELT-1", "verdict": "ok"}], 3.0, True)
    b = pr.score("B", poor, {}, [], 3.0, True)
    assert a["score"] > b["score"] and a["parts"]["proven"] == 20 and a["parts"]["staying"] == 15   # no salvage: 20 of 25
    assert "rich carbon" in a["reasons"][0] and a["best"] == ["carbon", "volatiles"]
    # far away and no relay costs access
    far = pr.score("A", rich, {}, [], 40.0, False)
    assert far["parts"]["access"] == 0 and any("replicant must ride" in x for x in far["reasons"])
    # what waiting prints lack weighs clearly (owner 2026-10-22): a belt rich in it gains, and beats a richer one without it
    tilted = pr.score("A", rich, {}, [], 3.0, True, wanted={"carbon": 1.0})
    plain = pr.score("A", rich, {}, [], 3.0, True)
    assert tilted["parts"]["richness"] > plain["parts"]["richness"] * 1.15
    sil = {"asteroid_belt": {"belts": [{"designation": "S-BELT-1", "density": "dense",
                                        "resources": {"silicates": "rich", "carbon": "low"}}]}}
    assert pr.score("S", sil, {}, [], 3.0, True)["score"] < plain["score"]
    assert pr.score("S", sil, {}, [], 3.0, True, wanted={"silicates": 1.0})["score"] > \
        pr.score("A", rich, {}, [], 3.0, True, wanted={"silicates": 1.0})["score"]
    assert any("short of (carbon)" in x for x in tilted["reasons"])
    # not scored: warded, a stationed fleet's home, unscanned
    assert pr.score("A", rich, {}, [], 3.0, True, warded=True)["status"] == "warded"
    assert pr.score("A", rich, {}, [], 3.0, True, stationed="Home")["score"] is None
    home = pr.score("A", rich, {}, [], 3.0, True, stationed="Home")
    assert home["status"] == "stationed" and home["review_score"] == pr.score("A", rich, {}, [], 3.0, True)["score"]
    assert pr.score("C", None, None, [], 3.0, False)["status"] == "unscanned"
    # wanted: a waiting print's shortfall counts most; a falling or low stockpile a little
    w = pr.wanted_from({"carbon": 10, "structural": 500, "silicates": 400, "volatiles": 300, "conductive": 450, "rares": 5},
                       {"volatiles": [600, 300]}, [{"carbon": 100}])
    assert w["carbon"] >= 0.9 and 0 < w["volatiles"] <= 0.6 and w["rares"] == 0.4 and w["structural"] == 0


def test_prospects_page_and_mining_targets(client):
    client.portal.call(client.app.state.worker.sync_devices)
    client.get("/systems/SOL", headers=H)   # a stored scan
    page = client.get("/systems/prospects", headers=H).text
    assert "Mining prospects" in page and "/systems/SOL" in page
    assert "Mining prospects" in client.get("/systems", headers=H).text
    eng = client.app.state.worker.automations
    client.portal.call(eng.save_fleets, [{"id": "m", "name": "M", "role": "mining", "home": "ABOTEIN", "wants": {}}])
    f = client.get("/fleets/details?all=1", headers=H).text
    assert "prospect " in f


def test_systems_page_sorts_by_distance_from_origin(client):
    client.portal.call(client.app.state.worker.sync_devices)
    for s in ("SOL", "FARAWAY", "NEARBY"):
        client.portal.call(client.app.state.db.execute, "INSERT OR REPLACE INTO systems (star, data, updated_at) "
                           "VALUES (?, '{}', '2026-10-18T00:00:00+00:00')", (s,))
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [
        {"designation": "SOL", "position": {"x": 0, "y": 0, "z": 0}},
        {"designation": "FARAWAY", "position": {"x": 40, "y": 0, "z": 0}},
        {"designation": "NEARBY", "position": {"x": 3, "y": 4, "z": 0}}]})
    page = client.get("/systems?origin=SOL", headers=H).text
    assert "distances from" in page and "5.0 ly" in page and "40.0 ly" in page
    assert page.index("/systems/SOL\"") < page.index("/systems/NEARBY\"") < page.index("/systems/FARAWAY\"")
    page = client.get("/systems?origin=FARAWAY", headers=H).text
    assert page.index("/systems/FARAWAY\"") < page.index("/systems/NEARBY\"") < page.index("/systems/SOL\"")


def test_a_system_we_mine_in_isnt_warded_against_us():
    from rsweb import wards
    stars = {"stars": [{"designation": "AEMEROTH", "has_ward": True}, {"designation": "ITHVALAI", "has_ward": True}]}
    drone = {"device_code": "D", "device_type": "mining_drone", "location": "AEMEROTH-BELT-1", "status": "mining (carbon)"}
    assert wards.foreign(stars, [drone]) == {"ITHVALAI"}


def test_trade_run_only_waits_once_everything_is_at_the_site():
    from rsweb import fleets as fl
    m = {"contract": {"designation": "E", "location": "SOL-4", "price": {"carbon": 200, "rares": 10}}, "loaded": ["F1"]}
    inv = {"SOL-4": {"carbon": 200}}
    # short of rares and nothing on its way: gather again
    assert fl.deal_check(m, inv, []) == ("regather", {"rares": 10})
    # a vessel flying there with the rares: wait for it
    fr = {"device_code": "F2", "travel": {"destination": "SOL-4"}, "cargo": [{"resource_type": "rares", "quantity": 10}]}
    assert fl.deal_check(m, inv, [fr]) == ("incoming", {"rares": 10})
    # a fleet freighter at the site still holding cargo: deposit again
    at = {"device_code": "F1", "location": "SOL-4", "cargo": {"rares": 10}}
    assert fl.deal_check(m, inv, [at])[0] == "redeliver"
    assert fl.deal_check(m, {"SOL-4": {"carbon": 200, "rares": 10}}, [])[0] == "ok"
    # the fleet's own replicant fulfills (contract template gets it)
    devs = [{"device_code": "HV", "tags": ["fleet:t"]}]
    assert fl.fleet_replicant({"id": "t"}, devs, {"R9": {"hosted_device_code": "XX"}, "R1": {"hosted_device_code": "HV"}})[0] == "R1"
    st = fl.fulfil_step(fl.deal(m), "R1", 'POST /locations/{location}/events/{designation} {"replicant_code": "{replicant}"}')
    assert st["path"] == "/locations/SOL-4/events/E" and st["body"] == {"replicant_code": "R1"}


def test_dry_belt_rests_controller_and_relaunches_with_its_directive(client):
    """Live 2026-10-08 (LORSELAN): eight drones mined out five small sites in eight minutes; a ten-minute-old belt read
    still listed them, the controller was relaunched onto nothing and logged ami_overheat (heat 2.0, −0.05 % / 20 s)."""
    eng, db, w = client.app.state.worker.automations, client.app.state.db, client.app.state.worker
    C = {"device_code": "C1000001", "device_type": "ami_mining_controller", "location": "LOR-BELT-1", "status": "coordinating",
         "features": ["ami", "cruise", "stow"], "in_control_range": True, "ami_directive_status": "active",
         "available_commands": ["set_directive", "clear_directive", "launch", "adopt"],
         "ami_directive": {"name": "gather_resources", "config": {"structural": 100},
                           "_eval_state": "exhausted:['structural']:LOR-BELT-1"}}
    drones = [{"device_code": f"D100000{i}", "device_type": "mining_drone", "location": "LOR-BELT-1", "status": "idle",
               "controller_device_code": "C1000001"} for i in range(2)]
    client.portal.call(db.kv_set, "devices", [C] + drones)
    client.portal.call(db.kv_set, "loc:LOR-BELT-1", {"resource_sites": [{"designation": "LOR-BELT-1-SITE-2"}]})
    read = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    client.portal.call(db.kv_set, "belt_reads", {"LOR-BELT-1": read})

    async def enable():
        s = await eng.settings()
        s["rules"]["salvage_when_depleted"]["enabled"] = True
        await eng.save_settings(s)
    client.portal.call(enable)
    assert client.portal.call(eng.open_sites_now, "LOR-BELT-1") == 1
    client.portal.call(w.handle_event, {**_ev(961, "site.depleted", device="D1000000", site="LOR-BELT-1-SITE-2"),
                                        "location": "LOR-BELT-1"})   # mined out after the read
    assert client.portal.call(eng.open_sites_now, "LOR-BELT-1") == 0
    assert client.portal.call(eng.belt_open_sites, ["LOR-BELT-1"]) == {"LOR-BELT-1": 0}
    assert client.portal.call(eng.rule_back_to_belt, {"back_to_belt": True}) == []     # no relaunch onto nothing
    out = client.portal.call(eng.rest_dry_controllers)
    assert out == ["C1000001 rests at LOR-BELT-1"]
    job = next(j for j in client.portal.call(eng.jobs) if j["device"] == "C1000001")
    assert job["steps"][0]["body"] == {"command": "clear_directive"}
    rested = client.portal.call(db.kv_get, "rested")
    assert rested["C1000001"]["directive"] == "gather_resources" and rested["C1000001"]["belt"] == "LOR-BELT-1"
    # the directive is cleared; later the survey drones open a site: relaunched with what it ran before
    C2 = {**C, "ami_directive": {"name": None, "config": None, "_eval_state": None}, "ami_directive_status": None}
    client.portal.call(db.kv_set, "devices", [C2] + drones)
    client.portal.call(db.kv_set, "automation_jobs", [])
    assert client.portal.call(eng.rest_dry_controllers) == []                     # still resting, not forgotten
    client.portal.call(db.kv_set, "loc:LOR-BELT-1", {"resource_sites": [{"designation": "LOR-BELT-1-SITE-3"}]})
    client.portal.call(db.kv_set, "belt_reads", {"LOR-BELT-1": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    done = client.portal.call(eng.rule_back_to_belt, {"back_to_belt": True})
    assert done == ["C1000001 → LOR-BELT-1"]
    job = next(j for j in client.portal.call(eng.jobs) if j["device"] == "C1000001")
    assert "rested while LOR-BELT-1 was dry" in job["title"]
    sd = next(s for s in job["steps"] if (s.get("body") or {}).get("command") == "set_directive")
    assert sd["body"]["directive"] == "gather_resources" and sd["body"].get("configuration") == {"structural": 100}
    assert "C1000001" not in (client.portal.call(db.kv_get, "rested") or {})


def test_site_yield_estimate_from_mining_history(client):
    from rsweb.targets import site_yields
    db, w = client.app.state.db, client.app.state.worker
    for i, (site, q) in enumerate((("SOL-BELT-1-SITE-1", 300), ("SOL-BELT-1-SITE-1", 100), ("SOL-BELT-1-SITE-2", 200))):
        client.portal.call(w.handle_event, _ev(880 + i, "mining.resource_depleted", f"D{i}", location="SOL-BELT-1",
                                               site=site, resource_type="carbon", quantity_mined=q))
    by_belt, _ = client.portal.call(site_yields, db)
    assert by_belt[("SOL-BELT-1", "carbon")] == (300.0, 2)      # site 1: 400 by two drones, site 2: 200


def test_a_mining_drone_is_deactivated_before_it_moves():
    """Live 2026-10-13: deactivate stops mining ("mining_stopped"), so a move needn't wait for the drone to go idle."""
    from rsweb import modular
    from rsweb.automations import step
    devs = [{"device_code": "M1", "device_type": "mining_drone", "status": "mining", "location": "SOL-BELT-1"}]
    steps = modular.with_untracking([step("M1 to SOL-3", "/devices/M1", {"command": "travel", "destination": "SOL-3"},
                                          wait=["travel.arrived"])], devs)
    assert [s["body"]["command"] for s in steps] == ["deactivate", "travel"]
    devs[0]["status"] = "idle"
    assert len(modular.with_untracking([step("M1 to SOL-3", "/devices/M1", {"command": "travel"})], devs)) == 1


def test_snapshot_2026_10_15_errors_count_as_done_and_salvage_messages_are_read():
    from rsweb.ingest import SALVAGE_MSG
    assert SALVAGE_MSG.search("Salvage discovered at KELHYRAS-6: a derelict hauler")
    assert not SALVAGE_MSG.search("Print completed: mining drone")
    from rsweb.ingest import salvage_message, message_unread, message_id
    m = {"message_id": 7, "is_read": False, "subject": "Salvage discovered: derelict hauler at KELHYRAS-6-SAL-2"}
    assert salvage_message(m) and message_unread(m) and message_id(m) == 7   # live wording, other field names

def test_scanning_drone_finishes_its_scan_before_moving():
    from rsweb.automations import step
    from rsweb.modular import prepare_moves
    devices = [{"device_code": "SD000001", "device_type": "survey_drone", "location": "SOLARES-4", "status": "scanning"},
               {"device_code": "SD000002", "device_type": "survey_drone", "location": "SOLARES-4", "status": "idle"}]
    steps = [step(f"{c} → SOLARES-4-L4 (board)", f"/devices/{c}", {"command": "travel", "destination": "SOLARES-4-L4"})
             for c in ("SD000001", "SD000002")]
    steps[1]["seq0_from"] = 0
    out = prepare_moves(steps, devices, {}, {})
    assert out[0]["method"] == "WAIT" and out[0]["wait"] == ["scan.completed"] and out[0]["wait_device"] == "SD000001"
    assert [s["desc"] for s in out[1:]] == [s["desc"] for s in steps] and out[2]["seq0_from"] == 1


def test_build_orders_mine_what_their_prints_need(client):
    """A mining drone costs carbon too: the built-in orders mine it, `mine:` takes weights, and saving an order whose
    print needs a resource no earlier step mines is refused. Built-ins stored before the fix are updated."""
    from rsweb import sim_order as so
    bps = {"mining_drone": {"resources": {"carbon": 25, "conductive": 50, "silicates": 25, "structural": 100}}}
    assert all(not so.check(so.parse(t)[0], bps) for t in (so.DEFAULT, so.EXPAND))
    assert so.parse("mine: structural 2, carbon")[0][0]["resources"] == ["structural", "structural", "carbon"]
    db, eng = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "blueprints", [{"device_type": "mining_drone", **bps["mining_drone"]}])
    t = client.post("/simulations/orders", data={"name": "x", "text": "mine: structural\nprint 1 mining_drone"}, headers=HX).text
    assert "Not saved" in t and "needs carbon, conductive, silicates" in t
    client.portal.call(db.kv_set, "sim", {"orders": {so.EXPAND_NAME: so.OLD_BUILTINS[0]}, "expand_seeded": True})
    assert client.portal.call(eng.sim_state)["orders"][so.EXPAND_NAME] == so.EXPAND


def test_salvage_counts_as_richness_for_a_next_home():
    """Owner 2026-10-19: salvage is a possible target for mining operations."""
    from rsweb import prospects as pr
    bare = pr.score("S", {"planets": []}, {}, [], 2.0, True)
    salvage = pr.score("S", {"planets": []}, {"salvageable": 5000}, [], 2.0, True)
    assert salvage["parts"]["richness"] == 20 and salvage["score"] > bare["score"] + 15
    assert any("salvage" in r for r in salvage["reasons"])


def test_nothing_to_mine_at_open_sites_is_skipped_quietly(client):
    eng = client.app.state.worker.automations
    from rsweb.automations import step

    async def refuse(method, path, body=None, *a, **k):
        if (body or {}).get("command") == "start_mining":
            return False, None, f"No {body['resource_type']} remaining at any active site"
        return True, {}, None
    eng.send = refuse
    steps = [step(f"M{i}: mine conductive", f"/devices/M{i}", {"command": "start_mining", "resource_type": "conductive"})
             for i in range(4)] + [step("R1: mine rares onboard", "/replicants/R1/mine", {"resource_type": "rares"})]
    job = client.portal.call(eng.create_job, "fleets", "Delta Force: mining", None, steps, {}, True)
    job = next(j for j in client.portal.call(eng.jobs) if j["id"] == job["id"])
    assert job["status"] == "done" and job["steps"][-1]["status"] == "done"
    log = client.portal.call(eng.db.kv_get, "automation_log", [])
    assert not any("stopped:" in e["text"] for e in log) and all(e["level"] == "info" for e in log if "skipped" in e["text"])


def test_restart_idle_miners_skips_a_resource_with_no_open_site(client):
    from unittest import mock
    """Live 2026-10-23: three alerts at ISOLITHOR-BELT-1 for 'No conductive remaining at any active site'."""
    eng = client.app.state.worker.automations
    client.post("/automations/rules/restart_idle_miners", data={"enabled": "on", "resource": "carbon", "cooldown_minutes": "10"}, headers=HX)
    sent = []

    async def send(method, path, body, why=""):
        sent.append((path, body))
        return False, None, "No carbon remaining at any active site"
    db = client.app.state.db
    with mock.patch.object(eng, "send", send):
        client.portal.call(eng.rule_restart_idle_miners)
        assert any(b.get("resource_type") == "carbon" for _, b in sent)
        log = client.portal.call(db.kv_get, "automation_log", [])
        assert not any(e["level"] == "alert" and "could not restart" in e["text"] for e in log)
        assert "carbon" in next(iter(client.portal.call(db.kv_get, "miner_no_site", {}).values()))
        n = len(sent)
        client.portal.call(db.kv_set, "miner_restarts", {})   # past the cooldown: carbon is still skipped
        client.portal.call(eng.rule_restart_idle_miners)
        assert not any(b.get("resource_type") == "carbon" for _, b in sent[n:])


def test_restart_idle_miners_mines_what_open_sites_hold_for_the_local_factory(client):
    """Live 2026-10-23 ISOLITHOR-BELT-1: the drones were sent for conductive (short somewhere) while the five open sites
    held carbon, silicates and volatiles and the autofactory there waited for rares and silicates."""
    from unittest import mock
    eng = client.app.state.worker.automations
    db = client.app.state.db
    client.post("/automations/rules/restart_idle_miners", data={"enabled": "on", "resource": "same", "cooldown_minutes": "10"}, headers=HX)
    devs = [{"device_code": "MD1", "device_type": "mining_drone", "status": "idle", "location": "ISO-BELT-1",
             "available_commands": ["start_mining"]},
            {"device_code": "AF1", "device_type": "autofactory", "status": "waiting_for_resources", "location": "ISO-BELT-1",
             "waiting_for": {"rares": {"have": 0, "need": 20}, "silicates": {"have": 27, "need": 40}}}]
    client.portal.call(db.kv_set, "loc:ISO-BELT-1", {"resource_sites": [
        {"designation": "ISO-BELT-1-SITE-4", "resources_remaining_pct": {"carbon": 100, "conductive": 0, "rares": 0}},
        {"designation": "ISO-BELT-1-SITE-6", "resources_remaining_pct": {"carbon": 100, "silicates": 100}}]})
    sent = []

    async def send(method, path, body, why=""):
        sent.append((path, body))
        return True, {}, None

    async def needs(devices):
        return {"conductive": 500.0}

    async def devices(*a, **k):
        return devs
    with mock.patch.object(eng, "send", send), mock.patch.object(eng, "print_needs", needs), \
            mock.patch.object(eng, "devices", devices):
        client.portal.call(eng.rule_restart_idle_miners)
    assert sent == [("/devices/MD1", {"command": "start_mining", "resource_type": "silicates"})]


def test_mining_rigs_work_belts_like_mining_drones():
    """2026-11-01: the Mining template wants 8 mining rigs and no drones; the 21 rigs sat idle at fleet homes because
    only mining drones were handed to the mining controller."""
    from rsweb import ami_schedule as am, fleets as fl
    rig = {"device_code": "RIG", "device_type": "mining_rig", "location": "LAR-BELT-1", "status": "idle",
           "features": ["cruise", "mine", "fracture"], "tags": ["fleet:m1"]}
    assert am.drone_kind(rig) == "mining" and am.runs("mining", "mining_rig")
    ctrl = {"device_code": "MC", "device_type": "ami_mining_controller", "location": "LAR-BELT-1", "status": "idle",
            "tags": ["fleet:m1"]}
    f = {"id": "m1", "name": "Miner 1", "home": "LAR", "station": True, "wants": {"mining_rig": 8}}
    steps, problems = fl.mining_work_steps(f, [ctrl, rig], "LAR-BELT-1", None)
    adopt = next(s for s in steps if (s.get("body") or {}).get("command") == "adopt")
    assert adopt["body"]["devices"] == ["RIG"] and not problems
    assert fl.has_miners(f, [])


def test_salvage_skips_pinned_component_rigs_and_used_up_resources(client):
    """Live 2026-11-12: two component rigs pinned to FALQUORYX-BELT-1 were sent to salvage at FALQUORYX-2-2 and asked for
    conductive, which the salvage no longer had ("No conductive remaining at salvage site")."""
    from rsweb import salvage as sv
    from rsweb.automations import QUIET_SKIP_RE, SALVAGE_GONE_RE
    sal = {"code": "X-2-2-SAL-1", "amounts": {"conductive": 300, "carbon": 20}}
    assert sv.main_resource(sal) == "conductive" and sv.main_resource(sal, ["conductive"]) == "carbon"
    assert sv.main_resource(sal, ["conductive", "carbon"]) is None and sv.main_resource({"code": "Y"}) == "structural"
    assert sv.drone_steps("R1", "X-2-2", sal, ["conductive"])[-1]["body"]["resource_type"] == "carbon"
    err = "No conductive remaining at salvage site"
    assert SALVAGE_GONE_RE.search(err).group(1) == "conductive" and QUIET_SKIP_RE.search(err)
    world, eng = _salvage_setup(client, drop_controller=True)
    for i, site in enumerate(("SOL-BELT-1-SITE-1", "SOL-BELT-1-SITE-2")):
        client.portal.call(client.app.state.worker.handle_event, _ev(300 + i, "site.depleted", site=site))
    db = client.app.state.db
    devs = client.portal.call(db.kv_get, "devices", [])
    for d in devs:
        if d["device_type"] == "mining_drone":
            d["tags"] = ["component", "at:sol-belt-1"]
    client.portal.call(db.kv_set, "devices", devs)
    assert not [x for x in client.portal.call(eng.rule_salvage) if "SAL" in x]   # pinned components stay put
