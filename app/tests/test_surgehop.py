import json

from test_ops import client, eng  # noqa: F401  (fixture)

SCAN = {"planets": [{"designation": "Z-1", "orbital_distance_au": 1.0}, {"designation": "Z-6", "orbital_distance_au": 400.0}],
        "asteroid_belt": {"belts": [{"designation": "Z-BELT-1", "inner_radius_au": 490, "outer_radius_au": 510}]},
        "outer_system": {"kuiper": {"designation": "Z-KUIPER", "distance_au": 505}}}


def _run(client, at="Z-1-L4", refuse_hop=False, dest="Z-BELT-1"):
    from rsweb import automations as au
    from rsweb.db import now_iso
    e, db = eng(client), client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": "Z", "entry_point": "Z-1-L4"}]})
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("Z", json.dumps(SCAN), now_iso()))
    client.portal.call(db.kv_set, "devices", [{"device_code": "V1", "device_type": "heaven_vessel", "status": "idle",
                                               "location": at, "features": ["surge", "cruise"]}])
    sent = []

    async def fake_send(method, path, body, label):
        sent.append(dict(body or {}))
        if refuse_hop and body.get("via"):
            return False, None, "Invalid destination"
        return True, {}, None
    e.send = fake_send
    job = client.portal.call(e.create_job, "fleets", "trip", "V1",
                             [au.step(f"V1 → {dest}", "/devices/V1", {"command": "travel", "destination": dest})], {}, True)
    return sent, next(j for j in client.portal.call(e.jobs) if j["id"] == job["id"])


def test_a_long_cruise_from_the_entry_point_goes_via_the_kuiper_belt(client):
    sent, job = _run(client)
    assert sent == [{"command": "travel", "destination": "Z-BELT-1", "via": ["Z-KUIPER"]}]
    assert "surge hop via Z-KUIPER" in job["steps"][0]["desc"] and len(job["steps"]) == 1


def test_a_refused_via_takes_the_games_own_route(client):
    sent, job = _run(client, refuse_hop=True)
    assert sent == [{"command": "travel", "destination": "Z-BELT-1", "via": ["Z-KUIPER"]},
                    {"command": "travel", "destination": "Z-BELT-1"}]
    assert job["steps"][0]["status"] != "failed"


def test_from_a_body_via_its_l_point_then_the_hop(client):
    sent, _ = _run(client, at="Z-6-L5")
    assert sent == [{"command": "travel", "destination": "Z-BELT-1", "via": ["Z-KUIPER"]}]
    sent, _ = _run(client, at="Z-6")
    assert sent == [{"command": "travel", "destination": "Z-BELT-1", "via": ["Z-6-L4", "Z-KUIPER"]}]


def test_leaving_from_a_belt_goes_via_the_nearest_surge_spot_not_the_entry_point(client):
    # ISOLITHOR 2026-10-22: belt → ISOBAREA took 3544 s by the entry point, 515 s via the Kuiper belt
    sent, _ = _run(client, at="Z-BELT-1", dest="Y-2-L4")
    assert sent == [{"command": "travel", "destination": "Y-2-L4", "via": ["Z-KUIPER"]}]
    sent, _ = _run(client, at="Z-1-1", dest="Y-2-L4")                                   # next to the entry point: as it is
    assert sent == [{"command": "travel", "destination": "Y-2-L4"}]


def test_arriving_takes_the_off_ramp_nearest_the_destination(client):
    # 2D6BFDF1 2026-10-22: surged to ISOLITHOR-1-L4, then cruised 499 AU to ISOLITHOR-BELT-1
    sent, job = _run(client, at="Y-2-L4", dest="Z-BELT-1")
    assert sent == [{"command": "travel", "destination": "Z-BELT-1", "via": ["Z-KUIPER"]}]
    sent, _ = _run(client, at="Y-2-L4", dest="Z-1")                                     # next to the entry point: as it is
    assert sent == [{"command": "travel", "destination": "Z-1"}]


def test_trip_via_plans_a_hand_sent_trip_quietly(client):
    _run(client, at="Y-2-L4")   # sets up system Z and V1
    e = eng(client)
    log0 = len(client.portal.call(client.app.state.db.kv_get, "automation_log", []) or [])
    assert client.portal.call(e.trip_via, "V1", "Z-BELT-1") == ["Z-KUIPER"]
    assert len(client.portal.call(client.app.state.db.kv_get, "automation_log", []) or []) == log0


def test_replicant_route_preview_takes_the_planned_waypoints_and_shows_the_saving(client):
    # Joe 2026-10-24: FALQUORYX → ISOLITHOR-BELT-1 previewed as the game's own route (no hub, no Kuiper hop)
    e, api = eng(client), client.app.state.api
    sent = []

    async def fake_via(device, dest):
        return ["Z-KUIPER"]

    async def fake_request(method, path, json_body=None, **kw):
        sent.append((path, dict(json_body or {})))
        secs = 700.0 if (json_body or {}).get("via") else 1000.0
        return {"status": "preview" if (json_body or {}).get("dry_run") else "travelling", "total_time_seconds": secs,
                "route": [{"leg": 1, "from": "Y-2-L4", "to": "Z-BELT-1", "type": "surge", "time_seconds": secs}]}
    e.trip_via, api.request = fake_via, fake_request
    HX = {"X-Auth-Request-Email": "joe@example.com", "HX-Request": "true"}
    r = client.post("/replicants/77F75255/travel", data={"destination": "Z-BELT-1", "dry_run": "1"}, headers=HX)
    assert "Re-routed via Z-KUIPER" in r.text and "saves <strong>5 min</strong> (30 %)" in r.text
    assert 'name="via" value="Z-KUIPER"' in r.text
    assert sent[-1][1] == {"destination": "Z-BELT-1", "via": ["Z-KUIPER"], "dry_run": True}
    client.post("/replicants/77F75255/travel", data={"destination": "Z-BELT-1", "via": "Z-KUIPER"}, headers=HX)
    assert sent[-1] == ("/replicants/77F75255/travel", {"destination": "Z-BELT-1", "via": ["Z-KUIPER"]})
    client.post("/replicants/77F75255/travel", data={"destination": "Z-BELT-1", "via": ""}, headers=HX)
    assert sent[-1] == ("/replicants/77F75255/travel", {"destination": "Z-BELT-1"})
