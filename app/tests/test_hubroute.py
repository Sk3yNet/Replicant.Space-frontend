import math

from rsweb import hubroute as hr
from test_ops import H, client, eng  # noqa: F401  (fixture)

POS = {"A": {"x": 0, "y": 0, "z": 0}, "HUB": {"x": 100, "y": 0, "z": 0}, "B": {"x": 110, "y": 0, "z": 0},
       "OFF": {"x": 0, "y": 100, "z": 0}}


def test_candidates_only_hubs_that_can_shorten_the_trip():
    assert hr.candidates("A", "B", {"HUB", "OFF"}, POS) == ["HUB"]
    assert hr.candidates("A", "B", {"OFF"}, POS) == []
    assert hr.candidates("A", "HUB", {"HUB"}, POS) == []      # already ends at a hub
    assert hr.candidates("HUB", "B", {"HUB"}, POS) == []      # starting at one gains nothing
    assert hr.worth_it(5500, 4200) and not hr.worth_it(5500, 5300) and not hr.worth_it(None, 10)


def _run(client, refuse_via=False):
    from rsweb import automations as au
    e = eng(client)
    db = client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": k, "position": v} for k, v in POS.items()]})
    client.portal.call(db.kv_set, "devices", [{"device_code": "V1", "status": "idle", "location": "A-OORT"},
                                               {"device_code": "H1", "device_type": "system_hub", "status": "active",
                                                "location": "HUB-4-L4"}])
    sent = []

    async def fake_request(method, path, json_body=None, background=False):   # the game's preview: 50 s/ly, 25 % into a hub
        stops = ["A"] + list(json_body.get("via") or []) + [json_body["destination"].split("-")[0]]
        secs = sum(math.dist(*(tuple(POS[s].values()) for s in (a, b))) * 50 * (.75 if b == "HUB" else 1)
                   for a, b in zip(stops, stops[1:]))
        return {"status": "preview", "total_time_seconds": secs}

    async def fake_send(method, path, body, label):
        sent.append(dict(body or {}))
        if refuse_via and "via" in body:
            return False, None, "Invalid waypoint"
        return True, {}, None
    e.api.request, e.send = fake_request, fake_send
    job = client.portal.call(e.create_job, "fleets", "trip", "V1",
                             [au.step("V1 → B", "/devices/V1", {"command": "travel", "destination": "B-3-L4"})], {}, True)
    return sent, next(j for j in client.portal.call(e.jobs) if j["id"] == job["id"])


def test_a_long_trip_goes_via_our_hub_when_faster(client):
    sent, job = _run(client)
    assert sent == [{"command": "travel", "destination": "B-3-L4", "via": ["HUB"]}]
    assert "via hub HUB" in job["steps"][0]["desc"]


def test_a_refused_waypoint_falls_back_to_the_direct_route(client):
    sent, job = _run(client, refuse_via=True)
    assert sent[-1] == {"command": "travel", "destination": "B-3-L4"} and job["steps"][0]["status"] != "failed"
