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
               "payload": {"destination": "SOL-BELT-1"}, "created_at": "2026-09-30T12:00:00+00:00"}
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
               "location": "SOL-BELT-1", "payload": {"destination": "SOL-BELT-1"}, "created_at": "2026-09-30T12:00:00+00:00"}
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
    assert '<optgroup label="Resource sites">' in r.text or '<optgroup label="Asteroid belts">' in r.text
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


def test_tree_page_renders_with_stowed_and_commands(client):
    client.portal.call(client.app.state.worker.sync_devices)  # also builds the stowed map
    page = client.get("/tree", headers=H).text
    assert "Expand all" in page and "Collapse all" in page and "Systems only" in page
    assert 'id="ts-SOL"' in page and 'id="t-11ADA230"' in page
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
        "location": "SOL-BELT-1", "payload": {"destination": "SOL-BELT-1"}, "created_at": "2026-09-30T12:00:00+00:00"})


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
