"""Core tests: BobNet, messages and notifications (split out of test_core.py by test name)."""
from corelib import *  # noqa: F401,F403


def test_worker_timers_notifications_and_digest(tmp_path):
    async def go():
        db = DB(str(tmp_path / "t.sqlite"))
        await db.open()
        s = Settings(api_token="t", api_base="http://x/v1", db_path=str(tmp_path / "t.sqlite"))
        w = Worker(s, db, RSClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))), Hub())
        since = iso(datetime.now(timezone.utc) - timedelta(minutes=1))
        arrive = datetime.now(timezone.utc) + timedelta(minutes=5)
        await w.handle_event(_ev(1, "travel.departed", destination="SOL-4", origin="SOL-3", arrives_at=iso(arrive)))
        assert len(await db.fetchall("SELECT * FROM timers")) == 1
        await w.handle_event(_ev(1, "travel.departed"))  # duplicate id ignored
        assert (await db.fetchone("SELECT COUNT(*) n FROM events"))["n"] == 1
        await w.handle_event(_ev(2, "travel.arrived", destination="SOL-4"))
        assert await db.fetchall("SELECT * FROM timers") == []
        await w.handle_event(_ev(3, "print.completed", device_type="mining_drone", new_device_code="BEEF0001"))
        await w.handle_event(_ev(4, "mining.stopped", resource_type="structural", quantity_mined=42))
        await w.handle_event(_ev(5, "hub.warning", warning_type="maintenance_due", capacity=70))
        await w.handle_event(_ev(6, "experience.gained", amount=25, source="mining"))
        levels = sorted(r["level"] for r in await db.fetchall("SELECT level FROM notifications"))
        assert levels == ["warning"]   # print done: feed only since 1.381.0; hub warning
        # inventory snapshots for the delta
        await db.execute("INSERT INTO inventory_history VALUES(?,?,?)", ("2000-01-01T00:00:00+00:00", "structural", 100))
        await db.execute("INSERT INTO inventory_history VALUES(?,?,?)", (iso(datetime.now(timezone.utc)), "structural", 180))
        d = await notify.build_digest(db, since)
        await db.close()
        return d

    d = run(go())
    assert d["printed"] == {"mining_drone": 1}
    assert d["mined"] == {"structural": 42}
    assert d["xp"] == 25
    assert d["arrivals_total"] == 1
    assert d["inventory_delta"] == {"structural": 80}
    assert len(d["alerts"]) == 1
    assert "printed 1 device" in d["headline"]


def test_bobnet_channels_list_and_subscribe(client):
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(client.app.state.worker.sync_account)
    page = client.get("/messages", headers=H).text
    assert "BobNet channels" in page and 'value="#trade" checked' in page
    r = client.post("/bobnet/channels/refresh", headers=HX)
    assert r.headers.get("HX-Refresh")
    page = client.get("/messages", headers=H).text
    assert 'value="#explorers"' in page and "Listed by relay BCN00001" in page
    # keep #general, drop #trade, add #explorers (ticked) and #ops (typed without the #)
    client.post("/bobnet/subscribe", data={"channel": ["#general", "#explorers"], "new": "ops"}, headers=HX)
    assert world.channels == ["#general", "#explorers", "#ops"]
    acct = client.portal.call(client.app.state.db.kv_get, "account")
    assert acct["bobnet_channels"] == ["#general", "#explorers", "#ops"]
    page = client.get("/messages", headers=H).text
    assert 'value="#trade" >' in page or 'value="#trade" ' in page and 'value="#trade" checked' not in page
    assert "<option>#explorers</option>" in page                       # the Send box offers subscribed channels
    r = client.post("/bobnet/history", data={"channel": "#explorers"}, headers=HX)
    assert "anyone near ZALDANAL?" in r.text and "hello" not in r.text
    assert "Older" in r.text and '&quot;cursor&quot;: 2' in r.text
    r = client.post("/bobnet/history", data={"cursor": "2"}, headers=HX)   # the page next_cursor points to
    assert "first light" in r.text and "Older" not in r.text


def test_notification_kinds_and_error_only_badge(client):
    from rsweb import notify
    assert notify.notification_for({"event": "site.depleted", "payload": {}}) is None          # expected: no notification
    assert notify.notification_for({"event": "teleport.failed", "payload": {}})["level"] == "error"
    assert notify.notification_for({"event": "hub.warning", "payload": {}})["level"] == "warning"
    assert notify.kind("done") == "info" and notify.kind("alert") == "warning"
    db = client.app.state.db
    eng = client.app.state.worker.automations
    client.portal.call(db.execute, "UPDATE notifications SET read=1")
    client.portal.call(eng.log, "loadouts", "x: waits for your Confirm", "error", True)
    client.portal.call(eng.log, "loadouts", "stopped: x — y failed: z", "alert", True)   # the engine plans again
    client.portal.call(eng.log, "fleets", "M: mission complete", "info", True)
    levels = [r["level"] for r in client.portal.call(db.fetchall, "SELECT level FROM notifications WHERE read=0 ORDER BY id")]
    assert levels == ["error", "warning", "info"]
    assert client.portal.call(notify.unread_errors, db) == 1
    page = client.get("/notifications", headers=H).text
    assert 'id="notif-toggles"' in page and "errors (1)" in page and "warnings (1)" in page
    assert 'data-n="1"' in client.get("/", headers=H).text                                    # badge: errors only


def test_bobnet_no_double_send_and_clear_on_success(client):
    client.portal.call(client.app.state.worker.sync_account)
    reps = client.portal.call(client.app.state.db.kv_get, "replicants") or {}
    code = next(iter(reps))
    r = client.post(f"/replicants/{code}/message", data={"channel": "#general", "text": "hello all"}, headers=HX)
    assert r.headers.get("X-Sent") == "1" and "Sent to #general" in r.text
    # the same text again (spacing / case aside) is refused; a different one goes
    r = client.post(f"/replicants/{code}/message", data={"channel": "#general", "text": "  Hello   all "}, headers=HX)
    assert "Already sent" in r.text and r.headers.get("X-Sent") is None
    assert client.post(f"/replicants/{code}/message", data={"channel": "#trade", "text": "hello all"}, headers=HX).headers.get("X-Sent") == "1"
    sent = client.portal.call(client.app.state.db.fetchall, "SELECT body FROM actions WHERE path=?", (f"/replicants/{code}/message",))
    assert len(sent) == 2
    page = client.get("/messages", headers=H).text
    assert "X-Sent" in page and "hx-disabled-elt" in page


def test_mentions_of_my_replicant_notify_and_come_first(client):
    from rsweb import notify
    assert notify.mentions("hey @Sk3y, trade?", ["Sk3y"]) and notify.mentions("SK3Y!", ["Sk3y"])
    assert notify.mentions("ping Sk3y-4", ["Sk3y"]) and notify.mentions("Sk3y-1 o7", ["Sk3y"])
    assert not notify.mentions("Sk3yNet is down", ["Sk3y"]) and not notify.mentions("hello", ["Sk3y"])
    db = client.app.state.db
    reps = client.portal.call(db.kv_get, "replicants") or {}
    if not reps:
        client.portal.call(client.app.state.worker.sync_account)
        reps = client.portal.call(db.kv_get, "replicants")
    code = next(iter(reps))
    reps[code]["name"] = "Sk3y"
    client.portal.call(db.kv_set, "replicants", reps)
    w = client.app.state.worker
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    client.portal.call(w.handle_event, {"id": "bn-1", "event": "bobnet.new", "created_at": now,
                                         "payload": {"channel": "#general", "replicant_name": "Sylphrena", "replicant_code": "X1",
                                                     "message": "anyone seen sk3y near LERNA?"}})
    client.portal.call(w.handle_event, {"id": "bn-2", "event": "bobnet.new", "created_at": now,
                                         "payload": {"channel": "#general", "replicant_name": "Sylphrena", "replicant_code": "X1",
                                                     "message": "o7"}})
    client.portal.call(w.handle_event, {"id": "bn-3", "event": "bobnet.new", "created_at": now,
                                         "payload": {"channel": "#general", "replicant_name": "Sk3y", "replicant_code": code,
                                                     "message": "Sk3y here"}})           # my own post: no notification
    rows = client.portal.call(db.fetchall, "SELECT level, title FROM notifications WHERE title LIKE 'Mentioned%'")
    assert len(rows) == 1 and rows[0]["level"] == "mention" and "anyone seen sk3y" in rows[0]["title"]
    from rsweb import notify as _n
    assert client.portal.call(_n.unread_errors, db) >= 1                     # mentions count in the badge
    page = client.get("/messages", headers=H).text
    assert "Mentioning you" in page and 'data-mention="1"' in page and 'id="bn-mentions"' in page
    # acknowledged, it drops off the card (the feed keeps its highlight)
    client.post("/messages/mentions/ack", data={"event_id": "bn-1"}, headers=HX)
    page = client.get("/messages", headers=H).text
    assert "Mentioning you" not in page and 'data-mention="1"' in page


def test_ami_digest_scans_are_stored_and_naive_times_use_the_account_zone(client):
    """Docs: adopted devices report through their controller's digest — its scans become scan.completed rows; a naive
    impact_eta is in the account's time zone."""
    from rsweb import shapes
    db = client.app.state.db
    client.portal.call(client.app.state.worker.handle_event, {
        "id": "dg-1", "event": "ami.survey.digest", "device_code": "SC", "location": "NUNKA-3", "created_at": "2026-10-13T12:00:00+00:00",
        "payload": {"directive": "survey_system", "devices": [{"device_code": "S1", "events": 2, "last_event": "scan.completed"}],
                    "report": {"scans": [{"device_code": "S1", "scan_target": "NUNKA-3", "scan_type": "planet", "report": {}}]}}})
    row = client.portal.call(db.fetchone, "SELECT device_code, location FROM events WHERE id='dg-1#scan0'")
    assert row == {"device_code": "S1", "location": "NUNKA-3"}
    assert "NUNKA-3" in client.portal.call(db.kv_get, "surveyed")
    old = shapes.ACCOUNT_TZ[0]
    try:
        shapes.ACCOUNT_TZ[0] = "Europe/London"
        assert shapes.game_time("2026-08-26T09:30:00").isoformat() == "2026-08-26T09:30:00+01:00"
        assert shapes.game_time("2026-08-26T09:30:00Z").utcoffset().total_seconds() == 0
    finally:
        shapes.ACCOUNT_TZ[0] = old


def test_messages_page_auto_read_checkboxes(client):
    """Owner 2026-10-30: checkboxes on the Messages page mark salvage discoveries and civilizations' requests for help
    read as they arrive (salvage on by default, as before)."""
    from rsweb.ingest import auto_read, civ_help_message, AUTO_READ_DEFAULT
    civ = {"id": 3, "title": "Broadcast intercepted: the Veyrani need food", "body": "Famine grips AEMEROTH-2."}
    sal = {"id": 4, "title": "Salvage discovered: derelict hauler"}
    assert civ_help_message(civ) and not civ_help_message({"title": "The Veyrani request your assistance"})
    assert not civ_help_message({"title": "Print completed: mining drone"}) and not civ_help_message(sal)
    assert auto_read(sal, AUTO_READ_DEFAULT) and not auto_read(civ, AUTO_READ_DEFAULT)
    assert auto_read(civ, {"salvage": False, "civ": True}) and not auto_read(sal, {"salvage": False, "civ": True})
    page = client.get("/messages", headers=H).text
    assert 'name="salvage" value="1" checked' in page and 'name="civ" value="1">' in page
    client.post("/messages/auto-read", data={"civ": "1"}, headers=HX)
    assert client.portal.call(client.app.state.db.kv_get, "message_auto_read") == {"salvage": False, "civ": True}
    assert 'name="civ" value="1" checked' in client.get("/messages", headers=H).text


def test_bobnet_feed_shows_where_the_poster_is(client):
    w = client.app.state.worker
    client.portal.call(w.handle_event, _ev(9901, "bobnet.new", replicant_name="Dash", replicant_code="D1",
                                           current_star="ROSALIATE", channel="#general", message="Shop open"))
    page = client.get("/messages", headers=H).text
    assert '@ <a href="/systems/ROSALIATE">ROSALIATE</a></span>: Shop open' in page


def test_bobnet_feed_pages_back(client, monkeypatch):
    from rsweb import web_activity as web   # where the Messages routes read the page size
    monkeypatch.setattr(web, "BOBNET_PAGE", 2)
    w = client.app.state.worker
    for i in range(5):
        client.portal.call(w.handle_event, _ev(9910 + i, "bobnet.new", replicant_name="Dash", replicant_code="D1",
                                               channel="#general", message=f"note-{i}"))
    page = client.get("/messages", headers=H).text
    assert "note-4" in page and "note-3" in page and "note-2" not in page and "/bobnet/older?before=" in page
    seq = page.split("/bobnet/older?before=")[1].split('"')[0]
    r = client.get(f"/bobnet/older?before={seq}", headers=HX).text
    assert "note-2" in r and "note-1" in r and "note-3" not in r and "/bobnet/older?before=" in r
    r = client.get("/bobnet/older?before=" + r.split("/bobnet/older?before=")[1].split('"')[0], headers=HX).text
    assert "note-0" in r and "Older" not in r
    r = client.get("/bobnet/older?q=NOTE-1", headers=HX).text                  # search: any case, every saved message
    assert "note-1" in r and "note-0" not in r and "Older" not in r
    assert "Nothing matches." in client.get("/bobnet/older?q=50%25", headers=HX).text   # % is literal
    r = client.get("/bobnet/older?q=dash", headers=HX).text                     # the poster's name too, paged
    assert "note-4" in r and "note-2" not in r and "q=dash" in r


def test_asteroid_impacts_are_warnings_and_unread_messages_stand_out(client):
    from rsweb import notify
    for e in ("system.object_impacted",):
        assert notify.notification_for({"event": e, "payload": {}})["level"] == "warning"
    assert notify.notification_for({"event": "system.impact_warning", "payload": {}})["level"] == "error"
    db = client.app.state.db
    client.portal.call(client.app.state.worker.sync_messages)
    client.portal.call(db.kv_set, "messages", [
        {"id": 1, "title": "Salvage discovered: old", "body": "a", "read": True},
        {"id": 2, "title": "Fresh news", "body": "b", "read": False}])
    client.app.state.worker.sync_messages = lambda: asyncio.sleep(0)
    page = client.get("/messages", headers=H).text
    assert "1 unread" in page and 'id="msg-unread"' in page
    assert page.index("Fresh news") < page.index(">Read<") < page.index("Salvage discovered: old")


def test_older_unread_messages_behind_the_badge_are_listed(client):
    """The badge counts every unread message; ones older than the latest page are fetched with unread_only."""
    client.app.state.api.http._transport.app.state.world.older_unread = [
        {"id": 90, "title": "Old unread news", "body": "x", "created_at": "2026-10-01T00:00:00Z", "is_read": False}]
    client.portal.call(client.app.state.worker.sync_messages)
    msgs = client.portal.call(client.app.state.db.kv_get, "messages")
    assert [m["id"] for m in msgs] == [1, 90]
    client.app.state.worker.sync_messages = lambda: asyncio.sleep(0)
    page = client.get("/messages", headers=H).text
    assert "Old unread news" in page and "2 unread" in page


def test_bobnet_novel_messages_first_and_repeats_grayed(client):
    from rsweb.web import bobnet_key
    assert bobnet_key("NOSOXA is in the rear-view mirror") == bobnet_key("ALGENIBUS-OORT is in the rear-view mirror!")
    assert bobnet_key("3 drones at SOL") == bobnet_key("12 drones at TWAFFY") != bobnet_key("hello there")
    w = client.app.state.worker
    for i, msg in enumerate(["NOSOXA is in the rear-view mirror", "fresh line one", "SOL is in the rear-view mirror"]):
        client.portal.call(w.handle_event, _ev(9950 + i, "bobnet.new", replicant_name="Bill", replicant_code="B1",
                                               channel="#general", message=msg))
    page = client.get("/messages", headers=H).text
    feed = page.split('id="bn-feed"')[1]
    assert feed.index("fresh line one") < feed.index("Repeats") < feed.index("SOL is in the rear-view")
    assert 'id="bn-repeats"' in page and feed.count('class="bn-repeat"') == 1
def test_automation_notifications_link_to_the_fixing_page(tmp_path):
    async def go():
        db = DB(str(tmp_path / "t.sqlite"))
        await db.open()
        s = Settings(api_token="t", api_base="http://x/v1", db_path=str(tmp_path / "t.sqlite"))
        w = Worker(s, db, RSClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))), Hub())
        a = w.automations

        async def fleets():
            return [{"id": "f1", "name": "Miner 5"}]
        a.fleets = fleets
        await a.log("fleets", "Miner 5: no room aboard", "error", notify=True)
        await a.log("fleets", "away from their fleet for an hour or more: X", "alert", notify=True)
        await a.log("evacuate", "evacuation failed: boom", "error", notify=True)
        await a.log("wards", "relay rollout paused: the last 3 systems", "error", notify=True)
        await a.log("logistics", "canceled: trip", notify=True)
        await a.log("chain", "stopped: Do it — failed", "error", notify=True, job={"id": "j7", "rule": "chain"})
        await a.log("logistics", "trip — far; waits for your Confirm", "error", notify=True, job={"id": "j8", "rule": "logistics"})
        await a.log("whatever", "something", "error", notify=True)
        links = [r["link"] for r in await db.fetchall("SELECT link FROM notifications ORDER BY id")]
        assert links == ["/fleets/details?fleet=f1", "/fleets", "/defence", "/map/relays", "/fleets/logistics",
                         "/automations#job-j7", "/fleets/logistics", "/automations"]
        await db.close()
    asyncio.run(go())
