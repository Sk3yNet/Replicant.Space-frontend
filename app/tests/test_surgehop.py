import json

from test_ops import client, eng  # noqa: F401  (fixture)

SCAN = {"planets": [{"designation": "Z-1", "orbital_distance_au": 1.0}, {"designation": "Z-6", "orbital_distance_au": 400.0}],
        "asteroid_belt": {"belts": [{"designation": "Z-BELT-1", "inner_radius_au": 490, "outer_radius_au": 510}]},
        "outer_system": {"kuiper": {"designation": "Z-KUIPER", "distance_au": 505}}}


def _run(client, at="Z-1-L4", refuse_hop=False):
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
        if refuse_hop and body.get("destination") == "Z-KUIPER":
            return False, None, "Invalid destination"
        return True, {}, None
    e.send = fake_send
    job = client.portal.call(e.create_job, "fleets", "trip", "V1",
                             [au.step("V1 → Z-BELT-1", "/devices/V1", {"command": "travel", "destination": "Z-BELT-1"})], {}, True)
    return sent, next(j for j in client.portal.call(e.jobs) if j["id"] == job["id"])


def test_a_long_cruise_from_the_entry_point_hops_to_the_kuiper_belt_first(client):
    sent, job = _run(client)
    assert sent == [{"command": "travel", "destination": "Z-KUIPER"}]                   # the hop, then wait for it
    assert "surge hop" in job["steps"][0]["desc"] and job["steps"][1]["method"] == "WAIT"
    assert job["steps"][2]["body"] == {"command": "travel", "destination": "Z-BELT-1"}


def test_a_refused_hop_cruises_as_planned(client):
    sent, job = _run(client, refuse_hop=True)
    assert sent == [{"command": "travel", "destination": "Z-KUIPER"}, {"command": "travel", "destination": "Z-BELT-1"}]
    assert job["steps"][1]["status"] == "skipped"


def test_hops_from_any_l_point_but_not_from_a_body(client):
    sent, _ = _run(client, at="Z-6-L5")                                                 # ≈100 AU: the Kuiper hop saves 95
    assert sent[0] == {"command": "travel", "destination": "Z-KUIPER"}
    sent, _ = _run(client, at="Z-6")                                                    # not a surge spot: no hop
    assert sent == [{"command": "travel", "destination": "Z-BELT-1"}]
