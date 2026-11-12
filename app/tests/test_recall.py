"""The standard recall (owner 2026-11-01): Recall now / Recall after task for every fleet role, to the station, else the
nearest receiving point."""
from rsweb import fleets as fl
from test_core import _fleet_world
from test_ops import H, HX, client, eng  # noqa: F401  (fixture)




def test_recall_target_station_then_nearest_receiving_point():
    f, devices = _fleet_world()
    others = [{"id": "hub-a", "home": "FAR", "station": True}, {"id": "hub-b", "home": "NEAR", "materials": "self"},
              {"id": "logi", "home": "CLOSE", "role": "logistics", "station": True}]
    pos = {"AEM": 0, "FAR": 50, "NEAR": 10, "CLOSE": 1}
    dist = lambda a, b: abs(pos.get(a, 1e9) - pos.get(b, 1e9))   # noqa: E731
    assert fl.where(f, devices) == "AEM"
    assert fl.recall_target(f, [f] + others, devices, dist) == ("NEAR", "the nearest receiving point")
    assert fl.recall_target({**f, "station": True}, [f] + others, devices, dist) == ("AEM", "its station")
    assert fl.recall_target(f, [f], devices, dist)[0] == "AEM"   # no receiving point: home


def test_homing_phases():
    m = {"homing": True, "phase": None}
    assert fl.next_phase("rock_chaser", m) == "recall"
    m["phase"] = "recall"
    assert fl.next_phase("logistics", m) == "return"
    m["phase"] = "unload"
    assert fl.next_phase("mining", m) is None
    fl.rewind({"role": "logistics"}, {**m, "phase": "return"})


def test_recall_now_cancels_and_flies_to_the_receiving_point(client):
    e = client.app.state.worker.automations
    fleet, devices = _fleet_world()
    fleet["mission"] = {"status": "running", "phase": "watch", "targets": ["AEM"], "belt": "AEM-BELT-1", "log": [],
                        "departed": True}
    hub = {"id": "hub", "name": "Hub", "role": "mining", "home": "FAL", "station": True}
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [
        {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in (("AEM", 0), ("FAL", 5))]})
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(e.save_fleets, [fleet, hub])
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Recall now" in page and "Recall after task" in page
    r = client.post(f"/fleets/{fleet['id']}/control", data={"action": "recall_now"}, headers=HX)
    assert r.headers.get("HX-Refresh")
    f = next(x for x in client.portal.call(e.fleets) if x["id"] == fleet["id"])
    m = f["mission"]
    assert m["homing"] and m["recall"]["mode"] == "now" and m["drop_star"] == "FAL" and m["phase"] == "recall"
    job = next(j for j in client.portal.call(e.jobs) if j["id"] == m["job"])
    cmds = [(s["body"] or {}).get("command") for s in job["steps"]]
    assert "clear_directive" in cmds or any(c in ("attach", "stow") for c in cmds)


def test_recall_after_task_lets_the_mission_finish(client):
    e = client.app.state.worker.automations
    fleet, devices = _fleet_world()
    fleet["role"] = "explore"
    fleet["mission"] = {"status": "running", "phase": "watch", "targets": ["AEM", "BEX", "CYR"], "idx": 0, "log": [],
                        "departed": True}
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(e.save_fleets, [fleet])
    client.post(f"/fleets/{fleet['id']}/control", data={"action": "recall_after"}, headers=HX)
    m = client.portal.call(e.fleets)[0]["mission"]
    assert not m.get("homing") and m["recall"]["mode"] == "after" and m["targets"] == ["AEM"] and m["phase"] in ("watch", "recall")


def test_hold_role_recall_waits_for_its_task_then_holds(client):
    e = client.app.state.worker.automations
    fleet, devices = _fleet_world()
    fleet.update(role="prospector", station=False)
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(e.save_fleets, [fleet])
    from rsweb.automations import step
    wait = step("wait for MF000001", "", None, method="WAIT", wait=["travel.arrived"], match={"destination": "X"})
    job = client.portal.call(e.create_job, "observatory", "tour leg", "MF000001", [wait], {"devices": ["MF000001"]}, True)
    jobs = client.portal.call(e.jobs)
    for j in jobs:
        if j["id"] == job["id"]:
            j["status"] = "running"
    client.portal.call(e.save_jobs, jobs)
    client.post(f"/fleets/{fleet['id']}/control", data={"action": "recall_after"}, headers=HX)
    f = client.portal.call(e.fleets)[0]
    assert f["recall_hold"]["state"] == "waiting" and not (f.get("mission") or {}).get("homing")
    assert "finishing its current task" in client.get("/fleets/details?all=1", headers=H).text
    jobs = client.portal.call(e.jobs)
    for j in jobs:
        if j["id"] == job["id"]:
            j["status"] = "done"
    client.portal.call(e.save_jobs, jobs)
    client.portal.call(e.run_fleets)
    f = client.portal.call(e.fleets)[0]
    assert f["mission"]["homing"] and f["recall_hold"]["state"] == "returning"
    assert client.portal.call(e.prospector_pass) == []   # held: its pass gives it no work
    # trip over: held at home until Resume
    f["mission"].update(phase="unload", job=None)
    client.portal.call(e.save_fleets, [f])
    client.portal.call(e.run_fleets)
    f = client.portal.call(e.fleets)[0]
    assert f["recall_hold"]["state"] == "home" and f["mission"]["status"] == "done"
    assert "Resume" in client.get("/fleets/details?all=1", headers=H).text
    client.post(f"/fleets/{fleet['id']}/control", data={"action": "resume_recall"}, headers=HX)
    assert not client.portal.call(e.fleets)[0].get("recall_hold")


def test_held_logistics_fleet_leaves_the_pool():
    from rsweb import logistics as lg
    fs = [{"id": "a", "role": "logistics"}, {"id": "b", "role": "logistics", "recall_hold": {"state": "home"}}]
    assert [f["id"] for f in lg.logistics_fleets(fs)] == ["a"]
    assert len(lg.logistics_fleets(fs, held=True)) == 2


def test_a_recall_to_its_own_station_ends_once_everyone_is_there(client):
    """Live 2026-11-12: Miner_7 recalled at its station DOMBTAIRE stalled for hours: no carrier to put everyone aboard,
    though nothing had to fly."""
    e = client.app.state.worker.automations
    fleet, devices = _fleet_world()
    home = fl.where(fleet, devices)
    devices = [{**d, "location": f"{home}-3-L4"} if d.get("location") else d for d in devices]   # everyone at home
    fleet = {**fleet, "station": True, "home": home, "role": "mining"}
    fleet["mission"] = {"status": "stalled", "homing": True, "phase": "recall", "targets": [], "idx": 0, "log": [],
                        "departed": True, "drop_star": home, "auto_retry": {"spot": [0, "recall"], "n": 1, "at": "2026-01-01T00:00:00+00:00"}}
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(e.save_fleets, [fleet])
    client.portal.call(e.run_fleets)
    m = next(x for x in client.portal.call(e.fleets) if x["id"] == fleet["id"])["mission"]
    assert m["status"] == "done" and "already at" in m["log"][-1]["text"]


def test_auto_scout_goes_beyond_relay_coverage_with_a_replicant_aboard(client):
    """Joe 2026-11-12: survey fleets carry replicants, so relay coverage shouldn't hold them back."""
    e = client.app.state.worker.automations
    db = client.app.state.db

    async def commandable(devices, stars=None, without=None):
        return {"HOMEA"}
    e.commandable = commandable
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": s, "position": {"x": x, "y": 0, "z": 0}}
                                                      for s, x in (("HOMEA", 0), ("FARB", 20))]})
    ves = {"device_code": "HV1", "device_type": "heaven_vessel", "location": "HOMEA-BELT-1", "status": "idle",
           "tags": ["fleet:s1"]}
    f = {"id": "s1", "name": "S1", "role": "explore", "home": "HOMEA", "station": True, "wants": {}}
    star, why = client.portal.call(e.scout_next, f, [f], [ves], None, {"HOMEA"})
    assert star is None and "inside relay coverage" in why
    star, _ = client.portal.call(e.scout_next, f, [f], [{**ves, "hosting_replicant": {"replicant_code": "R1"}}], None, {"HOMEA"})
    assert star == "FARB"
