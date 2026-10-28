import json

from test_ops import client, eng  # noqa: F401  (fixture)

SCAN = {"planets": [{"designation": "Z-1", "orbital_distance_au": 1.0}, {"designation": "Z-6", "orbital_distance_au": 400.0}],
        "asteroid_belt": {"belts": [{"designation": "Z-BELT-1", "inner_radius_au": 490, "outer_radius_au": 510}]},
        "outer_system": {"kuiper": {"designation": "Z-KUIPER", "distance_au": 505}}}


def _run(client, at="Z-1-L4", refuse_hop=False, dest="Z-BELT-1", hub=False, scanned=True):
    from rsweb import automations as au
    from rsweb.db import now_iso
    e, db = eng(client), client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": "Z", "entry_point": "Z-1-L4"}]})
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("Z", json.dumps(SCAN), now_iso()))
    radii = client.portal.call(eng(client).cruise_radii)   # the scan gives the belt and Kuiper radii
    if not scanned:   # the radii stay known (as from the star catalog), but no scan of ours found the entry point
        e.cruise_radii = lambda: _radii(radii)
        client.portal.call(db.execute, "DELETE FROM systems WHERE star=?", ("Z",))
    client.portal.call(db.kv_set, "devices", [{"device_code": "V1", "device_type": "heaven_vessel", "status": "idle",
                                               "location": at, "features": ["surge", "cruise"]}]
                       + ([{"device_code": "H1", "device_type": "system_hub", "status": "relaying", "location": "Z-1-L4"}]
                          if hub else []))
    sent = []

    async def fake_send(method, path, body, label):
        sent.append(dict(body or {}))
        if refuse_hop and body.get("via"):
            return False, None, refuse_hop if isinstance(refuse_hop, str) else "Invalid destination"
        return True, {}, None
    e.send = fake_send
    job = client.portal.call(e.create_job, "fleets", "trip", "V1",
                             [au.step(f"V1 → {dest}", "/devices/V1", {"command": "travel", "destination": dest})], {}, True)
    return sent, next(j for j in client.portal.call(e.jobs) if j["id"] == job["id"])


def _radii(r):
    async def f():
        return r
    return f()


def _arrive(client, at):
    e = eng(client)
    devs = client.portal.call(client.app.state.db.kv_get, "devices", [])
    client.portal.call(client.app.state.db.kv_set, "devices", [{**devs[0], "location": at}])
    client.portal.call(e.on_event, {"event": "travel.arrived", "device_code": "V1", "payload": {"destination": at}})
    return next(j for j in client.portal.call(e.jobs))


def test_a_long_cruise_from_the_entry_point_goes_via_the_kuiper_belt(client):
    sent, job = _run(client)
    assert sent == [{"command": "travel", "destination": "Z-BELT-1", "via": ["Z-KUIPER"]}]
    assert "surge hop via Z-KUIPER" in job["steps"][0]["desc"] and len(job["steps"]) == 1


def test_a_via_refused_as_a_loop_hops_to_the_spot_first(client):
    # live 2026-10-27: ISOLITHOR-1-L4 → ISOLITHOR-BELT-1 via ISOLITHOR-KUIPER: "Loop detected: already at ISOLITHOR"
    sent, job = _run(client, refuse_hop="Loop detected: already at Z")
    assert sent == [{"command": "travel", "destination": "Z-BELT-1", "via": ["Z-KUIPER"]},
                    {"command": "travel", "destination": "Z-KUIPER"}]
    assert "surge hop" in job["steps"][0]["desc"] and len(job["steps"]) == 2
    job = _arrive(client, "Z-KUIPER")
    assert sent[2:] == [{"command": "travel", "destination": "Z-BELT-1"}]
    assert [s["status"] for s in job["steps"]] == ["done", "done"]


def test_a_refused_via_takes_the_games_own_route(client):
    sent, job = _run(client, refuse_hop=True)
    assert sent == [{"command": "travel", "destination": "Z-BELT-1", "via": ["Z-KUIPER"]},
                    {"command": "travel", "destination": "Z-BELT-1"}]
    assert job["steps"][0]["status"] != "failed"


def test_an_l_point_other_than_the_entry_point_is_no_surge_spot(client):
    # game docs (Joe 2026-10-25): only the Oort cloud, the Kuiper belt and the entry point; an L4 / L5 cruises
    sent, _ = _run(client, at="Z-6-L5")
    assert sent == [{"command": "travel", "destination": "Z-BELT-1"}]
    sent, _ = _run(client, at="Z-6")
    assert sent == [{"command": "travel", "destination": "Z-BELT-1"}]


def test_system_page_lists_the_surge_spots(client):
    _run(client)
    r = client.get("/systems/Z", headers={"X-Auth-Request-Email": "joe@example.com"})
    assert "Surge spots:" in r.text and "Z-KUIPER" in r.text, r.text[:500]


def test_leaving_from_a_belt_goes_via_the_nearest_surge_spot_not_the_entry_point(client):
    # ISOLITHOR 2026-10-22: belt → ISOBAREA took 3544 s by the entry point, 515 s via the Kuiper belt
    sent, _ = _run(client, at="Z-BELT-1", dest="Y-2-L4")
    # the destination's star last (live 2026-10-26: "Cannot travel directly from ISOLITHOR-KUIPER to KHAZENEN-BELT-1")
    assert sent == [{"command": "travel", "destination": "Y-2-L4", "via": ["Z-KUIPER", "Y"]}]
    sent, _ = _run(client, at="Z-BELT-1", dest="Y")                                    # to a star: nothing more
    assert sent == [{"command": "travel", "destination": "Y", "via": ["Z-KUIPER"]}]
    sent, _ = _run(client, at="Z-1-1", dest="Y-2-L4")                                   # next to the entry point: as it is
    assert sent == [{"command": "travel", "destination": "Y-2-L4"}]


def test_arriving_takes_the_off_ramp_nearest_the_destination(client):
    # 2D6BFDF1 2026-10-22: surged to ISOLITHOR-1-L4, then cruised 499 AU to ISOLITHOR-BELT-1
    sent, job = _run(client, at="Y-2-L4", dest="Z-BELT-1", scanned=False)
    assert sent == [{"command": "travel", "destination": "Z-BELT-1", "via": ["Z", "Z-KUIPER"]}]   # the star first (live)
    sent, _ = _run(client, at="Y-2-L4", dest="Z-1")                                     # next to the entry point: as it is
    assert sent == [{"command": "travel", "destination": "Z-1"}]


def test_trip_via_plans_a_hand_sent_trip_quietly(client):
    _run(client, at="Y-2-L4", scanned=False)   # sets up system Z and V1
    e = eng(client)
    log0 = len(client.portal.call(client.app.state.db.kv_get, "automation_log", []) or [])
    assert client.portal.call(e.trip_via, "V1", "Z-BELT-1") == ["Z", "Z-KUIPER"]
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


def test_a_spot_in_another_system_gets_its_star_first():
    # Joe 2026-10-24: "Cannot travel directly from CAELZAROR to ISOLITHOR-KUIPER (different systems)"
    from rsweb.hubroute import stars_between
    assert stars_between("FALQUORYX-1-L4", ["CAELZAROR", "ISOLITHOR-KUIPER"]) == ["CAELZAROR", "ISOLITHOR", "ISOLITHOR-KUIPER"]
    assert stars_between("FALQUORYX-1-L4", ["CAELZAROR", "ISOLITHOR", "ISOLITHOR-KUIPER"]) == ["CAELZAROR", "ISOLITHOR",
                                                                                               "ISOLITHOR-KUIPER"]
    assert stars_between("Z-BELT-1", ["Z-KUIPER"]) == ["Z-KUIPER"]


def test_device_travel_form_takes_the_planned_route_and_shows_the_saving(client):
    e, api = eng(client), client.app.state.api
    sent = []

    async def fake_via(device, dest):
        return ["Z", "Z-KUIPER"]

    async def fake_request(method, path, json_body=None, **kw):
        sent.append(dict(json_body or {}))
        secs = 700.0 if (json_body or {}).get("via") else 1000.0
        return {"status": "preview", "total_time_seconds": secs}
    e.trip_via, api.request = fake_via, fake_request
    HX = {"X-Auth-Request-Email": "joe@example.com", "HX-Request": "true"}
    r = client.post("/devices/2AC61212/command", data={"command": "travel", "f.destination": "Z-BELT-1", "f.dry_run": "on"},
                    headers=HX)
    assert "Re-routed via Z, Z-KUIPER: saves 5 min (30 %)" in r.text, r.text[:2000]
    assert sent[0]["via"] == ["Z", "Z-KUIPER"] and "via" not in sent[1]


def test_arriving_in_a_hub_system_lands_at_the_entry_point_then_hops_to_the_off_ramp(client):
    # live 2026-10-28: F7E30727 ANLOREN → ISOLITHOR-BELT-1 via ISOLITHOR-KUIPER landed at ISOLITHOR-1-L4 (a hub there)
    # and cruised 527 AU to the Kuiper belt, 49 min
    sent, job = _run(client, at="Y-2-L4", dest="Z-BELT-1", hub=True, scanned=False)
    assert sent == [{"command": "travel", "destination": "Z-1-L4"}]
    assert [s["body"]["destination"] for s in job["steps"]] == ["Z-1-L4", "Z-KUIPER", "Z-BELT-1"]
    job = _arrive(client, "Z-1-L4")
    assert sent[1:] == [{"command": "travel", "destination": "Z-KUIPER"}]
    job = _arrive(client, "Z-KUIPER")
    assert sent[2:] == [{"command": "travel", "destination": "Z-BELT-1"}]
    assert client.portal.call(eng(client).trip_via, "V1", "Z-BELT-1") == []      # one hand-sent trip: no off-ramp


def test_a_scanned_system_lands_at_its_entry_point_too(client):
    sent, job = _run(client, at="Y-2-L4", dest="Z-BELT-1")
    assert sent == [{"command": "travel", "destination": "Z-1-L4"}] and len(job["steps"]) == 3


def test_a_wait_lasts_until_the_current_legs_eta_when_the_game_underprices_it(client):
    from datetime import timedelta
    from rsweb import automations as au
    e, db = eng(client), client.app.state.db
    now = au._now()
    tr = {"final_arrives_at": (now - timedelta(minutes=40)).isoformat(), "arrives_at": (now + timedelta(minutes=10)).isoformat()}
    client.portal.call(db.kv_set, "devices", [{"device_code": "V1", "device_type": "surge_plate", "status": "travelling",
                                               "location": "Z-1-L4", "travel": tr}])
    st = {"wait_device": "V1", "started_at": (now - timedelta(hours=1)).isoformat(), "timeout": 3600, "desc": "trip"}
    job = client.portal.call(e.create_job, "fleets", "trip", "V1", [au.step("x", "", None, method="WAIT")], {}, True)
    assert client.portal.call(e._still_travelling, st, job, now)


def test_a_surge_arrival_naming_the_star_matches_the_spot_it_landed_at():
    # live 2026-10-28: travel.arrived {destination: BELNULEON}, location BELNULEON-10-L4
    from rsweb.automations import event_matches
    st = {"wait": ["travel.arrived"], "match": {"destination": "BELNULEON-10-L4"}}
    ev = {"event": "travel.arrived", "device_code": "X", "location": "BELNULEON-10-L4",
          "payload": {"destination": "BELNULEON", "travel_type": "surge"}}
    assert event_matches(st, ev, "X")
    assert not event_matches(st, {**ev, "location": "BELNULEON-OORT"}, "X")
