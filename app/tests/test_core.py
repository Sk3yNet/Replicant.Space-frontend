"""Core behaviour: SSE parsing, rate limiting, safety blocks, timers, notifications, digest, auth."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from rsweb import notify
from rsweb.api import ApiError, Bucket, RSClient, parse_sse
from rsweb.config import Settings
from rsweb.db import DB
from rsweb.hub import Hub
from rsweb.ingest import Worker
from rsweb.main import create_app
from rsweb.mock import create_mock


def iso(dt):
    return dt.isoformat(timespec="seconds")


async def _lines(text):
    for line in text.split("\n"):
        yield line


def run(coro):
    return asyncio.run(coro)


def test_parse_sse_merges_id_and_skips_keepalive():
    text = ('id: 1-0\nevent: mining.started\ndata: {"event":"mining.started","payload":{"a":1}}\n\n'
            ": keepalive\n\n"
            'id: 2-0\nevent: travel.arrived\ndata: {"payload":{}}\n\n')

    async def collect():
        return [e async for e in parse_sse(_lines(text))]

    evs = run(collect())
    assert [e["id"] for e in evs] == ["1-0", "2-0"]
    assert evs[1]["event"] == "travel.arrived"  # filled from the SSE event: line


def test_bucket_reserve_and_refill():
    b = Bucket(60)
    for _ in range(50):
        assert b.wait_time() == 0
        b.take()
    # 10 left: background with reserve 20 must wait, interactive need not.
    assert b.wait_time(reserve=20) > 0
    assert b.wait_time() == 0
    assert b.used_last_minute() == 50


def test_blocked_paths():
    for m, p in [("DELETE", "/accounts/me"), ("DELETE", "/v1/accounts/me/"), ("POST", "/accounts/recover")]:
        with pytest.raises(ApiError):
            RSClient.check_allowed(m, p)
    RSClient.check_allowed("GET", "/accounts/me")
    RSClient.check_allowed("PATCH", "/accounts/me")


def test_429_backoff_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"code": 429, "status": "Too Many Requests"})
        return httpx.Response(200, json={"ok": True}, headers={"X-Replicant-Space-Unread-Count": "3"})

    async def go():
        c = RSClient(Settings(api_token="t", api_base="http://x/v1"), transport=httpx.MockTransport(handler))
        body = await c.get("/accounts/me")
        await c.close()
        return body, c

    body, c = run(go())
    assert body == {"ok": True} and calls["n"] == 2 and c.unread_count == 3


def test_error_envelope():
    def handler(request):
        return httpx.Response(400, json={"error": "Insufficient conductive resource"})

    async def go():
        c = RSClient(Settings(api_token="t", api_base="http://x/v1"), transport=httpx.MockTransport(handler))
        try:
            await c.post("/replicants/X/print", {"device_type": "mining_drone"})
        finally:
            await c.close()

    with pytest.raises(ApiError) as ei:
        run(go())
    assert ei.value.status == 400 and "conductive" in ei.value.message


def _ev(i, event, device="AAAA0001", created=None, **payload):
    return {"id": f"{1000 + i}-0", "event": event, "category": event.split(".")[0], "device_code": device,
            "device_type": "mining_drone", "location": "SOL-BELT-1", "payload": payload,
            "created_at": iso(created or datetime.now(timezone.utc))}


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
        assert levels == ["alert", "done"]
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


def test_visit_baseline(tmp_path):
    async def go():
        db = DB(str(tmp_path / "v.sqlite"))
        await db.open()
        first = await notify.touch_visit(db, "a@b.c", 30)
        assert first["new_visit"]
        again = await notify.touch_visit(db, "a@b.c", 30)
        assert not again["new_visit"] and again["baseline_at"] == first["baseline_at"]
        old = iso(datetime.now(timezone.utc) - timedelta(hours=3))
        await db.execute("UPDATE visitors SET last_seen_at=?, digest_dismissed=1", (old,))
        later = await notify.touch_visit(db, "a@b.c", 30)
        await db.close()
        return later, old

    later, old = run(go())
    assert later["new_visit"] and later["baseline_at"] == old and later["digest_dismissed"] == 0


# --- HTTP level, against the mock game API ------------------------------------------------------
@pytest.fixture()
def client(tmp_path):
    mock = create_mock()
    s = Settings(api_token="dev", api_base="http://mock/v1", db_path=str(tmp_path / "app.sqlite"),
                 allowed_emails=["joe@example.com"], disable_background=True)
    app = create_app(s, transport=httpx.ASGITransport(app=mock))
    with TestClient(app) as c:
        w = app.state.worker

        async def sync():
            await w.sync_account(); await w.sync_devices(); await w.sync_inventory(); await w.sync_blueprints(); await w.sync_catalogue()
        c.portal.call(sync)
        yield c


H = {"X-Auth-Request-Email": "joe@example.com"}
HX = {**H, "HX-Request": "true"}


def test_auth_required(client):
    assert client.get("/").status_code == 401
    assert client.get("/", headers={"X-Auth-Request-Email": "evil@example.com"}).status_code == 403
    # state-changing request without HX-Request header is refused (CSRF guard)
    assert client.post("/digest/dismiss", headers=H).status_code == 403


@pytest.mark.parametrize("path", ["/", "/fleet", "/devices/2AC61210", "/replicants/77F75255", "/systems", "/systems/SOL",
                                  "/map", "/api/map.json", "/blueprints", "/ami", "/events", "/notifications",
                                  "/messages", "/account", "/console", "/digest?hours=24", "/locations/SOL-BELT-1"])
def test_pages_render(client, path):
    r = client.get(path, headers=H)
    assert r.status_code == 200, r.text[:500]


def test_actions(client):
    r = client.post("/replicants/77F75255/travel", data={"destination": "abotein", "dry_run": "1"}, headers=HX)
    assert "Go — travel to ABOTEIN" in r.text
    r = client.post("/replicants/77F75255/travel", data={"destination": "ABOTEIN"}, headers=HX)
    assert "result ok" in r.text and "Arrives" in r.text
    r = client.post("/devices/2AC61212/command", data={"command": "nope", "args": ""}, headers=HX)
    assert "result err" in r.text and "not available" in r.text
    r = client.post("/console", data={"method": "DELETE", "path": "/accounts/me", "body": ""}, headers=HX)
    assert "blocked" in r.text
    r = client.post("/blueprints/plan", data={"location": "SOL-BELT-1", "qty:mining_drone": "2"}, headers=HX)
    assert "structural" in r.text
    timers = client.get("/partials/timers", headers=HX).text
    assert "ABOTEIN" in timers
    acts = client.get("/account", headers=H).text
    assert "/replicants/77F75255/travel" in acts


def test_as_amounts_shapes():
    from rsweb.shapes import as_amounts, normalize_inventory
    assert as_amounts({"carbon": 5, "rares": "2"}) == {"carbon": 5.0, "rares": 2.0}
    assert as_amounts([{"resource": "carbon", "quantity": 5}, {"resource": "carbon", "quantity": 1}]) == {"carbon": 6.0}
    assert as_amounts([{"name": "rares", "amount": 3}]) == {"rares": 3.0}
    assert as_amounts([["silicates", 4]]) == {"silicates": 4.0}
    assert as_amounts([{"structural": 9}]) == {"structural": 9.0}
    assert as_amounts(None) == {} and as_amounts("junk") == {}
    inv = normalize_inventory([{"location": "SOL-BELT-1", "items": [{"resource": "carbon", "quantity": 2}]}])
    assert inv[0]["items"] == {"carbon": 2.0}


def test_blueprints_with_list_shaped_inventory(client):
    r = client.get("/blueprints", headers=H)
    assert r.status_code == 200 and "×" in r.text
    r = client.post("/blueprints/plan", data={"location": "SOL-BELT-1", "qty:mining_drone": "1"}, headers=HX)
    import re
    have = re.search(r"<td>structural</td><td class=\"num\">[^<]+</td><td class=\"num\">([\d,.]+)</td>", r.text)
    assert have and float(have.group(1).replace(",", "")) >= 1200  # read from the list-shaped mock inventory


class _Form(dict):
    def getlist(self, k):
        v = self.get(k)
        return v if isinstance(v, list) else ([v] if v else [])


def test_parse_fields_builds_nested_bodies():
    from rsweb import commands as c
    body = c.parse_fields(c.COMMANDS["enqueue_print"], _Form({
        "f.device_type": "mining_drone", "f.quantity": "3", "f.tags": "fleet-1, belt",
        "f.oncomplete.command": "travel", "f.oncomplete.destination": "sol-belt-1",
        "f.oncomplete.resource_type": "carbon", "f.flatpack": "false"}))
    assert body == {"device_type": "mining_drone", "quantity": 3, "tags": ["fleet-1", "belt"],
                    "oncomplete": {"command": "travel", "destination": "SOL-BELT-1"}, "flatpack": False}
    body = c.parse_fields(c.COMMANDS["enqueue_print"], _Form({"f.device_type": "x", "f.oncomplete.command": ""}))
    assert "oncomplete" not in body
    body = c.parse_fields(c.DIRECTIVES["transport"]["delivery"], _Form({
        "f.route.collect": "SOL-BELT-1", "f.route.deliver": "SOL-3-L4", "f.requirement.carbon": "50"}))
    assert body == {"route": {"collect": "SOL-BELT-1", "deliver": "SOL-3-L4"}, "requirement": {"carbon": 50}}
    body = c.parse_fields(c.DIRECTIVES["mining"]["maintain_ratios"], _Form({"f..structural": "0.5", "f..rares": "0.1"}))
    assert body == {"structural": 0.5, "rares": 0.1}
    with pytest.raises(c.FormError):
        c.parse_fields(c.COMMANDS["travel"], _Form({}))
    assert c.parse_fields(c.COMMANDS["adopt"], _Form({"f.devices": ["A", "B"]})) == {"devices": ["A", "B"]}


def test_command_forms_render_with_suggestions(client):
    r = client.get("/devices/2AC61212/command-form?command=travel", headers=HX)
    assert r.status_code == 200 and 'list="c-2AC61212-loc"' in r.text and "SOL-BELT-1" in r.text and "SOL-3-L4" in r.text
    r = client.get("/devices/AF00BEEF/command-form?command=enqueue_print", headers=HX)
    assert "mining_drone" in r.text and "MC91FF22" in r.text  # device types + AMI controller choices
    r = client.get("/devices/MC91FF22/command-form?command=adopt", headers=HX)
    assert 'type="checkbox"' in r.text and "2AC61210" in r.text
    r = client.get("/devices/2AC61212/command-form?command=mystery", headers=HX)
    assert "No field list" in r.text
    r = client.get("/ami/MC91FF22/directive-form?directive=gather_resources", headers=HX)
    assert 'name="f..structural"' in r.text
    # submit through the fields
    r = client.post("/devices/2AC61212/command", data={"command": "start_mining", "f.resource_type": "rares"}, headers=HX)
    assert "result ok" in r.text
    r = client.post("/devices/2AC61212/command", data={"command": "travel"}, headers=HX)
    assert "destination is required" in r.text
    r = client.post("/ami/MC91FF22/directive", data={"directive": "gather_resources", "f..structural": "500"}, headers=HX)
    assert "result ok" in r.text
    acts = client.get("/account", headers=H).text  # audit log shows the body that was sent
    assert "&#34;configuration&#34;: {&#34;structural&#34;: 500}" in acts


def test_new_blueprint_detected_and_announced(client):
    from rsweb import mock as mockmod
    from rsweb.ingest import may_unlock_blueprint
    w = client.app.state.worker
    extra = {"device_type": "ftl_relay", "short_description": "Relays", "print_time": 900, "resources": {"rares": 50}}
    mockmod.BLUEPRINTS.append(extra)
    try:
        added = client.portal.call(w.sync_blueprints)
    finally:
        mockmod.BLUEPRINTS.remove(extra)
    assert added == ["ftl_relay"]
    page = client.get("/notifications", headers=H).text
    assert "New blueprint unlocked: ftl relay" in page
    bp = client.get("/blueprints", headers=H).text
    assert "ftl relay" not in bp or "new" in bp  # page re-syncs; relay gone again from mock is fine
    d = client.get("/digest?hours=1", headers=H).text
    assert "New blueprints" in d and "ftl relay" in d
    # heuristics
    assert may_unlock_blueprint({"event": "device.decommissioned", "category": "device", "payload": {"blueprint_discovered": "x"}})
    assert may_unlock_blueprint({"event": "event.completed", "category": "event", "payload": {}})
    assert not may_unlock_blueprint({"event": "mining.started", "category": "mining", "payload": {"site": "A"}})
    assert not may_unlock_blueprint({"event": "experience.gained", "category": "experience", "payload": {"amount": 5}})


def test_vessel_prints_directly_and_explains_busy(client):
    # heaven vessels have no queue: one print through the replicant endpoint
    r1 = client.post("/blueprints/print", data={"printer": "replicant:77F75255", "device_type": "mining_drone", "quantity": "2"}, headers=HX)
    assert "result ok" in r1.text and "/replicants/77F75255/print" in r1.text and "only 1 of 2" in r1.text
    r2 = client.post("/replicants/77F75255/print", data={"device_type": "mining_drone"}, headers=HX)
    assert "Printer is busy" in r2.text and "don&#39;t have a queue" in r2.text
    page = client.get("/replicants/77F75255", headers=H).text
    assert "Clear queue" not in page
    # the planner refuses more than one device on a vessel
    r3 = client.post("/blueprints/queue-plan", data={"printer": "replicant:77F75255", "qty:mining_drone": "2", "_planned": "1"}, headers=HX)
    assert "no queue" in r3.text


def test_autofactory_print_queue_panel(client):
    import time
    world = client.app.state.api.http._transport.app.state.world
    world.af_print_seconds = 0.5
    world.queues["AF00BEEF"] = []
    world.devices[7]["status"] = "idle"
    for t, n in (("survey_drone", 1), ("ftl_beacon", 2)):
        r = client.post("/devices/AF00BEEF/print-queue", data={"action": "add", "device_type": t, "quantity": str(n)}, headers=HX)
        assert "result ok" in r.text
    async def once():
        return True
    _pump(client, once)
    page = client.get("/devices/AF00BEEF", headers=H).text
    assert "Print queue" in page and "printing" in page and "survey drone" in page.lower()
    # the printing item is shown above the queue, not in it
    panel = client.get("/devices/AF00BEEF/print-queue", headers=HX).text
    assert panel.count("Remove</button>") == 2 and "ftl beacon" in panel.lower()
    assert "all done in" in panel
    # remove #1, then clear the rest
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "remove", "index": "1"}, headers=HX)
    assert "result ok" in r.text and r.text.count("Remove</button>") == 1
    assert '"index": 1' in client.portal.call(client.app.state.db.fetchone, "SELECT body FROM actions ORDER BY id DESC LIMIT 1")["body"]
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "clear"}, headers=HX)
    assert "Queue is empty" in r.text
    # finishes, and the panel says idle
    time.sleep(0.8)
    _pump(client, once)
    assert "Not printing" in client.get("/devices/AF00BEEF/print-queue", headers=HX).text
    # blueprints tab lists the autofactory's queue panel
    assert "/devices/AF00BEEF/print-queue?compact=1" in client.get("/blueprints", headers=H).text


def test_maintenance_drone_gets_patrol(client):
    from rsweb import commands as c
    # blueprint says patrol, even if the device type name doesn't match our built-in kinds
    assert c.directives_for({"device_type": "repair_bot"}, [{"device_type": "repair_bot", "directives": ["patrol"]}]) == ["patrol"]
    assert c.directives_for({"device_type": "maintenance_drone"}, []) == ["patrol"]
    world = client.app.state.api.http._transport.app.state.world
    world.devices.append({"device_code": "MD000001", "device_type": "maintenance_drone", "location": "SOL-BELT-1",
                          "status": "idle", "features": ["cruise", "repair", "ami", "stow"], "replicant_code": "77F75255",
                          "available_commands": ["set_directive", "clear_directive", "launch", "travel"]})
    client.portal.call(client.app.state.worker.sync_devices)
    r = client.get("/devices/MD000001/command-form?command=set_directive", headers=HX)
    assert '<option value="patrol">' in r.text
    r = client.post("/devices/MD000001/command", data={"command": "set_directive", "directive": "patrol"}, headers=HX)
    assert "result ok" in r.text and "directive patrol" in r.text
    assert '<option value="patrol">' in client.get("/ami", headers=H).text


def test_duplicate_timers_collapse_but_parallel_devices_stay():
    from rsweb.ingest import duplicate_timers
    t0 = "2026-09-30T12:00:00+00:00"
    t1 = "2026-09-30T12:00:05+00:00"
    rows = [
        # same trip reported three ways
        {"key": "travel:77F75255", "kind": "travel", "label": "77F75255 → ABOTEIN", "device_code": "77F75255", "ends_at": t0, "source": "action"},
        {"key": "travel:11ADA230", "kind": "travel", "label": "heaven vessel 11ADA230 → ABOTEIN", "device_code": "11ADA230", "ends_at": t1, "source": "travel.departed"},
        {"key": "travel:REP", "kind": "travel", "label": "device REP → ABOTEIN", "device_code": None, "ends_at": t1, "source": "travel.departed"},
        # two drones launched together to the same belt: both real
        {"key": "travel:D1", "kind": "travel", "label": "mining drone D1 → SOL-BELT-1", "device_code": "D1", "ends_at": t0, "source": "travel.departed"},
        {"key": "travel:D2", "kind": "travel", "label": "mining drone D2 → SOL-BELT-1", "device_code": "D2", "ends_at": t0, "source": "travel.departed"},
        # a print finishing at the same moment is a different kind
        {"key": "print:11ADA230", "kind": "print", "label": "print mining_drone @ SOL", "device_code": "11ADA230", "ends_at": t0, "source": "print.started"},
    ]
    kept, dups = duplicate_timers(rows)
    assert sorted(dups) == ["travel:77F75255", "travel:REP"]
    assert {k["key"] for k in kept} == {"travel:11ADA230", "travel:D1", "travel:D2", "print:11ADA230"}


def test_worker_removes_duplicate_timer_rows(tmp_path):
    async def go():
        db = DB(str(tmp_path / "d.sqlite")); await db.open()
        s = Settings(api_token="t", api_base="http://x/v1")
        w = Worker(s, db, RSClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))), Hub())
        end = datetime.now(timezone.utc) + timedelta(minutes=5)
        await w.handle_event({"id": "1-0", "event": "travel.departed", "device_code": "11ADA230", "device_type": "heaven_vessel",
                              "payload": {"destination": "ABOTEIN", "arrives_at": iso(end)}, "created_at": iso(datetime.now(timezone.utc))})
        await w.handle_event({"id": "2-0", "event": "travel.departed", "device_code": None, "replicant_code": "77F75255",
                              "payload": {"destination": "ABOTEIN", "arrives_at": iso(end)}, "created_at": iso(datetime.now(timezone.utc))})
        rows = await db.fetchall("SELECT key FROM timers")
        await db.close()
        return rows
    assert [r["key"] for r in run(go())] == ["travel:11ADA230"]


def _pump(client, until, timeout=10.0):
    """Feed new mock-world events to the worker (the event stream isn't running in tests)."""
    import asyncio as _a
    world = client.app.state.api.http._transport.app.state.world
    w = client.app.state.worker

    async def go():
        seen = getattr(client, "_seen", len(world.events))
        loop = _a.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            new = world.events[seen:]
            seen = len(world.events)
            for ev in new:
                await w.handle_event(dict(ev))
            if await until():
                break
            await _a.sleep(0.02)
        client._seen = seen
    client.portal.call(go)


def test_survey_targets_order_and_filters():
    from rsweb.automations import survey_targets
    scan = {"planets": [{"designation": "X-2", "orbital_distance_au": 2, "moon_count": 2},
                        {"designation": "X-1", "orbital_distance_au": 1, "moon_count": 0}],
            "asteroid_belt": {"belts": [{"designation": "X-BELT-1", "inner_radius_au": 1.5}]}}
    t = survey_targets(scan, {"X-1": "x"}, include_moons=True, include_belts=True, max_targets=10)
    assert [x["target"] for x in t] == ["X-BELT-1", "X-2", "X-2-1", "X-2-2"]
    assert t[0]["action"] == "search" and t[1]["action"] == "scan"
    assert len(survey_targets(scan, {}, False, False, 1)) == 1


def test_auto_survey_end_to_end_against_mock(client):
    world = client.app.state.api.http._transport.app.state.world
    world.move_seconds = 0.03
    eng = client.app.state.worker.automations
    client._seen = len(world.events)
    # switch the rule on through the UI endpoint, limit to 3 bodies, keep return-and-stow
    r = client.post("/automations/rules/auto_survey", data={"enabled": "on", "use_idle": "", "include_belts": "on",
                                                            "max_targets": "3", "return_and_stow": "on"}, headers=HX)
    assert "saved" in r.text
    # the vessel arrives at SOL-BELT-1
    arrival = {"id": "9999999999999-0", "event": "travel.arrived", "category": "travel", "device_code": "11ADA230",
               "device_type": "heaven_vessel", "location": "SOL-BELT-1", "star": "SOL",
               "payload": {"destination": "SOL-BELT-1", "origin": "ABOTEIN-OORT", "travel_type": "surge"}, "created_at": "2026-09-30T12:00:00+00:00"}
    client.portal.call(client.app.state.worker.handle_event, arrival)

    async def finished():
        jobs = await eng.jobs()
        return jobs and all(j["status"] not in ("running", "waiting") for j in jobs)
    _pump(client, finished)
    jobs = client.portal.call(eng.jobs)
    assert len(jobs) == 1, jobs
    job = jobs[0]
    assert job["status"] == "done", [(s["desc"], s["status"], s["error"]) for s in job["steps"]]
    descs = [s["desc"] for s in job["steps"]]
    assert descs[0] == "deploy SV000001" and descs[-1].startswith("stow SV000001")
    assert sum(1 for d in descs if d.startswith(("scan ", "search "))) == 3
    surveyed = client.portal.call(client.app.state.db.kv_get, "surveyed")
    assert set(job["meta"]["targets"]) <= set(surveyed)
    assert "finished: survey SOL" in client.get("/automations", headers=H).text
    # arriving again: those 3 are done, so the next 3 get surveyed
    arrival["id"] = "9999999999999-1"
    client.portal.call(client.app.state.worker.handle_event, arrival)
    _pump(client, finished)
    jobs = client.portal.call(eng.jobs)
    assert len(jobs) == 2 and not set(jobs[0]["meta"]["targets"]) & set(jobs[1]["meta"]["targets"])


def test_dry_run_sends_nothing(client):
    eng = client.app.state.worker.automations
    client.post("/automations/dry-run", data={"dry_run": "on"}, headers=HX)
    client.post("/automations/rules/auto_survey", data={"enabled": "on", "max_targets": "2"}, headers=HX)
    client.post("/automations/rules/deploy_beacon", data={"enabled": "on"}, headers=HX)
    before = client.portal.call(client.app.state.db.fetchone, "SELECT COUNT(*) n FROM actions WHERE user='automation'")
    r = client.post("/automations/survey-now", data={"vessel": "11ADA230"}, headers=HX)
    assert r.status_code == 200
    arrival = {"id": "8888888888888-0", "event": "travel.arrived", "device_code": "11ADA230", "device_type": "heaven_vessel",
               "location": "SOL-BELT-1", "payload": {"destination": "SOL-BELT-1", "origin": "ABOTEIN-OORT", "travel_type": "surge"}, "created_at": "2026-09-30T12:00:00+00:00"}
    client.portal.call(client.app.state.worker.handle_event, arrival)
    after = client.portal.call(client.app.state.db.fetchone, "SELECT COUNT(*) n FROM actions WHERE user='automation'")
    assert after["n"] == before["n"] == 0
    assert client.portal.call(eng.jobs) == []
    page = client.get("/automations", headers=H).text
    assert "[dry run] survey SOL" in page and "[dry run] deploy FTL beacon FB000001" in page


def test_restart_idle_miners(client):
    eng = client.app.state.worker.automations
    client.post("/automations/rules/restart_idle_miners", data={"enabled": "on", "resource": "carbon", "cooldown_minutes": "10"}, headers=HX)
    client.portal.call(eng.tick)
    acts = client.portal.call(client.app.state.db.fetchall, "SELECT path, body FROM actions WHERE user='automation'")
    assert any('"start_mining"' in a["body"] and '"carbon"' in a["body"] and a["path"] == "/devices/2AC61212" for a in acts)
    n = len(acts)
    client.portal.call(eng.tick)  # cooldown: no second attempt
    assert len(client.portal.call(client.app.state.db.fetchall, "SELECT 1 FROM actions WHERE user='automation'")) == n


def test_travel_chain_on_device(client):
    world = client.app.state.api.http._transport.app.state.world
    world.move_seconds = 0.03
    client._seen = len(world.events)
    r = client.get("/devices/2AC61212/command-form?command=travel", headers=HX)
    assert "Then, on arrival" in r.text and 'name="then_command_0"' in r.text and "start mining" in r.text
    r = client.post("/devices/2AC61212/command", data={
        "command": "travel", "f.destination": "SOL-3-L4",
        "then_device_0": "__self__", "then_command_0": "start_mining", "then_arg_0": "carbon"}, headers=HX)
    assert "follow on Automations" in r.text, r.text
    eng = client.app.state.worker.automations

    async def finished():
        return all(j["status"] not in ("running", "waiting") for j in await eng.jobs())
    _pump(client, finished)
    job = client.portal.call(eng.jobs)[-1]
    assert job["status"] == "done", [(s["desc"], s["status"], s["error"]) for s in job["steps"]]
    acts = client.portal.call(client.app.state.db.fetchall, "SELECT path, body FROM actions WHERE user='automation' ORDER BY id")
    bodies = [a["body"] for a in acts if a["path"] == "/devices/2AC61212"]
    assert '"travel"' in bodies[0] and '"start_mining"' in bodies[1] and '"carbon"' in bodies[1]


def test_replicant_travel_chain_launches_carried_drone(client):
    world = client.app.state.api.http._transport.app.state.world
    world.move_seconds = 0.03
    client._seen = len(world.events)
    r = client.post("/replicants/77F75255/travel", data={"destination": "ABOTEIN", "dry_run": "1"}, headers=HX)
    assert "Then, on arrival" in r.text and "SV000001" in r.text  # carried drone offered
    # dry run (the Automations switch) must not block a chain the player asked for
    client.post("/automations/dry-run", data={"dry_run": "on"}, headers=HX)
    r = client.post("/replicants/77F75255/travel", data={
        "destination": "ABOTEIN",
        "then_device_0": "SV000001", "then_command_0": "deploy",
        "then_device_1": "SV000001", "then_command_1": "scan",
        "then_device_2": "__self__", "then_command_2": "system_scan"}, headers=HX)
    assert "follow on Automations" in r.text, r.text
    eng = client.app.state.worker.automations

    async def finished():
        return all(j["status"] not in ("running", "waiting") for j in await eng.jobs())
    _pump(client, finished)
    job = client.portal.call(eng.jobs)[-1]
    # arrival reported at ABOTEIN-3-L4 still counts as arriving "at ABOTEIN"
    assert job["status"] == "done" and all(s["status"] == "done" for s in job["steps"]), \
        [(s["desc"], s["status"], s["error"]) for s in job["steps"]]
    assert [s["path"] for s in job["steps"]] == ["/replicants/77F75255/travel", "/devices/SV000001",
                                                  "/devices/SV000001", "/replicants/77F75255/scan"]


def test_chain_validation(client):
    r = client.post("/devices/2AC61212/command", data={
        "command": "travel", "f.destination": "SOL-3-L4", "then_device_0": "__self__", "then_command_0": "start_mining"}, headers=HX)
    assert "needs a resource" in r.text


def test_fill_plan():
    from rsweb.cargo import fill_plan
    assert fill_plan({"structural": 100, "carbon": 50}, 15) == {"structural": 10, "carbon": 5}
    p = fill_plan({"structural": 7, "carbon": 3, "rares": 1}, 5)
    assert sum(p.values()) == 5 and all(p[r] <= q for r, q in {"structural": 7, "carbon": 3, "rares": 1}.items() if r in p)
    assert fill_plan({"carbon": 3}, 50) == {"carbon": 3}          # less stock than space: take it all
    assert fill_plan({"carbon": 30}, 0) == {} and fill_plan({}, 10) == {}


def test_collect_resources_shows_stock_and_collects_all(client):
    r = client.get("/devices/TR000001/command-form?command=collect_resources", headers=HX)
    assert "At <b>SOL-BELT-1</b>" in r.text and "15" in r.text  # 20 capacity - 5 carbon aboard
    assert "Fill to capacity" in r.text and "Collect all now (15 units)" in r.text
    assert 'max="' in r.text and 'data-fill="' in r.text
    r = client.post("/devices/TR000001/collect-all", headers=HX)
    assert "result ok" in r.text and "collect all (15 units)" in r.text
    world = client.app.state.api.http._transport.app.state.world
    tr = next(d for d in world.devices if d["device_code"] == "TR000001")
    assert sum(tr["cargo"].values()) == 20
    r = client.post("/devices/TR000001/collect-all", headers=HX)
    assert "the hold is full" in r.text


def test_system_targets_from_scans_details_and_events(client):
    from rsweb.targets import category
    assert [category(c, "SOL") for c in ["SOL", "SOL-3", "SOL-3-1", "SOL-3-L4", "SOL-BELT-1", "SOL-BELT-1-SITE-2",
                                         "SOL-1-3-SAL-1", "SOL-OBJ-2", "SOL-KUIPER"]] == \
        ["star", "planet", "moon", "lagrange", "belt", "site", "salvage", "object", "outer"]
    # refresh = system scan + belt details (resource sites)
    r = client.post("/ami/TC000001/refresh-targets", headers=HX)
    assert r.headers.get("HX-Refresh") == "true", r.text
    # a salvage find reported by the event stream
    client.portal.call(client.app.state.worker.handle_event, {
        "id": "7777777777777-0", "event": "salvage.discovered", "star": "SOL", "location": "SOL-4",
        "payload": {"designation": "SOL-4-1-SAL-1", "location": "SOL-4-1", "name": "Old probe", "salvage_type": "wreck"},
        "created_at": "2026-09-30T12:00:00+00:00"})
    from rsweb.targets import system_targets
    t = client.portal.call(system_targets, client.app.state.db, "SOL")
    codes = {x["code"]: x for x in t["targets"]}
    assert codes["SOL-BELT-1"]["category"] == "belt" and "structural rich" in codes["SOL-BELT-1"]["label"]
    assert "SOL-BELT-1-SITE-1" in codes and codes["SOL-BELT-1-SITE-1"]["category"] == "site"
    assert codes["SOL-4-1-SAL-1"]["category"] == "salvage" and "Old probe" in codes["SOL-4-1-SAL-1"]["label"]
    assert "stock" in codes["SOL-3-L4"]["label"]
    assert t["resources"]["structural"]["level"] == "rich"


def test_ami_forms_offer_system_targets(client):
    client.post("/ami/TC000001/refresh-targets", headers=HX)
    r = client.get("/ami/TC000001/directive-form?directive=delivery", headers=HX)
    assert '<optgroup label="Stockpiles">' in r.text and '<optgroup label="Resource sites">' in r.text
    assert '<optgroup label="Lagrange points">' in r.text  # deliver-to list
    assert '<option value="SOL-BELT-1-SITE-1"' in r.text and 'name="f.route.collect__custom"' in r.text
    # ferry deliver goes to another system: free text with suggestions, not a system-only list
    r = client.get("/ami/TC000001/directive-form?directive=ferry", headers=HX)
    assert 'name="f.deliver" value="" list=' in r.text
    # mining controller: resource hints from the system's belts
    r = client.get("/ami/MC91FF22/directive-form?directive=gather_resources", headers=HX)
    assert "rich" in r.text and "richest belt level in SOL" in r.text
    r = client.get("/ami/MC91FF22/directive-form?directive=gather_salvage", headers=HX)
    assert '<optgroup label="Planets">' in r.text and '<optgroup label="Resource sites">' not in r.text
    # a -SAL- code picked anyway is sent as its body
    from rsweb.web import directive_body
    assert directive_body({"directive": "gather_salvage", "f.location": "SOL-3-1-SAL-1"})["configuration"]["location"] == "SOL-3-1"
    # the whole AMI page renders with per-controller targets
    page = client.get("/ami", headers=H).text
    assert "known targets in SOL" in page and "Refresh targets" in page
    # typed code overrides the list
    r = client.post("/ami/TC000001/directive", data={"directive": "shuttle", "f.collect": "SOL-BELT-1",
                                                     "f.collect__custom": "sol-belt-1-site-1", "f.deliver": "SOL-3-L4"}, headers=HX)
    assert "result ok" in r.text
    acts = client.get("/account", headers=H).text
    assert "&#34;collect&#34;: &#34;SOL-BELT-1-SITE-1&#34;" in acts


def test_carrier_plan_orders_steps():
    from rsweb.carrier import plan
    rows = [
        {"code": "A", "carried": True, "can_launch": True, "can_stow": False, "can_recall": False, "same_loc": True, "mobile": True, "location": "X-1"},
        {"code": "B", "carried": False, "can_launch": False, "can_stow": True, "can_recall": True, "same_loc": True, "mobile": True, "location": "X-1"},
        {"code": "C", "carried": False, "can_launch": False, "can_stow": True, "can_recall": True, "same_loc": False, "mobile": True, "location": "X-2"},
        {"code": "D", "carried": False, "can_launch": False, "can_stow": True, "can_recall": False, "same_loc": False, "mobile": False, "location": "X-3"},
        {"code": "E", "carried": False, "can_launch": False, "can_stow": False, "can_recall": True, "same_loc": False, "mobile": True, "location": "X-4"},
    ]
    steps = plan("V", "X-1", rows, {"A"}, {"B", "C", "D"}, {"C", "E"}, "/devices/V", return_after=True)
    descs = [s["desc"] for s in steps]
    assert descs == ["launch A", "recall E", "stow B in V", "C → X-1 (to be picked up)", "wait for C to reach X-1",
                     "stow C in V", "vessel → X-3 (pick-up)", "stow D in V", "vessel → X-1 (return)"]
    assert steps[4]["method"] == "WAIT" and steps[4]["seq0_from"] == 3


def test_carrier_card_and_pickup_with_vessel_move(client):
    world = client.app.state.api.http._transport.app.state.world
    world.move_seconds = 0.03
    client._seen = len(world.events)
    page = client.get("/devices/11ADA230", headers=H).text
    assert "Carrier · devices in SOL" in page
    assert 'name="launch" value="SV000001"' in page            # carried → launch
    assert 'name="stow" value="2AC61213"' in page              # mining drone here → stow
    assert 'name="stow" value="SP000001"' in page and "vessel will go and pick it up" in page  # immobile, elsewhere
    assert 'name="stow" value="D8C2A140"' in page and "will fly here" in page                 # mobile, elsewhere
    r = client.post("/devices/11ADA230/carrier", data={"stow": ["2AC61213", "D8C2A140", "SP000001"],
                                                       "launch": ["SV000001"], "return_after": "on"}, headers=HX)
    assert "follow on Automations" in r.text, r.text
    eng = client.app.state.worker.automations

    async def finished():
        return all(j["status"] not in ("running", "waiting") for j in await eng.jobs())
    _pump(client, finished)
    job = client.portal.call(eng.jobs)[-1]
    assert job["status"] == "done", [(s["desc"], s["status"], s["error"]) for s in job["steps"]]
    assert all(s["status"] == "done" for s in job["steps"]), [(s["desc"], s["status"], s["error"]) for s in job["steps"]]
    stowed = {d["device_code"] for d in world.devices if d["status"] == "stowed"}
    assert {"2AC61213", "D8C2A140", "SP000001"} <= stowed and "SV000001" not in stowed
    host = next(d for d in world.devices if d["device_code"] == "11ADA230")
    assert host["location"] == "SOL-BELT-1"  # went to SOL-3 for the surge plate and came back


def test_carrier_rejects_over_capacity(client):
    world = client.app.state.api.http._transport.app.state.world
    next(d for d in world.devices if d["device_code"] == "11ADA230")["stow_capacity"] = 2
    r = client.post("/devices/11ADA230/carrier", data={"stow": ["2AC61213", "D8C2A140"]}, headers=HX)
    assert "holds 2" in r.text


def test_build_tree_nesting():
    from rsweb.tree import build_tree
    devs = [
        {"device_code": "V", "device_type": "heaven_vessel", "location": "A-BELT-1", "status": "stationary"},
        {"device_code": "C", "device_type": "cargo_vessel", "location": "A-3-L4", "status": "idle"},
        {"device_code": "S1", "device_type": "survey_drone", "location": "A-BELT-1", "status": "stowed"},
        {"device_code": "S2", "device_type": "surge_plate", "location": "A-3-L4", "status": "stowed"},     # guessed into C
        {"device_code": "M", "device_type": "mining_drone", "location": "A-BELT-1", "status": "mining (rares)"},
        {"device_code": "X", "device_type": "mining_drone", "location": "B-BELT-1", "status": "idle"},
        {"device_code": "Q", "device_type": "probe", "location": "B-2", "status": "stowed"},               # no carrier known
    ]
    reps = {"R1": {"name": "bob-1", "hosted_device_code": "V", "location": "A-BELT-1",
                   "stowed_devices": [{"device_code": "S1"}, {"device_code": "NEW1", "device_type": "replicant_matrix"}]}}
    systems = build_tree(devs, reps, {"C": []}, {"V", "C"})
    a = next(s for s in systems if s["star"] == "A")
    assert systems[0]["star"] == "A"  # system with the replicant first
    top = {n["d"]["device_code"]: n for n in a["nodes"]}
    assert set(top) == {"V", "C", "M"}
    assert {k["d"]["device_code"] for k in top["V"]["children"]} == {"S1", "NEW1"}  # NEW1 known only from the replicant
    assert top["V"]["replicant"]["name"] == "bob-1"
    assert [k["d"]["device_code"] for k in top["C"]["children"]] == ["S2"] and top["C"]["children"][0]["guessed"]
    b = next(s for s in systems if s["star"] == "B")
    assert [n["d"]["device_code"] for n in b["unknown"]] == ["Q"] and b["counts"]["idle"] == 1
    # system → device type → devices; the replicant's host type first
    assert [g["type"] for g in a["groups"]] == ["heaven_vessel", "cargo_vessel", "mining_drone"]
    assert a["groups"][2]["counts"] == {"active": 1}


def test_tree_groups_many_devices_by_type():
    from rsweb.tree import build_tree
    devs = [{"device_code": f"M{i}", "device_type": "mining_drone", "location": "A-BELT-1",
             "status": "idle" if i % 3 == 0 else "mining (carbon)", "operational_capacity": 30 if i == 1 else 90} for i in range(9)]
    devs += [{"device_code": "T1", "device_type": "transport", "location": "A-3", "status": "idle"}]
    a = build_tree(devs, {}, {}, set())[0]
    mining = next(g for g in a["groups"] if g["type"] == "mining_drone")
    assert len(mining["nodes"]) == 9 and mining["counts"] == {"idle": 3, "active": 6, "low": 1}
    assert [g["type"] for g in a["groups"]] == ["mining_drone", "transport"]


def test_tree_page_renders_with_stowed_and_commands(client):
    client.portal.call(client.app.state.worker.sync_devices)  # also builds the stowed map
    page = client.get("/tree", headers=H).text
    assert "Expand all" in page and "Collapse all" in page and "Systems only" in page
    assert 'id="ts-SOL"' in page and 'id="t-11ADA230"' in page
    # devices sit under a type group, and nothing starts open
    grp = page[page.index('id="tg-SOL-mining_drone"'):]
    assert "mining drone" in grp[:400] and 'id="t-' in grp[:6000]
    assert "<details open" not in page and " open>" not in page
    # the vessel's stowed devices are nested inside its node
    vessel = page[page.index('id="t-11ADA230"'):]
    assert "Stowed in 11ADA230" in vessel and 'id="t-SV000001"' in vessel.split('id="t-2AC61210"')[0]
    # commands panel with the device's own commands, results go to its own box
    assert 'hx-get="/devices/2AC61212/command-form"' in page and 'id="tres-2AC61212"' in page
    r = client.get("/devices/TR000001/command-form?command=collect_resources&rid=tres-TR000001", headers=HX)
    assert 'hx-target="#tres-TR000001"' in r.text


def test_planner_queues_autofactory_and_gathers_shortfall(client):
    world = client.app.state.api.http._transport.app.state.world
    world.queues["AF00BEEF"] = []
    world.devices[7]["status"] = "printing (survey_drone)"  # busy, so new items stay queued
    page = client.get("/blueprints", headers=H).text
    # autofactory listed first as printer
    sel = page[page.index('name="printer"'):]
    assert sel.index("AF00BEEF") < sel.index("(vessel)")
    data = {"printer": "device:AF00BEEF", "qty:mining_drone": "3"}  # autofactory at SOL-3-L4 has no conductive
    r = client.post("/blueprints/plan", data=data, headers=HX)
    assert "Gather the shortfall with" in r.text and "MC91FF22" in r.text
    assert "Move it from SOL-BELT-1 to SOL-3-L4 with" in r.text and "TC000001" in r.text
    r = client.post("/blueprints/queue-plan", data={**data, "_planned": "1", "gather": "on", "deliver": "on",
                                                    "mining": "MC91FF22", "transport": "TC000001"}, headers=HX)
    assert "follow on Automations" in r.text, r.text
    job = client.portal.call(client.app.state.worker.automations.jobs)[-1]
    assert job["status"] == "done", [(s["desc"], s["status"], s["error"]) for s in job["steps"]]
    bodies = [(s["path"], s["body"]) for s in job["steps"]]
    assert bodies[0] == ("/devices/AF00BEEF", {"command": "enqueue_print", "device_type": "mining_drone", "quantity": 3})
    gather = next(b for p, b in bodies if p == "/devices/MC91FF22" and b["command"] == "set_directive")
    assert gather["directive"] == "gather_resources" and gather["configuration"]["conductive"] == 150
    assert "structural" not in gather["configuration"] or gather["configuration"]["structural"] <= 300
    deliver = next(b for p, b in bodies if p == "/devices/TC000001" and b["command"] == "set_directive")
    assert deliver["configuration"]["route"] == {"collect": "SOL-BELT-1", "deliver": "SOL-3-L4"}
    assert [i["device_type"] for i in world.queues["AF00BEEF"]] == ["mining_drone"] * 3
    # unticking gather survives a plan refresh
    r = client.post("/blueprints/plan", data={**data, "_planned": "1"}, headers=HX)
    assert 'name="gather" >' in r.text or 'name="gather" ' in r.text and 'name="gather" checked' not in r.text


def test_planner_without_mining_controller_warns():
    from rsweb.production import production_steps
    steps = production_steps("AF1", "autofactory AF1", [("mining_drone", 2)], {"rares": 10}, None, None, "X-3-L4", True)
    assert [s["body"]["command"] for s in steps] == ["enqueue_print"]


def _arrive(client, n):
    client.portal.call(client.app.state.worker.handle_event, {
        "id": f"66666666666{n:02d}-0", "event": "travel.arrived", "device_code": "11ADA230", "device_type": "heaven_vessel",
        "location": "SOL-BELT-1", "payload": {"destination": "SOL-BELT-1", "origin": "ABOTEIN-OORT", "travel_type": "surge"}, "created_at": "2026-09-30T12:00:00+00:00"})


def _beacon_jobs(client):
    return [j for j in client.portal.call(client.app.state.worker.automations.jobs) if j["rule"] == "deploy_beacon"]


def test_beacon_deployed_once_per_system(client):
    client.post("/automations/rules/deploy_beacon", data={"enabled": "on", "count_others": "on"}, headers=HX)
    _arrive(client, 1)
    assert len(_beacon_jobs(client)) == 1
    # the device list hasn't caught up yet, and the vessel keeps arriving at places in the system
    _arrive(client, 2)
    _arrive(client, 3)
    assert len(_beacon_jobs(client)) == 1


def test_beacon_skipped_when_one_is_already_there(client):
    world = client.app.state.api.http._transport.app.state.world
    world.devices.append({"device_code": "FB999999", "device_type": "ftl_beacon", "location": "SOL-5", "status": "monitoring",
                          "replicant_code": "77F75255", "features": ["monitor"], "available_commands": []})
    client.post("/automations/rules/deploy_beacon", data={"enabled": "on", "count_others": "on"}, headers=HX)
    _arrive(client, 4)
    assert _beacon_jobs(client) == []


def test_beacon_skipped_for_another_players_beacon(client):
    world = client.app.state.api.http._transport.app.state.world
    world.foreign_devices.append({"device_code": "OTHER001", "device_type": "ftl_beacon", "location": "SOL-2",
                                  "owner_replicant_code": "4A1F0B22", "owner_name": "helga-3"})
    client.post("/automations/rules/deploy_beacon", data={"enabled": "on", "count_others": "on"}, headers=HX)
    _arrive(client, 5)
    assert _beacon_jobs(client) == []
    # with the option off, someone else's beacon doesn't count
    client.post("/automations/rules/deploy_beacon", data={"enabled": "on"}, headers=HX)
    _arrive(client, 6)
    assert len(_beacon_jobs(client)) == 1


def test_ami_schedule_add_run_and_idle_check(client):
    page = client.get("/automations", headers=H).text
    assert "AMI schedules" in page and 'value="kind:mining"' in page
    r = client.get("/automations/schedule-form?target=TC000001", headers=HX)
    assert '<option value="delivery">' in r.text or 'value="shuttle"' in r.text
    # transport controller TC000001 is idle; TR000001 is an idle transport drone at the same location
    r = client.post("/automations/schedules", data={"target": "TC000001", "directive": "shuttle", "f.collect": "SOL-BELT-1",
                                                    "f.deliver": "SOL-3-L4", "every_minutes": "15", "only_idle": "on",
                                                    "adopt": "on", "launch": "on", "name": "belt shuttle"}, headers=HX)
    assert r.headers.get("HX-Refresh") == "true", r.text
    eng = client.app.state.worker.automations
    sched = client.portal.call(eng.schedules)[0]
    assert sched["configuration"] == {"collect": "SOL-BELT-1", "deliver": "SOL-3-L4"} and sched["every_minutes"] == 15
    r = client.post(f"/automations/schedules/{sched['id']}/run", headers=HX)
    assert "TC000001: started, adopting 1" in r.text, r.text
    job = client.portal.call(eng.jobs)[-1]
    assert [s["body"]["command"] for s in job["steps"]] == ["adopt", "set_directive", "launch"]
    assert job["steps"][0]["body"]["devices"] == ["TR000001"] and job["status"] == "done"
    # the mining controller is coordinating -> skipped while busy, run once its directive completes
    client.post("/automations/schedules", data={"target": "kind:mining", "directive": "gather_evenly", "every_minutes": "30",
                                                "only_idle": "on", "launch": "on"}, headers=HX)
    msched = client.portal.call(eng.schedules)[1]
    r = client.post(f"/automations/schedules/{msched['id']}/run", headers=HX)
    assert "MC91FF22: busy" in r.text
    client.portal.call(client.app.state.worker.handle_event, {"id": "5555555555555-0", "event": "directive.completed",
        "device_code": "MC91FF22", "device_type": "ami_mining_controller", "payload": {"directive": "gather_evenly"},
        "created_at": "2026-09-30T12:00:00+00:00"})
    r = client.post(f"/automations/schedules/{msched['id']}/run", headers=HX)
    assert "MC91FF22: started" in r.text and "last directive completed" in r.text


def test_ami_schedules_need_master_switch_and_respect_interval(client):
    eng = client.app.state.worker.automations
    client.post("/automations/schedules", data={"target": "TC000001", "directive": "consolidate", "f.deliver": "SOL-3-L4",
                                                "every_minutes": "60", "launch": "on"}, headers=HX)
    client.portal.call(eng.tick)
    assert client.portal.call(eng.schedules)[0]["last_run"] is None  # master switch off
    client.post("/automations/rules/ami_schedules", data={"enabled": "on"}, headers=HX)
    client.portal.call(eng.tick)
    first = client.portal.call(eng.schedules)[0]["last_run"]
    assert first
    client.portal.call(eng.tick)
    assert client.portal.call(eng.schedules)[0]["last_run"] == first  # not due again for an hour


def test_auto_survey_uses_carried_ami_survey_controller(client):
    world = client.app.state.api.http._transport.app.state.world
    world.devices.append({"device_code": "SC000001", "device_type": "ami_survey_controller", "location": "SOL-BELT-1",
                          "status": "stowed", "replicant_code": "77F75255", "features": ["cruise", "ami", "stow"],
                          "available_commands": ["deploy", "adopt", "set_directive", "launch", "stow"]})
    world.move_seconds = 0.03
    client._seen = len(world.events)
    client.post("/automations/rules/auto_survey", data={"enabled": "on", "use_ami": "on", "include_belts": "on",
                                                        "max_targets": "5", "return_and_stow": "on"}, headers=HX)
    _arrive(client, 7)
    eng = client.app.state.worker.automations

    async def finished():
        return all(j["status"] not in ("running", "waiting") for j in await eng.jobs())
    _pump(client, finished)
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "auto_survey"][-1]
    assert job["meta"].get("ami") and job["status"] == "done", [(s["desc"], s["status"], s["error"]) for s in job["steps"]]
    cmds = [(s["path"], s["body"]["command"]) for s in job["steps"]]
    assert cmds[0] == ("/devices/SC000001", "deploy") and ("/devices/SV000001", "deploy") in cmds
    final = job["steps"][-2]["body"]
    assert final["directive"] == "survey_system" and final["configuration"] == {"planets": "all", "moons": "none", "recall": True}


def test_idle_miners_handed_to_mining_controller(client):
    eng = client.app.state.worker.automations
    client.post("/automations/rules/restart_idle_miners", data={"enabled": "on", "prefer_ami": "on", "resource": "same",
                                                                "cooldown_minutes": "10"}, headers=HX)
    client.portal.call(eng.tick)
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "restart_idle_miners"][-1]
    assert job["steps"][0]["body"] == {"command": "adopt", "devices": ["2AC61212"]}
    assert len(job["steps"]) == 1  # controller is coordinating: adopt only, no relaunch
    acts = client.portal.call(client.app.state.db.fetchall, "SELECT body FROM actions WHERE user='automation'")
    assert not any('"start_mining"' in (a["body"] or "") for a in acts)


def _lo_world():
    def dev(code, t, loc, status="idle", **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": status,
                "features": kw.pop("features", ["cruise", "stow"]), "available_commands": kw.pop("cmds", ["travel", "stow", "deploy"]),
                "operational_capacity": 100.0, **kw}
    devices = [
        dev("A1", "mining_drone", "AAA-BELT-1"), dev("A2", "mining_drone", "AAA-BELT-1", "mining (carbon)"),
        dev("A3", "mining_drone", "AAA-BELT-1"), dev("A4", "mining_drone", "AAA-BELT-1", "mining (rares)"),
        dev("AC", "ami_mining_controller", "AAA-BELT-1", "coordinating"),
        dev("CAR", "surge_carrier", "AAA-OORT", features=["surge", "cruise"], cmds=["travel", "deploy"], stow_capacity=9),
        dev("AF", "autofactory", "AAA-3-L4", cmds=["enqueue_print", "dequeue_print", "clear_queue"], features=["print"]),
        dev("BS", "survey_drone", "BBB-2"),
        dev("CC", "ami_mining_controller", "CCC-BELT-1", tags=["spare"]),
        dev("CK", "mining_drone", "CCC-BELT-1", tags=["spare", "keep"]),
        dev("HV", "heaven_vessel", "AAA-BELT-1", features=["surge", "cruise", "print"], stow_capacity=10),
    ]
    cfg = {"phases": [{"id": "outpost", "name": "Outpost", "order": 1,
                       "wants": {"mining_drone": 2, "ami_mining_controller": 1, "survey_drone": 2}}],
           "systems": {"AAA": "outpost", "BBB": "outpost"}, "ignore_tags": ["keep"]}
    bps = [{"device_type": "survey_drone", "resources": {"structural": 100}, "print_time": 240},
           {"device_type": "mining_drone", "resources": {"structural": 100}, "print_time": 180},
           {"device_type": "ami_mining_controller", "resources": {"rares": 500}, "print_time": 600}]
    inv = {"AAA-3-L4": {"structural": 250.0}}
    stars = {"AAA": {"designation": "AAA", "position": {"x": 0, "y": 0, "z": 0}, "entry_point": "AAA-OORT"},
             "BBB": {"designation": "BBB", "position": {"x": 3, "y": 0, "z": 0}, "entry_point": "BBB-5-L4"},
             "CCC": {"designation": "CCC", "position": {"x": 9, "y": 0, "z": 0}}}
    return cfg, devices, bps, inv, stars


def test_loadout_plan_spares_prints_and_carriers():
    from rsweb import loadouts as lo
    cfg, devices, bps, inv, stars = _lo_world()
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    a, b = p["report"]["AAA"], p["report"]["BBB"]
    assert {r["type"]: (r["have"], r["short"], r["surplus"]) for r in a["rows"]} == {
        "mining_drone": (4, 0, 2), "ami_mining_controller": (1, 0, 0), "survey_drone": (0, 2, 0)}
    # the two idle drones are the extras, and they go to BBB (which has none)
    assert sorted(p["moves"][c] for c in ("A1", "A3")) == ["BBB", "BBB"]
    assert p["moves"]["CC"] == "BBB"           # spare controller from the unphased system
    assert "CK" not in p["moves"]              # ignored tag: never moved
    dl = p["deliveries"]
    assert len(dl) == 1 and dl[0]["carrier"] == "CAR" and sorted(dl[0]["devices"]) == ["A1", "A3"]
    assert any("carrier in CCC" in u["why"] for u in p["unmet"])   # nothing can carry CC yet
    # survey drones: 2 short in AAA and in BBB, stock covers 2 prints at AF in total
    assert sum(pr["n"] for pr in p["prints"]) == 2 and all(pr["factory"] == "AF" for pr in p["prints"])
    assert any(u["type"] == "survey_drone" for u in p["unmet"])
    steps = lo.delivery_steps(dl[0], p["by_code"], stars, True)
    bodies = [(s["path"], s["body"]) for s in steps]
    assert ("/devices/A1", {"configuration": {"add_tags": ["to:bbb"]}}) in bodies
    assert ("/devices/CAR", {"command": "attach", "device": "A1"}) in bodies   # the carrier attaches the cargo
    assert ("/devices/CAR", {"command": "travel", "destination": "BBB-5-L4"}) in bodies
    assert ("/devices/CAR", {"command": "detach", "device": "A3"}) in bodies
    board = next(st for st in steps if st["body"] == {"command": "attach", "device": "A1"})
    assert board["critical"]          # no boarding → the carrier doesn't fly off without it
    assert ("/devices/A3", {"configuration": {"add_tags": ["home:bbb"], "remove_tags": ["to:bbb"]}}) in bodies
    assert bodies[-1] == ("/devices/CAR", {"command": "travel", "destination": "AAA-OORT"})
    pr = lo.print_steps(p["prints"][0])[0]["body"]
    assert pr["command"] == "enqueue_print" and pr["tags"][0].startswith("to:")
    assert "HV" not in p["by_code"]            # the replicant's vessel is never counted or used


def test_loadout_incoming_and_arrivals_and_unspare():
    from rsweb import loadouts as lo
    cfg, devices, bps, inv, stars = _lo_world()
    # A1 and A3 already on their way; a survey drone was ordered for BBB; BS arrived tagged to:bbb
    for d in devices:
        if d["device_code"] in ("A1", "A3"):
            d["tags"] = ["to:bbb"]
        if d["device_code"] == "BS":
            d["tags"] = ["to:bbb", "spare"]
    orders = [{"star": "BBB", "device_type": "survey_drone", "factory": "AF", "at": "2026-01-01T00:00:00+00:00"}]
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), orders, {})
    b = {r["type"]: r for r in p["report"]["BBB"]["rows"]}
    assert b["mining_drone"]["incoming"] == 2 and b["mining_drone"]["short"] == 0
    assert b["survey_drone"]["incoming"] == 1 and b["survey_drone"]["short"] == 0
    assert "BS" in p["arrived"]
    steps = lo.arrived_steps("BS", p["by_code"]["BS"], {})
    assert steps[-1]["body"] == {"configuration": {"add_tags": ["home:bbb"], "remove_tags": ["spare", "to:bbb"]}}
    # AAA now has exactly 2 drones locally (A2, A4) — nothing more is marked spare there
    a = {r["type"]: r for r in p["report"]["AAA"]["rows"]}
    assert a["mining_drone"]["have"] == 2 and a["mining_drone"]["surplus"] == 0
    # a spare in a system that becomes short loses the tag
    for d in devices:
        if d["device_code"] == "A2":
            d["tags"] = ["spare"]
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), orders, {})
    assert p["tag_remove"].get("A2") == ["spare"]


def test_loadouts_page_and_apply_against_mock(client):
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    assert "Loadouts" in client.get("/", headers=H).text
    r = client.post("/loadouts/phases", data={"new_phase": "Mining hub"}, headers=HX)
    assert r.headers.get("HX-Refresh")
    cfg = client.portal.call(client.app.state.db.kv_get, "loadouts")
    pid = cfg["phases"][0]["id"]
    client.post("/loadouts/phases", data={f"name:{pid}": "Mining hub", f"order:{pid}": "1",
                                          f"want:{pid}:mining_drone": "2", f"want:{pid}:survey_drone": ""}, headers=HX)
    client.post("/loadouts/system", data={"star": "SOL", "phase": pid}, headers=HX)
    client.post("/loadouts/settings", data={"ignore_tags": "keep, Reserve", "print_missing": "on", "need_stock": "on"}, headers=HX)
    cfg = client.portal.call(client.app.state.db.kv_get, "loadouts")
    assert cfg["phases"][0]["wants"] == {"mining_drone": 2} and cfg["ignore_tags"] == ["keep", "reserve"]
    page = client.get("/loadouts", headers=H).text
    assert "Mining hub" in page and "2 spare" in page and "as spare" in page
    r = client.post("/loadouts/apply", data={"star": "SOL"}, headers=HX)
    assert "Applied to SOL" in r.text
    spares = [d["device_code"] for d in world.devices if "spare" in (d.get("tags") or [])]
    assert len(spares) == 2 and all(c.startswith("2AC6121") for c in spares)
    # survey drones aren't in the phase: untouched
    assert not any("spare" in (d.get("tags") or []) for d in world.devices if d["device_type"] == "survey_drone")
    assert "◆ Mining hub" in client.get("/tree", headers=H).text


def test_site_quantity_shapes():
    from rsweb.targets import site_quantity
    assert site_quantity({"resource_type": "carbon", "quantity": 120}) == ({"carbon": 120.0}, 120.0)
    assert site_quantity({"resource": "rares", "remaining": "40"}) == ({"rares": 40.0}, 40.0)
    assert site_quantity({"resources": {"structural": 10, "carbon": 5}}) == ({"structural": 10.0, "carbon": 5.0}, 15.0)
    assert site_quantity({"resources": [{"resource": "silicates", "quantity": 7}]}) == ({"silicates": 7.0}, 7.0)
    assert site_quantity({"resource_type": "carbon"}) == ({}, None)


def test_system_resources_and_map_places(client):
    from rsweb.targets import system_resources
    client.portal.call(client.app.state.worker.sync_devices)
    world = client.app.state.api.http._transport.app.state.world
    for ev in [e for e in world.events if e["event"] == "salvage.discovered"]:
        client.portal.call(client.app.state.worker.handle_event, dict(ev))
    page = client.get("/systems/SOL", headers=H).text
    assert "Resources available" in page and "Derelict hauler" in page and "SOL-3-1-SAL-1" in page
    r = client.post("/systems/SOL/resources/refresh", headers=HX)
    assert r.headers.get("HX-Refresh")
    res = client.portal.call(system_resources, client.app.state.db, "SOL")
    assert res["totals"]["structural"]["sites"] == 5200 and res["totals"]["rares"]["sites"] == 340
    assert res["totals"]["structural"]["salvage"] == 260   # location detail is newer than the discovery event
    assert res["mineable"] == 5540 and res["salvageable"] == 335
    page = client.get("/systems/SOL", headers=H).text
    assert 'class="marker place place-site' in page and 'class="marker place place-salvage' in page
    assert "SITE-1 · 5,200" in page or "SITE-1 · 5200" in page
    assert 'class="marker place place-lagrange' in page   # Lagrange points are drawn too
    lst = client.get("/systems", headers=H).text
    assert "Mineable" in lst and ("5,540" in lst or "5540" in lst)
    # a depleted site no longer counts
    client.portal.call(client.app.state.worker.handle_event, _ev(99, "site.depleted", site="SOL-BELT-1-SITE-2", location="SOL-BELT-1"))
    res = client.portal.call(system_resources, client.app.state.db, "SOL")
    assert res["mineable"] == 5200


def _salvage_setup(client, drop_controller=False):
    world = client.app.state.api.http._transport.app.state.world
    if drop_controller:
        world.devices = [d for d in world.devices if "controller" not in d["device_type"]]
    client.portal.call(client.app.state.worker.sync_devices)
    eng = client.app.state.worker.automations

    async def enable():
        s = await eng.settings()
        s["rules"]["salvage_when_depleted"]["enabled"] = True
        s["rules"]["restart_idle_miners"]["enabled"] = True
        await eng.save_settings(s)
    client.portal.call(enable)
    client.get("/systems/SOL", headers=H)
    client.post("/systems/SOL/resources/refresh", headers=HX)   # site + salvage quantities
    for ev in [e for e in world.events if e["event"] == "salvage.discovered"]:
        client.portal.call(client.app.state.worker.handle_event, dict(ev))
    return world, eng


def test_salvage_rule_switches_ami_when_belt_worked_out(client):
    world, eng = _salvage_setup(client)
    assert client.portal.call(eng.rule_salvage) == []          # sites still have stock: nothing to do
    for i, site in enumerate(("SOL-BELT-1-SITE-1", "SOL-BELT-1-SITE-2")):
        client.portal.call(client.app.state.worker.handle_event, _ev(200 + i, "site.depleted", site=site))
    jobs = [j for j in client.portal.call(eng.jobs) if j["rule"] == "salvage_when_depleted"]
    assert len(jobs) == 1 and jobs[0]["device"] == "MC91FF22"
    body = next(s["body"] for s in jobs[0]["steps"] if (s["body"] or {}).get("command") == "set_directive")
    assert body == {"command": "set_directive", "directive": "gather_salvage",
                    "configuration": {"location": "SOL-3-1", "recall": False}}   # the body, not the -SAL- code
    # no drones are sent on their own while the AMI handles the system
    assert not any(j["device"].startswith("2AC6121") for j in jobs)


def test_salvage_rule_sends_drones_without_ami(client):
    world, eng = _salvage_setup(client, drop_controller=True)
    for i, site in enumerate(("SOL-BELT-1-SITE-1", "SOL-BELT-1-SITE-2")):
        client.portal.call(client.app.state.worker.handle_event, _ev(300 + i, "site.depleted", site=site))
    jobs = [j for j in client.portal.call(eng.jobs) if j["rule"] == "salvage_when_depleted"]
    assert [j["device"] for j in jobs] == ["2AC61212"]          # the idle drone at the worked-out belt
    bodies = [s["body"] for s in jobs[0]["steps"]]
    assert bodies[0] == {"command": "travel", "destination": "SOL-3-1"}
    assert bodies[-1] == {"command": "start_mining", "resource_type": "structural"}
    # the idle-miner rule leaves drones at the worked-out belt alone
    acts = client.portal.call(client.app.state.db.fetchall, "SELECT body FROM actions WHERE body LIKE '%start_mining%'")
    client.portal.call(eng.rule_restart_idle_miners)
    after = client.portal.call(client.app.state.db.fetchall, "SELECT body FROM actions WHERE body LIKE '%start_mining%'")
    assert len(after) == len(acts)


def test_salvage_helpers():
    from rsweb import salvage as sv
    res = {"sites": [{"code": "X-BELT-1-SITE-1", "belt": "X-BELT-1", "depleted": True, "total": 50},
                     {"code": "X-BELT-2-SITE-1", "belt": "X-BELT-2", "depleted": False, "total": 10},
                     {"code": "X-BELT-2-SITE-2", "belt": "X-BELT-2", "depleted": False, "total": 0}],
           "salvage": [{"code": "X-1-SAL-1", "depleted": False, "total": 10, "amounts": {"carbon": 10}},
                       {"code": "X-2-SAL-1", "depleted": False, "total": 90, "amounts": {"rares": 60, "carbon": 30}},
                       {"code": "X-3-SAL-1", "depleted": True, "total": 500}]}
    assert sv.worked_out(res) == {"X-BELT-1"}
    assert [s["code"] for s in sv.available_salvage(res)] == ["X-2-SAL-1", "X-1-SAL-1"]
    assert sv.main_resource(sv.available_salvage(res)[0]) == "rares"
    assert sv.at_worked_out_place("X-BELT-1-SITE-1", {"X-BELT-1"}, set())
    assert sv.body_of("AEMEROTH-6-7-SAL-1") == "AEMEROTH-6-7" and sv.body_of("AEMEROTH-6-7") == "AEMEROTH-6-7"


def test_material_routes_source_to_nearest_destination():
    from rsweb import loadouts as lo
    stars = {"AAA": {"position": {"x": 0, "y": 0, "z": 0}}, "BBB": {"position": {"x": 3, "y": 0, "z": 0}, "entry_point": "BBB-5-L4"},
             "CCC": {"position": {"x": 9, "y": 0, "z": 0}}, "DDD": {"position": {"x": 20, "y": 0, "z": 0}}}
    ctrl = {"device_code": "TA", "device_type": "ami_transport_controller", "location": "AAA-BELT-1", "status": "idle",
            "available_commands": ["set_directive", "launch", "adopt"]}
    devices = [ctrl,
               {"device_code": "FR1", "device_type": "cargo_freighter", "location": "AAA-BELT-1", "status": "idle", "features": ["surge"]},
               {"device_code": "FR2", "device_type": "cargo_freighter", "location": "AAA-3-L4", "status": "idle", "features": ["surge"]},
               {"device_code": "FB", "device_type": "autofactory", "location": "CCC-3-L4", "available_commands": ["enqueue_print"]}]
    inv = {"AAA-BELT-1": {"carbon": 500}, "AAA-2": {"carbon": 10}, "BBB-4-L5": {"structural": 40}}
    cfg = {"roles": {"AAA": "source", "BBB": "destination", "CCC": "destination", "DDD": "source"}}
    routes, unmet = lo.material_routes(cfg, devices, inv, stars, set(), {}, {})
    assert len(routes) == 1
    r = routes[0]
    assert (r["controller"], r["dest"], r["collect"], r["deliver"]) == ("TA", "BBB", "AAA-BELT-1", "BBB-4-L5")
    assert [a["code"] for a in r["adopt"]] == ["FR1", "FR2"] and r["tag"]
    assert any(u["star"] == "DDD" and "transport controller" in u["why"] for u in unmet)
    bodies = [st["body"] for st in lo.ferry_steps(r)]
    assert bodies[0] == {"configuration": {"add_tags": ["ferry"]}}
    assert {"command": "travel", "destination": "AAA-BELT-1"} in bodies          # FR2 flies over to join
    assert {"command": "adopt", "devices": ["FR1", "FR2"]} in bodies
    assert {"command": "set_directive", "directive": "ferry",
            "configuration": {"collect": "AAA-BELT-1", "deliver": "BBB-4-L5"}} in bodies
    # running the same ferry with all freighters adopted → nothing to do; a new idle freighter → adopt only
    ctrl["tags"] = ["ferry"]
    managed = {"FR1": "TA", "FR2": "TA"}
    cur = {"TA": {"directive": "ferry", "configuration": {"collect": "AAA-BELT-1", "deliver": "BBB-4-L5"}, "finished": False}}
    assert lo.material_routes(cfg, devices, inv, stars, set(), cur, managed)[0] == []
    devices.append({"device_code": "FR3", "device_type": "cargo_freighter", "location": "AAA-BELT-1", "status": "idle"})
    r = lo.material_routes(cfg, devices, inv, stars, set(), cur, managed)[0][0]
    assert [a["code"] for a in r["adopt"]] == ["FR3"] and not r["resend"] and not r["tag"]
    assert not any(st["body"] and st["body"].get("command") == "set_directive" for st in lo.ferry_steps(r))
    # the only controller is on in-system work (consolidate) → it is not given the ferry
    devices2 = [dict(ctrl, tags=[]), {"device_code": "TD1", "device_type": "transport_drone", "location": "AAA-BELT-1", "status": "idle"},
                devices[1]]
    cur2 = {"TA": {"directive": "consolidate", "configuration": {}, "finished": False}}
    routes, unmet = lo.material_routes(cfg, devices2, inv, stars, set(), cur2, {"TD1": "TA"})
    assert routes == [] and any("in-system work" in u["why"] for u in unmet)
    # a controller already ferrying with drones on taxi plates is fine to keep (the game's own ferry does that)
    cur3 = {"TA": {"directive": "ferry", "configuration": {"collect": "X", "deliver": "Y"}, "finished": False}}
    routes, _ = lo.material_routes(cfg, devices2, inv, stars, set(), cur3, {"TD1": "TA"})
    assert routes and routes[0]["controller"] == "TA" and routes[0]["resend"]
    # no freighter at all → says so
    routes, unmet = lo.material_routes(cfg, [dict(ctrl, tags=[])], inv, stars, set(), {}, {})
    assert routes == [] and any("no cargo freighter" in u["why"] for u in unmet)


def test_ferry_controller_kept_out_of_in_system_work():
    from rsweb import production
    from rsweb.ami_schedule import adoptable, targets_of
    devs = [{"device_code": "TF", "device_type": "ami_transport_controller", "location": "A-1", "tags": ["ferry"], "features": ["ami"]},
            {"device_code": "TI", "device_type": "ami_transport_controller", "location": "A-1", "features": ["ami"]},
            {"device_code": "TD", "device_type": "transport_drone", "location": "A-1", "status": "idle"}]
    assert [c["device_code"] for c in production.controllers_in(devs, "A", "transport")] == ["TI"]
    assert [c["device_code"] for c in targets_of({"target": "kind:transport"}, devs)] == ["TI"]
    assert adoptable(devs, devs[0], {}) == [] and adoptable(devs, devs[1], {}) == ["TD"]


def test_loadout_roles_page(client):
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(client.app.state.worker.sync_inventory)
    r = client.post("/loadouts/role", data={"star": "SOL", "role": "source"}, headers=HX)
    assert r.headers.get("HX-Refresh")
    page = client.get("/loadouts", headers=H).text
    assert 'value="source" selected' in page and "materials from SOL: no destination system set" in page


def test_arrival_rules_ignore_in_system_hops_and_surveyed_systems(client):
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.worker.sync_devices)
    client.get("/systems/SOL", headers=H)  # cache the scan
    client.post("/automations/rules/auto_survey", data={"enabled": "on", "use_idle": "on", "include_belts": "on",
                                                        "max_targets": "20", "use_ami": "on"}, headers=HX)

    def arrive(n, **payload):
        client.portal.call(client.app.state.worker.handle_event, {
            "id": f"77777777777{n:02d}-0", "event": "travel.arrived", "device_code": "TC000001", "device_type": "transport",
            "location": "SOL-BELT-1", "payload": {"destination": "SOL-BELT-1", **payload}, "created_at": "2026-09-30T12:00:00+00:00"})

    def survey_jobs():
        return [j for j in client.portal.call(eng.jobs) if j["rule"] == "auto_survey"]

    arrive(1, origin="SOL-3")                     # a transport hopping inside SOL: nothing happens
    assert survey_jobs() == []
    # an AMI survey controller finishing survey_system marks every body in SOL as surveyed …
    client.portal.call(client.app.state.worker.handle_event, {
        "id": "7777777777799-0", "event": "directive.completed", "device_code": "SC000001", "device_type": "ami_survey_controller",
        "location": "SOL-BELT-1", "star": "SOL", "payload": {"directive": "survey_system"}, "created_at": "2026-09-30T12:00:00+00:00"})
    surveyed = client.portal.call(client.app.state.db.kv_get, "surveyed")
    assert {"SOL-3", "SOL-5", "SOL-BELT-1"} <= set(surveyed)
    # … so even a surge arrival from another system doesn't start a survey there again
    arrive(2, origin="ABOTEIN-OORT", travel_type="surge")
    assert survey_jobs() == []


def test_arrived_from_elsewhere(client):
    eng = client.app.state.worker.automations
    f = lambda p: client.portal.call(eng.arrived_from_elsewhere, {"device_code": "X1", "payload": p}, "SOL")  # noqa: E731
    assert f({"origin": "SOL-3"}) is False
    assert f({"origin": "ABOTEIN-OORT"}) is True
    assert f({"travel_type": "surge_hop"}) is True
    assert f({}) is False      # no origin known at all: treated as a local move



def test_loadout_home_tags_keep_devices_counted_while_away():
    from rsweb import loadouts as lo
    cfg, devices, bps, inv, stars = _lo_world()
    cfg["phases"][0]["wants"]["surge_carrier"] = 1
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    # first pass: everything counted for a phased system gets its home tag
    assert "home:aaa" in p["tag_add"]["CAR"] and "home:aaa" in p["tag_add"]["AC"]
    assert "home:bbb" in p["tag_add"]["BS"]
    assert any("home:aaa" in line for line in lo.describe(p))
    # tag them, then send the carrier off to BBB on a delivery
    for d in devices:
        if d["device_code"] in p["tag_add"] and d["device_code"] not in p["moves"]:
            d["tags"] = sorted(set(d.get("tags") or []) | {t for t in p["tag_add"][d["device_code"]] if t.startswith("home:")})
        if d["device_code"] == "CAR":
            d["location"], d["status"] = "BBB-5-L4", "idle"
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    a = {r["type"]: r for r in p["report"]["AAA"]["rows"]}
    assert a["surge_carrier"]["have"] == 1 and a["surge_carrier"]["short"] == 0 and a["surge_carrier"]["away"] == ["CAR"]
    b = {r["type"]: r for r in p["report"]["BBB"]["rows"]}
    assert "surge_carrier" not in b or b["surge_carrier"]["have"] == 0   # not counted (or made spare) where it's visiting
    assert "spare" not in p["tag_add"].get("CAR", [])



def test_job_stops_after_repeated_failures(client):
    eng = client.app.state.worker.automations
    from rsweb.automations import step
    steps = [step(f"travel {i}", "/devices/NOPE0000", {"command": "travel", "destination": f"SOL-{i}"}) for i in range(10)]
    job = client.portal.call(eng.create_job, "auto_survey", "doomed", "NOPE0000", steps, {}, True)
    job = next(j for j in client.portal.call(eng.jobs) if j["id"] == job["id"])
    assert job["status"] == "failed" and sum(1 for st in job["steps"] if st["status"] == "skipped") == 3


def test_ami_survey_not_restarted_on_every_arrival(client):
    eng = client.app.state.worker.automations

    async def go():
        started = {"SOL": __import__("rsweb.db", fromlist=["now_iso"]).now_iso()}
        await client.app.state.db.kv_set("ami_survey_started", started)
        await eng.rule_auto_survey("11ADA230", "SOL-BELT-1", "SOL", [], {"use_ami": True})
        return [j for j in await eng.jobs() if j["rule"] == "auto_survey"]
    assert client.portal.call(go) == []


def test_print_completed_tags_new_device_for_its_system(client):
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(client.app.state.db.kv_set, "loadout_orders",
                       [{"star": "SOL", "device_type": "survey_drone", "factory": "AF00BEEF", "at": "2026-10-01T00:00:00+00:00"}])
    world.devices.append({"device_code": "NEW00001", "device_type": "survey_drone", "location": "SOL-3-L4", "status": "idle",
                          "features": [], "available_commands": []})
    client.portal.call(client.app.state.worker.handle_event,
                       {"id": "5555555555555-0", "event": "print.completed", "device_code": "AF00BEEF", "device_type": "autofactory",
                        "location": "SOL-3-L4", "payload": {"device_type": "survey_drone", "new_device_code": "NEW00001", "tags": []},
                        "created_at": "2026-10-01T00:00:00+00:00"})
    assert "to:sol" in next(d for d in world.devices if d["device_code"] == "NEW00001")["tags"]
    assert client.portal.call(client.app.state.db.kv_get, "loadout_orders")[0]["device_code"] == "NEW00001"


def test_spare_devices_drop_their_home():
    from rsweb import loadouts as lo
    cfg, devices, bps, inv, stars = _lo_world()
    cfg["systems"] = {"AAA": "outpost"}            # nobody is short, so AAA's extras just become spare
    for d in devices:
        if d["device_code"] in ("A1", "A2", "A3", "A4"):
            d["tags"] = ["home:aaa"]
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    extras = [c for c, t in p["tag_add"].items() if "spare" in t]
    assert sorted(extras) == ["A1", "A3"]
    assert all("home:aaa" in p["tag_remove"][c] for c in extras)
    assert "home:aaa" not in p["tag_add"].get("A1", [])
    # next pass: the spares (no home) aren't counted for AAA, aren't re-homed, and aren't made spare again
    for d in devices:
        if d["device_code"] in extras:
            d["tags"] = ["spare"]
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    row = next(r for r in p["report"]["AAA"]["rows"] if r["type"] == "mining_drone")
    assert row["have"] == 2 and row["surplus"] == 0 and sorted(row["spares_here"]) == ["A1", "A3"]
    assert not any(c in p["tag_add"] for c in extras)


def test_managed_by_uses_controller_device_code(client):
    from rsweb.ami_schedule import managed_by
    db = client.app.state.db

    async def go():
        await db.kv_set("devices", [
            {"device_code": "57C506F0", "device_type": "cargo_freighter", "controller_device_code": "DF451241",
             "features": ["surge", "cruise", "transport"], "cargo_capacity": 500, "cargo_used": 500},
            {"device_code": "FREE0001", "device_type": "cargo_freighter", "controller_device_code": None}])
        return await managed_by(db)
    m = client.portal.call(go)
    assert m.get("57C506F0") == "DF451241" and "FREE0001" not in m
    from rsweb import loadouts as lo
    assert lo.is_freighter({"device_type": "cargo_freighter"}) and not lo.is_transport_controller({"device_type": "cargo_freighter"})


def _real_devices():
    """A trimmed copy of the player's real GET /devices output (2026-10-01)."""
    return [
        {"device_code": "84EE1EF1", "device_type": "ami_mining_controller", "location": "AEMEROTH-BELT-1", "status": "coordinating",
         "features": ["ami", "cruise", "stow"], "available_commands": ["adopt", "release", "set_directive", "launch"],
         "ami_directive": {"_eval_state": "exhausted:['carbon', 'rares']:AEMEROTH-4-2", "config": {}, "name": "gather_evenly"},
         "ami_directive_status": "active", "controller_device_code": None, "tags": ["home:aemeroth"], "in_control_range": True},
        {"device_code": "512FE0F9", "device_type": "mining_drone", "location": "AEMEROTH-5-L4", "status": "idle",
         "controller_device_code": "84EE1EF1", "tags": ["home:falquoryx"], "available_commands": ["travel", "stow"]},
        {"device_code": "DF451241", "device_type": "ami_transport_controller", "location": "FALQUORYX-BELT-1", "status": "coordinating",
         "features": ["ami"], "available_commands": ["adopt", "release", "set_directive", "launch"],
         "ami_directive": {"_eval_state": "idle:no_sources", "config": {"deliver": "FALQUORYX-BELT-1"}, "name": "consolidate"},
         "ami_directive_status": "active", "controller_device_code": None, "tags": ["home:falquoryx"]},
        {"device_code": "374C62A7", "device_type": "transport_drone", "location": "FALQUORYX-1-L4", "status": "idle",
         "controller_device_code": "DF451241", "tags": ["home:falquoryx", "spare"]},
        {"device_code": "57C506F0", "device_type": "cargo_freighter", "location": "FALQUORYX-BELT-1", "status": "idle",
         "features": ["surge", "cruise", "transport"], "cargo_capacity": 500, "cargo_used": 500,
         "controller_device_code": "DF451241", "tags": []},
        {"device_code": "F32E05A7", "device_type": "ami_mining_controller", "location": "FALQUORYX-BELT-1", "status": "coordinating",
         "features": ["ami"], "available_commands": ["adopt", "release", "set_directive", "launch"], "tags": ["home:falquoryx"],
         "ami_directive": {"_eval_state": "exhausted:['rares']:FALQUORYX-BELT-1", "config": {}, "name": "gather_evenly"},
         "ami_directive_status": "active"},
        {"device_code": "926637CA", "device_type": "mining_drone", "location": "ITHVALAI-2-L4", "status": "idle",
         "controller_device_code": "F32E05A7", "tags": ["home:falquoryx", "home:ithvalai"]},
    ]


def test_real_device_list_quirks():
    from rsweb import loadouts as lo
    devices = _real_devices()
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"mining_drone": 3, "transport_drone": 2}}],
           "systems": {"AEMEROTH": "p", "FALQUORYX": "p", "ITHVALAI": "p"},
           "roles": {"FALQUORYX": "source", "AEMEROTH": "destination"}}
    stars = {k: {"position": {"x": i, "y": 0, "z": 0}} for i, k in enumerate(["AEMEROTH", "FALQUORYX", "ITHVALAI"])}
    p = lo.plan(cfg, devices, [], {}, stars, {}, set(), [], {})
    # 512FE0F9 works for AEMEROTH's controller, in AEMEROTH: it belongs to AEMEROTH now, not FALQUORYX
    a = {r["type"]: r for r in p["report"]["AEMEROTH"]["rows"]}
    assert a["mining_drone"]["have"] == 1
    assert "home:aemeroth" in p["tag_add"]["512FE0F9"] and "home:falquoryx" in p["tag_remove"]["512FE0F9"]
    # 926637CA is in ITHVALAI but run by FALQUORYX's controller: released, and its duplicate home tag cleaned up
    assert p["releases"] == {"F32E05A7": ["926637CA"]}
    assert "home:falquoryx" in p["tag_remove"]["926637CA"] and "home:ithvalai" not in p["tag_remove"]["926637CA"]
    # the ferry is not given to DF451241: it's on in-system work (consolidate)
    cur = {"DF451241": {"directive": "consolidate", "configuration": {"deliver": "FALQUORYX-BELT-1"}, "finished": False}}
    managed = {d["device_code"]: d["controller_device_code"] for d in devices if d.get("controller_device_code")}
    routes, unmet = lo.material_routes(cfg, devices, {"FALQUORYX-BELT-1": {"carbon": 100}}, stars, set(), cur, managed)
    assert routes == [] and any("in-system work" in u["why"] for u in unmet)
    # with a second controller, the freighter is released by DF451241 and adopted by the new one
    devices.append({"device_code": "TF000001", "device_type": "ami_transport_controller", "location": "FALQUORYX-BELT-1",
                    "status": "idle", "features": ["ami"], "available_commands": ["adopt", "set_directive"], "tags": []})
    routes, _ = lo.material_routes(cfg, devices, {"FALQUORYX-BELT-1": {"carbon": 100}}, stars, set(), cur, managed)
    r = routes[0]
    assert r["controller"] == "TF000001" and r["release"] == {"DF451241": ["57C506F0"]}
    bodies = [st["body"] for st in lo.ferry_steps(r)]
    assert bodies.index({"command": "release", "devices": ["57C506F0"]}) < bodies.index({"command": "adopt", "devices": ["57C506F0"]})


def test_controller_idle_reads_ami_directive(client):
    from rsweb.ami_schedule import controller_idle
    d = _real_devices()
    assert client.portal.call(controller_idle, client.app.state.db, d[0])[0] is True        # exhausted
    assert client.portal.call(controller_idle, client.app.state.db, d[2])[0] is True        # idle:no_sources
    busy = dict(d[0], ami_directive={"_eval_state": "mining", "name": "gather_evenly", "config": {}})
    assert client.portal.call(controller_idle, client.app.state.db, busy)[0] is False


def test_print_queue_uses_printing_block(client):
    from rsweb import printqueue
    dev = {"device_code": "3E95BD59", "status": "printing (maintenance_drone)",
           "printing": {"completes_at": "2026-10-01T16:36:43-04:00", "device_type": "maintenance_drone",
                        "started_at": "2026-10-01T16:21:43-04:00", "tags": ["to:ithvalai"]},
           "print_queue": [{"device_type": "surge_plate", "notify": {"device": None}, "tags": ["to:ithvalai"]}]}
    cur = client.portal.call(printqueue.current, client.app.state.db, dev)
    assert cur["device_type"] == "maintenance_drone" and cur["completes_at"].startswith("2026-10-01T16:36") and cur["tags"] == ["to:ithvalai"]
    assert printqueue.items(dev)[0]["tags"] == ["to:ithvalai"]


def test_real_surge_plates_and_queue_space():
    from rsweb import loadouts as lo
    plate = lambda code, **kw: {"device_code": code, "device_type": "surge_plate", "location": "FFF-1-L4", "status": "idle",  # noqa: E731
                                "attach_capacity": 1, "features": ["surge", "cruise", "attach", "stow", "taxi"],
                                "available_commands": ["attach", "detach", "deploy", "stow", "travel"], **kw}
    devices = [
        plate("TAXI0001", taxi_mode="taxi", tags=["taxi"], controller_device_code="0E158313"),   # serving a ferry
        plate("FREE0001", tags=["home:fff"]),
        {"device_code": "TD000001", "device_type": "transport_drone", "location": "FFF-1-L4", "status": "idle",
         "available_commands": ["collect_resources", "deposit_resources", "recall", "travel"], "tags": ["to:aaa"]},
        {"device_code": "AF", "device_type": "autofactory", "location": "FFF-BELT-1", "status": "printing (maintenance_drone)",
         "available_commands": ["enqueue_print"], "printing": {"device_type": "maintenance_drone"},
         "print_queue": [{"device_type": "surge_plate"}] * 9},
    ]
    bps = [{"device_type": "autofactory", "queue_size": 10}, {"device_type": "survey_drone", "resources": {"structural": 1}}]
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"survey_drone": 2}}], "systems": {"AAA": "p"}}
    stars = {"AAA": {"position": {"x": 0, "y": 0, "z": 0}}, "FFF": {"position": {"x": 1, "y": 0, "z": 0}}}
    p = lo.plan(cfg, devices, bps, {"FFF-BELT-1": {"structural": 100}}, stars, {}, set(), [], {})
    # the transport drone (can't be stowed) rides the free plate, not the taxi plate
    assert [(d["carrier"], d["mode"], d["devices"]) for d in p["deliveries"]] == [("FREE0001", "attach", ["TD000001"])]
    # the factory queue is full (9 queued + 1 printing of 10): no print is attempted
    assert p["prints"] == [] and any("queue is full" in u["why"] for u in p["unmet"])


def _enable(client, *rules):
    eng = client.app.state.worker.automations

    async def go():
        s = await eng.settings()
        for r in rules:
            s["rules"][r]["enabled"] = True
        await eng.save_settings(s)
    client.portal.call(go)
    return eng


def test_reopen_sites_with_ami_survey_controller(client):
    eng = _enable(client, "reopen_sites", "salvage_when_depleted")
    devices = [
        {"device_code": "84EE1EF1", "device_type": "ami_mining_controller", "location": "AEMEROTH-BELT-1", "status": "coordinating",
         "ami_directive": {"_eval_state": "exhausted:['carbon']:AEMEROTH-4-2", "name": "gather_evenly", "config": {}}},
        {"device_code": "72779B6A", "device_type": "ami_survey_controller", "location": "AEMEROTH-BELT-1", "status": "coordinating",
         "ami_directive": {"_eval_state": "no_targets:recalling", "name": "survey_system", "config": {}}},
        {"device_code": "A9B9B55B", "device_type": "survey_drone", "location": "AEMEROTH-6-33", "status": "idle",
         "controller_device_code": "72779B6A"},
        {"device_code": "FREE0001", "device_type": "survey_drone", "location": "AEMEROTH-3", "status": "idle"},
    ]
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    done = client.portal.call(eng.rule_reopen_sites)
    assert done == ["72779B6A → belt_search AEMEROTH-BELT-1"]
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "reopen_sites"][-1]
    bodies = [s["body"] for s in job["steps"]]
    assert {"command": "adopt", "devices": ["FREE0001"]} in bodies        # tops it up to 2 drones
    assert {"command": "set_directive", "directive": "belt_search", "configuration": {}} in bodies
    # the salvage rule leaves the mining controller mining while sites are being re-opened
    assert client.portal.call(eng.rule_salvage) == []
    # once a drone is tracking a site there, and the controller is searching, nothing more happens
    devices[2].update(location="AEMEROTH-BELT-1", status="tracking")
    devices[3].update(location="AEMEROTH-BELT-1", status="searching")
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(client.app.state.db.kv_set, "reopen_state", {})
    assert client.portal.call(eng.rule_reopen_sites) == []


def test_reopen_sites_with_drones_only_and_tracking_drones_stay(client):
    eng = _enable(client, "reopen_sites")
    devices = [{"device_code": "SD1", "device_type": "survey_drone", "location": "FAL-1", "status": "idle"},
               {"device_code": "SD2", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "tracking"}]
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(client.app.state.db.kv_set, "exhausted_places", {"FAL-BELT-1": __import__("rsweb.db", fromlist=["x"]).now_iso()})
    assert client.portal.call(eng.rule_reopen_sites) == ["SD1 → search FAL-BELT-1"]
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "reopen_sites"][-1]
    assert [s["body"] for s in job["steps"]] == [{"command": "travel", "destination": "FAL-BELT-1"}, {"command": "search"}]
    # loadouts never marks a tracking drone spare or moves it
    from rsweb import loadouts as lo
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"survey_drone": 0}}], "systems": {"FAL": "p"}}
    p = lo.plan(cfg, devices, [], {}, {}, {}, set(), [], {})
    assert "spare" in p["tag_add"].get("SD1", []) and "spare" not in p["tag_add"].get("SD2", [])


def test_log_fixes_ferry_sticky_and_moving_carrier():
    from rsweb import loadouts as lo
    stars = {"AEM": {"position": {"x": 0, "y": 0, "z": 0}}, "FAL": {"position": {"x": 1, "y": 0, "z": 0}}}
    ctrl = {"device_code": "0E158313", "device_type": "ami_transport_controller", "location": "AEM-BELT-1", "status": "coordinating",
            "available_commands": ["set_directive"], "tags": []}
    devices = [ctrl, {"device_code": "D1", "device_type": "transport_drone", "location": "AEM-6-7", "controller_device_code": "0E158313"}]
    cfg = {"roles": {"AEM": "source", "FAL": "destination"}}
    cur = {"0E158313": {"directive": "ferry", "configuration": {"collect": "AEM-6-7", "deliver": "FAL-BELT-1"}, "finished": False}}
    # the biggest stockpile is now elsewhere in AEM, but the running ferry's pick-up still has stock: leave it alone
    inv = {"AEM-6-7": {"structural": 50}, "AEM-BELT-1": {"structural": 900}}
    assert lo.material_routes(cfg, devices, inv, stars, set(), cur, {"D1": "0E158313"})[0] == []
    # its pick-up ran dry → re-pointed at the stock that's left
    inv = {"AEM-6-7": {}, "AEM-BELT-1": {"structural": 900}}
    r = lo.material_routes(cfg, devices, inv, stars, set(), cur, {"D1": "0E158313"})[0][0]
    assert r["collect"] == "AEM-BELT-1" and r["resend"]
    # a surge plate that is itself bound for another system is not used as a carrier in the same pass
    plate = lambda c, **kw: {"device_code": c, "device_type": "surge_plate", "location": "FAL-1-L4", "status": "idle",  # noqa: E731
                             "attach_capacity": 1, "features": ["surge", "attach"], "available_commands": ["attach", "travel"], **kw}
    devs = [plate("2B30CEB4", tags=["to:ith"]), plate("FREE", tags=["home:fal"]),
            {"device_code": "EDE70565", "device_type": "transport_drone", "location": "FAL-1-L4", "status": "idle",
             "available_commands": ["travel"], "tags": ["to:aem"]}]
    p = lo.plan({"phases": [], "systems": {}}, devs, [], {}, {**stars, "ITH": {}}, {}, set(), [], {})
    assert [d["carrier"] for d in p["deliveries"]] == ["FREE"]
    assert ("2B30CEB4", "ITH") in p["self_moves"]


def test_schedule_skips_exhausted_and_salvaging_controllers(client):
    eng = client.app.state.worker.automations
    base = {"device_type": "ami_mining_controller", "location": "X-BELT-1", "status": "coordinating", "features": ["ami"],
            "available_commands": ["set_directive", "launch", "adopt"]}
    client.portal.call(client.app.state.db.kv_set, "devices", [
        {**base, "device_code": "M1", "ami_directive": {"name": "gather_evenly", "_eval_state": "exhausted:['rares']:X-BELT-1", "config": {}},
         "ami_directive_status": "active"},
        {**base, "device_code": "M2", "ami_directive": {"name": "gather_salvage", "_eval_state": "active", "config": {"location": "X-2"}},
         "ami_directive_status": "active"}])
    sched = {"id": "s", "name": "All gather", "target": "kind:mining", "directive": "gather_evenly", "configuration": {},
             "only_idle": True, "adopt": True, "launch": True}
    res = client.portal.call(eng.run_schedule, sched)
    assert any("M1: exhausted" in r for r in res) and any(r.startswith("M2:") and ("salvage" in r or "busy" in r) for r in res)
    assert not [j for j in client.portal.call(eng.jobs) if j["rule"] == "ami_schedules"]


def test_used_up_salvage_and_closed_sites_are_hidden(client):
    from rsweb.targets import system_resources, system_targets
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    for ev in [e for e in world.events if e["event"] == "salvage.discovered"]:
        client.portal.call(client.app.state.worker.handle_event, dict(ev))
    client.get("/systems/SOL", headers=H)
    client.post("/systems/SOL/resources/refresh", headers=HX)
    db = client.app.state.db
    # salvage used up, one site depleted
    client.portal.call(client.app.state.worker.handle_event, _ev(401, "salvage.depleted", site="SOL-3-1-SAL-1", location="SOL-3-1"))
    client.portal.call(client.app.state.worker.handle_event, _ev(402, "site.depleted", site="SOL-BELT-1-SITE-2"))
    # a site we only know from an old mining event, which the belt's latest detail no longer lists → closed
    client.portal.call(client.app.state.worker.handle_event, _ev(403, "mining.started", site="SOL-BELT-1-SITE-9", resource_type="carbon"))
    res = client.portal.call(system_resources, db, "SOL")
    assert {"SOL-3-1-SAL-1", "SOL-BELT-1-SITE-2", "SOL-BELT-1-SITE-9"} <= res["hidden"]
    assert [x["code"] for x in res["sites_shown"]] == ["SOL-BELT-1-SITE-1"] and res["salvage_shown"] == []
    codes = {t["code"] for t in client.portal.call(system_targets, db, "SOL")["targets"]}
    assert "SOL-3-1-SAL-1" not in codes and "SOL-BELT-1-SITE-2" not in codes and "SOL-BELT-1" in codes
    page = client.get("/systems/SOL", headers=H).text
    assert "SOL-3-1-SAL-1" not in page and "SOL-BELT-1-SITE-9" not in page and "hidden" in page
    # the salvage rule still knows it's used up
    assert any(x["code"] == "SOL-3-1-SAL-1" and x["depleted"] for x in res["salvage"])


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
    # fulfil uses the game's call by default
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


def test_freighter_at_destination_counts_for_its_ferry_controllers_system():
    from rsweb import loadouts as lo
    devices = [{"device_code": "TF", "device_type": "ami_transport_controller", "location": "AEM-BELT-1", "status": "coordinating",
                "ami_directive": {"name": "ferry", "config": {}, "_eval_state": "active:1l:0d"}},
               {"device_code": "57C506F0", "device_type": "cargo_freighter", "location": "FAL-BELT-1", "status": "idle",
                "controller_device_code": "TF", "tags": []}]
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"cargo_freighter": 1}}], "systems": {"AEM": "p", "FAL": "p"}}
    p = lo.plan(cfg, devices, [], {}, {}, {}, set(), [], {})
    aem = {r["type"]: r for r in p["report"]["AEM"]["rows"]}["cargo_freighter"]
    fal = {r["type"]: r for r in p["report"]["FAL"]["rows"]}["cargo_freighter"]
    assert aem["have"] == 1 and aem["away"] == ["57C506F0"] and fal["have"] == 0
    assert "home:aem" in p["tag_add"]["57C506F0"]



def test_contracts_rule_fulfils_when_ready(client):
    w = client.app.state.worker
    eng = _enable(client, "contracts")
    client.portal.call(w.sync_inventory)
    disc = {"criteria": [{"devices": [], "name": "default", "resources": {"structural": 350}}], "designation": "SOL-BELT-1-EVT-001",
            "location": "SOL-BELT-1", "title": "Belt Need", "tier": 1, "rewards": {"xp": 10}}
    client.portal.call(w.handle_event, {"id": "4444444444450-0", "event": "event.discovered", "category": "event",
                                        "location": "SOL-BELT-1", "payload": disc, "created_at": "2026-10-01T10:00:00+00:00"})
    client.portal.call(client.app.state.db.kv_set, "replicants", {"77F75255": {"name": "bob-1", "location": "SOL-BELT-1"}})
    done = client.portal.call(eng.rule_contracts, True)
    assert done == ["fulfil SOL-BELT-1-EVT-001"]
    act = client.portal.call(client.app.state.db.fetchone, "SELECT method, path FROM actions ORDER BY id DESC LIMIT 1")
    assert (act["method"], act["path"]) == ("POST", "/locations/SOL-BELT-1/events/SOL-BELT-1-EVT-001")
    assert client.portal.call(eng.rule_contracts, True) == []   # not retried straight away



def test_freighter_stranded_under_in_system_controller_is_released():
    """The real case: 57C506F0 sits idle in AEMEROTH, still run by FALQUORYX's consolidate controller DF451241."""
    from rsweb import loadouts as lo
    devices = [{"device_code": "DF451241", "device_type": "ami_transport_controller", "location": "FALQUORYX-BELT-1",
                "status": "coordinating", "ami_directive": {"name": "consolidate", "config": {"deliver": "FALQUORYX-BELT-1"},
                                                            "_eval_state": "idle:no_sources"}},
               {"device_code": "0E158313", "device_type": "ami_transport_controller", "location": "AEMEROTH-BELT-1",
                "status": "coordinating", "ami_directive": {"name": "ferry", "config": {"collect": "AEMEROTH-6-7", "deliver": "FALQUORYX-BELT-1"},
                                                            "_eval_state": "active:1l:0d"}},
               {"device_code": "57C506F0", "device_type": "cargo_freighter", "location": "AEMEROTH-5-L4", "status": "idle",
                "controller_device_code": "DF451241", "tags": ["home:aemeroth"], "cargo_used": 0}]
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"cargo_freighter": 1}}], "systems": {"AEMEROTH": "p", "FALQUORYX": "p"},
           "roles": {"AEMEROTH": "source", "FALQUORYX": "destination"}}
    stars = {"AEMEROTH": {"position": {"x": 0, "y": 0, "z": 0}}, "FALQUORYX": {"position": {"x": 1, "y": 0, "z": 0}}}
    p = lo.plan(cfg, devices, [], {}, stars, {}, set(), [], {})
    assert p["releases"] == {"DF451241": ["57C506F0"]}          # the consolidate controller lets it go
    aem = {r["type"]: r for r in p["report"]["AEMEROTH"]["rows"]}["cargo_freighter"]
    assert aem["have"] == 1                                    # it counts where it is (and is tagged)
    # next pass, released: AEMEROTH's ferry controller takes it on
    devices[2]["controller_device_code"] = None
    routes, _ = lo.material_routes(cfg, devices, {"AEMEROTH-6-7": {"structural": 277}}, stars, set(),
                                   {"0E158313": {"directive": "ferry", "configuration": {"collect": "AEMEROTH-6-7", "deliver": "FALQUORYX-BELT-1"},
                                                 "finished": False}}, {})
    assert routes and routes[0]["controller"] == "0E158313" and [a["code"] for a in routes[0]["adopt"]] == ["57C506F0"]
    assert not routes[0]["resend"]                              # adopt only; its ferry keeps running


def test_tag_hygiene_for_spare_devices():
    from rsweb import loadouts as lo
    devices = [  # from the player's list
        {"device_code": "1DE93245", "device_type": "transport_hauler", "location": "FALQUORYX-1", "status": "idle",
         "tags": ["home:falquoryx", "spare"]},
        {"device_code": "28C47D66", "device_type": "surge_plate", "location": "FALQUORYX-1-L4", "status": "idle",
         "tags": ["spare", "taxi"], "taxi_mode": "taxi"},
        {"device_code": "512FE0F9", "device_type": "mining_drone", "location": "AEMEROTH-5-L4", "status": "idle",
         "controller_device_code": "84EE1EF1", "tags": ["spare"]},
        {"device_code": "6B208B47", "device_type": "transport_drone", "location": "FALQUORYX-BELT-1", "status": "idle", "tags": ["spare"]},
    ]
    p = lo.plan({"phases": [], "systems": {}}, devices, [], {}, {}, {}, set(), [], {})
    assert p["tag_remove"]["1DE93245"] == ["home:falquoryx"]           # spare → no home, even with no phase for haulers
    assert p["tag_remove"]["28C47D66"] == ["spare"]                    # taxi plates serve a ferry
    assert p["tag_remove"]["512FE0F9"] == ["spare"]                    # working for a controller
    assert "6B208B47" not in p["tag_remove"]                           # a free spare stays spare
