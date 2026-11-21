"""Loadout option "pick up a spare replicant matrix" (Joe 2026-11-10): a fleet with no replicant aboard fetches one."""
from rsweb import fleets as fl
from test_ops import H, HX, client  # noqa: F401  (fixture)

FLEET = {"id": "surveyor-6", "name": "Surveyor 6", "role": "explore", "home": "SOLARES", "station": True,
         "wants": {"heaven_vessel": 1, "survey_drone": 8}, "pick_matrix": True}
STARS = {"SOLARES": {"position": {"x": 0, "y": 0, "z": 0}}, "FALQUORYX": {"position": {"x": 5, "y": 0, "z": 0}},
         "FARAWAY": {"position": {"x": 5000, "y": 0, "z": 0}}}


def _vessel(**kw):
    return {"device_code": "HV000006", "device_type": "heaven_vessel", "status": "idle", "location": "SOLARES-4-L4",
            "tags": ["fleet:surveyor-6"], "stow_capacity": 10, "stow_used": 1, **kw}


def _devices():
    return [
        _vessel(),
        {"device_code": "MX000001", "device_type": "replicant_matrix", "status": "idle", "location": "FALQUORYX-BELT-1", "tags": ["spare"]},
        {"device_code": "MX000007", "device_type": "replicant_matrix", "status": "idle", "location": "SOLARES-BELT-1", "tags": []},  # untagged: never
        # in a fleetless matrix container, tagged spare
        {"device_code": "MC000001", "device_type": "matrix_container", "status": "idle", "location": "SOLARES-BELT-1", "tags": [],
         "hosting_replicant": {"name": "Clone"}},
        {"device_code": "MX000002", "device_type": "replicant_matrix", "status": "stowed", "stowed_in_device_code": "MC000001", "tags": ["spare"]},
        # in a fleetless vessel of its own, untagged: not spare
        {"device_code": "HV000099", "device_type": "heaven_vessel", "status": "idle", "location": "SOLARES-3-L4", "tags": [],
         "hosting_replicant": {"name": "Rufus"}},
        {"device_code": "MX000003", "device_type": "replicant_matrix", "status": "stowed", "stowed_in_device_code": "HV000099", "tags": []},
        # a fleet's replicant marked spare by hand: spare
        {"device_code": "HV000050", "device_type": "heaven_vessel", "status": "idle", "location": "FALQUORYX-1-L4",
         "tags": ["fleet:logistics"], "hosting_replicant": {"name": "Sk3y-24"}},
        {"device_code": "MX000004", "device_type": "replicant_matrix", "status": "stowed", "stowed_in_device_code": "HV000050",
         "tags": ["spare"]},
        # reserved / empty / in a fleet: never
        {"device_code": "MX000005", "device_type": "replicant_matrix", "status": "idle", "location": "SOLARES-BELT-1", "tags": ["reserved"]},
        {"device_code": "EM000001", "device_type": "empty_replicant_matrix", "status": "idle", "location": "SOLARES-BELT-1", "tags": ["spare"]},
        {"device_code": "MX000006", "device_type": "replicant_matrix", "status": "idle", "location": "SOLARES-BELT-1", "tags": ["fleet:miner-6"]},
    ]


def test_spare_matrices_rules():
    codes = {m["device_code"] for m in fl.spare_matrices(_devices(), set())}
    assert codes == {"MX000001", "MX000002", "MX000004"}
    assert "MX000002" not in {m["device_code"] for m in fl.spare_matrices(_devices(), {"MC000001"})}   # host in a job
    m = next(m for m in fl.spare_matrices(_devices(), set()) if m["device_code"] == "MX000002")
    assert m["_at"] == "SOLARES-BELT-1" and m["_from_host"] == "MC000001"


def test_has_replicant():
    ds = _devices()
    assert not fl.has_replicant(FLEET, ds)
    ds[0] = _vessel(hosting_replicant={"name": "x"})
    assert fl.has_replicant(FLEET, ds)
    ds = _devices() + [{"device_code": "MX000009", "device_type": "replicant_matrix", "stowed_in_device_code": "HV000006"}]
    assert fl.has_replicant(FLEET, ds)


def test_pickup_nearest_in_range_then_back():
    ds = _devices()
    steps, m, v, note = fl.matrix_pickup(FLEET, ds, fl.spare_matrices(ds, set()), STARS, set(), 1728)
    assert m["device_code"] == "MX000002" and v["device_code"] == "HV000006"   # same system first
    body = [(s["method"], s["path"], s["body"]) for s in steps]
    assert body[0] == ("PATCH", "/devices/MX000002", {"configuration": {"add_tags": ["fleet:surveyor-6"], "remove_tags": ["spare"]}})
    assert body[1] == ("POST", "/devices/HV000006", {"command": "travel", "destination": "SOLARES-BELT-1"})
    assert body[2] == ("POST", "/devices/MX000002", {"command": "deploy"})
    assert body[3] == ("POST", "/devices/MX000002", {"command": "stow", "target": "HV000006"})
    assert body[4] == ("POST", "/devices/HV000006", {"command": "travel", "destination": "SOLARES-4-L4"})
    # the spare tag comes off a matrix taken from another fleet's vessel
    far = [x for x in fl.spare_matrices(ds, set()) if x["device_code"] == "MX000004"]
    steps, m, _, _ = fl.matrix_pickup(FLEET, ds, far, STARS, set(), 1728)
    assert steps[0]["body"]["configuration"]["remove_tags"] == ["spare"]
    # beyond the supply range: nothing
    away = [{**far[0], "_at": "FARAWAY-BELT-1"}]
    steps, m, _, note = fl.matrix_pickup(FLEET, ds, away, STARS, set(), 1728)
    assert not steps and "supply range" in note
    # no idle vessel
    steps, _, _, note = fl.matrix_pickup(FLEET, ds, far, STARS, {"HV000006"}, 1728)
    assert not steps and "no idle vessel" in note


def test_pass_starts_a_job_and_the_card_shows_the_option(client):
    e = client.app.state.worker.automations
    client.portal.call(client.app.state.db.kv_set, "devices", _devices())
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [{"designation": k, **v} for k, v in STARS.items()]})
    client.portal.call(e.save_fleets, [dict(FLEET)])
    out = client.portal.call(e.matrix_pickup_pass, True)
    assert out and "MX000002" in out[0]
    job = next(j for j in client.portal.call(e.jobs) if "replicant matrix" in j["title"])
    assert job["meta"]["fleet"] == "surveyor-6" and "MX000002" in job["meta"]["devices"]
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Needs an active replicant aboard" in page and "no replicant aboard" in page and "fetches matrix MX000002" in page
    ov = client.get("/fleets/overview", headers=HX).text
    assert "no replicant aboard" in ov and "fetches matrix MX000002" in ov
    ds = _devices()
    ds[0]["hosting_replicant"] = {"name": "Bob"}
    client.portal.call(client.app.state.db.kv_set, "devices", ds)
    assert "✓ replicant aboard" in client.get("/fleets/overview", headers=HX).text
    client.post("/fleets/surveyor-6/edit", data={"pick_matrix_set": "1", "template": ""}, headers=HX)
    assert not client.portal.call(e.fleets)[0].get("pick_matrix")


def test_template_option_reaches_its_fleets(client):
    e = client.app.state.worker.automations
    client.post("/loadouts/phases", data={"new_phase": "Survey"}, headers=HX)
    cfg = client.portal.call(e.loadout_cfg)
    pid = cfg["phases"][-1]["id"]
    client.post("/loadouts/phases", data={f"name:{pid}": "Survey", f"pick_matrix:{pid}": "on"}, headers=HX)
    assert next(p for p in client.portal.call(e.loadout_cfg)["phases"] if p["id"] == pid)["pick_matrix"]
    client.portal.call(e.save_fleets, [{**FLEET, "pick_matrix": False, "template": pid}])
    assert client.portal.call(e.fleets)[0]["pick_matrix"]
    assert "pick_matrix:" + pid in client.get("/fleets", headers=H).text


LOGI = {"id": "logistics", "name": "Logistics", "role": "logistics", "home": "SOLARES", "station": False,
        "wants": {}, "pick_matrix": True}


def _logi_devices():
    return [
        # a heaven vessel with an empty cradle of its own
        {"device_code": "HVEMPTY1", "device_type": "heaven_vessel", "status": "idle", "location": "SOLARES-4-L4",
         "tags": ["fleet:logistics"], "features": ["surge", "cradle"], "stow_capacity": 10, "stow_used": 2},
        # one with a replicant: full
        {"device_code": "HVFULL01", "device_type": "heaven_vessel", "status": "idle", "location": "SOLARES-4-L4",
         "tags": ["fleet:logistics"], "features": ["surge", "cradle"], "stow_capacity": 10, "stow_used": 1,
         "hosting_replicant": {"name": "Pip"}},
        # a mobile fleet towing an empty matrix container
        {"device_code": "MF000001", "device_type": "mobile_fleet", "status": "idle", "location": "SOLARES-BELT-1",
         "tags": ["fleet:logistics"], "features": ["surge", "attach"]},
        {"device_code": "MC000009", "device_type": "matrix_container", "status": "idle", "location": "SOLARES-BELT-1",
         "attached_to_device_code": "MF000001", "tags": [], "features": ["cruise", "cradle"], "stow_capacity": 1, "stow_used": 0},
        # a mobile fleet whose container holds a matrix: full
        {"device_code": "MF000002", "device_type": "mobile_fleet", "status": "idle", "location": "SOLARES-BELT-1",
         "tags": ["fleet:logistics"], "features": ["surge", "attach"]},
        {"device_code": "MC000010", "device_type": "matrix_container", "status": "idle", "location": "SOLARES-BELT-1",
         "attached_to_device_code": "MF000002", "tags": [], "features": ["cruise", "cradle"], "stow_capacity": 1, "stow_used": 1,
         "stowed_devices": [{"device_code": "MX000020", "device_type": "replicant_matrix"}]},
        {"device_code": "MX000020", "device_type": "replicant_matrix", "status": "stowed", "stowed_in_device_code": "MC000010", "tags": []},
        # a surge plate with no cradle: nothing to fill
        {"device_code": "SP000001", "device_type": "surge_plate", "status": "idle", "location": "SOLARES-BELT-1",
         "tags": ["fleet:logistics"], "features": ["surge", "attach"]},
        # away on a trip: left alone
        {"device_code": "HVEMPTY2", "device_type": "heaven_vessel", "status": "travelling", "location": "SOLARES-4-L4",
         "tags": ["fleet:logistics"], "features": ["surge", "cradle"], "stow_capacity": 10, "stow_used": 0},
        {"device_code": "MX000001", "device_type": "replicant_matrix", "status": "idle", "location": "FALQUORYX-BELT-1",
         "tags": ["spare"], "in_control_range": True},
    ]


def test_logistics_empty_cradles_per_carrier():
    got = [(c["device_code"], cr["device_code"]) for c, cr in fl.empty_cradles(LOGI, _logi_devices(), set())]
    assert got == [("HVEMPTY1", "HVEMPTY1"), ("MF000001", "MC000009")]
    assert [c["device_code"] for c, _ in fl.empty_cradles(LOGI, _logi_devices(), {"HVEMPTY1"})] == ["MF000001"]
    ds = _logi_devices()
    v, cr = fl.empty_cradles(LOGI, ds, set())[1]
    steps, m, _, _ = fl.matrix_pickup(LOGI, ds, fl.spare_matrices(ds, set()), STARS, set(), 1728, vessel=v, cradle=cr)
    body = [(s["path"], s["body"]) for s in steps]
    assert ("/devices/MF000001", {"command": "travel", "destination": "FALQUORYX-BELT-1"}) in body
    assert ("/devices/MX000001", {"command": "stow", "target": "MC000009"}) in body   # into the container, not the platform


def test_logistics_pass_fills_one_cradle_per_spare(client):
    e = client.app.state.worker.automations
    client.portal.call(client.app.state.db.kv_set, "devices", _logi_devices())
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [{"designation": k, **v} for k, v in STARS.items()]})
    client.portal.call(e.save_fleets, [dict(LOGI)])
    out = client.portal.call(e.matrix_pickup_pass, True)
    assert len(out) == 1 and "HVEMPTY1 fetches matrix MX000001" in out[0]
    note = client.portal.call(client.app.state.db.kv_get, "matrix_pickups")["logistics"]["note"]
    assert "no spare replicant matrix left" in note


def test_logistics_note_says_empty_carriers_are_busy(client):
    e = client.app.state.worker.automations
    ds = [d for d in _logi_devices() if d["device_code"] != "HVEMPTY1" and not d["device_code"].startswith(("MF", "MC"))]
    client.portal.call(client.app.state.db.kv_set, "devices", ds)
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [{"designation": k, **v} for k, v in STARS.items()]})
    client.portal.call(e.save_fleets, [dict(LOGI)])
    assert client.portal.call(e.matrix_pickup_pass, True) == []
    note = client.portal.call(client.app.state.db.kv_get, "matrix_pickups")["logistics"]["note"]
    assert note.startswith("1 carrier(s) without a replicant, busy")   # HVEMPTY2 is away, not "all have a replicant"


def test_dispatch_fills_empty_cradles_before_carriers_get_work(client):
    """Joe 2026-11-21: a pilotless vessel picks up a matrix before a run strands it out of relay range."""
    e = client.app.state.worker.automations
    client.portal.call(client.app.state.db.kv_set, "devices", _logi_devices())
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [{"designation": k, **v} for k, v in STARS.items()]})
    client.portal.call(e.save_fleets, [dict(LOGI)])

    async def enable():
        s = await e.settings()
        s["rules"]["loadouts"]["enabled"] = True
        await e.save_settings(s)
    client.portal.call(enable)
    out = client.portal.call(lambda: e.dispatch(full=False))
    jobs = [j for j in client.portal.call(e.jobs) if "picks up spare replicant matrix" in j["title"]]
    assert [j["device"] for j in jobs] == ["HVEMPTY1"], out
