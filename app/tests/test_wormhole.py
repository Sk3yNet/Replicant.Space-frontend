import math

from rsweb import hubroute, wormhole as wh
from test_ops import H, client, eng  # noqa: F401  (fixture)

POS = {"DELTA": {"x": 10257, "y": 26911, "z": 174}, "ISOLITHOR": {"x": 10300, "y": 26900, "z": 170},
       "TWAFFY": {"x": 160, "y": -187, "z": -120}, "SOLARES": {"x": 300, "y": -100, "z": 0},
       "NEARDELTA": {"x": 10400, "y": 26950, "z": 170}}


def test_gate_is_one_way_and_only_for_long_trips():
    assert wh.gate_for("ISOLITHOR", "SOLARES", POS) == ("DELTA", "DELTA-OBJ-2", "TWAFFY")
    assert wh.gate_for("DELTA", "SOLARES", POS)
    assert wh.gate_for("SOLARES", "ISOLITHOR", POS) is None      # no way back
    assert wh.gate_for("ISOLITHOR", "NEARDELTA", POS) is None    # inside the Delta sector
    assert wh.gate_for("ISOLITHOR", "UNCHARTED", POS) is None
    assert wh.ly("ISOLITHOR", "SOLARES", POS) < 300 < wh.ly("SOLARES", "ISOLITHOR", POS)


def test_old_sector_refusals_on_gate_legs_are_stale():
    assert wh.stale_far("ISOLITHOR>SOLARES", {"at": "2026-11-16T10:00:00+00:00", "h": 400}, POS)
    assert not wh.stale_far("ISOLITHOR>SOLARES", {"at": "2026-11-17T10:00:00+00:00", "h": 400}, POS)
    assert not wh.stale_far("SOLARES>ISOLITHOR", {"at": "2026-11-16T10:00:00+00:00", "h": 400}, POS)


def _run(client, gate_works=True, start="ISOLITHOR-4-L4", dest="SOLARES-2-L4", hand=True):
    from rsweb import automations as au
    e = eng(client)
    db = client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": k, "position": v} for k, v in POS.items()]})
    client.portal.call(db.kv_set, "devices", [{"device_code": "V1", "status": "idle", "location": start}])
    sent = []

    async def fake_request(method, path, json_body=None, background=False):   # 50 s/ly; the gate jumps to TWAFFY
        from rsweb.api import ApiError
        dest = json_body["destination"]
        stops = [start.split("-")[0]] + list(json_body.get("via") or [])
        stops += [dest.split("-")[0]] if hubroute.star_after(stops[-1]) != dest.split("-")[0] else []
        secs, prev = 30.0, stops[0]   # a final cruise
        for s in stops[1:] + ([dest] if dest != stops[-1] else []):
            star = s.split("-")[0]
            if s == star and prev == star and gate_works:   # live 2026-11-18: naming the far star after the gate
                raise ApiError(400, f"Loop detected: already at {star}")
            if star in POS:
                secs += math.dist(tuple(POS[prev].values()), tuple(POS[star].values())) * 50
                prev = star
            if s == "DELTA-OBJ-2" and gate_works:
                prev = "TWAFFY"
        return {"status": "preview", "total_time_seconds": secs}

    async def fake_send(method, path, body, label):
        sent.append(dict(body or {}))
        return True, {}, None
    e.api.request, e.send = fake_request, fake_send
    job = client.portal.call(e.create_job, "fleets", "trip", "V1",
                             [au.step("V1 → SOLARES", "/devices/V1", {"command": "travel", "destination": dest})],
                             {"by": "you"} if hand else {}, hand)
    return sent, next(j for j in client.portal.call(e.jobs) if j["id"] == job["id"])


def test_a_trip_out_of_delta_goes_through_the_gate(client):
    sent, job = _run(client)
    assert sent == [{"command": "travel", "destination": "SOLARES-2-L4", "via": ["DELTA", "DELTA-OBJ-2", "SOLARES"]}]
    assert "DELTA gate" in job["steps"][0]["desc"] and job["steps"][0]["status"] != "failed"


def test_automated_trips_never_take_the_one_way_gate(client):
    """Owner 2026-11-19: a new season rescan sent Delta Logistics' 6AA05D44 through the gate to CENIX, ~10 days from
    home. Only a trip a person orders takes it; the long way round is beyond the sector range and isn't sent."""
    sent, job = _run(client, hand=False)
    assert not sent and job["status"] == "failed" and "sector range" in job["steps"][0]["error"]


def test_no_gate_when_the_game_does_not_take_it(client):
    sent, job = _run(client, gate_works=False)
    assert all("DELTA-OBJ-2" not in (b.get("via") or []) for b in sent)


def test_a_trip_into_twaffy_never_names_twaffy_after_the_gate(client):
    """Live 2026-11-18: via [DELTA-OBJ-2, TWAFFY] was refused "Loop detected: already at TWAFFY" (the wormhole lands
    there), so 86C7569E got no gate route and was sent via the LLYRHAWR hub, bouncing out of TWAFFY and back."""
    for i, start in enumerate(("DELTA-4-L4", "DELTA-OBJ-2")):
        sent, job = _run(client, start=start, dest="TWAFFY-OBJ-1")
        assert sent[-1] == {"command": "travel", "destination": "TWAFFY-OBJ-1", "via": ["DELTA-OBJ-2"]}, (start, sent)
    assert hubroute.waypoints(["DELTA-OBJ-2", "TWAFFY-KUIPER"], "TWAFFY-1") == ["DELTA-OBJ-2", "TWAFFY-KUIPER"]
    assert hubroute.stars_between("DELTA-4-L4", ["DELTA-OBJ-2", "TWAFFY-KUIPER"]) == ["DELTA-OBJ-2", "TWAFFY-KUIPER"]
