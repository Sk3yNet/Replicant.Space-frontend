"""Devices › Cockpit: the swoop debris run flown by hand."""
import json

from rsweb import web_cockpit as wc

from test_ops import H, HX, client  # noqa: F401  (fixture)

HOST = "11ADA230"   # the mock's heaven vessel, which lists swoop


def test_device_page_and_command_form_link_to_the_cockpit(client):  # noqa: F811
    assert f'href="/cockpit/{HOST}"' in client.get(f"/devices/{HOST}", headers=H).text
    form = client.get(f"/devices/{HOST}/command-form?command=swoop", headers=HX).text
    assert f'href="/cockpit/{HOST}"' in form and "boost" in form
    pick = client.get("/cockpit", headers=H).text
    assert f"/cockpit/{HOST}" in pick


def test_swoop_start_and_action_reach_the_game_and_are_logged(client):  # noqa: F811
    page = client.get(f"/cockpit/{HOST}", headers=H).text
    assert "Start swoop" in page and '"actions"' in page and "Flight" not in page
    r = client.post(f"/cockpit/{HOST}/swoop", json={}, headers=HX).json()
    assert r["ok"] and r["response"]["status"] == "swoop_starting" and isinstance(r["ms"], int)
    r = client.post(f"/cockpit/{HOST}/swoop", json={"action": "dive"}, headers=HX).json()
    assert r["ok"] and r["action"] == "dive" and r["response"]["status"] == "warmup" and r["learned"] is None
    rows = client.portal.call(client.app.state.db.fetchall, "SELECT body FROM actions ORDER BY id")
    bodies = [json.loads(x["body"]) for x in rows[-2:]]
    assert bodies == [{"command": "swoop"}, {"command": "swoop", "action": "dive"}]
    bad = client.post(f"/cockpit/{HOST}/swoop", json={"action": "jump"}, headers=HX)
    assert bad.status_code == 400


def test_graded_moves_are_tallied_by_the_round_debris_status(client):  # noqa: F811
    """Live 2026-10-29: rounds name only the replicant; the first move of a round answers {correct, round}."""
    db = client.app.state.db
    for n, (at, status) in enumerate([("2026-10-29T12:11:56-04:00", "flanking"), ("2026-10-29T12:12:05-04:00", "flanking"),
                                      ("2026-10-29T12:12:15-04:00", "clear")], 1):
        client.portal.call(db.insert_event, {"id": f"r{n}", "event": "swoop.round", "category": "swoop",
                                             "replicant_code": "8EC42FBA", "created_at": at,
                                             "payload": {"round": n, "debris_status": status, "duration_seconds": 9}})
    moves = [("2026-10-29T16:11:56+00:00", "dive", {"correct": False, "round": 1, "status": "active"}),
             ("2026-10-29T16:11:58+00:00", "roll", {"round": 1, "status": "active", "waiting": True}),
             ("2026-10-29T16:12:07+00:00", "dodge", {"correct": False, "round": 2, "status": "active"}),
             ("2026-10-29T16:12:19+00:00", "boost", {"correct": True, "round": 3, "status": "active"})]
    for at, act, resp in moves:
        client.portal.call(db.execute, "INSERT INTO actions(at, user, method, path, body, status, response) VALUES(?,?,?,?,?,?,?)",
                           (at, "you", "POST", "/devices/5DF3161B", json.dumps({"command": "swoop", "action": act}), 200,
                            json.dumps(resp)))
    got = client.portal.call(wc.learned, db)
    assert got == {"flanking": {"dive": {"right": 0, "wrong": 1}, "dodge": {"right": 0, "wrong": 1}},
                   "clear": {"boost": {"right": 1, "wrong": 0}}}
    ev = wc.slim({"event": "swoop.round", "category": "swoop", "device_code": None, "replicant_code": "8EC42FBA",
                  "payload": "{}"}, "5DF3161B", "8EC42FBA")
    assert ev["mine"]
    assert not wc.slim({"event": "mining.stopped", "category": "mining", "device_code": "X", "replicant_code": "8EC42FBA",
                        "payload": {}}, "5DF3161B", "8EC42FBA")["mine"]


def test_events_about_the_vessel_are_marked_mine():
    ev = {"event": "swoop.debris", "device_code": "OTHER", "payload": json.dumps({"target": HOST, "direction": "left"})}
    out = wc.slim(ev, HOST)
    assert out["mine"] and out["payload"]["direction"] == "left"
    assert not wc.slim({"event": "x", "device_code": "OTHER", "payload": {}}, HOST)["mine"]


def test_scanned_moons_are_offered_as_destinations(client):  # noqa: F811
    from rsweb.targets import system_targets
    db = client.app.state.db
    scan = {"planets": [{"designation": "ETHIREL-2", "type": "gas_giant", "moon_count": 4}, {"designation": "ETHIREL-3"}]}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("ETHIREL", json.dumps(scan), "2026-10-29T00:00:00+00:00"))
    form = client.get(f"/devices/{HOST}/command-form?command=travel", headers=HX).text
    assert 'value="ETHIREL-2-4"' in form and 'value="ETHIREL-2-5"' not in form and "ETHIREL-3-1" not in form
    t = client.portal.call(system_targets, db, "ETHIREL")
    assert [x["code"] for x in t["targets"] if x["category"] == "moon"] == [f"ETHIREL-2-{n}" for n in range(1, 5)]
