from rsweb import logistics as lg
from test_ops import H, client, eng  # noqa: F401  (fixture)

# SOLARES and a star 2000 ly off (≈28 h at 50 s/ly), and one 100 ly off
POS = {"SOLARES": {"x": 0, "y": 0, "z": 0}, "FAR": {"x": 2000, "y": 0, "z": 0}, "NEAR": {"x": 100, "y": 0, "z": 0}}


def _run(client, dest, force=False, catalog=True):
    from rsweb import automations as au
    e = eng(client)
    db = client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": k, "position": v} for k, v in POS.items()
                                                      if catalog or k == "SOLARES"]})
    client.portal.call(db.kv_set, "devices", [{"device_code": "F1", "device_type": "cargo_freighter", "status": "idle",
                                                "location": "SOLARES-4-L4", "features": ["surge"]}])
    sent = []

    async def fake_request(method, path, json_body=None, background=False):   # the game's preview
        return {"status": "preview", "total_time_seconds": 241 * 3600}

    async def fake_send(method, path, body, label):
        sent.append(dict(body or {}))
        return True, {}, None
    e.api.request, e.send = fake_request, fake_send
    job = client.portal.call(e.create_job, "logistics", "trip", "F1",
                             [au.step("F1 → there", "/devices/F1", {"command": "travel", "destination": dest},
                                      wait=["travel.arrived"])], {}, force)
    return e, sent, job["id"]


def _job(client, e, jid):
    return next(j for j in client.portal.call(e.jobs) if j["id"] == jid)


def test_a_trip_beyond_the_sector_range_waits_for_confirm(client):
    e, sent, jid = _run(client, "FAR-BELT-1")
    job = _job(client, e, jid)
    assert not sent and job["status"] == "waiting" and job["steps"][0]["status"] == "confirm"
    assert "sector range" in job["steps"][0]["hold"]
    client.portal.call(e.tick)   # the tick leaves it alone: no timeout
    assert not sent and _job(client, e, jid)["steps"][0]["status"] == "confirm"
    assert client.portal.call(e.confirm_trip, jid)
    assert sent and sent[0]["destination"] == "FAR-BELT-1"


def test_trips_inside_the_sector_range_and_your_own_go_out(client):
    e, sent, _ = _run(client, "NEAR-BELT-1")
    assert sent and sent[0]["destination"] == "NEAR-BELT-1"
    e, sent, _ = _run(client, "FAR-BELT-1", force=True)
    assert sent


def test_a_star_the_catalog_lacks_is_measured_by_the_preview(client):
    e, sent, jid = _run(client, "FAR-BELT-1", catalog=False)   # ISOLITHOR on 2026-10-22: 241 h
    job = _job(client, e, jid)
    assert not sent and job["steps"][0]["sector_h"] == 241.0


def test_sector_range_in_light_years():
    assert lg.SECTOR_RANGE_LY == 1728.0


def test_a_planned_route_under_the_range_goes_out(client):
    # Joe 2026-10-24: every travel estimate uses the planned route (surge hops, a hub, the off-ramp)
    from rsweb import automations as au
    e = eng(client)
    _run(client, "NEAR-BELT-1")
    seen = []

    async def fake_via(device, dest):
        return ["HUB", "FAR", "FAR-KUIPER"]

    async def fake_request(method, path, json_body=None, background=False):
        seen.append(dict(json_body or {}))
        return {"status": "preview", "total_time_seconds": (20 if (json_body or {}).get("via") else 30) * 3600}
    sent = []

    async def fake_send(method, path, body, label):
        sent.append(dict(body or {}))
        return True, {}, None
    e.trip_via, e.api.request, e.send = fake_via, fake_request, fake_send
    job = client.portal.call(e.create_job, "logistics", "trip", "F1",
                             [au.step("F1 → there", "/devices/F1", {"command": "travel", "destination": "FAR-BELT-1"},
                                      wait=["travel.arrived"])], {}, False)
    assert seen[0]["via"] == ["HUB", "FAR", "FAR-KUIPER"] and _job(client, e, job["id"])["steps"][0]["sector_h"] == 20.0
    assert sent and sent[0]["destination"] == "FAR-BELT-1"
