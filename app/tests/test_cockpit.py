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


def test_events_reach_the_page_while_a_tick_holds_the_engine_lock(client):  # noqa: F811
    """Live 2026-10-29: an 80 s tick held the lock and the stream waited in automations.on_event, so the rounds of a
    swoop run arrived after it ended. Now the event is announced at once and the automations get it after the tick."""
    w = client.app.state.worker
    seen, handled = [], []
    q = client.app.state.hub.subscribe()
    orig = w.automations.on_event

    async def spy(ev, late=False):
        await orig(ev, late=late)
        handled.append(ev["id"])
    w.automations.on_event = spy

    async def run():
        import asyncio
        held, release = asyncio.Event(), asyncio.Event()

        async def tick():   # another task, like the engine's tick
            async with w.automations.lock:
                held.set()
                await release.wait()
        t = asyncio.create_task(tick())
        await held.wait()
        for n in (1, 2):
            await asyncio.wait_for(w.handle_event({"id": f"sw{n}", "event": "swoop.round", "category": "swoop",
                                                   "created_at": "2026-10-29T16:36:34+00:00",
                                                   "payload": {"round": n, "debris_status": "flanking"}}), 2)
        while not q.empty():
            kind, data = q.get_nowait()
            if kind == "event":
                seen.append(data["id"])
        assert handled == []
        release.set()
        await t
        await asyncio.wait_for(w._auto_task, 2)
    client.portal.call(run)
    assert seen == ["sw1", "sw2"] and handled == ["sw1", "sw2"]


def test_a_swoop_run_holds_the_engine_and_map_until_it_ends(client):  # noqa: F811
    """Live 2026-10-29 23:09: ticks of 18-47 s and map redraws of 21 s stalled the loop and round 4 came too late."""
    eng = client.app.state.worker.automations
    calls = []
    import rsweb.web as web
    orig = web.map_payload

    async def counting(request, part):
        calls.append(part)
        return {"n": len(calls)}
    web.map_payload = counting
    try:
        assert client.get("/api/map.json", headers=H).json() == {"n": 1}
        r = client.post(f"/cockpit/{HOST}/swoop", json={}, headers=HX).json()
        assert r["ok"] and eng.focused()
        assert client.get("/api/map.json", headers=H).json() == {"n": 1}   # the last one, not a redraw
        client.portal.call(client.app.state.worker.handle_event,
                           {"id": "done1", "event": "swoop.complete", "category": "swoop", "payload": {"score": 1}})
        assert not eng.focused()
        assert client.get("/api/map.json", headers=H).json() == {"n": 2}
    finally:
        web.map_payload = orig


def test_cockpit_engine_status_hold_and_release(client):  # noqa: F811
    eng = client.app.state.worker.automations
    assert client.get("/cockpit-engine", headers=H).json()["state"] == "idle"
    r = client.post("/cockpit-engine/hold", json={"seconds": 300}, headers=HX).json()
    assert r["state"] == "held" and 295 <= r["held_s"] <= 300 and eng.focused()
    import time
    eng.tick_t0, eng.tick_last_s, eng.stage = time.monotonic() - 5, 20.0, "stock_pass"
    r = client.get("/cockpit-engine", headers=H).json()
    assert r["state"] == "busy" and r["stage"] == "stock_pass" and 14 <= r["left_s"] <= 15
    eng.tick_t0, eng.stage = None, None
    assert client.post("/cockpit-engine/hold", json={"seconds": 0}, headers=HX).json()["state"] == "idle"
    assert not eng.focused()
    assert client.post("/cockpit-engine/hold", json={"seconds": 99999}, headers=HX).json()["held_s"] <= 900


def test_event_rules_wait_while_a_swoop_run_is_on(client):  # noqa: F811
    """Live 2026-10-29 23:28: a site.depleted event ran the salvage rule mid-run and stalled the server for 56 s."""
    w = client.app.state.worker
    handled = []
    orig = w.automations.on_event

    async def spy(ev, late=False):
        await orig(ev, late=late)
        handled.append(ev["id"])
    w.automations.on_event = spy

    async def run():
        import asyncio
        w.automations.focus(60)
        await w.handle_event({"id": "dep1", "event": "site.depleted", "category": "site", "payload": {}})
        await asyncio.sleep(1.5)
        assert handled == []
        await w.handle_event({"id": "end1", "event": "swoop.complete", "category": "swoop", "payload": {}})
        await asyncio.wait_for(w._auto_task, 5)
    client.portal.call(run)
    assert handled == ["dep1", "end1"]
