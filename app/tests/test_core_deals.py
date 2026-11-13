"""Core tests: contracts, location events, trades and the trail (split out of test_core.py by test name)."""
from corelib import *  # noqa: F401,F403


def test_later_units_of_a_deal_refused_skip_quietly(client):
    """Live 2026-10-28: a trade approved ×10 had 5 of 50 propulsors on site; one unit went through and three refusals
    stopped the job with four alerts. The other units are skipped at once and the job carries on."""
    eng = client.app.state.worker.automations
    world = client.app.state.api.http._transport.app.state.world
    from rsweb.automations import step
    world.trades["TC000009"] = [{"trade_code": "T9", "current_stock": 1}]
    steps = []
    for i in range(5):
        st = step("execute trade X ×5 at TC000009", "/devices/TC000009/trades/T9", None, critical=i == 0)
        st["extra_unit"] = i > 0
        steps.append(st)
    steps.append(step("then", "/devices/TC000009/trades/T9", None))
    job = client.portal.call(eng.create_job, "logistics", "deal", None, steps, {}, True)
    job = next(j for j in client.portal.call(eng.jobs) if j["id"] == job["id"])
    assert job["status"] == "done"
    assert [x["status"] for x in job["steps"][:5]] == ["done"] + ["skipped"] * 4
    log = [x for x in client.portal.call(client.app.state.db.kv_get, "automation_log", []) if x["text"].startswith("deal")]
    assert not any(x["level"] == "alert" and "execute" in x["text"] for x in log) and any("bought 1 of 5" in x["text"] for x in log)


def test_contracts_tracker_progress_and_actions(client):
    from rsweb import gameevents as gev
    w = client.app.state.worker
    client.portal.call(w.sync_devices)
    client.portal.call(w.sync_inventory)
    client.portal.call(w.sync_account)
    disc = {"category": "resource_trade", "criteria": [{"devices": [], "name": "default", "resources": {"carbon": 100, "structural": 350}}],
            "description": "Orbital stations are degrading.", "designation": "SOL-3-L4-EVT-001", "event_type": "atmospheric_harvest",
            "location": "SOL-3-L4", "rewards": {"civilisation_points": 1, "resources": {"volatiles": 200}, "xp": 500},
            "tier": 1, "title": "Atmospheric Harvest"}
    client.portal.call(w.handle_event, {"id": "4444444444444-0", "event": "event.discovered", "category": "event",
                                        "location": "SOL-3-L4", "star": "SOL", "payload": disc, "created_at": "2026-10-01T10:00:00+00:00"})
    evs = client.portal.call(gev.load, client.app.state.db)
    e = evs["SOL-3-L4-EVT-001"]
    assert e["status"] == "open" and e["title"] == "Atmospheric Harvest"
    page = client.get("/game-events", headers=H).text
    assert "Atmospheric Harvest" in page and "Contracts" in page
    assert "✗ no beacon</a>" in page                               # the mock's only beacon is stowed
    world = client.app.state.api.http._transport.app.state.world
    fb = next(d for d in world.devices if d["device_code"] == "FB000001")
    keep = dict(fb)
    fb.update(status="idle", location="SOL-3")
    client.portal.call(client.app.state.db.kv_set, "devices", world.devices)
    assert "beacon only in system</a>" in client.get("/game-events", headers=H).text
    fb.update(location="SOL-3-L4")
    client.portal.call(client.app.state.db.kv_set, "devices", world.devices)
    assert "✓ beacon here</a>" in client.get("/game-events", headers=H).text
    fb.clear(); fb.update(keep)
    client.portal.call(client.app.state.db.kv_set, "devices", world.devices)
    # SOL-3-L4 has 400 structural and no carbon; SOL-BELT-1 has 180 carbon → deliver carbon from the belt
    ctx = client.portal.call(__import__("rsweb.web", fromlist=["x"]).game_events_ctx, type("R", (), {"app": client.app})())
    ev = ctx["open"][0]
    rows = {x["resource"]: x for x in ev["prog"]["best"]["resources"]}
    assert rows["structural"]["short_here"] == 0 and rows["carbon"]["short_here"] == 100 and rows["carbon"]["short_system"] == 0
    assert ev["prog"]["state"] == "deliver"
    assert ev["plan"]["legs"] == [{"collect": "SOL-BELT-1", "deliver": "SOL-3-L4", "requirement": {"carbon": 100}}]
    r = client.post("/game-events/SOL-3-L4-EVT-001/deliver", headers=HX)
    assert "TC000001" in r.text
    job = [j for j in client.portal.call(w.automations.jobs) if j["rule"] == "chain"][-1]
    assert job["steps"][0]["body"]["configuration"] == {"route": {"collect": "SOL-BELT-1", "deliver": "SOL-3-L4"}, "requirement": {"carbon": 100}}
    # fulfill uses the game's call by default
    assert "/locations/SOL-3-L4/events/SOL-3-L4-EVT-001" in client.post("/game-events/SOL-3-L4-EVT-001/fulfil", headers=HX).text
    client.post("/game-events/settings", data={"fulfil": 'POST /replicants/{replicant}/events/{designation} {"criteria": "{criteria}"}'}, headers=HX)
    r = client.post("/game-events/SOL-3-L4-EVT-001/fulfil", data={"replicant": "77F75255"}, headers=HX)
    assert "/replicants/77F75255/events/SOL-3-L4-EVT-001" in r.text
    # completion closes it and records what it used
    client.portal.call(w.handle_event, {"id": "4444444444445-0", "event": "event.completed", "category": "event", "location": "SOL-3-L4",
                                        "payload": {"designation": "SOL-3-L4-EVT-001", "consumed": {"resources": {"carbon": 100, "structural": 350}},
                                                    "rewards": {"xp": 500, "resources": {"volatiles": 200}}, "tier": 1},
                                        "created_at": "2026-10-01T11:00:00+00:00"})
    page = client.get("/game-events", headers=H).text
    assert "No open events" in page and "used 100 carbon" in page



def test_contracts_rule_fulfils_when_ready(client):
    w = client.app.state.worker
    eng = _enable(client, "contracts")
    client.portal.call(w.sync_inventory)
    disc = {"criteria": [{"devices": [], "name": "default", "resources": {"structural": 350}}], "designation": "SOL-BELT-1-EVT-001",
            "location": "SOL-BELT-1", "title": "Belt Need", "tier": 1, "rewards": {"xp": 10}}
    client.portal.call(w.handle_event, {"id": "4444444444450-0", "event": "event.discovered", "category": "event",
                                        "location": "SOL-BELT-1", "payload": disc, "created_at": "2026-10-01T10:00:00+00:00"})
    client.portal.call(client.app.state.db.kv_set, "replicants", {"77F75255": {"name": "bob-1", "location": "SOL-BELT-1"}})
    assert client.portal.call(eng.rule_contracts, True) == []                  # not approved yet: left to you
    page = client.get("/game-events", headers=H).text
    assert "Pre-approve every contract from <b>the inhabitants of SOL-BELT-1</b>" in page
    client.post("/game-events/approve", data={"key": "body:SOL-BELT-1", "on": "1"}, headers=HX)
    client.portal.call(client.app.state.db.kv_set, "contracts_state", {})
    done = client.portal.call(eng.rule_contracts, True)
    assert done == ["fulfill SOL-BELT-1-EVT-001"]
    act = client.portal.call(client.app.state.db.fetchone, "SELECT method, path FROM actions ORDER BY id DESC LIMIT 1")
    assert (act["method"], act["path"]) == ("POST", "/locations/SOL-BELT-1/events/SOL-BELT-1-EVT-001")
    assert client.portal.call(eng.rule_contracts, True) == []   # not retried straight away


def test_late_events_are_kept_quiet_and_summarised(tmp_path):
    """A replay after downtime (seen live: ~44 h of events after the 1.12.2 deadlock) raises no notifications
    and fires no arrival / salvage rules; the first live event after it posts one catch-up note."""
    async def go():
        db = DB(str(tmp_path / "t.sqlite"))
        await db.open()
        s = Settings(api_token="t", api_base="http://x/v1", db_path=str(tmp_path / "t.sqlite"))
        w = Worker(s, db, RSClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))), Hub())
        fired = []

        async def arrival(ev):
            fired.append("arrival")

        async def salvage():
            fired.append("salvage")
            return []
        w.automations.on_arrival, w.automations.rule_salvage = arrival, salvage
        old = datetime.now(timezone.utc) - timedelta(hours=44)
        for i in range(5):
            await w.handle_event(_ev(i, "site.depleted", created=old + timedelta(minutes=i), site=f"SOL-BELT-1-SITE-{i}"))
        await w.handle_event(_ev(9, "travel.arrived", created=old, destination="SOL-4", origin="ALPHA-1"))
        assert (await db.fetchone("SELECT COUNT(*) n FROM events"))["n"] == 6        # still in the feed
        assert await db.fetchall("SELECT * FROM notifications") == []
        assert fired == []
        await w.handle_event(_ev(20, "site.depleted", site="SOL-BELT-1-SITE-20"))     # live again
        titles = [r["title"] for r in await db.fetchall("SELECT title FROM notifications ORDER BY id")]
        # site depletions are expected: feed and digest only, no notification (1.37)
        assert len(titles) == 1 and "caught up 6 late event(s) from the last 44 h" in titles[0]
        assert fired == ["salvage"]
        await w.handle_event(_ev(21, "site.depleted", site="SOL-BELT-1-SITE-21"))     # no second note
        assert (await db.fetchone("SELECT COUNT(*) n FROM notifications"))["n"] == 1
        await db.close()

    run(go())


def test_device_scan_on_arrival_finds_shops(client):
    from rsweb import shop
    world = client.app.state.api.http._transport.app.state.world
    world.foreign_devices = [{"device_code": "TC000001", "device_type": "ami_trade_controller", "location": "SOL-OORT",
                              "owner_replicant_code": "DA5H0001", "owner_name": "Dash"},
                             {"device_code": "BB000001", "device_type": "ftl_beacon", "location": "SOL-3",
                              "owner_replicant_code": "DA5H0001", "owner_name": "Dash"}]
    world.trades["TC000001"] = [{"trade_id": "t1", "offer": {"resources": {"carbon": 10}}}]
    client.portal.call(client.app.state.worker.sync_devices)
    arrival = {"id": "7777777777778-0", "event": "travel.arrived", "category": "travel", "device_code": "11ADA230",
               "device_type": "heaven_vessel", "location": "SOL-BELT-1", "star": "SOL",
               "payload": {"destination": "SOL-BELT-1", "origin": "ABOTEIN-OORT", "travel_type": "surge"},
               "created_at": iso(datetime.now(timezone.utc))}
    client.portal.call(client.app.state.worker.handle_event, arrival)
    db = client.app.state.db
    snap = client.portal.call(db.kv_get, "others")["stars"]["SOL"]
    assert [d["code"] for d in snap["devices"]] == ["TC000001", "BB000001"]
    known = client.portal.call(shop.known_traders, db)
    assert known["TC000001"]["star"] == "SOL" and known["TC000001"]["owner_name"] == "Dash"
    assert known["TC000001"]["trades"][0]["trade_id"] == "t1"
    log = client.portal.call(db.kv_get, "automation_log")
    assert any(e["rule"] == "devices_on_arrival" and "shops: TC000001" in e["text"] for e in log)


def test_trail_page_end_to_end(client):
    from rsweb import trail as tl
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_account)
    r = client.post("/trail/target", data={"name": "bill"}, headers=HX)
    s = client.portal.call(client.app.state.db.kv_get, tl.KV)
    assert s["target_code"] == "B1LL0001" and s["target_name"] == "Bill"       # the exact name wins over Billy-2
    world.foreign_devices = [{"device_code": "BB000001", "device_type": "ftl_beacon", "location": "SOL-3",
                              "owner_replicant_code": "B1LL0001", "owner_name": "Bill"},
                             {"device_code": "XX000001", "device_type": "ftl_beacon", "location": "SOL-4",
                              "owner_replicant_code": "30B93F2F", "owner_name": "Sylphrena"}]
    world.audit["BB000001"] = [{"id": 1, "device_code": "BV1", "device_type": "heaven_vessel", "replicant_code": "B1LL0001",
                                "travel_type": "departure", "location": "SOL-3", "logged_at": "2026-10-08T02:00:00+00:00",
                                "vector": "1,0,0"}]
    rep = next(iter(client.portal.call(client.app.state.db.kv_get, "replicants")))
    r = client.post("/trail/scan", data={"replicant": rep}, headers=HX)
    assert "BB000001" in r.text and "XX000001" not in r.text
    r = client.post("/trail/read", headers=HX)
    assert "1 entry, 0 new" in r.text                                            # a first read fills the log
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [
        {"designation": "SOL", "position": {"x": 0, "y": 0, "z": 0}}, {"designation": "EAST", "position": {"x": 5, "y": 0.1, "z": 0}},
        {"designation": "WEST", "position": {"x": -5, "y": 0, "z": 0}}]})
    page = client.get("/trail", headers=H).text
    assert "heading for <b>EAST</b>" in page and "Trail" in page and "B1LL0001" in page
    # the 15-minute pass reads the beacons again and notifies on a new move
    eng = client.app.state.worker.automations
    world.audit["BB000001"].append({"id": 2, "device_code": "BV1", "device_type": "heaven_vessel", "replicant_code": "B1LL0001",
                                    "travel_type": "arrival", "location": "SOL-3", "logged_at": "2026-10-08T04:00:00+00:00",
                                    "vector": None})
    out = client.portal.call(eng.trail_pass)
    assert out == ["Bill: arrival at SOL-3"]
    assert client.portal.call(eng.trail_pass) == []                              # not again within 15 minutes
    # the page keeps what was entered, and a candidate's travel button sends the replicant that scanned
    client.post("/trail/beacon", data={"code": "bb000001", "star": "sol"}, headers=HX)
    page = client.get("/trail", headers=H).text
    assert f'value="{rep}" selected' in page and 'value="BB000001"' in page and 'value="SOL"' in page
    assert '"/trail/travel"' in page and '{"star": "EAST"}' in page
    r = client.post("/trail/travel", data={"star": "EAST"}, headers=HX)
    assert f"/replicants/{rep}/travel" in r.text and "EAST" in r.text
    # travel time for the replicant that scanned: the game's estimate, cached
    assert "/trail/eta?star=EAST" in page
    assert ">?<" in client.get("/trail/eta?star=EAST", headers=HX).text            # the game doesn't know EAST
    r = client.get("/trail/eta?star=LERNA", headers=HX)
    assert "≈" in r.text and "ly from" in r.text
    eta = client.portal.call(client.app.state.db.kv_get, "trail_eta")
    assert any(k.endswith("|LERNA") for k in eta)


def test_review_bad_event_payload_doesnt_break_processing(tmp_path):
    async def go():
        db = DB(str(tmp_path / "t.sqlite"))
        await db.open()
        s = Settings(api_token="t", api_base="http://x/v1", db_path=str(tmp_path / "t.sqlite"))
        w = Worker(s, db, RSClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))), Hub())
        bad = _ev(1, "travel.departed", destination="SOL-4", travel_time_seconds="n/a", arrives_at=12345)
        await w.handle_event(bad)                                   # must not raise
        await w.handle_event({**_ev(2, "hub.warning"), "payload": ["odd"]})
        assert await db.kv_get("event_cursor") == _ev(2, "x")["id"]
        assert notify.describe({"event": "hub.warning", "payload": ["odd"]})
    asyncio.run(go())


def test_trail_follow_to_the_end(client):
    from rsweb import trail as tl
    world = client.app.state.api.http._transport.app.state.world
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.post("/trail/target", data={"name": "bill"}, headers=HX)
    rep = next(iter(client.portal.call(db.kv_get, "replicants")))
    stars = {"SOL": (0, 0, 0), "EAST": (5, 0, 0), "EAST2": (5, 1.5, 0), "FAR": (10, 0, 0)}
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": k, "position": dict(zip("xyz", v))} for k, v in stars.items()]})
    bill = {"owner_replicant_code": "B1LL0001", "owner_name": "Bill", "device_type": "ftl_beacon"}
    world.foreign_devices = [{**bill, "device_code": "BB000001", "location": "SOL-3"}]
    dep = lambda i, loc, v, t: {"id": i, "device_code": "BV1", "device_type": "heaven_vessel", "replicant_code": "B1LL0001",  # noqa: E731
                                "travel_type": "departure", "location": loc, "logged_at": t, "vector": v}
    arr = lambda i, loc, t: {"id": i, "device_code": "BV1", "device_type": "heaven_vessel", "replicant_code": "B1LL0001",  # noqa: E731
                             "travel_type": "arrival", "location": loc, "logged_at": t, "vector": None}
    world.audit["BB000001"] = [dep(1, "SOL-3", "1,0,0", "2026-10-08T01:00:00+00:00")]
    client.post("/trail/scan", data={"replicant": rep}, headers=HX)
    client.post("/trail/read", headers=HX)
    page = client.get("/trail", headers=H).text
    assert "follow Bill with" in page
    r = client.post("/trail/follow", headers=HX)
    assert "first stop EAST" in r.text

    def arrive():   # the trip: the job is done and the replicant is there
        jobs = client.portal.call(eng.jobs)
        fo = client.portal.call(db.kv_get, tl.KV)["follow"]
        for j in jobs:
            if j["id"] == fo["job"]:
                j["status"] = "done"
        client.portal.call(db.kv_set, "automation_jobs", jobs)
    client.portal.call(eng.trail_follow_pass)
    fo = client.portal.call(db.kv_get, tl.KV)["follow"]
    job = next(j for j in client.portal.call(eng.jobs) if j["id"] == fo["job"])
    assert job["steps"][0]["path"] == f"/replicants/{rep}/travel" and job["steps"][0]["body"] == {"destination": "EAST"}
    # EAST has a beacon of Bill's: he arrived and left again, for FAR
    world.foreign_devices.append({**bill, "device_code": "BB000002", "location": "EAST-2"})
    world.audit["BB000002"] = [arr(2, "EAST-2", "2026-10-08T02:00:00+00:00"), dep(3, "EAST-2", "1,0,0", "2026-10-08T03:00:00+00:00")]
    arrive()
    client.portal.call(eng.trail_follow_pass)       # reached EAST
    client.portal.call(eng.trail_follow_pass)       # read EAST → on to FAR
    fo = client.portal.call(db.kv_get, tl.KV)["follow"]
    assert fo["active"] and fo["state"] == "travel" and fo["star"] == "FAR" and fo["visited"] == ["EAST"]
    client.portal.call(eng.trail_follow_pass)
    world.foreign_devices.append({**bill, "device_code": "BB000003", "location": "FAR-1"})
    world.audit["BB000003"] = [arr(4, "FAR-1", "2026-10-08T04:00:00+00:00")]
    arrive()
    client.portal.call(eng.trail_follow_pass)
    client.portal.call(eng.trail_follow_pass)
    fo = client.portal.call(db.kv_get, tl.KV)["follow"]
    assert not fo["active"] and "found Bill" in fo["result"] and "FAR-1" in fo["result"] and fo["visited"] == ["EAST", "FAR"]
    notes = client.portal.call(db.fetchall, "SELECT level, title FROM notifications WHERE link='/trail'")
    assert any(n["level"] == "mention" and "found Bill" in n["title"] for n in notes)
    assert "found Bill" in client.get("/trail", headers=H).text


def test_contract_already_completed_counts_as_done(client):
    from rsweb import gameevents as gev
    from rsweb.automations import step
    eng, db, w = client.app.state.worker.automations, client.app.state.db, client.app.state.worker
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(w.handle_event, {**_ev(995, "event.discovered", designation="KELMORNEA-3-EVT-003", location="KELMORNEA-3",
                                                title="Electronics Shortage"), "location": "KELMORNEA-3"})
    world.events_done = {"KELMORNEA-3-EVT-003"}          # done already (an earlier try, or by hand)
    st = step("fulfill contract Electronics Shortage at KELMORNEA-3", "/locations/KELMORNEA-3/events/KELMORNEA-3-EVT-003",
              None, critical=True)
    job = client.portal.call(eng.create_job, "fleets", "Trader_1: trade", None, [st], {"devices": [], "fleet": "trader-1"})
    job = next(j for j in client.portal.call(eng.jobs) if j["id"] == job["id"])
    assert job["status"] == "done" and "already completed" in job["steps"][0]["note"]
    assert client.portal.call(gev.load, db)["KELMORNEA-3-EVT-003"]["status"] == "completed"   # not offered again
    notes = client.portal.call(db.fetchall, "SELECT title FROM notifications WHERE title LIKE '%Electronics%'")
    assert not any("failed" in n["title"] for n in notes)


def test_contract_run_accepts_any_option_and_waits_after_criteria_not_met(client):
    """Live 2026-10-08: Famine Assistance at AEMEROTH-2 — 3 orbital farms OR a nutrient synthesizer and resources. The
    run set out with one option's resources; the farms were delivered instead; the run had stalled on 'Event criteria
    not met' and never tried again."""
    eng, db, w = client.app.state.worker.automations, client.app.state.db, client.app.state.worker
    client.portal.call(w.handle_event, {**_ev(997, "event.discovered", designation="AEM-2-EVT-005", location="AEM-2",
                                                title="Famine Assistance", criteria=[
                                                    {"name": "orbital_farms", "resources": {}, "devices": [{"device_type": "orbital_farm", "quantity": 2}]},
                                                    {"name": "nutrient_synthesis", "resources": {"carbon": 50},
                                                     "devices": [{"device_type": "nutrient_synthesizer", "quantity": 1}]}]),
                                        "location": "AEM-2"})
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": f"OF{i}", "device_type": "orbital_farm", "location": "AEM-2", "status": "idle", "tags": []} for i in (1, 2)]
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-BELT-1", "items": [{"resource_type": "carbon", "quantity": 900}]}])
    m = {"status": "running", "phase": "wait", "idx": 0, "targets": ["AEM"], "log": [], "opts": {},
         "contract": {"designation": "AEM-2-EVT-005", "location": "AEM-2", "title": "Famine Assistance", "price": {"carbon": 50}}}
    f = {"id": "t", "name": "T", "role": "trade", "home": "SOL", "wants": {}, "mission": m}
    state, why = client.portal.call(eng._deal_gate, f, m, devices)
    assert state != "rewind" and "short" not in why          # the farms option is complete: no carbon needed there
    # a fulfill the game refuses with 'criteria not met' goes back to waiting, not stalled
    jobs = client.portal.call(eng.jobs)
    jobs.append({"id": "jt", "rule": "fleets", "title": "T: trade", "device": None, "status": "failed", "idx": 0, "meta": {"fleet": "t"},
                 "steps": [{"desc": "fulfil", "status": "failed", "error": "Event criteria not met"}]})
    client.portal.call(eng.save_jobs, jobs)
    m.update({"phase": "trade", "job": "jt"})
    client.portal.call(eng.save_fleets, [f])
    client.portal.call(eng.run_fleets)
    m2 = client.portal.call(eng.fleets)[0]["mission"]
    assert m2["status"] == "running" and m2["criteria_misses"] == 1
    assert any("criteria aren't met yet" in str(x) for x in m2["log"])


def test_contracts_page_lists_quiet_civilizations_for_approval(client):
    db = client.app.state.db
    scan = {"planets": [{"designation": "VETH-2", "life_stage": "intelligent", "species": "Veth"},
                        {"designation": "VETH-3", "life_stage": "microbial"}]}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("VETH", json.dumps(scan), "2026-10-09"))
    page = client.get("/game-events", headers=H).text
    assert "Civilizations not asking for anything" in page and "VETH-2" in page and "VETH-3" not in page
    client.post("/game-events/approve", data={"key": "body:VETH-2", "on": "1"}, headers=HX)
    assert "body:VETH-2" in client.portal.call(db.kv_get, "contract_approvals")
    # Joe 2026-10-31: approving moved the row and Firefox restored ticks by position onto other civilizations
    page = client.get("/game-events", headers=H).text
    box = page[page.index("VETH-2"):]
    assert 'autocomplete="off"' in box[:box.index("approved</label>")]
    # approving saves in place: no reload at all
    r = client.post("/game-events/approve", data={"key": "body:VETH-2", "on": ""}, headers=HX)
    assert "HX-Refresh" not in r.headers
    for path in ("/game-events", "/automations", "/fleets"):   # every full page: no box or select left to the browser's restore
        import re
        tags = re.findall(r'<(?:input[^>]*type="(?:checkbox|radio)"|select\b)[^>]*>', client.get(path, headers=H).text)
        assert tags and all('autocomplete="off"' in t for t in tags), path


def test_contract_option_picked_on_the_contracts_page(client):
    from rsweb import gameevents as gev
    eng, db, w = client.app.state.worker.automations, client.app.state.db, client.app.state.worker
    client.portal.call(w.handle_event, {**_ev(998, "event.discovered", designation="AEM-2-EVT-006", location="AEM-2",
                                                title="Famine Assistance", criteria=[
                                                    {"name": "orbital_farms", "resources": {}, "devices": [{"device_type": "orbital_farm", "quantity": 2}]},
                                                    {"name": "nutrient_synthesis", "resources": {"carbon": 50}, "devices": []}]),
                                        "location": "AEM-2"})
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": f"OF{i}", "device_type": "orbital_farm", "location": "AEM-2", "status": "idle", "tags": []} for i in (1, 2)]
    client.portal.call(db.kv_set, "devices", devices)
    e = client.portal.call(gev.load, db)["AEM-2-EVT-006"]
    assert gev.progress(e, {}, devices, {})["best"]["name"] == "orbital_farms"          # auto: the ready one
    r = client.post("/game-events/AEM-2-EVT-006/option", data={"option": "nutrient_synthesis"}, headers=HX)
    assert r.status_code == 200
    e = client.portal.call(gev.load, db)["AEM-2-EVT-006"]
    prog = gev.progress(e, {}, devices, {})
    assert prog["best"]["name"] == "nutrient_synthesis" and prog["state"] == "gather"   # picked: farms no longer count
    m = {"contract": {"designation": "AEM-2-EVT-006"}, "deal": {"designation": "AEM-2-EVT-006"}}
    assert not client.portal.call(eng._contract_option_ready, m, {}, devices)
    page = client.get("/game-events", headers=H).text
    assert "Fulfill with" in page and 'value="nutrient_synthesis" selected' in page
    client.post("/game-events/AEM-2-EVT-006/option", data={"option": ""}, headers=HX)
    assert gev.progress(client.portal.call(gev.load, db)["AEM-2-EVT-006"], {}, devices, {})["best"]["name"] == "orbital_farms"


def test_trail_sightings_newest_wins_and_his_trade_controller_ends_the_follow(client):
    from rsweb import trail as tl
    s = tl.normalize({"target_name": "Bill", "target_code": "B1LL0001"})
    bill = {"owner_replicant_code": "B1LL0001", "owner_name": "Bill"}
    tl.record_sightings(s, [{**bill, "device_code": "RV1", "device_type": "racing_vessel", "location": "ALG-OORT"},
                            {**bill, "device_code": "BB9", "device_type": "ftl_beacon", "location": "ALG-2"},
                            {**bill, "device_code": "X", "device_type": "ftl_relay", "location": "ELSE-1"}],
                        at="2026-10-13T10:00:00+00:00", star="ALG")
    assert set(s["sightings"]) == {"RV1"}                       # beacons are the trail, not a sighting; other systems ignored
    s["audit"] = {"B": [{"replicant_code": "B1LL0001", "travel_type": "arrival", "location": "OLD-1",
                         "logged_at": "2026-10-12T10:00:00+00:00"}]}
    s["beacons"] = {"B": {"star": "OLD"}}
    assert tl.latest_sighting(s)["star"] == "ALG"               # the newer sighting beats the older beacon log
    # following: arriving where his trade controller is ends the trail and lists his shop there
    world = client.app.state.api.http._transport.app.state.world
    eng, db = client.app.state.worker.automations, client.app.state.db
    rep, r = next(iter(client.portal.call(db.kv_get, "replicants").items()))
    here = tl.star_of(r.get("location") or r.get("current_location"))
    world.foreign_devices = [{**bill, "device_code": "3BF0ED09", "device_type": "ami_trade_controller", "location": f"{here}-OORT"}]
    client.portal.call(db.kv_set, tl.KV, {"target_name": "Bill", "target_code": "B1LL0001", "beacons": {}, "audit": {},
                                          "follow": {"active": True, "replicant": rep, "state": "arrived", "star": here,
                                                     "visited": [], "alts": [], "log": []}})
    client.portal.call(eng.trail_follow_pass)
    fo = client.portal.call(db.kv_get, tl.KV)["follow"]
    assert not fo["active"] and "shop" in fo["result"] and "3BF0ED09" in fo["result"]
    assert client.portal.call(db.kv_get, "traders_cache")["3BF0ED09"]["location"] == f"{here}-OORT"


def test_devices_paid_away_in_a_deal_are_not_lost(client):
    import json
    from rsweb import lost
    db = client.app.state.db
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    client.portal.call(db.execute, "INSERT INTO events(id, seq, event, device_code, location, payload, created_at, received_at) VALUES(?,?,?,?,?,?,?,?)",
                       ("ev-99901", 99901, "trade.completed", "REP1", "NOSOXA-OORT",
                        json.dumps({"role": "buyer", "criteria_paid": {"devices": {"ftl_relay": 1}}}), now, now))
    missing = {"R1": ("NOSOXA-OORT", "ftl_relay"), "R2": ("NOSOXA-OORT", "ftl_relay"), "F1": ("NOSOXA-OORT", "cargo_freighter"),
               "R3": ("SOL-3", "ftl_relay")}
    got = client.portal.call(lost.given_away, db, missing)
    assert got == {"R1"}                        # one relay paid there: one taken; the freighter that left isn't


def test_a_reused_designation_is_not_closed_by_the_former_rocks_event(client):
    """Live 2026-10-22: FALQUORYX-OBJ-2 impacted on 10-13; a new rock with the same name came on 10-21 and the old
    impact event closed it again, so it was neither read nor evacuated (a propulsor was lost)."""
    db, eng = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.insert_event, {"id": "r1", "event": "system.object_detected", "location": "FAL-OBJ-2",
                                          "created_at": "2026-10-12T10:00:00+00:00", "payload": {"impact_target": "FAL-5"}})
    client.portal.call(db.insert_event, {"id": "r2", "event": "system.object_impacted", "location": "FAL-OBJ-2",
                                          "created_at": "2026-10-13T12:27:00+00:00", "payload": {"impact_target": "FAL-5"}})
    assert client.portal.call(eng.defence_objects)["FAL-OBJ-2"]["closed"] == "impacted"
    client.portal.call(db.insert_event, {"id": "r3", "event": "system.object_detected", "location": "FAL-OBJ-2",
                                          "created_at": "2026-10-21T11:28:00+00:00",
                                          "payload": {"impact_target": "FAL-5", "impact_eta": "2099-10-22T10:28:00Z"}})
    o = client.portal.call(eng.defence_objects)["FAL-OBJ-2"]
    assert not o.get("closed") and o["impact_eta"] == "2099-10-22T10:28:00Z"
    client.portal.call(db.kv_set, "objects", {"FAL-OBJ-2": {**o, "closed": "impacted",   # saved closed by 1.208.0
                                                            "closed_at": "2026-10-13T12:27:00+00:00"}})
    assert not client.portal.call(eng.defence_objects)["FAL-OBJ-2"].get("closed")


def test_an_asteroid_event_is_over_once_its_impact_time_passes(client):
    """Owner 2026-10-15: no alerts with a negative time; a concluded event isn't tracked any more. The warning's
    destination shows in the Defense card's title."""
    db, eng = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.insert_event, {"id": "p1", "event": "system.impact_warning", "location": "KELM-4-L4",
                                          "created_at": "2026-10-15T10:00:00+00:00",
                                          "payload": {"destination": "KELM-4", "impact_eta": "2026-10-15T11:00:00Z"}})
    client.portal.call(db.insert_event, {"id": "p2", "event": "system.impact_warning", "location": "KELM-OBJ-2",
                                          "created_at": "2026-10-15T10:00:00+00:00",
                                          "payload": {"destination": "KELM-5", "impact_eta": "2099-10-15T11:00:00Z"}})
    objs = client.portal.call(eng.defence_objects)
    assert objs["KELM-4-L4"]["closed"].startswith("concluded") and not objs["KELM-OBJ-2"].get("closed")
    page = client.get("/defence", headers=H).text
    assert "destination <b class=\"mono\">KELM-5</b>" in page and "Concluded (1)" in page


def test_managed_by_folds_in_new_ami_events_and_replays_after_a_delete(tmp_path):
    import asyncio
    from rsweb.ami_schedule import managed_by
    from rsweb.db import DB

    async def go():
        db = DB(str(tmp_path / "m.sqlite"))
        await db.open()
        n = iter(range(100))

        async def ev(event, ctrl, devs):
            await db.insert_event({"id": f"e{next(n)}", "event": event, "device_code": ctrl, "payload": {"devices": devs}})
        await ev("ami.digest", "C1", [{"device_code": "D1"}, {"device_code": "D2"}])
        assert await managed_by(db) == {"D1": "C1", "D2": "C1"}
        await ev("ami.released", "C1", ["D2"])
        await ev("ami.adopted", "C2", ["D3"])
        assert await managed_by(db) == {"D1": "C1", "D3": "C2"}
        await db.execute("DELETE FROM events WHERE event='ami.adopted'")   # a simulation's events removed
        assert await managed_by(db) == {"D1": "C1"}
        await db.close()
    asyncio.run(go())
