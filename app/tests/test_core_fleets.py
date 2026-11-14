"""Core tests: fleets, missions, scouting and surveys (split out of test_core.py by test name)."""
from corelib import *  # noqa: F401,F403


def test_survey_targets_order_and_filters():
    from rsweb.automations import survey_targets
    scan = {"planets": [{"designation": "X-2", "orbital_distance_au": 2, "moon_count": 2},
                        {"designation": "X-1", "orbital_distance_au": 1, "moon_count": 0}],
            "asteroid_belt": {"belts": [{"designation": "X-BELT-1", "inner_radius_au": 1.5}]}}
    t = survey_targets(scan, {"X-1": "x"}, include_moons=True, include_belts=True, max_targets=10)
    assert [x["target"] for x in t] == ["X-BELT-1", "X-2", "X-2-1", "X-2-2"]
    assert t[0]["action"] == "search" and t[1]["action"] == "scan"
    assert len(survey_targets(scan, {}, False, False, 1)) == 1


def test_auto_survey_end_to_end_against_mock(client):
    world = client.app.state.api.http._transport.app.state.world
    world.move_seconds = 0.03
    eng = client.app.state.worker.automations
    client._seen = len(world.events)
    # switch the rule on through the UI endpoint, limit to 3 bodies, keep return-and-stow
    r = client.post("/automations/rules/auto_survey", data={"enabled": "on", "use_idle": "", "include_belts": "on",
                                                            "max_targets": "3", "return_and_stow": "on"}, headers=HX)
    assert "saved" in r.text
    # the vessel arrives at SOL-BELT-1
    arrival = {"id": "9999999999999-0", "event": "travel.arrived", "category": "travel", "device_code": "11ADA230",
               "device_type": "heaven_vessel", "location": "SOL-BELT-1", "star": "SOL",
               "payload": {"destination": "SOL-BELT-1", "origin": "ABOTEIN-OORT", "travel_type": "surge"}, "created_at": iso(datetime.now(timezone.utc))}
    client.portal.call(client.app.state.worker.handle_event, arrival)

    async def finished():
        jobs = await eng.jobs()
        return jobs and all(j["status"] not in ("running", "waiting") for j in jobs)
    _pump(client, finished)
    jobs = client.portal.call(eng.jobs)
    assert len(jobs) == 1, jobs
    job = jobs[0]
    assert job["status"] == "done", [(s["desc"], s["status"], s["error"]) for s in job["steps"]]
    descs = [s["desc"] for s in job["steps"]]
    assert descs[0] == "deploy SV000001" and descs[-1].startswith("stow SV000001")
    assert sum(1 for d in descs if d.startswith(("scan ", "search "))) == 3
    surveyed = client.portal.call(client.app.state.db.kv_get, "surveyed")
    assert set(job["meta"]["targets"]) <= set(surveyed)
    assert "finished: survey SOL" in client.get("/automations", headers=H).text
    # arriving again: those 3 are done, so the next 3 get surveyed
    arrival["id"] = "9999999999999-1"
    client.portal.call(client.app.state.worker.handle_event, arrival)
    _pump(client, finished)
    jobs = client.portal.call(eng.jobs)
    assert len(jobs) == 2 and not set(jobs[0]["meta"]["targets"]) & set(jobs[1]["meta"]["targets"])


def test_auto_survey_uses_carried_ami_survey_controller(client):
    world = client.app.state.api.http._transport.app.state.world
    world.devices.append({"device_code": "SC000001", "device_type": "ami_survey_controller", "location": "SOL-BELT-1",
                          "status": "stowed", "replicant_code": "77F75255", "features": ["cruise", "ami", "stow"],
                          "available_commands": ["deploy", "adopt", "set_directive", "launch", "stow"]})
    world.move_seconds = 0.03
    client._seen = len(world.events)
    client.post("/automations/rules/auto_survey", data={"enabled": "on", "use_ami": "on", "include_belts": "on",
                                                        "max_targets": "5", "return_and_stow": "on"}, headers=HX)
    _arrive(client, 7)
    eng = client.app.state.worker.automations

    async def finished():
        return all(j["status"] not in ("running", "waiting") for j in await eng.jobs())
    _pump(client, finished)
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "auto_survey"][-1]
    assert job["meta"].get("ami") and job["status"] == "done", [(s["desc"], s["status"], s["error"]) for s in job["steps"]]
    cmds = [(s["path"], s["body"]["command"]) for s in job["steps"]]
    assert cmds[0] == ("/devices/SC000001", "deploy") and ("/devices/SV000001", "deploy") in cmds
    final = job["steps"][-2]["body"]
    assert final["directive"] == "survey_system" and final["configuration"] == {"planets": "all", "moons": "none", "recall": True}


def test_auto_survey_leaves_fleet_passengers_aboard(client):
    """Live 2026-10-21: Surveyor 1's heaven vessel was flown into PHEVENIA and auto survey deployed the fleet's survey
    controller and a drone out of it while the fleet was boarding them for the trip home."""
    world = client.app.state.api.http._transport.app.state.world
    world.devices.append({"device_code": "SC000002", "device_type": "ami_survey_controller", "location": "SOL-BELT-1",
                          "status": "stowed", "replicant_code": "77F75255", "features": ["cruise", "ami", "stow"],
                          "tags": ["fleet:surveyor-x"], "available_commands": ["deploy", "adopt", "set_directive", "launch", "stow"]})
    world.move_seconds = 0.03
    client._seen = len(world.events)
    client.portal.call(client.app.state.worker.sync_devices)
    client.post("/automations/rules/auto_survey", data={"enabled": "on", "use_ami": "on", "include_belts": "on",
                                                        "max_targets": "5", "return_and_stow": "on"}, headers=HX)
    _arrive(client, 8)
    eng = client.app.state.worker.automations
    steps = [s["path"] for j in client.portal.call(eng.jobs) if j["rule"] == "auto_survey" for s in j["steps"]]
    assert steps and "/devices/SC000002" not in steps, steps


def test_fleet_materials_setting(client):
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(client.app.state.worker.sync_inventory)
    client.post("/fleets", data={"name": "Sol home", "home": "SOL", "station": "on"}, headers=HX)
    client.post("/fleets", data={"name": "Depot", "home": "ABOTEIN", "station": "on"}, headers=HX)
    r = client.post("/fleets/sol-home/station", data={"station": "on", "materials": "depot"}, headers=HX)
    assert r.headers.get("HX-Refresh")
    client.post("/fleets/depot/station", data={"station": "on", "materials": "self"}, headers=HX)
    client.post("/fleets/depot/station", data={"station": "on", "materials": "no-such-fleet"}, headers=HX)
    fleets = {f["id"]: f for f in client.portal.call(client.app.state.db.kv_get, "fleets")}
    assert fleets["sol-home"]["materials"] == "depot" and fleets["depot"]["materials"] == ""   # unknown fleet: cleared
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "materials from Sol home" in page
    assert 'value="depot" selected' in client.get("/fleets/overview", headers=HX).text   # its route (or why not) is listed


def test_arrival_rules_ignore_in_system_hops_and_surveyed_systems(client):
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.worker.sync_devices)
    client.get("/systems/SOL", headers=H)  # cache the scan
    client.post("/automations/rules/auto_survey", data={"enabled": "on", "use_idle": "on", "include_belts": "on",
                                                        "max_targets": "20", "use_ami": "on"}, headers=HX)

    def arrive(n, **payload):
        client.portal.call(client.app.state.worker.handle_event, {
            "id": f"77777777777{n:02d}-0", "event": "travel.arrived", "device_code": "TC000001", "device_type": "transport",
            "location": "SOL-BELT-1", "payload": {"destination": "SOL-BELT-1", **payload}, "created_at": iso(datetime.now(timezone.utc))})

    def survey_jobs():
        return [j for j in client.portal.call(eng.jobs) if j["rule"] == "auto_survey"]

    arrive(1, origin="SOL-3")                     # a transport hopping inside SOL: nothing happens
    assert survey_jobs() == []
    # an AMI survey controller finishing survey_system marks every body in SOL as surveyed …
    client.portal.call(client.app.state.worker.handle_event, {
        "id": "7777777777799-0", "event": "directive.completed", "device_code": "SC000001", "device_type": "ami_survey_controller",
        "location": "SOL-BELT-1", "star": "SOL", "payload": {"directive": "survey_system"}, "created_at": "2026-09-30T12:00:00+00:00"})
    surveyed = client.portal.call(client.app.state.db.kv_get, "surveyed")
    assert {"SOL-3", "SOL-5", "SOL-BELT-1"} <= set(surveyed)
    # … so even a surge arrival from another system doesn't start a survey there again
    arrive(2, origin="ABOTEIN-OORT", travel_type="surge")
    assert survey_jobs() == []


def test_ami_survey_not_restarted_on_every_arrival(client):
    eng = client.app.state.worker.automations

    async def go():
        started = {"SOL": __import__("rsweb.db", fromlist=["now_iso"]).now_iso()}
        await client.app.state.db.kv_set("ami_survey_started", started)
        await eng.rule_auto_survey("11ADA230", "SOL-BELT-1", "SOL", [], {"use_ami": True})
        return [j for j in await eng.jobs() if j["rule"] == "auto_survey"]
    assert client.portal.call(go) == []


def test_reopen_sites_with_ami_survey_controller(client):
    eng = _enable(client, "reopen_sites", "salvage_when_depleted")
    devices = [
        {"device_code": "84EE1EF1", "device_type": "ami_mining_controller", "location": "AEMEROTH-BELT-1", "status": "coordinating",
         "ami_directive": {"_eval_state": "exhausted:['carbon']:AEMEROTH-4-2", "name": "gather_evenly", "config": {}}},
        {"device_code": "72779B6A", "device_type": "ami_survey_controller", "location": "AEMEROTH-BELT-1", "status": "coordinating",
         "ami_directive": {"_eval_state": "no_targets:recalling", "name": "survey_system", "config": {}}},
        {"device_code": "A9B9B55B", "device_type": "survey_drone", "location": "AEMEROTH-6-33", "status": "idle",
         "controller_device_code": "72779B6A"},
        {"device_code": "FREE0001", "device_type": "survey_drone", "location": "AEMEROTH-3", "status": "idle"},
    ]
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    done = client.portal.call(eng.rule_reopen_sites)
    assert done == ["72779B6A → belt_search AEMEROTH-BELT-1"]
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "reopen_sites"][-1]
    bodies = [s["body"] for s in job["steps"]]
    assert {"command": "adopt", "devices": ["FREE0001"]} in bodies        # tops it up to 2 drones
    assert {"command": "set_directive", "directive": "belt_search", "configuration": {}} in bodies
    # the salvage rule leaves the mining controller mining while sites are being re-opened
    assert client.portal.call(eng.rule_salvage) == []
    # once a drone is tracking a site there, and the controller is searching, nothing more happens
    devices[2].update(location="AEMEROTH-BELT-1", status="tracking")
    devices[3].update(location="AEMEROTH-BELT-1", status="searching")
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(client.app.state.db.kv_set, "reopen_state", {})
    assert client.portal.call(eng.rule_reopen_sites) == []


def test_reopen_sites_resumes_a_paused_survey_controller(client):
    """Live 2026-11-10: the Season 4 time skip paused belt_search controllers on a stale belt_capacity state, holding
    their surveyors idle at belts with no open site."""
    eng = _enable(client, "reopen_sites")
    devices = [
        {"device_code": "639A138D", "device_type": "ami_survey_controller", "location": "FENIRON-BELT-1", "status": "paused",
         "ami_directive_status": "paused",
         "ami_directive": {"_eval_state": "belt_capacity:3+2/5", "name": "belt_search", "config": {}}},
        {"device_code": "BS000001", "device_type": "belt_surveyor", "location": "FENIRON-BELT-1", "status": "idle",
         "controller_device_code": "639A138D"},
    ]
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(client.app.state.db.kv_set, "managed_by", {"BS000001": "639A138D"})
    assert client.portal.call(eng.rule_reopen_sites) == ["639A138D → belt_search FENIRON-BELT-1 (resumed)"]
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "reopen_sites"][-1]
    assert [s["body"]["command"] for s in job["steps"]] == ["set_directive", "launch"]
    assert client.portal.call(eng.rule_reopen_sites) == []   # one job at a time


def test_reopen_sites_prefers_belt_surveyors(client):
    eng = _enable(client, "reopen_sites")
    devices = [
        {"device_code": "72779B6A", "device_type": "ami_survey_controller", "location": "FAL-BELT-1", "status": "idle"},
        {"device_code": "SD1", "device_type": "survey_drone", "location": "FAL-1", "status": "idle"},
        {"device_code": "BS1", "device_type": "belt_surveyor", "location": "FAL-2", "status": "idle", "features": ["cruise", "survey"]},
    ]
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(client.app.state.db.kv_set, "exhausted_places", {"FAL-BELT-1": __import__("rsweb.db", fromlist=["x"]).now_iso()})
    done = client.portal.call(eng.rule_reopen_sites)
    # the controller adopts the belt surveyor first, then the survey drone
    assert done == ["72779B6A → belt_search FAL-BELT-1"]
    adopt = [s["body"] for j in client.portal.call(eng.jobs) if j["rule"] == "reopen_sites" for s in j["steps"]
             if (s.get("body") or {}).get("command") == "adopt"]
    assert adopt == [{"command": "adopt", "devices": ["BS1", "SD1"]}]
    from rsweb import ami_schedule as am
    here = [{"device_code": "SD0", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "idle"},
            {"device_code": "BS9", "device_type": "belt_surveyor", "location": "FAL-BELT-1", "status": "idle"}]
    assert am.adoptable(here, devices[0], {}) == ["BS9", "SD0"] and am.drone_kind(here[1]) == "survey"
    from rsweb import sites
    assert [d["device_code"] for d in sites.surveyors_first(devices[1:])] == ["BS1", "SD1"]
    devices[2].update(location="FAL-BELT-1", status="tracking")
    assert [d["device_code"] for d in sites.survey_at(devices, "FAL-BELT-1")] == ["BS1"]


def test_fleet_roster_assemble_and_travel():
    from rsweb import fleets as fl
    fleet, devices = _fleet_world()
    r = fl.roster(fleet, devices)
    assert r["capacity"] == 36 and "LOCAL001" not in [d["device_code"] for d in r["members"]]
    md = next(x for x in r["rows"] if x["type"] == "mining_drone")
    assert md["have"] == 3 and md["short"] == 1 and "MD000002" in r["riding"]
    steps, problems = fl.assemble_steps(fleet, devices)
    bodies = [(s["path"], s["body"]) for s in steps]
    # the survey pair at the belt fly to the carrier first; the freighter flies itself, so it doesn't board
    assert ("/devices/SC000001", {"command": "travel", "destination": "AEM-5-L4"}) in bodies
    assert ("/devices/MF000001", {"command": "attach", "device": "MD000001"}) in bodies
    assert ("/devices/MF000001", {"command": "attach", "device": "MD000002"}) not in bodies     # already on board
    assert not any(b and b.get("device") == "CF000001" for _, b in bodies)
    assert not any("LOST0001" in p for p in problems)          # left to the gather phase
    trav = [(s["path"], s["body"]) for s in fl.travel_steps(fleet, devices, "KEL", {"KEL": {"entry_point": "KEL-3-L4"}}) if s["body"]]
    assert sorted(trav) == [("/devices/CF000001", {"command": "travel", "destination": "KEL-3-L4"}),
                            ("/devices/MF000001", {"command": "travel", "destination": "KEL-3-L4"})]


def test_fleet_mining_work_watch_and_phases():
    from rsweb import fleets as fl
    fleet, devices = _fleet_world()
    for d in devices:
        if fl.fleet_of(d):
            d["location"] = "KEL-3-L4"
    steps, problems = fl.mining_work_steps(fleet, devices, "KEL-BELT-1", "FAL-BELT-1")
    bodies = [s["body"] for s in steps if s["body"]]
    assert {"command": "set_directive", "directive": "belt_search", "configuration": {}} in bodies
    assert {"command": "set_directive", "directive": "gather_evenly", "configuration": {}} in bodies
    assert {"command": "set_directive", "directive": "ferry", "configuration": {"collect": "KEL-BELT-1", "deliver": "FAL-BELT-1"}} in bodies
    assert {"command": "adopt", "devices": ["MD000001", "MD000002", "LOST0001"]} in bodies and not problems
    # watch: exhausted, nothing searching → done only after the time limit
    mc = next(d for d in devices if d["device_code"] == "MC000001")
    mc["ami_directive"] = {"name": "gather_evenly", "_eval_state": "exhausted:['rares']:KEL-BELT-1"}
    m = {"opts": {"exhausted_minutes": 30}}
    done, why, upd = fl.watch_done(fleet, m, devices, "2026-10-02T10:00:00+00:00")
    assert not done and upd["exhausted_since"] == "2026-10-02T10:00:00+00:00"
    m.update(upd)
    assert fl.watch_done(fleet, m, devices, "2026-10-02T10:31:00+00:00")[0]
    next(d for d in devices if d["device_code"] == "SD000001")["status"] = "searching"
    assert not fl.watch_done(fleet, m, devices, "2026-10-02T10:31:00+00:00")[0]      # still finding sites
    # explore runs work→watch→recall per target, then goes home
    m = {"phase": "recall", "idx": 0, "targets": ["A", "B"]}
    assert fl.next_phase("explore", m) == "travel" and m["idx"] == 1
    m["phase"] = "recall"
    assert fl.next_phase("explore", m) == "return"
    assert fl.next_phase("mining", {"phase": "unload"}) is None


def test_a_mission_stalled_out_of_comms_range_resumes_once_back_in_range(client):
    """Live 2026-10-22: Surveyor 2's heaven vessel left ALFOLEN, the recall failed on 'Device is out of comms range'
    and the mission sat stalled. Now it waits and re-runs the phase once every member is in range again."""
    db, eng = client.app.state.db, client.app.state.worker.automations
    devs = [{"device_code": "SC1", "device_type": "ami_survey_controller", "location": "ALF", "status": "out_of_range",
             "in_control_range": False, "tags": ["fleet:s2"]}]
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(eng.save_fleets, [{"id": "s2", "name": "S2", "role": "explore", "home": "FAL", "wants": {},
                                          "mission": {"status": "stalled", "phase": "recall", "comms_wait": True,
                                                      "targets": ["ALF"], "idx": 0, "log": []}}])
    client.portal.call(eng.run_fleets)
    assert client.portal.call(eng.fleets)[0]["mission"]["status"] == "stalled"
    devs[0]["in_control_range"] = True
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(eng.run_fleets)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert "comms_wait" not in m and any("back in comms range" in x["text"] for x in m["log"])


def test_fleet_pages_and_mission_launch(client):
    eng = client.app.state.worker.automations
    r = client.post("/fleets", data={"name": "Prospector 1", "role": "mining", "home": "SOL"}, headers=HX)
    assert r.headers.get("HX-Refresh")
    fleet, devices = _fleet_world()
    for d in devices:
        if fl_tag := next((t for t in d["tags"] if t.startswith("fleet:")), None):
            d["tags"] = ["fleet:prospector-1"]
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Prospector 1" in page and "fleet:prospector-1" in page and "attach 7 / 36" in page
    client.post("/fleets/prospector-1/edit", data={"name": "Prospector 1", "role": "mining", "home": "SOL",
                                                   "want:mining_drone": "4", "want:mobile_fleet": "1"}, headers=HX)
    r = client.post("/fleets/prospector-1/mission", data={"targets": "ABOTEIN", "exhausted_minutes": "30"}, headers=HX)
    if "loadout not complete" in r.text:   # nothing leaves short unless you say so
        assert "mission" not in client.portal.call(eng.fleets)[0]
        r = client.post("/fleets/prospector-1/mission", data={"targets": "ABOTEIN", "exhausted_minutes": "30", "partial": "on"},
                        headers=HX)
    f = client.portal.call(eng.fleets)[0]
    assert f["mission"]["status"] == "running" and f["mission"]["phase"] == "assemble"
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "fleets" and j.get("meta", {}).get("fleet") == "prospector-1"][-1]
    assert any((s["body"] or {}).get("command") == "attach" for s in job["steps"])
    # the mock doesn't know these devices, so boarding fails → the mission stalls and says why
    client.portal.call(eng.run_fleets)
    f = client.portal.call(eng.fleets)[0]
    assert f["mission"]["status"] == "stalled" and "stalled in assemble" in f["mission"]["log"][-1]["text"]
    assert "Retry" in client.get("/fleets/details?all=1", headers=H).text
    # membership: adding a device swaps its home/spare tags for the fleet tag
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    world.devices[1]["tags"] = ["home:sol", "spare"]
    client.portal.call(client.app.state.worker.sync_devices)
    client.post("/fleets/prospector-1/members", data={"add": world.devices[1]["device_code"]}, headers=HX)
    assert world.devices[1]["tags"] == ["fleet:prospector-1"]


def test_fleet_print_shortfall_tags_prints(client):
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.worker.sync_devices)
    world = client.app.state.api.http._transport.app.state.world
    world.devices.append({"device_code": "0000AAAA", "device_type": "heaven_vessel", "location": "SOL-BELT-1",
                          "status": "stationary", "available_commands": ["enqueue_print", "travel"]})
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(eng.save_fleets, [{"id": "p1", "name": "P1", "role": "mining", "home": "SOL", "wants": {"mining_drone": 2}}])
    r = client.post("/fleets/p1/print", headers=HX)   # never on the vessel (owner 2026-11-07)
    assert "print 2× mining drone" in r.text
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "chain"][-1]
    assert job["steps"][0]["path"] == "/devices/AF00BEEF"
    assert job["steps"][0]["body"] == {"command": "enqueue_print", "device_type": "mining_drone", "quantity": 2, "tags": ["fleet:p1"]}


def test_recall_waiting_drones_join_their_systems_controller():
    # live 2026-10-28: drones a controller let go mid-recall stood recall_waiting for weeks, never adopted
    from rsweb.ami_schedule import handoffs, adoptable
    w = _arrival_world()
    for d in w:
        if d["device_code"] == "MD1":
            d["status"] = "recall_waiting"
    assert [(h["drone"], h["controller"]) for h in handoffs(w, {}, set())][0] == ("MD1", "MC1")
    ctrl = next(d for d in w if d["device_code"] == "MC1")
    ctrl = dict(ctrl, location="ITH-2-L4")
    assert "MD1" in adoptable(w, ctrl, {})


def test_a_depleted_salvage_brings_recall_waiting_drones_back_to_the_belt():
    """Live 2026-10-22: 84EE1EF1 at AEMEROTH-BELT-1 on gather_salvage, state 'depleted:recalling', its drones
    recall_waiting at AEMEROTH-5-L4 and none mining."""
    from rsweb import salvage as sv
    c = {"device_code": "C1", "device_type": "ami_mining_controller", "location": "AEMEROTH-BELT-1", "status": "coordinating",
         "ami_directive": {"name": "gather_salvage", "config": {"location": "KELCERUM-5-13", "recall": True},
                           "_eval_state": "depleted:recalling"}}
    kids = [{"device_code": f"D{i}", "device_type": "mining_drone", "location": "AEMEROTH-5-L4", "status": "recall_waiting",
             "controller_device_code": "C1"} for i in range(2)]
    plans = sv.back_to_belt_plan([c], [c, *kids], {}, {"AEMEROTH-BELT-1": 1}, {"AEMEROTH": ["AEMEROTH-BELT-1"]}, set())
    assert len(plans) == 1 and plans[0]["away"] == ["D0", "D1"] and plans[0]["directive"] == "gather_evenly"
    kids[0]["status"] = "recalling"   # really on its way: left alone
    assert sv.back_to_belt_plan([c], [c, *kids], {}, {"AEMEROTH-BELT-1": 1}, {"AEMEROTH": ["AEMEROTH-BELT-1"]},
                                set())[0]["away"] == ["D1"]


def test_fleet_end_mission_and_board(client):
    eng = client.app.state.worker.automations
    fleet, devices = _fleet_world()
    fleet["mission"] = {"status": "running", "phase": "watch", "targets": ["AEM"], "belt": "AEM-BELT-1", "log": []}
    for d in devices:
        if d["device_code"] == "MC000001":
            d["ami_directive"] = {"name": "gather_evenly"}
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(eng.save_fleets, [fleet])
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "End mission &amp; board" in page and "Recall now" in page and "Recall after task" in page
    r = client.post(f"/fleets/{fleet['id']}/control", data={"action": "end"}, headers=HX)
    assert r.headers.get("HX-Refresh")
    f = client.portal.call(eng.fleets)[0]
    m = f["mission"]
    assert m["phase"] == "recall" and m.get("end_started") and m["status"] in ("running", "stalled")
    job = next(j for j in client.portal.call(eng.jobs) if j["id"] == m["job"]) if m.get("job") else None
    assert job and job["title"].endswith("end mission & board")
    cmds = [(s["path"], (s["body"] or {}).get("command")) for s in job["steps"]]
    assert ("/devices/MC000001", "clear_directive") in cmds
    assert not any(c == "collect_resources" for _, c in cmds)          # ending early: no hauling
    assert any(c in ("attach", "stow") for _, c in cmds)                # everyone boards a carrier
    assert not any(p.endswith("MF000001") and c == "travel" for p, c in cmds)   # no trip home
    # once the recall job is over, the mission ends where it is
    jobs = client.portal.call(eng.jobs)
    for j in jobs:
        if j["id"] == m["job"]:
            j["status"] = "done"
    client.portal.call(eng.save_jobs, jobs)
    f = client.portal.call(eng.fleets)[0]
    f["mission"]["status"] = "running"
    client.portal.call(eng.save_fleets, [f])
    client.portal.call(eng.run_fleets)
    f = client.portal.call(eng.fleets)[0]
    assert f["mission"]["status"] == "ended" and "ended early" in f["mission"]["log"][-1]["text"]


def test_fleet_board_everyone_without_a_mission(client):
    eng = client.app.state.worker.automations
    fleet, devices = _fleet_world()
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(eng.save_fleets, [fleet])
    assert "Board everyone" in client.get("/fleets/details?all=1", headers=H).text
    client.post(f"/fleets/{fleet['id']}/control", data={"action": "end"}, headers=HX)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["phase"] == "recall" and m.get("end_started")


def test_audit_flags_non_members_run_by_a_fleet_controller():
    from rsweb import loadouts as lo
    devs = [{"device_code": "C1", "device_type": "ami_transport_controller", "location": "FAL-BELT-1", "tags": ["fleet:prospectors"]},
            {"device_code": "H1", "device_type": "transport_hauler", "location": "FAL-BELT-1", "status": "idle",
             "controller_device_code": "C1", "tags": ["home:fal"]}]
    cfg = {"phases": [], "systems": {}}
    p = lo.plan(cfg, devs, [], {}, {}, {}, set(), [], {})
    issues = lo.audit(cfg, devs, {}, p)
    assert any(a["code"] == "H1" and "controller of fleet:prospectors, but not in that fleet" in a["issue"] and not a["fixed"] for a in issues)


# --- 1.12.3: engine lock deadlock, already-deployed, home-tagged keepers -----------------------------------------------

def test_fleet_control_on_a_stalled_mission_does_not_deadlock(client):
    """Live 2026-10-04: Stop/Resume/End on a stalled fleet took the engine lock, then cancel() took it again —
    the request hung holding it and every tick and event waited behind it for ~43 h."""
    import threading
    eng = client.app.state.worker.automations
    fleet, devices = _fleet_world()
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(eng.save_jobs, [{"id": "fleets-1-0", "rule": "fleets", "title": "x: deploy", "device": None,
                                        "steps": [], "idx": 0, "status": "running", "created_at": "2026-10-04T02:54:07+00:00",
                                        "meta": {}}])
    fleet["mission"] = {"status": "stalled", "phase": "deploy", "targets": ["AEM"], "job": "fleets-1-0", "log": []}
    client.portal.call(eng.save_fleets, [fleet])
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", client.post(f"/fleets/{fleet['id']}/control",
                                                                          data={"action": "stop"}, headers=HX)), daemon=True)
    t.start()
    t.join(15)
    assert "r" in out, "fleet control hung (engine lock deadlock)"
    assert not eng.lock.locked()
    assert next(j for j in client.portal.call(eng.jobs) if j["id"] == "fleets-1-0")["status"] == "cancelled"
    assert client.portal.call(eng.fleets)[0]["mission"]["status"] == "stopped"
    # the reported follow-on symptom: adding a device to a fleet hung too (it waits for the same lock)
    code = next(d["device_code"] for d in devices if "fleet:" not in " ".join(d.get("tags") or []))
    out2 = {}
    t = threading.Thread(target=lambda: out2.setdefault("r", client.post(f"/fleets/{fleet['id']}/members",
                                                                           data={"add": code}, headers=HX)), daemon=True)
    t.start()
    t.join(15)
    assert "r" in out2 and any("membership" in j["title"] for j in client.portal.call(eng.jobs))


def test_explore_fleet_without_a_controller_scans_with_its_drones():
    from rsweb import fleets as fl
    fleet = {"id": "s", "name": "S", "role": "explore", "home": "AAA"}
    devs = [{"device_code": "SD", "device_type": "survey_drone", "location": "BBB-2", "status": "idle", "tags": ["fleet:s"],
             "controller_device_code": None}]
    steps, problems = fl.explore_work_steps(fleet, devs)
    assert [(st["path"], st["body"]) for st in steps] == [("/devices/SD", {"command": "scan"})] and not problems
    devs[0]["location"] = "BBB-2-L4"                                                     # no survey from a Lagrange point
    steps, _ = fl.explore_work_steps(fleet, devs, "BBB-BELT-1")
    sends = [(st["path"], st["body"]) for st in steps if st["method"] != "WAIT"]
    assert sends == [("/devices/SD", {"command": "travel", "destination": "BBB-BELT-1"}), ("/devices/SD", {"command": "scan"})]


def test_explore_fleet_takes_survey_controller_to_the_belt():
    from rsweb import fleets as fl
    fleet = {"id": "s", "name": "S", "role": "explore", "home": "AAA"}
    devs = [{"device_code": "SC", "device_type": "ami_survey_controller", "location": "BBB-5-L4", "status": "idle", "tags": ["fleet:s"]},
            {"device_code": "SD", "device_type": "survey_drone", "location": "BBB-5-L4", "status": "idle", "tags": ["fleet:s"]}]
    steps, _ = fl.explore_work_steps(fleet, devs, "BBB-BELT-1")
    bodies = [(st["path"], st["body"]) for st in steps]
    assert bodies[0] == ("/devices/SC", {"command": "travel", "destination": "BBB-BELT-1"})
    assert ("/devices/SD", {"command": "travel", "destination": "BBB-BELT-1"}) in bodies
    i_adopt = next(i for i, b in enumerate(bodies) if (b[1] or {}).get("command") == "adopt")
    assert all(st["method"] == "WAIT" for st in steps[2:i_adopt])


def test_missions_cannot_target_a_system_with_a_home_fleet(client):
    eng = client.app.state.worker.automations
    client.post("/fleets", data={"name": "Scouts", "role": "explore", "home": "SOL"}, headers=HX)
    # an old config (home fleets by system): KEL's becomes a stationed fleet on the next load
    client.portal.call(client.app.state.db.kv_set, "loadouts",
                       {"phases": [{"id": "p", "name": "Outpost", "order": 1, "wants": {"survey_drone": 1}}],
                        "systems": {"KEL": "p", "ABC": ""}})
    page = client.get("/fleets/details?all=1", headers=H).text
    targets = page[page.index('<datalist id="fl-stars-scouts">'):]
    targets = targets[:targets.index("</datalist>")]
    assert "Not available as targets" in page and '"KEL"' not in targets
    kel = next(f for f in client.portal.call(eng.fleets) if f["home"] == "KEL")
    assert kel["station"] and kel["template"] == "p" and kel["wants"] == {"survey_drone": 1}
    assert "systems" not in client.portal.call(client.app.state.db.kv_get, "loadouts")
    r = client.post("/fleets/scouts/mission", data={"targets": "ABOTEIN, KEL-BELT-1"}, headers=HX)
    assert "KEL-BELT-1: has a stationed fleet" in r.text
    assert not client.portal.call(eng.fleets)[0].get("mission")
    r = client.post("/fleets/scouts/mission", data={"targets": "ABOTEIN"}, headers=HX)   # no fleet stationed there
    assert r.headers.get("HX-Refresh") and client.portal.call(eng.fleets)[0]["mission"]["targets"] == ["ABOTEIN"]


def test_fleet_follows_a_template():
    from rsweb import fleets as fl
    cfg = {"phases": [{"id": "pros", "name": "Prospector", "wants": {"mining_drone": 6, "survey_drone": "", "mobile_fleet": 1,
                                                                      "cargo_freighter": 0}}]}
    f = fl.resolve_template({"id": "x", "wants": {"mining_drone": 2}, "template": "pros"}, cfg)
    assert f["wants"] == {"mining_drone": 6, "mobile_fleet": 1}          # blank and 0 mean none for a fleet
    cfg["phases"][0]["wants"]["mining_drone"] = 8                         # editing the template updates the fleet
    assert fl.resolve_template(f, cfg)["wants"]["mining_drone"] == 8
    gone = fl.resolve_template({"id": "y", "wants": {"mining_drone": 3}, "template": "deleted"}, cfg)
    assert gone["wants"] == {"mining_drone": 3}                           # a deleted template leaves the last loadout


def test_fleet_fill_stays_in_sector_range():
    from rsweb import fleets as fl
    from rsweb.logistics import SECTOR_RANGE_LY
    fleet, devices = _fleet_world()
    S = lambda code, loc: {"device_code": code, "device_type": "mining_drone", "location": loc, "status": "idle",  # noqa: E731
                           "tags": ["spare"], "available_commands": ["travel"]}
    stars = {"AEM": {"position": {"x": 0, "y": 0, "z": 0}, "entry_point": "AEM-5-L4"},
             "FAR": {"position": {"x": SECTOR_RANGE_LY + 50, "y": 0, "z": 0}, "entry_point": "FAR-4-L4"},
             "FAL": {"position": {"x": 600, "y": 0, "z": 0}, "entry_point": "FAL-4-L4"}}
    # 0 (any) and a setting past the sector range both stop at the sector range; an uncharted star counts as far
    for reach in (0, 5000):
        _, recruits, _ = fl.fill_plan(fleet, devices + [S("SPAREFAR", "FAR-BELT-1"), S("SPAREDLT", "DELTA-BELT-1")],
                                      stars, set(), reach=reach)
        assert recruits == []
    _, recruits, _ = fl.fill_plan(fleet, devices + [S("SPAREFAR", "FAR-BELT-1"), S("SPAREFAL", "FAL-BELT-1")], stars, set())
    assert [d["device_code"] for d in recruits] == ["SPAREFAL"]
    # no carrier: self-surging spares only from within range, nearest first
    solo = {**fleet, "id": "solo", "name": "Solo", "wants": {"propulsor": 1}}
    P = lambda code, loc: {"device_code": code, "device_type": "propulsor", "location": loc, "status": "idle",  # noqa: E731
                           "tags": ["spare"], "features": ["surge"], "available_commands": ["travel"]}
    mine = [{"device_code": "SOLO0001", "device_type": "survey_drone", "location": "AEM-5-L4", "status": "idle", "tags": ["fleet:solo"]}]
    _, recruits, notes = fl.fill_plan(solo, mine + [P("PROPFAR1", "FAR-BELT-1")], stars, set())
    assert recruits == [] and any("supply range" in n for n in notes)
    _, recruits, _ = fl.fill_plan(solo, mine + [P("PROPFAR1", "FAR-BELT-1"), P("PROPFAL1", "FAL-BELT-1")], stars, set())
    assert [d["device_code"] for d in recruits] == ["PROPFAL1"]
    assert fl.beyond_reach("DELTA", "ISOLITHOR", {}, 100) is False        # both uncharted: can't tell


def test_migrate_home_fleets_and_roles_to_fleets():
    from rsweb import fleets as fl
    cfg = {"phases": [{"id": "p", "name": "Outpost", "wants": {"mining_drone": 2, "survey_drone": "0"}}],
           "systems": {"AAA": "p", "BBB": "p", "CCC": "gone"}, "roles": {"AAA": "source", "DDD": "destination", "EEE": "destination"}}
    mobile = [{"id": "scouts", "name": "Scouts", "role": "explore", "home": "AAA", "wants": {"survey_drone": 2}}]
    pos = {"AAA": {"x": 0, "y": 0, "z": 0}, "DDD": {"x": 9, "y": 0, "z": 0}, "EEE": {"x": 2, "y": 0, "z": 0}}
    new, fleets, changed = fl.migrate(cfg, mobile, pos)
    assert changed and new["fleets_migrated"] and "systems" not in new and "roles" not in new
    by = {f["id"]: f for f in fleets}
    assert by["scouts"]["station"] is False and by["scouts"]["materials"] == ""      # mobile fleets carry on as before
    assert by["aaa-home"] == {"id": "aaa-home", "name": "Aaa home", "role": "mining", "home": "AAA", "wants": {},
                              "station": True, "materials": "eee-home", "template": "p"}   # nearest destination
    assert by["bbb-home"]["template"] == "p" and "ccc-home" not in by                  # unknown phase: nothing
    assert by["ddd-home"]["materials"] == by["eee-home"]["materials"] == "self"
    assert fl.migrate(new, fleets) == (new, fleets, False)                             # only once
    f = fl.resolve_template(dict(by["aaa-home"]), new)
    assert f["wants"] == {"mining_drone": 2} and fl.station_wants(f) == {"mining_drone": 2, "survey_drone": 0}


def test_two_stationed_fleets_share_a_system():
    from rsweb import loadouts as lo
    D = lambda code, t, tags=(): {"device_code": code, "device_type": t, "location": "AAA-BELT-1", "status": "idle",  # noqa: E731
                                  "tags": list(tags)}
    devices = [D("M1", "mining_drone", ["fleet:miners"]), D("M2", "mining_drone"), D("M3", "mining_drone"), D("M4", "mining_drone"),
               D("S1", "survey_drone"), D("T1", "transport_drone")]
    fleets = [_st("miners", "AAA", {"mining_drone": 2}), _st("reserve", "AAA", {"mining_drone": 1, "survey_drone": 0})]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, {}, {}, set(), [], {})
    joins = {c: t for c, tags in p["tag_add"].items() for t in tags if t.startswith("fleet:")}
    assert joins == {"M2": "fleet:miners", "M3": "fleet:reserve"}      # members first, then fleetless ones, in fleet order
    assert p["tag_add"]["M4"] == ["spare"] and p["tag_add"]["S1"] == ["spare"]   # counted there (S1: a 0 line) but not taken
    assert "T1" not in p["tag_add"]                                     # nobody counts transport drones: don't care
    assert {r["fleet"]["id"] for r in p["report"].values()} == {"miners", "reserve"}


def test_stationed_fleet_extras_and_template_zero_leave_the_fleet():
    from rsweb import loadouts as lo
    devices = [{"device_code": c, "device_type": t, "location": "AAA-BELT-1", "status": "idle", "tags": ["fleet:home"]}
               for c, t in (("M1", "mining_drone"), ("M2", "mining_drone"), ("S1", "survey_drone"))]
    cfg = {"phases": [{"id": "p", "name": "P", "wants": {"mining_drone": 1, "survey_drone": "0"}}], "fleets_migrated": True}
    from rsweb import fleets as fl
    cfg["fleets"] = [fl.resolve_template(_st("home", "AAA", {}, template="p"), cfg)]
    p = lo.plan(cfg, devices, [], {}, {}, {}, set(), [], {})
    assert sorted(p["made_spare"]) == ["M2", "S1"]
    assert p["tag_add"]["S1"] == ["spare"] and p["tag_remove"]["S1"] == ["fleet:home"]
    assert "M1" not in p["tag_add"] and "M1" not in p["tag_remove"]


def test_rules_work_a_stationed_fleets_devices_at_home_only():
    from rsweb import ami_schedule as amis
    at_home = {"device_code": "M1", "device_type": "mining_drone", "location": "AAA-BELT-1", "status": "idle", "tags": ["fleet:home"]}
    away = {**at_home, "device_code": "M2", "location": "BBB-BELT-1"}
    loose = {**at_home, "device_code": "M3", "tags": []}
    ctrl = {"device_code": "C1", "device_type": "ami_mining_controller", "location": "AAA-BELT-1", "status": "idle", "tags": []}
    try:
        amis.set_stationed({"fleet:home": "AAA"})
        assert not amis.reserved(at_home) and amis.reserved(away) and not amis.reserved(loose)
        # a fleetless controller at home adopts the stationed fleet's drones and fleetless ones alike
        assert amis.adoptable([at_home, loose, ctrl], ctrl, {}) == ["M1", "M3"]
        amis.set_stationed({})   # on a mission (or not stationed): the rules leave its devices alone
        assert amis.reserved(at_home) and amis.adoptable([at_home, loose, ctrl], ctrl, {}) == ["M3"]
    finally:
        amis.set_stationed({})


def test_materials_go_to_the_fleet_named():
    from rsweb import loadouts as lo
    devices = [{"device_code": "TC", "device_type": "ami_transport_controller", "location": "AAA-2-L4", "status": "idle",
                "available_commands": ["adopt", "set_directive"], "tags": []},
               {"device_code": "FR", "device_type": "cargo_freighter", "location": "AAA-2-L4", "status": "idle", "tags": []},
               {"device_code": "AF", "device_type": "autofactory", "location": "FAR-3", "status": "idle",
                "available_commands": ["enqueue_print"], "tags": []}]
    stars = {"AAA": {"position": {"x": 0, "y": 0, "z": 0}}, "NEAR": {"position": {"x": 1, "y": 0, "z": 0}},
             "FAR": {"position": {"x": 50, "y": 0, "z": 0}}}
    fleets = [_st("src", "AAA", {}, materials="far"), _st("near", "NEAR", {}, materials="self"), _st("far", "FAR", {}, materials="self"),
              _st("loop", "AAA", {}, materials="src")]
    routes, unmet = lo.material_routes({"fleets": fleets}, devices, {"AAA-2-L4": {"carbon": 50}}, stars, set(), {}, {})
    assert [(r["source"], r["dest"], r["deliver"], r["from_fleet"], r["to_fleet"]) for r in routes] == \
        [("AAA", "FAR", "FAR-3", "Src", "Far")]                       # the fleet it names, not the nearest
    assert any("same system" in u["why"] and u["fleet"] == "Loop" for u in unmet)
    assert lo.spare_depot({"fleets": fleets, "settings": {}}, devices) == "FAR"   # a destination fleet's home with an autofactory


def test_back_to_station_clears_an_ended_mission(client):
    eng = client.app.state.worker.automations
    client.post("/fleets", data={"name": "Sol home", "home": "SOL", "station": "on"}, headers=HX)
    items = client.portal.call(eng.fleets)
    items[0]["mission"] = {"status": "ended", "targets": ["KEL"], "log": []}
    client.portal.call(eng.save_fleets, items)
    from rsweb import fleets as fl
    assert fl.away(client.portal.call(eng.fleets)[0])
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Back to station" in page and "mission: ended" in client.get("/fleets/overview", headers=HX).text
    client.post("/fleets/sol-home/control", data={"action": "station"}, headers=HX)
    f = client.portal.call(eng.fleets)[0]
    assert f["mission"]["status"] == "done" and fl.stationed(f)


def test_recall_waits_for_passengers_already_flying_back():
    """Seen live 2026-10-06: survey_system (recall on) had already sent the drones back to the vessel; the fleet's recall
    ordered them there again, the game said "Device is already in motion" and the mission stalled."""
    from rsweb import fleets as fl
    fleet = {"id": "surveyors", "name": "Surveyors", "role": "explore", "home": "FAL", "wants": {}}
    trip = lambda dest: {"destination": dest, "final_destination": dest, "eta_seconds": 1424,  # noqa: E731
                         "arrives_at": "2099-01-01T00:00:00+00:00"}
    devices = [
        {"device_code": "HV", "device_type": "heaven_vessel", "location": "OTH-OORT", "status": "idle", "features": ["surge"],
         "stow_capacity": 10, "tags": ["fleet:surveyors"]},
        {"device_code": "D1", "device_type": "survey_drone", "location": None, "status": "recalling", "features": ["stow"],
         "travel": trip("OTH-OORT"), "controller_device_code": "SC", "tags": ["fleet:surveyors"]},
        {"device_code": "D2", "device_type": "survey_drone", "location": None, "status": "recalling", "features": ["stow"],
         "travel": trip("OTH-3"), "tags": ["fleet:surveyors"]},
        {"device_code": "D3", "device_type": "survey_drone", "location": "OTH-2", "status": "idle", "features": ["stow"],
         "tags": ["fleet:surveyors"]},
        {"device_code": "SC", "device_type": "ami_survey_controller", "location": "OTH-1", "status": "coordinating", "tags": []},
    ]
    steps, problems = fl.assemble_steps(fleet, devices)
    plan = [(s["method"], s["desc"]) for s in steps]
    assert problems == []
    assert ("POST", "D1 → OTH-OORT (board)") not in plan                      # already on its way: no new order
    w = next(s for s in steps if s["desc"] == "wait for D1 at OTH-OORT")
    assert w["seq0_from"] == 0 and w["timeout"] > 3600                          # counts from the job start, waits for its ETA
    # D2 lands at a planet and D3 is at one: ~2000 AU of cruising from the vessel at the Oort cloud, so the vessel fetches them
    assert not any(d.endswith("(board)") for _, d in plan)
    assert plan.index(("WAIT", "wait for D2 at OTH-3")) < plan.index(("POST", "HV → OTH-3 (pick up D2)"))
    assert [d for _, d in plan[-5:]] == ["stow D1 in HV", "HV → OTH-2 (pick up D3)", "stow D3 in HV",
                                         "HV → OTH-3 (pick up D2)", "stow D2 in HV"]
    assert [c["device_code"] for c in fl.outside_controllers(fleet, devices)] == ["SC"]


def test_unstationed_fleets_stay_aboard_at_home(client):
    eng = client.app.state.worker.automations
    devices = [
        {"device_code": "CV", "device_type": "cargo_vessel", "location": "SOL-3-L4", "status": "idle", "features": ["surge"],
         "stow_capacity": 50, "attach_capacity": 3, "tags": ["fleet:miners"]},
        {"device_code": "MD", "device_type": "mining_drone", "location": None, "stowed_in_device_code": "CV", "status": "stowed",
         "tags": ["fleet:miners"]},
        {"device_code": "TH", "device_type": "transport_hauler", "location": "SOL-3-L4", "attached_to_device_code": "CV",
         "status": "attached", "cargo_used": 40, "tags": ["fleet:miners"]},
        {"device_code": "FR", "device_type": "cargo_freighter", "location": "SOL-3-L4", "status": "idle", "features": ["surge"],
         "cargo_used": 200, "tags": ["fleet:miners"]},
    ]
    fleet = {"id": "miners", "name": "Miners", "role": "mining", "home": "SOL", "wants": {}, "station": False}
    m = {"status": "running", "phase": "return", "log": []}
    steps, _ = client.portal.call(eng.fleet_phase_steps, fleet, m, "unload", devices)
    descs = [s["desc"] for s in steps]
    assert not any(d.startswith("deploy MD") for d in descs)                    # passengers stay aboard
    th = [d for d in descs if "TH" in d]
    assert th[0] == "CV: detach TH (to deposit)" and "TH: unload" in th and th[-1] == "CV: attach TH"   # back on board
    assert "FR: unload" in descs and "everyone stays aboard" in m["log"][-1]["text"]
    fleet["station"] = True                                                       # a stationed fleet works its home
    steps, _ = client.portal.call(eng.fleet_phase_steps, fleet, {"status": "running", "log": []}, "unload", devices)
    descs = [s["desc"] for s in steps]
    assert "deploy MD from CV" in descs and "CV: detach TH" in descs and "CV: attach TH" not in descs


def test_fleet_owner_hands_members_to_one_replicant(client):
    from rsweb import fleets as fl
    eng = client.app.state.worker.automations
    fleet = {"id": "miners", "name": "Miners", "home": "SOL", "wants": {}, "owner": "AAAA0001"}
    D = lambda code, owner, **kw: {"device_code": code, "device_type": "mining_drone", "location": "SOL-BELT-1",  # noqa: E731
                                   "status": "idle", "replicant_code": owner, "tags": ["fleet:miners"], **kw}
    devices = [D("M1", "AAAA0001"), D("M2", "BBBB0002"), D("M3", "BBBB0002"),
               D("HV", "BBBB0002", device_type="cargo_vessel", hosting_replicant={"name": "Sk3y-6", "replicant_code": "BBBB0002"}),
               {**D("X1", "BBBB0002"), "tags": []}]
    own = fl.ownership(fleet, devices)
    assert [d["device_code"] for d in own["move"]] == ["M2", "M3"] and [d["device_code"] for d in own["hosts"]] == ["HV"]
    st = fl.owner_steps(fleet, devices)
    assert [(s["path"], s["body"]) for s in st] == [("/devices/M2", {"command": "change_owner", "target": "AAAA0001"}),
                                                    ("/devices/M3", {"command": "change_owner", "target": "AAAA0001"})]
    assert fl.owner_steps({**fleet, "owner": ""}, devices) == []
    # through the page: pick the owner, keep it, and the engine hands members over once (not again within 15 min)
    client.post("/fleets", data={"name": "Miners", "home": "SOL"}, headers=HX)
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(client.app.state.db.kv_set, "replicants", {"AAAA0001": {"name": "Sk3y-1"}, "BBBB0002": {"name": "Sk3y-6"}})
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Owner" in page and "owned by" in page
    client.post("/fleets/miners/owner", data={"owner": "AAAA0001", "keep_owner": "on"}, headers=HX)
    f = client.portal.call(eng.fleets)[0]
    assert f["owner"] == "AAAA0001" and f["keep_owner"] and "owner_move" not in client.portal.call(client.app.state.db.kv_get, "fleets")[0]
    assert "Left alone (they host a replicant): HV (Sk3y-6)" in client.get("/fleets/details?all=1", headers=H).text
    lines = client.portal.call(eng.fleet_owners)
    assert lines and "M2, M3 → owner AAAA0001" in lines[0]
    client.portal.call(client.app.state.db.kv_set, "fleet_owners_at", None)
    assert client.portal.call(eng.fleet_owners) == []          # just sent: not repeated
    r = client.post("/fleets/miners/owner", data={"owner": "NOPE"}, headers=HX)   # unknown replicant: cleared
    assert not client.portal.call(eng.fleets)[0]["owner"]


def test_mining_mission_salvages_where_there_is_no_belt(client):
    """Live 2026-10-06: KELMONENT has no belt; the made-up KELMONENT-1-BELT-1 was refused ('Invalid destination format')."""
    import json as _json
    from rsweb import fleets as fl
    assert fl.richest_belt("KELMONENT", {"planets": [], "asteroid_belt": None}) is None
    assert fl.richest_belt("X", {"asteroid_belt": {"belts": [{"designation": "X-BELT-1", "resources": {"iron": "low"}},
                                                             {"designation": "X-BELT-2", "resources": {"iron": "rich"}}]}}) == "X-BELT-2"
    eng = client.app.state.worker.automations
    db = client.app.state.db
    world = client.app.state.api.http._transport.app.state.world
    for ev in [e for e in world.events if e["event"] == "salvage.discovered"]:      # SOL-3-1-SAL-1, a derelict hauler
        client.portal.call(client.app.state.worker.handle_event, dict(ev))
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("SOL", _json.dumps({"planets": [{"designation": "SOL-3"}], "asteroid_belt": None}), "2026-10-06T00:00:00"))
    devices = [
        {"device_code": "MC", "device_type": "ami_mining_controller", "location": "SOL-3-L4", "status": "idle", "tags": ["fleet:p"]},
        {"device_code": "SC", "device_type": "ami_survey_controller", "location": "SOL-3-L4", "status": "idle", "tags": ["fleet:p"]},
        {"device_code": "MD", "device_type": "mining_drone", "location": "SOL-3-L4", "status": "idle", "tags": ["fleet:p"]},
        {"device_code": "SD", "device_type": "survey_drone", "location": "SOL-3-L4", "status": "idle", "tags": ["fleet:p"]},
    ]
    fleet = {"id": "p", "name": "Prospectors", "role": "mining", "home": "AEM", "wants": {}, "station": False}
    m = {"status": "running", "phase": "deploy", "target": "SOL", "targets": ["SOL"], "idx": 0, "opts": {}, "log": []}
    steps, problems = client.portal.call(eng.fleet_phase_steps, fleet, m, "work", devices)
    bodies = [s["body"] for s in steps if s["body"]]
    assert {"command": "travel", "destination": "SOL-3-1"} in bodies and not any("BELT" in str(b) for b in bodies)
    assert {"command": "set_directive", "directive": "gather_salvage", "configuration": {"location": "SOL-3-1", "recall": False}} in bodies
    assert not any(b.get("directive") in ("belt_search", "gather_evenly") for b in bodies)
    assert {"command": "adopt", "devices": ["MD"]} in bodies and not problems
    assert m["belt"] == "SOL-3-1" and m["salvage"] == "SOL-3-1-SAL-1" and "salvaging SOL-3-1-SAL-1" in m["log"][-1]["text"]
    # no belt and no salvage known: the mission stalls with a reason instead of watching forever
    client.portal.call(db.execute, "DELETE FROM events WHERE event='salvage.discovered'")
    m = {"status": "running", "phase": "deploy", "target": "SOL", "targets": ["SOL"], "idx": 0, "opts": {}, "log": []}
    steps, problems = client.portal.call(eng.fleet_phase_steps, fleet, m, "work", devices)
    assert not steps and "no asteroid belt and no salvage" in problems[0] and m["stall"]


def test_each_fleet_prints_on_its_own_autofactory():
    """Live 2026-10-06: three fleets with an autofactory each (all at FALQUORYX-BELT-1); an item added to all three
    loadouts was printed three times on one factory."""
    from rsweb import loadouts as lo
    devices = [{"device_code": c, "device_type": "autofactory", "location": "FAL-BELT-1", "status": "idle", "print_queue": [],
                "available_commands": ["enqueue_print"], "tags": [f"fleet:{fid}"]}
               for c, fid in (("AF1", "hub"), ("AF2", "m1"), ("AF3", "m2"))]
    devices.append({"device_code": "AF0", "device_type": "autofactory", "location": "FAL-BELT-1", "status": "idle",
                    "print_queue": [], "available_commands": ["enqueue_print"], "tags": []})
    fleets = [{"id": fid, "name": fid, "home": home, "station": True, "wants": {"maintenance_drone": 1}}
              for fid, home in (("hub", "FAL"), ("m1", "KEL"), ("m2", "LOR"))]
    fleets.append({"id": "m3", "name": "m3", "home": "LOR", "station": True, "wants": {"maintenance_drone": 1}})
    bps = [{"device_type": "maintenance_drone", "resources": {"structural": 50}, "print_time": 600}]
    stars = {s: {"position": {"x": i, "y": 0, "z": 0}} for i, s in enumerate(("FAL", "KEL", "LOR"))}
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, bps, {"FAL-BELT-1": {"structural": 1000}},
                stars, {}, set(), [], {})
    got = {pr["fleet"]: pr["factory"] for pr in p["prints"]}
    assert got == {"hub": "AF1", "m1": "AF2", "m2": "AF3", "m3": "AF0"}     # m3 has none: the fleetless one, not another's
    # no stock: a fleet's own factory still gets it, and it waits for materials there
    p = lo.plan({"phases": [], "fleets": fleets[:1], "fleets_migrated": True}, devices, bps, {}, stars, {}, set(), [], {})
    assert [(pr["factory"], pr["note"]) for pr in p["prints"]] == [("AF1", "the fleet's own autofactory; waits for materials")]
    # a fleet's factory pinned at the stockpile (at: tag) stays there instead of being sent to the fleet's home
    away = {"device_code": "AF2", "device_type": "autofactory", "location": "FAL-BELT-1", "status": "idle",
            "available_commands": ["enqueue_print"], "tags": ["fleet:m1"]}
    p = lo.plan({"phases": [], "fleets": fleets[1:2], "fleets_migrated": True}, [away], [], {}, stars, {}, set(), [], {})
    assert p["returning"] == ["AF2"]
    away["tags"].append("at:FAL-BELT-1")
    p = lo.plan({"phases": [], "fleets": fleets[1:2], "fleets_migrated": True}, [away], [], {}, stars, {}, set(), [], {})
    assert p["returning"] == []


def test_fleets_without_a_factory_spread_over_the_hubs_factories():
    """Live 2026-10-07: all three autofactories belong to Printing Hub 1; Miner 1's and Miner 2's prints both went on
    3E95BD59 while 9E13498F sat idle."""
    from rsweb import loadouts as lo
    devices = [{"device_code": c, "device_type": "autofactory", "location": "FAL-BELT-1", "print_queue": [],
                "status": st, "available_commands": ["enqueue_print"], "tags": ["fleet:hub"],
                **({"printing": {"device_type": "galactic_observatory"}} if st != "idle" else {})}
               for c, st in (("AF1", "idle"), ("AF2", "idle"), ("AF3", "printing (galactic_observatory)"))]
    fleets = [{"id": "hub", "name": "Hub", "home": "FAL", "station": True, "wants": {"autofactory": 3, "galactic_observatory": 1}},
              {"id": "m1", "name": "Miner 1", "home": "KEL", "station": True, "wants": {"galactic_observatory": 1}},
              {"id": "m2", "name": "Miner 2", "home": "LOR", "station": True, "wants": {"galactic_observatory": 1}}]
    bps = [{"device_type": "galactic_observatory", "resources": {"structural": 50}, "print_time": 28800}]
    stars = {s: {"position": {"x": i, "y": 0, "z": 0}} for i, s in enumerate(("FAL", "KEL", "LOR"))}
    orders = [{"star": "FAL", "fleet": "hub", "device_type": "galactic_observatory", "factory": "AF3"}]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, bps, {"FAL-BELT-1": {"structural": 1000}},
                stars, {}, set(), orders, {})
    assert sorted((pr["fleet"], pr["factory"]) for pr in p["prints"]) == [("m1", "AF1"), ("m2", "AF2")]


def test_explore_mission_leaves_no_relays_or_beacons(client):
    """Survey crews no longer drop relays or beacons (2026-10-11): the Systems page asks for one instead."""
    eng = client.app.state.worker.automations
    fleet, devices = _survey_world()
    m = {"status": "running", "phase": "travel", "idx": 0, "targets": ["LOR"], "opts": {}, "log": []}
    steps, _ = client.portal.call(eng.fleet_phase_steps, fleet, m, "deploy", devices)
    descs = [s["desc"] for s in steps]
    assert "deploy SC from HV" in descs and not any("relay" in d or "beacon" in d for d in descs)


def test_mission_to_a_system_without_a_relay_warns_without_a_replicant_aboard(client):
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(eng.save_fleets, [{"id": "p", "name": "Prospectors", "role": "mining", "home": "SOL", "wants": {},
                                          "station": False, "materials": "", "mission": None}])
    client.post("/fleets/p/mission", data={"targets": "ABOTEIN"}, headers=HX)
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "p")
    texts = [x["text"] for x in (f.get("mission") or {}).get("log") or []]
    assert any("no relay of yours in ABOTEIN and no replicant rides with the fleet" in t for t in texts), texts


def test_a_fleets_ward_goes_with_it_and_systems_list_shows_wards(client):
    """Wards travel with their fleet: deployed and activated where it works, deactivated and boarded when it leaves."""
    from rsweb import fleets as fl, outposts as op
    from rsweb.modular import prepare_moves
    fleet, devices = _survey_world()
    devices.append({"device_code": "W1", "device_type": "system_ward", "location": None, "stowed_in_device_code": "HV",
                    "status": "stowed", "features": ["cruise", "ward", "stow"], "tags": ["fleet:s"]})
    descs = [s["desc"] for s in fl.unload_steps(fleet, devices)]
    assert descs.index("deploy W1 from HV") + 1 == descs.index("W1: activate ward")
    # leaving: the active ward is switched off right before it flies to the carrier, and on again where it's unloaded
    devices[-1].update({"location": "LOR-2", "stowed_in_device_code": None, "status": "warding"})
    devices[1].update({"location": "LOR-1-L4", "stowed_in_device_code": None, "status": "idle"})
    board, _ = fl.assemble_steps(fleet, devices)
    out = [s["desc"] for s in prepare_moves(board, devices, {})]
    assert out.index("W1: stop warding (deactivate) to move") + 1 == out.index("W1 → LOR-1-L4 (board)")
    pres = op.presence(devices)
    assert [d["device_code"] for d in pres["FAL"]["relay"]] == ["R0"] and pres["FAL"]["ward"] == []
    # the Systems list: the outpost columns always show
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get("/systems", headers=H).text
    assert ">Relay</th>" in page and ">Beacon</th>" in page and ">Ward</th>" in page and "✓ BCN00001" in page
    assert "— none" in page and "only systems missing one" not in page and 'name="outposts"' not in page


def test_vessels_hosting_a_replicant_can_join_a_fleet(client):
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.worker.sync_devices)
    reps = client.portal.call(client.app.state.db.kv_get, "replicants") or {}
    code, rep = next((c, r) for c, r in reps.items() if r.get("hosted_device_code"))
    client.portal.call(eng.save_fleets, [{"id": "s", "name": "Surveyors", "role": "explore", "home": "SOL", "wants": {},
                                          "station": False, "materials": "", "mission": None}])
    assert 'hx-get="/fleets/s/picker"' in client.get("/fleets/details?all=1", headers=H).text   # loaded when the box opens
    page = client.get("/fleets/s/picker", headers=H).text
    assert f'name="add" value="{rep["hosted_device_code"]}"' in page and f"hosts {rep.get('name') or code}" in page


def test_wallpaper_dashboard_panel_and_fleet_markers(client):
    from rsweb import wallpaper as wp
    from rsweb import fleets as fl
    from rsweb.web import status_class
    eng = client.app.state.worker.automations
    world = client.app.state.api.http._transport.app.state.world
    world.devices[1]["tags"] = ["fleet:p1"]
    client.portal.call(eng.save_fleets, [{"id": "p1", "name": "Prospectors", "role": "mining", "home": "SOL", "wants": {},
                                          "mission": {"status": "running", "phase": "travel", "targets": ["KELMONENT"], "idx": 0}},
                                         {"id": "e1", "name": "Empty", "role": "explore", "home": "SOL", "wants": {}}])
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(client.app.state.worker.sync_inventory)
    r = client.post("/wallpaper-settings", data={"action": "create"}, headers=HX)
    import re as _re
    good = {wp.HEADER: _re.search(r"#key=(rsw_[^\"]+)", r.text).group(1)}
    # the panel's data needs the key like the rest
    assert client.get("/wallpaper/me/api/hud.json").status_code == 401
    hud = client.get("/wallpaper/me/api/hud.json", headers=good).json()
    assert set(hud["devices"]) == {"total", "working", "moving", "idle"} and hud["devices"]["total"] == len(world.devices)
    assert all(x["name"] in ("structural", "conductive", "silicates", "carbon", "volatiles", "rares") for x in hud["resources"])
    # fleets with members only, with their mission: phase, where they're headed, where they are
    f = next(x for x in hud["fleets"] if x["id"] == "p1")
    assert [x["id"] for x in hud["fleets"]] == ["p1"]
    assert f["state"] == "running" and f["phase"] == "travel" and f["target"] == "KELMONENT" and f["members"] == 1
    assert f["working"] + f["moving"] + f["idle"] <= 1
    # the galaxy data carries the same fleets (the Galaxy page and the wallpaper draw them)
    galaxy = client.get("/wallpaper/me/api/map.json", headers=good).json()
    assert [x["id"] for x in galaxy["fleets"]] == ["p1"] and galaxy["supply"] == [] and hud["supply"] == []
    assert "fleets" in client.get("/api/map.json", headers=H).json()
    # no mission: stationed or idle, and no target
    a = fl.activity({"id": "s", "home": "SOL", "station": True}, [], status_class)
    assert a["state"] == "stationed" and a["target"] is None and a["stars"] == []
    # members riding in a carrier count where the carrier is
    devs = [{"device_code": "C", "location": "ZED-1", "status": "travelling"},
            {"device_code": "D", "tags": ["fleet:x"], "stowed_in_device_code": "C", "status": "stowed"},
            {"device_code": "E", "tags": ["fleet:x"], "location": "ABC-2", "status": "mining"}]
    a = fl.activity({"id": "x", "mission": {"status": "stalled", "phase": "work", "targets": ["ABC", "ZED"], "idx": 5}}, devs, status_class)
    assert sorted(a["stars"]) == ["ABC", "ZED"] and a["target"] == "ZED" and a["working"] == 1 and a["state"] == "stalled"
    # the page's own files are what the panel needs
    assert client.get("/wallpaper/me/static/wallpaper.js").status_code == 200


def test_adding_a_device_already_in_the_fleet_sends_nothing(client):
    world = client.app.state.api.http._transport.app.state.world
    world.devices[1]["tags"] = ["fleet:t1"]
    client.portal.call(client.app.state.worker.sync_devices)
    eng = client.app.state.worker.automations
    client.portal.call(eng.save_fleets, [{"id": "t1", "name": "T1", "role": "trade", "home": "SOL", "wants": {}}])
    before = len(client.portal.call(eng.jobs))
    r = client.post("/fleets/t1/members", data={"add": world.devices[1]["device_code"]}, headers=HX)
    assert "already in this fleet" in r.text
    for j in client.portal.call(eng.jobs)[: max(0, len(client.portal.call(eng.jobs)) - before)]:
        for s in j["steps"]:
            c = (s.get("body") or {}).get("configuration") or {}
            assert not set(c.get("add_tags") or []) & set(c.get("remove_tags") or [])


def test_mining_mission_moves_on_to_the_next_salvage_before_ending(client, monkeypatch):
    from rsweb import targets
    eng = client.app.state.worker.automations
    db = client.app.state.db
    salvage = {"KEL": [{"code": "KEL-1-SAL-1", "total": 0, "depleted": True}, {"code": "KEL-2-SAL-1", "total": 500},
                       {"code": "KEL-3-SAL-1", "total": 200}]}

    async def fake_resources(_db, star):
        return {"salvage": [dict(x) for x in salvage.get(star.upper(), [])], "sites": []}
    monkeypatch.setattr(targets, "system_resources", fake_resources)
    devices = [{"device_code": "C", "device_type": "ami_mining_controller", "tags": ["fleet:p"], "location": "KEL-1",
                "status": "coordinating", "ami_directive": {"name": "gather_salvage", "_eval_state": "depleted:complete"},
                "available_commands": ["set_directive", "launch", "adopt"]},
               {"device_code": "D", "device_type": "mining_drone", "tags": ["fleet:p"], "location": "KEL-1", "status": "idle",
                "controller_device_code": "C"}]
    client.portal.call(db.kv_set, "devices", devices)
    mission = {"status": "running", "phase": "watch", "idx": 0, "targets": ["KEL"], "opts": {}, "log": [],
               "salvage": "KEL-1-SAL-1", "exhausted_since": "2026-01-01T00:00:00+00:00"}
    client.portal.call(eng.save_fleets, [{"id": "p", "name": "Prospectors", "role": "mining", "home": "FAL", "wants": {},
                                          "mission": mission}])
    client.portal.call(eng.run_fleets)
    m = next(f for f in client.portal.call(eng.fleets) if f["id"] == "p")["mission"]
    texts = [x["text"] for x in m["log"]]
    assert any("KEL-1-SAL-1 used up — moving on to KEL-2-SAL-1" in t for t in texts), texts
    assert m["salvage"] == "KEL-2-SAL-1" and m["salvaged"] == ["KEL-1-SAL-1"] and m["phase"] == "work" and m["status"] == "running"
    # the last one used up: the mission ends its work (recall …) instead of looping
    m.update({"phase": "watch", "job": None, "salvage": "KEL-3-SAL-1", "salvaged": ["KEL-1-SAL-1", "KEL-2-SAL-1"],
              "exhausted_since": "2026-01-01T00:00:00+00:00"})
    client.portal.call(eng.save_fleets, [{"id": "p", "name": "Prospectors", "role": "mining", "home": "FAL", "wants": {},
                                          "mission": m}])
    client.portal.call(eng.run_fleets)
    m = next(f for f in client.portal.call(eng.fleets) if f["id"] == "p")["mission"]
    assert any(x["text"].startswith("work done") for x in m["log"]) and m["phase"] != "work"


def test_decommission_at_an_autofactory(client):
    from rsweb import decommission as dc
    from rsweb import loadouts as lo
    devs = [{"device_code": "AF1", "device_type": "autofactory", "location": "FAL-1-L4", "status": "idle"},
            {"device_code": "AF2", "device_type": "autofactory", "location": "KEL-2-L4", "status": "idle"},
            {"device_code": "W", "device_type": "system_ward", "location": "KEL-OORT", "status": "idle",
             "tags": ["fleet:k", "home:kel"]}]
    stars = {"FAL": {"position": {"x": 0, "y": 0, "z": 0}}, "KEL": {"position": {"x": 5, "y": 0, "z": 0}}}
    f = dc.factories(devs, stars, "KEL-OORT")
    assert [x["code"] for x in f] == ["AF2", "AF1"]                                 # same system first
    t = dc.retag(devs[2], f[1], lo.to_tag, lo.at_tag)
    assert t == {"add_tags": ["at:fal-1-l4", "to:fal"], "remove_tags": ["fleet:k", "home:kel"]}
    assert dc.retag(devs[2], f[0], lo.to_tag, lo.at_tag)["add_tags"] == ["at:kel-2-l4"]   # already in the system
    e = {"factory": "AF1", "at": "FAL-1-L4"}
    assert dc.ready({"location": "FAL-1-L4", "status": "idle"}, e)
    assert not dc.ready({"location": "FAL-1-L4", "status": "unfurling"}, e) and not dc.ready({"location": "KEL-OORT"}, e)
    # end to end: queue it on the device page, then the pass decommissions it once it's there
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    victim = next(d for d in world.devices if "decommission" in d["available_commands"] and "autofactory" not in d["device_type"])
    factory = next(d for d in world.devices if d["device_type"] == "autofactory")
    page = client.get(f"/devices/{victim['device_code']}", headers=H).text
    assert "Decommission at an autofactory" in page
    r = client.post(f"/devices/{victim['device_code']}/decommission-at", data={"factory": factory["device_code"]}, headers=HX)
    assert "Queued" in r.text
    q = client.portal.call(client.app.state.db.kv_get, dc.KV)
    assert q[victim["device_code"]]["at"] == factory["location"]
    devices = client.portal.call(client.app.state.db.kv_get, "devices")
    for d in devices:
        if d["device_code"] == victim["device_code"]:
            d.update({"location": factory["location"], "status": "idle", "stowed_in_device_code": None})
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    eng = client.app.state.worker.automations
    assert client.portal.call(eng.decommission_queue_pass) == [f"{victim['device_code']} → decommission at {factory['location']}"]
    job = next(j for j in client.portal.call(eng.jobs) if j["rule"] == "decommission")
    assert job["steps"][0]["body"] == {"command": "decommission"}
    assert client.portal.call(eng.decommission_queue_pass) == []                    # not twice


def test_auto_scout_surveys_nearest_first_until_worn(client, monkeypatch):
    eng, db = client.app.state.worker.automations, client.app.state.db
    w = client.app.state.worker
    # only SOL in the catalogue: the mock's random stars (re-synced in the background) mustn't compete with NEARA / NEARB
    real_get = db.kv_get

    async def kv_get(key, default=None):
        if key == "stars":
            return {"stars": [{"designation": "SOL", "position": {"x": 0, "y": 0, "z": 0}}]}
        return await real_get(key, default)
    monkeypatch.setattr(db, "kv_get", kv_get)
    stars = [{"designation": d, "position": {"x": x, "y": 0, "z": 0}} for d, x in
             (("NEARA", 2), ("NEARB", 3), ("FARC", 9), ("FARD", 12))]
    client.portal.call(w.handle_event, _ev(950, "prospect.completed", device="OBS00001", origin="SOL", stars_generated=4, stars=stars))
    client.portal.call(eng.save_fleets, [{"id": "x1", "name": "Scouts", "role": "explore", "home": "SOL", "wants": {}},
                                         {"id": "x2", "name": "Lazy", "role": "explore", "home": "SOL", "wants": {}}])
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES('SOL', '{}', '2026-10-08')")
    r = client.post("/fleets/x1/auto-scout", data={"auto_scout": "on", "scout_radius": "250"}, headers=HX)
    assert "nearest unsurveyed" in r.text
    assert client.portal.call(db.kv_get, "scout_radii") == {"SOL": 250}   # per home hub, shared by its survey fleets
    devices = client.portal.call(db.kv_get, "devices")
    devices.append({"device_code": "TIRED", "device_type": "survey_drone", "location": "SOL-3", "status": "idle",
                    "operational_capacity": 0.8, "tags": ["fleet:x1"]})
    devices[-1:-1] = [{"device_code": f"RL{s}", "device_type": "ftl_relay", "location": f"{s}-1-L4", "status": "relaying"}
                      for s in ("SOL", "NEARA", "NEARB")]   # a relay commands its own system only: scouts go where one is
    # (SOL's joins them to the replicant there: a network commands only with a replicant in it)
    client.portal.call(db.kv_set, "devices", devices)
    assert client.portal.call(eng.auto_scout_pass) == []                      # 80 %: not until everything is at 85 %
    assert "below 85 %" in next(x for x in client.portal.call(eng.fleets) if x["id"] == "x1")["scout_note"]
    devices[-1]["operational_capacity"] = 0.9
    client.portal.call(db.kv_set, "devices", devices)
    out = client.portal.call(eng.auto_scout_pass)
    assert out == ["Scouts: scouting NEARA"]                                 # the nearest one first, one at a time
    assert client.portal.call(eng.auto_scout_pass) == []                      # busy; Lazy isn't auto-scouting
    nxt, _ = client.portal.call(eng.scout_next, {"id": "x9", "home": "SOL"}, client.portal.call(eng.fleets),
                                client.portal.call(eng.devices))
    assert nxt == "NEARB"                                                     # NEARA is taken by Scouts
    # no replicant aboard: FARC / FARD have no relay of ours — never chosen
    seen, ex = [], {"NEARB"}
    while True:
        nxt, why = client.portal.call(eng.scout_next, {"id": "x9", "home": "SOL"}, client.portal.call(eng.fleets),
                                      client.portal.call(eng.devices), None, set(ex))
        if not nxt:
            break
        seen.append(nxt)
        ex.add(nxt)
    assert "FARC" not in seen and "FARD" not in seen and "relay coverage" in why
    # NEARA done: it takes the next one; a member worn to 50 % ends the run (it finishes that system and goes home)
    items = client.portal.call(eng.fleets)
    m = items[0]["mission"]
    m.update({"phase": "recall", "job": None})
    client.portal.call(eng.save_fleets, items)
    client.portal.call(eng.run_fleets)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["targets"] == ["NEARA", "NEARB"] and any("on to NEARB" in str(x) for x in m["log"])
    devices = client.portal.call(db.kv_get, "devices")
    devices.append({"device_code": "WORN", "device_type": "survey_drone", "location": "NEARB-1", "status": "idle",
                    "operational_capacity": 0.5, "tags": ["fleet:x1"]})
    client.portal.call(db.kv_set, "devices", devices)
    items = client.portal.call(eng.fleets)
    items[0]["mission"].update({"phase": "recall", "job": None, "idx": 1})
    client.portal.call(eng.save_fleets, items)
    client.portal.call(eng.run_fleets)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["targets"] == ["NEARA", "NEARB"] and m["scout_ended"] and any("for repairs" in str(x) for x in m["log"])
    # a star the fleet already left behind isn't taken any more (Surveyor 10's 124 targets blocked every other fleet)
    nxt, _ = client.portal.call(eng.scout_next, {"id": "x9", "home": "SOL"}, client.portal.call(eng.fleets),
                                client.portal.call(eng.devices))
    assert nxt == "NEARA"


def test_scout_crew_left_behind_is_fetched_not_skipped(client, monkeypatch):
    """Live 2026-11-10: Surveyor 10's crew sat at SYRAXOS with its vessel back home; every pass in recall moved the
    mission on to one more target without leaving. Now the target stays and a carrier fetches the crew (or it stalls)."""
    eng, db = client.app.state.worker.automations, client.app.state.db
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": "VES00001", "device_type": "heaven_vessel", "location": "SOL-3", "status": "idle",
                 "tags": ["fleet:x1"], "features": ["surge", "cruise", "stow"], "available_commands": ["travel", "stow"]},
                {"device_code": "DRN00001", "device_type": "survey_drone", "location": "NEARA-1", "status": "idle",
                 "tags": ["fleet:x1"], "features": ["cruise", "survey", "stow"], "available_commands": ["travel", "stow"]}]
    client.portal.call(db.kv_set, "devices", devices)
    m = {"status": "running", "phase": "recall", "idx": 0, "targets": ["NEARA", "NEARB"], "departed": True,
         "log": [], "opts": {}, "scouting": True, "job": None}
    client.portal.call(eng.save_fleets, [{"id": "x1", "name": "Scouts", "role": "explore", "home": "SOL", "wants": {},
                                          "auto_scout": True, "mission": m}])
    for _ in range(3):
        client.portal.call(eng.run_fleets)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["targets"] == ["NEARA", "NEARB"] and m["idx"] <= 1
    assert m["phase"] == "board" or m["status"] == "stalled", m
    # the card says why a free auto-scouting fleet hasn't launched
    client.portal.call(eng.save_fleets, [{"id": "x1", "name": "Scouts", "role": "explore", "home": "SOL", "wants": {},
                                          "auto_scout": True, "scout_note": "every known system is surveyed or taken"}])
    page = client.get("/fleets/details?fleet=x1", headers=H).text
    assert "Not launched: every known system is surveyed or taken" in page


def test_review_reform_leaves_fleets_on_a_mission_alone():
    from rsweb import reform
    devices = [{"device_code": "M1", "device_type": "mining_drone", "location": "AAA-5-L4", "status": "idle",
                "tags": ["fleet:prospectors"]}]
    fleets = [{"id": "prospectors", "name": "P", "home": "BBB", "wants": {}, "mission": {"status": "running"}}]
    p = reform.plan({"phases": [], "ignore_tags": []}, devices, fleets, busy=set(), hosts=set())
    assert not p["retag"] and p["skipped"][0]["code"] == "M1"


def test_a_fleet_whose_miners_sit_idle_at_home_for_6_hours_moves_on(client):
    """Live 2026-11-12: Miner_7 (DOMBTAIRE) and Delta Miner 2 (XANSELEN) sat 28 and 37 h on a finished salvage, rigs idle
    at L4 points, and nothing marked the home dry."""
    from rsweb import pathing as pa
    ctrl = {"device_code": "C1", "device_type": "ami_mining_controller", "location": "HOMEA-6-L4", "status": "coordinating",
            "tags": ["fleet:m1"], "ami_directive": {"name": "gather_salvage", "_eval_state": "depleted:recalling"}}
    rigs = [{"device_code": f"R{i}", "device_type": "mining_rig", "location": "HOMEA-6-L4", "status": "idle", "tags": ["fleet:m1"]}
            for i in range(2)]
    assert "C1 at HOMEA-6-L4: gather_salvage depleted:recalling" in pa.idle_miners("fleet:m1", "HOMEA", [ctrl, *rigs])
    assert pa.idle_miners("fleet:m1", "HOMEA", [ctrl, {**rigs[0], "status": "mining (carbon)"}, rigs[1]]) is None
    assert pa.idle_miners("fleet:m1", "HOMEA", [ctrl, {**rigs[0], "location": "OTHER-BELT-1"}, rigs[1]]) is None  # still moving in
    assert pa.idle_miners("fleet:m1", "HOMEA", [ctrl]) is None                                                 # no miners
    eng, db = client.app.state.worker.automations, client.app.state.db

    async def commandable(devices, stars=None, without=None):
        return {"HOMEA", "AHEADB"}
    eng.commandable = commandable
    stars = {"HOMEA": (0, 0, 0), "AHEADB": (5, 0, 0)}
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": k, "position": dict(zip("xyz", v))} for k, v in stars.items()]})
    belt = {"asteroid_belt": {"belts": [{"designation": "X", "density": "dense", "resources": {"structural": "rich"}}]}}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("AHEADB", json.dumps(belt), "2026-10-08T00:00:00+00:00"))
    relays = [{"device_code": f"REL{s}", "device_type": "ftl_relay", "location": f"{s}-5-L4", "status": "relaying", "tags": []}
              for s in ("HOMEA", "AHEADB")]
    client.portal.call(db.kv_set, "devices", [ctrl, *rigs, *relays])
    client.portal.call(eng.save_fleets, [{"id": "m1", "name": "Miner 1", "role": "mining", "home": "HOMEA", "station": True,
                                          "wants": {}, "auto_relocate": True}])
    client.portal.call(eng.pathing_pass, True)
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "m1")
    assert f["idle_since"] and not f.get("depleted_since")                 # the clock starts
    items = client.portal.call(eng.fleets)
    items[0]["idle_since"] = "2026-10-01T00:00:00+00:00"
    client.portal.call(eng.save_fleets, items)
    out = client.portal.call(eng.pathing_pass, True)
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "m1")
    assert out == ["Miner 1: next home AHEADB"] and "none of its miners has mined for 6 h" in f["path_note"], f


def test_renaming_a_fleet_moves_its_tag(client):
    world = client.app.state.api.http._transport.app.state.world
    eng, db, w = client.app.state.worker.automations, client.app.state.db, client.app.state.worker
    for d in world.devices[:2]:
        d["tags"] = ["fleet:ithvalai-home", "keepme"]
    client.portal.call(w.sync_devices)
    codes = [d["device_code"] for d in world.devices[:2]]
    client.portal.call(eng.save_fleets, [{"id": "ithvalai-home", "name": "Miner 1", "role": "mining", "home": "SOL", "station": True, "wants": {}},
                                         {"id": "hub", "name": "Hub", "role": "mining", "home": "AAA", "wants": {}, "materials": "ithvalai-home"}])
    client.portal.call(db.kv_set, "loadout_orders", [{"fleet": "ithvalai-home", "star": "SOL", "device_type": "mining_drone"}])
    r = client.post("/fleets/ithvalai-home/edit", data={"name": "Larselan Miners", "home": "SOL"}, headers=HX)
    items = client.portal.call(eng.fleets)
    assert {f["id"] for f in items} == {"larselan-miners", "hub"}
    assert next(f for f in items if f["id"] == "hub")["materials"] == "larselan-miners"
    assert client.portal.call(db.kv_get, "loadout_orders")[0]["fleet"] == "larselan-miners"
    devs = {d["device_code"]: d for d in client.portal.call(db.kv_get, "devices")}
    assert all("fleet:larselan-miners" in devs[c]["tags"] and "keepme" in devs[c]["tags"] for c in codes)   # at once
    client.portal.call(w.sync_devices)                     # the game still has the old tag: still shown as the new one
    devs = {d["device_code"]: d for d in client.portal.call(db.kv_get, "devices")}
    assert all(devs[c]["_retag"] == "fleet:ithvalai-home" and "fleet:ithvalai-home" not in devs[c]["tags"] for c in codes)
    assert client.portal.call(eng.fleet_rename_pass) == ["retag 2"]
    import time
    for _ in range(50):
        if all("fleet:larselan-miners" in d["tags"] for d in world.devices[:2]):
            break
        time.sleep(0.1)
    assert all("fleet:larselan-miners" in d["tags"] and "fleet:ithvalai-home" not in d["tags"] for d in world.devices[:2])
    client.portal.call(w.sync_devices)
    assert client.portal.call(db.kv_get, "fleet_renames") == {}               # done: nothing carries the old tag
    assert not any(d.get("_retag") for d in client.portal.call(db.kv_get, "devices"))


def test_surveyed_checkboxes_then_system_indicator(client):
    from rsweb.automations import survey_targets
    db = client.app.state.db
    page = client.get("/systems/SOL", headers=H).text
    row = client.portal.call(db.fetchone, "SELECT data FROM systems WHERE star='SOL'")
    bodies = [t["target"] for t in survey_targets(json.loads(row["data"]), {}, False, True, 99)]
    assert bodies and "/systems/SOL/surveyed" in page and "system surveyed" not in page
    for b in bodies[:-1]:
        r = client.post("/systems/SOL/surveyed", data={"body": b, "on": "1"}, headers=HX)
        assert "✓" in r.text and "HX-Refresh" not in r.headers
    r = client.post("/systems/SOL/surveyed", data={"body": bodies[0]}, headers=HX)   # untick one
    assert bodies[0] not in client.portal.call(db.kv_get, "surveyed")
    client.post("/systems/SOL/surveyed", data={"body": bodies[0], "on": "1"}, headers=HX)
    r = client.post("/systems/SOL/surveyed", data={"body": bodies[-1], "on": "1"}, headers=HX)
    assert r.headers.get("HX-Refresh") == "true"                                    # the last one: whole system
    page = client.get("/systems/SOL", headers=H).text
    assert "✓ system surveyed" in page and "/systems/SOL/surveyed" not in page
    assert client.post("/systems/SOL/surveyed", data={"body": "LERNA-3", "on": "1"}, headers=HX).status_code == 400


def test_replicant_vessel_follows_its_fleet_to_a_new_home(client):
    eng, db = client.app.state.worker.automations, client.app.state.db
    reps = client.portal.call(db.kv_get, "replicants")
    rep = next(iter(reps))
    host = reps[rep]["hosted_device_code"]
    devices = client.portal.call(db.kv_get, "devices")
    for d in devices:
        if d["device_code"] == host:
            d["tags"] = (d.get("tags") or []) + ["fleet:m1"]
            here = d["location"].split("-")[0]
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(eng.save_fleets, [{"id": "m1", "name": "Miner 1", "role": "mining", "home": here, "station": True, "wants": {}}])
    assert client.portal.call(eng.rider_pass) == []                              # no home change: nothing to do
    items = client.portal.call(eng.fleets)
    items[0]["home"] = "LERNA"
    client.portal.call(eng.save_fleets, items)                                   # e.g. the fleet chose its next home
    out = client.portal.call(eng.rider_pass)
    assert out == [f"{host} → LERNA"]
    job = next(j for j in client.portal.call(eng.jobs) if j["device"] == host and j["rule"] == "fleets")
    assert job["steps"][0]["path"] == f"/replicants/{rep}/travel" and job["steps"][0]["body"] == {"destination": "LERNA"}
    for d in devices:   # arrived: the follow-up is done
        if d["device_code"] == host:
            d["location"] = "LERNA-3"
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(db.kv_set, "automation_jobs", [])
    client.portal.call(eng.rider_pass)
    assert client.portal.call(db.kv_get, "rider_moves") == {}


def test_crew_riding_in_another_fleets_vessel_is_collected():
    """Live 2026-10-08: the Surveyors' survey controller and drones were stowed in Trader_1's heaven vessel in AEMEROTH;
    the mission left FALQUORYX without them and surveyed AEMEROTH instead of LYRHYRAN."""
    from rsweb import fleets as fl
    S = lambda **kw: {"status": "idle", "tags": [], **kw}  # noqa: E731
    devices = [S(device_code="CV", device_type="cargo_vessel", location="FAL-BELT-1", tags=["fleet:surv"],
                 features=["surge", "cruise", "attach", "transport"], stow_capacity=50, attach_capacity=3, cargo_capacity=200),
               S(device_code="HV", device_type="heaven_vessel", location="AEM-2", tags=["fleet:trader"],
                 features=["surge", "cruise"], stow_capacity=10),
               S(device_code="SC", device_type="ami_survey_controller", location=None, status="stowed",
                 stowed_in_device_code="HV", tags=["fleet:surv"], features=["ami", "cruise", "stow"]),
               S(device_code="SD", device_type="survey_drone", location=None, status="stowed", stowed_in_device_code="HV",
                 tags=["fleet:surv"], features=["cruise", "survey", "stow"])]
    fleet = {"id": "surv", "name": "Surveyors", "role": "explore", "home": "FAL", "wants": {}}
    stars = {k: {"position": {"x": x, "y": 0, "z": 0}, "entry_point": f"{k}-5-L4"} for k, x in (("FAL", 0), ("AEM", 3))}
    plan = fl.gather_plan(fleet, devices, stars, set())
    assert [s for s, _ in plan["tour"]] == ["AEM"] and {d["device_code"] for d in plan["tour"][0][1]} == {"SC", "SD"}
    steps = fl.gather_steps(fleet, plan, stars)
    descs = [s["desc"] for s in steps]
    assert descs[0].startswith("CV → AEM") and "SC: deploy out of HV" in descs and "SD: deploy out of HV" in descs
    assert descs.index("SC: deploy out of HV") < next(i for i, x in enumerate(descs) if x.startswith("stow SC in CV"))
    # in the same system as the fleet's carrier: assemble deploys them out of the other vessel and boards them
    devices[1]["location"] = "FAL-BELT-1"
    steps, problems = fl.assemble_steps(fleet, devices)
    descs = [s["desc"] for s in steps]
    assert not problems and "SC: deploy out of HV" in descs and "stow SC in CV" in descs
    assert descs.index("SC: deploy out of HV") < descs.index("stow SC in CV")


def test_survey_without_controller_isnt_done_between_bodies():
    """Live 2026-10-08 (DABAH): auto-survey drove the drone moon by moon; caught idle between two scans, the mission
    called the survey done and its recall failed with 'Cannot cruise while scanning'."""
    from rsweb import fleets as fl
    f = {"id": "sol", "name": "Sol Sector", "role": "explore", "home": "SOL", "wants": {}}
    drone = {"device_code": "SD", "device_type": "survey_drone", "location": "DABAH-1-5", "status": "idle", "tags": ["fleet:sol"]}
    done, why, upd = fl.watch_done(f, {}, [drone], "2026-10-08T13:39:55+00:00", busy={"SD"})   # auto-survey's job runs it
    assert not done and upd["idle_since"] is None
    done, why, upd = fl.watch_done(f, {}, [drone], "2026-10-08T13:39:55+00:00", busy=set())
    assert not done and upd["idle_since"] == "2026-10-08T13:39:55+00:00"                       # a pause, not the end yet
    assert not fl.watch_done(f, upd, [{**drone, "status": "scanning"}], "2026-10-08T13:41:00+00:00")[0]
    done, why, _ = fl.watch_done(f, upd, [drone], "2026-10-08T13:43:00+00:00", busy=set())
    assert done and why == "drones finished"                                                    # idle 3 min: really done


def test_survey_scan_of_a_body_shows_its_civilization(client):
    """ETHIREL-2 (2026-10-31): a drone's planet scan found spacefaring Drafel, the stored system scan knew no life."""
    db = client.app.state.db
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("ETHIREL", json.dumps({"planets": [{"designation": "ETHIREL-2", "life_stage": None}]}), "2026-10-30"))
    planet = {"designation": "ETHIREL-2", "life_stage": "spacefaring", "species_name": "Drafel"}
    client.portal.call(db.insert_event, {"id": "dg1#scan0", "event": "scan.completed", "device_code": "AB62BAE2",
                                         "location": "ETHIREL-2", "created_at": "2026-10-31T03:55:56+00:00",
                                         "payload": {"scan_target": "ETHIREL-2", "scan_type": "planet",
                                                     "report": {"planet": planet, "moons": [{"designation": "ETHIREL-2-1"}]}}})
    page = client.get("/game-events", headers=H).text
    assert "ETHIREL-2" in page and "Drafel" in page and "ETHIREL-2-1" not in page
    assert "ETHIREL-2" in client.get("/traffic", headers=H).text


def test_replicant_cooperation_and_cohort_permission(client):
    from rsweb.web_profile import profile_changes
    world = client.app.state.api.http._transport.app.state.world
    db = client.app.state.db
    client.portal.call(client.app.state.worker.sync_devices)
    rep = next(iter(client.portal.call(db.kv_get, "replicants")))
    page = client.get(f"/replicants/{rep}", headers=H).text
    assert 'name="cohort_permission"' in page
    client.post(f"/replicants/{rep}/profile", data={"cohort_permission": "public", "orig_cohort_permission": "private"}, headers=HX)
    assert world.profile_patches[-1] == {"code": rep, "cohort_permission": "public"}
    assert profile_changes({"cohort_permission": "everyone", "orig_cohort_permission": "private"})[1]   # not a choice
    assert 'name="replicant_cooperation"' in client.get("/account", headers=H).text
    r = client.post("/account/cooperation", data={"replicant_cooperation": "shared"}, headers=HX)
    assert r.status_code == 200
    sent = client.portal.call(db.fetchall, "SELECT path, body FROM actions ORDER BY rowid DESC LIMIT 1")[0]
    assert sent["path"] == "/accounts/me" and '"replicant_cooperation": "shared"' in sent["body"]


def test_survey_relays_wait_at_home_and_one_relay_per_system():
    from rsweb import loadouts as lo
    from rsweb import placement as pl
    cfg, devices, bps, inv, stars = _lo_world()
    cfg = {**cfg, "fleets": list(cfg.get("fleets") or []) + [{"id": "surveyors", "role": "explore", "home": "AAA"}]}
    devices += [
        {"device_code": "SR1", "device_type": "ftl_relay", "location": "AAA-BELT-1", "status": "idle", "tags": ["fleet:surveyors"],
         "available_commands": ["travel", "activate"], "operational_capacity": 100.0},
        {"device_code": "SR2", "device_type": "ftl_relay", "location": "AAA-3-L4", "status": "relaying", "tags": ["fleet:surveyors"],
         "available_commands": ["deactivate"], "operational_capacity": 100.0},
        {"device_code": "SR3", "device_type": "ftl_relay", "location": "AAA-3-L4", "status": "relaying", "tags": ["fleet:surveyors"],
         "available_commands": ["deactivate"], "operational_capacity": 100.0}]
    geo = {"AAA": pl.geography("AAA", devices, None, "AAA-OORT")}
    geo["AAA"]["lagrange"] = ["AAA-3-L4"]
    assert lo.survey_cargo(devices, cfg["fleets"]) == {"SR1", "SR2", "SR3"}
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {}, geo=geo)
    assert "SR1" not in dict(p["places"])                         # cargo: not sent to the L4 point
    assert "fleet:surveyors" in p["tag_remove"].get("SR2", []) and "at:aaa-3-l4" in p["tag_add"]["SR2"]   # stays as AAA's relay
    assert "SR3" not in p["tag_remove"] or "fleet:surveyors" not in p["tag_remove"]["SR3"]               # goes with the fleet
    # an idle relay that isn't cargo isn't placed where one already relays
    devices.append({"device_code": "RX", "device_type": "ftl_relay", "location": "AAA-BELT-1", "status": "idle",
                    "available_commands": ["travel", "activate"], "operational_capacity": 100.0})
    assert "RX" not in dict(lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {}, geo=geo)["places"])


def test_recall_withdraws_a_launched_controller_first():
    """Seen live 2026-10-11: the Surveyors recall cleared the launched survey controller's directive, then its travel to
    board failed with "Device is not deployed"."""
    from rsweb import fleets as fl
    fleet = {"id": "surveyors", "name": "Surveyors", "role": "explore", "home": "LOR", "wants": {}}
    devices = [
        {"device_code": "HV", "device_type": "cargo_vessel", "location": "LOR-6-L4", "status": "idle", "features": ["surge"],
         "stow_capacity": 20, "tags": ["fleet:surveyors"]},
        {"device_code": "SC", "device_type": "ami_survey_controller", "location": "LOR-6", "status": "launched",
         "features": ["stow"], "ami_directive": {"name": "survey_system"}, "tags": ["fleet:surveyors"]},
    ]
    steps = fl.recall_steps(fleet, devices, {}, None)
    descs = [s["desc"] for s in steps]
    # docs: withdraw pauses the directive (activate resumes): a launched controller isn't cleared first
    assert "SC: clear directive" not in descs
    assert descs.index("SC: withdraw (call its drones back)") < descs.index("SC → LOR-6-L4 (board)")
    w = steps[descs.index("SC: withdraw (call its drones back)")]
    assert w["body"] == {"command": "withdraw"} and w["wait"] == ["ami.withdrawn"]
    assert "not deployed" in steps[descs.index("SC → LOR-6-L4 (board)")]["hint"]


def test_deleting_a_fleet_untags_its_devices(client):
    db, eng = client.app.state.db, client.app.state.worker.automations
    client.post("/fleets", data={"name": "Gone", "role": "mining", "home": "SOL"}, headers=HX)
    fid = next(f["id"] for f in client.portal.call(eng.fleets) if f["name"] == "Gone")
    devs = client.portal.call(db.kv_get, "devices")
    devs += [{"device_code": "G1", "device_type": "mining_drone", "location": "SOL-BELT-1", "status": "idle", "tags": [f"fleet:{fid}", "keep"]},
             {"device_code": "G2", "device_type": "survey_drone", "location": "SOL-3", "status": "idle", "tags": [f"to:{fid}"]},
             {"device_code": "G3", "device_type": "survey_drone", "location": "SOL-3", "status": "idle", "tags": ["fleet:other"]}]
    client.portal.call(db.kv_set, "devices", devs)
    client.post(f"/fleets/{fid}/edit", data={"delete": "1"}, headers=HX)
    assert not any(f["id"] == fid for f in client.portal.call(eng.fleets))
    job = next(j for j in client.portal.call(eng.jobs) if "deleted" in j["title"])
    bodies = {s["path"]: s["body"]["configuration"]["remove_tags"] for s in job["steps"]}
    assert bodies == {"/devices/G1": [f"fleet:{fid}"], "/devices/G2": [f"to:{fid}"]}



@pytest.mark.parametrize("obs_type", ["galactic_observatory", "parallax_array"])
def test_prospector_fleet_tours_one_prospect_per_star(client, monkeypatch, obs_type):
    """A prospector fleet (observatory + carrier) moves its folded observatory to the emptiest un-prospected star within
    30 ly of home; the next prospect aims at the emptiest hemisphere."""
    from rsweb import prospecting as pr
    eng, db = client.app.state.worker.automations, client.app.state.db
    stars = [{"designation": "HOME", "position": {"x": 0, "y": 0, "z": 0}},
             {"designation": "NEAR", "position": {"x": 5, "y": 0, "z": 0}},
             {"designation": "FAR", "position": {"x": 60, "y": 0, "z": 0}}]
    stars += [{"designation": f"C{i}", "position": {"x": -10 - i * 0.1, "y": 0, "z": 0}} for i in range(5)]   # a crowd west
    client.portal.call(db.kv_set, "stars", {"stars": stars})
    assert pr.rank_directions([[1, 0, 0], [-1, 0, 0]], [0, 0, 0],
                              {s["designation"]: [s["position"][k] for k in "xyz"] for s in stars})[0] == [1, 0, 0]
    obs = {"device_code": "OBS1", "device_type": obs_type, "location": "HOME-3-L4", "status": "compacted",
           "tags": ["fleet:pro"], "available_commands": ["prospect", "unfurl"]}
    car = {"device_code": "CAR1", "device_type": "surge_carrier", "location": "HOME-3-L4", "status": "idle",
           "features": ["surge"], "attach_capacity": 4, "tags": ["fleet:pro"]}
    client.portal.call(db.kv_set, "devices", [obs, car])
    client.portal.call(db.kv_set, "prospected_from", {"HOME": {"at": "2026-10-15T00:00:00+00:00", "outcome": "3 new star(s)"}})
    client.portal.call(eng.save_fleets, [{"id": "pro", "name": "Prospector 1", "role": "prospector", "home": "HOME", "wants": {}}])
    async def covered(devices, stars=None):   # a relay network over the whole area
        return {"HOME", "NEAR", "FAR"} | {f"C{i}" for i in range(5)}
    monkeypatch.setattr(eng, "commandable", covered)
    out = client.portal.call(eng.prospector_pass)
    assert out and "HOME → " in out[0] and "FAR" not in out[0]          # FAR is beyond 30 ly
    job = [j for j in client.portal.call(eng.jobs) if "prospecting tour" in j["title"]][-1]
    assert [s["body"]["command"] for s in job["steps"] if s.get("body")] == ["attach", "travel", "detach"]
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "pro")
    assert f["station"] is False and f["tour"]["target"] == "NEAR"     # the crowd west of HOME counts against C0…C4
    assert client.get("/fleets/details?all=1", headers=H).status_code == 200



def test_prospector_fleet_picks_up_every_member_first(client, monkeypatch):
    """A prospector fleet (observatory + carrier) moves its folded observatory to the emptiest un-prospected star within
    30 ly of home; the next prospect aims at the emptiest hemisphere."""
    from rsweb import prospecting as pr
    eng, db = client.app.state.worker.automations, client.app.state.db
    stars = [{"designation": "HOME", "position": {"x": 0, "y": 0, "z": 0}},
             {"designation": "NEAR", "position": {"x": 5, "y": 0, "z": 0}},
             {"designation": "FAR", "position": {"x": 60, "y": 0, "z": 0}}]
    stars += [{"designation": f"C{i}", "position": {"x": -10 - i * 0.1, "y": 0, "z": 0}} for i in range(5)]   # a crowd west
    client.portal.call(db.kv_set, "stars", {"stars": stars})
    assert pr.rank_directions([[1, 0, 0], [-1, 0, 0]], [0, 0, 0],
                              {s["designation"]: [s["position"][k] for k in "xyz"] for s in stars})[0] == [1, 0, 0]
    obs = {"device_code": "OBS1", "device_type": "galactic_observatory", "location": "HOME-3-L4", "status": "compacted",
           "tags": ["fleet:pro"], "available_commands": ["prospect", "unfurl"]}
    car = {"device_code": "CAR1", "device_type": "surge_carrier", "location": "HOME-3-L4", "status": "idle",
           "features": ["surge"], "attach_capacity": 4, "tags": ["fleet:pro"]}
    mat = {"device_code": "MAT1", "device_type": "matrix_container", "location": "HOME-BELT-1", "status": "idle",
           "tags": ["fleet:pro"]}
    client.portal.call(db.kv_set, "devices", [obs, car, mat])
    client.portal.call(db.kv_set, "prospected_from", {"HOME": {"at": "2026-10-15T00:00:00+00:00", "outcome": "3 new star(s)"}})
    client.portal.call(eng.save_fleets, [{"id": "pro", "name": "Prospector 1", "role": "prospector", "home": "HOME", "wants": {}}])
    async def covered(devices, stars=None):   # a relay network over the whole area
        return {"HOME", "NEAR", "FAR"} | {f"C{i}" for i in range(5)}
    monkeypatch.setattr(eng, "commandable", covered)
    out = client.portal.call(eng.prospector_pass)
    assert out and "HOME → " in out[0] and "FAR" not in out[0]          # FAR is beyond 30 ly
    job = [j for j in client.portal.call(eng.jobs) if "prospecting tour" in j["title"]][-1]
    cmds = [(s["path"].split("/")[2], s["body"]["command"], s["body"].get("device") or s["body"].get("destination"))
            for s in job["steps"] if s.get("body")]
    assert cmds[0] == ("CAR1", "travel", "HOME-BELT-1") and ("CAR1", "travel", "HOME-3-L4") in cmds   # fetch, come back
    assert any(c[0] == "MAT1" or c[2] == "MAT1" for c in cmds)                                       # MAT1 boards
    assert [c[1] for c in cmds][-3:] == ["attach", "travel", "detach"] and "picking up MAT1" in client.portal.call(
        eng.fleets)[0]["tour"]["note"]
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "pro")
    assert f["station"] is False and f["tour"]["target"] == "NEAR"     # the crowd west of HOME counts against C0…C4
    assert client.get("/fleets/details?all=1", headers=H).status_code == 200


def test_auto_scout_deploys_worn_members_for_repairs(client):
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(eng.save_fleets, [{"id": "x1", "name": "Scouts", "role": "explore", "home": "SOL", "wants": {},
                                          "auto_scout": True}])
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": "SHIP0001", "device_type": "heaven_vessel", "location": "SOL-3", "status": "idle",
                 "operational_capacity": 1.0, "tags": ["fleet:x1"]},
                {"device_code": "WORN0001", "device_type": "survey_drone", "location": "SOL-3", "status": "stowed",
                 "stowed_in_device_code": "SHIP0001", "operational_capacity": 0.6, "tags": ["fleet:x1"]}]
    client.portal.call(db.kv_set, "devices", devices)
    sent = []
    real = eng.send

    async def send(method, path, body, label):
        sent.append((path, body))
        return True, {}, None
    eng.send = send
    try:
        out = client.portal.call(eng.auto_scout_pass)
        assert out == ["Scouts: deployed WORN0001 for repairs"]
        assert sent == [("/devices/WORN0001", {"command": "deploy"})]
        assert client.portal.call(eng.auto_scout_pass) == [] and len(sent) == 1    # not again while the list lags
    finally:
        eng.send = real


def test_fleets_listed_alphabetically(client):
    eng = client.app.state.worker.automations
    client.portal.call(eng.save_fleets, [{"id": f"z{i}", "name": n, "role": "explore", "home": "SOL", "wants": {}}
                                         for i, n in enumerate(("Zulu Fleetq", "alpha Fleetq", "Mike Fleetq"))])
    for url in ("/fleets/overview", "/fleets/details?all=1"):
        t = client.get(url, headers=HX).text
        mark = "?fleet=" if url.endswith("overview") else 'id="fleet-'   # z1 alpha, z2 Mike, z0 Zulu
        assert t.index(mark + "z1") < t.index(mark + "z2") < t.index(mark + "z0"), url


def test_worn_fleets_return_home_and_deploy_for_repairs(client):
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(eng.save_fleets, [
        {"id": "m1", "name": "Miners", "role": "mining", "home": "SOL", "wants": {},
         "mission": {"status": "running", "phase": "watch", "targets": ["AEM"], "log": []}},
        {"id": "m2", "name": "Back", "role": "mining", "home": "SOL", "wants": {}, "mission": {"status": "done", "log": []}}])
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": "WORNM001", "device_type": "mining_drone", "location": "AEM-BELT-1", "status": "mining",
                 "operational_capacity": 45.0, "tags": ["fleet:m1"]},
                {"device_code": "SHIPM002", "device_type": "cargo_vessel", "location": "SOL-3", "status": "idle",
                 "operational_capacity": 100.0, "tags": ["fleet:m2"]},
                {"device_code": "WORNM002", "device_type": "mining_drone", "location": "SOL-3", "status": "stowed",
                 "stowed_in_device_code": "SHIPM002", "operational_capacity": 70.0, "tags": ["fleet:m2"]}]
    client.portal.call(db.kv_set, "devices", devices)
    sent = []
    real = eng.send

    async def send(method, path, body, label):
        sent.append((path, body))
        return True, {}, None
    eng.send = send
    try:
        out = client.portal.call(eng.repair_return_pass)
    finally:
        eng.send = real
    fl = {f["id"]: f for f in client.portal.call(eng.fleets)}
    assert fl["m1"]["mission"]["recall_now"] and fl["m1"]["mission"]["repair_return"]
    assert "Miners: home for repairs" in out and "Back: deployed WORNM002 for repairs" in out
    assert sent == [("/devices/WORNM002", {"command": "deploy"})]


# --- 1.189.0: a device's page says what it's doing ------------------------------------------------------------------

def test_device_page_shows_fleet_status_and_current_task(client):
    """Joe 2026-10-21: a fleet member's page shows its fleet's status, a logistics vessel's its current task."""
    eng = client.app.state.worker.automations
    fleet, devices = _fleet_world()
    devices.append({"device_code": "LV000001", "device_type": "cargo_freighter", "location": "AEM-5-L4", "status": "idle",
                    "tags": ["fleet:haulers"], "available_commands": ["travel"]})
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    fleet["mission"] = {"status": "running", "phase": "work", "idx": 0, "targets": ["XYZ"], "job": "fleets-1-0",
                        "log": [{"at": "2026-10-21T09:00:00+00:00", "text": "mining XYZ-BELT-1"}]}
    client.portal.call(eng.save_fleets, [fleet, {"id": "haulers", "name": "Haulers", "role": "logistics", "home": "AEM"}])
    client.portal.call(eng.save_jobs, [{"id": "logistics-1-0", "rule": "logistics", "title": "logistics: run 3: LV000001 carries 50 iron → BOB",
                                        "device": "LV000001", "idx": 1, "status": "running", "meta": {},
                                        "steps": [{"desc": "LV000001: load 50 iron", "path": "/devices/LV000001", "body": {}},
                                                  {"desc": "LV000001: travel to BOB", "path": "/devices/LV000001", "body": {}}]}])
    page = client.get(f"/devices/{fleet_member_code(devices)}", headers=H).text
    assert 'id="doing"' in page and "on a mission to XYZ (work phase)" in page and "mining XYZ-BELT-1" in page
    page = client.get("/devices/LV000001", headers=H).text
    assert "Current task:</b> run 3: LV000001 carries 50 iron → BOB" in page and "step 2/2: LV000001: travel to BOB" in page
    assert "logistics service: on a run" in page
    client.portal.call(eng.save_jobs, [])
    page = client.get("/devices/LV000001", headers=H).text
    assert "idle, waiting for a task" in page and "Current task" not in page


# --- 1.190.0: Fleets pages keep their plans for an hour ---------------------------------------------------------------

def test_fleet_pages_keep_plans_until_an_edit_or_recompute(client):
    """Joe 2026-10-21: the loadout plan, prospects and destination lists only need rerunning every hour or two."""
    eng = client.app.state.worker.automations
    calls = []
    real = eng.loadout_plan

    async def counted(*a, **k):
        calls.append(1)
        return await real(*a, **k)
    eng.loadout_plan = counted
    assert client.get("/fleets/overview", headers=HX).status_code == 200
    page = client.get("/fleets/details?all=1", headers=H).text
    assert len(calls) == 1 and "kept an hour" in page and "Recompute" in page
    client.post("/fleets/recompute", headers=HX)
    client.get("/fleets/overview", headers=HX)
    assert len(calls) == 2
    client.post("/fleets/nope/station", data={"station": "on"}, headers=HX)   # any change under /fleets clears them too
    client.get("/fleets/overview", headers=HX)
    assert len(calls) == 3


def test_survey_crew_stays_aboard_at_the_kuiper_belt():
    from rsweb import fleets as fl
    fleet = {"id": "s", "name": "S", "role": "explore", "home": "AAA"}
    devs = [{"device_code": "HV", "device_type": "heaven_vessel", "location": "BBB-KUIPER", "status": "idle",
             "features": ["surge"], "stow_capacity": 10, "tags": ["fleet:s"]},
            {"device_code": "SC", "device_type": "ami_survey_controller", "location": None, "stowed_in_device_code": "HV",
             "status": "stowed", "tags": ["fleet:s"]}]
    assert fl.loaded_outer(fleet, devs)
    devs[0]["location"] = "BBB-1-L4"
    assert not fl.loaded_outer(fleet, devs)


def test_a_stalled_mission_retries_by_itself_with_backoff(client):
    """Owner 2026-10-22: a stalled fleet tries the stalled phase again by itself, waiting longer each time
    (Surveyor 3 sat stalled for hours on a survey controller left in LYRRERARN)."""
    from datetime import datetime, timedelta, timezone
    from rsweb import fleets as fl
    db, eng = client.app.state.db, client.app.state.worker.automations
    devs = [{"device_code": "SC1", "device_type": "ami_survey_controller", "location": "LYR-1-L4", "status": "idle",
             "tags": ["fleet:s3"]},
            {"device_code": "HV1", "device_type": "heaven_vessel", "location": "ARF-1-L4", "status": "idle",
             "tags": ["fleet:s3"]}]
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(eng.save_fleets, [{"id": "s3", "name": "S3", "role": "explore", "home": "FAL", "wants": {},
                                          "mission": {"status": "running", "phase": "deploy", "targets": ["ARF"],
                                                      "idx": 0, "departed": True, "log": []}}])
    client.portal.call(eng.run_fleets)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["status"] == "stalled" and m["auto_retry"]["n"] == 0 and m["auto_retry"]["spot"] == [0, "work"]
    first = datetime.fromisoformat(m["auto_retry"]["at"])
    assert timedelta(minutes=14) < first - datetime.now(timezone.utc) <= timedelta(minutes=15)
    assert "retries by itself at" in client.get("/fleets/details?all=1", headers=H).text
    client.portal.call(eng.run_fleets)   # not due yet: stays put
    assert client.portal.call(eng.fleets)[0]["mission"]["auto_retry"]["n"] == 0
    items = client.portal.call(eng.fleets)
    items[0]["mission"]["auto_retry"]["at"] = "2026-01-01T00:00:00+00:00"
    client.portal.call(eng.save_fleets, items)
    client.portal.call(eng.run_fleets)   # due: tries again, stalls again on the same thing, waits twice as long
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["status"] == "stalled" and m["auto_retry"]["n"] == 1
    assert any("retrying by itself (try 1 of" in x["text"] for x in m["log"])
    assert datetime.fromisoformat(m["auto_retry"]["at"]) - datetime.now(timezone.utc) > timedelta(minutes=29)
    # used up: it waits for Retry
    items = client.portal.call(eng.fleets)
    items[0]["mission"]["auto_retry"].update(n=fl.RETRY_TRIES - 1, at="2026-01-01T00:00:00+00:00")
    client.portal.call(eng.save_fleets, items)
    client.portal.call(eng.run_fleets)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["status"] == "stalled" and m["auto_retry"]["at"] is None
    assert "no more automatic retries" in m["log"][-1]["text"]
    # Retry by hand starts the count over
    client.post("/fleets/s3/control", data={"action": "resume"}, headers=HX)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["auto_retry"]["n"] == 0 and m["auto_retry"]["at"]


def test_idle_survey_drones_search_a_belt_under_the_site_cap(client):
    """2026-10-23: idle survey drones at ISOLITHOR-BELT-1, whose open sites lacked what the factory waited for; the belt
    wasn't worked out, so nothing sent them searching."""
    eng = _enable(client, "reopen_sites")
    db = client.app.state.db
    devs = [{"device_code": f"S{i}", "device_type": "survey_drone", "status": "idle", "location": "ISO-BELT-1"} for i in range(3)]
    devs.append({"device_code": "S9", "device_type": "survey_drone", "status": "tracking", "location": "ISO-BELT-1"})
    sites = [{"designation": f"ISO-BELT-1-SITE-{i}", "resources_remaining_pct": {"carbon": 100}} for i in range(3)]
    client.portal.call(db.kv_set, "loc:ISO-BELT-1", {"resource_sites": sites})
    client.portal.call(db.kv_set, "belt_reads", {"ISO-BELT-1": "2099-01-01T00:00:00+00:00"})

    async def devices(*a, **k):
        return devs
    eng.devices = devices
    done = client.portal.call(eng.rule_reopen_sites)
    assert done == ["S0 → search ISO-BELT-1", "S1 → search ISO-BELT-1"]   # 3 open of 5
    assert client.portal.call(eng.rule_reopen_sites) == []   # running jobs / cooldown


def test_mining_fleets_may_work_a_hub_home_without_miners(client):
    from rsweb import fleets as fl
    hub = {"id": "h", "name": "Hub", "role": "mining", "home": "HUB", "station": True, "wants": {"autofactory": 1}}
    mine = {"id": "k", "name": "Kel", "role": "mining", "home": "KEL", "station": True, "wants": {"mining_drone": 6}}
    devs = [{"device_code": "D1", "device_type": "mining_drone", "location": "HUB-BELT-1", "tags": ["fleet:h"]}]
    assert fl.worked_systems([hub, mine]) == {"HUB", "KEL"}          # explore missions: every stationed home
    assert fl.worked_systems([hub, mine], []) == {"KEL"}             # mining: a hub without miners is open
    assert fl.worked_systems([hub, mine], devs) == {"HUB", "KEL"}    # a miner among its members claims it again
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [{"designation": "SOL"}, {"designation": "HUB"},
                                                                        {"designation": "KEL"}]})
    client.portal.call(eng.save_fleets, [hub, mine, {"id": "m", "name": "M", "role": "mining", "home": "FAL", "wants": {}},
                                         {"id": "e", "name": "E", "role": "explore", "home": "FAL", "wants": {}}])
    r = client.post("/fleets/m/mission", data={"targets": "KEL"}, headers=HX)
    assert "only the stationed fleet works" in r.text
    r = client.post("/fleets/m/mission", data={"targets": "HUB"}, headers=HX)
    assert "only the stationed fleet works" not in r.text
    r = client.post("/fleets/e/mission", data={"targets": "HUB"}, headers=HX)
    assert "only the stationed fleet works" in r.text


def test_arrival_at_a_new_home_drops_the_old_home_tag_and_another_fleets_tag():
    """Snapshot 2026-11-01: Miner_1 moved home ANOAAIEON → SOLFAMAR; its rigs at SOLFAMAR kept to:anoaaieon (the arrival
    step removed only the planner's to:solfamar), so every pass made a new "arrived" job. Rigs handed to another fleet
    kept their old fleet: tag."""
    from rsweb import loadouts as lo
    rig = {"device_code": "RIG", "device_type": "mining_rig", "location": "LAR-7-L4", "status": "idle",
           "replicant_code": "R1", "features": ["cruise", "mine", "fracture"], "tags": ["fleet:m1", "to:kel"]}
    stars = {s: {"position": {"x": i, "y": 0, "z": 0}, "entry_point": f"{s}-1-L4"} for i, s in enumerate(("FAL", "KEL", "LAR"))}
    fleets = [{"id": "m1", "name": "Miner 1", "home": "LAR", "station": True, "wants": {}}]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, [rig], [], {}, stars, {}, set(), [], {})
    assert p["arrived"] == ["RIG"]
    st = lo.arrived_steps("RIG", p["by_code"]["RIG"], {})[-1]
    assert sorted(st["body"]["configuration"]["remove_tags"]) == ["to:kel", "to:lar"]
    # joining another fleet: the old fleet tag goes
    st = lo.rehome_step("RIG", {"tags": ["fleet:m10", "to:lar"]}, "LAR", join="fleet:m7")
    assert st["body"]["configuration"] == {"add_tags": ["fleet:m7"], "remove_tags": ["fleet:m10", "to:lar"]}


def test_explore_watch_ends_when_the_game_paused_the_survey_controller():
    """Live 2026-11-10: the Season 4 time skip paused Surveyor 1's survey_system at MUSPELION; it watched for 18 hours."""
    from rsweb import fleets as fl
    f = {"id": "s1", "role": "explore", "wants": {"ami_survey_controller": 1, "survey_drone": 1}}
    devices = [{"device_code": "C1", "device_type": "ami_survey_controller", "location": "MUS-OORT", "status": "paused",
                "ami_directive_status": "paused", "tags": ["fleet:s1"],
                "ami_directive": {"name": "survey_system", "_eval_state": "assigned:7:0"}},
               {"device_code": "D1", "device_type": "survey_drone", "location": "MUS-3", "status": "idle", "tags": ["fleet:s1"]}]
    m = {"phase": "watch", "phase_at": "2026-11-09T18:30:00+00:00"}
    done, why, _ = fl.watch_done(f, m, devices, "2026-11-10T12:00:00+00:00")
    assert done and "paused" in why
    devices[1]["status"] = "scanning"
    assert not fl.watch_done(f, m, devices, "2026-11-10T12:00:00+00:00")[0]


def test_fleet_card_hides_seats_without_carrier(client):
    client.post("/fleets", data={"name": "Rocks", "role": "mining", "home": "SOL"}, headers=HX)
    client.post("/fleets/rocks/edit", data={"lines": "1", "type": ["mining_drone"], "qty": ["2"], "stock": [""]}, headers=HX)
    card = client.get("/fleets/details?fleet=rocks", headers=H).text
    card = card[card.index('id="fleet-rocks"'):]
    assert 'class="no-carrier"' in card and "No carrier in the loadout" in card
    assert "device(s) can't fly themselves" in card
    assert 'class="small attach-points" style="margin-top:6px" hidden' in card
    client.post("/fleets/rocks/edit", data={"lines": "1", "type": ["mining_drone", "surge_carrier"], "qty": ["2", "1"],
                                            "stock": ["", ""]}, headers=HX)
    card = client.get("/fleets/details?fleet=rocks", headers=H).text
    card = card[card.index('id="fleet-rocks"'):]
    assert 'class="no-carrier"' not in card and "At full loadout" in card
    assert 'class="small attach-points" style="margin-top:6px">' in card
