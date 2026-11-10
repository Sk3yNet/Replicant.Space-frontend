"""Game v4.0.0: an FTL relay switches on only at an L4/L5 point (live 2026-11-09: 18 relays dropped at Oort clouds and
Kuiper belts by two relay runs were refused and left dark out of reach)."""
from rsweb import loadouts as lo
from test_ops import add, client, eng as _eng  # noqa: F401


async def _noop(*a, **k):
    return None


def _run(entry=None):
    by = {c: {"device_code": c, "device_type": "ftl_relay", "location": None, "stowed_in_device_code": "HV",
              "tags": [f"to:{t.lower()}"]} for c, t in (("R1", "AAA"), ("R2", "BBB"))}
    by["HV"] = {"device_code": "HV", "device_type": "heaven_vessel", "location": "HUB-1"}
    dl = {"carrier": "HV", "carrier_loc": "HUB-1", "from": "HUB", "to": "AAA", "devices": ["R1", "R2"], "replicant": "REP",
          "mode": "stow", "dest_of": {"R2": "BBB"}, "stops": ["BBB"], "stay": True}
    stars = {"AAA": {"entry_point": entry}} if entry else {}
    return lo.delivery_steps(dl, by, stars, False)


def test_a_relay_run_flies_to_an_l4_l5_point_before_each_drop():
    steps = _run()
    entry = [s for s in steps if s.get("to_entry")]
    assert [s["to_entry"] for s in entry] == ["AAA", "BBB"]
    descs = [s["desc"] for s in steps]
    assert descs.index("HV → AAA (deliver 1 to AAA)") < steps.index(entry[0]) < descs.index("deploy R1 in AAA")
    act = next(s for s in steps if s["desc"] == "activate R1")
    assert act["critical"]   # refused: the job stops with the replicant still there, never flies off leaving it dark
    # the catalog knows AAA's entry point (an L4): no extra trip there
    assert [s["to_entry"] for s in _run("AAA-2-L4") if s.get("to_entry")] == ["BBB"]


def test_to_entry_resolves_the_spot_from_the_catalog(client):  # noqa: F811
    add(client, device_code="HV000001", device_type="heaven_vessel", location="AAA-OORT", status="idle", tags=[])
    e = _eng(client)
    client.portal.call(e.db.kv_set, "stars", {"stars": [{"designation": "AAA", "entry_point": "AAA-3-L5"}]})
    e._update = _noop
    st = {"desc": "x", "path": "/devices/HV000001", "method": "POST", "to_entry": "AAA", "wait_device": "HV000001",
          "body": {"command": "travel", "destination": "AAA"}, "match": {"destination": "AAA"}}
    job = {"idx": 0, "id": "j", "steps": [st], "meta": {}}
    assert client.portal.call(e._to_entry, st, job) is None
    assert st["body"]["destination"] == "AAA-3-L5" and st["match"] == {"destination": "AAA-3-L5"}
    assert job["meta"]["entries"] == {"AAA": "AAA-3-L5"}
    st2 = {**st, "to_entry": "ZZZ", "body": {"command": "travel", "destination": "ZZZ"}}
    assert "no L4/L5 point known in ZZZ" in client.portal.call(e._to_entry, st2, job)


def test_a_relay_deploy_waits_for_its_carrier_to_reach_an_l4_l5_point(client):  # noqa: F811
    """Jobs planned before the rule: the runner flies the carrier to an L4/L5 point before the deploy."""
    add(client, device_code="HV000001", device_type="heaven_vessel", location="AAA-OORT", status="idle", tags=[])
    add(client, device_code="RL000001", device_type="ftl_relay", location=None, status="stowed",
        stowed_in_device_code="HV000001", tags=[])
    e = _eng(client)
    e._update = _noop
    trip = {"desc": "HV → AAA", "path": "/devices/HV000001", "method": "POST", "status": "done", "wait_device": "HV000001",
            "body": {"command": "travel", "destination": "AAA"}}
    dep = {"desc": "deploy RL", "path": "/devices/RL000001", "method": "POST", "status": "pending", "tries": 1,
           "body": {"command": "deploy"}}
    act = {"desc": "activate RL", "path": "/devices/RL000001", "method": "POST", "status": "pending",
           "body": {"command": "activate"}}
    job = {"idx": 1, "id": "j", "steps": [trip, dep, act], "meta": {}}
    assert client.portal.call(e._relay_drop_spot, dep, job) is True
    assert job["steps"][1]["to_entry"] == "AAA" and job["steps"][2] is dep
    assert client.portal.call(e._relay_drop_spot, dep, job) is False   # checked once


def test_dark_relays_get_a_wake_trip(client):  # noqa: F811
    e = _eng(client)
    devices = [
        {"device_code": "DR1", "device_type": "ftl_relay", "location": "AAA", "status": "out_of_range", "tags": []},
        {"device_code": "DR2", "device_type": "ftl_relay", "location": "BBB", "status": "out_of_range", "tags": []},
        {"device_code": "LIT", "device_type": "ftl_relay", "location": "CCC", "status": "out_of_range", "tags": []},
        {"device_code": "ON", "device_type": "ftl_relay", "location": "CCC-1-L4", "status": "relaying", "tags": []},
        {"device_code": "HV", "device_type": "heaven_vessel", "location": "HUB-1", "status": "idle",
         "features": ["surge"], "available_commands": ["travel"], "stow_capacity": 10, "stow_used": 0,
         "hosting_replicant": {"replicant_code": "REP"}, "tags": ["fleet:logistics"]}]
    made = []

    async def devs():
        return devices

    async def jobs():
        return []

    async def fleets():
        return [{"id": "logistics", "role": "logistics"}]

    async def create_job(rule, title, device, steps, meta=None, force=False):
        made.append((title, steps, meta))
        return {"id": "j"}
    e.devices, e.jobs, e.fleets, e.create_job, e.log = devs, jobs, fleets, create_job, _noop
    client.portal.call(e.db.kv_set, "stars", {"stars": [{"designation": s, "position": {"x": x, "y": 0, "z": 0}}
                                                        for s, x in (("HUB", 0), ("AAA", 5), ("BBB", 9), ("CCC", 2))]})
    client.portal.call(e.dark_relay_pass)
    assert len(made) == 1
    title, steps, meta = made[0]
    assert title == "wake dark relays: HV → AAA → BBB" and meta["wake"] and meta["devices"] == ["DR1", "DR2"]
    descs = [s["desc"] for s in steps]
    assert "stow DR1 in HV" in descs and "activate DR2" in descs and descs[-1] == "HV → HUB-1 (back)"
    assert [s["to_entry"] for s in steps if s.get("to_entry")] == ["AAA", "BBB"]
    assert [s["meet"] for s in steps if s.get("meet")] == ["DR1", "DR1", "DR2", "DR2"]
