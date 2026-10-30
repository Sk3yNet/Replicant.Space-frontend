"""Core behavior: SSE parsing, rate limiting, safety blocks, timers, notifications, digest, auth."""
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
import json
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
        assert levels == ["done", "warning"]   # print done (info); hub warning
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
                                  "/messages", "/account", "/console", "/digest?hours=24", "/locations/SOL-BELT-1",
                                  "/traffic", "/defence", "/maintenance", "/shop", "/automations"])
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


def test_printers_mark_every_vessel():
    from rsweb.web import printers
    devs = [{"device_code": "R1", "device_type": "racing_vessel", "features": ["print"]},
            {"device_code": "A1", "device_type": "autofactory", "features": ["print"]}]
    ps = {p["code"]: p["vessel"] for p in printers({"devices": devs, "replicants": {"X": {"name": "X"}}})}
    assert ps == {"R1": True, "A1": False, "X": True}  # the hide vessels toggle covers racing vessels too


def test_blueprints_with_list_shaped_inventory(client):
    r = client.get("/blueprints", headers=H)
    assert r.status_code == 200 and "×" in r.text
    assert "bp-hide-vessels" in r.text and "afford-vessel" in r.text  # vessels marked for the hide vessels toggle
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
    body = c.parse_fields(c.DIRECTIVES["mining"]["maintain_ratios"], _Form({"f.ratios.structural": "0.5", "f.ratios.rares": "0.1"}))
    assert body == {"ratios": {"structural": 0.5, "rares": 0.1}}
    with pytest.raises(c.FormError):
        c.parse_fields(c.COMMANDS["travel"], _Form({}))
    assert c.parse_fields(c.COMMANDS["adopt"], _Form({"f.devices": ["A", "B"]})) == {"devices": ["A", "B"]}


def test_command_forms_render_with_suggestions(client):
    r = client.get("/devices/2AC61212/command-form?command=travel", headers=HX)
    assert r.status_code == 200 and 'list="c-2AC61212-loc-sys"' in r.text and "SOL-BELT-1" in r.text and "SOL-3-L4" in r.text
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
    # the panel's #1 Remove button sends the game's 0-based index 0, and the right item goes
    assert '"index": 0}' in panel and 'Remove #1 ' in panel
    waiting = [x.get("device_type") for x in world.queues["AF00BEEF"]]
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "remove", "index": "0"}, headers=HX)
    assert "result ok" in r.text and r.text.count("Remove</button>") == 1
    assert '"index": 0' in client.portal.call(client.app.state.db.fetchone, "SELECT body FROM actions ORDER BY id DESC LIMIT 1")["body"]
    assert [x.get("device_type") for x in world.queues["AF00BEEF"]] == waiting[1:]
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
               "payload": {"destination": "SOL-BELT-1", "origin": "ABOTEIN-OORT", "travel_type": "surge"}, "created_at": iso(datetime.now(timezone.utc))}
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
    before = client.portal.call(client.app.state.db.fetchone, "SELECT COUNT(*) n FROM actions WHERE user='automation'")
    r = client.post("/automations/survey-now", data={"vessel": "11ADA230"}, headers=HX)
    assert r.status_code == 200
    arrival = {"id": "8888888888888-0", "event": "travel.arrived", "device_code": "11ADA230", "device_type": "heaven_vessel",
               "location": "SOL-BELT-1", "payload": {"destination": "SOL-BELT-1", "origin": "ABOTEIN-OORT", "travel_type": "surge"}, "created_at": iso(datetime.now(timezone.utc))}
    client.portal.call(client.app.state.worker.handle_event, arrival)
    after = client.portal.call(client.app.state.db.fetchone, "SELECT COUNT(*) n FROM actions WHERE user='automation'")
    assert after["n"] == before["n"] == 0
    assert client.portal.call(eng.jobs) == []
    page = client.get("/automations", headers=H).text
    assert "[dry run] survey SOL" in page


def test_restart_idle_miners(client):
    eng = client.app.state.worker.automations
    client.post("/automations/rules/restart_idle_miners", data={"enabled": "on", "resource": "carbon", "cooldown_minutes": "10"}, headers=HX)
    client.portal.call(eng.tick)
    acts = client.portal.call(client.app.state.db.fetchall, "SELECT path, body FROM actions WHERE user='automation'")
    assert any('"start_mining"' in a["body"] and '"carbon"' in a["body"] and a["path"] == "/devices/2AC61212" for a in acts)
    n = len(acts)
    client.portal.call(eng.tick)  # cooldown: no second attempt
    assert len(client.portal.call(client.app.state.db.fetchall, "SELECT 1 FROM actions WHERE user='automation'")) == n



def test_restart_idle_miners_focus_one_site(client):
    eng = client.app.state.worker.automations
    db = client.app.state.db
    loc = client.portal.call(eng.devices)
    belt = next(d["location"] for d in loc if d["device_code"] == "2AC61212")
    world = client.app.state.api.http._transport.app.state.world
    world.belt_sites = {belt: [
        {"designation": f"{belt}-SITE-1", "resources_remaining_pct": {"carbon": 80, "structural": 10}},
        {"designation": f"{belt}-SITE-2", "resources_remaining_pct": {"carbon": 30, "structural": 100}},
        {"designation": f"{belt}-SITE-3", "resources_remaining_pct": {"carbon": 0, "structural": 5}}]}
    client.post("/automations/rules/restart_idle_miners", data={"enabled": "on", "resource": "carbon", "cooldown_minutes": "10",
                                                                 "focus_sites": "on"}, headers=HX)
    client.portal.call(eng.tick)
    acts = client.portal.call(db.fetchall, "SELECT path, body FROM actions WHERE user='automation' AND path='/devices/2AC61212'")
    assert any(f'"target": "{belt}-SITE-2"' in a["body"] for a in acts), acts


def test_focus_site_picks_the_least_left():
    from rsweb.sites import focus_site
    detail = {"resource_sites": [{"designation": "A-BELT-1-SITE-1", "resources_remaining_pct": {"carbon": 40}},
                                 {"designation": "A-BELT-1-SITE-2", "resources_remaining_pct": {"carbon": 40, "rares": 0}},
                                 {"designation": "A-BELT-1-SITE-3", "resources_remaining_pct": {"carbon": 10, "rares": 90}},
                                 {"designation": "A-1-SAL-1", "site_type": "salvage", "resources_remaining_pct": {"carbon": 1}}]}
    assert focus_site(detail, "carbon") == "A-BELT-1-SITE-3"
    assert focus_site(detail, "carbon", {"A-BELT-1-SITE-3"}) == "A-BELT-1-SITE-1"
    assert focus_site(detail, "volatiles") is None and focus_site(None, "carbon") is None

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


def test_system_targets_list_a_scans_system_objects(client):
    import json as _json
    db = client.app.state.db
    scan = {"entry_point": "TWAFFY-11-L4", "planets": [{"designation": "TWAFFY-1"}],
            "system_objects": [{"designation": "DERELICT-RING-001", "location": "TWAFFY-OBJ-1", "title": "Derelict Ring",
                                "object_type": "megastructure", "megastructure_type": "derelict_ring"}]}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("TWAFFY", _json.dumps(scan), "2026-10-18T00:00:00+00:00"))
    from rsweb.targets import system_targets
    codes = {x["code"]: x for x in client.portal.call(system_targets, db, "TWAFFY")["targets"]}
    assert codes["TWAFFY-OBJ-1"]["category"] == "object" and "Derelict Ring" in codes["TWAFFY-OBJ-1"]["label"]


def test_system_targets_show_only_objects_that_still_exist(client):
    """Owner 2026-10-22: the system page shows active asteroids only; past ones stay on the Defense page."""
    import json as _json
    db = client.app.state.db
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("KELX", _json.dumps({"planets": [{"designation": "KELX-1"}]}), "2026-10-18T00:00:00+00:00"))
    for i, loc in enumerate(["KELX-OBJ-1", "KELX-OBJ-2"]):
        client.portal.call(db.insert_event, {"id": f"od{i}", "event": "system.object_detected", "location": loc,
                                              "payload": {"object_designation": loc}})
    client.portal.call(db.kv_set, "objects", {"KELX-OBJ-1": {"designation": "KELX-OBJ-1", "status": "active"},
                                              "KELX-OBJ-2": {"designation": "KELX-OBJ-2", "closed": "impacted"}})
    from rsweb.targets import system_targets
    codes = {x["code"] for x in client.portal.call(system_targets, db, "KELX")["targets"]}
    assert "KELX-OBJ-1" in codes and "KELX-OBJ-2" not in codes


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
    db = client.app.state.db   # D8C2A140 starts out scanning: a scanning drone would wait for its scan first
    next(d for d in world.devices if d["device_code"] == "D8C2A140")["status"] = "idle"
    cached = client.portal.call(db.kv_get, "devices")
    next(d for d in cached if d["device_code"] == "D8C2A140")["status"] = "idle"
    client.portal.call(db.kv_set, "devices", cached)
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
        "location": "SOL-BELT-1", "payload": {"destination": "SOL-BELT-1", "origin": "ABOTEIN-OORT", "travel_type": "surge"}, "created_at": iso(datetime.now(timezone.utc))})


def test_ami_schedule_add_run_and_idle_check(client):
    page = client.get("/ami", headers=H).text   # schedules live on the AMI page since 1.15.0
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


def test_print_needs_tilt_drones_toward_what_waiting_prints_lack():
    from rsweb import prospects as pr
    short = pr.print_short({"silicates": 100, "carbon": 900}, [{"silicates": 400, "carbon": 50}])
    assert short["silicates"] > 0.8 and short["carbon"] == 0
    scan = {"asteroid_belt": {"belts": [{"designation": "X-BELT-1", "resources": {"silicates": "low", "carbon": "rich"}}]}}
    assert pr.belt_resources(scan, "X-BELT-1") == ["carbon", "silicates"]
    r = pr.print_ratios(["carbon", "silicates"], short)
    assert r["silicates"] > 0.7 and r["carbon"] < 0.3
    assert pr.print_ratios(["carbon"], short) is None        # the belt has none of it: gather_evenly as before


def test_a_gather_evenly_schedule_leans_toward_print_needs(client):
    import json as _j
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("SOL", _j.dumps({"asteroid_belt": {"belts": [{"designation": "SOL-BELT-1",
                                                                      "resources": {"carbon": "rich", "silicates": "low"}}]}}),
                        "2026-10-22T00:00:00+00:00"))

    async def needs(devices):
        return {"silicates": 1.0}
    eng.print_needs = needs
    ctrl = {"device_code": "MC91FF22", "device_type": "ami_mining_controller", "location": "SOL-BELT-1"}
    use = client.portal.call(eng.print_tilt, {"directive": "gather_evenly"}, ctrl, [])
    assert use["directive"] == "maintain_ratios" and use["configuration"]["ratios"]["silicates"] > use["configuration"]["ratios"]["carbon"]
    assert client.portal.call(eng.print_tilt, {"directive": "gather_evenly"}, {**ctrl, "tags": ["boot:x"]}, [])["directive"] \
        == "gather_evenly"


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


def test_auto_survey_leaves_fleet_passengers_aboard(client):
    """Live 2026-10-21: Surveyor 1's heaven vessel was flown into PHEVENIA and auto survey deployed the fleet's survey
    controller and a drone out of it while the fleet was boarding them for the trip home."""
    world = client.app.state.api.http._transport.app.state.world
    world.devices.append({"device_code": "SC000002", "device_type": "ami_survey_controller", "location": "SOL-BELT-1",
                          "status": "stowed", "replicant_code": "77F75255", "features": ["cruise", "ami", "stow"],
                          "tags": ["fleet:surveyor-x"], "available_commands": ["deploy", "adopt", "set_directive", "launch", "stow"]})
    world.move_seconds = 0.03
    client._seen = len(world.events)
    client.portal.call(client.app.state.worker.sync_devices)
    client.post("/automations/rules/auto_survey", data={"enabled": "on", "use_ami": "on", "include_belts": "on",
                                                        "max_targets": "5", "return_and_stow": "on"}, headers=HX)
    _arrive(client, 8)
    eng = client.app.state.worker.automations
    steps = [s["path"] for j in client.portal.call(eng.jobs) if j["rule"] == "auto_survey" for s in j["steps"]]
    assert steps and "/devices/SC000002" not in steps, steps


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


def _rep(p, star):
    """The loadout report of the fleet stationed in `star` (reports are per fleet)."""
    return next(r for r in p["report"].values() if r["star"] == star)


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
    a, b = _rep(p, "AAA"), _rep(p, "BBB")
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
    steps = lo.delivery_steps(dl[0], p["by_code"], stars, True, assign=p["assign"])
    bodies = [(s["path"], s["body"]) for s in steps]
    # they join BBB's stationed fleet as they leave (so they count as its incoming), and keep the tag on arrival
    assert ("/devices/A1", {"configuration": {"add_tags": ["to:bbb", "fleet:bbb-home"]}}) in bodies
    assert ("/devices/CAR", {"command": "attach", "device": "A1"}) in bodies   # the carrier attaches the cargo
    assert ("/devices/CAR", {"command": "travel", "destination": "BBB-5-L4"}) in bodies
    assert ("/devices/CAR", {"command": "detach", "device": "A3"}) in bodies
    board = next(st for st in steps if st["body"] == {"command": "attach", "device": "A1"})
    assert board["critical"]          # no boarding → the carrier doesn't fly off without it
    assert ("/devices/A3", {"configuration": {"add_tags": ["fleet:bbb-home"], "remove_tags": ["to:bbb"]}}) in bodies
    assert p["assign"]["A2"] == "fleet:aaa-home" and "fleet:aaa-home" in p["tag_add"]["A2"]   # fleetless at home: joins
    assert bodies[-1] == ("/devices/CAR", {"command": "travel", "destination": "AAA-OORT"})
    pr = lo.print_steps(p["prints"][0])[0]["body"]
    assert pr["command"] == "enqueue_print" and pr["tags"][0].startswith("to:") and pr["tags"][1].startswith("fleet:")
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
    b = {r["type"]: r for r in _rep(p, "BBB")["rows"]}
    assert b["mining_drone"]["incoming"] == 2 and b["mining_drone"]["short"] == 0
    assert b["survey_drone"]["incoming"] == 1 and b["survey_drone"]["short"] == 0
    assert "BS" in p["arrived"]
    steps = lo.arrived_steps("BS", p["by_code"]["BS"], {}, join=p["assign"]["BS"])
    assert steps[-1]["body"] == {"configuration": {"add_tags": ["fleet:bbb-home"], "remove_tags": ["spare", "to:bbb"]}}
    assert "BS" not in {st["path"][9:] for st in lo.tag_steps(p)}   # the arrival step sets its tags
    # AAA now has exactly 2 drones locally (A2, A4) — nothing more is marked spare there
    a = {r["type"]: r for r in _rep(p, "AAA")["rows"]}
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
    home = client.get("/", headers=H).text
    assert 'href="/fleets"' in home and 'href="/loadouts"' not in home   # nav: one Fleets tab
    assert client.get("/loadouts", headers=H, follow_redirects=False).headers["location"] == "/fleets"
    r = client.post("/loadouts/phases", data={"new_phase": "Mining hub"}, headers=HX)
    assert r.headers.get("HX-Refresh")
    cfg = client.portal.call(client.app.state.db.kv_get, "loadouts")
    pid = cfg["phases"][0]["id"]
    client.post("/loadouts/phases", data={f"name:{pid}": "Mining hub", f"order:{pid}": "1",
                                          f"want:{pid}:mining_drone": "2", f"want:{pid}:survey_drone": ""}, headers=HX)
    client.post("/fleets", data={"name": "Sol home", "role": "mining", "home": "sol", "template": pid, "station": "on"}, headers=HX)
    client.post("/loadouts/settings", data={"ignore_tags": "keep, Reserve", "print_missing": "on", "need_stock": "on"}, headers=HX)
    cfg = client.portal.call(client.app.state.db.kv_get, "loadouts")
    assert cfg["phases"][0]["wants"] == {"mining_drone": 2} and cfg["ignore_tags"] == ["keep", "reserve"]
    assert "fleets" not in cfg   # the fleets live in their own store
    fleets = client.portal.call(client.app.state.db.kv_get, "fleets")
    assert fleets[0]["home"] == "SOL" and fleets[0]["station"] and fleets[0]["template"] == pid
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Mining hub" in page and "2 spare" in page and "as spare" in page and "Tags &amp; controllers check" in page
    r = client.post("/loadouts/apply", data={"fleet": "sol-home"}, headers=HX)
    assert "Applied to sol-home" in r.text
    spares = [d["device_code"] for d in world.devices if "spare" in (d.get("tags") or [])]
    assert len(spares) == 2 and all(c.startswith("2AC6121") for c in spares)
    kept = [d for d in world.devices if d["device_type"] == "mining_drone" and "spare" not in (d.get("tags") or [])]
    assert len(kept) == 2 and all("fleet:sol-home" in d["tags"] for d in kept)   # the rest join the stationed fleet
    # survey drones aren't in the template: untouched (no fleet, not spare)
    assert not any(set(d.get("tags") or []) & {"spare", "fleet:sol-home"} for d in world.devices if d["device_type"] == "survey_drone")
    assert "◆ Sol home" in client.get("/tree", headers=H).text


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
    assert "<h2>Resources</h2>" in page and "Derelict hauler" in page and "SOL-3-1-SAL-1" in page
    r = client.post("/systems/SOL/resources/refresh", headers=HX)
    assert "open mining site(s)" in r.text and "salvage body" in r.text and "Could not read" not in r.text
    res = client.portal.call(system_resources, client.app.state.db, "SOL")
    assert res["totals"]["structural"]["sites"] == 5200 and res["totals"]["rares"]["sites"] == 340
    # the body's detail gives % remaining of what was discovered (300 structural, 80 conductive)
    assert res["totals"]["structural"]["salvage"] == pytest.approx(260, abs=0.1)
    assert res["mineable"] == 5540 and res["salvageable"] == pytest.approx(335, abs=0.1)
    sal = next(x for x in res["salvage"] if x["code"] == "SOL-3-1-SAL-1")
    assert sal["body"] == "SOL-3-1" and sal["remaining_pct"] == {"structural": 86.67, "conductive": 93.75}
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
        client.portal.call(client.app.state.db.kv_set, "devices", [])   # (a device just missing from the list is kept as unlisted)
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
                    "configuration": {"location": "SOL-3-1", "recall": True}}    # the body, not the -SAL- code; recall brings drones back
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


def test_fleet_materials_setting(client):
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(client.app.state.worker.sync_inventory)
    client.post("/fleets", data={"name": "Sol home", "home": "SOL", "station": "on"}, headers=HX)
    client.post("/fleets", data={"name": "Depot", "home": "ABOTEIN", "station": "on"}, headers=HX)
    r = client.post("/fleets/sol-home/station", data={"station": "on", "materials": "depot"}, headers=HX)
    assert r.headers.get("HX-Refresh")
    client.post("/fleets/depot/station", data={"station": "on", "materials": "self"}, headers=HX)
    client.post("/fleets/depot/station", data={"station": "on", "materials": "no-such-fleet"}, headers=HX)
    fleets = {f["id"]: f for f in client.portal.call(client.app.state.db.kv_get, "fleets")}
    assert fleets["sol-home"]["materials"] == "depot" and fleets["depot"]["materials"] == ""   # unknown fleet: cleared
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "materials from Sol home" in page
    assert 'value="depot" selected' in client.get("/fleets/overview", headers=HX).text   # its route (or why not) is listed


def test_arrival_rules_ignore_in_system_hops_and_surveyed_systems(client):
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.worker.sync_devices)
    client.get("/systems/SOL", headers=H)  # cache the scan
    client.post("/automations/rules/auto_survey", data={"enabled": "on", "use_idle": "on", "include_belts": "on",
                                                        "max_targets": "20", "use_ami": "on"}, headers=HX)

    def arrive(n, **payload):
        client.portal.call(client.app.state.worker.handle_event, {
            "id": f"77777777777{n:02d}-0", "event": "travel.arrived", "device_code": "TC000001", "device_type": "transport",
            "location": "SOL-BELT-1", "payload": {"destination": "SOL-BELT-1", **payload}, "created_at": iso(datetime.now(timezone.utc))})

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
    # first pass: everything counted for a stationed fleet joins it (fleet tag)
    assert "fleet:aaa-home" in p["tag_add"]["CAR"] and "fleet:aaa-home" in p["tag_add"]["AC"]
    assert "fleet:bbb-home" in p["tag_add"]["BS"]
    assert any("fleet:aaa-home" in line for line in lo.describe(p))
    # tag them, then send the carrier off to BBB on a delivery
    for d in devices:
        if d["device_code"] in p["tag_add"] and d["device_code"] not in p["moves"]:
            d["tags"] = sorted(set(d.get("tags") or []) | {t for t in p["tag_add"][d["device_code"]] if t.startswith("fleet:")})
        if d["device_code"] == "CAR":
            d["location"], d["status"] = "BBB-5-L4", "idle"
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    a = {r["type"]: r for r in _rep(p, "AAA")["rows"]}
    assert a["surge_carrier"]["have"] == 1 and a["surge_carrier"]["short"] == 0 and a["surge_carrier"]["away"] == ["CAR"]
    b = {r["type"]: r for r in _rep(p, "BBB")["rows"]}
    assert "surge_carrier" not in b or b["surge_carrier"]["have"] == 0   # not counted (or made spare) where it's visiting
    assert "spare" not in p["tag_add"].get("CAR", [])



def test_job_stops_after_repeated_failures(client):
    eng = client.app.state.worker.automations
    from rsweb.automations import step
    steps = [step(f"travel {i}", "/devices/NOPE0000", {"command": "travel", "destination": f"SOL-{i}"}) for i in range(10)]
    job = client.portal.call(eng.create_job, "auto_survey", "doomed", "NOPE0000", steps, {}, True)
    job = next(j for j in client.portal.call(eng.jobs) if j["id"] == job["id"])
    assert job["status"] == "failed" and sum(1 for st in job["steps"] if st["status"] == "skipped") == 3


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


def test_a_watch_waits_for_a_fresh_device_list():
    """Live 2026-10-28: Surveyor 3's watch read a cached list from before the launch, saw no directive and recalled."""
    from rsweb import fleets as fl
    fleet = {"id": "s", "role": "explore", "wants": {}}
    devs = [{"device_code": "SC1", "device_type": "ami_survey_controller", "location": "A-1", "status": "idle",
             "tags": ["fleet:s"]}]
    m = {"phase": "watch", "phase_at": "2026-10-28T19:54:28+00:00"}
    assert not fl.watch_done(fleet, m, devs, "2026-10-28T19:55:33+00:00")[0]
    assert fl.watch_done(fleet, m, devs, "2026-10-28T19:58:00+00:00")[0]


def test_rewind_drops_the_stalled_job():
    """Live 2026-10-28: the auto retry left the failed recall job in place, so each retry stalled again at once one
    phase earlier (watch, work, deploy), each a new 'try 1 of 8' alert."""
    from rsweb import fleets as fl
    m = {"status": "stalled", "phase": "recall", "job": "j1"}
    fl.rewind({"role": "explore"}, m)
    assert "job" not in m and m["phase"] == "watch" and m["recall_now"]


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
    row = next(r for r in _rep(p, "AAA")["rows"] if r["type"] == "mining_drone")
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
    # 512FE0F9 works for AEMEROTH's controller, in AEMEROTH: it joins AEMEROTH's stationed fleet, its old
    # home:falquoryx tag goes
    a = {r["type"]: r for r in _rep(p, "AEMEROTH")["rows"]}
    assert a["mining_drone"]["have"] == 1
    assert "fleet:aemeroth-home" in p["tag_add"]["512FE0F9"] and "home:falquoryx" in p["tag_remove"]["512FE0F9"]
    # 926637CA is in ITHVALAI but run by FALQUORYX's controller: released, joins ITHVALAI's fleet, both old home tags go
    assert p["releases"] == {"F32E05A7": ["926637CA"]}
    assert p["tag_add"]["926637CA"] == ["fleet:ithvalai-home"]
    assert p["tag_remove"]["926637CA"] == ["home:falquoryx", "home:ithvalai"]
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


def test_reopen_sites_prefers_belt_surveyors(client):
    eng = _enable(client, "reopen_sites")
    devices = [
        {"device_code": "72779B6A", "device_type": "ami_survey_controller", "location": "FAL-BELT-1", "status": "idle"},
        {"device_code": "SD1", "device_type": "survey_drone", "location": "FAL-1", "status": "idle"},
        {"device_code": "BS1", "device_type": "belt_surveyor", "location": "FAL-2", "status": "idle", "features": ["cruise", "survey"]},
    ]
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(client.app.state.db.kv_set, "exhausted_places", {"FAL-BELT-1": __import__("rsweb.db", fromlist=["x"]).now_iso()})
    done = client.portal.call(eng.rule_reopen_sites)
    # the controller adopts the belt surveyor first, then the survey drone
    assert done == ["72779B6A → belt_search FAL-BELT-1"]
    adopt = [s["body"] for j in client.portal.call(eng.jobs) if j["rule"] == "reopen_sites" for s in j["steps"]
             if (s.get("body") or {}).get("command") == "adopt"]
    assert adopt == [{"command": "adopt", "devices": ["BS1", "SD1"]}]
    from rsweb import ami_schedule as am
    here = [{"device_code": "SD0", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "idle"},
            {"device_code": "BS9", "device_type": "belt_surveyor", "location": "FAL-BELT-1", "status": "idle"}]
    assert am.adoptable(here, devices[0], {}) == ["BS9", "SD0"] and am.drone_kind(here[1]) == "survey"
    from rsweb import sites
    assert [d["device_code"] for d in sites.surveyors_first(devices[1:])] == ["BS1", "SD1"]
    devices[2].update(location="FAL-BELT-1", status="tracking")
    assert [d["device_code"] for d in sites.survey_at(devices, "FAL-BELT-1")] == ["BS1"]


def test_belt_surveyors_are_their_own_loadout_item():
    from rsweb import loadouts as lo
    devices = [{"device_code": "BS1", "device_type": "belt_surveyor", "location": "FAL-BELT-1", "status": "tracking",
                "tags": ["fleet:m"]},
               {"device_code": "SD1", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "idle", "tags": ["fleet:m"]},
               {"device_code": "SD2", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "idle", "tags": ["fleet:m"]}]
    cfg = {"fleets": [{"id": "m", "name": "M", "role": "mining", "home": "FAL", "station": True,
                       "wants": {"survey_drone": 2, "belt_surveyor": 1}}]}
    p = lo.plan(cfg, devices, [], {}, {}, {}, set(), [], {})
    assert not any("spare" in p["tag_add"].get(c, []) for c in ("SD1", "SD2", "BS1"))


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
         "ami_directive_status": "active"},
        {**base, "device_code": "M3", "status": "out_of_range", "in_control_range": False}])
    sched = {"id": "s", "name": "All gather", "target": "kind:mining", "directive": "gather_evenly", "configuration": {},
             "only_idle": True, "adopt": True, "launch": True}
    res = client.portal.call(eng.run_schedule, sched)
    assert any("M1: exhausted" in r for r in res) and any(r.startswith("M2:") and ("salvage" in r or "busy" in r) for r in res)
    assert "M3: out of comms range — skipped" in res
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
    assert "no beacon</a>" in page                               # the mock's only beacon is stowed
    world = client.app.state.api.http._transport.app.state.world
    fb = next(d for d in world.devices if d["device_code"] == "FB000001")
    keep = dict(fb)
    fb.update(status="idle", location="SOL-3")
    client.portal.call(client.app.state.db.kv_set, "devices", world.devices)
    assert "📡 in system</a>" in client.get("/game-events", headers=H).text
    fb.update(location="SOL-3-L4")
    client.portal.call(client.app.state.db.kv_set, "devices", world.devices)
    assert "📡 beacon</a>" in client.get("/game-events", headers=H).text
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


def test_freighter_at_destination_counts_for_its_ferry_controllers_system():
    from rsweb import loadouts as lo
    devices = [{"device_code": "TF", "device_type": "ami_transport_controller", "location": "AEM-BELT-1", "status": "coordinating",
                "ami_directive": {"name": "ferry", "config": {}, "_eval_state": "active:1l:0d"}},
               {"device_code": "57C506F0", "device_type": "cargo_freighter", "location": "FAL-BELT-1", "status": "idle",
                "controller_device_code": "TF", "tags": []}]
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"cargo_freighter": 1}}], "systems": {"AEM": "p", "FAL": "p"}}
    p = lo.plan(cfg, devices, [], {}, {}, {}, set(), [], {})
    aem = {r["type"]: r for r in _rep(p, "AEM")["rows"]}["cargo_freighter"]
    fal = {r["type"]: r for r in _rep(p, "FAL")["rows"]}["cargo_freighter"]
    assert aem["have"] == 1 and aem["away"] == ["57C506F0"] and fal["have"] == 0
    assert "fleet:aem-home" in p["tag_add"]["57C506F0"]



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
    assert "Pre-approve contracts from <b>the inhabitants of SOL-BELT-1</b>" in page
    client.post("/game-events/approve", data={"key": "body:SOL-BELT-1", "on": "1"}, headers=HX)
    client.portal.call(client.app.state.db.kv_set, "contracts_state", {})
    done = client.portal.call(eng.rule_contracts, True)
    assert done == ["fulfill SOL-BELT-1-EVT-001"]
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
    aem = {r["type"]: r for r in _rep(p, "AEMEROTH")["rows"]}["cargo_freighter"]
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


def _fleet_world():
    T = ["fleet:prospector-1"]
    def dev(code, t, loc, **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": kw.pop("status", "idle"),
                "tags": kw.pop("tags", T), "available_commands": kw.pop("cmds", ["travel"]), **kw}
    devices = [
        dev("MF000001", "mobile_fleet", "AEM-5-L4", features=["surge", "cruise", "attach"], attach_capacity=36, attached_devices=[]),
        dev("MC000001", "ami_mining_controller", "AEM-5-L4"),
        dev("SC000001", "ami_survey_controller", "AEM-BELT-1"),
        dev("TC000001", "ami_transport_controller", "AEM-5-L4"),
        dev("MD000001", "mining_drone", "AEM-5-L4"), dev("MD000002", "mining_drone", "AEM-5-L4", attached_to_device_code="MF000001"),
        dev("SD000001", "survey_drone", "AEM-BELT-1"),
        dev("CF000001", "cargo_freighter", "AEM-5-L4", features=["surge", "cruise", "transport"], cargo_capacity=500, cargo_used=0),
        dev("LOST0001", "mining_drone", "FAL-BELT-1"),                       # a member stranded elsewhere
        dev("LOCAL001", "mining_drone", "AEM-5-L4", tags=["home:aem"]),      # not in the fleet
    ]
    fleet = {"id": "prospector-1", "name": "Prospector-1", "role": "mining", "home": "AEM",
             "wants": {"mobile_fleet": 1, "mining_drone": 4, "ami_mining_controller": 1}}
    return fleet, devices


def test_fleet_roster_assemble_and_travel():
    from rsweb import fleets as fl
    fleet, devices = _fleet_world()
    r = fl.roster(fleet, devices)
    assert r["capacity"] == 36 and "LOCAL001" not in [d["device_code"] for d in r["members"]]
    md = next(x for x in r["rows"] if x["type"] == "mining_drone")
    assert md["have"] == 3 and md["short"] == 1 and "MD000002" in r["riding"]
    steps, problems = fl.assemble_steps(fleet, devices)
    bodies = [(s["path"], s["body"]) for s in steps]
    # the survey pair at the belt fly to the carrier first; the freighter flies itself, so it doesn't board
    assert ("/devices/SC000001", {"command": "travel", "destination": "AEM-5-L4"}) in bodies
    assert ("/devices/MF000001", {"command": "attach", "device": "MD000001"}) in bodies
    assert ("/devices/MF000001", {"command": "attach", "device": "MD000002"}) not in bodies     # already on board
    assert not any(b and b.get("device") == "CF000001" for _, b in bodies)
    assert not any("LOST0001" in p for p in problems)          # left to the gather phase
    trav = [(s["path"], s["body"]) for s in fl.travel_steps(fleet, devices, "KEL", {"KEL": {"entry_point": "KEL-3-L4"}}) if s["body"]]
    assert sorted(trav) == [("/devices/CF000001", {"command": "travel", "destination": "KEL-3-L4"}),
                            ("/devices/MF000001", {"command": "travel", "destination": "KEL-3-L4"})]


def test_fleet_mining_work_watch_and_phases():
    from rsweb import fleets as fl
    fleet, devices = _fleet_world()
    for d in devices:
        if fl.fleet_of(d):
            d["location"] = "KEL-3-L4"
    steps, problems = fl.mining_work_steps(fleet, devices, "KEL-BELT-1", "FAL-BELT-1")
    bodies = [s["body"] for s in steps if s["body"]]
    assert {"command": "set_directive", "directive": "belt_search", "configuration": {}} in bodies
    assert {"command": "set_directive", "directive": "gather_evenly", "configuration": {}} in bodies
    assert {"command": "set_directive", "directive": "ferry", "configuration": {"collect": "KEL-BELT-1", "deliver": "FAL-BELT-1"}} in bodies
    assert {"command": "adopt", "devices": ["MD000001", "MD000002", "LOST0001"]} in bodies and not problems
    # watch: exhausted, nothing searching → done only after the time limit
    mc = next(d for d in devices if d["device_code"] == "MC000001")
    mc["ami_directive"] = {"name": "gather_evenly", "_eval_state": "exhausted:['rares']:KEL-BELT-1"}
    m = {"opts": {"exhausted_minutes": 30}}
    done, why, upd = fl.watch_done(fleet, m, devices, "2026-10-02T10:00:00+00:00")
    assert not done and upd["exhausted_since"] == "2026-10-02T10:00:00+00:00"
    m.update(upd)
    assert fl.watch_done(fleet, m, devices, "2026-10-02T10:31:00+00:00")[0]
    next(d for d in devices if d["device_code"] == "SD000001")["status"] = "searching"
    assert not fl.watch_done(fleet, m, devices, "2026-10-02T10:31:00+00:00")[0]      # still finding sites
    # explore runs work→watch→recall per target, then goes home
    m = {"phase": "recall", "idx": 0, "targets": ["A", "B"]}
    assert fl.next_phase("explore", m) == "travel" and m["idx"] == 1
    m["phase"] = "recall"
    assert fl.next_phase("explore", m) == "return"
    assert fl.next_phase("mining", {"phase": "unload"}) is None


def test_fleet_devices_are_left_alone_by_loadouts_and_ami():
    from rsweb import loadouts as lo
    from rsweb.ami_schedule import adoptable
    fleet, devices = _fleet_world()
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"mining_drone": 0}}], "systems": {"AEM": "p"}}
    p = lo.plan(cfg, devices, [], {}, {}, {}, set(), [], {})
    assert set(p["tag_add"]) == {"LOCAL001"}                     # only the non-fleet drone is touched
    local_ctrl = {"device_code": "XC", "device_type": "ami_mining_controller", "location": "AEM-5-L4", "tags": []}
    assert adoptable(devices, local_ctrl, {}) == ["LOCAL001"]
    fleet_ctrl = next(d for d in devices if d["device_code"] == "MC000001")
    assert adoptable(devices, fleet_ctrl, {}) == ["MD000001"]     # its own fleet's idle drone at its location


def test_a_mission_stalled_out_of_comms_range_resumes_once_back_in_range(client):
    """Live 2026-10-22: Surveyor 2's heaven vessel left ALFOLEN, the recall failed on 'Device is out of comms range'
    and the mission sat stalled. Now it waits and re-runs the phase once every member is in range again."""
    db, eng = client.app.state.db, client.app.state.worker.automations
    devs = [{"device_code": "SC1", "device_type": "ami_survey_controller", "location": "ALF", "status": "out_of_range",
             "in_control_range": False, "tags": ["fleet:s2"]}]
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(eng.save_fleets, [{"id": "s2", "name": "S2", "role": "explore", "home": "FAL", "wants": {},
                                          "mission": {"status": "stalled", "phase": "recall", "comms_wait": True,
                                                      "targets": ["ALF"], "idx": 0, "log": []}}])
    client.portal.call(eng.run_fleets)
    assert client.portal.call(eng.fleets)[0]["mission"]["status"] == "stalled"
    devs[0]["in_control_range"] = True
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(eng.run_fleets)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert "comms_wait" not in m and any("back in comms range" in x["text"] for x in m["log"])


def test_fleet_pages_and_mission_launch(client):
    eng = client.app.state.worker.automations
    r = client.post("/fleets", data={"name": "Prospector 1", "role": "mining", "home": "SOL"}, headers=HX)
    assert r.headers.get("HX-Refresh")
    fleet, devices = _fleet_world()
    for d in devices:
        if fl_tag := next((t for t in d["tags"] if t.startswith("fleet:")), None):
            d["tags"] = ["fleet:prospector-1"]
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Prospector 1" in page and "fleet:prospector-1" in page and "attach 7 / 36" in page
    client.post("/fleets/prospector-1/edit", data={"name": "Prospector 1", "role": "mining", "home": "SOL",
                                                   "want:mining_drone": "4", "want:mobile_fleet": "1"}, headers=HX)
    r = client.post("/fleets/prospector-1/mission", data={"targets": "ABOTEIN", "exhausted_minutes": "30"}, headers=HX)
    if "loadout not complete" in r.text:   # nothing leaves short unless you say so
        assert "mission" not in client.portal.call(eng.fleets)[0]
        r = client.post("/fleets/prospector-1/mission", data={"targets": "ABOTEIN", "exhausted_minutes": "30", "partial": "on"},
                        headers=HX)
    f = client.portal.call(eng.fleets)[0]
    assert f["mission"]["status"] == "running" and f["mission"]["phase"] == "assemble"
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "fleets" and j.get("meta", {}).get("fleet") == "prospector-1"][-1]
    assert any((s["body"] or {}).get("command") == "attach" for s in job["steps"])
    # the mock doesn't know these devices, so boarding fails → the mission stalls and says why
    client.portal.call(eng.run_fleets)
    f = client.portal.call(eng.fleets)[0]
    assert f["mission"]["status"] == "stalled" and "stalled in assemble" in f["mission"]["log"][-1]["text"]
    assert "Retry" in client.get("/fleets/details?all=1", headers=H).text
    # membership: adding a device swaps its home/spare tags for the fleet tag
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    world.devices[1]["tags"] = ["home:sol", "spare"]
    client.portal.call(client.app.state.worker.sync_devices)
    client.post("/fleets/prospector-1/members", data={"add": world.devices[1]["device_code"]}, headers=HX)
    assert world.devices[1]["tags"] == ["fleet:prospector-1"]


def test_fleet_print_shortfall_tags_prints(client):
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(eng.save_fleets, [{"id": "p1", "name": "P1", "role": "mining", "home": "SOL", "wants": {"mining_drone": 2}}])
    r = client.post("/fleets/p1/print", headers=HX)
    assert "print 2× mining drone" in r.text
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "chain"][-1]
    assert job["steps"][0]["path"] == "/devices/AF00BEEF"
    assert job["steps"][0]["body"] == {"command": "enqueue_print", "device_type": "mining_drone", "quantity": 2, "tags": ["fleet:p1"]}


def test_printed_devices_go_home_on_a_surge_platform():
    from rsweb import loadouts as lo
    devices = [
        {"device_code": "AF", "device_type": "autofactory", "location": "FAL-BELT-1", "status": "printing (survey_drone)",
         "available_commands": ["enqueue_print"], "tags": ["fleet:fal"]},
        {"device_code": "PL000001", "device_type": "surge_platform", "location": "FAL-1-L4", "status": "idle", "attach_capacity": 4,
         "features": ["surge", "cruise", "attach", "taxi"], "taxi_mode": "taxi", "available_commands": ["attach", "detach", "travel"],
         "tags": ["fleet:fal"]},
        # printed here for AEM's stationed fleet (a member already, or to:aem by the print) — both must be delivered
        {"device_code": "SD1", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "idle", "tags": ["fleet:aem"],
         "available_commands": ["travel", "scan"]},
        {"device_code": "SD2", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "idle", "tags": ["to:aem"],
         "available_commands": ["travel", "scan"]},
        # away but working for a controller there: left alone
        {"device_code": "MD1", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "idle", "tags": ["fleet:aem"],
         "controller_device_code": "XC"},
    ]
    stars = {"FAL": {"position": {"x": 0, "y": 0, "z": 0}}, "AEM": {"position": {"x": 1, "y": 0, "z": 0}, "entry_point": "AEM-5-L4"}}
    fleets = [{"id": "aem", "name": "Aem", "home": "AEM", "station": True, "wants": {}},
              {"id": "fal", "name": "Fal", "home": "FAL", "station": True, "wants": {}}]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert p["returning"] == ["SD1"] and not p["deliveries"]   # Fal's platform delivers nothing for another fleet
    devices[1]["tags"] = ["fleet:cab"]                          # a logistics fleet's does
    fleets.append({"id": "cab", "name": "Cab", "role": "logistics", "home": "FAL"})
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert [(d["carrier"], d["mode"], sorted(d["devices"]), d["to"]) for d in p["deliveries"]] == [("PL000001", "attach", ["SD1", "SD2"], "AEM")]
    bodies = [s["body"] for s in lo.delivery_steps(p["deliveries"][0], p["by_code"], stars, True)]
    assert {"command": "travel", "destination": "FAL-1-L4"} in bodies                    # drones fly to the platform first
    assert {"command": "attach", "device": "SD1"} in bodies and {"command": "travel", "destination": "AEM-5-L4"} in bodies
    assert any("send it back to AEM" in line for line in lo.describe(p))
    # on a mission, a fleet's devices are the mission's: nothing is sent home
    fleets[0]["mission"] = {"status": "running"}
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert p["returning"] == [] and "SD1" not in p["by_code"]


def test_fleet_gather_collects_strays_and_recruits_spares():
    from rsweb import fleets as fl
    fleet, devices = _fleet_world()
    fleet["wants"]["mining_drone"] = 5     # has 3 (MD1, MD2, LOST) → 2 short
    devices += [
        {"device_code": "SPARE01", "device_type": "mining_drone", "location": "FAL-2", "status": "idle", "tags": ["spare"]},
        {"device_code": "SPARE02", "device_type": "mining_drone", "location": "ITH-BELT-1", "status": "idle", "tags": ["spare"]},
        {"device_code": "SPARE03", "device_type": "mining_drone", "location": "AEM-3", "status": "idle", "tags": ["spare"]},
        {"device_code": "BUSY01", "device_type": "mining_drone", "location": "AEM-3", "status": "idle", "tags": ["spare"],
         "controller_device_code": "X"},
        {"device_code": "SPARE04", "device_type": "survey_drone", "location": "AEM-3", "status": "idle", "tags": ["spare"]},  # not needed
    ]
    stars = {"AEM": {"position": {"x": 0, "y": 0, "z": 0}}, "FAL": {"position": {"x": 2, "y": 0, "z": 0}, "entry_point": "FAL-1-L4"},
             "ITH": {"position": {"x": 9, "y": 0, "z": 0}}}
    plan = fl.gather_plan(fleet, devices, stars, set())
    # nearest spares first: SPARE03 is in AEM with the carrier (boards next assemble/at travel), SPARE01 in FAL
    assert [d["device_code"] for d in plan["recruit"]] == ["SPARE03", "SPARE01"]
    assert plan["carrier"] == "MF000001"
    assert [(s_, sorted(d["device_code"] for d in ds)) for s_, ds in plan["tour"]] == [("AEM", ["SPARE03"]), ("FAL", ["LOST0001", "SPARE01"])]
    bodies = [(st["path"], st["body"]) for st in fl.gather_steps(fleet, plan, stars)]
    assert ("/devices/SPARE01", {"configuration": {"add_tags": ["fleet:prospector-1"], "remove_tags": ["spare"]}}) in bodies
    assert ("/devices/MF000001", {"command": "travel", "destination": "FAL-1-L4"}) in bodies
    assert ("/devices/LOST0001", {"command": "travel", "destination": "FAL-1-L4"}) in bodies
    assert ("/devices/MF000001", {"command": "attach", "device": "SPARE01"}) in bodies
    # SPARE03 boards right where the carrier is (no carrier trip), before it sets off
    i_local = bodies.index(("/devices/MF000001", {"command": "attach", "device": "SPARE03"}))
    assert ("/devices/SPARE03", {"command": "travel", "destination": "AEM-5-L4"}) in bodies
    assert i_local < bodies.index(("/devices/MF000001", {"command": "travel", "destination": "FAL-1-L4"}))


def test_device_snapshot_guard_while_replicant_travels():
    from rsweb.ingest import merge_device_snapshot
    from rsweb import loadouts as lo
    prev = [{"device_code": f"D{i}", "device_type": "mining_drone", "location": "AEM-BELT-1", "status": "idle", "tags": ["home:aem"]}
            for i in range(6)]
    assert merge_device_snapshot(prev, []) is None                       # empty → keep the old list
    assert merge_device_snapshot(prev, prev[:2]) is None                 # far shorter → keep the old list
    blank = [{**d, "location": None} for d in prev]                      # mid-surge: no locations
    merged = merge_device_snapshot(prev, blank)
    assert all(d["location"] == "AEM-BELT-1" and d["location_stale"] for d in merged)
    stowed = [{**prev[0], "location": None, "stowed_in_device_code": "V1"}]
    assert merge_device_snapshot(prev[:1], stowed)[0]["location"] is None   # genuinely stowed stays as is
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"mining_drone": 2}}], "systems": {"AEM": "p"}}
    p = lo.plan(cfg, merged, [], {}, {}, {}, set(), [], {})
    assert p["tag_add"] == {} and p["moves"] == {} and any(u["type"] == "data" for u in p["unmet"])
    assert any("waiting for good data" in line for line in lo.describe(p))


def test_partial_device_pages_while_travelling():
    from rsweb.ingest import merge_device_snapshot
    prev = [{"device_code": f"D{i}", "location": "AEM-BELT-1", "status": "idle"} for i in range(80)]
    page2 = [{"device_code": f"D{i}", "location": "FAL-BELT-1", "status": "idle"} for i in range(50, 80)]
    merged = merge_device_snapshot(prev, page2, partial=True)       # empty first page + cursor → partial
    assert len(merged) == 80
    assert sum(1 for d in merged if d.get("location_stale")) == 50
    assert next(d for d in merged if d["device_code"] == "D60")["location"] == "FAL-BELT-1"   # fresh data wins


def test_sync_devices_with_empty_first_page(client):
    world = client.app.state.api.http._transport.app.state.world
    w = client.app.state.worker
    client.portal.call(w.sync_devices)
    n = len(client.portal.call(client.app.state.db.kv_get, "devices"))
    app = client.app.state.api.http._transport.app
    # make the mock behave like the game mid-travel: first page empty but with a cursor
    from starlette.routing import Route
    orig = [r for r in app.router.routes if getattr(r, "path", "") == "/v1/devices"][0]
    async def travelling(request):
        from starlette.responses import JSONResponse
        cur = request.query_params.get("cursor")
        if not cur:
            return JSONResponse({"devices": [], "next_cursor": 5})
        return JSONResponse({"devices": world.devices[5:], "next_cursor": None})
    app.router.routes.insert(0, Route("/v1/devices", travelling))
    try:
        client.portal.call(w.sync_devices)
    finally:
        app.router.routes.pop(0)
    devs = client.portal.call(client.app.state.db.kv_get, "devices")
    assert len(devs) == n and sum(1 for d in devs if d.get("location_stale")) == 5


def test_fleet_loadout_lines_add_and_remove(client):
    eng = client.app.state.worker.automations
    client.portal.call(eng.save_fleets, [{"id": "p1", "name": "P1", "role": "mining", "home": "SOL", "wants": {}}])
    world = client.app.state.api.http._transport.app.state.world
    world.devices[1]["tags"] = ["fleet:p1"]
    dtype, code = world.devices[1]["device_type"], world.devices[1]["device_code"]
    client.portal.call(client.app.state.worker.sync_devices)
    # picking a type adds it straight away with qty 1 (no qty sent)
    r = client.post("/fleets/p1/want", data={"type": "ftl_relay_x"}, headers=HX)
    assert r.headers.get("HX-Refresh") and client.portal.call(eng.fleets)[0]["wants"] == {"ftl_relay_x": 1}
    client.post("/fleets/p1/want", data={"type": "ftl_relay_x", "qty": "3"}, headers=HX)
    client.post("/fleets/p1/want", data={"type": dtype, "qty": "1"}, headers=HX)
    page = client.get("/fleets/details?all=1", headers=H).text
    assert 'loadout-form' in page and "✕" in page
    # Save (name/role/home) no longer touches the loadout
    client.post("/fleets/p1/edit", data={"name": "P1b", "role": "mining", "home": "SOL"}, headers=HX)
    assert client.portal.call(eng.fleets)[0]["wants"] == {"ftl_relay_x": 3, dtype: 1}
    assert client.portal.call(eng.fleets)[0]["id"] == "p1b"                  # renamed: the id (and tag) follow the name
    # qty 0 removes the line entirely and releases that type's members
    client.post("/fleets/p1b/want", data={"type": dtype, "qty": "0"}, headers=HX)
    assert client.portal.call(eng.fleets)[0]["wants"] == {"ftl_relay_x": 3}
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "fleets" and "retag" not in j["title"]][-1]
    assert job["steps"][0]["path"] == f"/devices/{code}" and job["steps"][0]["body"] == {"configuration": {"remove_tags": ["fleet:p1b"]}}
    # sent while the game still has the old tag: the old one goes too, or the device would rejoin at the next sync
    from rsweb.fleets import with_renamed_tags
    assert with_renamed_tags("PATCH", f"/devices/{code}", job["steps"][0]["body"], {"p1": "p1b"}) == \
        {"configuration": {"remove_tags": ["fleet:p1b", "fleet:p1"]}}
    import time
    for _ in range(50):
        if "fleet:p1" not in (world.devices[1].get("tags") or []):
            break
        time.sleep(0.1)
    assert not any(t.startswith("fleet:p1") for t in world.devices[1].get("tags") or [])


def test_fleet_loadout_editor_lines(client):
    eng = client.app.state.worker.automations
    client.portal.call(eng.save_fleets, [{"id": "p1", "name": "P1", "role": "mining", "home": "SOL", "wants": {}}])
    world = client.app.state.api.http._transport.app.state.world
    world.devices[1]["tags"] = ["fleet:p1"]
    dtype, code = world.devices[1]["device_type"], world.devices[1]["device_code"]
    client.portal.call(client.app.state.worker.sync_devices)
    base = {"lines": "1", "name": "P1", "role": "mining", "home": "SOL", "autosave": "1"}
    # several lines; a blank line and a qty-0 line are ignored
    r = client.post("/fleets/p1/edit", data={**base, "type": [dtype, "survey_drone", "", "relay_x"], "qty": ["2", "1", "", "0"]}, headers=HX)
    assert "Loadout saved" in r.text and not r.headers.get("HX-Refresh")
    assert client.portal.call(eng.fleets)[0]["wants"] == {dtype: 2, "survey_drone": 1}
    page = client.get("/fleets/details?all=1", headers=H).text
    assert page.count('class="lnew"') == 1 and 'name="lines"' in page
    # removing a type with members (release) drops the line and releases them
    client.post("/fleets/p1/edit", data={**base, "type": [dtype, "survey_drone"], "qty": ["0", "1"], "release": dtype}, headers=HX)
    assert client.portal.call(eng.fleets)[0]["wants"] == {"survey_drone": 1}
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "fleets"][-1]
    assert job["steps"][0]["path"] == f"/devices/{code}"
    # Save without autosave refreshes the page; everything removed → empty loadout
    r = client.post("/fleets/p1/edit", data={"lines": "1", "name": "P1", "role": "mining", "home": "SOL"}, headers=HX)
    assert r.headers.get("HX-Refresh") and client.portal.call(eng.fleets)[0]["wants"] == {}


def _bound_controller_world():
    return [
        {"device_code": "PL000001", "device_type": "surge_platform", "location": "FAL-1-L4", "status": "idle", "attach_capacity": 4,
         "features": ["surge", "cruise", "attach"], "available_commands": ["attach", "detach", "travel"], "tags": ["home:fal"]},
        # printed in FAL for ITHVALAI, but an "all survey controllers" schedule put it to work there
        {"device_code": "SC000001", "device_type": "ami_survey_controller", "location": "FAL-BELT-1", "status": "coordinating",
         "features": ["cruise", "ami"], "available_commands": ["adopt", "release", "set_directive", "clear_directive", "launch", "travel"],
         "ami_directive": {"name": "belt_search", "config": {}, "_eval_state": "searching:1:0"}, "ami_directive_status": "active",
         "tags": ["home:ithvalai", "to:ithvalai"]},
        {"device_code": "SD000001", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "idle",
         "controller_device_code": "SC000001", "tags": ["home:fal"]},
        {"device_code": "SD000002", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "idle", "tags": ["to:ithvalai"]},
    ]


def test_bound_controller_drops_its_work_and_leaves():
    from rsweb import loadouts as lo
    stars = {"FAL": {"position": {"x": 0, "y": 0, "z": 0}}, "ITHVALAI": {"position": {"x": 1, "y": 0, "z": 0}, "entry_point": "ITHVALAI-2-L4"}}
    p = lo.plan({"phases": [], "systems": {}}, _bound_controller_world(), [], {}, stars, {}, set(), [], {})
    dl = next(d for d in p["deliveries"] if "SC000001" in d["devices"])
    assert dl["to"] == "ITHVALAI" and dl["mode"] == "attach"
    steps = lo.delivery_steps(dl, p["by_code"], stars, True, p["managed"])
    bodies = [(s["path"], s["body"]) for s in steps]
    rel = bodies.index(("/devices/SC000001", {"command": "release", "devices": ["SD000001"]}))
    clr = bodies.index(("/devices/SC000001", {"command": "clear_directive"}))
    att = bodies.index(("/devices/PL000001", {"command": "attach", "device": "SC000001"}))
    assert rel < clr < att
    # a busy bound device says why it isn't moving
    p = lo.plan({"phases": [], "systems": {}}, _bound_controller_world(), [], {}, stars, {}, {"SC000001"}, [], {})
    assert any("SC000001 can't leave FAL yet" in u["why"] for u in p["unmet"])

    # riding a carrier a running job holds: it waits for the carrier, and says so
    world = _bound_controller_world() + [
        {"device_code": "MF000001", "device_type": "mobile_fleet", "location": "FAL-BELT-1", "status": "idle",
         "features": ["surge", "cruise", "attach"], "tags": []}]
    next(d for d in world if d["device_code"] == "SD000002")["attached_to_device_code"] = "MF000001"
    p = lo.plan({"phases": [], "systems": {}}, world, [], {}, stars, {}, {"MF000001"}, [], {})
    assert not any("SD000002" in d["devices"] for d in p["deliveries"])
    assert any("SD000002 can't leave FAL yet: it rides MF000001" in u["why"] for u in p["unmet"])


def test_work_rules_skip_devices_bound_elsewhere():
    from rsweb.ami_schedule import adoptable, targets_of, in_transit
    devices = _bound_controller_world()
    assert in_transit(devices[1]) and not in_transit(devices[0])
    assert not in_transit({"device_code": "X", "location": "ITHVALAI-2-L4", "tags": ["to:ithvalai"]})   # arrived
    assert [d["device_code"] for d in targets_of({"target": "kind:survey"}, devices)] == []
    other = {"device_code": "SC2", "device_type": "ami_survey_controller", "location": "FAL-BELT-1", "status": "idle", "tags": []}
    assert adoptable(devices + [other], other, {"SD000001": "SC000001"}) == []    # SD000002 is bound for ITHVALAI


def test_fleet_attach_points():
    from rsweb import fleets as fl
    fleet, devices = _fleet_world()
    bps = {"surge_platform": {"device_type": "surge_platform", "features": ["surge", "attach"], "attach_capacity": 4},
           "transport_hauler": {"device_type": "transport_hauler", "features": ["cruise", "transport"]}}
    pt = fl.attach_points(fleet, devices, bps)
    # members: one mobile fleet (36); riders = 3 controllers + 3 mining drones + 1 survey drone; the freighter flies itself
    # (the test devices list no stow command, so they all need attach points)
    assert (pt["now"]["available"], pt["now"]["needed"], pt["now"]["short"], pt["now"]["attached"]) == (36, 7, 0, 1)
    assert pt["now"]["attach"] == {"available": 36, "needed": 7}
    assert pt["plan"]["attach"]["available"] == 36 and pt["plan"]["needed"] == 8      # 4 mining drones wanted (3 have)
    # swap the mobile fleet for one platform and add 10 haulers → short
    fleet["wants"] = {"surge_platform": 1, "transport_hauler": 10}
    devices = [d for d in devices if d["device_type"] != "mobile_fleet"]
    pt = fl.attach_points(fleet, devices, bps)
    assert pt["now"]["available"] == 0 and pt["now"]["short"] == 7
    assert pt["plan"]["attach"]["available"] == 4 and pt["plan"]["needed"] == 17 and pt["plan"]["short"] == 13
    assert pt["plan"]["fix"].startswith("2 surge carrier(s), 4 surge platform(s)")
    assert fl.type_profile("cargo_freighter", bps, devices) == {"carrier": 0, "hold": 0, "flies": True, "stowable": False}


def test_cargo_vessel_counts_hold_and_attach():
    from rsweb import fleets as fl
    T = ["fleet:hauler"]
    def dev(code, t, **kw):
        return {"device_code": code, "device_type": t, "location": "FAL-1-L4", "status": "idle", "tags": T, **kw}
    stow_cmds = ["travel", "stow", "deploy"]
    devices = [dev("CV1", "cargo_vessel", features=["surge", "cruise", "stow", "attach"], stow_capacity=50, attach_capacity=3,
                   available_commands=["travel", "attach", "detach"])]
    devices += [dev(f"MD{i}", "mining_drone", features=["cruise", "mine", "stow"], available_commands=stow_cmds) for i in range(6)]
    devices += [dev(f"TD{i}", "transport_drone", features=["cruise", "transport"], available_commands=["travel"]) for i in range(2)]
    fleet = {"id": "hauler", "name": "Hauler", "role": "mining", "home": "FAL",
             "wants": {"cargo_vessel": 1, "mining_drone": 6, "transport_drone": 4}}
    assert fl.is_carrier(devices[0]) and fl.hold(devices[0]) == 50 and fl.capacity(devices[0]) == 3
    pt = fl.attach_points(fleet, devices, {})
    assert pt["now"]["hold"] == {"available": 50, "needed": 6, "used": 6} and pt["now"]["attach"] == {"available": 3, "needed": 2}
    assert pt["now"]["short"] == 0 and pt["now"]["available"] == 53
    # loadout: 4 transport drones need attach points, only 3 → short 1 (the hold can't take them)
    assert pt["plan"]["attach"] == {"available": 3, "needed": 4} and pt["plan"]["short"] == 1
    assert pt["plan"]["fix"] == "1 surge carrier(s), 1 surge platform(s) or 1 cargo vessel(s)"
    prof = fl.type_profile("cargo_vessel", {}, [])   # from known sizes, no device or blueprint
    assert prof["carrier"] == 3 and prof["hold"] == 50 and not prof["flies"]
    # assemble: transport drones attach, mining drones stow themselves into the vessel
    steps, problems = fl.assemble_steps(fleet, devices)
    bodies = [(s["path"], s["body"]) for s in steps]
    assert problems == []
    assert ("/devices/CV1", {"command": "attach", "device": "TD0"}) in bodies and ("/devices/CV1", {"command": "attach", "device": "TD1"}) in bodies
    assert sum(1 for p, b in bodies if b and b.get("command") == "stow" and b.get("target") == "CV1") == 6
    # unload: deploy the stowed, detach the attached
    for d in devices:
        if d["device_type"] == "mining_drone":
            d["stowed_in_device_code"] = "CV1"
        elif d["device_type"] == "transport_drone":
            d["attached_to_device_code"] = "CV1"
    cmds = sorted(((s["body"] or {}).get("command"), s["path"]) for s in fl.unload_steps(fleet, devices))
    assert cmds.count(("deploy", "/devices/MD0")) == 1 and ("detach", "/devices/CV1") in cmds
    # only the carrier flies; stowed and attached members ride along
    assert {s["path"] for s in fl.travel_steps(fleet, devices, "AEM", {}) if s["method"] != "WAIT"} == {"/devices/CV1"}

def _arrival_world():
    A = ["adopt", "release", "set_directive", "launch", "activate"]
    return [
        {"device_code": "MC1", "device_type": "ami_mining_controller", "location": "ITH-BELT-1", "status": "coordinating",
         "features": ["ami"], "available_commands": A, "ami_directive": {"name": "gather_evenly"}, "tags": ["home:ith"]},
        {"device_code": "SC1", "device_type": "ami_survey_controller", "location": "ITH-3", "status": "idle",
         "features": ["ami"], "available_commands": A, "tags": ["home:ith"]},
        {"device_code": "TC1", "device_type": "ami_transport_controller", "location": "ITH-2-L4", "status": "idle",
         "features": ["ami"], "available_commands": A, "tags": ["home:ith", "ferry"]},          # ferry: never takes drones
        # delivered to the entry point: not where the controllers work
        {"device_code": "MD1", "device_type": "mining_drone", "location": "ITH-2-L4", "status": "idle", "tags": ["home:ith"]},
        {"device_code": "SD1", "device_type": "survey_drone", "location": "ITH-2-L4", "status": "idle", "tags": ["home:ith"]},
        {"device_code": "TD1", "device_type": "transport_drone", "location": "ITH-2-L4", "status": "idle", "tags": ["home:ith"]},
        {"device_code": "MD2", "device_type": "mining_drone", "location": "ITH-BELT-1", "status": "mining (carbon)",
         "controller_device_code": "MC1", "tags": ["home:ith"]},                                  # already run
        {"device_code": "MD3", "device_type": "mining_drone", "location": "ITH-2-L4", "status": "idle", "tags": ["home:fal"]},  # passing through
        {"device_code": "MD4", "device_type": "mining_drone", "location": "ITH-2-L4", "status": "idle", "tags": ["to:fal"]},    # leaving
        {"device_code": "MD5", "device_type": "mining_drone", "location": "ITH-2-L4", "status": "idle", "tags": ["spare"]},     # spare
        {"device_code": "MT1", "device_type": "maintenance_drone", "location": "ITH-2-L4", "status": "inactive",
         "available_commands": ["activate", "travel"], "tags": ["home:ith"]},
        {"device_code": "MT2", "device_type": "maintenance_drone", "location": "FAL-2-L4", "status": "inactive",
         "available_commands": ["activate", "travel"], "tags": ["home:ith"]},                     # not home yet
    ]


def test_arrivals_join_their_systems_controller():
    from rsweb.ami_schedule import handoffs, handoff_steps
    hs = handoffs(_arrival_world(), {}, set())
    assert [(h["drone"], h["controller"]) for h in hs] == [("MD1", "MC1"), ("SD1", "SC1")]   # TD1: only a ferry controller there
    md = hs[0]
    bodies = [(s["path"], s["body"]) for s in handoff_steps(md)]
    assert bodies == [("/devices/MD1", {"command": "travel", "destination": "ITH-BELT-1"}),
                      ("/devices/MC1", {"command": "adopt", "devices": ["MD1"]}),
                      ("/devices/MC1", {"command": "launch"})]                                  # MC1 is running a directive
    assert [s["body"]["command"] for s in handoff_steps(hs[1])] == ["travel", "adopt"]          # SC1 is idle: no launch
    assert handoffs(_arrival_world(), {"MD1": "MC9"}, {"SD1"}) == []                           # managed per digests / busy


def test_recall_waiting_drones_join_their_systems_controller():
    # live 2026-10-28: drones a controller let go mid-recall stood recall_waiting for weeks, never adopted
    from rsweb.ami_schedule import handoffs, adoptable
    w = _arrival_world()
    for d in w:
        if d["device_code"] == "MD1":
            d["status"] = "recall_waiting"
    assert [(h["drone"], h["controller"]) for h in handoffs(w, {}, set())][0] == ("MD1", "MC1")
    ctrl = next(d for d in w if d["device_code"] == "MC1")
    ctrl = dict(ctrl, location="ITH-2-L4")
    assert "MD1" in adoptable(w, ctrl, {})


def test_inactive_arrivals_are_activated_once():
    from rsweb.ami_schedule import wakeups
    w = _arrival_world()
    from rsweb.ami_schedule import wakeup_steps
    assert wakeups(w, set(), {}) == [{"code": "MT1", "activate": False, "patrol": True}]
    assert [s["body"] for s in wakeup_steps(wakeups(w, set(), {})[0])] == [{"command": "set_directive", "directive": "patrol"}]
    ctrl = {"device_code": "SC9", "device_type": "ami_survey_controller", "location": "ITH-3", "status": "inactive",
            "available_commands": ["activate"], "tags": ["home:ith"]}
    assert wakeups([ctrl], set(), {}) == [{"code": "SC9", "activate": True, "patrol": False}]
    assert wakeups(w, set(), {"MT1": "ITH"}) == []           # already done on this arrival
    # active at home but idle with no directive: patrol only; already patrolling: nothing
    w[10].update(status="idle")
    assert wakeups(w, set(), {}) == [{"code": "MT1", "activate": False, "patrol": True}]
    w[10].update(status="patrolling", ami_directive={"name": "patrol"})
    assert wakeups(w, set(), {}) == []


def test_loadout_pass_hands_off_and_activates(client):
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.db.kv_set, "devices", _arrival_world())
    lines = client.portal.call(eng.apply_loadouts, None, True)
    assert "MD1 joins controller MC1 (flies ITH-2-L4 → ITH-BELT-1)" in lines
    assert any(l.startswith("patrol MT1") for l in lines)
    jobs = client.portal.call(eng.jobs)
    assert any(j["title"] == "loadouts: patrol MT1" for j in jobs)
    assert any(j["title"] == "loadouts: MC1 adopts MD1" for j in jobs)
    assert client.portal.call(client.app.state.db.kv_get, "loadout_woken", {}) == {"MT1": "ITH"}


def test_refresh_without_a_scan_reads_the_star_and_salvage_bodies(client):
    db = client.app.state.db
    client.portal.call(db.execute, "DELETE FROM systems WHERE star=?", ("SOL",))
    r = client.post("/systems/SOL/resources/refresh", headers=HX)
    assert "read 1 belt(s): 2 open mining site(s)" in r.text
    row = client.portal.call(db.fetchone, "SELECT data FROM systems WHERE star=?", ("SOL",))
    assert row and "asteroid_belt" in row["data"]           # GET /locations/SOL stood in for the missing scan


def test_body_salvage_percentages_and_used_up():
    import asyncio
    from rsweb.targets import system_resources

    class FakeDB:
        def __init__(self, kv, events):
            self.kv, self.events = kv, events
        async def fetchall(self, q, args=()):
            if "FROM events" in q:
                return self.events
            if "FROM kv" in q:
                return [{"key": k, "value": json.dumps(v), "updated_at": "2026-10-02T10:00:00"} for k, v in self.kv.items()]
            return []
        async def fetchone(self, q, args=()):
            return None
        async def kv_get(self, k, default=None):
            return default
    body = {"location": "KELMONENT-1", "location_type": "planet", "resource_sites": [
        {"designation": "KELMONENT-1-SAL-2", "name": "Orbital Debris Field", "site_type": "salvage", "site_index": 1,
         "resources_remaining_pct": {"conductive": 100, "structural": 100}},
        {"designation": "KELMONENT-1-SAL-1", "name": "Ejected Instrument Package", "site_type": "salvage", "site_index": 0,
         "resources_remaining_pct": {"rares": 100, "silicates": 100, "volatiles": 100}}]}
    events = [{"event": "salvage.discovered", "created_at": "2026-10-01T00:00:00", "payload": json.dumps(
                   {"designation": "KELMONENT-1-SAL-2", "resources": {"conductive": 40, "structural": 120}})},
              {"event": "salvage.discovered", "created_at": "2026-10-01T00:00:00", "payload": json.dumps(
                   {"designation": "KELMONENT-1-SAL-3", "resources": {"carbon": 50}})}]   # no longer listed: used up
    res = asyncio.run(system_resources(FakeDB({"loc:KELMONENT-1": body}, events), "KELMONENT"))
    shown = {x["code"]: x for x in res["salvage_shown"]}
    assert set(shown) == {"KELMONENT-1-SAL-1", "KELMONENT-1-SAL-2"} and "KELMONENT-1-SAL-3" in res["hidden"]
    assert shown["KELMONENT-1-SAL-2"]["amounts"] == {"conductive": 40, "structural": 120} and shown["KELMONENT-1-SAL-2"]["total"] == 160
    assert shown["KELMONENT-1-SAL-1"]["amounts"] == {} and shown["KELMONENT-1-SAL-1"]["total"] is None   # % known, amounts not
    assert res["sites_shown"] == []                         # salvage isn't counted as mining sites


def test_diagnostics_snapshot_and_mining_diagnosis(client):
    import asyncio
    db = client.app.state.db
    assert "No snapshot yet" in client.get("/diagnostics", headers=H).text
    r = client.post("/diagnostics/snapshot", data={"stars": ""}, headers=HX)
    assert r.headers.get("HX-Refresh")
    for _ in range(100):
        st = client.portal.call(db.kv_get, "snapshot_status", {})
        if st.get("state") != "running":
            break
        client.portal.call(asyncio.sleep, 0.05)
    assert st["state"] == "done", st
    snap = client.portal.call(db.kv_get, "snapshot_last", None)
    paths = [c["path"] for c in snap["calls"]]
    assert "/devices" in paths and "/inventory" in paths and any(p.startswith("/locations/") for p in paths)
    assert snap["requests"] <= 60 and "diagnosis" in snap and "rules" in snap["app"]
    assert snap["app"]["devices"] and "ward_orders" in snap["app"]   # every device in one list
    page = client.get("/diagnostics", headers=H).text
    assert "Mining diagnosis" in page and "Mining drones" in page
    dl = client.get("/diagnostics/snapshot.json", headers=H)
    assert dl.headers["content-type"].startswith("application/json") and "attachment" in dl.headers["content-disposition"]
    assert "dev" not in json.loads(dl.text).get("token", "")      # no token field at all
    assert '"api_token"' not in dl.text


def test_mining_diagnosis_reasons():
    from rsweb.snapshot import diagnose
    devs = [
        {"device_code": "MC1", "device_type": "ami_mining_controller", "location": "KEL-BELT-1", "features": ["ami"], "status": "coordinating",
         "ami_directive": {"name": "gather_evenly", "_eval_state": "exhausted:[carbon]:KEL-BELT-1"}, "ami_directive_status": "active"},
        {"device_code": "MD1", "device_type": "mining_drone", "location": "KEL-BELT-1", "status": "idle", "controller_device_code": "MC1"},
        {"device_code": "MD2", "device_type": "mining_drone", "location": "KEL-2-L4", "status": "idle"},
        {"device_code": "MD3", "device_type": "mining_drone", "location": "KEL-BELT-1", "status": "mining (carbon)", "controller_device_code": "MC1"},
        {"device_code": "MC2", "device_type": "ami_mining_controller", "location": "ITH-BELT-1", "features": ["ami"], "status": "idle"},
    ]
    snap = {"calls": [{"path": "/devices", "status": 200, "body": {"devices": devs}},
                      {"path": "/locations/KEL-BELT-1", "status": 200, "body": {"location_type": "belt", "resource_sites": []}}],
            "app": {"rules": {"restart_idle_miners": {"enabled": False}, "reopen_sites": {"enabled": True}}, "schedules": [], "jobs": []}}
    d = diagnose(snap)
    rows = {r["code"]: r for r in d["drones"]}
    assert rows["MD3"]["state"] == "mining"
    assert any("no open resource sites" in w for w in rows["MD1"]["why"]) and any("reports exhausted" in w for w in rows["MD1"]["why"])
    assert any("isn't a belt" in w for w in rows["MD2"]["why"]) and any("Restart idle miners" in f or "rule is OFF" in w
                                                                         for f in rows["MD2"]["fix"] for w in rows["MD2"]["why"])
    ctrl = {c["code"]: c for c in d["controllers"]}
    assert any("no directive" in n for n in ctrl["MC2"]["notes"])
    assert "1 of 3 mining drones are mining" in d["headline"] and any("KEL-BELT-1" in h for h in d["headline"])


def test_server_runs_and_versioned_log(client):
    from rsweb import version as ver
    db = client.app.state.db
    runs = client.portal.call(db.kv_get, "server_runs", [])
    assert runs[-1]["run"] == ver.RUN_ID and runs[-1]["version"] == ver.VERSION and runs[-1]["fingerprint"] == ver.FINGERPRINT
    log = client.portal.call(db.kv_get, "automation_log", [])
    start = next(e for e in log if e["text"].startswith("server started"))
    assert start["v"] == ver.VERSION and start["run"] == ver.RUN_ID and start["rule"] == "engine"
    # an entry from before versioning and one from an older version are told apart
    log += [{"at": "2026-09-30T10:00:00", "rule": "loadouts", "level": "alert", "text": "old problem"},
            {"at": "2026-10-01T10:00:00", "rule": "loadouts", "level": "alert", "text": "older version problem", "v": "1.3.0", "run": "x"}]
    client.portal.call(db.kv_set, "automation_log", log)
    page = client.get("/automations", headers=H).text
    assert "unversioned" in page and "v1.3.0 older version" in page and "<b>server started" in page
    acct = client.get("/account", headers=H).text
    assert 'id="server"' in acct and ver.RUN_ID in acct and f"v{ver.VERSION}" in acct and "Version history" in acct
    assert f"v{ver.VERSION}" in client.get("/", headers=H).text      # header label


def test_start_notes_explain_restarts_and_upgrades():
    from rsweb import version as ver
    me = {"run": ver.RUN_ID, "version": ver.VERSION, "fingerprint": ver.FINGERPRINT}
    assert "no earlier run on record" in " ".join(ver.start_notes(me, None))
    crashed = {"run": "r1", "version": "1.3.0", "fingerprint": "abc", "started_at": "2026-10-01T00:00:00",
               "last_seen": "2026-10-01T05:00:00", "stopped_at": None}
    lines = ver.start_notes(me, crashed)
    assert any("did not stop cleanly — last seen 2026-10-01T05:00:00" in l for l in lines)
    assert any(l == f"upgraded v1.3.0 → v{ver.VERSION}" for l in lines)
    assert any(l.strip().startswith("v1.4.0 (") for l in lines) and not any(l.strip().startswith("v1.3.0 (") for l in lines)
    same = {**crashed, "version": ver.VERSION, "fingerprint": "zzz", "stopped_at": "2026-10-01T05:00:00"}
    lines = ver.start_notes(me, same)
    assert any("stopped cleanly" in l for l in lines) and any("code changed (zzz →" in l for l in lines)
    assert ver.age_of({"run": ver.RUN_ID}, []) == "current" and ver.age_of({}, []) == "unversioned"
    assert ver.age_of({"run": "x", "v": ver.VERSION}, []) == "earlier run" and ver.age_of({"run": "x", "v": "0.9"}, []) == "older version"


def test_diagnosis_handles_null_locations():
    from rsweb.snapshot import diagnose
    devs = [{"device_code": "MD1", "device_type": "mining_drone", "location": None, "status": "stowed", "stowed_in_device_code": "V1"},
            {"device_code": "MD2", "device_type": "mining_drone", "location": "K-BELT-1", "status": "idle"},
            {"device_code": "SD1", "device_type": "survey_drone", "location": None, "status": "stowed"},
            {"device_code": "MC1", "device_type": "ami_mining_controller", "location": None, "features": ["ami"], "status": "stowed"}]
    snap = {"calls": [{"path": "/devices", "status": 200, "body": {"devices": devs}},
                      {"path": "/locations/K-BELT-1", "status": 200, "body": {"resource_sites": []}}],
            "app": {"rules": {}, "schedules": [], "jobs": [], "log": [{"level": "alert", "text": None}]}}
    d = diagnose(snap)
    rows = {r["code"]: r for r in d["drones"]}
    assert rows["MD1"]["state"] == "stowed" and any("stowed" in w for w in rows["MD1"]["why"])
    assert "diagnosis failed" not in " ".join(d["headline"])


def test_snapshot_capture_with_stowed_devices(client):
    import asyncio
    world = client.app.state.api.http._transport.app.state.world
    for d in world.devices:
        if d["device_type"] == "mining_drone":
            d.update(location=None, status="stowed", stowed_in_device_code="2AC61200")
            break
    db = client.app.state.db
    client.post("/diagnostics/snapshot", data={"stars": ""}, headers=HX)
    for _ in range(100):
        st = client.portal.call(db.kv_get, "snapshot_status", {})
        if st.get("state") != "running":
            break
        client.portal.call(asyncio.sleep, 0.05)
    assert st["state"] == "done", st
    snap = client.portal.call(db.kv_get, "snapshot_last", None)
    assert not any("diagnosis failed" in h for h in snap["diagnosis"]["headline"])
    assert any(r["state"] == "stowed" for r in snap["diagnosis"]["drones"])
    assert "Mining drones" in client.get("/diagnostics", headers=H).text


def _live():
    import pathlib
    return json.loads((pathlib.Path(__file__).parent / "fixtures" / "live_2026-10-02.json").read_text())


def test_live_back_to_belt_plan():
    """From the 2026-10-02 snapshot: 0 of 12 drones mining, every controller 'exhausted' though both read belts have
    open sites at 100%. AEMEROTH's drones sat at used-up salvage on AEMEROTH-4-2; FALQUORYX's controller was paused
    with a stale 'exhausted' at its own belt."""
    from rsweb import salvage as sv
    from rsweb.snapshot import devices_of
    snap = _live()
    devices = devices_of(snap)
    belts = {c["path"].split("/")[2]: c["body"] for c in snap["calls"] if c["path"].startswith("/locations/")}
    open_sites = {b: sv.open_site_count(d) for b, d in belts.items()}
    assert open_sites == {"AEMEROTH-BELT-1": 2, "FALQUORYX-BELT-1": 4}
    ctrls = [d for d in devices if d.get("device_type") == "ami_mining_controller" and not any(t.startswith("fleet:") for t in d.get("tags") or [])]
    plans = {p["ctrl"]: p for p in sv.back_to_belt_plan(ctrls, devices, snap["app"]["managed_by"], open_sites,
                                                       {"AEMEROTH": ["AEMEROTH-BELT-1"], "FALQUORYX": ["FALQUORYX-BELT-1"]}, set())}
    aem = plans["84EE1EF1"]
    assert aem["belt"] == "AEMEROTH-BELT-1" and not aem["move_ctrl"] and aem["directive"] == "gather_evenly"
    assert set(aem["away"]) == {"1886BD05", "399FF11C", "607F9110", "EB6DF4A3"} and "AEMEROTH-4-2" in aem["why"]
    fal = plans["F32E05A7"]
    assert fal["belt"] == "FALQUORYX-BELT-1" and fal["away"] == [] and "stale" in fal["why"]
    assert "90DEC78F" not in plans          # ITHVALAI's belt wasn't read: no open sites known → nothing guessed
    steps = sv.back_to_belt_steps(aem)
    cmds = [(s["path"], (s["body"] or {}).get("command")) for s in steps]
    assert cmds[0] == ("/devices/84EE1EF1", "release") and ("/devices/84EE1EF1", "adopt") in cmds
    assert cmds[-2:] == [("/devices/84EE1EF1", "set_directive"), ("/devices/84EE1EF1", "launch")]
    assert sum(1 for p, c in cmds if c == "travel") == 4
    assert [s["body"]["command"] for s in sv.back_to_belt_steps(fal) if s["body"]] == ["set_directive", "launch"]
    # with ITHVALAI's belt open, its controller (at the entry point) flies there with its drones
    plans = {p["ctrl"]: p for p in sv.back_to_belt_plan(ctrls, devices, snap["app"]["managed_by"],
                                                       {**open_sites, "ITHVALAI-BELT-1": 3}, {"ITHVALAI": ["ITHVALAI-BELT-1"]}, set())}
    ith = plans["90DEC78F"]
    assert ith["belt"] == "ITHVALAI-BELT-1" and ith["move_ctrl"] and len(ith["away"]) == 4


def test_a_depleted_salvage_brings_recall_waiting_drones_back_to_the_belt():
    """Live 2026-10-22: 84EE1EF1 at AEMEROTH-BELT-1 on gather_salvage, state 'depleted:recalling', its drones
    recall_waiting at AEMEROTH-5-L4 and none mining."""
    from rsweb import salvage as sv
    c = {"device_code": "C1", "device_type": "ami_mining_controller", "location": "AEMEROTH-BELT-1", "status": "coordinating",
         "ami_directive": {"name": "gather_salvage", "config": {"location": "KELCERUM-5-13", "recall": True},
                           "_eval_state": "depleted:recalling"}}
    kids = [{"device_code": f"D{i}", "device_type": "mining_drone", "location": "AEMEROTH-5-L4", "status": "recall_waiting",
             "controller_device_code": "C1"} for i in range(2)]
    plans = sv.back_to_belt_plan([c], [c, *kids], {}, {"AEMEROTH-BELT-1": 1}, {"AEMEROTH": ["AEMEROTH-BELT-1"]}, set())
    assert len(plans) == 1 and plans[0]["away"] == ["D0", "D1"] and plans[0]["directive"] == "gather_evenly"
    kids[0]["status"] = "recalling"   # really on its way: left alone
    assert sv.back_to_belt_plan([c], [c, *kids], {}, {"AEMEROTH-BELT-1": 1}, {"AEMEROTH": ["AEMEROTH-BELT-1"]},
                                set())[0]["away"] == ["D1"]


def test_live_diagnosis_runs_clean():
    from rsweb.snapshot import diagnose
    snap = _live()
    snap["app"].update(jobs=[], log=[])
    d = diagnose(snap)
    assert "0 of 12 mining drones are mining" in d["headline"]
    ctrl = {c["code"]: c for c in d["controllers"]}
    assert any("drones are away" in n or "back to" in n for n in ctrl["84EE1EF1"]["notes"])
    assert any("until a mission puts it to work" in n for n in ctrl["D0011B15"]["notes"])   # fleet controllers wait for a mission
    assert not any("D0011B15" in h for h in d["headline"])


def test_live_back_to_belt_rule_and_schedule(client):
    from rsweb.snapshot import devices_of
    eng = client.app.state.worker.automations
    db = client.app.state.db
    snap = _live()
    client.portal.call(db.kv_set, "devices", devices_of(snap))
    for c in snap["calls"]:
        if c["path"].startswith("/locations/"):
            client.portal.call(db.kv_set, f"loc:{c['path'].split('/')[2]}", c["body"])
    client.portal.call(db.kv_set, "belt_reads", {"AEMEROTH-BELT-1": "2099-01-01T00:00:00+00:00",
                                                 "FALQUORYX-BELT-1": "2099-01-01T00:00:00+00:00"})

    async def enable():
        s = await eng.settings()
        s["rules"]["salvage_when_depleted"]["enabled"] = True
        await eng.save_settings(s)
    client.portal.call(enable)
    client.portal.call(eng.save_schedules, snap["app"]["schedules"])
    done = client.portal.call(eng.rule_back_to_belt, {"back_to_belt": True})
    assert sorted(d.split(" ")[0] for d in done) == ["84EE1EF1", "F32E05A7"]
    jobs = {j["device"]: j for j in client.portal.call(eng.jobs) if j["rule"] == "salvage_when_depleted"}
    assert "bringing 4 drone(s)" in jobs["84EE1EF1"]["title"] and "stale" in jobs["F32E05A7"]["title"]
    # second pass: cooling down / busy — nothing new
    assert client.portal.call(eng.rule_back_to_belt, {"back_to_belt": True}) == []
    # the schedule no longer writes off a stale 'exhausted' at a belt that has open sites
    sched = dict(snap["app"]["schedules"][0])
    devs = devices_of(snap)
    for d in devs:
        if d["device_code"] == "F32E05A7":
            d["ami_directive_status"] = "active"
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(db.kv_set, "automation_jobs", [])
    res = client.portal.call(eng.run_schedule, sched)
    by = {r.split(":")[0]: r for r in res}
    assert "exhausted at AEMEROTH-4-2" in by["84EE1EF1"] and "brings them back" in by["84EE1EF1"]
    assert "won't help" not in by["F32E05A7"]


def test_stowable_follows_live_device_data():
    from rsweb import fleets as fl
    snap = _live()
    from rsweb.snapshot import devices_of
    by_type = {}
    for d in devices_of(snap):
        by_type.setdefault(d["device_type"], d)
    for t in ("mining_drone", "survey_drone", "ami_mining_controller", "ami_transport_controller"):
        assert fl.stowable(by_type[t]), t
    for t in ("transport_drone", "transport_hauler", "cargo_freighter"):
        if t in by_type:
            assert not fl.stowable(by_type[t]), t


def _live_b():
    import pathlib
    return json.loads((pathlib.Path(__file__).parent / "fixtures" / "live_2026-10-02b.json").read_text())


def test_live_after_recovery_leaves_partly_exhausted_controller_alone():
    """Second snapshot (20:39Z): every drone mining; F32E05A7 reports exhausted for silicates/structural only."""
    from rsweb import salvage as sv
    from rsweb.snapshot import devices_of, diagnose
    snap = _live_b()
    devices = devices_of(snap)
    belts = {c["path"].split("/")[2]: c["body"] for c in snap["calls"] if c["path"].startswith("/locations/")}
    open_sites = {b: sv.open_site_count(d) for b, d in belts.items()}
    ctrls = [d for d in devices if d.get("device_type") == "ami_mining_controller"]
    assert sv.back_to_belt_plan(ctrls, devices, snap["app"]["managed_by"], open_sites,
                                {b.split("-")[0]: [b] for b in belts}, set()) == []
    snap["app"].update(jobs=[], log=[])
    d = diagnose(snap)
    assert "12 of 12 mining drones are mining" in d["headline"]
    assert not any("reporting exhausted" in h for h in d["headline"])
    f = next(c for c in d["controllers"] if c["code"] == "F32E05A7")
    assert any("partly exhausted" in n for n in f["notes"]) and not any("stale" in n for n in f["notes"])


def test_detach_already_released_counts_as_done(client):
    from rsweb.automations import step
    eng = client.app.state.worker.automations
    world = client.app.state.api.http._transport.app.state.world
    async def fake_send(method, path, body, why):
        return False, None, "Target device is not attached to this carrier"
    orig = eng.send
    eng.send = fake_send
    try:
        job = client.portal.call(eng.create_job, "loadouts", "detach test", "SP000001",
                                 [step("SP000001: detach X", "/devices/SP000001", {"command": "detach", "device": "X"}, critical=True),
                                  step("tag X", "/devices/X", {"configuration": {"add_tags": ["home:sol"]}}, method="PATCH")],
                                 {"devices": []}, True)
    finally:
        eng.send = orig
    j = next(x for x in client.portal.call(eng.jobs) if x["id"] == job["id"])
    assert j["steps"][0]["status"] == "done" and j["status"] != "failed"


def test_fleet_end_mission_and_board(client):
    eng = client.app.state.worker.automations
    fleet, devices = _fleet_world()
    fleet["mission"] = {"status": "running", "phase": "watch", "targets": ["AEM"], "belt": "AEM-BELT-1", "log": []}
    for d in devices:
        if d["device_code"] == "MC000001":
            d["ami_directive"] = {"name": "gather_evenly"}
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(eng.save_fleets, [fleet])
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "End mission &amp; board" in page and "Recall &amp; return home" in page
    r = client.post(f"/fleets/{fleet['id']}/control", data={"action": "end"}, headers=HX)
    assert r.headers.get("HX-Refresh")
    f = client.portal.call(eng.fleets)[0]
    m = f["mission"]
    assert m["phase"] == "recall" and m.get("end_started") and m["status"] in ("running", "stalled")
    job = next(j for j in client.portal.call(eng.jobs) if j["id"] == m["job"]) if m.get("job") else None
    assert job and job["title"].endswith("end mission & board")
    cmds = [(s["path"], (s["body"] or {}).get("command")) for s in job["steps"]]
    assert ("/devices/MC000001", "clear_directive") in cmds
    assert not any(c == "collect_resources" for _, c in cmds)          # ending early: no hauling
    assert any(c in ("attach", "stow") for _, c in cmds)                # everyone boards a carrier
    assert not any(p.endswith("MF000001") and c == "travel" for p, c in cmds)   # no trip home
    # once the recall job is over, the mission ends where it is
    jobs = client.portal.call(eng.jobs)
    for j in jobs:
        if j["id"] == m["job"]:
            j["status"] = "done"
    client.portal.call(eng.save_jobs, jobs)
    f = client.portal.call(eng.fleets)[0]
    f["mission"]["status"] = "running"
    client.portal.call(eng.save_fleets, [f])
    client.portal.call(eng.run_fleets)
    f = client.portal.call(eng.fleets)[0]
    assert f["mission"]["status"] == "ended" and "ended early" in f["mission"]["log"][-1]["text"]


def test_fleet_board_everyone_without_a_mission(client):
    eng = client.app.state.worker.automations
    fleet, devices = _fleet_world()
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(eng.save_fleets, [fleet])
    assert "Board everyone" in client.get("/fleets/details?all=1", headers=H).text
    client.post(f"/fleets/{fleet['id']}/control", data={"action": "end"}, headers=HX)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["phase"] == "recall" and m.get("end_started")


def test_surging_devices_missing_from_the_list_are_kept():
    """Live (2026-10-02T21:24Z): two cargo freighters surging between systems were absent from GET /devices."""
    from rsweb.ingest import merge_device_snapshot
    prev = [{"device_code": f"D{i}", "device_type": "mining_drone", "location": "AEM-BELT-1", "status": "mining"} for i in range(6)]
    prev.append({"device_code": "57C506F0", "device_type": "cargo_freighter", "location": "AEMEROTH-6-33", "status": "collecting",
                 "tags": ["home:aemeroth"], "controller_device_code": "0E158313"})
    new = [dict(d) for d in prev[:6]]
    out = merge_device_snapshot(prev, new, False, set(), now="2026-10-02T21:24:43+00:00")
    f = next(d for d in out if d["device_code"] == "57C506F0")
    assert f["unlisted"] and f["location_stale"] and f["location"] == "AEMEROTH-6-33" and f["tags"] == ["home:aemeroth"]
    assert f["unlisted_since"] == "2026-10-02T21:24:43+00:00"
    # still missing next sync: keeps its first-missed time; gone after 12 h
    out2 = merge_device_snapshot(out, new, False, set(), now="2026-10-02T23:00:00+00:00")
    assert next(d for d in out2 if d["device_code"] == "57C506F0")["unlisted_since"] == "2026-10-02T21:24:43+00:00"
    assert not any(d["device_code"] == "57C506F0" for d in merge_device_snapshot(out, new, False, set(), now="2026-10-03T10:00:00+00:00"))
    # back in the list: flags cleared
    back = new + [{**prev[6], "location": "ITHVALAI-2-L4", "status": "idle"}]
    f = next(d for d in merge_device_snapshot(out, back, False, set()) if d["device_code"] == "57C506F0")
    assert "unlisted" not in f and "location_stale" not in f and f["location"] == "ITHVALAI-2-L4"
    # decommissioned (an event says so): dropped at once
    assert not any(d["device_code"] == "57C506F0" for d in merge_device_snapshot(out, new, False, {"57C506F0"}))


def test_unlisted_freighter_still_counts_in_loadouts_and_fleets():
    from rsweb import fleets as fl
    from rsweb import loadouts as lo
    devices = [
        {"device_code": "TC1", "device_type": "ami_transport_controller", "location": "AEM-BELT-1", "status": "coordinating",
         "ami_directive": {"name": "ferry"}, "tags": ["home:aem", "ferry"]},
        {"device_code": "CF1", "device_type": "cargo_freighter", "location": "AEM-6-33", "status": "surging", "unlisted": True,
         "location_stale": True, "controller_device_code": "TC1", "tags": ["home:aem", "fleet:haul"], "features": ["surge", "transport"]},
    ]
    cfg = {"phases": [{"id": "p1", "name": "Outpost", "wants": {"cargo_freighter": 1}}], "systems": {"AEM": "p1"}}
    p = lo.plan(cfg, [devices[0], {**devices[1], "tags": ["home:aem"]}], [], {}, {"AEM": {}}, {}, set(), [], {})
    row = next(r for r in _rep(p, "AEM")["rows"] if r["type"] == "cargo_freighter")
    assert row["have"] == 1 and row["short"] == 0 and p["prints"] == []      # counted, not re-printed
    r = fl.roster({"id": "haul", "wants": {"cargo_freighter": 1}}, devices)
    assert [d["device_code"] for d in r["members"]] == ["CF1"] and r["unlisted"] == {"CF1"}


def test_locations_list_hides_sites_only_seen_in_old_events(client):
    from rsweb.targets import system_targets, system_resources
    db = client.app.state.db
    client.portal.call(db.kv_set, "loc:SOL-BELT-1", {"location_type": "belt", "resource_sites": [
        {"designation": "SOL-BELT-1-SITE-7", "resources_remaining_pct": {"carbon": 40, "structural": 0}},
        {"designation": "SOL-BELT-1-SITE-8", "resources_remaining_pct": {"carbon": 0, "structural": 0}}]})
    # an old digest names a site that has since closed
    client.portal.call(client.app.state.worker.handle_event,
                       _ev(901, "ami.mining.digest", location="SOL-BELT-1", report={"site": "SOL-BELT-1-SITE-3"}))
    t = client.portal.call(system_targets, db, "SOL")
    codes = {x["code"] for x in t["targets"]}
    assert "SOL-BELT-1-SITE-7" in codes
    assert "SOL-BELT-1-SITE-3" not in codes and "SOL-BELT-1-SITE-8" not in codes   # closed / all 0 %
    res = client.portal.call(system_resources, db, "SOL")
    s7 = next(x for x in res["sites_shown"] if x["code"] == "SOL-BELT-1-SITE-7")
    assert s7["remaining_pct"] == {"carbon": 40.0, "structural": 0.0}
    assert "carbon 40%" in client.get("/systems/SOL", headers=H).text      # the open site's % left, per resource


def test_live_mined_out_belt_with_searches_running():
    """21:40Z: FALQUORYX-BELT-1 lists no sites (SITE-79 'fully depleted'); 4 survey drones are ~90% through a search."""
    import pathlib
    from rsweb.snapshot import diagnose
    snap = json.loads((pathlib.Path(__file__).parent / "fixtures" / "live_2026-10-02c.json").read_text())
    snap["app"].update(jobs=[], log=[])
    d = diagnose(snap)
    assert "8 of 12 mining drones are mining" in d["headline"]
    h = next(x for x in d["headline"] if x.startswith("belts with no open sites"))
    assert "FALQUORYX-BELT-1 (searching, new sites due 2026-10-02T17:46:10-04:00)" in h
    r = next(x for x in d["drones"] if x["code"] == "01BA3403")
    assert any("4 survey drone(s) searching FALQUORYX-BELT-1, 89.2%+ done" in f for f in r["fix"])
    c = next(x for x in d["controllers"] if x["code"] == "F32E05A7")
    assert not any("survey drones must search" in n for n in c["notes"])


def _belt(*sites):
    return {"location_type": "belt", "resource_sites": [
        {"designation": f"FAL-BELT-1-SITE-{i}", "site_index": i, "resources_remaining_pct": {"carbon": pc}} for i, pc in sites]}


def test_viability_observe_and_assess():
    from rsweb import viability as via
    st: dict = {}
    miners = [{"device_code": f"MD{i}", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "mining (carbon)"} for i in range(4)]
    survey = [{"device_code": f"SD{i}", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "searching",
               "scan": {"target": "FAL-BELT-1", "started_at": f"2026-10-02T20:3{i}:00+00:00",
                        "completes_at": f"2026-10-02T21:4{i}:00+00:00"}} for i in range(4)]
    # 20:05 four fresh sites; searches seen at 20:40; sites gone by 21:25
    via.observe(st, miners, {"FAL-BELT-1": _belt((76, 100), (77, 100), (78, 100), (79, 100))}, "2026-10-02T20:05:00+00:00")
    via.observe(st, miners + survey, {}, "2026-10-02T20:40:00+00:00")
    via.observe(st, miners + survey, {"FAL-BELT-1": _belt((79, 10))}, "2026-10-02T21:05:00+00:00")
    _, notes = via.observe(st, miners + survey, {"FAL-BELT-1": _belt()}, "2026-10-02T21:25:00+00:00")
    assert any("SITE-79 closed after ~80 min" in n for n in notes)
    rec = st["belts"]["FAL-BELT-1"]
    assert len(rec["searches"]) == 4 and rec["searches"][0]["minutes"] == 70.0 and rec["max_index"] == 79
    a = via.assess("FAL-BELT-1", rec)
    assert a["search_min"] == 70.0 and a["site_life_min"] == 60.0      # 76–78 closed at 21:05 (60 min), 79 at 21:25 (80)
    assert a["ratio"] == 1.17 and a["verdict"] == "watch" and a["miners"] == 4 and a["survey_needed"] == 9
    assert a["survey"] == 4 and a["survey_short"] == 5
    # the same search seen twice isn't counted twice
    via.observe(st, survey, {}, "2026-10-02T21:30:00+00:00")
    assert len(st["belts"]["FAL-BELT-1"]["searches"]) == 4


def test_viability_report_suggests_a_cheaper_belt():
    from rsweb import viability as via
    def rec(search, life, idx, miners=4):
        return {"searches": [{"minutes": search}] * 3,
                "sites": {f"S{i}": {"first": "2026-10-02T20:00:00+00:00", "closed": f"2026-10-02T20:{life:02d}:00+00:00"} for i in range(3)},
                "max_index": idx, "miners": miners, "mining": 0, "survey": 4}
    st = {"belts": {"FAL-BELT-1": rec(59, 20, 120), "ITH-BELT-1": rec(5, 40, 3)}}
    stars = {"FAL": {"position": {"x": 0, "y": 0, "z": 0}}, "ITH": {"position": {"x": 3, "y": 4, "z": 0}},
             "AEM": {"position": {"x": 1, "y": 0, "z": 0}}}
    rows = via.report(st, stars, 2.5, {"AEM-BELT-1": 2})
    fal = rows[0]
    assert fal["belt"] == "FAL-BELT-1" and fal["verdict"] == "consider moving" and fal["ratio"] == 2.95
    assert fal["move_to"]["belt"] == "AEM-BELT-1" and fal["move_to"]["ly"] == 1.0     # nearest cheaper: untracked, site #2
    text = via.alert_text(fal)
    assert "2.95× a site's life" in text and "AEM-BELT-1 (1.0 ly)" in text and "~16 survey drones" in text
    assert next(r for r in rows if r["belt"] == "ITH-BELT-1")["verdict"] == "ok"


def test_viability_engine_alerts_once_and_pages_render(client):
    from rsweb import viability as via
    eng = client.app.state.worker.automations
    db = client.app.state.db
    st = {"belts": {"SOL-BELT-1": {"searches": [{"minutes": 90}] * 3, "max_index": 80, "miners": 2, "mining": 0, "survey": 2,
                                   "sites": {f"S{i}": {"first": "2026-10-02T20:00:00+00:00", "closed": "2026-10-02T20:30:00+00:00"}
                                             for i in range(3)}}}}
    client.portal.call(db.kv_set, "viability", st)

    async def enable():
        s = await eng.settings()
        s["rules"]["belt_viability"]["enabled"] = True
        await eng.save_settings(s)
    client.portal.call(enable)
    alerts = client.portal.call(eng.track_viability)
    assert len(alerts) == 1 and "SOL-BELT-1: searches take 3.0× a site's life" in alerts[0]
    assert client.portal.call(eng.track_viability) == []            # once per crossing
    page = client.get("/systems/SOL", headers=H).text
    assert "site life" in page and "consider moving" in page
    assert "Belt viability alerts" in client.get("/automations", headers=H).text


def test_loadout_reduction_releases_spares_from_their_controller():
    from rsweb import loadouts as lo
    devs = [{"device_code": "MC1", "device_type": "ami_mining_controller", "location": "FAL-BELT-1", "status": "coordinating",
             "tags": ["home:fal"], "features": ["ami"]}]
    devs += [{"device_code": f"MD{i}", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "mining (carbon)",
              "controller_device_code": "MC1", "tags": ["home:fal"]} for i in range(4)]
    cfg = {"phases": [{"id": "p", "name": "P", "wants": {"mining_drone": 2}}], "systems": {"FAL": "p"}}
    p = lo.plan(cfg, devs, [], {}, {"FAL": {}}, {}, set(), [], {})
    assert len(p["made_spare"]) == 2
    for code in p["made_spare"]:
        assert lo.SPARE in p["tag_add"][code] and "home:fal" in p["tag_remove"][code]
    assert sorted(p["releases"]["MC1"]) == p["made_spare"]
    assert any("no longer in the loadout" in l for l in lo.describe(p))
    # audit before the pass: nothing wrong yet (tags not applied); after tags applied but before release: fixed by the pass
    for d in devs:
        if d["device_code"] in p["made_spare"]:
            d["tags"] = ["spare"]
    p2 = lo.plan(cfg, devs, [], {}, {"FAL": {}}, {}, set(), [], {})
    assert sorted(p2["releases"]["MC1"]) == p["made_spare"]            # still wanted gone: released, spare kept
    assert not any(lo.SPARE in (p2["tag_remove"].get(c) or []) for c in p["made_spare"])
    issues = lo.audit(cfg, devs, {"FAL": {}}, p2)
    spare_issues = [a for a in issues if "spare but still run by" in a["issue"]]
    assert len(spare_issues) == 2 and all(a["fixed"] and a["how"] == "released by its controller" for a in spare_issues)
    # released: no longer flip-flops, nothing left to fix
    for d in devs:
        if d["device_code"] in p["made_spare"]:
            d.pop("controller_device_code")
    p3 = lo.plan(cfg, devs, [], {}, {"FAL": {}}, {}, set(), [], {})
    assert not p3["releases"] and not any(lo.SPARE in (p3["tag_remove"].get(c) or []) for c in p["made_spare"])
    assert [a for a in lo.audit(cfg, devs, {"FAL": {}}, p3) if not a["fixed"]] == []


def test_audit_flags_mismatches():
    from rsweb import loadouts as lo
    devs = [
        {"device_code": "MC1", "device_type": "ami_mining_controller", "location": "AEM-BELT-1", "status": "coordinating",
         "tags": ["fleet:aem"]},
        {"device_code": "MD1", "device_type": "mining_drone", "location": "AEM-BELT-1", "status": "mining", "controller_device_code": "MC1",
         "tags": ["fleet:fal"]},                                             # run by another fleet's controller
        {"device_code": "MD2", "device_type": "mining_drone", "location": "FAL-1-L4", "status": "idle", "tags": ["fleet:fal", "fleet:aem"]},
        {"device_code": "MD3", "device_type": "mining_drone", "location": "AEM-2-L4", "status": "idle", "tags": ["to:aem"]},   # arrived
        {"device_code": "MD4", "device_type": "mining_drone", "location": "AEM-2-L4", "status": "idle", "tags": ["home:aem"]},  # old tag
        {"device_code": "MD5", "device_type": "mining_drone", "location": "AEM-2-L4", "status": "idle", "tags": ["fleet:aem", "spare"]},
        {"device_code": "MD6", "device_type": "mining_drone", "location": "AEM-2-L4", "status": "idle", "tags": ["fleet:ghost"]},
        {"device_code": "FL1", "device_type": "mining_drone", "location": "AEM-2-L4", "status": "idle", "tags": ["fleet:x", "spare"]},
    ]
    fleets = [{"id": "aem", "name": "Aem", "home": "AEM", "station": True, "wants": {"mining_drone": 3}},
              {"id": "fal", "name": "Fal", "home": "FAL", "station": True, "wants": {}},
              {"id": "x", "name": "X", "home": "AEM", "station": False, "wants": {}, "mission": {"status": "running"}}]
    cfg = {"phases": [], "fleets": fleets, "fleets_migrated": True}
    p = lo.plan(cfg, devs, [], {}, {"AEM": {}, "FAL": {}}, {}, set(), [], {})
    by = {}
    for a in lo.audit(cfg, devs, {"AEM": {}, "FAL": {}}, p, {"MD3"}):
        by.setdefault(a["code"], []).append(a)
    assert any("a controller of fleet:aem, but not in that fleet" in a["issue"] for a in by["MD1"])
    assert any("2 fleet tags" in a["issue"] for a in by["MD2"])
    assert any("still tagged to:aem" in a["issue"] and a["fixed"] for a in by["MD3"])
    assert any("old home:aem tag" in a["issue"] and a["fixed"] for a in by["MD4"])
    assert any("tagged spare and fleet:aem" in a["issue"] and a["fixed"] for a in by["MD5"])   # needed: spare removed
    assert any("fleet:ghost: no such fleet" in a["issue"] for a in by["MD6"])
    assert "FL1" not in by                                                      # a fleet on a mission: the mission's business


def test_loadouts_page_shows_the_check(client):
    landing = client.get("/fleets", headers=H).text   # landing: status (loaded after) and templates; the slow parts on details
    assert 'hx-get="/fleets/overview"' in landing and 'id="templates"' in landing and "Tags &amp;" not in landing
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Tags &amp; controllers check" in page


def _live_c_inventory():
    import pathlib
    s = json.loads((pathlib.Path(__file__).parent / "fixtures" / "live_2026-10-02c.json").read_text())
    return s


def test_consolidate_falquoryx_leftovers():
    """Live 21:40Z: autofactory at FALQUORYX-BELT-1 waiting for resources; 1,235 units left at FALQUORYX-5 by a contract delivery."""
    from rsweb import consolidate as co
    from rsweb.snapshot import devices_of
    snap = _live_c_inventory()
    devices = [d for d in devices_of(snap) if d["device_code"].startswith(("3E95", "DF45", "41B6", "C582", "374C", "1179"))]
    assert {d["device_code"] for d in devices} >= {"3E95BD59", "DF451241"}
    inv = {"FALQUORYX-BELT-1": [{"quantity": 1665, "resource_type": "carbon"}, {"quantity": 36, "resource_type": "volatiles"}, {"quantity": 6284, "resource_type": "structural"},
                                {"quantity": 68, "resource_type": "rares"}],
           "FALQUORYX-5": [{"quantity": 127, "resource_type": "conductive"}, {"quantity": 58, "resource_type": "rares"},
                           {"quantity": 224, "resource_type": "silicates"}, {"quantity": 615, "resource_type": "structural"},
                           {"quantity": 211, "resource_type": "volatiles"}],
           "FALQUORYX-2": [{"quantity": 40, "resource_type": "carbon"}]}                         # below the minimum
    fac = next(d for d in devices if d["device_code"] == "3E95BD59")
    fac["printing"] = {"device_type": "survey_drone"}
    bps = {"survey_drone": {"device_type": "survey_drone", "resources": {"volatiles": 100, "rares": 80, "structural": 50}}}
    plans = co.plan(devices, inv, bps, set())
    assert len(plans) == 1
    p = plans[0]
    assert p["controller"] == "DF451241" and p["collect"] == "FALQUORYX-5" and p["deliver"] == "FALQUORYX-BELT-1"
    # "deliver until": the home pile plus what's picked up (rares 68, structural 6284, volatiles 36 there already)
    assert p["requirement"] == {"conductive": 127, "rares": 126, "silicates": 224, "structural": 6899, "volatiles": 247}
    assert p["helps"] == {"volatiles": 64, "rares": 12}
    body = co.steps(p)[0]["body"]
    assert body == {"command": "set_directive", "directive": "delivery",
                    "configuration": {"route": {"collect": "FALQUORYX-5", "deliver": "FALQUORYX-BELT-1"}, "requirement": p["requirement"]}}
    assert "has what the autofactory is waiting for: 12 rares, 64 volatiles" in co.describe(p)
    # a contract staged there: left alone; controller busy / no drones: reported, not sent
    assert co.plan(devices, inv, bps, set(), {"FALQUORYX-5"}) == []
    waiting = co.plan(devices, inv, bps, {"DF451241"})
    assert waiting[0]["controller"] is None and "no free in-system transport controller" in co.describe(waiting[0])


def test_consolidate_rule_creates_job(client):
    eng = client.app.state.worker.automations
    db = client.app.state.db
    devices = [
        {"device_code": "AF1", "device_type": "autofactory", "location": "SOL-BELT-1", "status": "waiting_for_resources",
         "available_commands": ["enqueue_print"], "tags": []},
        {"device_code": "TC1", "device_type": "ami_transport_controller", "location": "SOL-BELT-1", "status": "idle",
         "ami_directive": {"name": "delivery", "_eval_state": "completed:delivered"}, "ami_directive_status": "completed", "tags": []},
        {"device_code": "TD1", "device_type": "transport_drone", "location": "SOL-BELT-1", "status": "idle", "controller_device_code": "TC1"},
        {"device_code": "TC2", "device_type": "ami_transport_controller", "location": "SOL-3-L4", "status": "coordinating",
         "ami_directive": {"name": "ferry"}, "tags": ["ferry"]},
    ]
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-BELT-1", "items": {"carbon": 500}},
                                                {"location": "SOL-3", "items": {"structural": 300, "silicates": 50}}])
    done = client.portal.call(eng.rule_consolidate, True)
    assert len(done) == 1 and done[0].startswith("TC1 hauls SOL-3 (350 units) → autofactory at SOL-BELT-1")
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "consolidate"][-1]
    assert job["device"] == "TC1"
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Consolidation at the autofactory" in page


def test_loadouts_hold_miners_for_mined_out_systems_and_working_spares():
    from rsweb import loadouts as lo
    cfg = {"phases": [{"id": "big", "name": "Mine", "wants": {"mining_drone": 4}}, {"id": "small", "name": "S", "wants": {"mining_drone": 1}}],
           "systems": {"AEM": "big", "FAL": "small"}}
    devs = [{"device_code": f"F{i}", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "mining (carbon)",
             "tags": ["home:fal"]} for i in range(3)]
    stars = {"AEM": {}, "FAL": {}}
    # AEM's belts have no open sites: nothing is sent or printed for it
    p = lo.plan(cfg, devs, [], {}, stars, {}, set(), [], {}, None, {"AEM": 0, "FAL": 5})
    assert not p["moves"] and not p["prints"]
    assert any("no open mining sites" in u["why"] for u in p["unmet"] if u["star"] == "AEM")
    # AEM has sites, FAL's spares are still mining: they wait (no "Cannot cruise while mining"), and nothing is printed instead
    for d in devs[1:]:
        d["tags"] = ["spare"]
    p = lo.plan(cfg, devs, [], {}, stars, {}, set(), [], {}, None, {"AEM": 3, "FAL": 5})
    assert not p["moves"]
    assert any("still working — sent once idle" in u["why"] for u in p["unmet"])
    assert sum(pr["n"] for pr in p["prints"] if pr["device_type"] == "mining_drone") <= 2   # only the 2 not covered by spares
    # once idle they go
    for d in devs[1:]:
        d["status"] = "idle"
    p = lo.plan(cfg, devs, [], {}, stars, {}, set(), [], {}, None, {"AEM": 3, "FAL": 5})
    assert sorted(c for c, dest in p["moves"].items() if dest == "AEM") == ["F1", "F2"]


def test_audit_flags_non_members_run_by_a_fleet_controller():
    from rsweb import loadouts as lo
    devs = [{"device_code": "C1", "device_type": "ami_transport_controller", "location": "FAL-BELT-1", "tags": ["fleet:prospectors"]},
            {"device_code": "H1", "device_type": "transport_hauler", "location": "FAL-BELT-1", "status": "idle",
             "controller_device_code": "C1", "tags": ["home:fal"]}]
    cfg = {"phases": [], "systems": {}}
    p = lo.plan(cfg, devs, [], {}, {}, {}, set(), [], {})
    issues = lo.audit(cfg, devs, {}, p)
    assert any(a["code"] == "H1" and "controller of fleet:prospectors, but not in that fleet" in a["issue"] and not a["fixed"] for a in issues)


def test_loadouts_fill_surveyors_before_miners():
    from rsweb import loadouts as lo
    cfg = {"phases": [{"id": "m", "name": "Mine", "wants": {"mining_drone": 8, "survey_drone": 10}}],
           "systems": {"AEM": "m", "ITH": "m"}, "settings": {"need_stock": False}}
    fac = {"device_code": "AF", "device_type": "autofactory", "location": "FAL-BELT-1", "status": "idle",
           "available_commands": ["enqueue_print"], "print_queue": [{"device_type": "x"}] * 7}   # 3 slots free
    bps = [{"device_type": "mining_drone", "resources": {"structural": 1}, "queue_size": 10},
           {"device_type": "survey_drone", "resources": {"structural": 1}}, {"device_type": "autofactory", "queue_size": 10}]
    p = lo.plan(cfg, [fac], bps, {"FAL-BELT-1": {"structural": 1000}}, {"AEM": {}, "ITH": {}, "FAL": {}}, {}, set(), [], {})
    assert [pr["device_type"] for pr in p["prints"]] == ["survey_drone"] * len(p["prints"])
    assert sum(pr["n"] for pr in p["prints"]) == 3
    assert lo.fill_rank("ami_mining_controller") < lo.fill_rank("survey_drone") < lo.fill_rank("mining_drone") < lo.fill_rank("transport_drone")


def test_print_with_a_destination(client):
    world = client.app.state.api.http._transport.app.state.world
    world.queues["AF00BEEF"] = []
    db = client.app.state.db
    here = next(d for d in world.devices if d["device_code"] == "AF00BEEF")["location"].split("-")[0]
    # the location list for a system
    opts = client.get(f"/print-queue/locations?dest_star={here}", headers=HX).text
    assert f"anywhere in {here}" in opts and f'value="{here}-BELT-1"' in opts
    # same system + a location: the game's oncomplete sends it, and it's pinned
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "add", "device_type": "survey_drone", "quantity": "1",
                                                          "dest_star": here, "dest_loc": f"{here}-3"}, headers=HX)
    assert "result ok" in r.text
    body = json.loads(client.portal.call(db.fetchone, "SELECT body FROM actions ORDER BY id DESC LIMIT 1")["body"])
    assert body["oncomplete"] == {"command": "travel", "destination": f"{here}-3"} and body["tags"] == [f"at:{here.lower()}-3"]
    # another system + a typed location: to: and at: tags, an order counted for that system, no oncomplete
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "add", "device_type": "mining_drone", "quantity": "2",
                                                          "dest_star": "ITHVALAI", "dest_text": "ithvalai-belt-1"}, headers=HX)
    body = json.loads(client.portal.call(db.fetchone, "SELECT body FROM actions ORDER BY id DESC LIMIT 1")["body"])
    assert body["tags"] == ["to:ithvalai", "at:ithvalai-belt-1"] and "oncomplete" not in body
    orders = [o for o in client.portal.call(db.kv_get, "loadout_orders", []) if o.get("manual")]
    assert len(orders) == 2 and orders[0]["star"] == "ITHVALAI" and orders[0]["location"] == "ITHVALAI-BELT-1"
    assert all(x.get("tags") for x in world.queues["AF00BEEF"])
    # a location outside the chosen system is refused
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "add", "device_type": "mining_drone", "quantity": "1",
                                                          "dest_star": "ITHVALAI", "dest_text": "AEMEROTH-3"}, headers=HX)
    assert "AEMEROTH-3 isn&#39;t in ITHVALAI" in r.text


def test_pinned_devices_reach_their_spot():
    from rsweb import loadouts as lo
    from rsweb.ami_schedule import handoffs
    devs = [
        # delivered to ITHVALAI (still tagged to:), pinned to its belt
        {"device_code": "MD1", "device_type": "mining_drone", "location": "ITHVALAI-2-L4", "status": "idle",
         "tags": ["to:ithvalai", "at:ithvalai-belt-1"]},
        # already home, pinned to planet 3, but sitting at the entry point
        {"device_code": "SD1", "device_type": "survey_drone", "location": "ITHVALAI-2-L4", "status": "idle",
         "tags": ["home:ithvalai", "at:ithvalai-3"]},
        {"device_code": "SC1", "device_type": "ami_survey_controller", "location": "ITHVALAI-BELT-1", "status": "idle",
         "features": ["ami"], "available_commands": ["adopt"], "tags": ["home:ithvalai"]},
    ]
    cfg = {"phases": [], "systems": {}}
    p = lo.plan(cfg, devs, [], {}, {"ITHVALAI": {}}, {}, set(), [], {})
    assert "MD1" in p["arrived"] and ("SD1", "ITHVALAI-3") in p["pins"]
    steps = lo.arrived_steps("MD1", devs[0], {})
    assert steps[-1]["body"] == {"command": "travel", "destination": "ITHVALAI-BELT-1"}
    assert any("goes to ITHVALAI-3 (pinned there)" in l for l in lo.describe(p))
    # the survey controller at the belt doesn't pull the pinned drone away
    assert handoffs(devs, {}, set()) == []
    # moving to another system drops a pin for the old one
    rh = lo.rehome_step("SD1", devs[1], "AEMEROTH")
    assert "at:ithvalai-3" in rh["body"]["configuration"]["remove_tags"]


def test_relaying_relay_goes_to_its_pin():
    # live 2026-10-29: B2B8B2EA, sent to the OTHILETH-1-EVT-007 contract, relayed at OTHILETH-1-L4 and never moved
    from rsweb import loadouts as lo
    relay = {"device_code": "B2B8B2EA", "device_type": "ftl_relay", "location": "OTHILETH-1-L4", "status": "relaying",
             "in_control_range": True, "available_commands": ["deactivate", "travel"],
             "tags": ["at:othileth-1", "contract:othileth-1-evt-007"]}
    other = {"device_code": "4A8A3109", "device_type": "ftl_relay", "location": "OTHILETH-1-L4", "status": "relaying",
             "tags": []}
    cfg = {"phases": [], "systems": {}}
    p = lo.plan(cfg, [relay, other], [], {}, {"OTHILETH": {}}, {}, set(), [], {})
    assert ("B2B8B2EA", "OTHILETH-1") in p["pins"] and p["pins_off"] == ["B2B8B2EA"] and lo.relay_off_to_pin(relay, [relay, other])
    # the system's only relay stays on where it is
    p = lo.plan(cfg, [relay], [], {}, {"OTHILETH": {}}, {}, set(), [], {})
    assert not p["pins"] and not lo.relay_off_to_pin(relay, [relay])


def test_loadout_pass_switches_a_relaying_relay_off_before_its_pin(client):
    # live 2026-10-29 (1.308.1): the pass itself crashed with NameError 'devices' on this path
    eng = client.app.state.worker.automations
    relay = {"device_code": "B2B8B2EA", "device_type": "ftl_relay", "location": "OTHILETH-1-L4", "status": "relaying",
             "in_control_range": True, "available_commands": ["deactivate", "travel"], "tags": ["at:othileth-1"]}
    other = {"device_code": "4A8A3109", "device_type": "ftl_relay", "location": "OTHILETH-1-L4", "status": "relaying",
             "tags": []}
    client.portal.call(client.app.state.db.kv_set, "devices", [relay, other])
    client.portal.call(eng.apply_loadouts, None, True)
    job = next(j for j in client.portal.call(eng.jobs) if j["title"] == "loadouts: B2B8B2EA → OTHILETH-1 (pinned)")
    assert job["steps"][0]["body"] == {"command": "deactivate"}


def test_large_devices_learned_from_a_refused_attach_only_stow(client):
    # live 2026-10-29: a rescue sent a carrier to attach vector charge 42B4755C, twice: "Cannot attach a large device"
    from rsweb import fleets as fl
    eng = client.app.state.worker.automations
    charge = {"device_code": "42B4755C", "device_type": "vector_charge", "location": "ALKALAROPS-KUIPER",
              "features": [], "available_commands": ["travel"]}
    client.portal.call(client.app.state.db.kv_set, "devices", [charge])
    assert fl.take_seat({"hold": 1, "attach": 2}, charge) == "stow"   # stows first (owner 2026-10-29)
    fl.NO_ATTACH_TYPES.discard("vector_charge")
    try:
        client.portal.call(eng.learn_no_attach, "42B4755C")
        assert fl.take_seat({"hold": 0, "attach": 2}, charge) is None
        assert fl.take_seat({"hold": 1, "attach": 2}, charge) == "stow"
        assert "vector_charge" in client.portal.call(client.app.state.db.kv_get, "no_attach_types", {})
        platform = {"device_code": "P1", "device_type": "surge_platform", "attach_capacity": 2}
        assert fl.cargo_carrier(platform, [platform, charge], False, fl.attachable(charge)) is None
    finally:
        fl.NO_ATTACH_TYPES.discard("vector_charge")


def test_blueprint_stow_feature_makes_a_type_stowable():
    from rsweb import fleets as fl
    d = {"device_code": "X1", "device_type": "gravity_lens", "features": ["focus"], "available_commands": ["travel"]}
    assert not fl.stowable(d)
    try:
        fl.learn_stowable([{"device_type": "gravity_lens", "features": ["focus", "stow"]}])
        assert fl.stowable(d)
    finally:
        fl.STOWABLE_TYPES.discard("gravity_lens")


def test_a_grounded_pinned_device_is_carried_to_its_spot():
    """Live 2026-10-21: 3 compute cores for the TWAFFY-OORT locker were dropped at TWAFFY's entry point (TWAFFY-11-L4)
    and could not fly to their pin on their own."""
    from rsweb import loadouts as lo
    core = lambda c, loc, tags: {"device_code": c, "device_type": "compute_core", "location": loc, "status": "idle",  # noqa: E731
                                 "features": ["stow"], "available_commands": ["deploy", "stow"], "tags": tags}
    cv = {"device_code": "CV1", "device_type": "cargo_vessel", "location": "TWAFFY-11-L4", "status": "idle",
          "features": ["surge", "transport"], "available_commands": ["travel", "stow"], "stow_capacity": 9, "tags": []}
    devs = [core("C1", "TWAFFY-11-L4", ["at:twaffy-oort"]), core("C2", "TWAFFY-11-L4", ["at:twaffy-oort"]), cv]
    stars = {"TWAFFY": {"entry_point": "TWAFFY-11-L4"}}
    p = lo.plan({"phases": [], "systems": {}}, devs, [], {}, stars, {}, set(), [], {})
    dl = p["deliveries"]
    assert len(dl) == 1 and sorted(dl[0]["devices"]) == ["C1", "C2"] and dl[0]["to"] == "TWAFFY"
    bodies = [s["body"] for s in lo.delivery_steps(dl[0], p["by_code"], stars, False)]
    i = bodies.index({"command": "travel", "destination": "TWAFFY-OORT"})
    assert {"command": "deploy"} in bodies[i:] and {"command": "travel", "destination": "TWAFFY-11-L4"} not in bodies[:i]
    # arriving on its own somewhere else in the system, a grounded device doesn't try to fly to its pin
    assert not any((s["body"] or {}).get("command") == "travel" for s in lo.arrived_steps("C1", devs[0], {}))
    # from another system, a pinned device is dropped at its pin, not at the entry point
    devs2 = [core("C3", "FAL-BELT-1", ["to:twaffy", "at:twaffy-oort"]), {**cv, "location": "FAL-1-L4"}]
    stars2 = {**stars, "FAL": {"entry_point": "FAL-1-L4"}}
    p2 = lo.plan({"phases": [], "systems": {}}, devs2, [], {}, stars2, {}, set(), [], {})
    bodies2 = [s["body"] for s in lo.delivery_steps(p2["deliveries"][0], p2["by_code"], stars2, False)]
    assert {"command": "travel", "destination": "TWAFFY-OORT"} in bodies2
    assert {"command": "travel", "destination": "TWAFFY-11-L4"} not in bodies2


def test_a_trip_refused_for_distance_is_lifted_by_a_carrier(client):
    """Live 2026-10-30: survey controller 27925A12 at ISOLITHOR-1, sent to ISOLITHOR-BELT-1 (≈639 AU, over the 30 AU
    cruise limit), was refused every pass and nothing fetched it. The refusal tags it `at:` + `lift`; the loadouts pass
    then sends a carrier, which drops it there and takes both tags off."""
    from rsweb import loadouts as lo
    eng = client.app.state.worker.automations
    ctrl = {"device_code": "SC1", "device_type": "ami_survey_controller", "location": "SOL-OORT", "status": "idle",
            "features": ["ami", "cruise", "stow"], "available_commands": ["travel", "stow", "deploy"], "tags": ["fleet:x"]}

    async def devices():
        return [ctrl]
    sent = []

    async def send(method, path, body, label):
        sent.append((method, path, body))
        return True, {}, None
    eng.devices, eng.send = devices, send
    st = {"path": "/devices/SC1", "body": {"command": "travel", "destination": "SOL-BELT-1"}}
    note = client.portal.call(eng._lift, st)
    assert sent == [("PATCH", "/devices/SC1", {"configuration": {"add_tags": ["at:sol-belt-1", "lift"]}})] and "carrier" in note
    ctrl["tags"] += ["at:sol-belt-1", "lift"]
    assert "already" in client.portal.call(eng._lift, st) and len(sent) == 1
    cv = {"device_code": "CV1", "device_type": "cargo_vessel", "location": "SOL-3-L4", "status": "idle",
          "features": ["surge", "transport"], "available_commands": ["travel", "stow"], "stow_capacity": 9, "tags": []}
    stars = {"SOL": {"entry_point": "SOL-3-L4"}}
    cfg = {"phases": [], "systems": {}, "fleets": [{"id": "x", "name": "X", "role": "explore", "home": "SOL", "station": True,
                                                    "wants": {}}]}
    p = lo.plan(cfg, [ctrl, cv], [], {}, stars, {}, set(), [], {})
    dl = p["deliveries"]
    assert len(dl) == 1 and dl[0]["devices"] == ["SC1"] and not p.get("pins")
    bodies = [s["body"] for s in lo.delivery_steps(dl[0], p["by_code"], stars, False)]
    assert {"command": "travel", "destination": "SOL-3-L4"} not in bodies   # the carrier picks it up, it doesn't fly over
    assert {"command": "travel", "destination": "SOL-OORT"} in bodies and {"command": "travel", "destination": "SOL-BELT-1"} in bodies
    removed = [t for b in bodies if b and "configuration" in b for t in b["configuration"].get("remove_tags") or []]
    assert "lift" in removed and "at:sol-belt-1" in removed


def test_a_working_beacon_pinned_to_a_civ_body_is_carried_there():
    """Live 2026-10-23: beacons left at relays' L4 points in a dozen civilization systems with no vessel there; the civ
    beacon pass pins them (`at:<body>`) and a carrier from elsewhere picks one up and drops it at the body."""
    from rsweb import loadouts as lo
    b = {"device_code": "B1", "device_type": "ftl_beacon", "location": "KAM-5-L4", "status": "monitoring",
         "features": ["stow", "audit"], "available_commands": ["deploy", "stow"], "tags": ["at:kam-3"]}
    cv = {"device_code": "CV1", "device_type": "cargo_vessel", "location": "KAM-OORT", "status": "idle",
          "features": ["surge", "transport"], "available_commands": ["travel", "stow"], "stow_capacity": 9, "tags": []}
    assert not lo.placed(b) and lo.placed({**b, "location": "KAM-3"}) and lo.placed({**b, "tags": []})
    stars = {"KAM": {"entry_point": "KAM-5-L4"}}
    p = lo.plan({"phases": [], "systems": {}}, [b, cv], [], {}, stars, {}, set(), [], {})
    dl = p["deliveries"]
    assert len(dl) == 1 and dl[0]["devices"] == ["B1"], dl
    bodies = [s["body"] for s in lo.delivery_steps(dl[0], p["by_code"], stars, False)]
    assert {"command": "travel", "destination": "KAM-3"} in bodies and {"command": "deploy"} in bodies


def test_new_print_bound_elsewhere_is_dispatched_at_once(client):
    eng = client.app.state.worker.automations
    db = client.app.state.db

    async def enable():
        s = await eng.settings()
        s["rules"]["loadouts"]["enabled"] = True
        await eng.save_settings(s)
    client.portal.call(enable)
    devices = [
        {"device_code": "AF1", "device_type": "autofactory", "location": "FAL-BELT-1", "status": "printing (mining_drone)",
         "available_commands": ["enqueue_print"], "tags": ["home:fal"]},
        {"device_code": "PL1", "device_type": "surge_plate", "location": "FAL-1-L4", "status": "idle", "attach_capacity": 1,
         "features": ["surge", "attach", "taxi"], "taxi_mode": "taxi", "available_commands": ["attach", "detach", "travel"],
         "tags": ["home:fal", "taxi"]},
        {"device_code": "NEW1", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "idle",
         "available_commands": ["travel", "stow", "deploy"], "tags": ["to:ith"]},
        # ITH is commandable: its relay links to the one in FAL (1 ly away), where a replicant is — an active network
        {"device_code": "RLF", "device_type": "ftl_relay", "location": "FAL-1-L4", "status": "relaying", "tags": []},
        {"device_code": "RLI", "device_type": "ftl_relay", "location": "ITH-2-L4", "status": "relaying", "tags": []},
    ]
    client.portal.call(db.kv_set, "devices", devices)
    reps = client.portal.call(db.kv_get, "replicants") or {}
    client.portal.call(db.kv_set, "replicants", {**reps, "RX": {"name": "rx", "location": "FAL-1-L4"}})
    cat = {"stars": [{"designation": "FAL", "position": {"x": 0, "y": 0, "z": 0}, "entry_point": "FAL-1-L4"},
                     {"designation": "ITH", "position": {"x": 1, "y": 0, "z": 0}, "entry_point": "ITH-2-L4"}]}
    client.portal.call(db.kv_set, "stars", cat)
    client.portal.call(client.app.state.worker.handle_event,
                       _ev(950, "print.completed", device_code="AF1", device_type="mining_drone", new_device_code="NEW1",
                           tags=["to:ith"], location="FAL-BELT-1"))
    assert "NEW1" in client.portal.call(db.kv_get, "dispatch_pending", {})
    started = client.portal.call(eng.dispatch_new_prints)
    assert started == ["PL1 carries NEW1 to ITH"]
    job = [j for j in client.portal.call(eng.jobs) if j["rule"] == "loadouts"][-1]
    assert job["title"].endswith("(just printed)") and job["device"] == "PL1"
    assert client.portal.call(db.kv_get, "dispatch_pending", {}) == {}


# --- 1.12.3: engine lock deadlock, already-deployed, home-tagged keepers -----------------------------------------------

def test_fleet_control_on_a_stalled_mission_does_not_deadlock(client):
    """Live 2026-10-04: Stop/Resume/End on a stalled fleet took the engine lock, then cancel() took it again —
    the request hung holding it and every tick and event waited behind it for ~43 h."""
    import threading
    eng = client.app.state.worker.automations
    fleet, devices = _fleet_world()
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(eng.save_jobs, [{"id": "fleets-1-0", "rule": "fleets", "title": "x: deploy", "device": None,
                                        "steps": [], "idx": 0, "status": "running", "created_at": "2026-10-04T02:54:07+00:00",
                                        "meta": {}}])
    fleet["mission"] = {"status": "stalled", "phase": "deploy", "targets": ["AEM"], "job": "fleets-1-0", "log": []}
    client.portal.call(eng.save_fleets, [fleet])
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", client.post(f"/fleets/{fleet['id']}/control",
                                                                          data={"action": "stop"}, headers=HX)), daemon=True)
    t.start()
    t.join(15)
    assert "r" in out, "fleet control hung (engine lock deadlock)"
    assert not eng.lock.locked()
    assert next(j for j in client.portal.call(eng.jobs) if j["id"] == "fleets-1-0")["status"] == "cancelled"
    assert client.portal.call(eng.fleets)[0]["mission"]["status"] == "stopped"
    # the reported follow-on symptom: adding a device to a fleet hung too (it waits for the same lock)
    code = next(d["device_code"] for d in devices if "fleet:" not in " ".join(d.get("tags") or []))
    out2 = {}
    t = threading.Thread(target=lambda: out2.setdefault("r", client.post(f"/fleets/{fleet['id']}/members",
                                                                           data={"add": code}, headers=HX)), daemon=True)
    t.start()
    t.join(15)
    assert "r" in out2 and any("membership" in j["title"] for j in client.portal.call(eng.jobs))


def test_engine_lock_is_reentrant_and_watchdog_alerts():
    from rsweb.automations import EngineLock
    import rsweb.automations as au

    async def go():
        lk = EngineLock()
        async with lk:
            async with lk:          # same task: no deadlock
                assert lk.locked() and lk.holder
            assert lk.locked()
        assert not lk.locked() and lk.holder is None
        # another task still waits for it
        order = []

        async def other():
            async with lk:
                order.append("other")
        async with lk:
            t = asyncio.create_task(other())
            await asyncio.sleep(0.01)
            order.append("first")
        await t
        assert order == ["first", "other"]

    run(go())

    class Eng(au.AutomationEngine):
        def __init__(self):
            super().__init__(None, None, None, None)
            self.logged = []

        async def log(self, rule, text, level="info", notify=False):
            self.logged.append((level, text))

    async def wd():
        e = Eng()
        await e.lock.acquire()
        e.lock.since -= au.LOCK_ALERT_SECONDS + 5
        e.stage = "run_fleets"
        await e.watchdog()
        await e.watchdog()                       # alerts once per stall
        assert len(e.logged) == 1 and e.logged[0][0] == "error" and "run_fleets" in e.logged[0][1]
        e.lock.release()
        await e.watchdog()
        assert "running again" in e.logged[-1][1]

    run(wd())


def test_deploy_already_deployed_counts_as_done(client):
    eng = client.app.state.worker.automations
    from rsweb.automations import step
    from rsweb.api import ApiError

    async def go():
        real = eng.send

        async def fake(method, path, body, label):
            if (body or {}).get("command") == "deploy":
                return False, None, "Device is already deployed"
            return await real(method, path, body, label)
        eng.send = fake
        steps = [step(f"deploy D{i}", f"/devices/D{i}", {"command": "deploy"}, wait=["device.deployed"]) for i in range(4)]
        async with eng.lock:
            job = await eng.create_job("fleets", "Surveyors: deploy", None, steps, force=True)
        return next(j for j in await eng.jobs() if j["id"] == job["id"])

    j = client.portal.call(go)
    assert j["status"] == "done" and all(s["status"] == "done" for s in j["steps"])


def test_loadout_surplus_keeps_the_home_tagged_device():
    """Live 2026-10-05: FALQUORYX wants 1 autofactory and has 3; the one tagged home:falquoryx was being made spare."""
    from rsweb import loadouts as lo
    cfg, devices, bps, inv, stars = _lo_world()
    cfg["phases"][0]["wants"]["autofactory"] = 1
    for code in ("AF0", "AF1"):
        devices.append({**next(d for d in devices if d["device_code"] == "AF"), "device_code": code})
    next(d for d in devices if d["device_code"] == "AF1")["tags"] = ["home:aaa"]
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    row = next(r for r in _rep(p, "AAA")["rows"] if r["type"] == "autofactory")
    assert row["surplus"] == 2 and "AF1" not in row["spare"]


# --- 1.13.0: placement (relays at Lagrange points, controllers at the belt), slingshot ---------------------------------

def test_placement_rules():
    from rsweb import placement as pl
    scan = {"entry_point": "AAA-5-L4", "asteroid_belt": {"belts": [{"designation": "AAA-BELT-1"}]},
            "planets": [{"designation": "AAA-5", "orbital_distance_au": 1.2}, {"designation": "AAA-1", "orbital_distance_au": 0.1}]}
    g = pl.geography("AAA", [], scan, "AAA-5-L4")
    assert g["belts"] == ["AAA-BELT-1"] and g["lagrange"][0] == "AAA-5-L4" and g["inner"][0] == "AAA-1"
    assert pl.ok("ftl_relay", "AAA-5-L4", g) and not pl.ok("ftl_relay", "AAA-BELT-1", g) and not pl.ok("ftl_relay", "AAA-5", g)
    assert pl.target("ftl_relay", g) == "AAA-5-L4"
    assert pl.ok("ami_mining_controller", "AAA-BELT-1", g) and not pl.ok("ami_mining_controller", "AAA-5-L4", g)
    assert pl.target("ami_mining_controller", g) == "AAA-BELT-1"
    assert not pl.ok("ami_survey_controller", "AAA-1", g)            # there is a belt: it goes there
    nobelt = pl.geography("BBB", [], {"planets": [{"designation": "BBB-2", "orbital_distance_au": 0.3},
                                                   {"designation": "BBB-1", "orbital_distance_au": 0.1}]}, "BBB-2-L4")
    assert pl.target("ami_survey_controller", nobelt) == "BBB-1"
    assert pl.ok("ami_survey_controller", "BBB-2-L4", nobelt) and not pl.ok("ami_survey_controller", "BBB-KUIPER", nobelt)
    assert pl.target("ami_mining_controller", nobelt) is None
    assert pl.ok("transport_drone", "BBB-KUIPER", nobelt)            # no rule for other types


def test_loadout_plan_places_relays_and_controllers():
    from rsweb import loadouts as lo
    from rsweb import placement as pl
    cfg, devices, bps, inv, stars = _lo_world()
    devices.append({"device_code": "RL", "device_type": "ftl_relay", "location": "AAA-BELT-1", "status": "idle",
                    "features": ["cruise", "relay"], "available_commands": ["travel", "activate"], "operational_capacity": 100.0})
    devices.append({"device_code": "RL2", "device_type": "ftl_relay", "location": "BBB-3", "status": "relaying",
                    "features": ["cruise", "relay"], "available_commands": ["travel", "activate"], "operational_capacity": 100.0})
    devices.append({"device_code": "MC2", "device_type": "ami_mining_controller", "location": "BBB-5-L4", "status": "idle",
                    "features": ["cruise", "ami"], "available_commands": ["travel"], "operational_capacity": 100.0})
    geo = {"AAA": pl.geography("AAA", devices, None, "AAA-OORT"),
           "BBB": pl.geography("BBB", devices, {"asteroid_belt": {"belts": [{"designation": "BBB-BELT-1"}]}}, "BBB-5-L4")}
    geo["AAA"]["lagrange"] = ["AAA-3-L4"]
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {}, geo=geo)
    places = dict(p["places"])
    assert places.get("RL") == "AAA-3-L4"
    assert "RL2" not in places and any(m["code"] == "RL2" for m in p["misplaced"])   # relaying: only reported
    assert places.get("MC2") == "BBB-BELT-1"
    assert "AC" not in places                                                      # already at the belt
    assert any("RL" in line and "AAA-3-L4" in line for line in lo.describe(p))


def test_relay_is_activated_only_at_a_lagrange_point():
    from rsweb.ami_schedule import wakeups
    devs = [{"device_code": "R1", "device_type": "ftl_relay", "location": "AAA-3-L4", "status": "idle", "tags": ["home:aaa"],
             "available_commands": ["activate", "travel"]},
            {"device_code": "R2", "device_type": "ftl_relay", "location": "AAA-BELT-1", "status": "idle", "tags": ["home:aaa"],
             "available_commands": ["activate", "travel"]},
            {"device_code": "R3", "device_type": "ftl_relay", "location": "AAA-4-L5", "status": "relaying", "tags": [],
             "available_commands": ["activate"]}]
    w = {x["code"]: x for x in wakeups(devs, set(), {})}
    assert w.get("R1", {}).get("activate") and "R2" not in w and "R3" not in w


def test_explore_fleet_without_a_controller_scans_with_its_drones():
    from rsweb import fleets as fl
    fleet = {"id": "s", "name": "S", "role": "explore", "home": "AAA"}
    devs = [{"device_code": "SD", "device_type": "survey_drone", "location": "BBB-2", "status": "idle", "tags": ["fleet:s"],
             "controller_device_code": None}]
    steps, problems = fl.explore_work_steps(fleet, devs)
    assert [(st["path"], st["body"]) for st in steps] == [("/devices/SD", {"command": "scan"})] and not problems
    devs[0]["location"] = "BBB-2-L4"                                                     # no survey from a Lagrange point
    steps, _ = fl.explore_work_steps(fleet, devs, "BBB-BELT-1")
    sends = [(st["path"], st["body"]) for st in steps if st["method"] != "WAIT"]
    assert sends == [("/devices/SD", {"command": "travel", "destination": "BBB-BELT-1"}), ("/devices/SD", {"command": "scan"})]


def test_explore_fleet_takes_survey_controller_to_the_belt():
    from rsweb import fleets as fl
    fleet = {"id": "s", "name": "S", "role": "explore", "home": "AAA"}
    devs = [{"device_code": "SC", "device_type": "ami_survey_controller", "location": "BBB-5-L4", "status": "idle", "tags": ["fleet:s"]},
            {"device_code": "SD", "device_type": "survey_drone", "location": "BBB-5-L4", "status": "idle", "tags": ["fleet:s"]}]
    steps, _ = fl.explore_work_steps(fleet, devs, "BBB-BELT-1")
    bodies = [(st["path"], st["body"]) for st in steps]
    assert bodies[0] == ("/devices/SC", {"command": "travel", "destination": "BBB-BELT-1"})
    assert ("/devices/SD", {"command": "travel", "destination": "BBB-BELT-1"}) in bodies
    i_adopt = next(i for i, b in enumerate(bodies) if (b[1] or {}).get("command") == "adopt")
    assert all(st["method"] == "WAIT" for st in steps[2:i_adopt])


def test_replicant_slingshot_link_and_fire(client):
    """Docs /docs/ftl-slingshots/: link to an empty matrix at the slingshot's location (PATCH linked_device); the
    replicant fires the slingshot where it is (teleport target = slingshot); ≥80 % capacity, 5 % after."""
    from rsweb.mock import REP
    world = client.app.state.api.http._transport.app.state.world
    sl = {"device_code": "SL000001", "device_type": "ftl_slingshot", "location": "SOL-BELT-1", "status": "idle",
          "features": ["slingshot", "stow"], "operational_capacity": 100.0, "linked_device": None, "replicant_code": REP,
          "available_commands": ["deploy", "stow"]}
    far = {**sl, "device_code": "SL000002", "location": "SOL-3-L4", "linked_device": "XX000009"}
    mx = {"device_code": "MX000001", "device_type": "empty_replicant_matrix", "location": "SOL-BELT-1", "status": "idle",
          "features": ["matrix"], "operational_capacity": 100.0, "replicant_code": REP, "available_commands": ["deploy"]}
    mx2 = {**mx, "device_code": "MX000002", "location": "SOL-5"}
    world.devices += [sl, far, mx, mx2]
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get(f"/replicants/{REP}", headers=H).text
    assert "FTL slingshot" in page and "SL000001" in page and 'value="MX000001"' in page and '<option value="MX000002"' not in page
    r = client.post(f"/replicants/{REP}/slingshot", data={"slingshot": "SL000001"}, headers=HX)
    assert "linked to a matrix yet" in r.text and not world.teleports
    r = client.post(f"/replicants/{REP}/slingshot", data={"slingshot": "SL000002"}, headers=HX)
    assert "go to the slingshot first" in r.text and not world.teleports
    r = client.post("/slingshots/SL000001/link", data={"matrix": "MX000002"}, headers=HX)
    assert "must be together" in r.text and not sl["linked_device"]
    client.post("/slingshots/SL000001/link", data={"matrix": "MX000001"}, headers=HX)
    assert sl["linked_device"] == "MX000001"
    client.portal.call(client.app.state.worker.sync_devices)
    client.post(f"/replicants/{REP}/slingshot", data={"slingshot": "SL000001"}, headers=HX)
    assert world.teleports == [(REP, "SL000001")]
    client.portal.call(client.app.state.worker.sync_devices)
    r = client.post(f"/replicants/{REP}/slingshot", data={"slingshot": "SL000001"}, headers=HX)
    assert "at least 80" in r.text and len(world.teleports) == 1


def test_slingshot_stowed_with_the_replicant_counts_as_here(client):
    """1.208.1 (live 2026-10-22: slingshot 7DBF642C and replicant 8EC42FBA's matrix in the same hold of 939E7168):
    a stowed slingshot has no location of its own; it is where its carrier is. Linking works there; firing needs it
    deployed (1.209.1: the game answers 400 "Slingshot must be deployed"), so the page says so instead."""
    from rsweb.mock import REP
    world = client.app.state.api.http._transport.app.state.world
    vs = {"device_code": "VS000001", "device_type": "cargo_vessel", "location": "SOL-BELT-1", "status": "idle",
          "features": ["stow"], "operational_capacity": 100.0, "replicant_code": REP, "available_commands": ["travel"]}
    sl = {"device_code": "SL000003", "device_type": "ftl_slingshot", "location": None, "status": "stowed",
          "stowed_in_device_code": "VS000001", "features": ["slingshot", "stow"], "operational_capacity": 100.0,
          "linked_device": None, "replicant_code": REP, "available_commands": ["deploy"]}
    mx = {"device_code": "MX000003", "device_type": "empty_replicant_matrix", "location": None, "status": "stowed",
          "stowed_in_device_code": "VS000001", "features": ["matrix"], "operational_capacity": 100.0,
          "replicant_code": REP, "available_commands": ["deploy"]}
    world.devices += [vs, sl, mx]
    client.portal.call(client.app.state.worker.sync_devices)
    r = client.post("/slingshots/SL000003/link", data={"matrix": "MX000003"}, headers=HX)
    assert "must be together" not in r.text and sl["linked_device"] == "MX000003"
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get(f"/replicants/{REP}", headers=H).text
    assert '<option value="SL000003">' not in page and "SL000003 is stowed in" in page
    r = client.post(f"/replicants/{REP}/slingshot", data={"slingshot": "SL000003"}, headers=HX)
    assert "deploy it first" in r.text and not world.teleports   # live: 400 "Slingshot must be deployed"
    sl.update(location="SOL-BELT-1", status="idle", stowed_in_device_code=None)
    client.portal.call(client.app.state.worker.sync_devices)
    assert '<option value="SL000003">' in client.get(f"/replicants/{REP}", headers=H).text
    client.post(f"/replicants/{REP}/slingshot", data={"slingshot": "SL000003"}, headers=HX)
    assert world.teleports == [(REP, "SL000003")]


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


def test_split_prints_evenly_by_print_time():
    from rsweb import printqueue as pq
    bps = {"survey_drone": {"print_time": 240}, "mining_drone": {"print_time": 180}, "relay": {"print_time": 600}}
    f = lambda code, **kw: {"device_code": code, "device_type": "autofactory", "status": "idle", **kw}  # noqa: E731
    a, b, c = f("A"), f("B", status="printing (relay)", printing={"device_type": "relay"}), f("C")
    load = {x["device_code"]: pq.load_seconds(x, bps) for x in (a, b, c)}
    assert load == {"A": 0, "B": 600, "C": 0}
    parts = pq.split([a, b, c], "mining_drone", 5, bps, load)
    # A and C take the first four; B, 600 s behind, only gets the 5th once A and C would be past 540 s
    assert [(x["device_code"], k) for x, k in parts] == [("A", 2), ("C", 3)] or \
           [(x["device_code"], k) for x, k in parts] == [("A", 3), ("C", 2)]
    parts = pq.split([a, b, c], "survey_drone", 4, bps, load)        # carries on from the load so far
    assert sum(k for _, k in parts) == 4 and max(load.values()) - min(load.values()) <= 240
    room = {"A": 1, "B": 0, "C": 0}
    assert [(x["device_code"], k) for x, k in pq.split([a, b, c], "relay", 3, bps, {}, room)] == [("A", 1)]
    assert pq.least_loaded([b, c], bps)["device_code"] == "C"


def test_loadout_prints_spread_over_a_systems_autofactories():
    from rsweb import loadouts as lo
    cfg, devices, bps, inv, stars = _lo_world()
    devices.append({**next(d for d in devices if d["device_code"] == "AF"), "device_code": "AF2"})
    devices.append({**next(d for d in devices if d["device_code"] == "AF"), "device_code": "AF3",
                    "status": "printing (ami_mining_controller)", "printing": {"device_type": "ami_mining_controller"}})
    inv = {"AAA-3-L4": {"structural": 1000.0}}
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    per = Counter()
    for pr in p["prints"]:
        assert pr["device_type"] == "survey_drone"
        per[pr["factory"]] += pr["n"]
    # 3 survey drones short (2 in AAA, 1 in BBB): shared by AF and AF2; AF3 is 600 s into a controller, so it gets none
    assert sum(per.values()) == 3 and sorted((per["AF"], per["AF2"])) == [1, 2] and not per["AF3"]
    # one print per factory step, each tagged for its system
    assert {pr["star"] for pr in p["prints"]} == {"AAA", "BBB"}


def test_planner_spreads_over_autofactories_at_the_same_stockpile(client):
    world = client.app.state.api.http._transport.app.state.world
    af = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    world.devices.append({**af, "device_code": "AF00CAFE"})
    world.queues["AF00BEEF"], world.queues["AF00CAFE"] = [], []
    client.portal.call(client.app.state.worker.sync_devices)
    data = {"printer": "device:AF00BEEF", "qty:mining_drone": "3", "qty:survey_drone": "1"}
    r = client.post("/blueprints/plan", data=data, headers=HX)
    assert "Spread over the 2 autofactories at SOL-3-L4" in r.text and 'name="nosplit" checked' not in r.text
    assert "Queue on 2 autofactories" in r.text
    r = client.post("/blueprints/queue-plan", data={**data, "_planned": "1"}, headers=HX)   # spread unless told not to
    job = client.portal.call(client.app.state.worker.automations.jobs)[-1]
    queued = [(s["path"], s["body"]["device_type"], s["body"]["quantity"]) for s in job["steps"]
              if s["body"].get("command") == "enqueue_print"]
    assert sum(q for _, _, q in queued) == 4 and {p for p, _, _ in queued} == {"/devices/AF00BEEF", "/devices/AF00CAFE"}
    # "keep it on one": everything on the chosen printer
    r = client.post("/blueprints/queue-plan", data={**data, "_planned": "1", "nosplit": "on"}, headers=HX)
    job = client.portal.call(client.app.state.worker.automations.jobs)[-1]
    assert {s["path"] for s in job["steps"] if s["body"].get("command") == "enqueue_print"} == {"/devices/AF00BEEF"}
    # Queue a print (Blueprints): 3 of one type are shared too
    world.queues["AF00BEEF"], world.queues["AF00CAFE"] = [], []
    client.portal.call(client.app.state.worker.sync_devices)
    client.post("/blueprints/print", data={"printer": "device:AF00BEEF", "device_type": "mining_drone", "quantity": "3"}, headers=HX)
    assert world.queues["AF00BEEF"] and world.queues["AF00CAFE"]


def test_missions_cannot_target_a_system_with_a_home_fleet(client):
    eng = client.app.state.worker.automations
    client.post("/fleets", data={"name": "Scouts", "role": "explore", "home": "SOL"}, headers=HX)
    # an old config (home fleets by system): KEL's becomes a stationed fleet on the next load
    client.portal.call(client.app.state.db.kv_set, "loadouts",
                       {"phases": [{"id": "p", "name": "Outpost", "order": 1, "wants": {"survey_drone": 1}}],
                        "systems": {"KEL": "p", "ABC": ""}})
    page = client.get("/fleets/details?all=1", headers=H).text
    targets = page[page.index('<datalist id="fl-stars-scouts">'):]
    targets = targets[:targets.index("</datalist>")]
    assert "Not available as targets" in page and '"KEL"' not in targets
    kel = next(f for f in client.portal.call(eng.fleets) if f["home"] == "KEL")
    assert kel["station"] and kel["template"] == "p" and kel["wants"] == {"survey_drone": 1}
    assert "systems" not in client.portal.call(client.app.state.db.kv_get, "loadouts")
    r = client.post("/fleets/scouts/mission", data={"targets": "ABOTEIN, KEL-BELT-1"}, headers=HX)
    assert "KEL-BELT-1: has a stationed fleet" in r.text
    assert not client.portal.call(eng.fleets)[0].get("mission")
    r = client.post("/fleets/scouts/mission", data={"targets": "ABOTEIN"}, headers=HX)   # no fleet stationed there
    assert r.headers.get("HX-Refresh") and client.portal.call(eng.fleets)[0]["mission"]["targets"] == ["ABOTEIN"]


def test_rules_live_on_their_pages_and_nav_is_grouped(client):
    eng = client.app.state.worker.automations
    # the Automations page lists every rule with a link to where its settings are
    page = client.get("/automations", headers=H).text
    assert "Asteroid defense" in page and 'href="/defence">Map › Defense →' in page
    assert 'name="max_prints"' not in page                       # the settings cards moved out
    # each page loads its own rules' cards
    assert 'rules-panel?ids=asteroid_defence,asteroid_mining"' in client.get("/defence", headers=H).text
    r = client.get("/automations/rules-panel?ids=asteroid_defence,nope", headers=HX)
    assert 'hx-post="/automations/rules/asteroid_defence"' in r.text and 'name="max_prints"' in r.text and "nope" not in r.text
    assert "Survey this system now" in client.get("/automations/rules-panel?ids=auto_survey", headers=HX).text
    # the overview toggle flips the switch and keeps the options
    s = client.portal.call(eng.settings)
    before = dict(s["rules"]["asteroid_defence"])
    client.post("/automations/rules/asteroid_defence/toggle", headers=HX)
    after = client.portal.call(eng.settings)["rules"]["asteroid_defence"]
    assert after["enabled"] != before["enabled"] and {k: v for k, v in after.items() if k != "enabled"} == \
           {k: v for k, v in before.items() if k != "enabled"}
    # AMI schedules moved to the AMI page
    assert "AMI schedules" in client.get("/ami", headers=H).text
    # nav: 6 groups, the current one's pages as sub-tabs; old URLs still work
    page = client.get("/defence", headers=H).text
    nav = page[page.index('<nav class="main">'):page.index("</nav>")]
    for g in ("Dashboard", "Devices", "Map", "Fleets", "Economy", "Activity"):
        assert f">{g}<" in nav or f">{g}<span" in nav
    sub = page[page.index('<nav class="sub">'):]
    sub = sub[:sub.index("</nav>")]
    assert all(t in sub for t in ("Galaxy", "Systems", "Traffic", "Defense", "Upkeep")) and "Blueprints" not in sub
    assert 'href="/diagnostics"' in page and 'href="/console"' in page   # account menu
    fp = client.get("/loadouts", headers=H).text   # the old Home fleets URL lands on Fleets
    assert "<h1>Fleets" in fp and ">Fleets</a>" in fp and ">Reset &amp; reform</a>" in fp
    assert "Home fleets" not in fp and "Mobile fleets" not in fp


def test_fleet_follows_a_template():
    from rsweb import fleets as fl
    cfg = {"phases": [{"id": "pros", "name": "Prospector", "wants": {"mining_drone": 6, "survey_drone": "", "mobile_fleet": 1,
                                                                      "cargo_freighter": 0}}]}
    f = fl.resolve_template({"id": "x", "wants": {"mining_drone": 2}, "template": "pros"}, cfg)
    assert f["wants"] == {"mining_drone": 6, "mobile_fleet": 1}          # blank and 0 mean none for a fleet
    cfg["phases"][0]["wants"]["mining_drone"] = 8                         # editing the template updates the fleet
    assert fl.resolve_template(f, cfg)["wants"]["mining_drone"] == 8
    gone = fl.resolve_template({"id": "y", "wants": {"mining_drone": 3}, "template": "deleted"}, cfg)
    assert gone["wants"] == {"mining_drone": 3}                           # a deleted template leaves the last loadout


def test_fleet_fill_takes_nearest_spares_and_comes_back():
    from rsweb import fleets as fl
    fleet, devices = _fleet_world()
    devices += [{"device_code": "SPARE001", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "idle",
                 "tags": ["spare"], "available_commands": ["travel"]},
                {"device_code": "SPARE002", "device_type": "mining_drone", "location": "FAR-BELT-1", "status": "idle",
                 "tags": ["spare"], "available_commands": ["travel"]},
                {"device_code": "BUSY0001", "device_type": "mining_drone", "location": "AEM-5-L4", "status": "mining (carbon)",
                 "tags": ["spare"], "available_commands": ["travel"]}]
    stars = {"AEM": {"position": {"x": 0, "y": 0, "z": 0}, "entry_point": "AEM-5-L4"},
             "FAL": {"position": {"x": 1, "y": 0, "z": 0}, "entry_point": "FAL-4-L4"},
             "FAR": {"position": {"x": 9, "y": 0, "z": 0}, "entry_point": "FAR-4-L4"}}
    steps, recruits, notes = fl.fill_plan(fleet, devices, stars, set())
    assert [d["device_code"] for d in recruits] == ["SPARE001"]           # 1 short: the nearest idle spare
    bodies = [(s["path"], s["body"]) for s in steps]
    assert ("/devices/SPARE001", {"configuration": {"add_tags": ["fleet:prospector-1"], "remove_tags": ["spare"]}}) in bodies
    travels = [b["destination"] for p, b in bodies if p == "/devices/MF000001" and b and b.get("command") == "travel"]
    assert travels == ["FAL-4-L4", "AEM-5-L4"]                             # picks up (and the stranded LOST0001), comes back


def test_reform_rebuilds_assignments_from_where_devices_are():
    from rsweb import reform
    D = lambda code, t, loc, tags=(), **kw: {"device_code": code, "device_type": t, "location": loc, "status": "idle",  # noqa: E731
                                             "tags": list(tags), **kw}
    devices = [
        D("MF1", "mobile_fleet", "AAA-5-L4", ["fleet:f1"]),
        D("M1", "mining_drone", "AAA-5-L4", ["fleet:f1", "home:aaa"]),       # member with a stray home tag
        D("M2", "mining_drone", "AAA-5-L4", ["spare", "to:bbb"]),            # stale to: — in AAA, idle
        D("M3", "mining_drone", "AAA-5-L4", ["home:bbb"], controller_device_code="CB"),  # wrong home, run from BBB
        D("M4", "mining_drone", "AAA-5-L4", ["home:aaa", "keepme"], controller_device_code="CA"),
        D("CA", "ami_mining_controller", "AAA-BELT-1", ["home:aaa"], features=["ami"]),
        D("CB", "ami_mining_controller", "BBB-BELT-1", ["home:bbb"], features=["ami"]),
        D("AF", "autofactory", "AAA-3-L4", []),
        D("P1", "surge_plate", "AAA-5-L4", ["taxi", "home:aaa"]),            # ferry gear: untouched
        D("K1", "mining_drone", "AAA-5-L4", ["keep", "spare"]),              # ignored
        D("MV", "mining_drone", "AAA-5-L4", ["to:ccc"], status="moving"),     # in flight: untouched
        D("X1", "survey_drone", "ZZZ-1", ["spare"]), D("X2", "survey_drone", "ZZZ-1", ["home:zzz"]),
    ]
    cfg = {"phases": [{"id": "p", "name": "P", "wants": {"mining_drone": 1, "ami_mining_controller": 1, "autofactory": None}}],
           "ignore_tags": ["keep"]}
    st_wants = {"mining_drone": 1, "ami_mining_controller": 1}     # template p, resolved (blank = don't care)
    fleets = [{"id": "f1", "name": "F1", "home": "AAA", "wants": {"mobile_fleet": 1, "mining_drone": 2}},
              {"id": "aaa", "name": "Aaa", "home": "AAA", "station": True, "template": "p", "wants": st_wants},
              {"id": "bbb", "name": "Bbb", "home": "BBB", "station": True, "template": "p", "wants": st_wants}]
    p = reform.plan(cfg, devices, fleets, busy=set(), hosts=set())
    to = {r["code"]: (r["to"], sorted(r["remove"])) for r in p["retag"]}
    assert to["M1"] == ("fleet:f1", ["home:aaa"])
    assert to["M2"] == ("fleet:f1", ["spare", "to:bbb"])                    # fills the fleet's gap
    assert to["M3"] == ("spare", ["home:bbb"])                              # AAA's fleet wants 1 miner: M4, home there before
    assert to["M4"] == ("fleet:aaa", ["home:aaa"])                          # joins AAA's stationed fleet (keeps 'keepme')
    assert to["CA"] == ("fleet:aaa", ["home:aaa"]) and to["CB"] == ("fleet:bbb", ["home:bbb"])
    assert "AF" not in to                                                   # blank in the template: stays untagged
    assert "MF1" not in to                                                  # already right
    assert to["X2"] == ("(untagged)", ["home:zzz"])                        # no fleet stationed in ZZZ
    assert "X1" not in to                                                   # spare in an unmanaged system stays spare
    assert {s["code"] for s in p["skipped"]} >= {"P1", "K1", "MV"}
    assert p["releases"] == {"CB": ["M3"]}                                 # spare now, and run from another system
    st = reform.steps(p)
    assert st[0]["body"] == {"command": "release", "devices": ["M3"]} and st[0]["path"] == "/devices/CB"
    assert reform.plan(cfg, devices, fleets, set(), set(), full=True)["releases"] == {"CA": ["M4"], "CB": ["M3"]}


def test_reform_and_fill_pages(client):
    page = client.get("/fleets/reform", headers=H).text
    assert "<h1>Reset &amp; reform" in page and ">Reset &amp; reform</a>" in page
    r = client.post("/fleets/reform/preview", data={}, headers=HX)
    assert "Tag changes" in r.text and "Controller releases" in r.text
    r = client.post("/fleets/reform/apply", data={}, headers=HX)
    assert "Started job" in r.text or "Nothing to change" in r.text
    client.post("/fleets", data={"name": "Scouts", "role": "explore", "home": "SOL"}, headers=HX)
    client.portal.call(client.app.state.db.kv_set, "loadouts",
                       {"phases": [{"id": "sc", "name": "Scout set", "order": 1, "wants": {"survey_drone": 2}}], "systems": {}})
    client.post("/fleets/scouts/edit", data={"template": "sc"}, headers=HX)
    page = client.get("/fleets/details?all=1", headers=H).text
    assert '<option value="sc" selected>Scout set</option>' in page and "fieldset disabled" in page
    assert "Fill from spares" in page and 'rules-panel?ids=loadouts,fleet_fill"' in client.get("/fleets", headers=H).text
    assert "Scouts" in client.post("/fleets/scouts/fill", headers=HX).text or "Nothing to fill" in client.post("/fleets/scouts/fill", headers=HX).text


# --- stationed fleets (1.18.0) ------------------------------------------------------------------------------
def _st(fid, home, wants, **kw):
    return {"id": fid, "name": fid.title(), "role": "mining", "home": home, "station": True, "wants": wants, **kw}


def test_migrate_home_fleets_and_roles_to_fleets():
    from rsweb import fleets as fl
    cfg = {"phases": [{"id": "p", "name": "Outpost", "wants": {"mining_drone": 2, "survey_drone": "0"}}],
           "systems": {"AAA": "p", "BBB": "p", "CCC": "gone"}, "roles": {"AAA": "source", "DDD": "destination", "EEE": "destination"}}
    mobile = [{"id": "scouts", "name": "Scouts", "role": "explore", "home": "AAA", "wants": {"survey_drone": 2}}]
    pos = {"AAA": {"x": 0, "y": 0, "z": 0}, "DDD": {"x": 9, "y": 0, "z": 0}, "EEE": {"x": 2, "y": 0, "z": 0}}
    new, fleets, changed = fl.migrate(cfg, mobile, pos)
    assert changed and new["fleets_migrated"] and "systems" not in new and "roles" not in new
    by = {f["id"]: f for f in fleets}
    assert by["scouts"]["station"] is False and by["scouts"]["materials"] == ""      # mobile fleets carry on as before
    assert by["aaa-home"] == {"id": "aaa-home", "name": "Aaa home", "role": "mining", "home": "AAA", "wants": {},
                              "station": True, "materials": "eee-home", "template": "p"}   # nearest destination
    assert by["bbb-home"]["template"] == "p" and "ccc-home" not in by                  # unknown phase: nothing
    assert by["ddd-home"]["materials"] == by["eee-home"]["materials"] == "self"
    assert fl.migrate(new, fleets) == (new, fleets, False)                             # only once
    f = fl.resolve_template(dict(by["aaa-home"]), new)
    assert f["wants"] == {"mining_drone": 2} and fl.station_wants(f) == {"mining_drone": 2, "survey_drone": 0}


def test_two_stationed_fleets_share_a_system():
    from rsweb import loadouts as lo
    D = lambda code, t, tags=(): {"device_code": code, "device_type": t, "location": "AAA-BELT-1", "status": "idle",  # noqa: E731
                                  "tags": list(tags)}
    devices = [D("M1", "mining_drone", ["fleet:miners"]), D("M2", "mining_drone"), D("M3", "mining_drone"), D("M4", "mining_drone"),
               D("S1", "survey_drone"), D("T1", "transport_drone")]
    fleets = [_st("miners", "AAA", {"mining_drone": 2}), _st("reserve", "AAA", {"mining_drone": 1, "survey_drone": 0})]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, {}, {}, set(), [], {})
    joins = {c: t for c, tags in p["tag_add"].items() for t in tags if t.startswith("fleet:")}
    assert joins == {"M2": "fleet:miners", "M3": "fleet:reserve"}      # members first, then fleetless ones, in fleet order
    assert p["tag_add"]["M4"] == ["spare"] and p["tag_add"]["S1"] == ["spare"]   # counted there (S1: a 0 line) but not taken
    assert "T1" not in p["tag_add"]                                     # nobody counts transport drones: don't care
    assert {r["fleet"]["id"] for r in p["report"].values()} == {"miners", "reserve"}


def test_stationed_fleet_extras_and_template_zero_leave_the_fleet():
    from rsweb import loadouts as lo
    devices = [{"device_code": c, "device_type": t, "location": "AAA-BELT-1", "status": "idle", "tags": ["fleet:home"]}
               for c, t in (("M1", "mining_drone"), ("M2", "mining_drone"), ("S1", "survey_drone"))]
    cfg = {"phases": [{"id": "p", "name": "P", "wants": {"mining_drone": 1, "survey_drone": "0"}}], "fleets_migrated": True}
    from rsweb import fleets as fl
    cfg["fleets"] = [fl.resolve_template(_st("home", "AAA", {}, template="p"), cfg)]
    p = lo.plan(cfg, devices, [], {}, {}, {}, set(), [], {})
    assert sorted(p["made_spare"]) == ["M2", "S1"]
    assert p["tag_add"]["S1"] == ["spare"] and p["tag_remove"]["S1"] == ["fleet:home"]
    assert "M1" not in p["tag_add"] and "M1" not in p["tag_remove"]


def test_rules_work_a_stationed_fleets_devices_at_home_only():
    from rsweb import ami_schedule as amis
    at_home = {"device_code": "M1", "device_type": "mining_drone", "location": "AAA-BELT-1", "status": "idle", "tags": ["fleet:home"]}
    away = {**at_home, "device_code": "M2", "location": "BBB-BELT-1"}
    loose = {**at_home, "device_code": "M3", "tags": []}
    ctrl = {"device_code": "C1", "device_type": "ami_mining_controller", "location": "AAA-BELT-1", "status": "idle", "tags": []}
    try:
        amis.set_stationed({"fleet:home": "AAA"})
        assert not amis.reserved(at_home) and amis.reserved(away) and not amis.reserved(loose)
        # a fleetless controller at home adopts the stationed fleet's drones and fleetless ones alike
        assert amis.adoptable([at_home, loose, ctrl], ctrl, {}) == ["M1", "M3"]
        amis.set_stationed({})   # on a mission (or not stationed): the rules leave its devices alone
        assert amis.reserved(at_home) and amis.adoptable([at_home, loose, ctrl], ctrl, {}) == ["M3"]
    finally:
        amis.set_stationed({})


def test_materials_go_to_the_fleet_named():
    from rsweb import loadouts as lo
    devices = [{"device_code": "TC", "device_type": "ami_transport_controller", "location": "AAA-2-L4", "status": "idle",
                "available_commands": ["adopt", "set_directive"], "tags": []},
               {"device_code": "FR", "device_type": "cargo_freighter", "location": "AAA-2-L4", "status": "idle", "tags": []},
               {"device_code": "AF", "device_type": "autofactory", "location": "FAR-3", "status": "idle",
                "available_commands": ["enqueue_print"], "tags": []}]
    stars = {"AAA": {"position": {"x": 0, "y": 0, "z": 0}}, "NEAR": {"position": {"x": 1, "y": 0, "z": 0}},
             "FAR": {"position": {"x": 50, "y": 0, "z": 0}}}
    fleets = [_st("src", "AAA", {}, materials="far"), _st("near", "NEAR", {}, materials="self"), _st("far", "FAR", {}, materials="self"),
              _st("loop", "AAA", {}, materials="src")]
    routes, unmet = lo.material_routes({"fleets": fleets}, devices, {"AAA-2-L4": {"carbon": 50}}, stars, set(), {}, {})
    assert [(r["source"], r["dest"], r["deliver"], r["from_fleet"], r["to_fleet"]) for r in routes] == \
        [("AAA", "FAR", "FAR-3", "Src", "Far")]                       # the fleet it names, not the nearest
    assert any("same system" in u["why"] and u["fleet"] == "Loop" for u in unmet)
    assert lo.spare_depot({"fleets": fleets, "settings": {}}, devices) == "FAR"   # a destination fleet's home with an autofactory


def test_back_to_station_clears_an_ended_mission(client):
    eng = client.app.state.worker.automations
    client.post("/fleets", data={"name": "Sol home", "home": "SOL", "station": "on"}, headers=HX)
    items = client.portal.call(eng.fleets)
    items[0]["mission"] = {"status": "ended", "targets": ["KEL"], "log": []}
    client.portal.call(eng.save_fleets, items)
    from rsweb import fleets as fl
    assert fl.away(client.portal.call(eng.fleets)[0])
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Back to station" in page and "mission: ended" in client.get("/fleets/overview", headers=HX).text
    client.post("/fleets/sol-home/control", data={"action": "station"}, headers=HX)
    f = client.portal.call(eng.fleets)[0]
    assert f["mission"]["status"] == "done" and fl.stationed(f)


def test_recall_waits_for_passengers_already_flying_back():
    """Seen live 2026-10-06: survey_system (recall on) had already sent the drones back to the vessel; the fleet's recall
    ordered them there again, the game said "Device is already in motion" and the mission stalled."""
    from rsweb import fleets as fl
    fleet = {"id": "surveyors", "name": "Surveyors", "role": "explore", "home": "FAL", "wants": {}}
    trip = lambda dest: {"destination": dest, "final_destination": dest, "eta_seconds": 1424,  # noqa: E731
                         "arrives_at": "2099-01-01T00:00:00+00:00"}
    devices = [
        {"device_code": "HV", "device_type": "heaven_vessel", "location": "OTH-OORT", "status": "idle", "features": ["surge"],
         "stow_capacity": 10, "tags": ["fleet:surveyors"]},
        {"device_code": "D1", "device_type": "survey_drone", "location": None, "status": "recalling", "features": ["stow"],
         "travel": trip("OTH-OORT"), "controller_device_code": "SC", "tags": ["fleet:surveyors"]},
        {"device_code": "D2", "device_type": "survey_drone", "location": None, "status": "recalling", "features": ["stow"],
         "travel": trip("OTH-3"), "tags": ["fleet:surveyors"]},
        {"device_code": "D3", "device_type": "survey_drone", "location": "OTH-2", "status": "idle", "features": ["stow"],
         "tags": ["fleet:surveyors"]},
        {"device_code": "SC", "device_type": "ami_survey_controller", "location": "OTH-1", "status": "coordinating", "tags": []},
    ]
    steps, problems = fl.assemble_steps(fleet, devices)
    plan = [(s["method"], s["desc"]) for s in steps]
    assert problems == []
    assert ("POST", "D1 → OTH-OORT (board)") not in plan                      # already on its way: no new order
    w = next(s for s in steps if s["desc"] == "wait for D1 at OTH-OORT")
    assert w["seq0_from"] == 0 and w["timeout"] > 3600                          # counts from the job start, waits for its ETA
    # D2 lands at a planet and D3 is at one: ~2000 AU of cruising from the vessel at the Oort cloud, so the vessel fetches them
    assert not any(d.endswith("(board)") for _, d in plan)
    assert plan.index(("WAIT", "wait for D2 at OTH-3")) < plan.index(("POST", "HV → OTH-3 (pick up D2)"))
    assert [d for _, d in plan[-5:]] == ["stow D1 in HV", "HV → OTH-2 (pick up D3)", "stow D3 in HV",
                                         "HV → OTH-3 (pick up D2)", "stow D2 in HV"]
    assert [c["device_code"] for c in fl.outside_controllers(fleet, devices)] == ["SC"]


def test_far_passengers_are_fetched_near_ones_fly_over():
    from rsweb import fleets as fl, loadouts as lo
    scan = {"planets": [{"designation": "OTH-2", "orbital_distance_au": 0.7}, {"designation": "OTH-3", "orbital_distance_au": 1.0}],
            "asteroid_belt": {"belts": [{"designation": "OTH-BELT-1", "inner_radius_au": 2, "outer_radius_au": 4}]},
            "outer_system": {"kuiper": {"designation": "OTH-KUIPER", "distance_au": 48.0},
                             "oort": {"designation": "OTH-OORT", "distance_au": 1800.0}}}
    radii = fl.system_radii(scan)
    assert radii == {"OTH-2": 0.7, "OTH-3": 1.0, "OTH-BELT-1": 3.0, "OTH-KUIPER": 48.0, "OTH-OORT": 1800.0}
    assert fl.radius_au("OTH-3-L4", radii) == 1.0 and fl.radius_au("OTH-3-2", radii) == 1.0   # L-points, moons: their planet
    assert fl.cruise_au("OTH-2", "OTH-BELT-1", radii) == pytest.approx(2.3) and fl.far_apart("OTH-3", "OTH-KUIPER", radii)
    assert fl.far_apart("ZZ-1", "ZZ-OORT") and not fl.far_apart("ZZ-1", "ZZ-BELT-1")   # unscanned: guessed from the codes
    fleet = {"id": "f", "name": "F", "role": "mining", "home": "OTH", "wants": {}}
    D = lambda code, loc: {"device_code": code, "device_type": "survey_drone", "location": loc, "status": "idle",  # noqa: E731
                           "features": ["stow", "cruise"], "tags": ["fleet:f"]}
    devices = [{"device_code": "HV", "device_type": "cargo_vessel", "location": "OTH-3", "status": "idle", "features": ["surge"],
                "stow_capacity": 50, "tags": ["fleet:f"]},
               D("N1", "OTH-2"), D("N2", "OTH-BELT-1"), D("F1", "OTH-KUIPER"), D("F2", "OTH-OORT")]
    plan = [s["desc"] for s in fl.assemble_steps(fleet, devices, radii)[0]]
    assert "N1 → OTH-3 (board)" in plan and "N2 → OTH-3 (board)" in plan        # short hops: they fly over
    assert plan[-4:] == ["HV → OTH-KUIPER (pick up F1)", "stow F1 in HV", "HV → OTH-OORT (pick up F2)", "stow F2 in HV"]
    assert plan.index("stow N1 in HV") < plan.index("HV → OTH-KUIPER (pick up F1)")   # near ones board before it leaves
    plan = [s["desc"] for s in fl.assemble_steps(fleet, devices, radii, limit=5000)[0]]
    assert "F2 → OTH-3 (board)" in plan                                          # the limit is a setting
    # loadout deliveries: same rule, and everyone at the pick-up point boards before the carrier goes fetching
    by = {d["device_code"]: d for d in devices}
    by["BC"] = {"device_code": "BC", "device_type": "ftl_beacon", "location": "OTH-2", "available_commands": ["stow"]}
    dl = {"carrier": "HV", "carrier_loc": "OTH-3", "from": "OTH", "to": "AEM", "devices": ["N1", "F2", "BC"], "mode": "stow"}
    steps = [s["desc"] for s in lo.delivery_steps(dl, by, {}, False, radii=radii)]
    assert "N1 → OTH-3 (to board HV)" in steps and not any(x.startswith("F2 →") for x in steps)
    assert steps.index("stow N1 in HV") < steps.index("HV → OTH-2 (pick up BC)") < steps.index("HV → OTH-OORT (pick up F2)")


# --- stellar census (1.19.0) -------------------------------------------------------------------------------------
CENSUS = {"page": 1, "per_page": 20, "total_pages": 1, "replicant_position": {"x": -458.9, "y": -223.75, "z": 3.3},
          "stars": [{"designation": "OTHILETH", "distance_from_replicant": 0.0, "entry_point": "OTHILETH-1-L4", "explored": True,
                     "estimated_travel_time": 0, "position": {"x": -458.9, "y": -223.75, "z": 3.3}, "has_ward": True},
                    {"designation": "ZALDANAL", "distance_from_replicant": 5.83, "entry_point": None, "explored": False,
                     "estimated_travel_time": 291, "estimated_planets": 3, "position": {"x": -463.461, "y": -220.8789, "z": 1.1055}},
                    {"designation": "LORQELYR", "distance_from_replicant": 14.55, "entry_point": None, "explored": False,
                     "estimated_travel_time": 727, "position": {"x": -466.2084, "y": -212.8483, "z": 9.5955}}]}


def test_census_merges_into_the_catalogue_and_lists_unexplored():
    from rsweb import census
    cat = {"stars": [{"designation": "SOL", "position": {"x": 0, "y": 0, "z": 0}},
                     {"designation": "OTHILETH", "position": {"x": -458.9, "y": -223.75, "z": 3.3}}]}
    known = {s["designation"]: s for s in CENSUS["stars"]}
    merged = census.merge(cat, known)
    by = {s["designation"]: s for s in merged["stars"]}
    assert by["ZALDANAL"]["from_census"] and "distance_from_replicant" not in by["ZALDANAL"]   # beyond the catalog: added
    assert by["OTHILETH"]["explored"] is True and by["OTHILETH"]["entry_point"] == "OTHILETH-1-L4"
    assert not by["OTHILETH"].get("from_census") and len(merged["stars"]) == 4
    assert census.seconds_per_ly(known) == pytest.approx(727 / 14.55)
    rows = census.unexplored(merged, {"OTHILETH"}, by["OTHILETH"]["position"], known)
    assert [r["designation"] for r in rows] == ["ZALDANAL", "LORQELYR", "SOL"]     # nearest first; SOL never visited
    assert rows[0]["distance"] == pytest.approx(5.83, abs=0.01) and 280 < rows[0]["eta"] < 300
    opts = census.destination_systems(merged, {"OTHILETH"}, {"OTHILETH"}, "OTHILETH-1-L4")
    assert [(o["value"], o["group"]) for o in opts] == [("OTHILETH", "Your systems"), ("ZALDANAL", "Unexplored"),
                                                        ("LORQELYR", "Unexplored"), ("SOL", "Unexplored")]
    assert "census" in opts[1]["label"]


def test_census_on_arrival_runs_once_per_system(client):
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.worker.sync_devices)
    arrival = {"id": "7777777777777-0", "event": "travel.arrived", "category": "travel", "device_code": "11ADA230",
               "device_type": "heaven_vessel", "location": "SOL-BELT-1", "star": "SOL",
               "payload": {"destination": "SOL-BELT-1", "origin": "ABOTEIN-OORT", "travel_type": "surge"},
               "created_at": iso(datetime.now(timezone.utc))}
    count = lambda: client.portal.call(client.app.state.db.fetchone,  # noqa: E731
                                       "SELECT COUNT(*) n FROM actions WHERE body LIKE '%stellar_census%'")["n"]
    client.portal.call(client.app.state.worker.handle_event, arrival)
    assert count() == 1
    assert client.portal.call(client.app.state.db.kv_get, "census")["SOL"]["device"] == "11ADA230"
    cat = client.portal.call(client.app.state.db.kv_get, "stars")
    assert any(s["designation"] == "ZALDANAL" and s.get("from_census") for s in cat["stars"])
    arrival["id"] = "7777777777777-1"
    client.portal.call(client.app.state.worker.handle_event, arrival)
    assert count() == 1                                           # SOL has had its census
    # the catalog refresh keeps census stars
    client.portal.call(client.app.state.worker.sync_catalogue)
    assert any(s["designation"] == "ZALDANAL" for s in client.portal.call(client.app.state.db.kv_get, "stars")["stars"])
    page = client.get("/stars", headers=H).text
    assert "ZALDANAL" in page and "Census from SOL" in page and 'rules-panel?ids=census_on_arrival"' in page
    assert ">Stars</a>" in page                                   # Map › Stars sub-tab
    assert page.count('id="route-from"') == 1 and "route-from').value" in page   # one replicant dropdown for the list
    r = client.post("/stars/census", data={"device": "11ADA230"}, headers=HX)
    assert "unexplored: ZALDANAL" in r.text and count() == 2


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


def test_travel_destination_picker(client):
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(client.app.state.db.kv_set, "census_stars",
                       {"ZALDANAL": {"designation": "ZALDANAL", "explored": False, "position": {"x": 5, "y": 3, "z": 0}}})
    client.portal.call(client.app.state.worker.sync_catalogue)
    page = client.get("/replicants/77F75255", headers=H).text
    # one field: type anything; its list suggests your systems, then the nearest explored (unexplored only typed)
    assert 'name="destination__star"' in page and 'list="nearby-stars-sys"' in page and 'value="SOL">yours' in page
    assert 'value="ZALDANAL"' not in page
    assert "page 1" in page
    r = client.get("/print-queue/locations?dest_star=SOL", headers=H)   # the second list: spots in the system
    assert "anywhere in SOL" in r.text
    # the system alone, the spot, or the typed code
    for form, dest in (({"destination__star": "ZALDANAL"}, "ZALDANAL"),
                       ({"destination__star": "SOL", "destination": "SOL-BELT-1"}, "SOL-BELT-1"),
                       ({"destination__star": "SOL", "destination__custom": "sol-3-l4"}, "SOL-3-L4")):
        r = client.post("/replicants/77F75255/travel", data={**form, "dry_run": "1"}, headers=HX)
        assert dest in r.text, r.text[:300]
    assert "Pick a system" in client.post("/replicants/77F75255/travel", data={"dry_run": "1"}, headers=HX).text
    from rsweb import commands
    from starlette.datastructures import FormData
    travel = commands.COMMANDS["travel"]
    assert commands.parse_fields(travel, FormData({"f.destination__star": "ZALDANAL"})) == {"destination": "ZALDANAL"}
    assert commands.parse_fields(travel, FormData({"f.destination__star": "SOL", "f.destination": "SOL-3"})) == {"destination": "SOL-3"}
    # the device page's command box offers the same picker
    form = client.get("/devices/11ADA230/command-form?command=travel", headers=H).text
    assert 'name="f.destination__star"' in form and 'value="SOL">yours' in form


# --- devices in transit on the maps (1.20.0) ---------------------------------------------------------------------
def _moving(code, origin, dest, legs, start, end, dtype="survey_drone"):
    return {"device_code": code, "device_type": dtype, "status": "recalling", "location": None,
            "travel": {"origin": origin, "destination": dest, "final_destination": dest,
                       "departed_at": start.isoformat(), "arrives_at": end.isoformat(), "final_arrives_at": end.isoformat(),
                       "route": [{"from": f, "to": t, "type": ty, "time_seconds": s} for f, t, ty, s in legs]}}


def test_transit_trips_on_the_system_and_galaxy_maps():
    from rsweb import transit
    from rsweb.web import build_system_view
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(seconds=7060), now + timedelta(seconds=1423)   # 83 % of the live recall trip
    drones = [_moving(c, f"OTHDANAX-{o}", "OTHDANAX-OORT", [(f"OTHDANAX-{o}", "OTHDANAX-OORT", "cruise", 8483.6)], start, end)
              for c, o in (("A42C5AB6", "1-2"), ("C3BE3BEF", "1-2"))]
    ship = _moving("HV", "OTHDANAX-2", "FALQUORYX-1-L4",
                   [("OTHDANAX-2", "OTHDANAX-OORT", "cruise", 100), ("OTHDANAX-OORT", "FALQUORYX-1-L4", "surge", 800),
                    ("FALQUORYX-1-L4", "FALQUORYX-1-L4", "cruise", 100)], now - timedelta(seconds=500), now + timedelta(seconds=500),
                   "heaven_vessel")
    stale = _moving("OLD", "OTHDANAX-2", "OTHDANAX-3", [("OTHDANAX-2", "OTHDANAX-3", "cruise", 10)],
                    now - timedelta(hours=2), now - timedelta(hours=1))
    groups = transit.trips(drones + [ship, stale])
    assert [g["label"] for g in groups] == ["HV heaven vessel", "2× survey drone"]      # together; the old trip is over
    d = groups[1]
    assert d["progress"] == pytest.approx(0.832, abs=0.01) and 1400 < d["eta"] <= 1423
    leg = groups[0]["legs"][1]
    assert leg["type"] == "surge" and leg["t1"] - leg["t0"] == pytest.approx(800)
    pos = {"OTHDANAX": {"x": -456.1, "y": -220.2, "z": 1.6}, "FALQUORYX": {"x": -460.3, "y": -214.8, "z": 4.9}}
    gal = transit.galaxy_movers(groups, pos)
    assert len(gal) == 1 and gal[0]["origin"] == "OTHDANAX" and gal[0]["destination"] == "FALQUORYX-1-L4"
    assert [sg["a"] == sg["b"] for sg in gal[0]["segs"]] == [True, False, True]       # cruise legs stay at the star
    scan = {"planets": [{"designation": "OTHDANAX-1", "orbital_distance_au": .1}, {"designation": "OTHDANAX-2", "orbital_distance_au": .3}],
            "outer_system": {"oort": {"designation": "OTHDANAX-OORT", "distance_au": 1800}}}
    view = build_system_view("OTHDANAX", scan, [], [], [], None, groups, pos)
    ways = {m["label"]: m for m in view["movers"]}
    assert ways["2× survey drone"]["way"] == "local" and ways["2× survey drone"]["segs"][0][2:4] == [730, 30]   # to the Oort corner
    out = ways["HV heaven vessel"]
    assert out["way"] == "out" and out["other"] == "FALQUORYX" and len(out["segs"]) == 2   # cruise out, then surge to the rim
    rx, ry = out["segs"][1][2:4]
    assert rx < 380 and ry < 380            # FALQUORYX is to the -x, +y (map up) of OTHDANAX: up and to the left
    arriving = build_system_view("FALQUORYX", {}, [], [], [], None, groups, pos)["movers"][0]
    assert arriving["way"] == "in" and arriving["other"] == "OTHDANAX"


def test_moving_devices_show_on_both_maps(client):
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    client.post("/devices/11ADA230/command", data={"command": "travel", "f.destination": "ABOTEIN-3-L4"}, headers=HX)
    client.portal.call(client.app.state.worker.sync_devices)
    assert any(d.get("travel") for d in world.devices)
    m = client.get("/api/map.json", headers=H).json()
    assert m["moving"] and m["moving"][0]["destination"] == "ABOTEIN-3-L4" and m["moving"][0]["origin"] == "SOL"
    page = client.get("/systems/SOL", headers=H).text
    assert 'class="mover mover-out"' in page and "→ ABOTEIN-3-L4" in page
    assert 'id="opt-moving"' in client.get("/map", headers=H).text


def test_map_names_the_device_each_replicant_rides_in(client):
    # the wallpaper's follow setting tracks a replicant in transit by the ship hosting it
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    rep = client.get("/api/map.json?part=core", headers=H).json()["replicants"][0]["code"]
    next(d for d in world.devices if d["device_code"] == "11ADA230")["hosting_replicant"] = {"replicant_code": rep}
    client.portal.call(client.app.state.worker.sync_devices)
    reps = {r["code"]: r for r in client.get("/api/map.json?part=core", headers=H).json()["replicants"]}
    assert reps[rep]["host"] == "11ADA230"


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


def test_unstationed_fleets_stay_aboard_at_home(client):
    eng = client.app.state.worker.automations
    devices = [
        {"device_code": "CV", "device_type": "cargo_vessel", "location": "SOL-3-L4", "status": "idle", "features": ["surge"],
         "stow_capacity": 50, "attach_capacity": 3, "tags": ["fleet:miners"]},
        {"device_code": "MD", "device_type": "mining_drone", "location": None, "stowed_in_device_code": "CV", "status": "stowed",
         "tags": ["fleet:miners"]},
        {"device_code": "TH", "device_type": "transport_hauler", "location": "SOL-3-L4", "attached_to_device_code": "CV",
         "status": "attached", "cargo_used": 40, "tags": ["fleet:miners"]},
        {"device_code": "FR", "device_type": "cargo_freighter", "location": "SOL-3-L4", "status": "idle", "features": ["surge"],
         "cargo_used": 200, "tags": ["fleet:miners"]},
    ]
    fleet = {"id": "miners", "name": "Miners", "role": "mining", "home": "SOL", "wants": {}, "station": False}
    m = {"status": "running", "phase": "return", "log": []}
    steps, _ = client.portal.call(eng.fleet_phase_steps, fleet, m, "unload", devices)
    descs = [s["desc"] for s in steps]
    assert not any(d.startswith("deploy MD") for d in descs)                    # passengers stay aboard
    th = [d for d in descs if "TH" in d]
    assert th[0] == "CV: detach TH (to deposit)" and "TH: unload" in th and th[-1] == "CV: attach TH"   # back on board
    assert "FR: unload" in descs and "everyone stays aboard" in m["log"][-1]["text"]
    fleet["station"] = True                                                       # a stationed fleet works its home
    steps, _ = client.portal.call(eng.fleet_phase_steps, fleet, {"status": "running", "log": []}, "unload", devices)
    descs = [s["desc"] for s in steps]
    assert "deploy MD from CV" in descs and "CV: detach TH" in descs and "CV: attach TH" not in descs


def test_change_owner_sends_target():
    """Live 2026-10-06: 'target: Missing data for required field.; replicant_code: Unknown field.'"""
    from starlette.datastructures import FormData
    from rsweb import commands
    assert commands.parse_fields(commands.COMMANDS["change_owner"], FormData({"f.target": "D9351B81"})) == {"target": "D9351B81"}


def test_drone_indicators_on_the_maps(client):
    from rsweb.web import drone_summary
    devs = [{"device_code": "M1", "device_type": "mining_drone", "status": "mining (carbon)"},
            {"device_code": "M2", "device_type": "mining_drone", "status": "mining (carbon)"},
            {"device_code": "M3", "device_type": "mining_drone", "status": "idle"},
            {"device_code": "S1", "device_type": "survey_drone", "status": "searching"},
            {"device_code": "T1", "device_type": "transport_hauler", "status": "idle"},
            {"device_code": "AF", "device_type": "autofactory", "status": "idle"}]
    g = {x["kind"]: x for x in drone_summary(devs)}
    assert list(g) == ["mining", "survey", "transport"]
    assert (g["mining"]["n"], g["mining"]["working"], g["mining"]["idle"], g["mining"]["state"]) == (3, 2, 1, "working")
    assert g["survey"]["state"] == "working" and g["transport"]["state"] == "idle"
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get("/systems/SOL", headers=H).text
    assert 'class="drone drone-' in page and "mining drone(s) at SOL-BELT-1" in page and "nodrones" in page
    sol = next(s for s in client.get("/api/map.json", headers=H).json()["stars"] if s["designation"] == "SOL")
    assert any(d["kind"] == "mining" and d["n"] >= 1 for d in sol["drones"])


def test_working_drones_orbit_their_site(client):
    from rsweb.web import drone_orbits
    devs = [{"device_code": "M1", "device_type": "mining_drone", "status": "mining (carbon)"},
            {"device_code": "M2", "device_type": "mining_drone", "status": "mining (carbon)"},
            {"device_code": "M3", "device_type": "mining_drone", "status": "idle"},
            {"device_code": "S1", "device_type": "survey_drone", "status": "tracking"},
            {"device_code": "AF", "device_type": "autofactory", "status": "printing"}]
    o = {x["code"]: x for x in drone_orbits(devs, 10)}
    assert set(o) == {"M1", "M2", "S1"}   # idle drones and non-drones don't orbit
    assert o["M1"]["kind"] == "work" and o["S1"]["kind"] == "survey" and o["S1"]["r"] > o["M1"]["r"] > 10
    assert {o["M1"]["delay"], o["M2"]["delay"]} == {0, -4.5}   # spread evenly around the ring
    from rsweb.web import build_system_view
    belt = [{"device_code": "M1", "device_type": "mining_drone", "status": "mining (carbon)", "location": "SOL-BELT-1"},
            {"device_code": "M2", "device_type": "mining_drone", "status": "mining (carbon)", "location": "SOL-BELT-1"},
            {"device_code": "S1", "device_type": "survey_drone", "status": "tracking", "location": "SOL-BELT-1",
             "tracking_site": "SOL-BELT-1-SITE-2"}]
    scan = {"asteroid_belt": {"belts": [{"designation": "SOL-BELT-1", "inner_radius_au": 2.2, "outer_radius_au": 3.2}]}}
    places = [{"code": "SOL-BELT-1-SITE-2", "category": "site"}]
    view = build_system_view("SOL", scan, belt, [], places, drone_sites={"M1": "SOL-BELT-1-SITE-2", "M2": "SOL-BELT-9-SITE-1"})
    rings = {(g["x"], g["y"]): sorted(o["code"] for o in g["orbits"]) for g in view["orbits"]}
    site = next(pl for pl in view["places"] if pl["code"] == "SOL-BELT-1-SITE-2")
    loc = view["markers"][0]
    assert rings == {(site["x"], site["y"]): ["M1", "S1"], (loc["x"], loc["y"]): ["M2"]}   # M2's site isn't drawn here
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get("/systems/SOL", headers=H).text
    assert 'orbiter orbit-survey' in page and 'class="scan-beam"' in page

def test_map_core_loads_your_systems_first(client):
    client.portal.call(client.app.state.worker.sync_devices)
    whole = {s["designation"] for s in client.get("/api/map.json?part=core", headers=H).json()["stars"]}
    mine = {s["designation"] for s in client.get("/api/map.json?part=core&scope=mine", headers=H).json()["stars"]}
    rest = {s["designation"] for s in client.get("/api/map.json?part=core&scope=rest", headers=H).json()["stars"]}
    assert "SOL" in mine and mine | rest == whole and not mine & rest and rest


def test_snapshot_2026_10_06_fixes(client):
    """Live 2026-10-06 (19:54): a mission to LORALEL (typo), a fleet controller given a production gather order, and
    to:/at: tags naming a fleet."""
    from rsweb import loadouts as lo, production
    from rsweb.ami_schedule import set_stationed
    # 1. unknown mission targets are refused, with a suggestion
    client.portal.call(client.app.state.db.kv_set, "census_stars",
                       {"LORALAEL": {"designation": "LORALAEL", "explored": False, "position": {"x": 9, "y": 9, "z": 0}}})
    client.portal.call(client.app.state.worker.sync_catalogue)
    client.post("/fleets", data={"name": "Scouts", "role": "explore", "home": "SOL"}, headers=HX)
    r = client.post("/fleets/scouts/mission", data={"targets": "LORALEL"}, headers=HX)
    assert "Unknown system" in r.text and "did you mean LORALAEL" in r.text
    assert 'value="LORALAEL"' in client.get("/fleets/details?all=1", headers=H).text            # census stars are offered
    r = client.post("/fleets/scouts/mission", data={"targets": "LORALAEL"}, headers=HX)
    assert r.headers.get("HX-Refresh")
    # 2. the production planner skips controllers of fleets that aren't at their station
    ctrls = [{"device_code": "FC", "device_type": "ami_mining_controller", "location": "FAL-BELT-1", "status": "idle",
              "features": ["ami"], "tags": ["fleet:prospectors"]},
             {"device_code": "HC", "device_type": "ami_mining_controller", "location": "FAL-BELT-1", "status": "coordinating",
              "features": ["ami"], "tags": ["fleet:fal-home"]}]
    try:
        set_stationed({"fleet:fal-home": "FAL"})
        assert [c["device_code"] for c in production.controllers_in(ctrls, "FAL", "mining")] == ["HC"]
    finally:
        set_stationed({})
    # 3. to:/at: tags that aren't locations are ignored
    d = {"device_code": "CV", "tags": ["at:fleet:prospectors", "fleet:prospectors", "to:fleet:prospectors"]}
    assert lo.bound_for(d, {"FAL"}) is None and lo.pinned_at(d) is None
    assert lo.is_place("FALQUORYX-BELT-1") and not lo.is_place("fleet:prospectors")


def test_fleet_owner_hands_members_to_one_replicant(client):
    from rsweb import fleets as fl
    eng = client.app.state.worker.automations
    fleet = {"id": "miners", "name": "Miners", "home": "SOL", "wants": {}, "owner": "AAAA0001"}
    D = lambda code, owner, **kw: {"device_code": code, "device_type": "mining_drone", "location": "SOL-BELT-1",  # noqa: E731
                                   "status": "idle", "replicant_code": owner, "tags": ["fleet:miners"], **kw}
    devices = [D("M1", "AAAA0001"), D("M2", "BBBB0002"), D("M3", "BBBB0002"),
               D("HV", "BBBB0002", device_type="cargo_vessel", hosting_replicant={"name": "Sk3y-6", "replicant_code": "BBBB0002"}),
               {**D("X1", "BBBB0002"), "tags": []}]
    own = fl.ownership(fleet, devices)
    assert [d["device_code"] for d in own["move"]] == ["M2", "M3"] and [d["device_code"] for d in own["hosts"]] == ["HV"]
    st = fl.owner_steps(fleet, devices)
    assert [(s["path"], s["body"]) for s in st] == [("/devices/M2", {"command": "change_owner", "target": "AAAA0001"}),
                                                    ("/devices/M3", {"command": "change_owner", "target": "AAAA0001"})]
    assert fl.owner_steps({**fleet, "owner": ""}, devices) == []
    # through the page: pick the owner, keep it, and the engine hands members over once (not again within 15 min)
    client.post("/fleets", data={"name": "Miners", "home": "SOL"}, headers=HX)
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    client.portal.call(client.app.state.db.kv_set, "replicants", {"AAAA0001": {"name": "Sk3y-1"}, "BBBB0002": {"name": "Sk3y-6"}})
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Owner" in page and "owned by" in page
    client.post("/fleets/miners/owner", data={"owner": "AAAA0001", "keep_owner": "on"}, headers=HX)
    f = client.portal.call(eng.fleets)[0]
    assert f["owner"] == "AAAA0001" and f["keep_owner"] and "owner_move" not in client.portal.call(client.app.state.db.kv_get, "fleets")[0]
    assert "Left alone (they host a replicant): HV (Sk3y-6)" in client.get("/fleets/details?all=1", headers=H).text
    lines = client.portal.call(eng.fleet_owners)
    assert lines and "M2, M3 → owner AAAA0001" in lines[0]
    client.portal.call(client.app.state.db.kv_set, "fleet_owners_at", None)
    assert client.portal.call(eng.fleet_owners) == []          # just sent: not repeated
    r = client.post("/fleets/miners/owner", data={"owner": "NOPE"}, headers=HX)   # unknown replicant: cleared
    assert not client.portal.call(eng.fleets)[0]["owner"]


def test_mining_mission_salvages_where_there_is_no_belt(client):
    """Live 2026-10-06: KELMONENT has no belt; the made-up KELMONENT-1-BELT-1 was refused ('Invalid destination format')."""
    import json as _json
    from rsweb import fleets as fl
    assert fl.richest_belt("KELMONENT", {"planets": [], "asteroid_belt": None}) is None
    assert fl.richest_belt("X", {"asteroid_belt": {"belts": [{"designation": "X-BELT-1", "resources": {"iron": "low"}},
                                                             {"designation": "X-BELT-2", "resources": {"iron": "rich"}}]}}) == "X-BELT-2"
    eng = client.app.state.worker.automations
    db = client.app.state.db
    world = client.app.state.api.http._transport.app.state.world
    for ev in [e for e in world.events if e["event"] == "salvage.discovered"]:      # SOL-3-1-SAL-1, a derelict hauler
        client.portal.call(client.app.state.worker.handle_event, dict(ev))
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("SOL", _json.dumps({"planets": [{"designation": "SOL-3"}], "asteroid_belt": None}), "2026-10-06T00:00:00"))
    devices = [
        {"device_code": "MC", "device_type": "ami_mining_controller", "location": "SOL-3-L4", "status": "idle", "tags": ["fleet:p"]},
        {"device_code": "SC", "device_type": "ami_survey_controller", "location": "SOL-3-L4", "status": "idle", "tags": ["fleet:p"]},
        {"device_code": "MD", "device_type": "mining_drone", "location": "SOL-3-L4", "status": "idle", "tags": ["fleet:p"]},
        {"device_code": "SD", "device_type": "survey_drone", "location": "SOL-3-L4", "status": "idle", "tags": ["fleet:p"]},
    ]
    fleet = {"id": "p", "name": "Prospectors", "role": "mining", "home": "AEM", "wants": {}, "station": False}
    m = {"status": "running", "phase": "deploy", "target": "SOL", "targets": ["SOL"], "idx": 0, "opts": {}, "log": []}
    steps, problems = client.portal.call(eng.fleet_phase_steps, fleet, m, "work", devices)
    bodies = [s["body"] for s in steps if s["body"]]
    assert {"command": "travel", "destination": "SOL-3-1"} in bodies and not any("BELT" in str(b) for b in bodies)
    assert {"command": "set_directive", "directive": "gather_salvage", "configuration": {"location": "SOL-3-1", "recall": False}} in bodies
    assert not any(b.get("directive") in ("belt_search", "gather_evenly") for b in bodies)
    assert {"command": "adopt", "devices": ["MD"]} in bodies and not problems
    assert m["belt"] == "SOL-3-1" and m["salvage"] == "SOL-3-1-SAL-1" and "salvaging SOL-3-1-SAL-1" in m["log"][-1]["text"]
    # no belt and no salvage known: the mission stalls with a reason instead of watching forever
    client.portal.call(db.execute, "DELETE FROM events WHERE event='salvage.discovered'")
    m = {"status": "running", "phase": "deploy", "target": "SOL", "targets": ["SOL"], "idx": 0, "opts": {}, "log": []}
    steps, problems = client.portal.call(eng.fleet_phase_steps, fleet, m, "work", devices)
    assert not steps and "no asteroid belt and no salvage" in problems[0] and m["stall"]


def test_change_owner_already_owned_counts_as_done(client):
    """Live 2026-10-06: a second owner pass got 'Device already belongs to that replicant' and failed the job."""
    import time
    from rsweb.automations import step
    eng = client.app.state.worker.automations
    world = client.app.state.api.http._transport.app.state.world
    d = next(x for x in world.devices if "change_owner" in x["available_commands"])
    d["replicant_code"] = "AAAA0001"
    job = client.portal.call(eng.create_job, "fleets", "owners", None,
                             [step("owner", f"/devices/{d['device_code']}", {"command": "change_owner", "target": "AAAA0001"})],
                             {}, True)
    for _ in range(20):
        client.portal.call(eng.tick)
        j = next(x for x in client.portal.call(eng.jobs) if x["id"] == job["id"])
        if j["status"] not in ("running", "waiting"):
            break
        time.sleep(0.05)
    assert j["status"] == "done" and "already belongs" in j["steps"][0]["note"]


def test_new_home_brings_the_whole_working_group():
    """Live 2026-10-06: Miner 1's home moved ITHVALAI → KELMORNEA; its coordinating controllers, their adopted drones
    and the searching survey drones were all left behind."""
    from rsweb import loadouts as lo
    D = lambda code, t, loc, status="idle", **kw: {"device_code": code, "device_type": t, "location": loc, "status": status,  # noqa: E731
                                                  "tags": kw.pop("tags", ["fleet:m1"]), **kw}
    devices = [
        D("MC", "ami_mining_controller", "ITH-BELT-1", "coordinating", ami_directive={"name": "gather_evenly"},
          available_commands=["set_directive", "release", "travel", "stow"]),
        D("SC", "ami_survey_controller", "ITH-BELT-1", "coordinating", ami_directive={"name": "belt_search"},
          available_commands=["set_directive", "release", "travel", "stow"]),
        D("FC", "ami_transport_controller", "ITH-2-L4", "coordinating", tags=["ferry", "fleet:m1"],
          ami_directive={"name": "ferry", "config": {"collect": "ITH-BELT-1", "deliver": "FAL-BELT-1"}},
          available_commands=["set_directive", "release", "travel", "stow"]),
        D("MD1", "mining_drone", "ITH-BELT-1", controller_device_code="MC"),
        D("MD2", "mining_drone", "ITH-BELT-1", controller_device_code="MC"),
        D("SD1", "survey_drone", "ITH-BELT-1", "searching", controller_device_code="SC"),
        D("SD2", "survey_drone", "ITH-BELT-1", "tracking", controller_device_code="SC", tags=["fleet:m1", "to:kel"]),
        D("BUSY", "mining_drone", "ITH-BELT-1", "mining (carbon)"),           # can't cruise while mining: next pass
        D("CV", "cargo_vessel", "ITH-2-L4", features=["surge", "cruise", "stow"], stow_capacity=50, tags=[]),
        # a bigger carrier right there, but its fleet is on a mission: not the planner's to use
        D("PV", "cargo_vessel", "ITH-2-L4", features=["surge", "cruise", "stow"], stow_capacity=99, tags=["fleet:p"]),
        # the new home's own ferry: its freighter out delivering is not "left behind"
        D("FC2", "ami_transport_controller", "KEL-4-L4", "coordinating", tags=["ferry", "fleet:m1"],
          ami_directive={"name": "ferry", "config": {"collect": "KEL-BELT-1", "deliver": "FAL-BELT-1"}}),
        D("FR", "cargo_freighter", "FAL-BELT-1", controller_device_code="FC2", features=["surge"]),
    ]
    stars = {"ITH": {"position": {"x": 0, "y": 0, "z": 0}}, "KEL": {"position": {"x": 1, "y": 0, "z": 0}, "entry_point": "KEL-4-L4"},
             "FAL": {"position": {"x": 2, "y": 0, "z": 0}}}
    fleets = [{"id": "m1", "name": "Miner 1", "home": "KEL", "station": True, "wants": {}},
              {"id": "p", "name": "Prospectors", "home": "FAL", "station": False, "wants": {}, "mission": {"status": "running"}}]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert sorted(p["returning"]) == ["FC", "MC", "MD1", "MD2", "SC", "SD1"]
    dl = p["deliveries"][0]
    assert "SD2" in dl["devices"]                    # tagged to go home on an earlier pass: its site doesn't hold it
    assert dl["carrier"] == "CV" and dl["to"] == "KEL"
    steps = lo.delivery_steps(dl, p["by_code"], stars, True, {"MC": ["MD1", "MD2"], "SC": ["SD1"]})
    descs = [s["desc"] for s in steps]
    assert "MC: release 2 device(s) before leaving" in descs and "MC: clear directive before leaving" in descs
    assert "SC: clear directive before leaving" in descs and "FC: clear directive before leaving" in descs
    assert not any(d.startswith("MC: release MD") for d in descs)          # let go once, by the controller leaving with them


def test_deliveries_hand_devices_to_the_carriers_owner():
    """Live 2026-10-06: other replicants' carriers failed to attach Miner 1's drones ('Target device belongs to a
    different account'). Since 1.26.0 any carrier will do: the device is handed to the carrier's owner as it boards
    (its fleet's keep-owner setting takes it back). A device hosting a replicant only rides its own replicant's carrier."""
    from rsweb import loadouts as lo
    from rsweb.modular import prepare_moves
    D = lambda code, t, loc, owner, **kw: {"device_code": code, "device_type": t, "location": loc, "status": "idle",  # noqa: E731
                                          "replicant_code": owner, **kw}
    devices = [
        D("MD1", "mining_drone", "ITH-BELT-1", "R1", tags=["fleet:m1"]),
        D("PLATE", "surge_plate", "ITH-BELT-1", "R2", features=["surge", "attach"], attach_capacity=4, tags=[]),
        D("MINE", "mobile_fleet", "KEL-4-L4", "R1", features=["surge", "stow"], stow_capacity=36, tags=["fleet:m1"]),
    ]
    stars = {"ITH": {"position": {"x": 0, "y": 0, "z": 0}}, "KEL": {"position": {"x": 1, "y": 0, "z": 0}, "entry_point": "KEL-4-L4"}}
    fleets = [{"id": "m1", "name": "Miner 1", "home": "KEL", "station": True, "wants": {}}]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert [(dl["carrier"], dl["devices"]) for dl in p["deliveries"]] == [("PLATE", ["MD1"])]   # the one already there
    steps = prepare_moves(lo.delivery_steps(p["deliveries"][0], p["by_code"], stars, True), devices)
    descs = [s["desc"] for s in steps]
    assert descs.index("MD1: hand to R2 (owner of carrier PLATE)") + 1 == descs.index("PLATE: attach MD1")
    # same owner available in the system too: that one is preferred
    devices.append(D("OWN", "surge_plate", "ITH-BELT-1", "R1", features=["surge", "attach"], attach_capacity=1, tags=[]))
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert [dl["carrier"] for dl in p["deliveries"]] == ["OWN"]
    assert not any("hand to" in s["desc"] for s in prepare_moves(lo.delivery_steps(p["deliveries"][0], p["by_code"], stars, True), devices))
    # a vessel hosting a replicant is never handed over: it waits for its own replicant's carrier
    devices = [D("HV", "heaven_vessel", "ITH-1-L4", "R1", tags=["fleet:m1"], hosting_replicant={"code": "R1"}, features=["stow"]),
               D("PLATE", "surge_plate", "ITH-1-L4", "R2", features=["surge", "attach"], attach_capacity=4, tags=[])]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert not p["deliveries"]

def test_mission_carrier_unloads_inside_the_system_not_at_the_kuiper_belt():
    """Live 2026-10-06: the Surveyors' carrier arrived at LORQELYR-KUIPER (the entry point) and unloaded there."""
    from rsweb import fleets as fl, placement as pl
    devices = [
        {"device_code": "HV", "device_type": "heaven_vessel", "location": "LOR-KUIPER", "status": "idle", "features": ["surge"],
         "stow_capacity": 10, "tags": ["fleet:s"]},
        {"device_code": "SC", "device_type": "ami_survey_controller", "location": None, "stowed_in_device_code": "HV",
         "status": "stowed", "tags": ["fleet:s"]},
    ]
    fleet = {"id": "s", "name": "Surveyors", "role": "explore", "home": "FAL", "wants": {}}
    geo = pl.geography("LOR", devices, {"planets": [{"designation": "LOR-2", "orbital_distance_au": 2},
                                                    {"designation": "LOR-1", "orbital_distance_au": 1}]}, "LOR-KUIPER")
    assert fl.deploy_spot(geo) == "LOR-1-L4"
    descs = [s["desc"] for s in fl.unload_steps(fleet, devices, fl.deploy_spot(geo))]
    assert descs == ["HV → LOR-1-L4 (to unload inside the system)", "wait for HV at LOR-1-L4", "deploy SC from HV"]
    assert fl.deploy_spot(pl.geography("LOR", [], None, "LOR-KUIPER")) is None     # nothing known: unload where it is


def test_prints_spread_over_factories_across_passes():
    """Live 2026-10-06: three prints of one item all landed on one of three idle autofactories — one print a pass, and
    each pass every factory still looked idle."""
    from rsweb import loadouts as lo
    devices = [{"device_code": c, "device_type": "autofactory", "location": "FAL-BELT-1", "status": "idle", "print_queue": [],
                "available_commands": ["enqueue_print"], "tags": []} for c in ("AF1", "AF2", "AF3")]
    fleets = [{"id": "fal", "name": "Fal", "home": "FAL", "station": True, "wants": {"autofactory": 3, "maintenance_drone": 1}}]
    bps = [{"device_type": "maintenance_drone", "resources": {"structural": 50}, "print_time": 600}]
    inv = {"FAL-BELT-1": {"structural": 1000}}
    orders = []
    for _ in range(3):
        p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, bps, inv, {}, {}, set(), orders, {})
        (pr,) = p["prints"]
        orders.append({"star": "FAL", "fleet": "fal", "device_type": "maintenance_drone", "factory": pr["factory"]})
        fleets[0]["wants"]["maintenance_drone"] += 1          # one more wanted each pass, as when stock allows one at a time
    assert sorted(o["factory"] for o in orders) == ["AF1", "AF2", "AF3"]


def test_each_fleet_prints_on_its_own_autofactory():
    """Live 2026-10-06: three fleets with an autofactory each (all at FALQUORYX-BELT-1); an item added to all three
    loadouts was printed three times on one factory."""
    from rsweb import loadouts as lo
    devices = [{"device_code": c, "device_type": "autofactory", "location": "FAL-BELT-1", "status": "idle", "print_queue": [],
                "available_commands": ["enqueue_print"], "tags": [f"fleet:{fid}"]}
               for c, fid in (("AF1", "hub"), ("AF2", "m1"), ("AF3", "m2"))]
    devices.append({"device_code": "AF0", "device_type": "autofactory", "location": "FAL-BELT-1", "status": "idle",
                    "print_queue": [], "available_commands": ["enqueue_print"], "tags": []})
    fleets = [{"id": fid, "name": fid, "home": home, "station": True, "wants": {"maintenance_drone": 1}}
              for fid, home in (("hub", "FAL"), ("m1", "KEL"), ("m2", "LOR"))]
    fleets.append({"id": "m3", "name": "m3", "home": "LOR", "station": True, "wants": {"maintenance_drone": 1}})
    bps = [{"device_type": "maintenance_drone", "resources": {"structural": 50}, "print_time": 600}]
    stars = {s: {"position": {"x": i, "y": 0, "z": 0}} for i, s in enumerate(("FAL", "KEL", "LOR"))}
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, bps, {"FAL-BELT-1": {"structural": 1000}},
                stars, {}, set(), [], {})
    got = {pr["fleet"]: pr["factory"] for pr in p["prints"]}
    assert got == {"hub": "AF1", "m1": "AF2", "m2": "AF3", "m3": "AF0"}     # m3 has none: the fleetless one, not another's
    # no stock: a fleet's own factory still gets it, and it waits for materials there
    p = lo.plan({"phases": [], "fleets": fleets[:1], "fleets_migrated": True}, devices, bps, {}, stars, {}, set(), [], {})
    assert [(pr["factory"], pr["note"]) for pr in p["prints"]] == [("AF1", "the fleet's own autofactory; waits for materials")]
    # a fleet's factory pinned at the stockpile (at: tag) stays there instead of being sent to the fleet's home
    away = {"device_code": "AF2", "device_type": "autofactory", "location": "FAL-BELT-1", "status": "idle",
            "available_commands": ["enqueue_print"], "tags": ["fleet:m1"]}
    p = lo.plan({"phases": [], "fleets": fleets[1:2], "fleets_migrated": True}, [away], [], {}, stars, {}, set(), [], {})
    assert p["returning"] == ["AF2"]
    away["tags"].append("at:FAL-BELT-1")
    p = lo.plan({"phases": [], "fleets": fleets[1:2], "fleets_migrated": True}, [away], [], {}, stars, {}, set(), [], {})
    assert p["returning"] == []


def test_print_orders_removed_from_a_queue_stop_counting_as_incoming(client):
    """Live 2026-10-07: prints taken off a queue by hand kept counting as incoming for 48 h, so the fleet looked complete
    and nothing could be queued to fill its loadout."""
    from rsweb import loadouts as lo
    from datetime import datetime, timezone
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc).timestamp()
    O = lambda t, f, at="2026-10-07T11:00:00+00:00", **kw: {"star": "FAL", "fleet": "hub", "device_type": t, "factory": f, "at": at, **kw}  # noqa: E731
    orders = [O("maintenance_drone", "AF1"), O("maintenance_drone", "AF1"), O("maintenance_drone", "AF1"),
              O("survey_drone", "AF2"), O("survey_drone", "AF2", at="2026-10-07T11:58:00+00:00"),   # fresh: kept
              O("mining_drone", "AF3", device_code="?", printed_at="2026-10-07T11:00:00+00:00"),  # printed, code never came
              O("mining_drone", "GONE")]                                                            # factory not listed: kept
    devices = [{"device_code": "AF1", "device_type": "autofactory", "status": "printing (maintenance_drone)", "print_queue": []},
               {"device_code": "AF2", "device_type": "autofactory", "status": "idle", "print_queue": []},
               {"device_code": "AF3", "device_type": "autofactory", "status": "idle", "print_queue": []}]
    kept = lo.reconcile_orders(orders, devices, now)
    assert [(o["device_type"], o["factory"]) for o in kept] == [("maintenance_drone", "AF1"), ("survey_drone", "AF2"),
                                                                ("mining_drone", "GONE")]
    devices[0]["print_queue"] = [{"device_type": "maintenance_drone", "quantity": 2}]
    assert len([o for o in lo.reconcile_orders(orders, devices, now) if o["factory"] == "AF1"]) == 3
    assert lo.forget_queued(orders, "AF1", "maintenance_drone", 1) == orders[:2] + orders[3:]
    assert [o["factory"] for o in lo.forget_queued(orders, "AF1")] == ["AF2", "AF2", "AF3", "GONE"]
    # the fleet card lists them, and Forget these drops only that fleet's
    db = client.app.state.db
    client.portal.call(db.kv_set, "loadout_orders", orders + [{**O("survey_drone", "AF2"), "fleet": "other"}])
    client.post("/loadouts/orders/clear", data={"fleet": "hub"}, headers=HX)
    assert [o["fleet"] for o in client.portal.call(db.kv_get, "loadout_orders")] == ["other"]


def test_removing_from_a_queue_forgets_its_order(client):
    world = client.app.state.api.http._transport.app.state.world
    world.queues["AF00BEEF"] = [{"device_type": "survey_drone"}, {"device_type": "ftl_beacon"}]
    world.devices[7]["status"] = "printing (mining_drone)"
    db = client.app.state.db
    O = lambda t: {"star": "FAL", "fleet": "hub", "device_type": t, "factory": "AF00BEEF", "at": "2026-10-07T11:00:00+00:00"}  # noqa: E731
    client.portal.call(db.kv_set, "loadout_orders", [O("survey_drone"), O("ftl_beacon"), O("ftl_beacon"),
                                                     {**O("ftl_beacon"), "factory": "OTHER"}])
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "remove", "index": "1"}, headers=HX)   # the ftl beacon
    assert "result ok" in r.text
    left = client.portal.call(db.kv_get, "loadout_orders")
    assert [(o["device_type"], o["factory"]) for o in left] == [("survey_drone", "AF00BEEF"), ("ftl_beacon", "AF00BEEF"),
                                                                ("ftl_beacon", "OTHER")]
    client.post("/devices/AF00BEEF/print-queue", data={"action": "clear"}, headers=HX)
    assert [o["factory"] for o in client.portal.call(db.kv_get, "loadout_orders")] == ["OTHER"]
    # the fleet card lists a stationed fleet's queued prints with a Forget button
    eng = client.app.state.worker.automations
    client.portal.call(eng.save_fleets, [{"id": "hub", "name": "Hub", "role": "mining", "home": "SOL", "station": True,
                                          "wants": {"ftl_beacon": 3}, "materials": "", "mission": None}])
    client.portal.call(db.kv_set, "loadout_orders", [{**O("ftl_beacon"), "at": "2099-01-01T00:00:00+00:00"}])
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Queued prints counted as incoming" in page and "1 queued print(s)" in page and "Forget these" in page


def test_fleets_without_a_factory_spread_over_the_hubs_factories():
    """Live 2026-10-07: all three autofactories belong to Printing Hub 1; Miner 1's and Miner 2's prints both went on
    3E95BD59 while 9E13498F sat idle."""
    from rsweb import loadouts as lo
    devices = [{"device_code": c, "device_type": "autofactory", "location": "FAL-BELT-1", "print_queue": [],
                "status": st, "available_commands": ["enqueue_print"], "tags": ["fleet:hub"],
                **({"printing": {"device_type": "galactic_observatory"}} if st != "idle" else {})}
               for c, st in (("AF1", "idle"), ("AF2", "idle"), ("AF3", "printing (galactic_observatory)"))]
    fleets = [{"id": "hub", "name": "Hub", "home": "FAL", "station": True, "wants": {"autofactory": 3, "galactic_observatory": 1}},
              {"id": "m1", "name": "Miner 1", "home": "KEL", "station": True, "wants": {"galactic_observatory": 1}},
              {"id": "m2", "name": "Miner 2", "home": "LOR", "station": True, "wants": {"galactic_observatory": 1}}]
    bps = [{"device_type": "galactic_observatory", "resources": {"structural": 50}, "print_time": 28800}]
    stars = {s: {"position": {"x": i, "y": 0, "z": 0}} for i, s in enumerate(("FAL", "KEL", "LOR"))}
    orders = [{"star": "FAL", "fleet": "hub", "device_type": "galactic_observatory", "factory": "AF3"}]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, bps, {"FAL-BELT-1": {"structural": 1000}},
                stars, {}, set(), orders, {})
    assert sorted((pr["fleet"], pr["factory"]) for pr in p["prints"]) == [("m1", "AF1"), ("m2", "AF2")]


def _survey_world():
    devices = [
        {"device_code": "HV", "device_type": "heaven_vessel", "location": "LOR-1-L4", "status": "idle", "features": ["surge"],
         "stow_capacity": 10, "tags": ["fleet:s"]},
        {"device_code": "SC", "device_type": "ami_survey_controller", "location": None, "stowed_in_device_code": "HV",
         "status": "stowed", "tags": ["fleet:s"]},
        {"device_code": "R1", "device_type": "ftl_relay", "location": None, "stowed_in_device_code": "HV", "status": "stowed", "tags": []},
        {"device_code": "B1", "device_type": "ftl_beacon", "location": None, "stowed_in_device_code": "HV", "status": "stowed", "tags": []},
        {"device_code": "R0", "device_type": "ftl_relay", "location": "FAL-1-L4", "status": "relaying", "tags": []},
        {"device_code": "B0", "device_type": "ftl_beacon", "location": "FAL-5", "status": "monitoring", "tags": []},
    ]
    fleet = {"id": "s", "name": "Surveyors", "role": "explore", "home": "FAL", "wants": {}}
    return fleet, devices


def test_explore_mission_leaves_no_relays_or_beacons(client):
    """Survey crews no longer drop relays or beacons (2026-10-11): the Systems page asks for one instead."""
    eng = client.app.state.worker.automations
    fleet, devices = _survey_world()
    m = {"status": "running", "phase": "travel", "idx": 0, "targets": ["LOR"], "opts": {}, "log": []}
    steps, _ = client.portal.call(eng.fleet_phase_steps, fleet, m, "deploy", devices)
    descs = [s["desc"] for s in steps]
    assert "deploy SC from HV" in descs and not any("relay" in d or "beacon" in d for d in descs)


def test_deal_pickup_plan_uses_the_nearest_stockpiles():
    from rsweb import fleets as fl
    fleet = {"id": "t", "name": "Traders", "role": "trade", "home": "FAL", "wants": {}}
    devices = [{"device_code": c, "device_type": "cargo_freighter", "location": "FAL-1-L4", "features": ["surge"],
                "cargo_capacity": 300, "cargo_used": 0, "tags": ["fleet:t"]} for c in ("F1", "F2")]
    inv = {"FAL-BELT-1": {"carbon": 1000, "silicates": 50}, "KEL-3": {"silicates": 500}, "SITE-2": {"carbon": 100},
           "FAR-BELT-1": {"silicates": 900}}
    pos = {"FAL": 0, "KEL": 1, "SITE": 2, "FAR": 50}
    need = fl.site_short({"carbon": 450, "silicates": 200}, inv["SITE-2"])
    assert need == {"carbon": 350, "silicates": 200}
    legs, left, problems = fl.pickup_plan(fleet, devices, need, inv, "SITE-2", lambda a, b: abs(pos[a] - pos[b]))
    # KEL is nearest the site: its silicates first; then FAL's carbon; FAR is never needed
    assert [(x["freighter"], x["pile"], x["take"]) for x in legs] == [
        ("F1", "KEL-3", {"silicates": 200}), ("F1", "FAL-BELT-1", {"carbon": 100}), ("F2", "FAL-BELT-1", {"carbon": 250})]
    assert not left and not problems
    steps = fl.pickup_steps(legs, devices)
    assert [s["desc"] for s in steps][:3] == ["F1 → KEL-3 (pick up)", "F1: load 200 silicates at KEL-3", "F1 → FAL-BELT-1 (pick up)"]
    # not enough anywhere: says what's missing
    legs, left, problems = fl.pickup_plan(fleet, devices, {"rares": 10}, inv, "SITE-2", lambda a, b: 0)
    assert not legs and left == {"rares": 10} and "missing" in problems[0]


def test_contract_fleet_delivers_waits_fulfils_and_brings_the_rewards(client):
    from rsweb import fleets as fl
    eng = client.app.state.worker.automations
    db = client.app.state.db
    devices = [{"device_code": "F1", "device_type": "cargo_freighter", "location": "SOL-BELT-1", "features": ["surge"],
                "cargo_capacity": 500, "cargo_used": 0, "tags": ["fleet:t"], "status": "idle"},
               {"device_code": "HV", "device_type": "heaven_vessel", "location": "SOL-3-L4", "features": ["surge"],
                "stow_capacity": 10, "tags": ["fleet:t"], "status": "idle"}]
    fleet = {"id": "t", "name": "Traders", "role": "trade", "home": "SOL", "wants": {}, "station": False, "materials": ""}
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-BELT-1", "items": [{"resource_type": "carbon", "quantity": 900}]}])
    client.portal.call(db.kv_set, "replicants", {"R1": {"name": "Joe", "hosted_device_code": "HV", "location": "SOL-3-L4"}})
    m = {"status": "running", "phase": None, "idx": 0, "targets": ["SOL"], "opts": {}, "log": [],
         "contract": {"designation": "EV-1", "location": "SOL-4", "title": "Help them", "price": {"carbon": 200},
                      "rewards": {"rares": 40}}}
    steps, problems = client.portal.call(eng.fleet_phase_steps, fleet, m, "load", devices)
    assert [s["desc"] for s in steps] == ["F1: load 200 carbon at SOL-BELT-1"] and m["loaded"] == ["F1"] and not problems
    steps, _ = client.portal.call(eng.fleet_phase_steps, fleet, m, "deliver", devices)
    assert [s["desc"] for s in steps] == ["F1 → SOL-4", "F1: deposit at SOL-4", "HV → SOL-4 (brings its replicant)"]
    steps, _ = client.portal.call(eng.fleet_phase_steps, fleet, m, "trade", devices)
    assert steps[0]["path"] == "/locations/SOL-4/events/EV-1" and steps[0]["method"] == "POST"
    devices[0]["location"] = "SOL-4"
    steps, _ = client.portal.call(eng.fleet_phase_steps, fleet, m, "collect", devices)
    assert steps[0]["body"] == {"command": "collect_resources", "resources": {"rares": 40}} and m["drop_star"] == "SOL"
    # a trade executes on the trader's controller
    t = {"status": "running", "log": [], "trade": {"controller": "TC1", "trade_code": "TRD-1", "location": "KEL-2", "price": {}}}
    steps, _ = client.portal.call(eng.fleet_phase_steps, fleet, t, "trade", devices)
    assert steps[0]["path"] == "/devices/TC1/trades/TRD-1"
    # the wait phase holds until the whole price is at the site and the fleet's own replicant (Joe rides on HV) is there
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-BELT-1", "items": [{"resource_type": "carbon", "quantity": 700}]},
                                                {"location": "SOL-4", "items": [{"resource_type": "carbon", "quantity": 200}]}])
    client.portal.call(db.kv_set, "replicants", {"R1": {"name": "Joe", "hosted_device_code": "HV", "location": "SOL-3-L4"}})
    m.update({"phase": "wait", "job": None, "status": "running"})
    client.portal.call(eng.save_fleets, [{**fleet, "mission": m}])
    client.portal.call(eng.run_fleets)
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "t")
    assert f["mission"]["phase"] == "wait" and "waiting for Joe (the fleet's replicant)" in f["mission"]["watch_note"]
    client.portal.call(db.kv_set, "replicants", {"R1": {"name": "Joe", "hosted_device_code": "HV", "location": "SOL-4"}})
    client.portal.call(eng.run_fleets)
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "t")
    assert f["mission"]["phase"] == "trade" and any("Joe at SOL-4: fulfilling" in x["text"] for x in f["mission"]["log"])
    # the Fleets page offers open contracts to a trade fleet, and starting one targets its system
    disc = {"designation": "SOL-3-L4-EVT-009", "location": "SOL-3-L4", "title": "Atmospheric Harvest", "tier": 1,
            "criteria": [{"name": "default", "resources": {"carbon": 150}, "devices": []}], "rewards": {"resources": {"volatiles": 200}}}
    client.portal.call(client.app.state.worker.handle_event, {"id": "4444444444499-0", "event": "event.discovered",
                       "category": "event", "location": "SOL-3-L4", "star": "SOL", "payload": disc,
                       "created_at": "2026-10-07T10:00:00+00:00"})
    client.portal.call(eng.save_fleets, [{**fleet, "mission": None}])
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Atmospheric Harvest @ SOL-3-L4" in page and 'name="contract"' in page
    import json as _json
    client.post("/fleets/t/mission", data={"contract": _json.dumps({"designation": "SOL-3-L4-EVT-009", "location": "SOL-3-L4",
                                                                    "title": "x", "price": {"carbon": 150}, "rewards": {}})},
                headers=HX)
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "t")
    assert f["mission"]["targets"] == ["SOL"] and f["mission"]["contract"]["designation"] == "SOL-3-L4-EVT-009"


def test_modular_devices_compact_before_moving_and_unfurl_after():
    """Autofactories and galactic observatories (feature `modular`) must be compacted before they move."""
    from rsweb import loadouts as lo
    from rsweb.modular import with_compaction
    af = {"device_code": "AF1", "device_type": "autofactory", "location": "FAL-BELT-1", "status": "idle",
          "features": ["cruise", "modular", "print"], "available_commands": ["compact", "unfurl", "travel"], "tags": []}
    plate = {"device_code": "MF", "device_type": "mobile_fleet", "location": "FAL-1-L4", "status": "idle",
             "features": ["surge", "attach"], "attach_capacity": 36}
    md = {"device_code": "MD1", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "idle", "features": ["cruise", "stow"]}
    by = {d["device_code"]: d for d in (af, plate, md)}
    dl = {"carrier": "MF", "carrier_loc": "FAL-1-L4", "from": "FAL", "to": "KEL", "devices": ["AF1", "MD1"], "mode": "attach"}
    steps = lo.delivery_steps(dl, by, {"KEL": {"entry_point": "KEL-4-L4"}}, False)
    out = with_compaction(steps, [af, plate, md], {"autofactory": {"print_time": 36000}})
    descs = [s["desc"] for s in out]
    ci, ti = descs.index("AF1: compact before moving"), descs.index("AF1 → FAL-1-L4 (to board MF)")
    assert ci < ti and out[ci]["wait"] == ["device.compacted"] and out[ci]["timeout"] >= 36000 * 0.3
    assert descs.index("MF: detach AF1 in KEL") + 1 == descs.index("AF1: unfurl")
    assert not any(d.startswith("MD1: compact") for d in descs)              # not modular: unchanged
    # WAIT steps still point at the right travel step after the insertions
    for s in out:
        if "seq0_from" in s:
            assert out[s["seq0_from"]]["body"]["command"] == "travel"
            assert out[s["seq0_from"]]["path"].split("/")[2] == s["wait_device"]
    # already compacted (e.g. printed compacted): no compact, still unfurls on landing
    af["status"] = "compacted"
    out = with_compaction(steps, [af, plate, md], {})
    assert not any("compact before" in s["desc"] for s in out) and any(s["desc"] == "AF1: unfurl" for s in out)
    # flying itself inside the system: compact, travel, unfurl on arrival
    af["status"] = "idle"
    st = lo.step("AF1 → FAL-2", "/devices/AF1", {"command": "travel", "destination": "FAL-2"}, wait=["travel.arrived"])
    assert [s["desc"] for s in with_compaction([st], [af], {})] == ["AF1: compact before moving", "AF1 → FAL-2", "AF1: unfurl"]


def test_tracking_drones_deactivate_right_before_they_move(client):
    """Live 2026-10-07: 'Cannot cruise while tracking a site - deactivate first' on every pass moving Miner 1 and 2."""
    from rsweb import loadouts as lo
    from rsweb.modular import prepare_moves
    sd = {"device_code": "SD1", "device_type": "survey_drone", "location": "KEL-BELT-1", "status": "tracking",
          "features": ["cruise", "survey", "stow"], "available_commands": ["deactivate", "stow", "travel"]}
    md = {"device_code": "MD1", "device_type": "mining_drone", "location": "KEL-BELT-1", "status": "idle", "features": ["stow"]}
    mf = {"device_code": "MF", "device_type": "mobile_fleet", "location": "KEL-4-L4", "status": "idle", "features": ["surge"],
          "attach_capacity": 36}
    by = {d["device_code"]: d for d in (sd, md, mf)}
    dl = {"carrier": "MF", "carrier_loc": "KEL-4-L4", "from": "KEL", "to": "LAR", "devices": ["MD1", "SD1"], "mode": "attach"}
    out = prepare_moves(lo.delivery_steps(dl, by, {"LAR": {"entry_point": "LAR-1-L4"}}, False), [sd, md, mf], {})
    descs = [s["desc"] for s in out]
    d_i = descs.index("SD1: stop tracking its site (deactivate) to move")
    assert d_i + 1 == descs.index("SD1 → KEL-4-L4 (to board MF)")
    assert descs.index("MF: detach SD1 in LAR") + 1 == descs.index("SD1: activate")
    assert not any(d.startswith("MD1: stop tracking") for d in descs)
    # command descriptions: on the form and as the picker's tooltips
    from rsweb.commands import COMMANDS, DESCRIPTIONS
    assert set(COMMANDS) <= set(DESCRIPTIONS)
    client.portal.call(client.app.state.worker.sync_devices)
    r = client.get("/devices/AF00BEEF/command-form?command=compact", headers=HX)
    assert "Fold a large (modular) device" in r.text
    assert 'title="Fold a large (modular) device' in client.get("/devices/AF00BEEF", headers=H).text


def test_fleet_carrier_fetches_before_going_home_and_old_home_tags_follow_the_fleet():
    """Live 2026-10-07: Miner 1's only carrier was sent home every pass (it was away fetching an observatory), so the 31
    devices waiting in KELMORNEA were never collected; the observatory was still tagged to:kelmornea after the move."""
    from rsweb import loadouts as lo
    D = lambda code, t, loc, **kw: {"device_code": code, "device_type": t, "location": loc, "status": "idle",  # noqa: E731
                                    "replicant_code": "R1", **kw}
    devices = [D("MF", "mobile_fleet", "FAL-1-L4", features=["surge", "attach"], attach_capacity=36, tags=["fleet:m1"]),
               D("MD1", "mining_drone", "KEL-BELT-1", tags=["fleet:m1"]),
               D("MD2", "mining_drone", "KEL-BELT-1", tags=["fleet:m1"]),
               D("OBS", "galactic_observatory", "FAL-1-L4", features=["cruise", "modular"], tags=["fleet:m1", "to:kel"])]
    stars = {s: {"position": {"x": i, "y": 0, "z": 0}, "entry_point": f"{s}-1-L4"} for i, s in enumerate(("FAL", "KEL", "LAR"))}
    fleets = [{"id": "m1", "name": "Miner 1", "home": "LAR", "station": True, "wants": {}}]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert [(dl["carrier"], dl["from"], sorted(dl["devices"]), dl.get("stay")) for dl in p["deliveries"]] == \
        [("MF", "KEL", ["MD1", "MD2"], True)]                       # the biggest batch first, and it stays home after
    assert "MF" not in p["returning"] and p["tag_add"]["OBS"] == ["to:lar"] and p["tag_remove"]["OBS"] == ["to:kel"]
    steps = lo.delivery_steps(p["deliveries"][0], p["by_code"], stars, True)
    assert "return" not in steps[-1]["desc"]
    # nothing to carry: the carrier goes home on its own
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices[:1], [], {}, stars, {}, set(), [], {})
    assert p["self_moves"] == [("MF", "LAR")] and p["returning"] == ["MF"]


def test_large_devices_compact_on_their_own_before_a_carrier_comes(client):
    """Observatories take over 2 h to compact: that starts as soon as the move is planned, without a carrier waiting."""
    from rsweb import loadouts as lo
    obs = {"device_code": "OBS", "device_type": "galactic_observatory", "location": "FAL-1-L4", "status": "idle",
           "features": ["cruise", "modular"], "available_commands": ["compact", "unfurl", "travel"], "tags": ["fleet:m1"]}
    mf = {"device_code": "MF", "device_type": "mobile_fleet", "location": "FAL-1-L4", "status": "idle",
          "features": ["surge", "attach"], "attach_capacity": 36, "tags": ["fleet:m1"]}
    stars = {s: {"position": {"x": i, "y": 0, "z": 0}, "entry_point": f"{s}-1-L4"} for i, s in enumerate(("FAL", "LAR"))}
    fleets = [{"id": "m1", "name": "Miner 1", "home": "LAR", "station": True, "wants": {}}]
    cfg = {"phases": [], "fleets": fleets, "fleets_migrated": True}
    p = lo.plan(cfg, [obs, mf], [], {}, stars, {}, set(), [], {})
    assert p["compact"] == [("OBS", "LAR")] and not p["deliveries"]
    assert any("OBS is being compacted" in u["why"] for u in p["unmet"])
    obs["status"] = "compacting"                                                   # under way: no second job, no carrier
    p = lo.plan(cfg, [obs, mf], [], {}, stars, {}, set(), [], {})
    assert p["compact"] == [] and not p["deliveries"]
    obs["status"] = "compacted"                                                    # done: now the carrier comes
    p = lo.plan(cfg, [obs, mf], [], {}, stars, {}, set(), [], {})
    assert [(dl["carrier"], dl["devices"]) for dl in p["deliveries"]] == [("MF", ["OBS"])]
    # the engine's compaction job: one compact step, waiting long enough for an observatory (≈30 % of 8 h + 30 min)
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.db.kv_set, "blueprints", [{"device_type": "galactic_observatory", "print_time": 28800}])
    obs["status"] = "idle"
    assert client.portal.call(eng.start_compactions, {"compact": [("OBS", "LAR")], "by_code": {"OBS": obs}}) == 1
    job = next(j for j in client.portal.call(eng.jobs) if "compact OBS" in j["title"])
    assert [s["body"] for s in job["steps"]] == [{"command": "compact"}] and job["steps"][0]["timeout"] >= 28800 * 0.3 + 1800
    assert "≈2.4 h" in job["title"]
    # prints bound for another system come out flat-packed
    st = lo.print_steps({"factory": "AF", "factory_star": "FAL", "device_type": "galactic_observatory", "n": 1, "star": "LAR",
                         "fleet": "m1"})[0]
    assert st["body"]["flatpack"] is True and "flat-packed" in st["desc"]
    assert "flatpack" not in lo.print_steps({"factory": "AF", "factory_star": "FAL", "device_type": "galactic_observatory",
                                             "n": 1, "star": "FAL"})[0]["body"]


def test_mission_to_a_system_without_a_relay_warns_without_a_replicant_aboard(client):
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(eng.save_fleets, [{"id": "p", "name": "Prospectors", "role": "mining", "home": "SOL", "wants": {},
                                          "station": False, "materials": "", "mission": None}])
    client.post("/fleets/p/mission", data={"targets": "ABOTEIN"}, headers=HX)
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "p")
    texts = [x["text"] for x in (f.get("mission") or {}).get("log") or []]
    assert any("no relay of yours in ABOTEIN and no replicant rides with the fleet" in t for t in texts), texts


def test_a_fleets_ward_goes_with_it_and_systems_list_shows_wards(client):
    """Wards travel with their fleet: deployed and activated where it works, deactivated and boarded when it leaves."""
    from rsweb import fleets as fl, outposts as op
    from rsweb.modular import prepare_moves
    fleet, devices = _survey_world()
    devices.append({"device_code": "W1", "device_type": "system_ward", "location": None, "stowed_in_device_code": "HV",
                    "status": "stowed", "features": ["cruise", "ward", "stow"], "tags": ["fleet:s"]})
    descs = [s["desc"] for s in fl.unload_steps(fleet, devices)]
    assert descs.index("deploy W1 from HV") + 1 == descs.index("W1: activate ward")
    # leaving: the active ward is switched off right before it flies to the carrier, and on again where it's unloaded
    devices[-1].update({"location": "LOR-2", "stowed_in_device_code": None, "status": "warding"})
    devices[1].update({"location": "LOR-1-L4", "stowed_in_device_code": None, "status": "idle"})
    board, _ = fl.assemble_steps(fleet, devices)
    out = [s["desc"] for s in prepare_moves(board, devices, {})]
    assert out.index("W1: stop warding (deactivate) to move") + 1 == out.index("W1 → LOR-1-L4 (board)")
    pres = op.presence(devices)
    assert [d["device_code"] for d in pres["FAL"]["relay"]] == ["R0"] and pres["FAL"]["ward"] == []
    # the Systems list: the outpost columns always show
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get("/systems", headers=H).text
    assert ">Relay</th>" in page and ">Beacon</th>" in page and ">Ward</th>" in page and "✓ BCN00001" in page
    assert "— none" in page and "only systems missing one" not in page and 'name="outposts"' not in page


def test_vessels_hosting_a_replicant_can_join_a_fleet(client):
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.worker.sync_devices)
    reps = client.portal.call(client.app.state.db.kv_get, "replicants") or {}
    code, rep = next((c, r) for c, r in reps.items() if r.get("hosted_device_code"))
    client.portal.call(eng.save_fleets, [{"id": "s", "name": "Surveyors", "role": "explore", "home": "SOL", "wants": {},
                                          "station": False, "materials": "", "mission": None}])
    assert 'hx-get="/fleets/s/picker"' in client.get("/fleets/details?all=1", headers=H).text   # loaded when the box opens
    page = client.get("/fleets/s/picker", headers=H).text
    assert f'name="add" value="{rep["hosted_device_code"]}"' in page and f"hosts {rep.get('name') or code}" in page


def test_deploy_is_refused_while_the_carrier_is_travelling(client):
    """Live 2026-10-06: slingshot E28DBE58 deployed mid-surge came out between systems, with no location."""
    world = client.app.state.api.http._transport.app.state.world
    vessel = next(d for d in world.devices if "vessel" in d["device_type"])
    cargo = next(d for d in world.devices if d["device_type"] == "survey_drone")
    vessel.update({"status": "travelling", "travel": {"destination": "ABOTEIN-1-L4", "arrives_at": "2099-01-01T00:00:00+00:00"}})
    cargo.update({"status": "stowed", "location": None, "stowed_in_device_code": vessel["device_code"]})
    client.portal.call(client.app.state.worker.sync_devices)
    r = client.post(f"/devices/{cargo['device_code']}/command", data={"command": "deploy"}, headers=HX)
    assert "is traveling to ABOTEIN-1-L4" in r.text and "between systems" in r.text


def test_feedback_goes_to_the_developers(client):
    world = client.app.state.api.http._transport.app.state.world
    page = client.get("/diagnostics", headers=H).text
    assert "Send feedback to the game" in page
    r = client.post("/diagnostics/feedback", data={"kind": "bug", "body": "Slingshot has no location after a deploy mid-surge",
                                                   "context": "on"}, headers=HX)
    assert "Sent" in r.text and world.feedback[-1]["type"] == "bug"
    assert world.feedback[-1]["body"].startswith("Slingshot has no location") and "web client" in world.feedback[-1]["body"]
    assert "Slingshot has no location" in client.get("/diagnostics", headers=H).text      # listed under Sent
    assert "Write a few words" in client.post("/diagnostics/feedback", data={"kind": "idea", "body": ""}, headers=HX).text


def test_a_cancelled_trip_stops_the_wait(client):
    """travel.cancelled: the device turns back to its origin — a job waiting for its arrival stops waiting."""
    from rsweb.automations import step
    eng = client.app.state.worker.automations
    st = step("SD1 → KEL-3", "/devices/SD1", {"command": "travel", "destination": "KEL-3"}, wait=["travel.arrived"],
              match={"destination": "KEL-3"}, critical=True)
    st["wait_device"] = "SD1"
    job = {"id": "j-cancel", "rule": "fleets", "title": "move SD1", "device": "SD1", "steps": [st], "idx": 0,
           "status": "waiting", "created_at": "2026-10-07T12:00:00+00:00", "meta": {}}
    st["status"] = "waiting"
    client.portal.call(eng.save_jobs, [job])
    client.portal.call(eng.on_event, {"event": "travel.cancelled", "device_code": "SD1",
                                      "payload": {"origin": "KEL-1-L4", "destination": "KEL-3", "return_time_seconds": 300}})
    j = next(x for x in client.portal.call(eng.jobs) if x["id"] == "j-cancel")
    assert j["status"] == "failed" and "returns to KEL-1-L4 (≈5 min)" in j["steps"][0]["error"]


def test_cancel_travel_from_the_device_page(client):
    world = client.app.state.api.http._transport.app.state.world
    world.move_seconds = 300
    drone = next(d for d in world.devices if d["device_type"] == "survey_drone")
    start = drone["location"]
    r = client.post(f"/devices/{drone['device_code']}/command", data={"command": "travel", "f.destination": "SOL-4"}, headers=HX)
    assert "result ok" in r.text, r.text
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get(f"/devices/{drone['device_code']}", headers=H).text
    assert "Traveling to <b>SOL-4</b>" in page and "Cancel travel" in page
    r = client.post(f"/devices/{drone['device_code']}/cancel-travel", headers=HX)
    assert "result ok" in r.text and "DELETE" in r.text
    assert drone["location"] == start and not drone.get("travel")
    assert any(e["event"] == "travel.cancelled" for e in world.events)
    # the vessel hosting the replicant cancels through its replicant
    rep_code = next(iter(client.portal.call(client.app.state.db.kv_get, "replicants")))
    host = next(d for d in world.devices if d["device_code"] == client.portal.call(client.app.state.db.kv_get, "replicants")[rep_code]["hosted_device_code"])
    r = client.post(f"/devices/{host['device_code']}/cancel-travel", headers=HX)
    assert f"/replicants/{rep_code}/travel" in r.text


def test_desktop_wallpaper_routes_need_a_key_and_stay_read_only(client, monkeypatch):
    import re as _re
    from rsweb import wallpaper as wp
    client.portal.call(client.app.state.worker.sync_devices)
    # off until a link is made: the page and the data are 404, the API without a key too
    assert client.get("/wallpaper/me/").status_code == 404
    assert client.get("/wallpaper/me/api/map.json").status_code == 404
    page = client.get("/account", headers=H).text
    assert "Desktop wallpaper" in page and "Create wallpaper link" in page
    r = client.post("/wallpaper-settings", data={"action": "create", "label": "office PC"}, headers=HX)
    link = _re.search(r'value="(https?://[^"]+/wallpaper/me/#key=(rsw_[^"]+))"', r.text)
    assert link, r.text
    key = link.group(2)
    st = client.portal.call(client.app.state.db.kv_get, wp.KV)
    assert st["enabled"] and key not in str(st) and st["keys"][0]["label"] == "office PC"   # only the hash is stored
    # the page is served (no data in it); the data needs the key, sent as a header
    page = client.get("/wallpaper/me/").text
    assert "wallpaper.js" in page and "importmap" in page and key not in page
    from rsweb.version import VERSION
    assert f"wallpaper.js?v={VERSION}" in page   # versioned: a redeploy is never hidden by a cached script
    assert client.get("/wallpaper/me/").headers["cache-control"] == "no-cache"
    assert client.get("/wallpaper/me/api/map.json").status_code == 401
    assert client.get("/wallpaper/me/api/map.json", headers={wp.HEADER: "rsw_wrong"}).status_code == 401
    good = {wp.HEADER: key}
    data = client.get("/wallpaper/me/api/map.json", headers=good).json()
    assert "stars" in data and "replicants" in data
    systems = client.get("/wallpaper/me/api/systems.json", headers=good).json()["systems"]
    if systems:
        svg = client.get(f"/wallpaper/me/api/system/{systems[0]['star']}", headers=good).text
        assert '<svg class="system"' in svg
    assert client.get("/wallpaper/me/api/system/NOWHERE", headers=good).status_code == 404
    # only the wallpaper's own files are served without signing in
    assert client.get("/wallpaper/me/static/map.js").status_code == 200
    assert client.get("/wallpaper/me/static/vendor/three.module.min.js").status_code == 200
    assert client.get("/wallpaper/me/static/app.js").status_code == 404
    assert client.get("/wallpaper/me/static/../web.py").status_code == 404
    # it can't change anything: no write routes under /wallpaper/
    assert client.post("/wallpaper/me/api/map.json", headers=good).status_code in (404, 405)
    # multi-user: a server answers only for its own slug
    monkeypatch.setenv("WALLPAPER_SLUG", "joe-abc123")
    assert client.get("/wallpaper/me/api/map.json", headers=good).status_code == 404
    assert client.get("/wallpaper/joe-abc123/api/map.json", headers=good).status_code == 200
    monkeypatch.delenv("WALLPAPER_SLUG")
    # turning it off stops every link; revoking stops one
    client.post("/wallpaper-settings", data={"action": "disable"}, headers=HX)
    assert client.get("/wallpaper/me/api/map.json", headers=good).status_code == 404
    client.post("/wallpaper-settings", data={"action": "enable"}, headers=HX)
    assert client.get("/wallpaper/me/api/map.json", headers=good).status_code == 200
    kid = client.portal.call(client.app.state.db.kv_get, wp.KV)["keys"][0]["id"]
    client.post("/wallpaper-settings", data={"action": "revoke", "key_id": kid}, headers=HX)
    assert client.get("/wallpaper/me/api/map.json", headers=good).status_code == 401
    # the settings endpoint itself needs the signed-in user (an htmx request from the app)
    assert client.post("/wallpaper-settings", data={"action": "enable"}, headers=H).status_code == 403


def test_wallpaper_dashboard_panel_and_fleet_markers(client):
    from rsweb import wallpaper as wp
    from rsweb import fleets as fl
    from rsweb.web import status_class
    eng = client.app.state.worker.automations
    world = client.app.state.api.http._transport.app.state.world
    world.devices[1]["tags"] = ["fleet:p1"]
    client.portal.call(eng.save_fleets, [{"id": "p1", "name": "Prospectors", "role": "mining", "home": "SOL", "wants": {},
                                          "mission": {"status": "running", "phase": "travel", "targets": ["KELMONENT"], "idx": 0}},
                                         {"id": "e1", "name": "Empty", "role": "explore", "home": "SOL", "wants": {}}])
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(client.app.state.worker.sync_inventory)
    r = client.post("/wallpaper-settings", data={"action": "create"}, headers=HX)
    import re as _re
    good = {wp.HEADER: _re.search(r"#key=(rsw_[^\"]+)", r.text).group(1)}
    # the panel's data needs the key like the rest
    assert client.get("/wallpaper/me/api/hud.json").status_code == 401
    hud = client.get("/wallpaper/me/api/hud.json", headers=good).json()
    assert set(hud["devices"]) == {"total", "working", "moving", "idle"} and hud["devices"]["total"] == len(world.devices)
    assert all(x["name"] in ("structural", "conductive", "silicates", "carbon", "volatiles", "rares") for x in hud["resources"])
    # fleets with members only, with their mission: phase, where they're headed, where they are
    f = next(x for x in hud["fleets"] if x["id"] == "p1")
    assert [x["id"] for x in hud["fleets"]] == ["p1"]
    assert f["state"] == "running" and f["phase"] == "travel" and f["target"] == "KELMONENT" and f["members"] == 1
    assert f["working"] + f["moving"] + f["idle"] <= 1
    # the galaxy data carries the same fleets (the Galaxy page and the wallpaper draw them)
    galaxy = client.get("/wallpaper/me/api/map.json", headers=good).json()
    assert [x["id"] for x in galaxy["fleets"]] == ["p1"] and galaxy["supply"] == [] and hud["supply"] == []
    assert "fleets" in client.get("/api/map.json", headers=H).json()
    # no mission: stationed or idle, and no target
    a = fl.activity({"id": "s", "home": "SOL", "station": True}, [], status_class)
    assert a["state"] == "stationed" and a["target"] is None and a["stars"] == []
    # members riding in a carrier count where the carrier is
    devs = [{"device_code": "C", "location": "ZED-1", "status": "travelling"},
            {"device_code": "D", "tags": ["fleet:x"], "stowed_in_device_code": "C", "status": "stowed"},
            {"device_code": "E", "tags": ["fleet:x"], "location": "ABC-2", "status": "mining"}]
    a = fl.activity({"id": "x", "mission": {"status": "stalled", "phase": "work", "targets": ["ABC", "ZED"], "idx": 5}}, devs, status_class)
    assert sorted(a["stars"]) == ["ABC", "ZED"] and a["target"] == "ZED" and a["working"] == 1 and a["state"] == "stalled"
    # the page's own files are what the panel needs
    assert client.get("/wallpaper/me/static/wallpaper.js").status_code == 200


def test_supply_links_between_fleets():
    from datetime import datetime, timedelta, timezone
    from rsweb import fleets as fl
    now = datetime.now(timezone.utc)
    fleets = [{"id": "m", "name": "Miners", "role": "mining", "home": "ABC", "materials": "f"},
              {"id": "f", "name": "Factory", "role": "mining", "home": "XYZ", "materials": "self"},
              {"id": "d", "name": "Deliverers", "role": "mining", "home": "ABC",
               "mission": {"status": "running", "phase": "work", "targets": ["KEL-BELT-1"], "idx": 0, "deliver_to": "XYZ-3-L4"}},
              {"id": "t", "name": "Traders", "role": "trade", "home": "XYZ",
               "mission": {"status": "running", "phase": "collect", "contract": {"location": "QRS-2", "designation": "C1"},
                           "drop_star": "ABC"}},
              {"id": "o", "name": "Old", "role": "trade", "home": "XYZ",
               "mission": {"status": "done", "contract": {"location": "NOPE-1"}}}]
    links = {(x["from"], x["to"], x["kind"]): x for x in fl.supply_links(fleets, [])}
    # configured only: planned; missions in progress: active; finished missions draw nothing
    assert links[("ABC", "XYZ", "materials")]["state"] == "planned"
    assert links[("ABC", "XYZ", "materials")]["from_fleet"] == "Miners" and links[("ABC", "XYZ", "materials")]["to_fleet"] == "Factory"
    assert links[("KEL", "XYZ", "materials")]["state"] == "active" and links[("KEL", "XYZ", "materials")]["to_fleet"] == "Factory"
    assert links[("XYZ", "QRS", "trade")]["state"] == "active" and links[("QRS", "ABC", "trade")]["state"] == "active"
    assert not any("NOPE" in k for k in links)
    # a ferry controller running the route: ferrying, with its freighters; something traveling along it: moving
    devices = [{"device_code": "TC", "device_type": "ami_transport_controller", "location": "ABC-2",
                "ami_directive": {"name": "ferry", "config": {"collect": "ABC-2", "deliver": "XYZ-3-L4"}}},
               {"device_code": "F1", "device_type": "cargo_freighter", "controller_device_code": "TC", "location": "ABC-2"},
               {"device_code": "F2", "device_type": "cargo_freighter", "controller_device_code": "TC", "location": "ABC-2"}]
    x = {(l["from"], l["to"]): l for l in fl.supply_links(fleets, devices)}[("ABC", "XYZ")]
    assert x["state"] == "ferrying" and x["freighters"] == 2 and x["in_transit"] == 0
    devices[1]["travel"] = {"origin": "ABC-2", "destination": "XYZ-3-L4", "departed_at": (now - timedelta(minutes=5)).isoformat(),
                            "arrives_at": (now + timedelta(minutes=5)).isoformat()}
    x = {(l["from"], l["to"]): l for l in fl.supply_links(fleets, devices)}[("ABC", "XYZ")]
    assert x["state"] == "moving" and x["in_transit"] == 1
    # a fleet sending to itself or to a fleet in the same system draws no line
    assert fl.supply_links([{"id": "a", "home": "ABC", "materials": "b"}, {"id": "b", "home": "ABC", "materials": "self"}], []) == []


def test_compact_on_an_already_compacted_device_counts_as_done(client, monkeypatch):
    """A move's compact step refused because the device is already compacted is what the step wanted: the job goes
    on. Still compacting: it keeps waiting for device.compacted. Anything else still fails the job."""
    import time
    from rsweb.automations import _already_compact, _already_unfurled, step
    assert _already_compact("Device is already compacted") and _already_compact("Cannot compact: device is compacted")
    assert _already_compact("Device is already compacting") and not _already_compact("Device is not compacted")
    assert not _already_compact("Cannot compact while traveling")
    assert _already_unfurled("Device is not compacted") and _already_unfurled("Device is already unfurled")
    eng = client.app.state.worker.automations
    reply = {}

    async def fake_send(method, path, body, label):
        return False, None, reply["err"]
    monkeypatch.setattr(eng, "send", fake_send)

    def run(err):
        reply["err"] = err
        job = client.portal.call(eng.create_job, "loadouts", "move", None,
                                 [step("X: compact before moving", "/devices/X", {"command": "compact"}, wait=["device.compacted"],
                                       critical=True)], {}, True)
        for _ in range(10):
            client.portal.call(eng.tick)
            j = next(x for x in client.portal.call(eng.jobs) if x["id"] == job["id"])
            if j["status"] not in ("running", "waiting"):
                break
            time.sleep(0.05)
        return j
    j = run("Device is already compacted")
    assert j["status"] == "done" and "already compacted" in j["steps"][0]["note"]
    from rsweb.modular import FOLDED_KV
    assert "X" in client.portal.call(client.app.state.db.kv_get, FOLDED_KV)   # remembered: the next pass sends the carrier
    j = run("Device is already compacting")
    assert j["status"] in ("running", "waiting") and j["steps"][0]["wait"] == ["device.compacted"]
    assert run("Cannot compact while traveling")["status"] == "failed"


def test_folded_devices_are_remembered_so_the_carrier_comes(client):
    """Live 2026-10-07: observatory B3AEBF60 was compacted but its status didn't say so; every loadout pass made another
    compact job ('Device is already compacted') and no carrier was ever sent."""
    from rsweb import loadouts as lo
    from rsweb.modular import FOLDED_KV, remember
    obs = {"device_code": "OBS", "device_type": "galactic_observatory", "location": "FAL-1-L4", "status": "idle",
           "features": ["cruise", "modular"], "available_commands": ["compact", "unfurl", "travel"], "tags": ["fleet:m1"]}
    mf = {"device_code": "MF", "device_type": "mobile_fleet", "location": "FAL-1-L4", "status": "idle",
          "features": ["surge", "attach"], "attach_capacity": 36, "tags": ["fleet:m1"]}
    stars = {s: {"position": {"x": i, "y": 0, "z": 0}, "entry_point": f"{s}-1-L4"} for i, s in enumerate(("FAL", "LAR"))}
    cfg = {"phases": [], "fleets": [{"id": "m1", "name": "Miner 1", "home": "LAR", "station": True, "wants": {}}],
           "fleets_migrated": True}
    obs["folded"] = True                                    # what the device sync adds for a device we know is folded
    p = lo.plan(cfg, [obs, mf], [], {}, stars, {}, set(), [], {})
    assert p["compact"] == [] and [(dl["carrier"], dl["devices"]) for dl in p["deliveries"]] == [("MF", ["OBS"])]
    # the mark: set by a refused compact / device.compacted / a compacted print, carried by the device sync, cleared on unfurl
    db, worker = client.app.state.db, client.app.state.worker
    world = client.app.state.api.http._transport.app.state.world
    code = world.devices[0]["device_code"]
    client.portal.call(remember, db, code, True, "2026-10-07T00:00:00+00:00")
    client.portal.call(worker.sync_devices)
    assert next(d for d in client.portal.call(db.kv_get, "devices") if d["device_code"] == code).get("folded") is True
    client.portal.call(worker.apply_timers, {"event": "device.unfurling", "device_code": code, "payload": {}})
    assert code not in client.portal.call(db.kv_get, FOLDED_KV)
    client.portal.call(worker.sync_devices)
    assert "folded" not in next(d for d in client.portal.call(db.kv_get, "devices") if d["device_code"] == code)
    client.portal.call(worker.apply_timers, {"event": "print.completed", "device_code": "AF",
                                             "payload": {"new_device_code": "NEW1", "compacted": True}})
    assert "NEW1" in client.portal.call(db.kv_get, FOLDED_KV)


def test_tag_changes_never_add_and_remove_the_same_tag():
    """Live 2026-10-07: 'A53A86C0 joins trader-1' was refused: 'Tag appears in both add_tags and remove_tags'."""
    from rsweb import loadouts as lo
    st = lo.tag_step("X", add=["fleet:a"], remove=["fleet:a", "spare"])
    assert st["body"]["configuration"] == {"add_tags": ["fleet:a"], "remove_tags": ["spare"]}


def test_adding_a_device_already_in_the_fleet_sends_nothing(client):
    world = client.app.state.api.http._transport.app.state.world
    world.devices[1]["tags"] = ["fleet:t1"]
    client.portal.call(client.app.state.worker.sync_devices)
    eng = client.app.state.worker.automations
    client.portal.call(eng.save_fleets, [{"id": "t1", "name": "T1", "role": "trade", "home": "SOL", "wants": {}}])
    before = len(client.portal.call(eng.jobs))
    r = client.post("/fleets/t1/members", data={"add": world.devices[1]["device_code"]}, headers=HX)
    assert "already in this fleet" in r.text
    for j in client.portal.call(eng.jobs)[: max(0, len(client.portal.call(eng.jobs)) - before)]:
        for s in j["steps"]:
            c = (s.get("body") or {}).get("configuration") or {}
            assert not set(c.get("add_tags") or []) & set(c.get("remove_tags") or [])


def test_galaxy_shows_what_each_system_is_mining(client):
    from rsweb.web import mining_now
    assert mining_now([{"status": "mining (carbon)"}, {"status": "mining (carbon)"}, {"status": "mining (rares)"},
                       {"status": "mining"}, {"status": "idle"}]) == {"carbon": 2, "rares": 1}
    world = client.app.state.api.http._transport.app.state.world
    world.devices[1]["status"] = "mining (volatiles)"
    client.portal.call(client.app.state.worker.sync_devices)
    star = world.devices[1]["location"].split("-")[0]
    stars = client.get("/api/map.json", headers=H).json()["stars"]
    s = next((x for x in stars if x["designation"] == star), None)
    assert s is None or s["mining"].get("volatiles", 0) >= 1


def test_parked_mining_controller_goes_to_the_belt_with_its_drones():
    """Live 2026-10-07: LORSELAN / LARSELAN controllers sat at Lagrange points ('gated:cold_repair', logging
    ami_overheat) with every drone idle, though the system has a belt."""
    from rsweb import salvage as sv
    ctrl = {"device_code": "C1", "device_type": "ami_mining_controller", "location": "LOR-6-L4", "status": "coordinating",
            "ami_directive": {"name": "gather_evenly", "_eval_state": "gated:cold_repair"}, "ami_directive_status": "active"}
    mine = [{"device_code": f"M{i}", "device_type": "mining_drone", "location": "LOR-6-L4", "status": "idle",
             "controller_device_code": "C1"} for i in range(2)]
    stray = {"device_code": "S1", "device_type": "mining_drone", "location": "LOR-2", "status": "idle"}
    busy = {"device_code": "S2", "device_type": "mining_drone", "location": "LOR-2", "status": "idle"}
    devices = [ctrl, *mine, stray, busy]
    assert sv.parked(ctrl)
    assert not sv.parked({**ctrl, "location": "LOR-BELT-1"})                                       # at a belt
    assert not sv.parked({**ctrl, "ami_directive": {"name": "gather_salvage", "_eval_state": "active"}})   # salvaging
    assert not sv.parked({**ctrl, "status": "travelling"})
    plans = sv.back_to_belt_plan([ctrl], devices, {}, {}, {"LOR": ["LOR-BELT-1"]}, set(), None, {"C1", "M0", "M1", "S1"})
    assert len(plans) == 1
    p = plans[0]
    assert p["belt"] == "LOR-BELT-1" and p["move_ctrl"] and p["away"] == ["M0", "M1"] and p["strays"] == ["S1"]
    assert "parked" in p["why"] and p["directive"] == "gather_evenly"
    steps = [(s["path"], s["body"]) for s in sv.back_to_belt_steps(p)]
    assert steps[0] == ("/devices/C1", {"command": "release", "devices": ["M0", "M1"]})   # only its own drones are released
    assert ("/devices/C1", {"command": "travel", "destination": "LOR-BELT-1"}) in steps
    assert ("/devices/S1", {"command": "travel", "destination": "LOR-BELT-1"}) in steps
    assert ("/devices/C1", {"command": "adopt", "devices": ["M0", "M1", "S1"]}) in steps
    assert steps[-1] == ("/devices/C1", {"command": "launch"})
    # no belt in the system: nothing to plan here (salvage takes over)
    assert sv.back_to_belt_plan([ctrl], devices, {}, {}, {"LOR": []}, set(), None, set()) == []
    # live 2026-10-25: drones it holds in another system can't surge back — let go, never flown
    far = {"device_code": "M9", "device_type": "mining_drone", "location": "SOL-BELT-1", "status": "idle",
           "controller_device_code": "C1"}
    p = sv.back_to_belt_plan([ctrl], devices + [far], {}, {}, {"LOR": ["LOR-BELT-1"]}, set(), None, set())[0]
    assert p["away"] == ["M0", "M1"] and p["gone"] == ["M9"]
    steps = [(s["path"], s["body"]) for s in sv.back_to_belt_steps(p)]
    assert ("/devices/C1", {"command": "release", "devices": ["M9"]}) in steps
    assert not any(path == "/devices/M9" for path, _ in steps)


def test_miners_in_a_system_without_a_belt_go_to_salvage(client):
    import json as _json
    world, eng = _salvage_setup(client)
    db = client.app.state.db
    row = client.portal.call(db.fetchone, "SELECT data FROM systems WHERE star='SOL'")
    scan = _json.loads(row["data"])
    scan.pop("asteroid_belt", None)                                      # scanned, and no belt
    client.portal.call(db.execute, "UPDATE systems SET data=? WHERE star='SOL'", (_json.dumps(scan),))
    client.portal.call(eng.rule_salvage)
    jobs = [j for j in client.portal.call(eng.jobs) if j["rule"] == "salvage_when_depleted"]
    assert any(j["device"] == "MC91FF22" and "salvage" in j["title"] for j in jobs)


def test_automation_log_kinds_and_toggles(client):
    from rsweb.automations import severity, stop_level
    # owner 2026-10-30: an error is only what no automation will fix; a stopped automated job is a warning
    assert stop_level({"rule": "loadouts"}) == "alert" and stop_level({"rule": "loadouts", "forced": True}) == "error"
    assert severity({"level": "alert", "text": "stopped: loadouts: x — y failed: out of range"}) == "warning"
    assert severity({"level": "alert", "text": "scan of SOL failed: busy"}) == "warning"
    assert severity({"level": "error", "text": "x: waits for your Confirm on the Automations page"}) == "error"
    assert severity({"level": "alert", "text": "Miner 1: stalled in work — no more automatic retries (8 tried)"}) == "error"
    assert severity({"level": "alert", "text": "fleet: 1 change(s): skipped 'A joins b' (Tag appears in both)"}) == "warning"
    assert severity({"level": "alert", "text": "Miner 1: warning: no relay of yours in X"}) == "warning"
    assert severity({"level": "info", "text": "finished: loadouts: X"}) == "info"
    eng = client.app.state.worker.automations
    client.portal.call(eng.log, "chain", "stopped: a — b failed: c", stop_level({"rule": "chain"}))
    client.portal.call(eng.log, "fleets", "skipped 'x' (y)", "alert")
    client.portal.call(eng.log, "fleets", "finished: z")
    page = client.get("/automations", headers=H).text
    assert 'id="log-toggles"' in page and 'data-sev="error"' in page and 'data-sev="warning"' in page and 'data-sev="info"' in page
    assert "errors (1)" in page and "warnings (1)" in page


def test_controller_at_the_belt_brings_its_scattered_drones():
    """The game's docs: a controller running drones at many places multi-tasks, which brings ami_overheat. Live
    2026-10-07: LORSELAN's controller at the belt, its 8 drones idle at LORSELAN-6-L4."""
    from rsweb import salvage as sv
    ctrl = {"device_code": "C1", "device_type": "ami_mining_controller", "location": "LOR-BELT-1", "status": "coordinating",
            "ami_directive": {"name": "gather_evenly", "_eval_state": "gated:cold_repair"}}
    kids = [{"device_code": f"M{i}", "device_type": "mining_drone", "location": "LOR-6-L4", "status": "idle",
             "controller_device_code": "C1"} for i in range(3)]
    assert sv.scattered(ctrl, [ctrl, *kids]) == ["M0", "M1", "M2"]
    p = sv.back_to_belt_plan([ctrl], [ctrl, *kids], {}, {}, {"LOR": ["LOR-BELT-1"]}, set())[0]
    assert p["belt"] == "LOR-BELT-1" and not p["move_ctrl"] and p["away"] == ["M0", "M1", "M2"] and "overheats" in p["why"]
    # left alone: one of them mining, salvage on purpose, drones at a site in the belt, or a controller away from belts
    assert sv.scattered(ctrl, [ctrl, {**kids[0], "status": "mining (carbon)"}, *kids[1:]]) == []
    assert sv.scattered({**ctrl, "ami_directive": {"name": "gather_salvage"}}, [ctrl, *kids]) == []
    assert sv.scattered(ctrl, [ctrl, {**kids[0], "location": "LOR-BELT-1-SITE-2"}]) == []
    assert sv.scattered({**ctrl, "location": "LOR-6-L4"}, [ctrl, *kids]) == []


def test_mining_watch_ends_when_the_salvage_is_used_up():
    """Live 2026-10-07: Prospectors at KELMONENT (no belt) salvaged; the controller reported gather_salvage
    'depleted:complete' and the watch phase waited for 'exhausted' for ever."""
    from rsweb import fleets as fl
    f = {"id": "p", "role": "mining"}
    ctrl = {"device_code": "C", "device_type": "ami_mining_controller", "tags": ["fleet:p"], "location": "KEL-1",
            "ami_directive": {"name": "gather_salvage", "_eval_state": "depleted:complete"}}
    drone = {"device_code": "D", "device_type": "mining_drone", "tags": ["fleet:p"], "location": "KEL-1", "status": "idle"}
    done, why, upd = fl.watch_done(f, {}, [ctrl, drone], "2026-10-07T12:00:00+00:00")
    assert not done and "salvage used up" in why and upd["exhausted_since"]          # the grace period starts
    done, why, _ = fl.watch_done(f, {"exhausted_since": "2026-10-07T11:00:00+00:00"}, [ctrl, drone], "2026-10-07T12:00:00+00:00")
    assert done
    # still mining something: not done, whatever the state says
    assert not fl.watch_done(f, {"exhausted_since": "2026-10-07T11:00:00+00:00"}, [ctrl, {**drone, "status": "mining (carbon)"}],
                             "2026-10-07T12:00:00+00:00")[0]
    # gated / working: not done
    assert not fl.watch_done(f, {}, [{**ctrl, "ami_directive": {"name": "gather_evenly", "_eval_state": "gated:cold_repair"}}, drone],
                             "2026-10-07T12:00:00+00:00")[0]
    # no controller: done once no drone has mined for the grace period
    assert fl.watch_done(f, {"exhausted_since": "2026-10-07T11:00:00+00:00"}, [drone], "2026-10-07T12:00:00+00:00")[0]


def test_mining_mission_moves_on_to_the_next_salvage_before_ending(client, monkeypatch):
    from rsweb import targets
    eng = client.app.state.worker.automations
    db = client.app.state.db
    salvage = {"KEL": [{"code": "KEL-1-SAL-1", "total": 0, "depleted": True}, {"code": "KEL-2-SAL-1", "total": 500},
                       {"code": "KEL-3-SAL-1", "total": 200}]}

    async def fake_resources(_db, star):
        return {"salvage": [dict(x) for x in salvage.get(star.upper(), [])], "sites": []}
    monkeypatch.setattr(targets, "system_resources", fake_resources)
    devices = [{"device_code": "C", "device_type": "ami_mining_controller", "tags": ["fleet:p"], "location": "KEL-1",
                "status": "coordinating", "ami_directive": {"name": "gather_salvage", "_eval_state": "depleted:complete"},
                "available_commands": ["set_directive", "launch", "adopt"]},
               {"device_code": "D", "device_type": "mining_drone", "tags": ["fleet:p"], "location": "KEL-1", "status": "idle",
                "controller_device_code": "C"}]
    client.portal.call(db.kv_set, "devices", devices)
    mission = {"status": "running", "phase": "watch", "idx": 0, "targets": ["KEL"], "opts": {}, "log": [],
               "salvage": "KEL-1-SAL-1", "exhausted_since": "2026-01-01T00:00:00+00:00"}
    client.portal.call(eng.save_fleets, [{"id": "p", "name": "Prospectors", "role": "mining", "home": "FAL", "wants": {},
                                          "mission": mission}])
    client.portal.call(eng.run_fleets)
    m = next(f for f in client.portal.call(eng.fleets) if f["id"] == "p")["mission"]
    texts = [x["text"] for x in m["log"]]
    assert any("KEL-1-SAL-1 used up — moving on to KEL-2-SAL-1" in t for t in texts), texts
    assert m["salvage"] == "KEL-2-SAL-1" and m["salvaged"] == ["KEL-1-SAL-1"] and m["phase"] == "work" and m["status"] == "running"
    # the last one used up: the mission ends its work (recall …) instead of looping
    m.update({"phase": "watch", "job": None, "salvage": "KEL-3-SAL-1", "salvaged": ["KEL-1-SAL-1", "KEL-2-SAL-1"],
              "exhausted_since": "2026-01-01T00:00:00+00:00"})
    client.portal.call(eng.save_fleets, [{"id": "p", "name": "Prospectors", "role": "mining", "home": "FAL", "wants": {},
                                          "mission": m}])
    client.portal.call(eng.run_fleets)
    m = next(f for f in client.portal.call(eng.fleets) if f["id"] == "p")["mission"]
    assert any(x["text"].startswith("work done") for x in m["log"]) and m["phase"] != "work"


def test_system_page_lists_your_devices_there(client):
    from rsweb.web import devices_in_system
    devs = [{"device_code": "A", "device_type": "mining_drone", "location": "SOL-BELT-1", "status": "mining (carbon)",
             "tags": ["fleet:m1"], "controller_device_code": "C"},
            {"device_code": "V", "device_type": "cargo_vessel", "location": "SOL-3", "status": "idle"},
            {"device_code": "R", "device_type": "survey_drone", "stowed_in_device_code": "V", "status": "stowed"},
            {"device_code": "X", "device_type": "mining_drone", "location": "FAL-1", "status": "idle"}]
    h = devices_in_system(devs, "SOL")
    assert [d["device_code"] for d in h["rows"]] == ["V", "R", "A"] and h["rows"][1]["_where"] == "aboard V"   # by place
    assert h["rows"][2]["_fleet"] == "m1" and dict((t, n) for t, n, _ in h["summary"])["mining_drone"] == 1
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get("/systems/SOL", headers=H).text
    assert 'id="devices-here"' in page and "Your devices here" in page


def test_another_players_ward_keeps_our_miners_out(client):
    from rsweb import loadouts as lo
    from rsweb import wards
    stars = {"SOL": {"position": {"x": 0, "y": 0, "z": 0}}, "ZAL": {"position": {"x": 5, "y": 0, "z": 0}, "has_ward": True},
             "OUR": {"position": {"x": 9, "y": 0, "z": 0}, "has_ward": True}}
    ours = {"device_code": "W", "device_type": "system_ward", "location": "OUR-1-L4", "status": "warding"}
    assert wards.foreign(stars, [ours]) == {"ZAL"}                                   # our own ward isn't "another player's"
    assert wards.foreign({"stars": [{"designation": "ZAL", "has_ward": True}]}, []) == {"ZAL"}
    # a stationed fleet at a warded home: no mining controllers / drones are sent; the rest still is
    fleets = [{"id": "z", "name": "Z", "home": "ZAL", "station": True,
               "wants": {"mining_drone": 4, "ami_mining_controller": 1, "survey_drone": 2}}]
    cfg = {"phases": [], "fleets": fleets, "fleets_migrated": True}
    p = lo.plan(cfg, [], [], {}, stars, {}, set(), [], {})
    rows = {r["type"]: r for r in p["report"]["z"]["rows"]}
    assert rows["mining_drone"]["short"] == 0 and rows["mining_drone"]["warded"]
    assert rows["ami_mining_controller"]["short"] == 0 and rows["survey_drone"]["short"] == 2
    assert any("ward" in u["why"] for u in p["unmet"]) and p["report"]["z"]["warded"]
    # a mining mission to a warded system can't launch
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [{"designation": "SOL"},
                                                                        {"designation": "ABOTEIN", "has_ward": True}]})
    client.portal.call(eng.save_fleets, [{"id": "m", "name": "M", "role": "mining", "home": "FAL", "wants": {}}])
    r = client.post("/fleets/m/mission", data={"targets": "ABOTEIN"}, headers=HX)
    assert "another player" in r.text and not (client.portal.call(eng.fleets)[0].get("mission") or {}).get("status")
    # a running mining mission whose target became warded stalls before it unloads
    m = {"status": "running", "phase": "travel", "idx": 0, "targets": ["ABOTEIN"], "opts": {}, "log": []}
    steps, problems = client.portal.call(eng.fleet_phase_steps, {"id": "m", "name": "M", "role": "mining", "home": "FAL"},
                                         m, "deploy", [])
    assert not steps and m.get("stall") and "ward" in problems[0]


def test_mining_prospects_score():
    from rsweb import prospects as pr
    rich = {"asteroid_belt": {"belts": [{"designation": "A-BELT-1", "density": "dense",
                                         "resources": {"carbon": "rich", "volatiles": "high", "rares": "low"}}]}}
    poor = {"asteroid_belt": {"belts": [{"designation": "B-BELT-1", "density": "sparse",
                                         "resources": {"carbon": "scarce", "volatiles": "low", "rares": "low"}}]}}
    a = pr.score("A", rich, {"mineable": 6000, "sites": [{"code": "s"}] * 5}, [{"belt": "A-BELT-1", "verdict": "ok"}], 3.0, True)
    b = pr.score("B", poor, {}, [], 3.0, True)
    assert a["score"] > b["score"] and a["parts"]["proven"] == 20 and a["parts"]["staying"] == 15   # no salvage: 20 of 25
    assert "rich carbon" in a["reasons"][0] and a["best"] == ["carbon", "volatiles"]
    # far away and no relay costs access
    far = pr.score("A", rich, {}, [], 40.0, False)
    assert far["parts"]["access"] == 0 and any("replicant must ride" in x for x in far["reasons"])
    # what waiting prints lack weighs clearly (owner 2026-10-22): a belt rich in it gains, and beats a richer one without it
    tilted = pr.score("A", rich, {}, [], 3.0, True, wanted={"carbon": 1.0})
    plain = pr.score("A", rich, {}, [], 3.0, True)
    assert tilted["parts"]["richness"] > plain["parts"]["richness"] * 1.15
    sil = {"asteroid_belt": {"belts": [{"designation": "S-BELT-1", "density": "dense",
                                        "resources": {"silicates": "rich", "carbon": "low"}}]}}
    assert pr.score("S", sil, {}, [], 3.0, True)["score"] < plain["score"]
    assert pr.score("S", sil, {}, [], 3.0, True, wanted={"silicates": 1.0})["score"] > \
        pr.score("A", rich, {}, [], 3.0, True, wanted={"silicates": 1.0})["score"]
    assert any("short of (carbon)" in x for x in tilted["reasons"])
    # not scored: warded, a stationed fleet's home, unscanned
    assert pr.score("A", rich, {}, [], 3.0, True, warded=True)["status"] == "warded"
    assert pr.score("A", rich, {}, [], 3.0, True, stationed="Home")["score"] is None
    home = pr.score("A", rich, {}, [], 3.0, True, stationed="Home")
    assert home["status"] == "stationed" and home["review_score"] == pr.score("A", rich, {}, [], 3.0, True)["score"]
    assert pr.score("C", None, None, [], 3.0, False)["status"] == "unscanned"
    # wanted: a waiting print's shortfall counts most; a falling or low stockpile a little
    w = pr.wanted_from({"carbon": 10, "structural": 500, "silicates": 400, "volatiles": 300, "conductive": 450, "rares": 5},
                       {"volatiles": [600, 300]}, [{"carbon": 100}])
    assert w["carbon"] >= 0.9 and 0 < w["volatiles"] <= 0.6 and w["rares"] == 0.4 and w["structural"] == 0


def test_prospects_page_and_mining_targets(client):
    client.portal.call(client.app.state.worker.sync_devices)
    client.get("/systems/SOL", headers=H)   # a stored scan
    page = client.get("/systems/prospects", headers=H).text
    assert "Mining prospects" in page and "/systems/SOL" in page
    assert "Mining prospects" in client.get("/systems", headers=H).text
    eng = client.app.state.worker.automations
    client.portal.call(eng.save_fleets, [{"id": "m", "name": "M", "role": "mining", "home": "ABOTEIN", "wants": {}}])
    f = client.get("/fleets/details?all=1", headers=H).text
    assert "prospect " in f


def test_systems_page_sorts_by_distance_from_origin(client):
    client.portal.call(client.app.state.worker.sync_devices)
    for s in ("SOL", "FARAWAY", "NEARBY"):
        client.portal.call(client.app.state.db.execute, "INSERT OR REPLACE INTO systems (star, data, updated_at) "
                           "VALUES (?, '{}', '2026-10-18T00:00:00+00:00')", (s,))
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [
        {"designation": "SOL", "position": {"x": 0, "y": 0, "z": 0}},
        {"designation": "FARAWAY", "position": {"x": 40, "y": 0, "z": 0}},
        {"designation": "NEARBY", "position": {"x": 3, "y": 4, "z": 0}}]})
    page = client.get("/systems?origin=SOL", headers=H).text
    assert "distances from" in page and "5.0 ly" in page and "40.0 ly" in page
    assert page.index("/systems/SOL\"") < page.index("/systems/NEARBY\"") < page.index("/systems/FARAWAY\"")
    page = client.get("/systems?origin=FARAWAY", headers=H).text
    assert page.index("/systems/FARAWAY\"") < page.index("/systems/NEARBY\"") < page.index("/systems/SOL\"")


def test_prospects_page_scores_stationed_homes_for_review(client):
    eng = client.app.state.worker.automations
    client.portal.call(eng.save_fleets, [{"id": "h", "name": "Homers", "role": "mining", "home": "SOL", "station": True,
                                          "wants": {"mining_drone": 3}}])
    client.portal.call(client.app.state.worker.sync_devices)
    client.get("/systems/SOL", headers=H)
    page = client.get("/systems/prospects", headers=H).text
    row = page[page.index('href="/systems/SOL"'):].split("</tr>")[0]
    assert "your fleet here: Homers" in row and "⚑" in row and ">home<" in row


def test_another_players_hub_counts_as_a_ward():
    from rsweb import wards
    stars = {"stars": [{"designation": "CYGNUS", "has_hub": True}, {"designation": "TARAZEDAR", "has_ward": True},
                       {"designation": "SOL"}]}
    assert wards.foreign(stars, []) == {"CYGNUS", "TARAZEDAR"}
    our_hub = {"device_code": "H", "device_type": "system_hub", "location": "CYGNUS-5-L4", "status": "relaying"}
    assert wards.foreign(stars, [our_hub]) == {"TARAZEDAR"}                         # our own hub doesn't keep us out
    assert wards.foreign(stars, [{**our_hub, "status": "compacted"}]) == {"CYGNUS", "TARAZEDAR"}   # folded up: not ours there


def test_observatory_aims_and_found_stars(client):
    from rsweb.observatory import aim_vector
    here, there = {"x": 10, "y": -2, "z": 4}, {"x": 13, "y": 0, "z": 4}
    assert aim_vector("outward", here) is None and aim_vector("", here) is None   # omit: away from Sol
    assert aim_vector("sol", here) == [-10.0, 2.0, -4.0]
    assert aim_vector("sideways", here) == [0.0, 1.0, 0.0]
    assert aim_vector("star", here, there) == [3.0, 2.0, 0.0]
    import pytest
    with pytest.raises(ValueError):
        aim_vector("sol", {"x": 0, "y": 0, "z": 0})
    with pytest.raises(ValueError):
        aim_vector("star", here, None)
    # the command form offers the aims; the command sends the worked-out direction
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [
        {"designation": "SOL", "position": {"x": 0, "y": 0, "z": 0}}, {"designation": "ABOTEIN", "position": {"x": 3, "y": 4, "z": 0}}]})
    code = world.devices[0]["device_code"]
    form = client.get(f"/devices/{code}/command-form", params={"command": "prospect"}, headers=HX).text
    assert 'name="aim"' in form and "Toward Sol" in form and "ABOTEIN" in form
    client.post(f"/devices/{code}/command", data={"command": "prospect", "aim": "star", "aim_star": "abotein"}, headers=HX)
    row = client.portal.call(client.app.state.db.fetchone, "SELECT body FROM actions WHERE path=? ORDER BY id DESC LIMIT 1",
                             (f"/devices/{code}",))
    import json as _json
    assert _json.loads(row["body"]) == {"command": "prospect", "direction": [3.0, 4.0, 0.0]}
    # stars a prospect finds go on the map
    client.portal.call(client.app.state.worker.handle_event, {
        "id": "p-1", "event": "prospect.completed", "device_code": code, "location": "SOL-3-L4", "created_at": "2026-10-07T12:00:00+00:00",
        "payload": {"origin": "SOL", "stars_generated": 1,
                    "stars": [{"designation": "NEWSTAR", "position": {"x": 80, "y": 1, "z": 2}}]}})
    stars = {s["designation"] for s in client.portal.call(client.app.state.db.kv_get, "stars")["stars"]}
    assert "NEWSTAR" in stars


def test_catalogue_includes_observatory_finds_even_from_before(client):
    """Stars from every stored prospect.completed go on the map (finds from before 1.36 were never merged); a find with
    no position is reported, not plotted at Sol."""
    db = client.app.state.db
    client.portal.call(db.insert_event, {"id": "old-1", "event": "prospect.completed", "device_code": "OBS",
                                          "created_at": "2026-10-01T00:00:00+00:00",
                                          "payload": {"stars_generated": 2, "stars": [
                                              {"designation": "FARSTAR", "position": {"x": 90, "y": 0, "z": 1}}, "NOPOS"]}})
    client.portal.call(client.app.state.worker.sync_catalogue)
    cat = client.portal.call(db.kv_get, "stars")
    names = {s["designation"] for s in cat["stars"]}
    assert "FARSTAR" in names and "NOPOS" not in names
    assert cat["sources"]["observatory"] >= 1 and cat["sources"]["observatory_unplaced"] == ["NOPOS"]
    assert cat["sources"]["catalogue"] > 0
    data = client.get("/api/map.json", headers=H).json()
    assert data["sources"]["observatory"] >= 1
    r = client.post("/map/refresh", headers=HX)
    assert "found by your observatories" in r.text and "without a position" in r.text


def test_a_system_we_mine_in_isnt_warded_against_us():
    from rsweb import wards
    stars = {"stars": [{"designation": "AEMEROTH", "has_ward": True}, {"designation": "ITHVALAI", "has_ward": True}]}
    drone = {"device_code": "D", "device_type": "mining_drone", "location": "AEMEROTH-BELT-1", "status": "mining (carbon)"}
    assert wards.foreign(stars, [drone]) == {"ITHVALAI"}


def test_tree_files_carried_devices_under_their_carriers_system():
    """Live 2026-10-07: 13 stowed devices (no location of their own) showed as an empty '?' system."""
    from rsweb.tree import build_tree
    devs = [{"device_code": "HV", "device_type": "heaven_vessel", "location": "FAL-BELT-1", "status": "idle"},
            {"device_code": "S1", "device_type": "survey_drone", "location": None, "status": "stowed", "stowed_in_device_code": "HV"},
            {"device_code": "L1", "device_type": "ftl_slingshot", "location": None, "status": "idle"}]
    systems = {s["star"]: s for s in build_tree(devs, {}, {}, {"HV"})}
    assert systems["FAL"]["counts"]["devices"] == 2 and systems["FAL"]["nodes"][0]["children"][0]["d"]["device_code"] == "S1"
    lost = systems["?"]
    assert lost["counts"]["devices"] == 1 and "no location" in (lost["nodes"] + lost["unknown"])[0]["nowhere"]


def test_ward_or_hub_lock_blocks_contracts_not_trades(client):
    """Species interaction lock: other players can't complete location events where a ward or hub is."""
    import json as _json
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [{"designation": "SOL"},
                                                                        {"designation": "ABOTEIN", "has_hub": True}]})
    client.portal.call(eng.save_fleets, [{"id": "t", "name": "T", "role": "trade", "home": "SOL", "wants": {}}])
    r = client.post("/fleets/t/mission", data={"kind": "contract", "contract": _json.dumps(
        {"designation": "E1", "location": "ABOTEIN-3", "price": {"carbon": 5}})}, headers=HX)
    assert "species interaction lock" in r.text
    r = client.post("/fleets/t/mission", data={"kind": "trade", "trade": _json.dumps(
        {"name": "T1", "trade_code": "X", "location": "ABOTEIN-3", "price": {"carbon": 5}})}, headers=HX)
    assert "species interaction lock" not in r.text and "another player" not in r.text   # trades still go


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


def test_decommission_at_an_autofactory(client):
    from rsweb import decommission as dc
    from rsweb import loadouts as lo
    devs = [{"device_code": "AF1", "device_type": "autofactory", "location": "FAL-1-L4", "status": "idle"},
            {"device_code": "AF2", "device_type": "autofactory", "location": "KEL-2-L4", "status": "idle"},
            {"device_code": "W", "device_type": "system_ward", "location": "KEL-OORT", "status": "idle",
             "tags": ["fleet:k", "home:kel"]}]
    stars = {"FAL": {"position": {"x": 0, "y": 0, "z": 0}}, "KEL": {"position": {"x": 5, "y": 0, "z": 0}}}
    f = dc.factories(devs, stars, "KEL-OORT")
    assert [x["code"] for x in f] == ["AF2", "AF1"]                                 # same system first
    t = dc.retag(devs[2], f[1], lo.to_tag, lo.at_tag)
    assert t == {"add_tags": ["at:fal-1-l4", "to:fal"], "remove_tags": ["fleet:k", "home:kel"]}
    assert dc.retag(devs[2], f[0], lo.to_tag, lo.at_tag)["add_tags"] == ["at:kel-2-l4"]   # already in the system
    e = {"factory": "AF1", "at": "FAL-1-L4"}
    assert dc.ready({"location": "FAL-1-L4", "status": "idle"}, e)
    assert not dc.ready({"location": "FAL-1-L4", "status": "unfurling"}, e) and not dc.ready({"location": "KEL-OORT"}, e)
    # end to end: queue it on the device page, then the pass decommissions it once it's there
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    victim = next(d for d in world.devices if "decommission" in d["available_commands"] and "autofactory" not in d["device_type"])
    factory = next(d for d in world.devices if d["device_type"] == "autofactory")
    page = client.get(f"/devices/{victim['device_code']}", headers=H).text
    assert "Decommission at an autofactory" in page
    r = client.post(f"/devices/{victim['device_code']}/decommission-at", data={"factory": factory["device_code"]}, headers=HX)
    assert "Queued" in r.text
    q = client.portal.call(client.app.state.db.kv_get, dc.KV)
    assert q[victim["device_code"]]["at"] == factory["location"]
    devices = client.portal.call(client.app.state.db.kv_get, "devices")
    for d in devices:
        if d["device_code"] == victim["device_code"]:
            d.update({"location": factory["location"], "status": "idle", "stowed_in_device_code": None})
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    eng = client.app.state.worker.automations
    assert client.portal.call(eng.decommission_queue_pass) == [f"{victim['device_code']} → decommission at {factory['location']}"]
    job = next(j for j in client.portal.call(eng.jobs) if j["rule"] == "decommission")
    assert job["steps"][0]["body"] == {"command": "decommission"}
    assert client.portal.call(eng.decommission_queue_pass) == []                    # not twice


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


def test_my_names_include_the_base_name(client):
    from rsweb import notify
    db = client.app.state.db
    client.portal.call(db.kv_set, "replicants", {"A": {"name": "Sk3y-1"}, "B": {"name": "Sk3y-4"}})
    names = client.portal.call(notify.my_names, db)
    assert names == ["Sk3y", "Sk3y-1", "Sk3y-4"]
    assert all(notify.mentions(t, names) for t in ("hi Sk3y", "Sk3y-4 where are you", "sk3y-1?"))
    assert not notify.mentions("Sk3yNet", names)


def test_galaxy_map_loads_in_two_parts(client):
    client.portal.call(client.app.state.worker.sync_devices)
    core = client.get("/api/map.json", params={"part": "core"}, headers=H).json()
    assert core["stars"] and "per_star" not in core and "fleets" not in core and "drones" not in core["stars"][0]
    assert set(core["stars"][0]) <= {"designation", "name", "position", "color", "spectral_type", "region", "estimated_planets",
                                     "entry_point", "has_hub", "has_ward", "has_life", "explored", "from_census",
                                     "from_observatory", "devices", "infra", "reach", "scanned", "radius",
                                     "radius_measured"}
    over = client.get("/api/map.json", params={"part": "overlay"}, headers=H).json()
    assert "stars" not in over and {"per_star", "moving", "fleets", "supply"} <= set(over)
    assert any(v["drones"] for v in over["per_star"].values())
    full = client.get("/api/map.json", headers=H).json()                      # the old one-shot shape still works
    assert "drones" in full["stars"][0] and "fleets" in full


def test_trade_run_only_waits_once_everything_is_at_the_site():
    from rsweb import fleets as fl
    m = {"contract": {"designation": "E", "location": "SOL-4", "price": {"carbon": 200, "rares": 10}}, "loaded": ["F1"]}
    inv = {"SOL-4": {"carbon": 200}}
    # short of rares and nothing on its way: gather again
    assert fl.deal_check(m, inv, []) == ("regather", {"rares": 10})
    # a vessel flying there with the rares: wait for it
    fr = {"device_code": "F2", "travel": {"destination": "SOL-4"}, "cargo": [{"resource_type": "rares", "quantity": 10}]}
    assert fl.deal_check(m, inv, [fr]) == ("incoming", {"rares": 10})
    # a fleet freighter at the site still holding cargo: deposit again
    at = {"device_code": "F1", "location": "SOL-4", "cargo": {"rares": 10}}
    assert fl.deal_check(m, inv, [at])[0] == "redeliver"
    assert fl.deal_check(m, {"SOL-4": {"carbon": 200, "rares": 10}}, [])[0] == "ok"
    # the fleet's own replicant fulfills (contract template gets it)
    devs = [{"device_code": "HV", "tags": ["fleet:t"]}]
    assert fl.fleet_replicant({"id": "t"}, devs, {"R9": {"hosted_device_code": "XX"}, "R1": {"hosted_device_code": "HV"}})[0] == "R1"
    st = fl.fulfil_step(fl.deal(m), "R1", 'POST /locations/{location}/events/{designation} {"replicant_code": "{replicant}"}')
    assert st["path"] == "/locations/SOL-4/events/E" and st["body"] == {"replicant_code": "R1"}


def test_trail_matches_departure_vectors_to_stars():
    from rsweb import trail as tl
    pos = {"HOME": {"x": 0, "y": 0, "z": 0}, "A": {"x": 4, "y": 2, "z": -3}, "B": {"x": -4, "y": 2, "z": 3},
           "FAR": {"x": 40, "y": 20, "z": -30}, "NEAR": {"x": 0.5, "y": 0, "z": 0}}
    # the blog's example: (0,0,0) → (4,2,-3) gives about (0.7, 0.4, -0.6)
    c = tl.candidates("HOME", "0.7,0.4,-0.6", pos)
    assert c[0]["star"] == "A" and c[0]["good"] and c[0]["in_relay"]
    assert c[1]["star"] == "FAR" and not c[1]["in_relay"]                 # same line, further: second
    assert all(x["star"] not in ("B", "NEAR") for x in c)                 # the wrong way
    assert tl.parse_vector({"x": 3, "y": 4, "z": 0}) == [0.6, 0.8, 0.0]
    audit = {"BC1": [{"id": 2, "travel_type": "departure", "replicant_code": "BILL", "location": "HOME-1",
                      "logged_at": "2026-10-08T02:00:00+00:00", "vector": "0.7,0.4,-0.6", "device_code": "V1"},
                     {"id": 1, "travel_type": "arrival", "replicant_code": "BILL", "location": "HOME-1",
                      "logged_at": "2026-10-08T01:00:00+00:00", "vector": None},
                     {"id": 0, "travel_type": "departure", "replicant_code": "SOMEONE", "location": "HOME-1",
                      "logged_at": "2026-10-08T03:00:00+00:00", "vector": "-1,0,0"}]}
    legs = tl.legs(audit, {"BC1": {"star": "HOME"}}, "BILL", pos)
    assert len(legs) == 1 and legs[0]["best"]["star"] == "A"
    assert tl.last_seen(audit, {"BC1": {"star": "HOME"}}, "BILL")["travel_type"] == "departure"


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


def _ward(code, loc, status="warding"):
    return {"device_code": code, "device_type": "system_ward", "location": loc, "status": status, "features": ["ward"],
            "operational_capacity": 100.0, "available_commands": ["deactivate" if status == "warding" else "activate", "travel"]}


def test_wardhub_hub_shield_projection_and_ward_cap():
    from rsweb import wardhub as wh
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    hub = {"device_code": "HUB1", "device_type": "system_hub", "location": "SOL-5-L4", "status": "active",
           "operational_capacity": 100.0}
    evs = [{"event": "hub.activated", "device_code": "HUB1", "location": "SOL-5-L4", "payload": "{}",
            "created_at": "2026-10-01T00:00:00+00:00"},
           {"event": "hub.maintained", "device_code": "HUB1", "location": "SOL-5-L4",
            "payload": json.dumps({"resources_consumed": {"structural": 50}, "capacity": 80}),
            "created_at": "2026-10-18T00:00:00+00:00"}]
    h = wh.hubs([hub], evs, {"SOL": {"structural": 99}, "SOL-5-L4": {"structural": 20}}, now=now)[0]   # the hub's own spot counts
    assert not h["shielded"] and h["shield_until"].startswith("2026-10-08")
    assert h["capacity"] == 80 and h["projected"] == 60.0 and h["days_left"] == 6.0     # 2 days at −10 %/day
    assert h["short"] == {"structural": 30.0} and h["level"] == "warn"
    # inside the 7 days: shielded, no decay
    h = wh.hubs([hub], evs[:1], {}, now=datetime(2026, 10, 3, tzinfo=timezone.utc))[0]
    assert h["shielded"] and h["projected"] == 100.0 and h["level"] == "ok"
    w = wh.wards([_ward(f"W{i}", f"S{i}-OORT") for i in range(25)] + [_ward("WX", "SOL-OORT", "inactive"), hub,
                                                                     _ward("WH", "SOL-KUIPER")])
    assert w["active"] == 26 and w["free"] == 0 and w["clash"] == ["SOL"]
    assert wh.evicted({"evicted_miners": ["D1", {"device_code": "D2", "owner_name": "Sylphrena"}]}) == [
        {"device_code": "D1"}, {"device_code": "D2", "owner_name": "Sylphrena"}]


def test_wards_page_activate_cap_and_evictions(client):
    world = client.app.state.api.http._transport.app.state.world
    world.devices += [_ward("WA000001", "SOL-OORT", "inactive")] + [_ward(f"WF{i:06d}", f"STAR{i}-OORT") for i in range(24)]
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get("/wards", headers=H).text
    assert "24 of 25 active" in page and "/wards/WA000001/activate" in page
    world.evict_next = [{"device_code": "MD777777", "owner_name": "Sylphrena"}]
    r = client.post("/wards/WA000001/activate", headers=HX)
    assert "Evicted miners: MD777777 (Sylphrena)" in r.text
    notes = client.portal.call(client.app.state.db.fetchall, "SELECT title, link FROM notifications WHERE link='/wards'")
    assert any("MD777777" in n["title"] for n in notes)
    client.portal.call(client.app.state.worker.sync_devices)
    assert "25 of 25 active" in client.get("/wards", headers=H).text
    world.devices.append(_ward("WB000001", "LERNA-OORT", "inactive"))
    client.portal.call(client.app.state.worker.sync_devices)
    r = client.post("/wards/WB000001/activate", headers=HX)
    assert "25 wards are already active" in r.text
    assert client.post("/wards/WB000001/explode", headers=HX).status_code == 404


def test_hub_watch_pass_warns_once(client):
    world = client.app.state.api.http._transport.app.state.world
    world.devices.append({"device_code": "HUB00001", "device_type": "system_hub", "location": "SOL-5-L4", "status": "active",
                          "operational_capacity": 100.0, "available_commands": ["set_welcome_message"]})
    client.portal.call(client.app.state.worker.sync_devices)
    old = datetime.now(timezone.utc) - timedelta(days=12)
    w = client.app.state.worker
    client.portal.call(w.handle_event, {**_ev(901, "hub.activated", device="HUB00001", created=old), "location": "SOL-5-L4"})
    client.portal.call(w.handle_event, {**_ev(902, "hub.maintained", device="HUB00001",
                                              created=datetime.now(timezone.utc) - timedelta(days=4),
                                              resources_consumed={"rares": 500}, capacity=70), "location": "SOL-5-L4"})
    eng = w.automations
    out = client.portal.call(eng.hub_watch_pass)
    assert len(out) == 1 and "capacity about 30 %" in out[0] and "rares" in out[0]
    assert client.portal.call(eng.hub_watch_pass) == []                       # once per state
    page = client.get("/wards", headers=H).text
    assert "HUB00001" in page and "needs maintenance" in page and "down since" in page


def test_auto_scout_surveys_nearest_first_until_worn(client, monkeypatch):
    eng, db = client.app.state.worker.automations, client.app.state.db
    w = client.app.state.worker
    # only SOL in the catalogue: the mock's random stars (re-synced in the background) mustn't compete with NEARA / NEARB
    real_get = db.kv_get

    async def kv_get(key, default=None):
        if key == "stars":
            return {"stars": [{"designation": "SOL", "position": {"x": 0, "y": 0, "z": 0}}]}
        return await real_get(key, default)
    monkeypatch.setattr(db, "kv_get", kv_get)
    stars = [{"designation": d, "position": {"x": x, "y": 0, "z": 0}} for d, x in
             (("NEARA", 2), ("NEARB", 3), ("FARC", 9), ("FARD", 12))]
    client.portal.call(w.handle_event, _ev(950, "prospect.completed", device="OBS00001", origin="SOL", stars_generated=4, stars=stars))
    client.portal.call(eng.save_fleets, [{"id": "x1", "name": "Scouts", "role": "explore", "home": "SOL", "wants": {}},
                                         {"id": "x2", "name": "Lazy", "role": "explore", "home": "SOL", "wants": {}}])
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES('SOL', '{}', '2026-10-08')")
    r = client.post("/fleets/x1/auto-scout", data={"auto_scout": "on"}, headers=HX)
    assert "nearest unsurveyed" in r.text
    devices = client.portal.call(db.kv_get, "devices")
    devices.append({"device_code": "TIRED", "device_type": "survey_drone", "location": "SOL-3", "status": "idle",
                    "operational_capacity": 0.8, "tags": ["fleet:x1"]})
    devices[-1:-1] = [{"device_code": f"RL{s}", "device_type": "ftl_relay", "location": f"{s}-1-L4", "status": "relaying"}
                      for s in ("SOL", "NEARA", "NEARB")]   # a relay commands its own system only: scouts go where one is
    # (SOL's joins them to the replicant there: a network commands only with a replicant in it)
    client.portal.call(db.kv_set, "devices", devices)
    assert client.portal.call(eng.auto_scout_pass) == []                      # 80 %: not until everything is at 85 %
    assert "below 85 %" in next(x for x in client.portal.call(eng.fleets) if x["id"] == "x1")["scout_note"]
    devices[-1]["operational_capacity"] = 0.9
    client.portal.call(db.kv_set, "devices", devices)
    out = client.portal.call(eng.auto_scout_pass)
    assert out == ["Scouts: scouting NEARA"]                                 # the nearest one first, one at a time
    assert client.portal.call(eng.auto_scout_pass) == []                      # busy; Lazy isn't auto-scouting
    nxt, _ = client.portal.call(eng.scout_next, {"id": "x9", "home": "SOL"}, client.portal.call(eng.fleets),
                                client.portal.call(eng.devices))
    assert nxt == "NEARB"                                                     # NEARA is taken by Scouts
    # no replicant aboard: FARC / FARD have no relay of ours — never chosen
    seen, ex = [], {"NEARB"}
    while True:
        nxt, why = client.portal.call(eng.scout_next, {"id": "x9", "home": "SOL"}, client.portal.call(eng.fleets),
                                      client.portal.call(eng.devices), None, set(ex))
        if not nxt:
            break
        seen.append(nxt)
        ex.add(nxt)
    assert "FARC" not in seen and "FARD" not in seen and "relay coverage" in why
    # NEARA done: it takes the next one; a member worn to 50 % ends the run (it finishes that system and goes home)
    items = client.portal.call(eng.fleets)
    m = items[0]["mission"]
    m.update({"phase": "recall", "job": None})
    client.portal.call(eng.save_fleets, items)
    client.portal.call(eng.run_fleets)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["targets"] == ["NEARA", "NEARB"] and any("on to NEARB" in str(x) for x in m["log"])
    devices = client.portal.call(db.kv_get, "devices")
    devices.append({"device_code": "WORN", "device_type": "survey_drone", "location": "NEARB-1", "status": "idle",
                    "operational_capacity": 0.5, "tags": ["fleet:x1"]})
    client.portal.call(db.kv_set, "devices", devices)
    items = client.portal.call(eng.fleets)
    items[0]["mission"].update({"phase": "recall", "job": None, "idx": 1})
    client.portal.call(eng.save_fleets, items)
    client.portal.call(eng.run_fleets)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["targets"] == ["NEARA", "NEARB"] and m["scout_ended"] and any("for repairs" in str(x) for x in m["log"])


def test_profile_edit_and_reputation(client):
    from rsweb.web_profile import profile_changes
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    rep = next(iter(client.portal.call(client.app.state.db.kv_get, "replicants")))
    page = client.get(f"/replicants/{rep}", headers=H).text
    assert f"/replicants/{rep}/profile" in page and f"/replicants/{rep}/reputation" in page
    r = client.post(f"/replicants/{rep}/profile", data={"name": "bob-1", "orig_name": "bob-1", "pronouns": "they/them",
                                                        "orig_pronouns": "", "plan": "", "orig_plan": ""}, headers=HX)
    assert world.profile_patches[-1] == {"code": rep, "pronouns": "they/them"}       # only what changed
    r = client.post(f"/replicants/{rep}/profile", data={"name": "Bill", "orig_name": "bob-1"}, headers=HX)
    assert "Name already taken" in r.text
    assert "Nothing changed" in client.post(f"/replicants/{rep}/profile", data={"name": "x", "orig_name": "x"}, headers=HX).text
    assert profile_changes({"pronouns": "x" * 51, "orig_pronouns": ""})[1]
    assert profile_changes({"name": "", "orig_name": "bob"})[1]
    reps = client.portal.call(client.app.state.db.kv_get, "replicants")
    other = next((c for c in reps if c != rep), None)
    text = json.dumps({rep: {"name": "Quill_Ferry", "plan": "Haul relays", "pronouns": "they/them"},
                       "NOPE1234": {"name": "Ghost"}, **({other: {"description": "x" * 501}} if other else {})})
    r = client.post("/profiles/preview", data={"profiles": text}, headers=HX).text
    assert "Apply 1 profile" in r and "not one of your replicants" in r and "unchanged: pronouns" in r
    if other:
        assert "501 characters" in r
    r = client.post("/profiles/import", data={"profiles": text}, headers=HX).text
    assert "saved" in r and world.profile_patches[-1] == {"code": rep, "name": "Quill_Ferry", "plan": "Haul relays"}
    assert client.portal.call(client.app.state.db.kv_get, "replicants")[rep]["name"] == "Quill_Ferry"
    assert "not valid JSON" in client.post("/profiles/preview", data={"profiles": "{"}, headers=HX).text
    r = client.post("/profiles/preview", data={"profiles": json.dumps({"Quill_Ferry": {"name": "Bill"}})}, headers=HX).text
    assert "Apply 1 profile" in r
    assert "Name already taken" in client.post("/profiles/import", data={"profiles": json.dumps({rep: {"name": "Bill"}})},
                                                headers=HX).text
    assert "Bulk profile import" in client.get("/reputation", headers=H).text
    assert "acquainted" in client.get(f"/replicants/{rep}/reputation", headers=HX).text
    page = client.get("/reputation", headers=H).text
    assert "Veth" in page and "intelligent" in page and "curious, aquatic" in page and "events completed" in page


# --- regressions from the 2026-10-08 code review -------------------------------------------------------------------
def test_review_api_blocklist_dot_segments_and_write_retries():
    for path in ("/accounts/./me", "/accounts/me/.", "/accounts/x/../me", "/v1/Accounts/Me", "/accounts%2fme"):
        with pytest.raises(ApiError):
            RSClient.check_allowed("DELETE", path)
    RSClient.check_allowed("GET", "/devices/ABCD/audit")
    from rsweb.api import retry_after
    assert retry_after("7", 5) == 7 and retry_after("garbage", 5) == 5
    assert retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 5) == 0      # a date in the past: go now

    calls = []

    def handler(req):
        calls.append(req.method)
        raise httpx.ReadTimeout("slow", request=req)
    s = Settings(api_token="t", api_base="http://x/v1")
    c = RSClient(s, transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError):
        asyncio.run(c.request("POST", "/devices/A", json_body={"command": "travel"}, retries=2))
    assert calls == ["POST"]                         # a command that timed out may have happened: never resent


def test_review_device_snapshot_gone_and_persistent_short_list():
    from rsweb.ingest import merge_device_snapshot
    prev = [{"device_code": c, "location": "SOL-3"} for c in ("A", "B", "C")]
    new = [{"device_code": "A", "location": "SOL-3"}]
    assert merge_device_snapshot(prev, new) is None                          # a glitchy short list is ignored…
    assert [d["device_code"] for d in merge_device_snapshot(prev, new, gone={"B", "C"})] == ["A"]   # …not real losses
    assert merge_device_snapshot(prev, new, accept_short=True) is not None  # and the same short list again is the truth


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


def test_review_reform_leaves_fleets_on_a_mission_alone():
    from rsweb import reform
    devices = [{"device_code": "M1", "device_type": "mining_drone", "location": "AAA-5-L4", "status": "idle",
                "tags": ["fleet:prospectors"]}]
    fleets = [{"id": "prospectors", "name": "P", "home": "BBB", "wants": {}, "mission": {"status": "running"}}]
    p = reform.plan({"phases": [], "ignore_tags": []}, devices, fleets, busy=set(), hosts=set())
    assert not p["retag"] and p["skipped"][0]["code"] == "M1"


def test_review_prospects_transit_and_star_prefixes():
    from rsweb import prospects, targets, transit
    assert prospects.VERDICT.get("learning", 8) == 8 and prospects.VERDICT["consider moving"] == 3
    d = {"device_code": "A", "travel": {"departed_at": "2026-10-08T00:00:00", "arrives_at": "2026-10-08T01:00:00",
                                        "route": [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}]}}
    legs = transit.trip(d, now=0)["legs"]
    assert [round(x["t1"] - x["t0"]) for x in legs] == [1800, 1800]          # not both at the destination at once
    assert targets.in_star("KEL-3", "KEL") and not targets.in_star("KELMORNEA-3", "KEL")


def test_review_escaping(client):
    r = client.get("/print-queue/locations?dest_star=<img src=x onerror=alert(1)>", headers=H)
    assert "<IMG" not in r.text and "&lt;IMG" in r.text
    r = client.get("/devices/x');alert(1);('/command-form?command=collect_resources", headers=H)
    assert "clearCargo('" not in r.text


def test_review_engine_stage_failure_is_isolated(client):
    eng = client.app.state.worker.automations
    ran = []

    async def boom():
        raise ValueError("bad template")

    async def fine():
        ran.append(1)
        return []
    eng.run_fleets, eng.hub_watch_pass = boom, fine
    client.portal.call(eng.tick)
    client.portal.call(eng.tick)
    assert ran == [1, 1]                                                       # later stages still ran, each tick
    notes = client.portal.call(client.app.state.db.fetchall, "SELECT title FROM notifications WHERE title LIKE '%run_fleets%'")
    assert len(notes) == 1                                                     # said once, not every minute


def test_dry_belt_rests_controller_and_relaunches_with_its_directive(client):
    """Live 2026-10-08 (LORSELAN): eight drones mined out five small sites in eight minutes; a ten-minute-old belt read
    still listed them, the controller was relaunched onto nothing and logged ami_overheat (heat 2.0, −0.05 % / 20 s)."""
    eng, db, w = client.app.state.worker.automations, client.app.state.db, client.app.state.worker
    C = {"device_code": "C1000001", "device_type": "ami_mining_controller", "location": "LOR-BELT-1", "status": "coordinating",
         "features": ["ami", "cruise", "stow"], "in_control_range": True, "ami_directive_status": "active",
         "available_commands": ["set_directive", "clear_directive", "launch", "adopt"],
         "ami_directive": {"name": "gather_resources", "config": {"structural": 100},
                           "_eval_state": "exhausted:['structural']:LOR-BELT-1"}}
    drones = [{"device_code": f"D100000{i}", "device_type": "mining_drone", "location": "LOR-BELT-1", "status": "idle",
               "controller_device_code": "C1000001"} for i in range(2)]
    client.portal.call(db.kv_set, "devices", [C] + drones)
    client.portal.call(db.kv_set, "loc:LOR-BELT-1", {"resource_sites": [{"designation": "LOR-BELT-1-SITE-2"}]})
    read = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    client.portal.call(db.kv_set, "belt_reads", {"LOR-BELT-1": read})

    async def enable():
        s = await eng.settings()
        s["rules"]["salvage_when_depleted"]["enabled"] = True
        await eng.save_settings(s)
    client.portal.call(enable)
    assert client.portal.call(eng.open_sites_now, "LOR-BELT-1") == 1
    client.portal.call(w.handle_event, {**_ev(961, "site.depleted", device="D1000000", site="LOR-BELT-1-SITE-2"),
                                        "location": "LOR-BELT-1"})   # mined out after the read
    assert client.portal.call(eng.open_sites_now, "LOR-BELT-1") == 0
    assert client.portal.call(eng.belt_open_sites, ["LOR-BELT-1"]) == {"LOR-BELT-1": 0}
    assert client.portal.call(eng.rule_back_to_belt, {"back_to_belt": True}) == []     # no relaunch onto nothing
    out = client.portal.call(eng.rest_dry_controllers)
    assert out == ["C1000001 rests at LOR-BELT-1"]
    job = next(j for j in client.portal.call(eng.jobs) if j["device"] == "C1000001")
    assert job["steps"][0]["body"] == {"command": "clear_directive"}
    rested = client.portal.call(db.kv_get, "rested")
    assert rested["C1000001"]["directive"] == "gather_resources" and rested["C1000001"]["belt"] == "LOR-BELT-1"
    # the directive is cleared; later the survey drones open a site: relaunched with what it ran before
    C2 = {**C, "ami_directive": {"name": None, "config": None, "_eval_state": None}, "ami_directive_status": None}
    client.portal.call(db.kv_set, "devices", [C2] + drones)
    client.portal.call(db.kv_set, "automation_jobs", [])
    assert client.portal.call(eng.rest_dry_controllers) == []                     # still resting, not forgotten
    client.portal.call(db.kv_set, "loc:LOR-BELT-1", {"resource_sites": [{"designation": "LOR-BELT-1-SITE-3"}]})
    client.portal.call(db.kv_set, "belt_reads", {"LOR-BELT-1": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    done = client.portal.call(eng.rule_back_to_belt, {"back_to_belt": True})
    assert done == ["C1000001 → LOR-BELT-1"]
    job = next(j for j in client.portal.call(eng.jobs) if j["device"] == "C1000001")
    assert "rested while LOR-BELT-1 was dry" in job["title"]
    sd = next(s for s in job["steps"] if (s.get("body") or {}).get("command") == "set_directive")
    assert sd["body"]["directive"] == "gather_resources" and sd["body"].get("configuration") == {"structural": 100}
    assert "C1000001" not in (client.portal.call(db.kv_get, "rested") or {})


def test_pathing_choose_never_picks_an_excluded_system():
    from rsweb import pathing as pa
    xyz = {"HOME": (0, 0, 0), "GOOD": (5, 0, 0), "BEST_BEHIND": (-5, 0, 0), "OTHERS": (4, 1, 0), "NEXT": (4, -1, 0),
           "TARGET": (3, 0, 1), "WARD": (4, 0, 0), "FAR": (12, 0, 0), "UNSCANNED": (2, 0, 0)}
    relay = [{"device_code": f"R{s}", "device_type": "ftl_relay", "location": f"{s}-5-L4", "status": "relaying"}
             for s in ("HOME", "GOOD", "BEST_BEHIND", "OTHERS", "NEXT", "TARGET", "WARD", "UNSCANNED")]   # not FAR
    fleet = {"id": "me", "name": "Me", "role": "mining", "station": True, "home": "HOME"}
    fleets = [fleet, {"id": "o1", "name": "Other", "role": "mining", "home": "OTHERS"},
              {"id": "o2", "name": "Mover", "role": "mining", "home": "ELSEWHERE", "next_home": "NEXT"},
              {"id": "o3", "name": "Prospector", "role": "mining", "home": "ELSEWHERE2",
               "mission": {"status": "running", "targets": ["TARGET-BELT-1"]}}]
    rows = [{"star": s, "score": sc, "status": st, "distance": 1.0, "reasons": []} for s, sc, st in (
        ("BEST_BEHIND", 99, "ok"), ("OTHERS", 95, "ok"), ("NEXT", 94, "ok"), ("TARGET", 93, "ok"), ("WARD", None, "warded"),
        ("FAR", 92, "ok"), ("UNSCANNED", None, "unscanned"), ("HOME", 98, "ok"), ("GOOD", 50, "ok"))]
    heading = pa.heading_from("custom", None, custom=[1, 0, 0])
    star, why = pa.choose(fleet, fleets, rows, xyz, relay, heading, 60)
    assert star == "GOOD" and why[0] == "score 50"
    assert pa.choose(fleet, fleets, rows, xyz, relay, None, 60)[0] == "BEST_BEHIND"     # no heading: best anywhere served
    none, reasons = pa.choose(fleet, fleets, [r for r in rows if r["star"] != "GOOD"], xyz, relay, heading, 60)
    assert none is None and any("held by another fleet" in r for r in reasons) and any("outside relay" in r for r in reasons)
    # owner 2026-10-19: no automatic assignments outside the network — a relay without a replicant on its network
    # doesn't count, only the commandable systems do
    assert pa.choose(fleet, fleets, rows, xyz, relay, heading, 60, reach={"HOME", "BEST_BEHIND"})[0] is None
    assert pa.choose(fleet, fleets, rows, xyz, relay, None, 60, reach={"HOME", "GOOD"})[0] == "GOOD"
    assert pa.dry("HOME", [], [], {"HOME": "x"}, set(), []) and not pa.dry("HOME", [], [], {}, set(), ["HOME-BELT-1"])
    assert pa.dry("HOME", [], [{"star": "HOME", "verdict": "consider moving"}], {}, set(), ["HOME-BELT-1"])
    assert pa.heading_from("outward", (3, 4, 0)) == [0.6, 0.8, 0.0]


def test_pathing_pass_prospects_then_relocates(client):
    from rsweb import prospects
    eng, db = client.app.state.worker.automations, client.app.state.db

    async def commandable(devices, stars=None, without=None):   # a replicant on the relay network: every system it covers
        return {"HOMEA", "AHEADB", "BEHINDC"}
    eng.commandable = commandable
    stars = {"HOMEA": (0, 0, 0), "AHEADB": (5, 0, 0), "BEHINDC": (-5, 0, 0)}
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": k, "position": dict(zip("xyz", v))} for k, v in stars.items()]})
    belt = {"asteroid_belt": {"belts": [{"designation": "X", "density": "dense", "resources": {"structural": "rich", "rares": "high"}}]}}
    for s in ("AHEADB", "BEHINDC"):
        client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                           (s, json.dumps(belt), "2026-10-08T00:00:00+00:00"))
    devices = [{"device_code": "OBS00001", "device_type": "galactic_observatory", "location": "HOMEA-5-L4", "status": "idle",
                "tags": ["fleet:m1"], "available_commands": ["prospect", "compact", "unfurl"]},
               {"device_code": "REL00001", "device_type": "ftl_relay", "location": "HOMEA-5-L4", "status": "relaying", "tags": []}]
    devices += [{"device_code": f"REL{s}", "device_type": "ftl_relay", "location": f"{s}-5-L4", "status": "relaying", "tags": []}
                for s in ("AHEADB", "BEHINDC")]
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(eng.save_fleets, [{"id": "m1", "name": "Miner 1", "role": "mining", "home": "HOMEA", "station": True, "wants": {}}])
    r = client.post("/fleets/m1/path", data={"heading_kind": "star", "heading_star": "AHEADB", "cone": "60",
                                             "auto_relocate": "on", "auto_prospect": "on"}, headers=HX)
    assert r.status_code == 200
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "m1")
    assert f["heading"]["vector"] == [1.0, 0.0, 0.0] and f["auto_relocate"] and f["auto_prospect"]
    prospects.forget_inputs()
    out = client.portal.call(eng.observatory_pass)
    assert out == ["OBS00001: prospecting from HOMEA, direction 1"]        # along the heading first
    job = next(j for j in client.portal.call(eng.jobs) if j["rule"] == "observatory")
    assert job["steps"][0]["body"] == {"command": "prospect", "direction": [1.0, 0.0, 0.0]}
    # the home runs dry (no belt, no salvage) — after 2 h the fleet picks the system ahead, then moves after the grace
    client.portal.call(db.kv_set, "salvage_state", {"nothing": {"HOMEA": datetime.now(timezone.utc).isoformat()}})
    client.portal.call(eng.pathing_pass, True)
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "m1")
    assert f["depleted_since"] and not f.get("next_home")
    items = client.portal.call(eng.fleets)
    for x in items:
        if x["id"] == "m1":
            x["depleted_since"] = "2026-10-01T00:00:00+00:00"
    client.portal.call(eng.save_fleets, items)
    out = client.portal.call(eng.pathing_pass, True)
    assert out == ["Miner 1: next home AHEADB"]                            # not BEHINDC, though it scores the same
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Next home <b><a href=\"/systems/AHEADB\">AHEADB</a></b>" in page and "/fleets/m1/next-home" in page
    r = client.post("/fleets/m1/next-home", data={"action": "now"}, headers=HX)
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "m1")
    assert f["home"] == "AHEADB" and f["prev_home"] == "HOMEA" and not f.get("next_home")
    notes = client.portal.call(db.fetchall, "SELECT title FROM notifications WHERE link='/fleets'")
    assert any("moving home: HOMEA → AHEADB" in n["title"] for n in notes)


def test_renaming_a_fleet_moves_its_tag(client):
    world = client.app.state.api.http._transport.app.state.world
    eng, db, w = client.app.state.worker.automations, client.app.state.db, client.app.state.worker
    for d in world.devices[:2]:
        d["tags"] = ["fleet:ithvalai-home", "keepme"]
    client.portal.call(w.sync_devices)
    codes = [d["device_code"] for d in world.devices[:2]]
    client.portal.call(eng.save_fleets, [{"id": "ithvalai-home", "name": "Miner 1", "role": "mining", "home": "SOL", "station": True, "wants": {}},
                                         {"id": "hub", "name": "Hub", "role": "mining", "home": "AAA", "wants": {}, "materials": "ithvalai-home"}])
    client.portal.call(db.kv_set, "loadout_orders", [{"fleet": "ithvalai-home", "star": "SOL", "device_type": "mining_drone"}])
    r = client.post("/fleets/ithvalai-home/edit", data={"name": "Larselan Miners", "home": "SOL"}, headers=HX)
    items = client.portal.call(eng.fleets)
    assert {f["id"] for f in items} == {"larselan-miners", "hub"}
    assert next(f for f in items if f["id"] == "hub")["materials"] == "larselan-miners"
    assert client.portal.call(db.kv_get, "loadout_orders")[0]["fleet"] == "larselan-miners"
    devs = {d["device_code"]: d for d in client.portal.call(db.kv_get, "devices")}
    assert all("fleet:larselan-miners" in devs[c]["tags"] and "keepme" in devs[c]["tags"] for c in codes)   # at once
    client.portal.call(w.sync_devices)                     # the game still has the old tag: still shown as the new one
    devs = {d["device_code"]: d for d in client.portal.call(db.kv_get, "devices")}
    assert all(devs[c]["_retag"] == "fleet:ithvalai-home" and "fleet:ithvalai-home" not in devs[c]["tags"] for c in codes)
    assert client.portal.call(eng.fleet_rename_pass) == ["retag 2"]
    import time
    for _ in range(50):
        if all("fleet:larselan-miners" in d["tags"] for d in world.devices[:2]):
            break
        time.sleep(0.1)
    assert all("fleet:larselan-miners" in d["tags"] and "fleet:ithvalai-home" not in d["tags"] for d in world.devices[:2])
    client.portal.call(w.sync_devices)
    assert client.portal.call(db.kv_get, "fleet_renames") == {}               # done: nothing carries the old tag
    assert not any(d.get("_retag") for d in client.portal.call(db.kv_get, "devices"))


def test_surveyed_checkboxes_then_system_indicator(client):
    from rsweb.automations import survey_targets
    db = client.app.state.db
    page = client.get("/systems/SOL", headers=H).text
    row = client.portal.call(db.fetchone, "SELECT data FROM systems WHERE star='SOL'")
    bodies = [t["target"] for t in survey_targets(json.loads(row["data"]), {}, False, True, 99)]
    assert bodies and "/systems/SOL/surveyed" in page and "system surveyed" not in page
    for b in bodies[:-1]:
        r = client.post("/systems/SOL/surveyed", data={"body": b, "on": "1"}, headers=HX)
        assert "✓" in r.text and "HX-Refresh" not in r.headers
    r = client.post("/systems/SOL/surveyed", data={"body": bodies[0]}, headers=HX)   # untick one
    assert bodies[0] not in client.portal.call(db.kv_get, "surveyed")
    client.post("/systems/SOL/surveyed", data={"body": bodies[0], "on": "1"}, headers=HX)
    r = client.post("/systems/SOL/surveyed", data={"body": bodies[-1], "on": "1"}, headers=HX)
    assert r.headers.get("HX-Refresh") == "true"                                    # the last one: whole system
    page = client.get("/systems/SOL", headers=H).text
    assert "✓ system surveyed" in page and "/systems/SOL/surveyed" not in page
    assert client.post("/systems/SOL/surveyed", data={"body": "LERNA-3", "on": "1"}, headers=HX).status_code == 400


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


def test_supply_range_keeps_prints_and_spares_near_the_fleet():
    from rsweb import loadouts as lo
    D = lambda code, t, loc, **kw: {"device_code": code, "device_type": t, "location": loc, "status": "idle",  # noqa: E731
                                    "operational_capacity": 100.0, **kw}
    devices = [D("AF1", "autofactory", "FALQ-3-L4", available_commands=["enqueue_print"]),
               D("SP1", "mining_drone", "FALQ-BELT-1", tags=["spare"])]
    bps = [{"device_type": "mining_drone", "resources": {"structural": 10}, "print_time": 60},
           {"device_type": "autofactory", "resources": {"structural": 100}, "print_time": 600, "queue_size": 10}]
    inv = {"FALQ-3-L4": {"structural": 1000}}
    stars = {k: {"position": {"x": x, "y": 0, "z": 0}} for k, x in (("FALQ", 0), ("SOL", 40), ("NEAR", 5))}

    def run(home, reach):
        cfg = lo.normalize({"phases": [], "settings": {"max_supply_ly": reach}, "fleets_migrated": True,
                            "fleets": [{"id": "f1", "name": "F1", "role": "mining", "home": home, "station": True,
                                        "wants": {"mining_drone": 2}}]})
        return lo.plan(cfg, devices, bps, inv, stars, {}, set(), [], {})
    far = run("SOL", 20)
    assert not far["prints"] and not far.get("moves")
    assert any("no autofactory within 20 ly of SOL" in u["why"] for u in far["unmet"])
    near = run("NEAR", 20)
    assert near["prints"] and near["prints"][0]["factory"] == "AF1"
    assert run("SOL", 0)["prints"]                                     # 0 = any distance, as before
    from rsweb.logistics import SECTOR_RANGE_LY
    assert lo.normalize({})["settings"]["max_supply_ly"] == SECTOR_RANGE_LY   # the default: the sector range
    assert lo.normalize({"settings": {"max_supply_ly": 15}})["settings"]["max_supply_ly"] == SECTOR_RANGE_LY   # old defaults
    assert lo.normalize({"settings": {"max_supply_ly": 100, "supply_range_v2": True}})["settings"]["max_supply_ly"] == SECTOR_RANGE_LY
    kept = lo.normalize({"settings": {"max_supply_ly": 15, "supply_range_v2": True, "supply_range_v3": True}})["settings"]["max_supply_ly"]
    assert kept == 15                                                  # chosen after the change: kept


def test_replicant_vessel_in_a_fleet_counts_toward_its_loadout():
    """Live 2026-10-08: the SOL fleet wants 1 heaven_vessel and has one (hosting a replicant); a second was printed."""
    from rsweb import loadouts as lo
    devices = [{"device_code": "E1029CB0", "device_type": "heaven_vessel", "location": "SOL-3-1", "status": "idle",
                "tags": ["fleet:sol-sector"], "operational_capacity": 100.0},
               {"device_code": "AF1", "device_type": "autofactory", "location": "SOL-3-L4", "status": "idle",
                "available_commands": ["enqueue_print"], "operational_capacity": 100.0}]
    bps = [{"device_type": "heaven_vessel", "resources": {"structural": 10}, "print_time": 60}]
    cfg = lo.normalize({"phases": [], "fleets_migrated": True, "settings": {},
                        "fleets": [{"id": "sol-sector", "name": "Sol Sector", "role": "explore", "home": "SOL", "station": True,
                                    "wants": {"heaven_vessel": 1}}]})
    p = lo.plan(cfg, devices, bps, {"SOL-3-L4": {"structural": 999}}, {}, {"E1029CB0": "D9351B81"}, set(), [], {})
    assert not p["prints"]
    row = next(r for r in p["report"]["sol-sector"]["rows"] if r["type"] == "heaven_vessel")
    assert row["have"] == 1 and row["short"] == 0 and row["riders"] == 1
    assert "E1029CB0" not in str(p.get("retag") or "") and "E1029CB0" not in (p.get("moves") or {})
    cfg["fleets"][0]["wants"] = {"heaven_vessel": 2}                 # wanting two: one more is printed
    p = lo.plan(lo.normalize(cfg), devices, bps, {"SOL-3-L4": {"structural": 999}}, {}, {"E1029CB0": "D9351B81"}, set(), [], {})
    assert p["prints"] and p["prints"][0]["n"] == 1


def test_replicant_vessel_follows_its_fleet_to_a_new_home(client):
    eng, db = client.app.state.worker.automations, client.app.state.db
    reps = client.portal.call(db.kv_get, "replicants")
    rep = next(iter(reps))
    host = reps[rep]["hosted_device_code"]
    devices = client.portal.call(db.kv_get, "devices")
    for d in devices:
        if d["device_code"] == host:
            d["tags"] = (d.get("tags") or []) + ["fleet:m1"]
            here = d["location"].split("-")[0]
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(eng.save_fleets, [{"id": "m1", "name": "Miner 1", "role": "mining", "home": here, "station": True, "wants": {}}])
    assert client.portal.call(eng.rider_pass) == []                              # no home change: nothing to do
    items = client.portal.call(eng.fleets)
    items[0]["home"] = "LERNA"
    client.portal.call(eng.save_fleets, items)                                   # e.g. the fleet chose its next home
    out = client.portal.call(eng.rider_pass)
    assert out == [f"{host} → LERNA"]
    job = next(j for j in client.portal.call(eng.jobs) if j["device"] == host and j["rule"] == "fleets")
    assert job["steps"][0]["path"] == f"/replicants/{rep}/travel" and job["steps"][0]["body"] == {"destination": "LERNA"}
    for d in devices:   # arrived: the follow-up is done
        if d["device_code"] == host:
            d["location"] = "LERNA-3"
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(db.kv_set, "automation_jobs", [])
    client.portal.call(eng.rider_pass)
    assert client.portal.call(db.kv_get, "rider_moves") == {}


def test_edit_tags_of_a_print_in_progress(client):
    world = client.app.state.api.http._transport.app.state.world
    eng, db, w = client.app.state.worker.automations, client.app.state.db, client.app.state.worker
    client.portal.call(db.kv_set, "loadout_orders", [{"star": "SOL", "fleet": "sol-sector", "device_type": "mining_drone",
                                                       "factory": "AF00BEEF", "at": "2026-10-08T11:55:20+00:00"}])
    r = client.post("/devices/AF00BEEF/print-tags", data={"device_type": "mining_drone", "original": "to:sol, fleet:sol-sector",
                                                          "tags": "fleet:falquoryx-home, keepme"}, headers=HX)
    assert "applied when printed" not in r.text or "will be tagged fleet:falquoryx-home, keepme" in r.text
    assert client.portal.call(db.kv_get, "loadout_orders") == []              # no longer bound for SOL: not incoming there
    edits = client.portal.call(db.kv_get, "print_tag_edits")
    assert edits["AF00BEEF"][0]["to"] == ["fleet:falquoryx-home", "keepme"]
    new = world.devices[2]
    new["tags"] = ["to:sol", "fleet:sol-sector"]                              # as the game prints it
    client.portal.call(w.handle_event, {**_ev(990, "print.completed", device="AF00BEEF", device_type="mining_drone",
                                                new_device_code=new["device_code"], tags=["to:sol", "fleet:sol-sector"]),
                                        "device_type": "autofactory"})
    assert sorted(new["tags"]) == ["fleet:falquoryx-home", "keepme"]
    assert not client.portal.call(db.kv_get, "print_tag_edits")
    # re-aimed at another system instead: the order follows
    client.portal.call(db.kv_set, "loadout_orders", [{"star": "SOL", "fleet": "sol-sector", "device_type": "mining_drone",
                                                       "factory": "AF00BEEF"}])
    client.post("/devices/AF00BEEF/print-tags", data={"device_type": "mining_drone", "original": "to:sol, fleet:sol-sector",
                                                      "tags": "to:lerna, fleet:lerna-miners"}, headers=HX)
    o = client.portal.call(db.kv_get, "loadout_orders")[0]
    assert o["star"] == "LERNA" and o["fleet"] == "lerna-miners"
    world.queues["AF00BEEF"] = [{"device_type": "mining_drone", "tags": ["to:sol", "fleet:sol-sector"]}]   # something queued
    page = client.get("/devices/AF00BEEF/print-queue", headers=HX).text
    assert "/devices/AF00BEEF/print-tags" in page and "applied when printed" in page


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


def test_crew_riding_in_another_fleets_vessel_is_collected():
    """Live 2026-10-08: the Surveyors' survey controller and drones were stowed in Trader_1's heaven vessel in AEMEROTH;
    the mission left FALQUORYX without them and surveyed AEMEROTH instead of LYRHYRAN."""
    from rsweb import fleets as fl
    S = lambda **kw: {"status": "idle", "tags": [], **kw}  # noqa: E731
    devices = [S(device_code="CV", device_type="cargo_vessel", location="FAL-BELT-1", tags=["fleet:surv"],
                 features=["surge", "cruise", "attach", "transport"], stow_capacity=50, attach_capacity=3, cargo_capacity=200),
               S(device_code="HV", device_type="heaven_vessel", location="AEM-2", tags=["fleet:trader"],
                 features=["surge", "cruise"], stow_capacity=10),
               S(device_code="SC", device_type="ami_survey_controller", location=None, status="stowed",
                 stowed_in_device_code="HV", tags=["fleet:surv"], features=["ami", "cruise", "stow"]),
               S(device_code="SD", device_type="survey_drone", location=None, status="stowed", stowed_in_device_code="HV",
                 tags=["fleet:surv"], features=["cruise", "survey", "stow"])]
    fleet = {"id": "surv", "name": "Surveyors", "role": "explore", "home": "FAL", "wants": {}}
    stars = {k: {"position": {"x": x, "y": 0, "z": 0}, "entry_point": f"{k}-5-L4"} for k, x in (("FAL", 0), ("AEM", 3))}
    plan = fl.gather_plan(fleet, devices, stars, set())
    assert [s for s, _ in plan["tour"]] == ["AEM"] and {d["device_code"] for d in plan["tour"][0][1]} == {"SC", "SD"}
    steps = fl.gather_steps(fleet, plan, stars)
    descs = [s["desc"] for s in steps]
    assert descs[0].startswith("CV → AEM") and "SC: deploy out of HV" in descs and "SD: deploy out of HV" in descs
    assert descs.index("SC: deploy out of HV") < next(i for i, x in enumerate(descs) if x.startswith("stow SC in CV"))
    # in the same system as the fleet's carrier: assemble deploys them out of the other vessel and boards them
    devices[1]["location"] = "FAL-BELT-1"
    steps, problems = fl.assemble_steps(fleet, devices)
    descs = [s["desc"] for s in steps]
    assert not problems and "SC: deploy out of HV" in descs and "stow SC in CV" in descs
    assert descs.index("SC: deploy out of HV") < descs.index("stow SC in CV")


def test_survey_without_controller_isnt_done_between_bodies():
    """Live 2026-10-08 (DABAH): auto-survey drove the drone moon by moon; caught idle between two scans, the mission
    called the survey done and its recall failed with 'Cannot cruise while scanning'."""
    from rsweb import fleets as fl
    f = {"id": "sol", "name": "Sol Sector", "role": "explore", "home": "SOL", "wants": {}}
    drone = {"device_code": "SD", "device_type": "survey_drone", "location": "DABAH-1-5", "status": "idle", "tags": ["fleet:sol"]}
    done, why, upd = fl.watch_done(f, {}, [drone], "2026-10-08T13:39:55+00:00", busy={"SD"})   # auto-survey's job runs it
    assert not done and upd["idle_since"] is None
    done, why, upd = fl.watch_done(f, {}, [drone], "2026-10-08T13:39:55+00:00", busy=set())
    assert not done and upd["idle_since"] == "2026-10-08T13:39:55+00:00"                       # a pause, not the end yet
    assert not fl.watch_done(f, upd, [{**drone, "status": "scanning"}], "2026-10-08T13:41:00+00:00")[0]
    done, why, _ = fl.watch_done(f, upd, [drone], "2026-10-08T13:43:00+00:00", busy=set())
    assert done and why == "drones finished"                                                    # idle 3 min: really done


def test_loadouts_leave_contract_devices_alone():
    from rsweb import loadouts as lo
    cfg, devices, bps, inv, stars = _lo_world()
    devices.append({"device_code": "CS", "device_type": "survey_drone", "location": "BBB-2", "status": "idle",
                    "tags": ["contract:bbb-evt-1", "at:bbb-2"], "available_commands": ["travel"]})
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    b = {r["type"]: r for r in _rep(p, "BBB")["rows"]}
    assert b["survey_drone"]["have"] == 1                       # BS only: the contract's drone isn't the fleet's
    assert "CS" not in p["tag_add"] and "CS" not in p["moves"]  # neither joined nor made spare nor gathered


def test_contract_device_delivery_waits_for_print_authorization(client):
    eng, db, w = client.app.state.worker.automations, client.app.state.db, client.app.state.worker
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-BELT-1", "items": [{"resource_type": "carbon", "quantity": 900}]}])
    client.portal.call(db.kv_set, "blueprints", [{"device_type": "ftl_relay", "resources": {"carbon": 40}, "print_time": 600}])
    client.portal.call(w.handle_event, {**_ev(997, "event.discovered", designation="AEM-2-EVT-001", location="AEM-2",
                                                title="Relay Network", criteria=[{"name": "default", "resources": {"carbon": 50},
                                                                                  "devices": [{"device_type": "ftl_relay", "quantity": 2}]}]),
                                        "location": "AEM-2"})
    client.post("/game-events/approve", data={"key": "body:AEM-2", "on": "1"}, headers=HX)   # its species approved
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": "S1", "device_type": "ftl_relay", "location": "SOL-3", "status": "idle", "tags": ["spare"]},
                {"device_code": "F1", "device_type": "ftl_relay", "location": "SOL-3", "status": "idle", "tags": ["fleet:x"]},
                {"device_code": "AF9", "device_type": "autofactory", "location": "SOL-BELT-1", "status": "idle",
                 "available_commands": ["enqueue_print"], "tags": []}]
    client.portal.call(db.kv_set, "devices", devices)
    assert client.portal.call(eng.contract_supply_pass, True) == []    # nothing happens until you ask for it
    client.post("/game-events/AEM-2-EVT-001/supply", data={"action": "start"}, headers=HX)
    jobs = [j for j in client.portal.call(eng.jobs) if j["rule"] == "contracts"]
    assert len(jobs) == 1 and "S1" in jobs[0]["title"]                 # the spare goes; the fleet's relay doesn't
    tags = jobs[0]["steps"][0]["body"]["configuration"]
    assert set(tags["add_tags"]) == {"to:aem", "at:aem-2", "contract:aem-2-evt-001"} and tags["remove_tags"] == ["spare"]
    sup = client.portal.call(db.kv_get, "contract_supply")["AEM-2-EVT-001"]
    row = sup["rows"][0]
    assert row["short"] == 1 and row["print"] == 0 and "authorize" in row["why"]   # one more: no print without a yes
    page = client.get("/game-events", headers=H).text
    assert "Authorize printing" in page and "Stop delivering devices" in page
    # once the tag shows, S1 counts as on its way; authorizing prints the other one, tagged for the contract
    for d in devices:
        if d["device_code"] == "S1":
            d["tags"] = ["to:aem", "at:aem-2", "contract:aem-2-evt-001"]
    client.portal.call(db.kv_set, "devices", devices)
    client.post("/game-events/AEM-2-EVT-001/authorize-prints", headers=HX)
    jobs = [j for j in client.portal.call(eng.jobs) if j["rule"] == "contracts"]
    pj = [j for j in jobs if "print" in j["title"]]
    assert len(pj) == 1 and pj[0]["device"] == "AF9"
    body = pj[0]["steps"][0]["body"]
    assert body["command"] == "enqueue_print" and body["quantity"] == 1
    assert set(body["tags"]) == {"to:aem", "at:aem-2", "contract:aem-2-evt-001"}
    client.portal.call(db.kv_set, "contract_supply_last", None)
    client.portal.call(eng.contract_supply_pass, True)
    jobs = [j for j in client.portal.call(eng.jobs) if j["rule"] == "contracts"]
    assert len(jobs) == 2                                              # the print counts as coming: nothing more
    row = client.portal.call(db.kv_get, "contract_supply")["AEM-2-EVT-001"]["rows"][0]
    assert (row["coming"], row["printing"], row["short"]) == (["S1"], 1, 0)
    # the print never came out (no autofactory holds it): Restart prints forgets it and prints it again
    client.post("/game-events/AEM-2-EVT-001/restart-prints", headers=HX)
    sup = client.portal.call(db.kv_get, "contract_supply")["AEM-2-EVT-001"]
    assert len(sup["orders"]) == 1 and sup["print_ok"]["ftl_relay"] == 1
    assert len([j for j in client.portal.call(eng.jobs) if j["rule"] == "contracts" and "print" in j["title"]]) == 2
    # the contract closes: the leftover relay goes back to spare
    client.portal.call(w.handle_event, {**_ev(998, "event.completed", designation="AEM-2-EVT-001", location="AEM-2"),
                                        "location": "AEM-2"})
    client.portal.call(eng.contract_supply_pass, True)
    rel = [j for j in client.portal.call(eng.jobs) if j["rule"] == "contracts" and "back to spare" in j["title"]]
    assert len(rel) == 1
    cfgx = rel[0]["steps"][0]["body"]["configuration"]
    assert cfgx["add_tags"] == ["spare"] and set(cfgx["remove_tags"]) == {"to:aem", "at:aem-2", "contract:aem-2-evt-001"}
    assert "AEM-2-EVT-001" not in client.portal.call(db.kv_get, "contract_supply")


def test_taxi_plate_no_controller_runs_goes_home():
    """Live 2026-10-08: Printing Hub 1's four surge plates (tagged taxi, no controller, not in taxi mode) sat idle in
    ITHVALAI — the check flagged them, the planner never sent them home."""
    from rsweb import loadouts as lo
    plate = lambda code, **kw: {"device_code": code, "device_type": "surge_plate", "location": "BBB-BELT-1", "status": "idle",  # noqa: E731
                                "attach_capacity": 1, "features": ["surge", "cruise", "attach", "taxi"],
                                "available_commands": ["attach", "detach", "travel"], **kw}
    devices = [plate("ORPHAN01", tags=["fleet:hub", "taxi"]),
               plate("WORKING1", tags=["fleet:hub", "taxi"], taxi_mode="taxi"),
               {"device_code": "TC", "device_type": "ami_transport_controller", "location": "BBB-BELT-1", "status": "idle",
                "tags": ["ferry"]},
               plate("RUN00001", tags=["fleet:hub", "taxi"], controller_device_code="TC")]
    cfg = lo.normalize({"phases": [], "fleets_migrated": True,
                        "fleets": [{"id": "hub", "name": "Hub", "role": "mining", "home": "AAA", "station": True,
                                    "wants": {"surge_plate": 3}}]})
    stars = {"AAA": {"position": {"x": 0, "y": 0, "z": 0}}, "BBB": {"position": {"x": 2, "y": 0, "z": 0}}}
    p = lo.plan(cfg, devices, [], {}, stars, {}, set(), [], {})
    assert p["moves"].get("ORPHAN01") == "AAA"
    assert "WORKING1" not in p["moves"] and "RUN00001" not in p["moves"]
    flagged = {x["code"]: x["fixed"] for x in lo.audit(cfg, devices, stars, p)}
    assert flagged.get("ORPHAN01") is True and "WORKING1" not in flagged   # in taxi mode: at work, not flagged


def test_bulk_command_on_ticked_devices(client):
    page = client.get("/fleet", headers=H).text
    assert 'class="bulk-pick" name="codes" value="2AC61210"' in page and "Send to ticked" in page
    assert ">Recall (4 here)" in page or "recall" in page.lower()
    assert 'value="set_directive"' not in page.split('id="bulk-cmd"')[1].split("</select>")[0]   # per-device only
    form = client.get("/fleet/bulk-form?command=travel", headers=HX).text
    assert 'name="f.destination"' in form
    # the warning names the device that can't take it
    pick = {"codes": ["2AC61210", "2AC61211", "AF00BEEF"], "command": "recall"}
    warn = client.post("/fleet/bulk-check", data=pick, headers=HX).text
    assert "<b>2</b> of 3 ticked" in warn and "AF00BEEF" in warn and "doesn&#x27;t take recall" in warn
    # a device a running job uses is skipped unless included
    eng, db = client.app.state.worker.automations, client.app.state.db
    jobs = client.portal.call(eng.jobs)
    jobs.append({"id": "j1", "rule": "x", "title": "t", "device": "2AC61211", "steps": [], "idx": 0, "status": "running",
                 "meta": {}})
    client.portal.call(eng.save_jobs, jobs)
    r = client.post("/fleet/bulk", data=pick, headers=HX).text
    assert "recall: 1 of 3 sent" in r and "automation job is using it" in r
    sent = client.portal.call(db.fetchall, "SELECT path, body FROM actions WHERE body LIKE '%recall%'")
    assert [x["path"] for x in sent] == ["/devices/2AC61210"]
    r = client.post("/fleet/bulk", data={**pick, "include_busy": "on"}, headers=HX).text
    assert "recall: 2 of 3 sent" in r
    # nothing ticked / a per-device command
    assert "Tick at least one" in client.post("/fleet/bulk", data={"command": "recall"}, headers=HX).text
    r = client.post("/fleet/bulk", data={"codes": ["MC91FF22"], "command": "set_directive"}, headers=HX).text
    assert "0 of 1 sent" in r and "on its page" in r


def test_bulk_check_detach_while_carrier_travels():
    from rsweb.web_bulk import bulk_check
    devices = [{"device_code": "P1", "device_type": "surge_plate", "available_commands": ["detach"], "status": "surging",
                "travel": {"destination": "BBB", "arrives_at": "2099-01-01T00:00:00+00:00"}},
               {"device_code": "P2", "device_type": "surge_plate", "available_commands": ["detach"], "status": "idle"}]
    ok, skip = bulk_check(["P1", "P2", "GONE"], "detach", devices, set(), {})
    assert ok == ["P2"] and {c for c, _ in skip} == {"P1", "GONE"}
    assert "between systems" in dict(skip)["P1"]


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


def test_travel_shows_on_the_map_straight_away(client):
    """The travel command's answer is put on the cached device at once (the device sync is up to a minute away)."""
    db = client.app.state.db
    devices = client.portal.call(db.kv_get, "devices")
    for d in devices:
        if d["device_code"] == "2AC61212":
            d.pop("travel", None)
            d["status"] = "idle"
    client.portal.call(db.kv_set, "devices", devices)
    r = client.post("/devices/2AC61212/command", data={"command": "travel", "f.destination": "ABOTEIN-3"}, headers=HX)
    assert r.status_code == 200
    d = next(x for x in client.portal.call(db.kv_get, "devices") if x["device_code"] == "2AC61212")
    assert d["travel"]["destination"] == "ABOTEIN-3" and d["travel"]["departed_at"] and d["status"] == "travelling"
    assert "attached_devices" not in d["travel"]
    ov = client.get("/api/map.json?part=overlay", headers=H).json()
    assert any("2AC61212" in m["label"] for m in ov["moving"])


def test_placed_beacons_are_never_spare():
    """Live 2026-10-08: 13 working beacons (dropped by the Surveyors) carried an old spare tag — fair game for
    shortfalls and the spare depot. A fleet's placed beacon loses the tag; a fleetless one keeps it (the redundant-beacon
    pass sets it) but is never moved by the loadouts pass."""
    from rsweb import loadouts as lo
    B = lambda code, loc, tags, status="monitoring", **kw: {"device_code": code, "device_type": "ftl_beacon",  # noqa: E731
                                                           "location": loc, "status": status, "tags": tags,
                                                           "available_commands": ["deploy", "stow"], **kw}
    devices = [B("FAR", "ZALDANAL-1-L4", ["spare"]),                       # dropped in a surveyed system
               B("HOME", "AAA-5", ["fleet:hub", "spare"]),                 # the fleet's own, at its home
               B("ABOARD", None, ["fleet:hub"], status="stowed", stowed_in_device_code="V1")]
    cfg = lo.normalize({"phases": [], "fleets_migrated": True,
                        "fleets": [{"id": "hub", "name": "Hub", "role": "mining", "home": "AAA", "station": True,
                                    "wants": {"ftl_beacon": 1}},
                                   {"id": "bbb", "name": "B", "role": "mining", "home": "BBB", "station": True,
                                    "wants": {"ftl_beacon": 1}}]})
    stars = {"AAA": {"position": {"x": 0, "y": 0, "z": 0}}, "BBB": {"position": {"x": 1, "y": 0, "z": 0}},
             "ZALDANAL": {"position": {"x": 2, "y": 0, "z": 0}}}
    p = lo.plan(cfg, devices, [], {}, stars, {}, set(), [], {})
    assert "spare" in p["tag_remove"]["HOME"]
    assert "FAR" not in p["tag_remove"]       # a fleetless one keeps it (redundant: the civ beacon pass may move it)
    assert "FAR" not in p["moves"] and "HOME" not in p["moves"]          # BBB's shortfall isn't filled from them
    assert "spare" not in p["tag_add"].get("HOME", []) and "spare" not in p["tag_add"].get("ABOARD", [])
    hub = {r["type"]: r for r in _rep(p, "AAA")["rows"]}["ftl_beacon"]
    assert hub["have"] == 2                                              # the one at home and the one aboard count


def test_enqueue_print_on_a_replicant_vessel_uses_the_replicant_print(client):
    db = client.app.state.db
    reps = client.portal.call(db.kv_get, "replicants")
    code, rep = next(iter(reps.items()))
    host = rep.get("hosted_device_code")
    assert host
    r = client.post(f"/devices/{host}/command", data={"command": "enqueue_print", "f.device_type": "mining_drone",
                                                      "f.quantity": "2"}, headers=HX)
    assert "one device at a time" in r.text
    sent = client.portal.call(db.fetchall, "SELECT path, body FROM actions ORDER BY rowid DESC LIMIT 1")[0]
    assert sent["path"] == f"/replicants/{code}/print" and '"device_type": "mining_drone"' in sent["body"]


def test_prospect_directions():
    from rsweb import prospecting as pr
    import math
    h = [1.0, 0.0, 0.0]
    d = pr.directions(h)
    assert len(d) == 14 and d[0] == h and d[-1] == [-1.0, 0.0, 0.0]
    ang = lambda v: round(math.degrees(math.acos(max(-1, min(1, sum(a * b for a, b in zip(v, h)))))))  # noqa: E731
    assert [ang(v) for v in d[1:7]] == [45] * 6 and [ang(v) for v in d[7:13]] == [90] * 6
    assert pr.base_heading(None, (3, 4, 0)) == [0.6, 0.8, 0.0]               # no heading: outward from Sol


def test_observatory_prospects_each_direction_then_compacts(client):
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": "HOMEA", "position": {"x": 0, "y": 5, "z": 0}}]})
    obs = {"device_code": "OBS00001", "device_type": "galactic_observatory", "location": "HOMEA-5-L4", "status": "idle",
           "tags": ["fleet:ex"], "available_commands": ["prospect", "compact", "unfurl"]}
    client.portal.call(db.kv_set, "devices", [obs])
    client.portal.call(eng.save_fleets, [{"id": "ex", "name": "Explorers", "role": "explore", "home": "HOMEA", "wants": {}}])

    def last_job():
        return [j for j in client.portal.call(eng.jobs) if j["rule"] == "observatory"][-1]

    def finish(stars=None, error=None):
        jobs = client.portal.call(eng.jobs)
        j = jobs[-1]
        j["status"] = "failed" if error else "done"
        if error:
            j["steps"][0].update({"status": "failed", "error": error})
        client.portal.call(eng.save_jobs, jobs)
        if stars is not None:
            client.portal.call(client.app.state.worker.handle_event, {**_ev(990 + len(jobs), "prospect.completed", "OBS00001",
                                                                              datetime(2026, 10, 8, 12, tzinfo=timezone.utc),
                                                                              stars=[{"designation": s, "position": {"x": 1, "y": 1, "z": 1}} for s in stars])})

    assert client.portal.call(eng.observatory_pass) == ["OBS00001: prospecting from HOMEA, direction 1"]
    assert last_job()["steps"][0]["body"]["direction"] == [0.0, 1.0, 0.0]     # outward from Sol: no heading set
    finish(error="No new stars visible from this location")                    # too dense that way: the next direction
    assert client.portal.call(eng.observatory_pass) == ["OBS00001: prospecting from HOMEA, direction 2"]
    finish(stars=["NEWSTAR1"])                                                  # one prospect per star: HOMEA is used up
    out = client.portal.call(eng.observatory_pass)
    assert out == ["OBS00001: compacting (every direction tried in HOMEA)"]
    pf = client.portal.call(db.kv_get, "prospected_from")
    assert pf["HOMEA"]["by"] == "OBS00001" and pf["HOMEA"]["found"] == 1
    assert client.get("/api/map.json?part=core", headers=H).json()["stars"][0]["prospected_from"]["found"] == 1
    assert "done in HOMEA" in client.get("/fleets/details?all=1", headers=H).text


def test_prospect_cones_on_the_galaxy_map(client):
    from rsweb.observatory import live_prospect, reach_estimate
    pos = {"HOME": {"x": 0, "y": 0, "z": 0}}
    finds = [{"origin": "HOME", "direction": [1, 0, 0],
              "stars": [{"designation": f"S{i}", "position": {"x": 10 + i, "y": i, "z": 0}} for i in range(6)]}]
    reach, half, learned = reach_estimate(finds, pos)
    assert learned and reach == 16.0 and 0 < half < 30
    assert reach_estimate([], pos) == (25.0, 30.0, False)                  # nothing learned yet: the guess
    db = client.app.state.db
    live = {"completes_at": "2099-10-08T23:34:17-04:00", "direction": [-0.9068, -0.4214, 0.0094], "eta_seconds": 10284,
            "origin": "SOL", "progress_percent": 76.2, "started_at": "2026-10-08T11:34:17-04:00"}
    assert live_prospect({"device_code": "O", "prospect": live}) == live
    devices = client.portal.call(db.kv_get, "devices")
    devices.append({"device_code": "OBSV0001", "device_type": "galactic_observatory", "location": "SOL-3-L4",
                    "status": "prospecting", "prospect": live, "tags": []})
    client.portal.call(db.kv_set, "devices", devices)
    ov = client.get("/api/map.json?part=overlay", headers=H).json()
    p = ov["prospects"][0]
    assert p["code"] == "OBSV0001" and p["origin"] == "SOL" and p["direction"] == live["direction"]
    assert p["t1"] > p["t0"] and p["reach"] == 25.0 and not p["learned"]
    assert 'id="opt-prospect"' in client.get("/map", headers=H).text


def test_observatory_pass_stands_aside_while_loadouts_moves_it(client):
    """Live 2026-10-09: Miner 2's observatory was unfurled to prospect while loadouts was compacting it for ITHVALAI."""
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": "HOMEA", "position": {"x": 0, "y": 5, "z": 0}}]})
    obs = {"device_code": "OBS00002", "device_type": "galactic_observatory", "location": "HOMEA-6-L4", "status": "compacted",
           "tags": ["fleet:m2"], "available_commands": ["prospect", "compact", "unfurl"]}
    client.portal.call(db.kv_set, "devices", [obs])
    client.portal.call(eng.save_fleets, [{"id": "m2", "name": "Miner 2", "role": "mining", "home": "HOMEA", "wants": {}}])
    from rsweb.db import now_iso
    client.portal.call(db.kv_set, "loadout_moves", {"at": now_iso(), "moves": {"OBS00002": "ITHVALAI"}})
    assert client.portal.call(eng.observatory_pass) == []                    # no unfurl, no compact of its own
    assert "moving it to ITHVALAI" in client.portal.call(db.kv_get, "observatory_runs")["OBS00002"]["note"]
    client.portal.call(db.kv_set, "loadout_moves", {"at": now_iso(), "moves": {}})
    assert client.portal.call(eng.observatory_pass) == ["OBS00002: unfurling"]   # staying: back to prospecting


def test_site_yield_estimate_from_mining_history(client):
    from rsweb.targets import site_yields
    db, w = client.app.state.db, client.app.state.worker
    for i, (site, q) in enumerate((("SOL-BELT-1-SITE-1", 300), ("SOL-BELT-1-SITE-1", 100), ("SOL-BELT-1-SITE-2", 200))):
        client.portal.call(w.handle_event, _ev(880 + i, "mining.resource_depleted", f"D{i}", location="SOL-BELT-1",
                                               site=site, resource_type="carbon", quantity_mined=q))
    by_belt, _ = client.portal.call(site_yields, db)
    assert by_belt[("SOL-BELT-1", "carbon")] == (300.0, 2)      # site 1: 400 by two drones, site 2: 200


def test_contracts_page_lists_quiet_civilizations_for_approval(client):
    db = client.app.state.db
    scan = {"planets": [{"designation": "VETH-2", "life_stage": "intelligent", "species": "Veth"},
                        {"designation": "VETH-3", "life_stage": "microbial"}]}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("VETH", json.dumps(scan), "2026-10-09"))
    page = client.get("/game-events", headers=H).text
    assert "Civilizations not asking for anything" in page and "VETH-2" in page and "VETH-3" not in page
    client.post("/game-events/approve", data={"key": "species:veth", "on": "1"}, headers=HX)
    assert "species:veth" in client.portal.call(db.kv_get, "contract_approvals")


def test_advancing_explorer_moves_home_to_farthest_star_ahead(client):
    eng, db = client.app.state.worker.automations, client.app.state.db
    stars = {"HOMEA": (0, 0, 0), "NEAR1": (5, 0, 0), "FAR1": (20, 1, 0), "TOOFAR": (60, 0, 0), "BEHIND": (-10, 0, 0)}
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": k, "position": dict(zip("xyz", v))} for k, v in stars.items()]})
    devices = [{"device_code": "OBSX", "device_type": "galactic_observatory", "location": "HOMEA-3-L4", "status": "idle",
                "tags": ["fleet:ex"], "available_commands": ["prospect", "compact", "unfurl"]},
               {"device_code": "VES1", "device_type": "heaven_vessel", "location": "HOMEA-3-L4", "status": "idle", "tags": ["fleet:ex"],
                "features": ["surge", "cruise", "stow"], "stow_capacity": 10},
               {"device_code": "PLT1", "device_type": "surge_plate", "location": "HOMEA-3-L4", "status": "idle", "tags": ["fleet:ex"],
                "features": ["surge", "attach"], "attach_capacity": 1},   # the observatory can't stow: it attaches
               {"device_code": "RLY1", "device_type": "ftl_relay", "location": "HOMEA-3-L4", "status": "relaying", "tags": ["fleet:ex"]}]
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(db.kv_set, "replicants", {"R1": {"name": "bob", "hosted_device_code": "VES1"}})   # rides along
    client.portal.call(eng.save_fleets, [{"id": "ex", "name": "Pathfinder", "role": "explore", "home": "HOMEA", "wants": {}}])
    r = client.post("/fleets/ex/path", data={"heading_kind": "custom", "heading_vec": "1, 0, 0", "cone": "30",
                                             "advance": "on", "hop_ly": "30"}, headers=HX)
    assert r.status_code == 200
    assert "advance along the heading" in client.get("/fleets/details?all=1", headers=H).text
    client.portal.call(db.kv_set, "observatory_runs", {"OBSX": {"star": "HOMEA", "used": list(range(14)), "idx": 0, "found": 3}})
    out = client.portal.call(eng.observatory_pass)
    assert out == ["Pathfinder: advancing HOMEA → FAR1"]                     # farthest ahead within 30 ly, not NEAR1
    f = client.portal.call(eng.fleets)[0]
    assert f["home"] == "FAR1" and f["prev_home"] == "HOMEA" and f["station"] and f["advanced_from"] == ["HOMEA"]
    m = f["mission"]
    assert m["relocate"] and m["targets"] == ["FAR1"] and m["status"] == "running"   # one move, everyone together
    from rsweb import fleets as fl
    assert fl.next_phase("explore", {**m, "phase": "travel"}) == "deploy"
    assert fl.next_phase("explore", {**m, "phase": "deploy"}) is None               # unloaded at the new home: done
    assert client.portal.call(eng.observatory_pass) == []                          # the mission handles the observatory
    job = [j for j in client.portal.call(eng.jobs) if "leave a relay" in j["title"]][0]
    assert job["steps"][0]["body"]["configuration"] == {"add_tags": ["at:homea-3-l4"], "remove_tags": ["fleet:ex"]}


def test_loadout_prints_never_go_to_a_vessel():
    """Live 2026-10-09: a replenishment print went to an idle heaven vessel (it lists enqueue_print) instead of the
    system's autofactory."""
    from rsweb import loadouts as lo
    D = lambda code, t, **kw: {"device_code": code, "device_type": t, "location": "AAA-3-L4", "status": "idle", **kw}  # noqa: E731
    devices = [D("AF1", "autofactory", available_commands=["enqueue_print"], print_queue=[{"device_type": "x"}] * 3),
               D("HV2", "heaven_vessel", available_commands=["enqueue_print", "travel"])]
    cfg = lo.normalize({"phases": [], "fleets_migrated": True, "settings": {"need_stock": False},
                        "fleets": [{"id": "f", "name": "F", "role": "mining", "home": "AAA", "station": True,
                                    "wants": {"mining_drone": 2}}]})
    bps = [{"device_type": "mining_drone", "resources": {"structural": 10}, "print_time": 60},
           {"device_type": "autofactory", "queue_size": 10}]
    p = lo.plan(cfg, devices, bps, {"AAA-3-L4": {"structural": 100}}, {"AAA": {"position": {"x": 0, "y": 0, "z": 0}}},
                {}, set(), [], {})
    assert p["prints"] and all(pr["factory"] == "AF1" for pr in p["prints"])


def test_device_list_shows_a_stowed_device_where_its_carrier_is(client):
    db = client.app.state.db
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": "CARRY001", "device_type": "cargo_vessel", "location": "SOL-3-L4", "status": "idle"},
                {"device_code": "RIDER001", "device_type": "survey_drone", "location": None, "status": "stowed",
                 "stowed_in_device_code": "CARRY001"}]
    client.portal.call(db.kv_set, "devices", devices)
    page = client.get("/fleet?q=RIDER001", headers=H).text
    assert "SOL-3-L4" in page and 'in <a href="/devices/CARRY001">CARRY001</a>' in page
    assert "RIDER001" in client.get("/fleet?star=SOL&q=RIDER001", headers=H).text      # the system filter finds it


def test_replicant_cooperation_and_cohort_permission(client):
    from rsweb.web_profile import profile_changes
    world = client.app.state.api.http._transport.app.state.world
    db = client.app.state.db
    client.portal.call(client.app.state.worker.sync_devices)
    rep = next(iter(client.portal.call(db.kv_get, "replicants")))
    page = client.get(f"/replicants/{rep}", headers=H).text
    assert 'name="cohort_permission"' in page
    client.post(f"/replicants/{rep}/profile", data={"cohort_permission": "public", "orig_cohort_permission": "private"}, headers=HX)
    assert world.profile_patches[-1] == {"code": rep, "cohort_permission": "public"}
    assert profile_changes({"cohort_permission": "everyone", "orig_cohort_permission": "private"})[1]   # not a choice
    assert 'name="replicant_cooperation"' in client.get("/account", headers=H).text
    r = client.post("/account/cooperation", data={"replicant_cooperation": "shared"}, headers=HX)
    assert r.status_code == 200
    sent = client.portal.call(db.fetchall, "SELECT path, body FROM actions ORDER BY rowid DESC LIMIT 1")[0]
    assert sent["path"] == "/accounts/me" and '"replicant_cooperation": "shared"' in sent["body"]


def test_owner_handoff_skipped_when_cooperation_allows():
    from rsweb.automations import step
    from rsweb.modular import ownership_hint, with_owner_handoff
    devices = [{"device_code": "CAR", "replicant_code": "A"}, {"device_code": "DRN", "replicant_code": "B"}]
    attach = [step("attach", "/devices/CAR", {"command": "attach", "device": "DRN"})]
    stow = [step("stow", "/devices/DRN", {"command": "stow", "target": "CAR"})]
    assert len(with_owner_handoff(attach, devices)) == 2                                      # private: hand over
    assert len(with_owner_handoff(attach, devices, {"shared": True})) == 1                    # shared account
    assert len(with_owner_handoff(attach, devices, {"public": {"B"}})) == 1                   # A may act on B's drone
    assert len(with_owner_handoff(stow, devices, {"public": {"B"}})) == 2                     # B may not use A's carrier
    assert len(with_owner_handoff(stow, devices, {"public": {"A"}})) == 1
    assert "Replicant cooperation" in ownership_hint("Target host device must belong to this replicant")
    assert ownership_hint("Device is already in motion") == ""


def test_path_forward_form_shows_the_saved_heading(client):
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": "HOMEA", "position": {"x": 3, "y": 4, "z": 0}},
                                                      {"designation": "TGT", "position": {"x": 9, "y": 4, "z": 0}}]})
    client.portal.call(eng.save_fleets, [{"id": "m1", "name": "Miner 1", "role": "mining", "home": "HOMEA", "station": True, "wants": {}}])
    client.post("/fleets/m1/path", data={"heading_kind": "star", "heading_star": "TGT", "cone": "45"}, headers=HX)
    page = client.get("/fleets/details?all=1", headers=H).text
    assert '<option value="star" selected>' in page and 'name="heading_star" placeholder="star" size="10" list="path-stars" value="TGT"' in page
    items = client.portal.call(eng.fleets)
    items[0]["home"] = "ELSEWHERE"                                           # the fleet moved since
    client.portal.call(eng.save_fleets, items)
    client.post("/fleets/m1/path", data={"heading_kind": "star", "heading_star": "TGT", "cone": "30"}, headers=HX)
    f = client.portal.call(eng.fleets)[0]
    assert f["heading"]["vector"] == [1.0, 0.0, 0.0] and f["cone"] == 30     # unchanged choice: vector kept, cone saved


def test_relocation_only_when_the_carriers_fit_everyone(client):
    eng, db = client.app.state.worker.automations, client.app.state.db
    rider = lambda c: {"device_code": c, "device_type": "survey_drone", "location": "AAA-1", "status": "idle",  # noqa: E731
                       "tags": ["fleet:m"], "features": ["cruise", "stow"]}
    ves = {"device_code": "V", "device_type": "cargo_vessel", "location": "AAA-1", "status": "idle", "tags": ["fleet:m"],
           "features": ["surge", "cruise"], "stow_capacity": 2}
    f = {"id": "m", "name": "M", "role": "mining", "home": "BBB", "station": True, "wants": {}}

    async def commandable(devices, stars=None, without=None):
        return {"AAA", "BBB"}
    eng.commandable = commandable
    how = client.portal.call(eng.start_relocation, dict(f), "AAA", "BBB", [ves, rider("D1"), rider("D2")])
    assert "loadout pass" in how                         # a mining fleet never assembles (owner 2026-10-21): piecemeal
    f = {**f, "role": "explore"}
    how = client.portal.call(eng.start_relocation, dict(f), "AAA", "BBB", [ves, rider("D1"), rider("D2")])
    assert "relocation mission" in how                                         # 2 riders, 2 hold slots: together
    f2 = dict(f)
    how = client.portal.call(eng.start_relocation, f2, "AAA", "BBB", [ves, rider("D1"), rider("D2"), rider("D3"), rider("D4"), rider("D5"), rider("D6")])
    assert "piecemeal" in how and "mission" not in f2                         # too many riders: the loadout pass carries them


def test_replicant_vessel_travels_last():
    """Without a relay the other carriers are only commandable while the replicant is still there."""
    from rsweb import fleets as fl
    D = lambda code, t, **kw: {"device_code": code, "device_type": t, "location": "AAA-3-L4", "status": "idle",  # noqa: E731
                               "tags": ["fleet:x"], "features": ["surge", "cruise"], **kw}
    devices = [D("HV1", "heaven_vessel", hosting_replicant="R1", stow_capacity=10), D("PL1", "surge_plate", attach_capacity=1),
               D("CV1", "cargo_vessel", stow_capacity=50)]
    steps = fl.travel_steps({"id": "x", "home": "AAA"}, devices, "BBB", {})
    sent = [s["path"].split("/")[-1] for s in steps if (s.get("body") or {}).get("command") == "travel"]
    assert sent[-1] == "HV1" and set(sent) == {"HV1", "PL1", "CV1"}


def test_survey_relays_wait_at_home_and_one_relay_per_system():
    from rsweb import loadouts as lo
    from rsweb import placement as pl
    cfg, devices, bps, inv, stars = _lo_world()
    cfg = {**cfg, "fleets": list(cfg.get("fleets") or []) + [{"id": "surveyors", "role": "explore", "home": "AAA"}]}
    devices += [
        {"device_code": "SR1", "device_type": "ftl_relay", "location": "AAA-BELT-1", "status": "idle", "tags": ["fleet:surveyors"],
         "available_commands": ["travel", "activate"], "operational_capacity": 100.0},
        {"device_code": "SR2", "device_type": "ftl_relay", "location": "AAA-3-L4", "status": "relaying", "tags": ["fleet:surveyors"],
         "available_commands": ["deactivate"], "operational_capacity": 100.0},
        {"device_code": "SR3", "device_type": "ftl_relay", "location": "AAA-3-L4", "status": "relaying", "tags": ["fleet:surveyors"],
         "available_commands": ["deactivate"], "operational_capacity": 100.0}]
    geo = {"AAA": pl.geography("AAA", devices, None, "AAA-OORT")}
    geo["AAA"]["lagrange"] = ["AAA-3-L4"]
    assert lo.survey_cargo(devices, cfg["fleets"]) == {"SR1", "SR2", "SR3"}
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {}, geo=geo)
    assert "SR1" not in dict(p["places"])                         # cargo: not sent to the L4 point
    assert "fleet:surveyors" in p["tag_remove"].get("SR2", []) and "at:aaa-3-l4" in p["tag_add"]["SR2"]   # stays as AAA's relay
    assert "SR3" not in p["tag_remove"] or "fleet:surveyors" not in p["tag_remove"]["SR3"]               # goes with the fleet
    # an idle relay that isn't cargo isn't placed where one already relays
    devices.append({"device_code": "RX", "device_type": "ftl_relay", "location": "AAA-BELT-1", "status": "idle",
                    "available_commands": ["travel", "activate"], "operational_capacity": 100.0})
    assert "RX" not in dict(lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {}, geo=geo)["places"])


def test_working_relay_is_switched_off_before_boarding():
    from rsweb import fleets as fl
    f = {"id": "s", "role": "explore", "home": "AAA"}
    devs = [{"device_code": "MF", "device_type": "mobile_fleet", "location": "AAA-3-L4", "status": "idle", "tags": ["fleet:s"],
             "attach_capacity": 36, "features": ["surge"]},
            {"device_code": "RR", "device_type": "ftl_relay", "location": "AAA-3-L4", "status": "relaying", "tags": ["fleet:s"],
             "available_commands": ["deactivate", "travel"]}]
    steps, _ = fl.assemble_steps(f, devs)
    names = [s["name"] if "name" in s else s.get("label", "") for s in steps]
    i = next(i for i, s in enumerate(steps) if (s.get("body") or {}).get("command") == "deactivate")
    j = next(i for i, s in enumerate(steps) if "RR" in str(s.get("body")) and (s.get("body") or {}).get("command") != "deactivate")
    assert i < j, names


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


def test_recall_withdraws_a_launched_controller_first():
    """Seen live 2026-10-11: the Surveyors recall cleared the launched survey controller's directive, then its travel to
    board failed with "Device is not deployed"."""
    from rsweb import fleets as fl
    fleet = {"id": "surveyors", "name": "Surveyors", "role": "explore", "home": "LOR", "wants": {}}
    devices = [
        {"device_code": "HV", "device_type": "cargo_vessel", "location": "LOR-6-L4", "status": "idle", "features": ["surge"],
         "stow_capacity": 20, "tags": ["fleet:surveyors"]},
        {"device_code": "SC", "device_type": "ami_survey_controller", "location": "LOR-6", "status": "launched",
         "features": ["stow"], "ami_directive": {"name": "survey_system"}, "tags": ["fleet:surveyors"]},
    ]
    steps = fl.recall_steps(fleet, devices, {}, None)
    descs = [s["desc"] for s in steps]
    # docs: withdraw pauses the directive (activate resumes): a launched controller isn't cleared first
    assert "SC: clear directive" not in descs
    assert descs.index("SC: withdraw (call its drones back)") < descs.index("SC → LOR-6-L4 (board)")
    w = steps[descs.index("SC: withdraw (call its drones back)")]
    assert w["body"] == {"command": "withdraw"} and w["wait"] == ["ami.withdrawn"]
    assert "not deployed" in steps[descs.index("SC → LOR-6-L4 (board)")]["hint"]


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


def test_large_devices_can_be_printed_compacted(client):
    import json as _j
    db = client.app.state.db
    bps = client.portal.call(db.kv_get, "blueprints") or []
    if not any(b.get("device_type") == "galactic_observatory" for b in bps):
        bps = bps + [{"device_type": "galactic_observatory", "features": ["modular", "prospect"], "resources": {"structural": 10},
                      "print_time": 60}]
        client.portal.call(db.kv_set, "blueprints", bps)
    page = client.get("/blueprints", headers=H).text
    assert 'value="galactic_observatory" data-large=1' in page and 'name="flatpack"' in page
    client.post("/blueprints/print", data={"printer": "device:AF00BEEF", "device_type": "galactic_observatory", "quantity": "1",
                                           "flatpack": "on"}, headers=HX)
    client.post("/blueprints/print", data={"printer": "device:AF00BEEF", "device_type": "mining_drone", "quantity": "1",
                                           "flatpack": "on"}, headers=HX)
    rows = client.portal.call(db.fetchall, "SELECT body FROM actions WHERE path='/devices/AF00BEEF' ORDER BY rowid DESC LIMIT 2")
    bodies = [_j.loads(r["body"]) for r in rows]
    assert {b["device_type"]: b.get("flatpack") for b in bodies} == {"galactic_observatory": True, "mining_drone": None}
    # the planner: large lines go compacted when ticked
    data = {"printer": "device:AF00BEEF", "qty:galactic_observatory": "1", "qty:mining_drone": "1", "_planned": "1",
            "flatpack": "on", "nosplit": "on"}
    assert "print galactic observatory compacted" in client.post("/blueprints/plan", data=data, headers=HX).text
    client.post("/blueprints/queue-plan", data=data, headers=HX)
    job = client.portal.call(client.app.state.worker.automations.jobs)[-1]
    flat = {s["body"]["device_type"]: s["body"].get("flatpack") for s in job["steps"] if s["body"].get("command") == "enqueue_print"}
    assert flat == {"galactic_observatory": True, "mining_drone": None}


def test_deleting_a_fleet_untags_its_devices(client):
    db, eng = client.app.state.db, client.app.state.worker.automations
    client.post("/fleets", data={"name": "Gone", "role": "mining", "home": "SOL"}, headers=HX)
    fid = next(f["id"] for f in client.portal.call(eng.fleets) if f["name"] == "Gone")
    devs = client.portal.call(db.kv_get, "devices")
    devs += [{"device_code": "G1", "device_type": "mining_drone", "location": "SOL-BELT-1", "status": "idle", "tags": [f"fleet:{fid}", "keep"]},
             {"device_code": "G2", "device_type": "survey_drone", "location": "SOL-3", "status": "idle", "tags": [f"to:{fid}"]},
             {"device_code": "G3", "device_type": "survey_drone", "location": "SOL-3", "status": "idle", "tags": ["fleet:other"]}]
    client.portal.call(db.kv_set, "devices", devs)
    client.post(f"/fleets/{fid}/edit", data={"delete": "1"}, headers=HX)
    assert not any(f["id"] == fid for f in client.portal.call(eng.fleets))
    job = next(j for j in client.portal.call(eng.jobs) if "deleted" in j["title"])
    bodies = {s["path"]: s["body"]["configuration"]["remove_tags"] for s in job["steps"]}
    assert bodies == {"/devices/G1": [f"fleet:{fid}"], "/devices/G2": [f"to:{fid}"]}


def test_performance_diagnostics(client):
    """Each request is timed (Server-Timing header); the diagnostics page lists slow pages, stages and storage."""
    from rsweb import perf
    r = client.get("/", headers=H)
    assert "db;desc=" in r.headers.get("server-timing", "")
    assert any(x["path"] == "/" and x["db_n"] > 0 and x["kv"] for x in perf.requests)
    perf.note_stage("logistics_pass", 12.5)
    body = client.get("/diagnostics?perf=1", headers=HX).text
    assert "Engine stages" in body and "logistics_pass" in body and "Largest saved values" in body and "events:" in body
    rep = client.portal.call(perf.report, client.app.state.db)
    assert rep["storage"]["tables"]["kv"] > 0 and rep["by_path"]


def test_dashboard_shows_tutorial_progress(client):
    """GET /tutorials lists progress; the first unfinished one's steps show with the current step's hint."""
    assert 'hx-get="/partials/tutorials"' in client.get("/", headers=H).text
    body = client.get("/partials/tutorials", headers=HX).text
    assert "0 of 2 done" in body and "Exploring the Belt" in body and "3 / 4" in body
    assert "Check your vessel and list of stowed devices." in body and "GET /replicants/:code" in body


def test_game_news_from_the_changelog_and_blog(client, monkeypatch):
    """The changelog and blog are read every 6 hours; the first read is the baseline, later new entries ring the bell."""
    from rsweb import news
    page = {"/changelog/": """<ol class="changelog-list"><li class="changelog-entry" id="v3.5.0"><details>
      <span class="entry-version">v3.5.0</span> <span class="entry-codename">alteration</span>
      <time class="entry-date" datetime="2026-10-13">13 October 2026</time></summary><article class="entry-body">
      <p>Asteroids got busier. Evacuate before the ETA.</p><h3 id="api">API updates</h3><ul><li>New <code>system.impact_warning</code> event.</li></ul>
      </article></details></li></ol>""",
            "/blog/": """<article class="featured"><h2><a href="/blog/7/">Crystal Growth</a></h2>
      <p>Written by <span class="byline-author">Kepler</span>, on <time datetime="2026-09-25">25 September</time></p></article>"""}

    async def fetch(path):
        return page[path]
    monkeypatch.setattr(news, "fetch", fetch)
    eng = client.app.state.worker.automations
    db = client.app.state.db
    assert client.portal.call(eng.news_pass, True) == []            # baseline: no bell for history
    s = client.portal.call(db.kv_get, news.KV)
    assert s["changelog"][0]["sections"]["API updates"] == ["New system.impact_warning event."] and s["blog"][0]["author"] == "Kepler"
    page["/changelog/"] = page["/changelog/"].replace('<ol class="changelog-list">', """<ol class="changelog-list">
      <li class="changelog-entry" id="v3.5.1"><span class="entry-codename">alteration</span><time datetime="2026-10-14"></time>
      <article class="entry-body"><p>A patch.</p></article></li>""")
    assert client.portal.call(eng.news_pass, True) == ["3.5.1"]
    bell = client.portal.call(db.fetchall, "SELECT title, link FROM notifications ORDER BY id DESC LIMIT 1")[0]
    assert bell["title"].startswith("Game update v3.5.1 (alteration): A patch.") and bell["link"].endswith("#v3.5.1")
    body = client.get("/partials/news", headers=HX).text
    assert "v3.5.1" in body and "new" in body and "Crystal Growth" in body


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


def test_docs_review_2026_10_13_fixes(client):
    """From reading the published docs: v3.5.0 impact events key on the envelope location, a partial diversion keeps
    the object open, recalled arrivals don't satisfy waits, evicted_miners is a count, a device sent to another
    replicant is gone (received isn't), and per-endpoint 429s don't block the whole bucket."""
    from rsweb import api, notify, wardhub as wh
    from rsweb.automations import event_matches
    db, eng = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.insert_event, {"id": "w1", "event": "system.impact_warning", "location": "KELM-OBJ-1",
                                          "created_at": "2026-10-13T10:00:00+00:00",
                                          "payload": {"destination": "KELM-3", "impact_eta": "2099-10-13T11:00:00Z",
                                                      "device_codes": ["D1"]}})
    o = client.portal.call(eng.defence_objects)["KELM-OBJ-1"]
    assert o["impact_target"] == "KELM-3" and o["impact_eta"] == "2099-10-13T11:00:00Z" and not o.get("closed")
    client.portal.call(db.insert_event, {"id": "w2", "event": "system.object_diverted", "location": "KELM-OBJ-1",
                                          "payload": {"outcome": "partial", "impact_target": "KELM-3"}})
    assert not client.portal.call(eng.defence_objects)["KELM-OBJ-1"].get("closed")
    client.portal.call(db.insert_event, {"id": "w3", "event": "system.object_impacted", "location": "KELM-OBJ-1",
                                          "payload": {"impact_target": "KELM-3", "terraform_effects": "craters"}})
    assert client.portal.call(eng.defence_objects)["KELM-OBJ-1"]["closed"] == "impacted"
    assert "hits KELM-3" in notify.describe({"event": "system.impact_warning", "location": "KELM-OBJ-1",
                                             "payload": {"destination": "KELM-3", "impact_eta": "x", "device_codes": ["D1"]}})
    st = {"wait": ["travel.arrived"], "match": {"destination": "SOL"}}
    ev = {"event": "travel.arrived", "device_code": "V", "payload": {"destination": "SOL-3", "recalling": False}}
    assert event_matches(st, ev, "V")
    assert not event_matches(st, {**ev, "payload": {"destination": "SOL-3", "recalling": True}}, "V")
    assert len(wh.evicted({"evicted_miners": 3})) == 3 and "count" in wh.eviction_text(wh.evicted({"evicted_miners": 3}))
    assert api.own_limit("/stars") and api.own_limit("/feedback") and not api.own_limit("/stars/SOL")
    from rsweb.ingest import GONE_SQL, GONE_EVENTS
    for i, (code, direction) in enumerate((("G1", "sent"), ("G2", "received"))):
        client.portal.call(db.insert_event, {"id": f"co{i}", "event": "device.changed_owner", "device_code": code,
                                              "payload": {"direction": direction}})
    rows = client.portal.call(db.fetchall, f"SELECT DISTINCT device_code FROM events WHERE {GONE_SQL} AND device_code IN ('G1','G2')",
                              GONE_EVENTS)
    assert {r["device_code"] for r in rows} == {"G1"}


def test_a_mining_drone_is_deactivated_before_it_moves():
    """Live 2026-10-13: deactivate stops mining ("mining_stopped"), so a move needn't wait for the drone to go idle."""
    from rsweb import modular
    from rsweb.automations import step
    devs = [{"device_code": "M1", "device_type": "mining_drone", "status": "mining", "location": "SOL-BELT-1"}]
    steps = modular.with_untracking([step("M1 to SOL-3", "/devices/M1", {"command": "travel", "destination": "SOL-3"},
                                          wait=["travel.arrived"])], devs)
    assert [s["body"]["command"] for s in steps] == ["deactivate", "travel"]
    devs[0]["status"] = "idle"
    assert len(modular.with_untracking([step("M1 to SOL-3", "/devices/M1", {"command": "travel"})], devs)) == 1


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


def test_materials_in_transit_on_logistics_and_dashboard(client):
    """What's in holds: per device (from its last transport.collected), its trip, and totals on the dashboard."""
    from rsweb import cargo
    db = client.app.state.db
    client.portal.call(db.insert_event, {"id": "tc-1", "event": "transport.collected", "device_code": "FR1",
                                          "payload": {"cargo_after": {"structural": 120, "carbon": 30}}})
    devs = [{"device_code": "FR1", "device_type": "cargo_freighter", "location": "SOL-3", "status": "travelling",
             "travel": {"destination": "FALQUORYX-3", "eta_seconds": 3600}},
            {"device_code": "FR2", "device_type": "cargo_freighter", "location": "SOL-3", "status": "idle"}]
    out = client.portal.call(cargo.in_transit, db, devs, [])
    assert [r["code"] for r in out["rows"]] == ["FR1"] and out["rows"][0]["dest"] == "FALQUORYX-3"
    assert out["totals"] == {"structural": 120, "carbon": 30}
    assert "In transit" in client.get("/fleets/logistics", headers=H).text


def test_console_presets_relay_links_matrix_moves_and_megastructures(client):
    """Console presets with fill-in blanks; a relay's live links; transfer / teleport into an empty matrix;
    megastructure contribution and leaderboard; attach_used counts (v3.5.1)."""
    from rsweb import fleets as fl
    page = client.get("/console", headers=H).text
    assert 'data-p="/devices/{relay}/network"' in page and 'id="c-dev"' in page and "Simulations" in page
    assert 'data-p="/devices/{locker}/retrieve"' in page and "Open an equipment locker" in page   # 1.204.0
    assert 'data-p="/devices/{controller}/trades/{trade}"' in page and "/leaderboards/colony_moon" in page   # 1.206.0
    r = client.get("/map/relays/AAAA1111/network", headers=HX).text
    assert "relaying" in r and "MUROPE" in r
    r = client.post("/replicants/77F75255/move-to", data={"target": "b22a1198", "how": "transfer"}, headers=HX)
    row = client.portal.call(client.app.state.db.fetchone, "SELECT path, body FROM actions ORDER BY id DESC LIMIT 1")
    assert row["path"] == "/replicants/77F75255/transfer" and '"B22A1198"' in row["body"]
    r = client.post("/locations/FLARM-OBJ-1/contribute", data={"devices": ["A473F411"]}, headers=HX)
    row = client.portal.call(client.app.state.db.fetchone, "SELECT path, body FROM actions ORDER BY id DESC LIMIT 1")
    assert row["path"] == "/locations/FLARM-OBJ-1/contribute" and "A473F411" in row["body"]
    assert "Bob-19" in client.get("/megastructures", headers=H).text
    c = {"device_code": "C", "device_type": "surge_carrier", "attach_capacity": 4, "attach_used": 3}
    assert fl.seats(c, [])["attach"] == 1


def test_snapshot_2026_10_15_errors_count_as_done_and_salvage_messages_are_read():
    from rsweb.ingest import SALVAGE_MSG
    assert SALVAGE_MSG.search("Salvage discovered at KELHYRAS-6: a derelict hauler")
    assert not SALVAGE_MSG.search("Print completed: mining drone")
    from rsweb.ingest import salvage_message, message_unread, message_id
    m = {"message_id": 7, "is_read": False, "subject": "Salvage discovered: derelict hauler at KELHYRAS-6-SAL-2"}
    assert salvage_message(m) and message_unread(m) and message_id(m) == 7   # live wording, other field names


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


def test_simulation_prep_run_and_discard(client):
    """Queue a run: a spare compute core is pinned to the interface; start it; the run plays with the bootstrap logic
    and records its build order; at the end its devices / events / scans are purged and the report is kept."""
    from rsweb import sim
    db, eng = client.app.state.db, client.app.state.worker.automations
    client.portal.call(eng.sim_save, {"interface": {"code": "IF000001", "location": sim.DATACENTER}})
    devs = client.portal.call(db.kv_get, "devices")
    core = {"device_code": "CC000001", "device_type": "compute_core", "location": "SOL-3", "status": "idle", "tags": ["spare"]}
    devs.append(core)
    client.portal.call(db.kv_set, "devices", devs)
    client.app.state.api.http._transport.app.state.world.devices.append(dict(core))
    assert "Queue a run" in client.get("/simulations", headers=H).text
    client.post("/simulations/queue", data={"scenario": "mining_rush"}, headers=HX)
    out = client.portal.call(eng.sim_pass)
    assert any("CC000001 pinned" in x for x in out)
    row = client.portal.call(db.fetchone, "SELECT path, body FROM actions ORDER BY id DESC LIMIT 1")
    assert row["path"] == "/devices/CC000001" and "at:mirfaka-obj-1" in row["body"]
    ok, text = client.portal.call(eng.sim_start, "mining_rush", "77F75255")
    assert ok, text
    s = client.portal.call(eng.sim_state)
    assert s["active"]["id"] == 42 and "SIM8F2A1" in s["active"]["devices"] and not s.get("prep")
    assert [d for d in client.portal.call(eng.devices) if d.get("device_code", "").startswith("SIM")] == []
    client.portal.call(eng.sim_pass)                       # scans its system
    client.portal.call(eng.sim_pass)                       # then plays it like a bootstrap's home stage
    client.portal.call(db.insert_event, {"id": "simev", "event": "mining.started", "device_code": "SIM3B7C2",
                                          "star": "VIRTUHOMAM", "location": "VIRTUHOMAM-BELT-1"})
    s = client.portal.call(eng.sim_state)
    assert any(x["step"] == "system scanned" for x in s["active"]["timeline"])
    client.portal.call(eng.sim_finish, s, "completed")
    s = client.portal.call(eng.sim_state)
    assert not s.get("active") and s["runs"][-1]["scenario"] == "mining_rush"
    assert client.portal.call(db.fetchone, "SELECT COUNT(*) AS n FROM events WHERE id='simev'")["n"] == 0
    assert "Reports" in client.get("/simulations", headers=H).text


def test_explained_disappearances_show_with_their_reason(client):
    """Devices that leave the list for a known reason are listed on the Lost devices page with it."""
    from rsweb import lost
    db = client.app.state.db
    for i, (ev, code, payload, loc) in enumerate((("device.destroyed", "D1", {}, "SOL-OBJ-1"),
                                                  ("device.lost", "D2", {}, "SOL-OBJ-1"),
                                                  ("device.changed_owner", "D3", {"direction": "sent", "to_replicant": "R9"}, None))):
        client.portal.call(db.insert_event, {"id": f"g{i}", "event": ev, "device_code": code, "location": loc, "payload": payload})
    client.portal.call(db.insert_event, {"id": "g9", "event": "contract.completed", "created_at": lost._now(),
                                          "payload": {"consumed": {"devices": ["D4"]}}})
    why = client.portal.call(lost.reasons, db, ["D1", "D2", "D3", "D4", "D5"])
    assert why["D1"].startswith("destroyed at SOL-OBJ-1 (asteroid impact)") and why["D2"].startswith("lost in space")
    assert why["D3"] == "sent to replicant R9" and why["D4"] == "paid to a contract" and "D5" not in why
    s = lost.normalize({})
    lost.record_gone(s, [{"code": "D1", "type": "mining_drone", "last_location": "SOL-OBJ-1", "reason": why["D1"]}])
    client.portal.call(db.kv_set, lost.KV, s)
    assert "asteroid impact" in client.get("/lost", headers=H).text


def test_hubs_link_by_distance_and_hub_links_are_marked(client):
    """Live 2026-10-15: hubs link by distance (15 ly), not across the galaxy. A hub's links are marked; two relays both
    linked to one hub don't show their own link on the map."""
    from rsweb import relaynet
    pos = {"FAL": {"x": 0, "y": 0, "z": 0}, "SOL": {"x": 500, "y": 0, "z": 0}, "NEAR": {"x": 506, "y": 0, "z": 0},
           "NEAR2": {"x": 503, "y": 4, "z": 0}}
    devs = [{"device_code": "H1", "device_type": "system_hub", "location": "FAL-3-L4", "status": "active"},
            {"device_code": "H2", "device_type": "system_hub", "location": "SOL-3-L4", "status": "active"},
            {"device_code": "R1", "device_type": "ftl_relay", "location": "NEAR-2-L4", "status": "relaying"},
            {"device_code": "R2", "device_type": "ftl_relay", "location": "NEAR2-2-L4", "status": "relaying"}]
    rn = relaynet.networks(devs, pos)
    assert len(rn["networks"]) == 2                              # FAL's hub is 500 ly away: its own network
    assert all(l.get("hub") for l in rn["links"] if "SOL" in (l["a"], l["b"]))
    db = client.app.state.db
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": k, "position": v} for k, v in pos.items()]})
    links = client.get("/api/map.json?part=overlay", headers=H).json()["relay_net"]["links"]
    assert {frozenset((l["a"], l["b"])) for l in links} == {frozenset(("SOL", "NEAR")), frozenset(("SOL", "NEAR2"))}



def test_prospector_fleet_tours_one_prospect_per_star(client, monkeypatch):
    """A prospector fleet (observatory + carrier) moves its folded observatory to the emptiest un-prospected star within
    30 ly of home; the next prospect aims at the emptiest hemisphere."""
    from rsweb import prospecting as pr
    eng, db = client.app.state.worker.automations, client.app.state.db
    stars = [{"designation": "HOME", "position": {"x": 0, "y": 0, "z": 0}},
             {"designation": "NEAR", "position": {"x": 5, "y": 0, "z": 0}},
             {"designation": "FAR", "position": {"x": 60, "y": 0, "z": 0}}]
    stars += [{"designation": f"C{i}", "position": {"x": -10 - i * 0.1, "y": 0, "z": 0}} for i in range(5)]   # a crowd west
    client.portal.call(db.kv_set, "stars", {"stars": stars})
    assert pr.rank_directions([[1, 0, 0], [-1, 0, 0]], [0, 0, 0],
                              {s["designation"]: [s["position"][k] for k in "xyz"] for s in stars})[0] == [1, 0, 0]
    obs = {"device_code": "OBS1", "device_type": "galactic_observatory", "location": "HOME-3-L4", "status": "compacted",
           "tags": ["fleet:pro"], "available_commands": ["prospect", "unfurl"]}
    car = {"device_code": "CAR1", "device_type": "surge_carrier", "location": "HOME-3-L4", "status": "idle",
           "features": ["surge"], "attach_capacity": 4, "tags": ["fleet:pro"]}
    client.portal.call(db.kv_set, "devices", [obs, car])
    client.portal.call(db.kv_set, "prospected_from", {"HOME": {"at": "2026-10-15T00:00:00+00:00", "outcome": "3 new star(s)"}})
    client.portal.call(eng.save_fleets, [{"id": "pro", "name": "Prospector 1", "role": "prospector", "home": "HOME", "wants": {}}])
    async def covered(devices, stars=None):   # a relay network over the whole area
        return {"HOME", "NEAR", "FAR"} | {f"C{i}" for i in range(5)}
    monkeypatch.setattr(eng, "commandable", covered)
    out = client.portal.call(eng.prospector_pass)
    assert out and "HOME → " in out[0] and "FAR" not in out[0]          # FAR is beyond 30 ly
    job = [j for j in client.portal.call(eng.jobs) if "prospecting tour" in j["title"]][-1]
    assert [s["body"]["command"] for s in job["steps"] if s.get("body")] == ["attach", "travel", "detach"]
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "pro")
    assert f["station"] is False and f["tour"]["target"] == "NEAR"     # the crowd west of HOME counts against C0…C4
    assert client.get("/fleets/details?all=1", headers=H).status_code == 200



def test_prospector_fleet_picks_up_every_member_first(client, monkeypatch):
    """A prospector fleet (observatory + carrier) moves its folded observatory to the emptiest un-prospected star within
    30 ly of home; the next prospect aims at the emptiest hemisphere."""
    from rsweb import prospecting as pr
    eng, db = client.app.state.worker.automations, client.app.state.db
    stars = [{"designation": "HOME", "position": {"x": 0, "y": 0, "z": 0}},
             {"designation": "NEAR", "position": {"x": 5, "y": 0, "z": 0}},
             {"designation": "FAR", "position": {"x": 60, "y": 0, "z": 0}}]
    stars += [{"designation": f"C{i}", "position": {"x": -10 - i * 0.1, "y": 0, "z": 0}} for i in range(5)]   # a crowd west
    client.portal.call(db.kv_set, "stars", {"stars": stars})
    assert pr.rank_directions([[1, 0, 0], [-1, 0, 0]], [0, 0, 0],
                              {s["designation"]: [s["position"][k] for k in "xyz"] for s in stars})[0] == [1, 0, 0]
    obs = {"device_code": "OBS1", "device_type": "galactic_observatory", "location": "HOME-3-L4", "status": "compacted",
           "tags": ["fleet:pro"], "available_commands": ["prospect", "unfurl"]}
    car = {"device_code": "CAR1", "device_type": "surge_carrier", "location": "HOME-3-L4", "status": "idle",
           "features": ["surge"], "attach_capacity": 4, "tags": ["fleet:pro"]}
    mat = {"device_code": "MAT1", "device_type": "matrix_container", "location": "HOME-BELT-1", "status": "idle",
           "tags": ["fleet:pro"]}
    client.portal.call(db.kv_set, "devices", [obs, car, mat])
    client.portal.call(db.kv_set, "prospected_from", {"HOME": {"at": "2026-10-15T00:00:00+00:00", "outcome": "3 new star(s)"}})
    client.portal.call(eng.save_fleets, [{"id": "pro", "name": "Prospector 1", "role": "prospector", "home": "HOME", "wants": {}}])
    async def covered(devices, stars=None):   # a relay network over the whole area
        return {"HOME", "NEAR", "FAR"} | {f"C{i}" for i in range(5)}
    monkeypatch.setattr(eng, "commandable", covered)
    out = client.portal.call(eng.prospector_pass)
    assert out and "HOME → " in out[0] and "FAR" not in out[0]          # FAR is beyond 30 ly
    job = [j for j in client.portal.call(eng.jobs) if "prospecting tour" in j["title"]][-1]
    cmds = [(s["path"].split("/")[2], s["body"]["command"], s["body"].get("device") or s["body"].get("destination"))
            for s in job["steps"] if s.get("body")]
    assert cmds[0] == ("CAR1", "travel", "HOME-BELT-1") and ("CAR1", "travel", "HOME-3-L4") in cmds   # fetch, come back
    assert any(c[0] == "MAT1" or c[2] == "MAT1" for c in cmds)                                       # MAT1 boards
    assert [c[1] for c in cmds][-3:] == ["attach", "travel", "detach"] and "picking up MAT1" in client.portal.call(
        eng.fleets)[0]["tour"]["note"]
    f = next(x for x in client.portal.call(eng.fleets) if x["id"] == "pro")
    assert f["station"] is False and f["tour"]["target"] == "NEAR"     # the crowd west of HOME counts against C0…C4
    assert client.get("/fleets/details?all=1", headers=H).status_code == 200


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


def test_a_relay_out_of_range_is_a_network_of_its_own():
    """Owner 2026-10-15: a relay the game reports out of range starts its own network (its own color)."""
    from rsweb import relaynet
    pos = {"A": {"x": 0, "y": 0, "z": 0}, "B": {"x": 3, "y": 0, "z": 0}, "K": {"x": 40, "y": 0, "z": 0}}
    devs = [{"device_code": "R1", "device_type": "ftl_relay", "location": "A-1-L4", "status": "relaying"},
            {"device_code": "R2", "device_type": "ftl_relay", "location": "B-1-L4", "status": "relaying"},
            {"device_code": "R3", "device_type": "ftl_relay", "location": "K", "status": "out_of_range"}]
    assert "K" not in relaynet.networks(devs, pos)["of"]         # out of range reads the same switched off
    devs[2]["_on"] = True                                         # … switched on (web.relays_on): its own network
    rn = relaynet.networks(devs, pos)
    assert rn["of"]["A"] == rn["of"]["B"] != rn["of"]["K"]
    assert len({n["color"] for n in rn["networks"]}) == 2


def test_relays_switched_off_get_a_red_ring_unless_their_system_has_a_working_one(client):
    """Owner 2026-10-15: a relay switched off gets a red ring, but not in a system with a working relay of yours."""
    import json as _json
    from types import SimpleNamespace
    from rsweb import web
    db = client.app.state.db
    devs = [{"device_code": "R1", "device_type": "ftl_relay", "location": "A-1-L4", "status": "relaying"},
            {"device_code": "R0", "device_type": "ftl_relay", "location": "A-1-L4", "status": "inactive"},
            {"device_code": "R2", "device_type": "ftl_relay", "location": "B-1-L4", "status": "inactive"},
            {"device_code": "R3", "device_type": "ftl_relay", "location": "K", "status": "out_of_range"},
            {"device_code": "R4", "device_type": "ftl_relay", "location": "M", "status": "out_of_range"}]
    client.portal.call(db.execute, "INSERT INTO actions(at, user, method, path, body, status, response) VALUES(?,?,?,?,?,?,?)",
                       ("2026-10-15T16:00:00+00:00", "you", "POST", "/devices/R3", _json.dumps({"command": "activate"}), 200, "{}"))
    marked = client.portal.call(web.relays_on, db, devs)
    assert [d["device_code"] for d in marked if d.get("_on")] == ["R3"]
    pos = {"A": {"x": 0, "y": 0, "z": 0}, "B": {"x": 30, "y": 0, "z": 0}, "K": {"x": 60, "y": 0, "z": 0},
           "M": {"x": 90, "y": 0, "z": 0}}
    req = SimpleNamespace(app=client.app)
    out = client.portal.call(web.relay_overlay, req, {"devices": devs, "replicants": {}}, pos)
    assert out["off"] == ["B", "M"]   # A has a working relay; K is out of range but on: a network of its own
    # a replicant visits M: the list shows R4 relaying for a while; back out of range it stays on
    w = client.app.state.worker
    client.portal.call(w._note_relay_states, [dict(devs[4], status="relaying")])
    out = client.portal.call(web.relay_overlay, req, {"devices": devs, "replicants": {}}, pos)
    assert out["off"] == ["B"] and any(n["star"] == "M" for n in out["nodes"])


def test_megastructures_for_the_map(client):
    """Owner 2026-10-15: a symbol on the galaxy map where megastructures are."""
    import json as _json
    from rsweb import megas
    db = client.app.state.db
    scan = {"planets": [], "objects": [{"designation": "FLARM-OBJ-1", "location_type": "megastructure"},
                                       {"designation": "FLARM-OBJ-2", "location_type": "object"}]}
    assert set(megas.in_scan(scan)) == {"FLARM-OBJ-1"}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("FLARM", _json.dumps(scan), "2026-10-15T00:00:00+00:00"))
    client.portal.call(db.insert_event, {"id": "m1", "event": "megastructure.contributed", "location": "VEGA-OBJ-4",
                                          "payload": {"accepted_count": 1}})
    got = client.portal.call(megas.known, db)
    assert got["FLARM"] == ["FLARM-OBJ-1"] and got["VEGA"] == ["VEGA-OBJ-4"] and got["MIRFAKA"] == ["MIRFAKA-OBJ-1"]


def test_an_impact_warning_is_filed_under_the_asteroids_designation(client):
    """Owner 2026-10-15: an incoming asteroid is named by its location designation. Live, the warning's location was
    KELMORNEA-4-L4 (where devices were): it goes under the asteroid tracked in that system, and devices there leave."""
    from rsweb import evacuate as ev
    db, eng = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.insert_event, {"id": "d1", "event": "system.object_detected", "location": "KELM-OBJ-2",
                                          "created_at": "2026-10-15T09:00:00+00:00",
                                          "payload": {"impact_target": "KELM-4", "impact_eta": "2099-10-15T11:00:00Z"}})
    client.portal.call(db.insert_event, {"id": "w9", "event": "system.impact_warning", "location": "KELM-4-L4",
                                          "created_at": "2099-10-15T10:00:00+00:00",
                                          "payload": {"destination": "KELM-4", "impact_eta": "2099-10-15T11:00:00Z",
                                                      "device_codes": ["D1"]}})
    objs = client.portal.call(eng.defence_objects)
    assert "KELM-4-L4" not in objs and objs["KELM-OBJ-2"]["also_at"] == ["KELM-4-L4"]
    assert objs["KELM-OBJ-2"]["warned_devices"] == ["D1"]
    assert set(ev.due(objs, warned={"KELM-OBJ-2"})) == {"KELM-OBJ-2", "KELM-4-L4"}
    page = client.get("/defence", headers=H).text
    assert "KELM-OBJ-2" in page and "warned your devices at" in page


def test_docs_coverage_pages_2026_10_15(client):
    """Interfaces for the docs not covered before: terraforming, research, simulation leaderboards, equipment lockers."""
    import json as _json
    db = client.app.state.db
    status = {"body": "SOL-3", "attributes": {"temperature": {"value": 361.7, "delta": 0.2}, "pressure": {"value": 0.34, "delta": 0},
                                              "oxygen": {"value": 5.2, "delta": 0.1}, "toxicity": {"value": 63.9, "delta": -0.4},
                                              "hydrosphere": {"value": 8.1, "delta": 0.05}, "tectonic": {"value": 15, "delta": 0},
                                              "biosphere": {"value": 0, "delta": 0}},
              "projections": {"temperature": {"+6": 360.8}}, "warnings": ["temperature approaching Evaporation threshold (373)"],
              "effects": {"devices": {}, "equilibrium": [{"attribute": "temperature", "direction": "decrease", "strength": "weak"}]}}
    client.portal.call(db.insert_event, {"id": "tf1", "event": "terraform.status", "location": "SOL-3", "payload": status})
    page = client.get("/terraforming", headers=H).text
    assert "SOL-3" in page and "Evaporation" in page and "Toxic suppression" in page and "Bio seeders" in page
    assert "MARYR" not in page
    frag = client.get("/terraforming/kuiper/SOL", headers=HX).text
    assert "SOL-KUIPER-001" in frag
    from rsweb import terraform as tf
    assert [c["rule"] for c in tf.coupling(status) if c["active"]] == ["Toxic suppression"]
    assert {b["attr"] for b in tf.bio_ready(status) if not b["ok"]} == {"temperature", "oxygen", "toxicity", "hydrosphere"}
    assert client.get("/lockers", headers=H).status_code == 200
    assert "Leaderboards" in client.get("/simulations", headers=H).text
    assert "Mining Rush" in client.get("/simulations/leaderboard", headers=HX).text
    assert "Bob" in client.get("/simulations/leaderboard?scenario=mining_rush", headers=HX).text
    # a rock you launched isn't a threat
    from rsweb import defence as dfn
    a = dfn.assess({"designation": "SOL-OBJ-9", "impact_eta": "2099-01-01T00:00:00Z", "launched_by_you": True}, [], {})
    assert a["verdict"] == "launched by you" and not a["threat"]


def test_auto_scout_deploys_worn_members_for_repairs(client):
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(eng.save_fleets, [{"id": "x1", "name": "Scouts", "role": "explore", "home": "SOL", "wants": {},
                                          "auto_scout": True}])
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": "SHIP0001", "device_type": "heaven_vessel", "location": "SOL-3", "status": "idle",
                 "operational_capacity": 1.0, "tags": ["fleet:x1"]},
                {"device_code": "WORN0001", "device_type": "survey_drone", "location": "SOL-3", "status": "stowed",
                 "stowed_in_device_code": "SHIP0001", "operational_capacity": 0.6, "tags": ["fleet:x1"]}]
    client.portal.call(db.kv_set, "devices", devices)
    sent = []
    real = eng.send

    async def send(method, path, body, label):
        sent.append((path, body))
        return True, {}, None
    eng.send = send
    try:
        out = client.portal.call(eng.auto_scout_pass)
        assert out == ["Scouts: deployed WORN0001 for repairs"]
        assert sent == [("/devices/WORN0001", {"command": "deploy"})]
        assert client.portal.call(eng.auto_scout_pass) == [] and len(sent) == 1    # not again while the list lags
    finally:
        eng.send = real


def test_fleets_listed_alphabetically(client):
    eng = client.app.state.worker.automations
    client.portal.call(eng.save_fleets, [{"id": f"z{i}", "name": n, "role": "explore", "home": "SOL", "wants": {}}
                                         for i, n in enumerate(("Zulu Fleetq", "alpha Fleetq", "Mike Fleetq"))])
    for url in ("/fleets/overview", "/fleets/details?all=1"):
        t = client.get(url, headers=HX).text
        mark = "?fleet=" if url.endswith("overview") else 'id="fleet-'   # z1 alpha, z2 Mike, z0 Zulu
        assert t.index(mark + "z1") < t.index(mark + "z2") < t.index(mark + "z0"), url


def test_attach_already_attached(client):
    from rsweb.automations import step
    eng, db = client.app.state.worker.automations, client.app.state.db
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": c, "device_type": "cargo_vessel", "location": "SOL-3", "status": "idle", "tags": t}
                for c, t in (("CAR00001", ["fleet:m3"]), ("CAR00002", ["fleet:m3"]), ("CAR00003", ["fleet:other"]))]
    devices.append({"device_code": "OBS0000A", "device_type": "observatory", "location": "SOL-3", "status": "attached"})
    client.portal.call(db.kv_set, "devices", devices)
    sent = []

    async def fake_send(method, path, body, why):
        sent.append((path, body.get("command")))
        if body.get("command") == "attach" and len([s for s in sent if s[1] == "attach"]) == 1:
            return False, None, "Target device is already attached to a carrier"
        return True, {}, None
    orig = eng.send
    eng.send = fake_send
    try:
        for on, carrier in (("CAR00001", "CAR00001"), ("CAR00001", "CAR00002"), ("CAR00003", "CAR00002"), (None, "CAR00002")):
            cached = client.portal.call(db.kv_get, "devices")
            next(d for d in cached if d["device_code"] == "OBS0000A")["attached_to_device_code"] = on
            client.portal.call(db.kv_set, "devices", cached)
            sent.clear()
            job = client.portal.call(eng.create_job, "fleets", "attach test", carrier,
                                     [step(f"{carrier}: attach OBS0000A", f"/devices/{carrier}",
                                           {"command": "attach", "device": "OBS0000A"}, critical=True)], {"devices": []}, True)
            j = next(x for x in client.portal.call(eng.jobs) if x["id"] == job["id"])
            assert j["status"] != "failed" and j["steps"][0]["status"] == "done", (on, carrier)
            if on in ("CAR00001",):   # this carrier, or another of the fleet: done
                assert sent == [(f"/devices/{carrier}", "attach")], (on, carrier)
            elif on == "CAR00003":    # a carrier outside the fleet: it detaches it, then attach again
                assert sent[:2] == [(f"/devices/{carrier}", "attach"), ("/devices/CAR00003", "detach")], (on, carrier, sent)
                assert [s["desc"].split(" ")[0] for s in j["steps"]][:2] == [f"{carrier}:", "CAR00003:"]
            else:                     # no carrier named: deploy it, then attach again
                assert sent == [(f"/devices/{carrier}", "attach"), ("/devices/OBS0000A", "deploy")], (on, carrier, sent)  # then waits
                assert [s["desc"].split(" ")[0] for s in j["steps"]] == [f"{carrier}:", "deploy", f"{carrier}:"]
    finally:
        eng.send = orig


def test_return_load_takes_what_is_left(client):
    from rsweb.automations import step
    eng = client.app.state.worker.automations
    sent = []

    async def fake_send(method, path, body, why):
        sent.append(dict(body.get("resources") or {}))
        if body.get("resources", {}).get("structural", 0) > 5:
            return False, None, "Insufficient structural at location: need 18.0, have 5"
        return True, {}, None
    orig = eng.send
    eng.send = fake_send
    try:
        for req, want in (("return:X-BELT", "done"), ("contract:EV1", "failed")):
            sent.clear()
            job = client.portal.call(eng.create_job, "logistics", f"short load {req}", "F1",
                                     [step("F1: load 14 conductive, 18 structural at X-BELT", "/devices/F1",
                                           {"command": "collect_resources", "resources": {"conductive": 14, "structural": 18}},
                                           critical=True)], {"devices": ["F1"], "logistics": {"req": req}}, True)
            j = next(x for x in client.portal.call(eng.jobs) if x["id"] == job["id"])
            assert j["status"] == want, (req, j["status"])
            if want == "done":   # a pile going home loads what's there; a contract's price can't come up short
                assert sent == [{"conductive": 14, "structural": 18}, {"conductive": 14, "structural": 5}]
    finally:
        eng.send = orig

def test_unescorted_trip_does_not_leave_for_a_dark_system(client):
    from rsweb.automations import step
    eng = client.app.state.worker.automations
    sent = []

    async def fake_send(method, path, body, why):
        sent.append(path)
        return True, {}, None
    orig = eng.send
    eng.send = fake_send
    try:
        st = step("F1 → NOWHERE-3-1", "/devices/F1", {"command": "travel", "destination": "NOWHERE-3-1"}, critical=True)
        st["need_comms"] = "NOWHERE"
        job = client.portal.call(eng.create_job, "logistics", "dark trip", "F1", [st], {"devices": ["F1"]}, True)
        j = next(x for x in client.portal.call(eng.jobs) if x["id"] == job["id"])
        assert j["status"] == "failed" and sent == [] and "NOWHERE is out of comms range" in j["steps"][0]["error"]
    finally:
        eng.send = orig

def test_scanning_drone_finishes_its_scan_before_moving():
    from rsweb.automations import step
    from rsweb.modular import prepare_moves
    devices = [{"device_code": "SD000001", "device_type": "survey_drone", "location": "SOLARES-4", "status": "scanning"},
               {"device_code": "SD000002", "device_type": "survey_drone", "location": "SOLARES-4", "status": "idle"}]
    steps = [step(f"{c} → SOLARES-4-L4 (board)", f"/devices/{c}", {"command": "travel", "destination": "SOLARES-4-L4"})
             for c in ("SD000001", "SD000002")]
    steps[1]["seq0_from"] = 0
    out = prepare_moves(steps, devices, {}, {})
    assert out[0]["method"] == "WAIT" and out[0]["wait"] == ["scan.completed"] and out[0]["wait_device"] == "SD000001"
    assert [s["desc"] for s in out[1:]] == [s["desc"] for s in steps] and out[2]["seq0_from"] == 1


def test_dashboard_shows_achievements(client):
    """GET /accounts/achievements (yours) against GET /achievements (all): earned with XP, the rest still to earn."""
    assert 'hx-get="/partials/achievements"' in client.get("/", headers=H).text
    body = client.get("/partials/achievements", headers=HX).text
    assert "1 of 2 earned · 100 XP" in body and "First Scan" in body
    assert "1 still to earn" in body and "Signal Fire" in body and "17 players" in body


def test_simulation_interface_known_and_matched_loosely(client):
    from rsweb import sim
    eng = client.app.state.worker.automations
    s = client.portal.call(eng.sim_state)
    assert s["interface"]["code"] == "F4791EDF" and s["interface"]["location"] == "MIRFAKA-OBJ-1"
    assert sim.is_interface({"device_type": "replicant interface"}) and sim.is_interface({"device_type": "replicant_interface"})
    assert not sim.is_interface({"device_type": "compute_core"})


def test_simulation_run_started_elsewhere_is_taken_over(client):
    eng = client.app.state.worker.automations
    r = client.app.state.api   # start it in the game only, as if the app's record had been lost
    client.portal.call(lambda: r.request("POST", "/devices/F4791EDF/simulate",
                                         json_body={"scenario": "mining_rush", "replicant_code": "77F75255"}))
    eng._sim_adopt_t = 0
    out = client.portal.call(eng.sim_pass)
    assert any("took over Mining Rush #42" in x for x in out), out
    s = client.portal.call(eng.sim_state)
    assert s["active"]["id"] == 42 and s["active"]["replicant"] == "77F75255" and s["active"]["adopted"]
    assert "Runs in the game" in client.get("/simulations", headers=H).text


def test_simulated_world_kept_apart(client):
    db = client.app.state.db
    devices = client.portal.call(db.kv_get, "devices")
    n = len(devices)
    devices += [{"device_code": "459B6E13", "device_type": "heaven_vessel", "location": "VIRTCRYSARON-BELT-1", "status": "idle"},
                {"device_code": "E4B28B5D", "device_type": "mining_drone", "location": None, "status": "stowed",
                 "stowed_in_device_code": "459B6E13"}]
    client.portal.call(db.kv_set, "devices", devices)
    assert len(client.portal.call(db.kv_get, "devices")) == n
    assert {d["device_code"] for d in client.portal.call(db.kv_get, "sim:devices")} == {"459B6E13", "E4B28B5D"}
    client.portal.call(db.kv_set, "inventory", [{"location": "VIRTCRYSARON-BELT-1", "items": {"silicates": 40}},
                                                {"location": "SOL-BELT-1", "items": {"structural": 5}}])
    assert [x["location"] for x in client.portal.call(db.kv_get, "inventory")] == ["SOL-BELT-1"]
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("VIRTCRYSARON", '{"star": "VIRTCRYSARON"}', "2026-10-16"))
    assert not client.portal.call(db.fetchall, "SELECT star FROM systems WHERE star='VIRTCRYSARON'")
    assert "VIRTCRYSARON" in client.portal.call(db.kv_get, "sim:systems")
    w = client.app.state.worker
    client.portal.call(w.handle_event, _ev(990, "scan.completed", device="459B6E13", location="VIRTCRYSARON-3"))
    row = client.portal.call(db.fetchone, "SELECT category FROM events WHERE id='1990-0'")
    assert row["category"] == "simulation"
    client.portal.call(db.kv_set, "devices", devices)   # (the mock's own syncs list no simulated devices)
    t = client.get("/simulations", headers=H).text
    assert "Virtual galaxy" in t


def test_build_order_parse_and_play():
    from rsweb import sim_order
    steps, errors = sim_order.parse("scan system\ndeploy all\nmine: structural, conductive\nprint 2 mining_drone\n"
                                    "wait: 500 structural\nmine until objective\n# note\nfly away")
    assert [s["kind"] for s in steps] == ["scan", "deploy", "mine", "print", "wait", "mine_all"]
    assert errors == ["line 8: not understood: fly away"]
    a = {"order": steps}
    belt, now = "VIRTX-BELT-1", "2026-10-16T00:00:00+00:00"
    vessel = {"device_code": "V1", "device_type": "heaven_vessel", "location": "VIRTX-3-L4", "status": "idle"}
    devs = [vessel, {"device_code": "M1", "device_type": "mining_drone", "stowed_in_device_code": "V1"},
            {"device_code": "M2", "device_type": "mining_drone", "stowed_in_device_code": "V1"}]
    acts, note = sim_order.advance(a, devs, vessel, belt, {}, True, now)
    assert acts == [{"kind": "travel", "device": "V1", "to": belt}] and a["order_idx"] == 1   # scan done, flying out
    vessel["location"] = belt
    acts, _ = sim_order.advance(a, devs, vessel, belt, {}, True, now)
    assert [x["kind"] for x in acts] == ["deploy", "deploy"]
    for d in devs[1:]:
        d.pop("stowed_in_device_code"); d["location"], d["status"] = belt, "idle"
    acts, _ = sim_order.advance(a, devs, vessel, belt, {}, True, now)
    assert [(x["device"], x["resource"]) for x in acts] == [("M1", "structural"), ("M2", "conductive")]
    for d in devs[1:]:
        d["status"] = "mining"
    acts, _ = sim_order.advance(a, devs, vessel, belt, {}, True, now)
    assert acts == [{"kind": "print", "device_type": "mining_drone"}] and a["order_idx"] == 3
    vessel["status"] = "printing"
    assert sim_order.advance(a, devs, vessel, belt, {}, True, now)[0] == []
    devs += [{"device_code": f"M{i}", "device_type": "mining_drone", "location": belt, "status": "mining"} for i in (3, 4)]
    vessel["status"] = "idle"
    acts, note = sim_order.advance(a, devs, vessel, belt, {"structural": 120}, True, now)
    assert acts == [] and "120 so far" in note and a["order_idx"] == 4
    acts, note = sim_order.advance(a, devs, vessel, belt, {"structural": 600}, True, now)
    assert a["order_idx"] == 5 and note == "mine until objective"


def test_build_order_saved_and_followed(client):
    eng = client.app.state.worker.automations
    r = client.post("/simulations/orders", data={"name": "Fast", "text": "scan system\nmine: structural 4, conductive 2, carbon, silicates\nprint 1 mining_drone"}, headers=HX)
    assert "Saved Fast (3 steps)" in r.text
    assert "Not saved" in client.post("/simulations/orders", data={"name": "Bad", "text": "dance"}, headers=HX).text
    page = client.get("/simulations", headers=H).text
    assert "Build orders" in page and "the next run follows <b>Fast</b>" in page
    assert "boAdd('deploy all')" in page and 'class="bo-res" value="carbon"' in page and "<option>mining_drone</option>" in page.replace(" selected", "")
    ok, _ = client.portal.call(eng.sim_start, "mining_rush", "77F75255")
    a = client.portal.call(eng.sim_state)["active"]
    assert ok and a["order_name"] == "Fast" and [x["kind"] for x in a["order"]] == ["scan", "mine", "print"]


def test_simulation_runs_end_early_and_world(client):
    eng = client.app.state.worker.automations
    ok, _ = client.portal.call(eng.sim_start, "mining_rush", "77F75255")
    assert ok
    t = client.get("/simulations/active", headers=HX).text
    assert "#42" in t and "End early" in t and 'href="/fleet?sim=42"' in t and 'href="/map?sim=42"' in t
    w = client.get("/fleet?sim=42", headers=H).text
    assert "Simulation #42" in w and "SIM8F2A1" in w and "11ADA230" not in w and "REAL0001" not in w   # only the run's
    assert 'dataUrl: "/api/map.json?sim=42"' in client.get("/map?sim=42", headers=H).text
    j = client.get("/api/map.json?sim=42&part=core", headers=H).json()
    assert "11ADA230" not in str(j)
    assert "11ADA230" in client.get("/fleet", headers=H).text   # the real list is untouched
    r = client.post("/simulations/end", data={"sim_id": "42"}, headers=HX)
    assert r.status_code == 200
    s = client.portal.call(eng.sim_state)
    assert not s.get("active") and s["runs"][-1]["id"] == 42
    assert "No simulation running" in client.get("/simulations/active", headers=HX).text


def test_worn_fleets_return_home_and_deploy_for_repairs(client):
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(eng.save_fleets, [
        {"id": "m1", "name": "Miners", "role": "mining", "home": "SOL", "wants": {},
         "mission": {"status": "running", "phase": "watch", "targets": ["AEM"], "log": []}},
        {"id": "m2", "name": "Back", "role": "mining", "home": "SOL", "wants": {}, "mission": {"status": "done", "log": []}}])
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": "WORNM001", "device_type": "mining_drone", "location": "AEM-BELT-1", "status": "mining",
                 "operational_capacity": 45.0, "tags": ["fleet:m1"]},
                {"device_code": "SHIPM002", "device_type": "cargo_vessel", "location": "SOL-3", "status": "idle",
                 "operational_capacity": 100.0, "tags": ["fleet:m2"]},
                {"device_code": "WORNM002", "device_type": "mining_drone", "location": "SOL-3", "status": "stowed",
                 "stowed_in_device_code": "SHIPM002", "operational_capacity": 70.0, "tags": ["fleet:m2"]}]
    client.portal.call(db.kv_set, "devices", devices)
    sent = []
    real = eng.send

    async def send(method, path, body, label):
        sent.append((path, body))
        return True, {}, None
    eng.send = send
    try:
        out = client.portal.call(eng.repair_return_pass)
    finally:
        eng.send = real
    fl = {f["id"]: f for f in client.portal.call(eng.fleets)}
    assert fl["m1"]["mission"]["recall_now"] and fl["m1"]["mission"]["repair_return"]
    assert "Miners: home for repairs" in out and "Back: deployed WORNM002 for repairs" in out
    assert sent == [("/devices/WORNM002", {"command": "deploy"})]


def test_print_components_used_and_returned(client):
    from rsweb import lost
    db, w = client.app.state.db, client.app.state.worker
    now = datetime.now(timezone.utc)
    client.portal.call(w.handle_event, _ev(2001, "print.started", device="FAC00001", created=now - timedelta(minutes=5),
                                           device_type="ami_mining_controller", component_device_codes=["USED0001", "BACK0001"]))
    client.portal.call(w.handle_event, _ev(2002, "print.cancelled", device="FAC00001", created=now - timedelta(minutes=2),
                                           device_type="ami_mining_controller", returned_device_codes=["BACK0001"]))
    got = client.portal.call(lost.given_away, db, {"USED0001": ("SOL-BELT-1", "mining_drone"),
                                                   "BACK0001": ("SOL-BELT-1", "mining_drone")})
    assert got == {"USED0001"}
    why = client.portal.call(lost.reasons, db, ["USED0001", "BACK0001"])
    assert why == {"USED0001": "used in a print"}
    body = client.get("/partials/tutorials", headers=HX).text
    assert "On graduation:" in body and "graduate: 100 XP" in body


def test_lookbacks_compare_real_times_not_the_games_offset(client):
    """The game stamps events in -04:00 (live 2026-10-20): a print 46 h ago is inside the 48 h window, though its text
    sorts before a UTC cutoff."""
    from rsweb import lost
    db, w = client.app.state.db, client.app.state.worker
    at = (datetime.now(timezone.utc) - timedelta(hours=46)).astimezone(timezone(timedelta(hours=-4)))
    ev = {**_ev(2101, "print.started", device="FAC00002", device_type="ami_mining_controller",
                component_device_codes=["OLD00001"]), "created_at": at.isoformat(timespec="seconds")}
    client.portal.call(w.handle_event, ev)
    assert client.portal.call(lost.reasons, db, ["OLD00001"]) == {"OLD00001": "used in a print"}


def test_spec_endpoints_pages_and_cards(client):
    t = client.get("/leaderboards", headers=H).text
    assert 'hx-get="/partials/leaderboard/xp"' in t and "Distance" in t
    assert "101" in client.get("/partials/leaderboard/xp", headers=HX).text
    assert "Game API ok" in client.get("/partials/health", headers=HX).text
    x = client.get("/partials/system-extra/SOL", headers=HX).text
    assert "Famine Signal" in x and "structural 120" in x and "FARAWAYX" in x
    cat = client.portal.call(client.app.state.db.kv_get, "stars")
    assert any(s.get("designation") == "FARAWAYX" for s in cat["stars"])   # nearby stars join the map
    assert "Arrived at SOL-3" in client.get("/partials/replicant-events/77F75255", headers=HX).text
    client.post("/devices/HV2E8F3A/permissions", data={"permission_type": "command", "account_id": "42"}, headers=HX)
    p = client.get("/partials/permissions/HV2E8F3A", headers=HX).text
    assert "command" in p and ">42<" in p and "revoke" in p
    b = client.get("/blog", headers=H).text
    assert "New shop location" in b and "Write a post" in b
    client.post("/blog", data={"replicant_name": "Bob", "subject": "Hello", "content": "From the app"}, headers=HX)
    assert "Hello" in client.get("/blog", headers=H).text
    assert 'href="/leaderboards"' in client.get("/messages", headers=H).text and 'hx-get="/partials/health"' in client.get("/", headers=H).text


def test_assemble_stops_controllers_and_scans_are_waited_out(client):
    from rsweb import fleets as fl
    from rsweb.automations import step
    fleet = {"id": "m5", "name": "Miner 0.5", "role": "mining", "home": "BADU", "wants": {}}
    t = ["fleet:m5"]
    devices = [{"device_code": "CAR9", "device_type": "cargo_vessel", "location": "BADU-3-L4", "status": "idle", "tags": t,
                "features": ["surge", "cruise", "attach", "stow"], "attach_capacity": 3, "stow_capacity": 20},
               {"device_code": "TC1", "device_type": "ami_transport_controller", "location": "BADU-3-L4", "status": "coordinating",
                "tags": t, "ami_directive": {"name": "gather"}},
               {"device_code": "TD1", "device_type": "transport_drone", "location": "BADU-BELT-1", "status": "collecting",
                "controller_device_code": "TC1", "tags": t, "available_commands": ["travel"]}]
    steps, _ = fl.assemble_steps(fleet, devices)
    cmds = [(s["path"].split("/")[2], (s.get("body") or {}).get("command")) for s in steps if s.get("body")]
    assert cmds[:2] == [("TC1", "clear_directive"), ("TC1", "release")] and ("TD1", "travel") in cmds
    eng = client.app.state.worker.automations
    calls = []

    async def send(method, path, body, label):
        calls.append((path, (body or {}).get("command")))
        if len(calls) == 1:
            return False, None, "Cannot cruise while scanning"
        return True, {}, None
    real = eng.send
    eng.send = send
    try:
        job = client.portal.call(eng.create_job, "fleets", "scan test", "SD1",
                                 [step("SD1 → X-1 (board)", "/devices/SD1", {"command": "travel", "destination": "X-1"})],
                                 {"devices": []}, True)
    finally:
        eng.send = real
    j = next(x for x in client.portal.call(eng.jobs) if x["id"] == job["id"])
    assert j["status"] != "failed" and j["steps"][1]["wait"] == ["scan.completed", "search.completed"]
    assert j["steps"][2]["scan_retry"] and j["steps"][2]["body"]["command"] == "travel"


def test_supplying_fleet_sends_to_the_nearest_receiver(client):
    from rsweb import fleets as fl
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": d, "position": {"x": x, "y": 0, "z": 0}}
                                                      for d, x in (("MINE", 0), ("NEARHUB", 3), ("FARHUB", 40))]})
    client.portal.call(eng.save_fleets, [
        {"id": "far", "name": "Far hub", "role": "mining", "home": "FARHUB", "wants": {}, "materials": "self"},
        {"id": "near", "name": "Near hub", "role": "mining", "home": "NEARHUB", "wants": {}, "materials": "self"},
        {"id": "m", "name": "Miner", "role": "mining", "home": "MINE", "wants": {}, "materials": ""}])
    r = client.post("/fleets/m/station", data={"materials": "supply"}, headers=HX)
    assert r.status_code == 200
    items = client.portal.call(eng.fleets)
    m = next(f for f in items if f["id"] == "m")
    assert m["materials"] == "supply" and fl.materials_target(m, items)["id"] == "near"
    t = client.get("/fleets/overview", headers=HX).text
    assert "supplying the nearest receiver" in t and "to Near hub (NEARHUB), the nearest" in t


def test_device_page_links_host_and_cargo_codes(client):
    db = client.app.state.db
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": "HOSTV001", "device_type": "cargo_vessel", "location": "SOL-3", "status": "idle",
                 "stowed_devices": ["RIDE0001"], "attached_devices": [{"device_code": "TOW00001", "device_type": "autofactory"}]},
                {"device_code": "RIDE0001", "device_type": "mining_drone", "location": None, "status": "stowed",
                 "stowed_in_device_code": "HOSTV001"}]
    client.portal.call(db.kv_set, "devices", devices)
    t = client.get("/devices/RIDE0001", headers=H).text
    assert '<a class="mono" href="/devices/HOSTV001">HOSTV001</a>' in t
    t = client.get("/devices/HOSTV001", headers=H).text
    assert 'href="/devices/RIDE0001">RIDE0001</a>' in t and 'href="/devices/TOW00001">TOW00001</a>' in t


def test_star_sizes_on_the_maps(client):
    """Stars are drawn by radius: a scan's radius_solar when known, else the typical radius of the spectral class."""
    from rsweb.web import build_system_view, star_radius
    assert star_radius({"spectral_type": "M3V"}) == (0.4, False)
    assert star_radius({"spectral_type": "K1III"}) == (8.0, False)
    assert star_radius({"spectral_type": "G2"}, 1.4) == (1.4, True)
    assert star_radius({}) == (None, False)
    sol = build_system_view("SOL", {"star": {"spectral_type": "G2", "radius_solar": 1.0}}, [], [])
    red = build_system_view("RED", {"star": {"spectral_type": "M5V"}}, [], [])
    assert sol["star_r"] == 12 and 5 <= red["star_r"] < 12 and not red["star_radius"]["measured"]
    db = client.app.state.db
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": "BIG", "position": {"x": 1, "y": 0, "z": 0},
                                                       "spectral_type": "B1"},
                                                      {"designation": "ODD", "position": {"x": 9, "y": 0, "z": 0}}]})
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("BIG", json.dumps({"star": {"radius_solar": 6.2}}), "2026-10-17"))
    stars = {s["designation"]: s for s in client.get("/api/map.json?part=core", headers=H).json()["stars"]}
    assert stars["BIG"]["radius"] == 6.2 and stars["BIG"]["radius_measured"] and "radius" not in stars["ODD"]


def test_simulation_next_step_follows_a_finished_job(client):
    """A run's clock is fast: when one of its jobs finishes, its next step is played at once, not on the next tick."""
    eng = client.app.state.worker.automations
    calls = []

    async def fake_pass(s):
        calls.append(s["active"]["id"])
        return []
    ok, _ = client.portal.call(eng.sim_start, "mining_rush", "77F75255")
    assert ok
    eng.sim_run_pass = fake_pass
    job = {"id": "simjob", "title": "t", "rule": "simulation", "status": "running", "idx": 0, "steps": [],
           "meta": {"sim": 42}}

    async def finish():
        jobs = await eng.jobs()
        await eng.save_jobs(jobs + [job])
        await eng._advance("simjob")
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
    client.portal.call(finish)
    assert calls == [42]


def test_print_queue_reorder_and_keep_in_stock(client):
    """The game can't reorder a queue: moving a print takes the waiting prints off from its new place down and queues
    them again. A printing hub keeps its stock: one print at a time on an idle factory, tagged spare + stock, and a
    waiting stock print goes behind any other print."""
    from rsweb import printqueue as pq, stock
    db, eng = client.app.state.db, client.app.state.worker.automations
    w = client.app.state.api.http._transport.app.state.world
    af = next(d for d in w.devices if d["device_code"] == "AF00BEEF")
    af["status"] = "printing (mining_drone)"
    w.queues["AF00BEEF"] = [{"device_type": t, "notify": {"device": None}, "tags": tags}
                            for t, tags in (("ftl_relay", ["spare", "stock"]), ("mining_drone", ["fleet:x"]),
                                            ("cargo_freighter", []))]
    q = pq.items({"print_queue": w.queues["AF00BEEF"]})
    t = client.post("/devices/AF00BEEF/print-queue", data={"action": "top", "index": 2}, headers=HX).text
    assert [x["device_type"] for x in w.queues["AF00BEEF"]] == ["cargo_freighter", "ftl_relay", "mining_drone"]
    assert w.queues["AF00BEEF"][2]["tags"] == ["fleet:x"] and "⤒" in t
    assert stock.stock_last(q)[-1]["device_type"] == "ftl_relay"
    # keep in stock: SOL's receiving fleet wants one relay; the factory there is idle and nothing is in stock
    af["status"] = "idle"
    w.queues["AF00BEEF"] = []
    devs = client.portal.call(db.kv_get, "devices")
    for d in devs:
        if d["device_code"] == "AF00BEEF":
            d.update(status="idle", print_queue=[], tags=[])
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(eng.save_fleets, [{"id": "hub", "name": "Hub", "role": "mining", "home": "SOL", "station": True,
                                          "materials": "self", "stock": {"ftl_relay": 1}}])
    out = client.portal.call(eng.stock_pass)
    assert any("ftl_relay to keep in stock" in x for x in out)
    assert client.portal.call(eng.stock_pass) == []            # ordered: not again while the device list catches up
    hub = client.portal.call(eng.fleets)[0]
    devs[-1:] = [devs[-1], {"device_code": "R1", "device_type": "ftl_relay", "location": "SOL-3-L4", "tags": ["spare"]}]
    assert stock.shortfall(hub, devs) == {}                     # a spare in the hub's system counts
    # set in the loadout editor (a Stock column beside Want) …
    client.post("/fleets/hub/edit", data={"name": "Hub", "lines": "1", "autosave": "1", "type": ["mining_drone", "ftl_relay"],
                                          "qty": ["2", ""], "stock": ["", "3"]}, headers=HX)
    hub = client.portal.call(eng.fleets)[0]
    assert hub["wants"] == {"mining_drone": 2} and hub["stock"] == {"ftl_relay": 3}
    assert 'name="stock" value="3"' in client.get("/fleets/details?all=1", headers=H).text
    # … or in a template, which its fleets follow
    client.post("/loadouts/phases", data={"new_phase": "Depot"}, headers=HX)
    pid = next(p["id"] for p in client.portal.call(eng.loadout_cfg)["phases"] if p["name"] == "Depot")
    client.post("/loadouts/phases", data={f"want:{pid}:mining_drone": "1", f"stock:{pid}:system_ward": "2"}, headers=HX)
    client.post("/fleets/hub/edit", data={"name": "Hub", "template": pid}, headers=HX)
    hub = client.portal.call(eng.fleets)[0]
    assert hub["stock"] == {"system_ward": 2} and stock.shortfall(hub, devs) == {"system_ward": 2}


def test_build_order_spreads_out_over_systems():
    """Survey drones search the belt, a mining controller takes over the drones there and is launched, the vessel goes
    on to the nearest unvisited system with what it printed, and `repeat` loops; a system with no belt is skipped."""
    from rsweb import sim_order as so
    steps, err = so.parse("each system:\nsurvey\ncontroller: gather_evenly\nprint 1 mining_drone\nnext system\nrepeat")
    assert not err and [s_["kind"] for s_ in steps] == ["label", "survey", "controller", "print", "next", "repeat"]
    assert so.parse("controller: gather_resources")[1]                    # needs a configuration: refused
    assert not so.parse(so.EXPAND)[1]
    belt = "VIRTA-BELT-1"
    v = {"device_code": "V", "device_type": "heaven_vessel", "location": belt, "status": "idle"}
    devs = [v, {"device_code": "S1", "device_type": "survey_drone", "location": belt, "status": "idle"},
            {"device_code": "M1", "device_type": "mining_drone", "location": belt, "status": "idle"},
            {"device_code": "C1", "device_type": "ami_mining_controller", "location": belt, "status": "idle"}]
    a = {"order": steps, "stars": ["VIRTA"], "near": ["VIRTA", "VIRTB", "VIRTC"]}
    acts, _ = so.advance(a, devs, v, belt, {}, True, "t")
    assert acts == [{"kind": "search", "device": "S1"}]
    acts, _ = so.advance(a, devs, v, belt, {}, True, "t")
    assert [x["body"]["command"] for x in acts] == ["adopt", "set_directive", "launch"] and acts[0]["body"]["devices"] == ["M1"]
    acts, note = so.advance(a, devs, v, belt, {}, True, "t")
    assert acts == [{"kind": "print", "device_type": "mining_drone"}]
    devs.append({"device_code": "M2", "device_type": "mining_drone", "stowed_in_device_code": "V"})
    acts, _ = so.advance(a, devs, v, belt, {}, True, "t")
    assert acts[0]["kind"] == "travel" and acts[0]["to"] == "VIRTB"
    v2 = {**v, "location": "VIRTB"}                                      # arrived: no belt there, so on again
    a["stars"].append("VIRTB")
    acts, _ = so.advance(a, devs, v2, None, {}, True, "t")
    assert acts[0]["kind"] == "travel" and acts[0]["to"] == "VIRTC" and a["loops"] == 1


def test_simulations_listed_per_interface_and_approved(client):
    """Every known interface's scenarios (GET /devices/{code}/simulate) are listed on the Simulations page; approving
    one queues it at that interface, like a contract: its entry cost and a replicant go there. A null entry cost is free."""
    from rsweb import others as oth, sim
    db, eng = client.app.state.db, client.app.state.worker.automations
    snap = oth.normalize({})
    snap["stars"]["ALGOL"] = {"scanned_at": "2026-10-17T00:00:00+00:00", "devices": [], "drones": [],
                              "raw": [{"device_code": "IF2", "device_type": "replicant_interface", "location": "ALGOL-OBJ-2",
                                       "owner_name": "Bill"}]}
    client.portal.call(db.kv_set, oth.KV, snap)
    t = client.post("/simulations/scan", headers=HX).text
    assert "IF2 at ALGOL-OBJ-2 lists 2 scenario(s)" in t
    page = client.get("/simulations", headers=H).text
    assert "ALGOL-OBJ-2" in page and "Terraforming Training" in page and "free" in page and "Approve" in page
    t = client.post("/simulations/queue", data={"scenario": "terraforming_training_1", "interface": "IF2"}, headers=HX).text
    assert "a replicant goes (no entry cost) to ALGOL-OBJ-2" in t
    s = client.portal.call(eng.sim_state)
    assert s["interface"]["code"] == "IF2" and s["prep"]["location"] == "ALGOL-OBJ-2"
    assert sim.cost(sim.scenario(s, "terraforming_training_1")) == ("compute_core", 0)
    assert "approved" in client.get("/simulations", headers=H).text


def test_build_orders_mine_what_their_prints_need(client):
    """A mining drone costs carbon too: the built-in orders mine it, `mine:` takes weights, and saving an order whose
    print needs a resource no earlier step mines is refused. Built-ins stored before the fix are updated."""
    from rsweb import sim_order as so
    bps = {"mining_drone": {"resources": {"carbon": 25, "conductive": 50, "silicates": 25, "structural": 100}}}
    assert all(not so.check(so.parse(t)[0], bps) for t in (so.DEFAULT, so.EXPAND))
    assert so.parse("mine: structural 2, carbon")[0][0]["resources"] == ["structural", "structural", "carbon"]
    db, eng = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "blueprints", [{"device_type": "mining_drone", **bps["mining_drone"]}])
    t = client.post("/simulations/orders", data={"name": "x", "text": "mine: structural\nprint 1 mining_drone"}, headers=HX).text
    assert "Not saved" in t and "needs carbon, conductive, silicates" in t
    client.portal.call(db.kv_set, "sim", {"orders": {so.EXPAND_NAME: so.OLD_BUILTINS[0]}, "expand_seeded": True})
    assert client.portal.call(eng.sim_state)["orders"][so.EXPAND_NAME] == so.EXPAND


def test_terraforming_training_controls(client):
    """The terraforming training is played by hand: the Terraforming page shows a run's devices and its monitor's
    reports (?sim=), with habitable ranges and a switch for each kind of device; the engine leaves the run alone; a
    run's world never shows among the real ones."""
    import time
    from rsweb import sim
    db, eng = client.app.state.db, client.app.state.worker.automations
    devs = [{"device_code": f"M{i}", "device_type": "orbital_mirror", "location": "VIRTCEL-1", "status": "idle"} for i in range(3)]
    devs.append({"device_code": "RG1", "device_type": "atmospheric_regulator", "location": "VIRTCEL-1", "status": "idle"})
    client.portal.call(db.kv_set, "sim:world:7", {"t": time.time(), "rep": "R1", "devices": devs, "stars": [],
                                                  "inventory": [], "replicants": {}, "totals": {}})
    client.portal.call(db.insert_event, {"id": "tf0", "event": "terraform.status", "location": "VIRTCEL-1",   # an earlier run's
                                          "created_at": "2026-01-01T00:00:00+00:00",
                                          "payload": {"body": "VIRTCEL-1", "attributes": {"temperature": {"value": 999}}}})
    client.portal.call(db.kv_set, sim.KV, {"active": {"id": 7, "name": "Terraforming Training", "scenario": "terraforming_training_1",
                                                      "started_at": "2026-10-01T00:00:00+00:00"}})
    client.portal.call(db.insert_event, {"id": "tf1", "event": "terraform.status", "location": "VIRTCEL-1",
                                          "payload": {"body": "VIRTCEL-1", "attributes": {"temperature": {"value": 410, "delta": -1},
                                                                                         "oxygen": {"value": 12, "delta": 0}}}})
    t = client.get("/terraforming?sim=7", headers=H).text
    # judged for humans by default (GET /species environment, live 2026-10-17)
    assert "VIRTCEL-1" in t and "0/3 on" in t and "270–300 K" in t and "18–25 %" in t and "2 out of range: temperature, oxygen" in t
    assert 'name="codes" value="M0 M1 M2"' in t and "Not ready: temperature, oxygen" in t and "out of range for Humans" in t
    assert 'class="tf-switch"' in t and 'hx-trigger="every 15s' in t   # a console: switches, refreshes itself
    assert "999" not in t   # an earlier run's report: neither shown nor graphed
    assert t.index("<b>Control</b>") < t.index("0/3 on") and "fixes" not in t   # mirrors heat: no help at 410 K
    t = client.get("/terraforming?sim=7&species=branthar", headers=H).text   # another species, remembered
    assert "300–340 K" in t and "1 out of range: temperature" in t
    assert "300–340" in client.get("/terraforming?sim=7", headers=H).text
    client.get("/terraforming?sim=7&species=humans", headers=H)
    assert "VIRTCEL-1" not in client.get("/terraforming", headers=H).text
    # live 2026-10-17: the running monitor carries its report (terraform_status); numbers from the training run
    from rsweb import terraform as tf
    hum = tf.species_targets({"environment": {"temperature": [270, 300], "gravity": [0.7, 1.3]}})
    assert hum == {"temperature": (270, 300)} and tf.in_range("temperature", 305.45, hum) is False
    assert tf.range_eta("toxicity", 60.75, -0.7737) == {"enter": 53} and tf.range_eta("oxygen", 7.81, 0.5658) == {"enter": 4}
    assert tf.range_eta("temperature", 350.15, 0.03) == {"away": True} and tf.range_eta("hydrosphere", 10.15, 0.5) == {"ok": True}
    assert tf.range_eta("temperature", 349.0, 0.5) == {"leave": 3}
    assert tf.range_eta("toxicity", 0, -0.1, {"toxicity": (0, 5)}) == {"ok": True}   # can't go below 0
    devs.append({"device_code": "MON", "device_type": "terraform_monitor", "location": "VIRTOTHER-2", "status": "monitoring",
                 "terraform_status": {"body": "VIRTOTHER-2", "attributes": {"toxicity": {"value": 60.75, "delta": -0.7737}}}})
    client.portal.call(db.kv_set, "sim:world:7", {"t": time.time(), "rep": "R1", "devices": devs, "stars": [],
                                                  "inventory": [], "replicants": {}, "totals": {}})
    t = client.get("/terraforming?sim=7", headers=H).text
    assert "VIRTOTHER-2" in t and "in range in 73 ticks" in t   # humans: toxicity under 5
    assert "1 out of range: toxicity" in t and "2 out of range: temperature, oxygen" in t
    assert tf.out_of_range({"attributes": {"temperature": {"value": 300}, "toxicity": {"value": 5}}}) == []
    devs[3]["status"] = "regulating"   # live 2026-10-17: the regulator can be switched off again; how long it has run shows
    client.portal.call(db.insert_event, {"id": "rg1", "event": "terraform.device_activated", "device_code": "RG1", "created_at": "2026-10-17T22:30:13+00:00",
                                          "location": "VIRTCEL-1", "payload": {}})
    client.portal.call(db.kv_set, "sim:world:7", {"t": time.time(), "rep": "R1", "devices": devs, "stars": [],
                                                  "inventory": [], "replicants": {}, "totals": {}})
    assert "regulating since" in client.get("/terraforming?sim=7", headers=H).text
    # the console's live refresh and a switch: both answer with the world's partial
    w = client.get("/terraforming/world/VIRTCEL-1?sim=7", headers=HX).text
    assert w.lstrip().startswith('<div class="card tf-world"') and "regulating since" in w
    w = client.post("/terraforming/M0/power?sim=7", data={"on": "1", "body": "VIRTCEL-1"}, headers=HX).text
    assert "tf-world" in w and "activate M0" in w
    # what was just commanded shows until the game agrees (the switch doesn't flip back on a stale read)
    import time as _t
    from rsweb.web_terraform import PENDING_KV, _overlay, _remember
    client.portal.call(lambda: _remember(db, "M0", on=True))
    pend = client.portal.call(db.kv_get, PENDING_KV)
    assert pend["M0"]["on"] is True
    row = _overlay({"device_code": "M0", "device_type": "orbital_mirror", "status": "idle"}, pend)
    assert row["_on"] is True and row["_pending"]
    row = _overlay({"device_code": "M0", "device_type": "orbital_mirror", "status": "terraforming"}, pend)
    assert row["_on"] is True and not row.get("_pending")
    old = {"M0": {**pend["M0"], "at": _t.time() - 600}}
    assert _overlay({"device_code": "M0", "status": "idle"}, old)["_on"] is False   # stale command: the game wins
    row = _overlay({"device_code": "A1", "status": "terraforming", "settings": {"direction": "increase"}},
                   {"A1": {"set": {"direction": "decrease"}, "at": _t.time()}})
    assert row["_set"]["direction"] == "decrease" and row["_pending"] and row["_set_pending"]
    # settings are read from the device list's `settings` (live 2026-10-17), so the console shows what's set
    assert tf.settings_of({"settings": {"direction": "decrease", "strength": 0.5}}) == {"strength": 0.5, "direction": "decrease"}
    sp = tf.spark([1, 2, 3], (2, 4))
    assert sp["band"] and len(sp["pts"].split()) == 3
    assert not sim.plays({"code": "terraforming_training_1", "objective_type": "terraform_training"})
    assert sim.plays({"code": "mining_rush", "objective_type": "yield_quota"})
    s = client.portal.call(eng.sim_state)
    s["active"] = {"id": 7, "scenario": "terraforming_training_1", "replicant": "R1", "devices": [], "stars": [], "scans": {}}
    assert client.portal.call(eng.sim_run_pass, s) == []
    client.portal.call(eng.sim_purge, {"id": 7})   # the run ends: its world's reports go, whatever it recorded
    assert not client.portal.call(db.fetchall, "SELECT 1 FROM events WHERE location = 'VIRTCEL-1'")
    client.portal.call(db.kv_set, sim.KV, {})
    assert "410" not in client.get("/terraforming?sim=7", headers=H).text


def test_queueing_a_simulation_checks_its_offer_and_preps_at_once(client):
    """Queuing asks the interface right away whether it offers the scenario and at what cost, then runs the prep pass
    (the free training sends a replicant at once) instead of waiting for the next tick."""
    import time
    eng = client.app.state.worker.automations
    t = client.post("/simulations/queue", data={"scenario": "nope"}, headers=HX).text
    assert "Not queued" in t and "offers mining_rush, terraforming_training_1" in t
    assert not client.portal.call(eng.sim_state).get("prep")
    t = client.post("/simulations/queue", data={"scenario": "terraforming_training_1"}, headers=HX).text
    assert "no entry cost" in t
    for _ in range(50):
        prep = client.portal.call(eng.sim_state)["prep"]
        if prep.get("rep_sent"):
            break
        time.sleep(0.05)
    assert prep["cores_needed"] == 0 and prep["rep_sent"]
    assert "Entry cost: <b>free</b>" in client.get("/simulations", headers=H).text


def test_terraforming_candidates():
    """Map › Terraforming candidates: surveyed planets and moons, read on request, rated for a species and ranked by the
    estimated time to habitable; gravity out of range, gas giants and intelligent natives are dropped."""
    import json
    from rsweb import tfcandidates as tc
    hum = {"temperature": (270, 300), "pressure": (0.8, 1.2), "oxygen": (18, 25), "toxicity": (0, 5),
           "hydrosphere": (40, 70), "biosphere": (30, 70), "tectonic": (5, 30), "gravity": (0.7, 1.3)}
    earthish = {"surface_temp_k": 280, "atmo_pressure_atm": 1.0, "atmo_o2_pct": 20, "atmo_toxicity": 1, "hydrosphere_pct": 50,
                "biosphere_index": 40, "tectonic_index": 10, "surface_gravity": 1.0, "type": "rocky"}
    r = tc.rate_body(earthish, hum)
    assert r["ticks"] == 0 and not r["blocked"] and not r["dropped"]
    cold = {**earthish, "surface_temp_k": 250, "biosphere_index": None}   # 20 K short; no biosphere yet
    r = tc.rate_body(cold, hum, kit=1)
    assert r["attrs"]["temperature"]["ticks"] == 37 and r["attrs"]["temperature"]["devices"] == ["orbital_mirror", "thermal_lance"]
    assert r["attrs"]["biosphere"]["ticks"] == 100 and r["ticks"] == 100   # 250 K meets the bio seeder preconditions already
    assert tc.rate_body({**earthish, "atmo_toxicity": None, "atmosphere": False, "atmo_pressure_atm": None}, hum)["attrs"]["pressure"]["gap"] == 0.8
    assert tc.rate_body({**earthish, "biosphere_index": 90}, hum)["blocked"] == ["biosphere"]   # nothing lowers it
    assert "gravity" in tc.rate_body({**earthish, "surface_gravity": 0.11}, hum)["dropped"]
    assert tc.rate_body({**earthish, "type": "Gas Giant"}, hum)["dropped"]
    assert tc.bodies_to_read({"X": {"planets": [{"designation": "X-1", "moon_count": 2}, {"designation": "X-2", "moon_count": 1}]}},
                             {"X-1": "t", "X-2-1": "t"}, {}) == ["X-1", "X-1-1", "X-1-2", "X-2-1"]
    assert tc.bodies_to_read({"X": {"planets": [{"designation": "X-1", "moon_count": 0}]}}, {}, {"X": "t"}) == ["X-1"]


def test_terraforming_candidates_page(client):
    import json
    db = client.app.state.db
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("SOL", json.dumps({"planets": [{"designation": "SOL-3", "moon_count": 1}]}), "2026-10-20T00:00:00+00:00"))
    client.portal.call(db.kv_set, "surveyed", {"SOL-3": "2026-10-20T00:00:00+00:00"})
    t = client.get("/terraforming/candidates", headers=H).text
    assert "2 surveyed body/bodies not read yet" in t and "Terraforming candidates" in t
    out = client.post("/terraforming/candidates/read", headers=HX).text
    assert "Read 2 body/bodies" in out
    assert "Read 0" in client.post("/terraforming/candidates/read", headers=HX).text   # read within the week: left alone
    t = client.get("/terraforming/candidates?species=humans&kit=3", headers=H).text
    assert "SOL-3</a>" in t and "Every surveyed body has been read" in t and "270–300" in t
    assert "1 read but not surveyed yet" not in t
    t = client.get("/terraforming/candidates?max_ly=5", headers=H).text   # the mock's autofactory is in SOL
    assert "SOL-3</a>" in t and "0.0 ly" in t


def test_terraforming_groups_keep_a_fixed_order():
    """Monitor and regulator share one Control card, first; the other kinds follow a fixed order by what they primarily
    do (never moving while in use); what's out of range is only marked (fixes …)."""
    from rsweb import terraform as tf
    st = {"attributes": {"temperature": {"value": 410}, "oxygen": {"value": 12}, "toxicity": {"value": 2}}}
    assert tf.needs(st, {"temperature": (270, 300), "oxygen": (18, 25), "toxicity": (0, 5)}) == {"temperature": -1, "oxygen": 1}
    types = ["bio_seeder", "orbital_shade", "atmospheric_regulator", "gas_separator", "orbital_mirror", "cryo_disperser"]
    assert sorted(types, key=tf.priority) == [
        "atmospheric_regulator", "orbital_mirror", "orbital_shade", "cryo_disperser", "gas_separator", "bio_seeder"]


def test_device_list_filters_by_owning_replicant(client):
    """Fleet › Device list filters by the replicant owning a device (by name), e.g. to reassign the devices of one gone
    into a simulation with change_owner."""
    db = client.app.state.db
    client.portal.call(db.kv_set, "replicants", {"AAAA0001": {"replicant_code": "AAAA0001", "name": "bob-1"},
                                                 "BBBB0002": {"replicant_code": "BBBB0002", "name": "riker-2"}})
    client.portal.call(db.kv_set, "devices", [
        {"device_code": "D1", "device_type": "mining_drone", "location": "SOL-1", "replicant_code": "AAAA0001"},
        {"device_code": "D2", "device_type": "mining_drone", "location": "SOL-1", "replicant_code": "BBBB0002"}])
    page = client.get("/fleet", headers=H).text
    assert "any owner" in page and "riker-2 (BBBB0002)" in page
    t = client.get("/fleet?owner=BBBB0002", headers={**HX, "HX-Target": "fleet-table"}).text
    assert "D2" in t and "D1" not in t


def test_only_logistics_carriers_deliver_a_fleets_carrier_only_fetches_its_own():
    """Live 2026-10-18: Miner 0.5's cargo vessel was borrowed to carry compute cores for a simulation, and flew off with
    fleet members aboard while the fleet's move waited. Deliveries are the logistics fleets' work (or a spare carrier's)."""
    from rsweb import loadouts as lo
    D = lambda code, t, loc, **kw: {"device_code": code, "device_type": t, "location": loc, "status": "idle",  # noqa: E731
                                    "replicant_code": "R1", **kw}
    devices = [D("MF", "mobile_fleet", "FAL-1-L4", features=["surge", "attach"], attach_capacity=36, tags=["fleet:m1"]),
               D("CC", "compute_core", "FAL-1-L4", tags=["to:kel"])]
    stars = {s: {"position": {"x": i, "y": 0, "z": 0}, "entry_point": f"{s}-1-L4"} for i, s in enumerate(("FAL", "KEL", "LAR"))}
    fleets = [{"id": "m1", "name": "Miner 1", "home": "LAR", "station": True, "wants": {"mobile_fleet": 1}}]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert not any("CC" in x["devices"] for x in p["deliveries"])
    devices[0]["tags"] = []   # a spare carrier, in no fleet, still delivers
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert [x["carrier"] for x in p["deliveries"] if "CC" in x["devices"]] == ["MF"]


def test_lockers_deliver_and_show_on_the_system_page(client):
    db = client.app.state.db
    page = client.get("/lockers", headers=H).text
    assert "0 of 50 compute core there" in page and "Deliver" in page
    devs = client.portal.call(db.kv_get, "devices") or []
    devs[0] = {**devs[0], "device_type": "compute_core", "tags": ["spare"]}   # a device the mock game knows, as a spare core
    client.portal.call(db.kv_set, "devices", devs)
    r = client.post("/lockers/deliver", data={"location": "TWAFFY-OORT"}, headers=HX).text
    assert "compute core: 1 spare(s) sent" in r, r
    assert "TWAFFY-OORT" in client.get("/systems/TWAFFY", headers=H).text
    # all 50 on site: the row offers Retrieve, which scans from the replicant there and retrieves (2026-10-22: the row
    # kept offering Deliver, which never retrieves)
    world = client.app.state.api.http._transport.app.state.world
    cores = [{"device_code": f"CC{i:06d}", "device_type": "compute_core", "location": "TWAFFY-OORT", "status": "idle",
              "tags": []} for i in range(50)]
    client.portal.call(db.kv_set, "devices", devs + cores)
    r = client.post("/lockers/retrieve_at", data={"location": "TWAFFY-OORT"}, headers=HX).text
    assert "No replicant of yours is at TWAFFY-OORT" in r
    client.portal.call(db.kv_set, "devices", devs + cores + [
        {"device_code": "CV000001", "device_type": "cargo_vessel", "location": "TWAFFY-OORT", "status": "idle",
         "hosting_replicant": {"name": "Percival", "replicant_code": "8EC42FBA"}}])
    page = client.get("/lockers", headers=H).text
    assert "50 of 50 compute core there" in page and "/lockers/retrieve_at" in page
    world.foreign_devices = [{"device_code": "70C835AC", "device_type": "equipment_locker", "location": "TWAFFY-OORT"}]
    try:
        r = client.post("/lockers/retrieve_at", data={"location": "TWAFFY-OORT"}, headers=HX).text
    finally:
        world.foreign_devices = []
    assert "retrieved" in r and "HV2E8F3A" in r, r


def test_device_scans_keep_lockers(client):
    """Arrival / traffic device scans record equipment lockers (2026-10-22): the Lockers and System pages list them
    with their code, and the row's Retrieve uses it without another scan."""
    from rsweb import others as oth
    db = client.app.state.db
    s = oth.normalize({})
    scanned = [{"device_code": "70C835AC", "device_type": "equipment_locker", "location": "TWAFFY-OORT",
                "owner_name": "Bill", "owner_replicant_code": "A8F48B26"},
               {"device_code": "EL000002", "device_type": "equipment_locker", "location": "NOVA-OORT", "owner_name": "Bill"}]
    oth.record_scan(s, "TWAFFY", scanned, set())
    oth.prune(s)
    assert s["lockers"]["TWAFFY-OORT"]["code"] == "70C835AC" and "NOVA-OORT" in s["lockers"]
    assert any(d["type"] == "equipment_locker" for d in s["stars"]["TWAFFY"]["devices"])
    client.portal.call(db.kv_set, oth.KV, s)
    page = client.get("/lockers", headers=H).text
    assert "70C835AC" in page and "NOVA-OORT" in page and "EL000002" in page
    assert "NOVA-OORT" in client.get("/systems/NOVA", headers=H).text
    r = client.post("/lockers/retrieve_at", data={"location": "TWAFFY-OORT"}, headers=HX).text
    assert "retrieved" in r and "HV2E8F3A" in r, r


def test_a_drone_aboard_a_carrier_at_the_destination_has_arrived(client):
    db, e = client.app.state.db, client.app.state.worker.automations
    devs = client.portal.call(db.kv_get, "devices") or []
    devs += [{"device_code": "HV000009", "device_type": "heaven_vessel", "location": "SOL-4-L4", "status": "idle"},
             {"device_code": "SD000009", "device_type": "survey_drone", "location": None, "status": "stowed",
              "stowed_in_device_code": "HV000009"}]
    client.portal.call(db.kv_set, "devices", devs)
    st = {"wait": ["travel.arrived"], "match": {"destination": "SOL-4-L4"}, "wait_device": "SD000009"}
    assert client.portal.call(e._arrived_now, st, {})


def test_dashboard_story_card_shows_the_game_overview(client):
    client.portal.call(client.app.state.worker.sync_overview)
    page = client.get("/partials/story", headers=H).text
    assert "Alteration Protocol" in page and "The Shattering of Ethirel" in page and "/systems/ETHIREL" in page
    assert "Terraforming the Colony Worlds" in page and "Riker" in page
    assert 'id="story"' in client.get("/", headers=H).text
    # the live spec's fields (2026-10-20): visible_until, reward, locations (names or {name, system}), the Ark's schedule
    assert "until 2026-10-31" in page and "Reward: Blueprint for a mining_rig." in page and "stop 12/12" in page
    assert "Season likely ends <b>2026-10-31</b>" in page
    from rsweb import season
    assert season.likely_end({"events": [{"visible_until": "2026-10-31"}], "storylines": [{"visible_until": "2026-10-20"}]})[0] == "2026-10-31"
    assert season.likely_end({"season": {"ends_at": "2026-11-30T00:00:00Z"}})[0] == "2026-11-30" and season.likely_end({}) is None
    assert '/systems/TRYNLLYR">TRYNLLYR</a>' in page and '/systems/MIRFAKA">Deep Space Tracking Array (MIRFAKA)</a>' in page


def test_bobnet_feed_shows_where_the_poster_is(client):
    w = client.app.state.worker
    client.portal.call(w.handle_event, _ev(9901, "bobnet.new", replicant_name="Dash", replicant_code="D1",
                                           current_star="ROSALIATE", channel="#general", message="Shop open"))
    page = client.get("/messages", headers=H).text
    assert '@ <a href="/systems/ROSALIATE">ROSALIATE</a></span>: Shop open' in page


def test_bobnet_feed_pages_back(client, monkeypatch):
    from rsweb import web
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


def test_relay_rollout_queue_goes_outward_and_chains():
    """Owner 2026-10-19: deliver relays to all systems, nearest the designated systems first."""
    from rsweb import relayroll as rr
    pos = {s: {"x": x, "y": 0, "z": 0} for s, x in
           {"S": 0, "A": 5, "B": 10, "C": 3, "FAR": 40, "D": -6, "OUT": 80}.items()}
    q = rr.queue(["S"], pos, set(), set(), set(), 50, True)
    assert [x["star"] for x in q] == ["S", "C", "A", "D", "B"]   # FAR: no relay within 7.5 ly of it; OUT: past 50 ly
    assert q[1] == {"star": "C", "ly": 3.0, "seed": "S", "kind": "relay"}
    assert [x["star"] for x in rr.queue(["S"], pos, {"S"}, {"C"}, {"D"}, 50, False)] == ["A", "B", "FAR"]
    assert [x["star"] for x in rr.queue(["S"], pos, set(), set(), set(), 50, True, limit=2)] == ["S", "C"]
    assert rr.queue(["NOWHERE"], pos, set(), set(), set(), 50, True) == []


def test_relay_rollout_long_range_stations(client):
    """Owner 2026-10-29: a 10 ly deep space relay station bridges gaps a relay can't (either end in reach links)."""
    from rsweb import relayroll as rr
    pos = {s: {"x": x, "y": 0, "z": 0} for s, x in
           {"S": 0, "A": 5, "B": 10, "W": 19, "V": 25, "X": 34, "FAR": 60}.items()}
    assert [x["star"] for x in rr.queue(["S"], pos, {"S"}, set(), set(), 50, True)] == ["A", "B"]
    q = rr.queue(["S"], pos, {"S"}, set(), set(), 50, True, long_hop=10.0)
    # relays first; W (9 ly from B) only by a station; V is a relay hop from W, X 9 ly from V a station again
    assert [(x["star"], x["kind"]) for x in q] == [("A", "relay"), ("B", "relay"), ("W", "station"), ("V", "relay"),
                                                   ("X", "station")]
    # a station already on order in B reaches W: a relay there links to it
    q = rr.queue(["S"], pos, {"S", "A"}, {"B"}, set(), 50, True, ranges={"B": 10.0}, long_hop=10.0, limit=1)
    assert q == [{"star": "W", "ly": 19.0, "seed": "S", "kind": "relay"}]
    r = client.post("/map/relays/rollout", headers=HX, data={"action": "save", "seeds": "S", "radius": "50",
                                                            "chained": "1", "long": "1"})
    assert r.status_code == 200 and rr.state(client.portal.call(client.app.state.db.kv_get, rr.KV, {}))["long"]
    page = client.get("/map/relays", headers=HX).text
    assert 'name="long" value="1" checked' in page and "deep space relay station" in page


def test_relay_rollout_radius_per_seed(client):
    """Owner 2026-10-24: SOLARES needs a much smaller rollout radius than FALQUORYX; unset seeds use the default."""
    from rsweb import relayroll as rr
    pos = {s: {"x": x, "y": 0, "z": 0} for s, x in
           {"SOL": 0, "N1": 3, "N2": 12, "FAL": 100, "F1": 104, "F2": 140}.items()}
    q = rr.queue(["SOL", "FAL"], pos, {"SOL", "FAL"}, set(), set(), 50, False, radii={"SOL": 5})
    assert [x["star"] for x in q] == ["N1", "F1", "F2"]   # N2 is past SOL's 5 ly, F2 within FAL's default 50
    assert q[-1]["seed"] == "FAL"
    r = client.post("/map/relays/rollout", headers=HX, data={"action": "save", "seeds": "sol fal f1:20", "radius": "50",
                                                            "r_SOL": "5", "r_FAL": "50", "chained": "1"})
    assert r.status_code == 200
    roll = rr.state(client.portal.call(client.app.state.db.kv_get, rr.KV, {}))
    assert roll["seeds"] == ["SOL", "FAL", "F1"] and roll["radii"] == {"SOL": 5.0, "F1": 20.0}
    page = client.get("/map/relays", headers=HX).text
    assert 'name="r_SOL"' in page and 'name="r_FAL"' in page


def test_relay_rollout_pass_keeps_orders_open_and_skips_failures(client, monkeypatch):
    from rsweb import relayroll as rr
    monkeypatch.setattr(rr, "capacity", lambda *a: 2)   # two free hold slots on logistics vessels with a replicant
    from rsweb import ward_orders as wo
    db = client.app.state.db
    eng = client.app.state.worker.automations
    stars = [{"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in {"S": 0, "A": 4, "B": 8, "C": 10}.items()]
    client.portal.call(db.kv_set, "stars", {"stars": stars})
    devs = [{"device_code": "R1", "device_type": "ftl_relay", "location": "S-1-L4", "status": "relaying"}]

    async def devices():
        return devs

    async def order(kind, star, user=""):
        if star == "B":
            return "no spare FTL relay and no autofactory within 100 ly of B"
        orders = await db.kv_get(wo.kv_key("relay"), {}) or {}
        orders[star] = {"at": "2026-10-19T00:00:00+00:00", "how": "print"}
        await db.kv_set(wo.kv_key("relay"), orders)
        return f"printing a relay for {star}"
    eng.devices, eng.order_outpost = devices, order
    r = client.post("/map/relays/rollout", headers=HX, data={"action": "start", "seeds": "s", "radius": "50",
                                                              "chained": "1"})
    assert r.status_code == 200
    st = rr.state(client.portal.call(db.kv_get, rr.KV, {}))
    assert st["on"] and st["seeds"] == ["S"] and sorted(st["ordered"]) == ["A", "C"] and "B" in st["skipped"]
    # A's relay is up, C's order was dropped: both make room, and nothing is left, so the rollout ends
    devs.append({"device_code": "R2", "device_type": "ftl_relay", "location": "A-1-L4", "status": "relaying"})
    client.portal.call(db.kv_set, wo.kv_key("relay"), {})
    client.portal.call(eng.relay_rollout_pass)
    st = rr.state(client.portal.call(db.kv_get, rr.KV, {}))
    assert st["done"] == 1 and sorted(st["skipped"]) == ["B", "C"] and not st["ordered"] and not st["on"]
    page = client.get("/map/relays", headers=H).text
    assert "Relay rollout" in page and "Retry skipped (2)" in page


def test_relay_rollout_pass_orders_station_pairs(client, monkeypatch):
    """Owner 2026-10-29: with long-range stations on, a gap no relay crosses gets a station."""
    from rsweb import relayroll as rr
    monkeypatch.setattr(rr, "capacity", lambda *a: 3)
    from rsweb import ward_orders as wo
    db = client.app.state.db
    eng = client.app.state.worker.automations
    stars = [{"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in {"S": 0, "W": 9}.items()]
    client.portal.call(db.kv_set, "stars", {"stars": stars})
    devs = [{"device_code": "R1", "device_type": "ftl_relay", "location": "S-1-L4", "status": "relaying"}]
    calls = []

    async def devices():
        return devs

    async def order(kind, star, user=""):
        calls.append((kind, star))
        orders = await db.kv_get(wo.kv_key(kind), {}) or {}
        orders[star] = {"at": "2026-10-29T00:00:00+00:00", "how": "print"}
        await db.kv_set(wo.kv_key(kind), orders)
        return f"printing a {kind} for {star}"
    eng.devices, eng.order_outpost = devices, order
    client.portal.call(db.kv_set, rr.KV, {"on": True, "seeds": ["S"], "chained": True})
    client.portal.call(eng.relay_rollout_pass)
    assert calls == [] and not rr.state(client.portal.call(db.kv_get, rr.KV, {}))["on"]   # no relay reaches W
    client.portal.call(db.kv_set, rr.KV, {"on": True, "seeds": ["S"], "chained": True, "long": True})
    client.portal.call(eng.relay_rollout_pass)
    assert calls == [("station", "W")]
    st = rr.state(client.portal.call(db.kv_get, rr.KV, {}))
    assert sorted(st["ordered"]) == ["W"] and st["on"]


def test_relays_route_orders_stations(client, monkeypatch):
    eng = client.app.state.worker.automations
    calls = []

    async def order(kind, star, user=""):
        calls.append((kind, star))
        return "ok"
    monkeypatch.setattr(eng, "order_outpost", order)
    r = client.post("/map/relays/route", headers=HX, data={"stars": "A,G,B", "long": "A,B"})
    assert r.status_code == 200 and calls == [("station", "A"), ("relay", "G"), ("station", "B")]


def test_relay_rollout_pause_cancels_relay_prints_and_prints_no_more(client, monkeypatch):
    """Owner 2026-10-25: pausing the relay rollout cancels every relay print not started yet and keeps new ones (the
    1.241.1 lost-print re-source included) from being created until it starts again."""
    from rsweb import relayroll as rr
    from rsweb import ward_orders as wo
    db = client.app.state.db
    eng = client.app.state.worker.automations
    fab = {"device_code": "F1", "device_type": "autofactory", "location": "S-BELT-1", "status": "printing (ftl_relay)",
           "available_commands": ["enqueue_print", "dequeue_print"], "queue_size": 10,
           "print_queue": [{"device_type": "ftl_relay", "tags": ["to:A"]}, {"device_type": "mining_drone", "tags": []},
                           {"device_type": "ftl_relay", "tags": ["to:B"]}, {"device_type": "ftl_relay", "tags": ["stock"]}]}
    devs = [fab]

    async def devices():
        return devs
    eng.devices = devices
    client.portal.call(db.kv_set, wo.kv_key("relay"), {
        "A": {"at": "2026-10-25T00:00:00+00:00", "how": "print", "factory": "F1"},
        "B": {"at": "2026-10-25T00:00:00+00:00", "how": "print", "factory": "F1"},
        "C": {"at": "2026-10-25T00:00:00+00:00", "how": "print"},                  # lost, waiting for a slot
        "D": {"at": "2026-10-25T00:00:00+00:00", "how": "send", "device": "R9"}})
    job = "wards-print-E"
    client.portal.call(eng.save_jobs, [{"id": job, "rule": "wards", "title": "print a FTL relay on F1 for E", "device": "F1",
                                        "steps": [], "idx": 0, "status": "running", "meta": {"outpost": "E"}}])
    client.portal.call(db.kv_set, rr.KV, {"on": True, "seeds": ["S"], "ordered": {s: "x" for s in "ABCD"}})
    r = client.post("/map/relays/rollout", headers=HX, data={"action": "stop", "seeds": "S", "radius": "50", "chained": "1"})
    assert r.status_code == 200
    st = rr.state(client.portal.call(db.kv_get, rr.KV, {}))
    assert st["paused"] and not st["on"] and list(st["ordered"]) == ["D"] and not st["skipped"]
    assert list(client.portal.call(db.kv_get, wo.kv_key("relay"), {})) == ["D"]
    jobs = client.portal.call(eng.jobs)
    assert next(j for j in jobs if j["id"] == job)["status"] == "cancelled"
    drop = next(j for j in jobs if j["title"].startswith("F1: drop 2 queued"))
    assert [s["body"]["index"] for s in drop["steps"]] == [2, 0]   # highest first, so the indices hold
    # paused: an order (by hand, or a lost print sourced again) finds no spare and prints nothing
    how, msg = client.portal.call(eng._outpost_source, "relay", "E", devs, set(), {})
    assert how is None and "relay prints are paused" in msg
    page = client.get("/map/relays", headers=H).text
    assert "relay prints paused" in page and ">Resume<" in page
    client.post("/map/relays/rollout", headers=HX, data={"action": "start", "seeds": "S", "radius": "50", "chained": "1"})
    assert not rr.state(client.portal.call(db.kv_get, rr.KV, {}))["paused"]


def test_relocation_waits_for_command_reach_at_the_new_home(client):
    """Live 2026-10-19: Miner_4 relocated to OXETH (no relay, no replicant aboard) and everything aboard went
    out_of_range before it could be unloaded."""
    eng = client.app.state.worker.automations
    ves = {"device_code": "V", "device_type": "cargo_vessel", "location": "AAA-1", "status": "idle", "tags": ["fleet:m"],
           "features": ["surge", "cruise"], "stow_capacity": 2}
    d1 = {"device_code": "D1", "device_type": "survey_drone", "location": "AAA-1", "status": "idle", "tags": ["fleet:m"],
          "features": ["cruise", "stow"]}
    f = {"id": "m", "name": "M", "role": "explore", "home": "BBB", "station": True, "wants": {}}
    reach = {"AAA"}

    async def commandable(devices, stars=None, without=None):
        return reach
    eng.commandable = commandable
    how = client.portal.call(eng.start_relocation, f, "AAA", "BBB", [ves, d1])
    assert "outside an active relay network" in how and f["home"] == "AAA" and f["pending_home"] == "BBB" and "mission" not in f
    # a mission already under way stalls before the travel phase
    m = {"status": "running", "targets": ["BBB"], "idx": 0, "relocate": True, "log": []}
    steps, problems = client.portal.call(eng.fleet_phase_steps, f, m, "travel", [ves, d1])
    assert steps == [] and m.get("stall") and "BBB" in problems[0]
    reach.add("BBB")   # a relay of an active network there now: it goes
    assert "relocation mission" in client.portal.call(eng.start_relocation, f, "AAA", "BBB", [ves, d1])


def test_loadout_carrier_room_counts_what_the_game_lists_aboard():
    """Live 2026-10-19: 89D9AB0D (4 attach points) had two haulers attached; the plan gave it 3 more."""
    from rsweb import loadouts as lo
    c = {"device_code": "C", "device_type": "surge_platform", "attach_capacity": 4, "attach_used": 4,
         "attached_devices": [{"device_code": x} for x in ("H1", "H2", "A", "B")]}
    assert lo._free(c, {}, {}) == 0
    assert lo._free(c, {}, {}, ["A", "B", "X"]) == 2          # the seats A and B already hold: X doesn't fit
    c2 = {"device_code": "V", "device_type": "cargo_vessel", "stow_capacity": 10, "attach_capacity": 2,
          "attached_devices": [{"device_code": "H1"}], "stowed_devices": [{"device_code": "S1"}]}
    assert lo._free(c2, {}, {}) == 9                          # a hold: only what's stowed takes its seats


def test_salvage_counts_as_richness_for_a_next_home():
    """Owner 2026-10-19: salvage is a possible target for mining operations."""
    from rsweb import prospects as pr
    bare = pr.score("S", {"planets": []}, {}, [], 2.0, True)
    salvage = pr.score("S", {"planets": []}, {"salvageable": 5000}, [], 2.0, True)
    assert salvage["parts"]["richness"] == 20 and salvage["score"] > bare["score"] + 15
    assert any("salvage" in r for r in salvage["reasons"])


def test_relay_rollout_capacity_is_free_hold_on_logistics_vessels_with_a_replicant():
    """Owner 2026-10-20: no hard cap; what each carrier takes stays within its free stowage."""
    from rsweb import relayroll as rr
    from rsweb.loadouts import _free
    devs = [{"device_code": "HV", "device_type": "heaven_vessel", "tags": ["fleet:logistics"], "features": ["surge"],
             "stow_capacity": 10, "hosting_replicant": {"replicant_code": "R1"},
             "stowed_devices": [{"device_code": "M"}, {"device_code": "X"}]},
            {"device_code": "CV", "device_type": "cargo_vessel", "tags": ["fleet:logistics"], "features": ["surge"],
             "stow_capacity": 50},                                                   # no replicant aboard: not an escort
            {"device_code": "MV", "device_type": "heaven_vessel", "tags": ["fleet:miner-1"], "features": ["surge"],
             "stow_capacity": 10, "hosting_replicant": {"replicant_code": "R2"}}]    # not logistics
    assert rr.capacity(devs, {"fleet:logistics"}, lambda d: _free(d, {}, {})) == 8


def test_a_relay_run_drops_relays_system_by_system():
    from rsweb import loadouts as lo
    by = {c: {"device_code": c, "device_type": "ftl_relay", "location": None, "stowed_in_device_code": "HV",
              "tags": [f"to:{t.lower()}"]} for c, t in (("R1", "AAA"), ("R2", "BBB"), ("R3", "BBB"))}
    by["HV"] = {"device_code": "HV", "device_type": "heaven_vessel", "location": "HUB-1"}
    dl = {"carrier": "HV", "carrier_loc": "HUB-1", "from": "HUB", "to": "AAA", "devices": ["R1", "R2", "R3"],
          "mode": "stow", "dest_of": {"R2": "BBB", "R3": "BBB"}, "stops": ["BBB"], "stay": True}
    descs = [s["desc"] for s in lo.delivery_steps(dl, by, {}, False)]
    moves = [d for d in descs if d.startswith("HV → ")]
    assert moves == ["HV → AAA (deliver 1 to AAA)", "HV → BBB (deliver 2 to BBB)"]
    assert descs.index("deploy R1 in AAA") < descs.index("HV → BBB (deliver 2 to BBB)") < descs.index("deploy R3 in BBB")


def test_loadout_plan_fills_a_carrier_hold_with_relays_for_several_systems():
    """Owner 2026-10-20: a carrier takes as many relays as its free stowage holds, nearest drop first."""
    from rsweb import loadouts as lo
    def dev(code, t, loc, **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": "idle", "operational_capacity": 100.0,
                "features": kw.pop("features", ["cruise", "stow"]), "available_commands": ["travel", "stow", "deploy"], **kw}
    devices = [dev(f"R{t}", "ftl_relay", "HUB-1", tags=[f"to:{t.lower()}"]) for t in ("AAA", "BBB", "CCC")]
    devices.append(dev("HV", "heaven_vessel", "HUB-1", features=["surge", "cruise"], stow_capacity=3,
                       hosting_replicant={"replicant_code": "R1"}, tags=["fleet:logistics"]))
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in
             (("HUB", 0), ("AAA", 5), ("BBB", 9), ("CCC", 2))}
    cfg = {"phases": [], "systems": {}, "fleets": [{"id": "logistics", "name": "Logistics", "role": "logistics",
                                                    "home": "HUB", "wants": {}}]}
    p = lo.plan(cfg, devices, [], {}, stars, {"HV": "R1"}, set(), [], {}, commandable={"HUB"})
    dl = [d for d in p["deliveries"] if d["carrier"] == "HV"]
    assert len(dl) == 1 and sorted(dl[0]["devices"]) == ["RAAA", "RBBB", "RCCC"]   # 2 slots left after the first: all go
    assert dl[0]["to"] == "AAA" and dl[0]["stops"] == ["CCC", "BBB"]               # then nearest first: CCC 3 ly, BBB 4 ly
    devices[-1]["stow_capacity"] = 2   # room for two: the third waits for the next run
    p = lo.plan(cfg, devices, [], {}, stars, {"HV": "R1"}, set(), [], {}, commandable={"HUB"})
    assert sum(len(d["devices"]) for d in p["deliveries"] if d["carrier"] == "HV") == 2
    devices[-1].update(stow_capacity=3, replicant_code="R1")
    for d in devices[:3]:
        d["replicant_code"] = "FAB"   # printed by another replicant's factory (live 2026-10-21): handed over as they board
    p = lo.plan(cfg, devices, [], {}, stars, {"HV": "R1"}, set(), [], {}, commandable={"HUB"})
    assert sorted(x for d in p["deliveries"] if d["carrier"] == "HV" for x in d["devices"]) == ["RAAA", "RBBB", "RCCC"]


def test_logistics_replicant_vessels_carry_inside_the_network():
    """Owner 2026-10-20: every vessel in a logistics fleet is there for automation (the old 'use replicant vessels'
    setting is gone); another fleet's replicant vessel isn't borrowed."""
    from rsweb import loadouts as lo
    def dev(c, t, loc, **kw):
        return {"device_code": c, "device_type": t, "location": loc, "status": "idle", "operational_capacity": 100.0,
                "features": kw.pop("features", ["cruise", "stow"]), "available_commands": ["travel", "stow", "deploy"], **kw}
    f = {"id": "m", "name": "M", "role": "mining", "home": "BBB", "station": True, "wants": {"mining_drone": 2}}
    drones = [dev(c, "mining_drone", "AAA-BELT-1", tags=["fleet:m"]) for c in ("D1", "D2")]
    hv = dev("HV", "heaven_vessel", "AAA-1", features=["surge", "cruise"], stow_capacity=10,
             hosting_replicant={"replicant_code": "R1"}, tags=["fleet:logistics"])
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in (("AAA", 0), ("BBB", 5))}
    cfg = {"phases": [], "systems": {}, "fleets": [f, {"id": "logistics", "name": "L", "role": "logistics", "home": "AAA",
                                                        "wants": {}},
                                                   {"id": "s", "name": "S", "role": "explore", "home": "AAA", "wants": {}}]}
    p = lo.plan(cfg, drones + [hv], [], {}, stars, {"HV": "R1"}, set(), [], {}, commandable={"AAA", "BBB"})
    assert [(d["carrier"], d["devices"]) for d in p["deliveries"]] == [("HV", ["D1", "D2"])]
    other = {**hv, "tags": ["fleet:s"]}
    p = lo.plan(cfg, drones + [other], [], {}, stars, {"HV": "R1"}, set(), [], {}, commandable={"AAA", "BBB"})
    assert not p["deliveries"]


def test_an_approved_trade_keeps_logistics_replicant_vessels_out_of_loadouts(client):
    """Owner 2026-10-20: a shop trade must complete before it expires; device runs mustn't tie up its only escorts."""
    from rsweb import logistics as lg
    eng, db = client.app.state.worker.automations, client.app.state.db
    seen = {}

    def fake_plan(*a, **kw):
        seen["busy"] = set(a[6])
        raise RuntimeError("stop")
    import rsweb.loadouts as lo
    real = lo.plan
    lo.plan = fake_plan
    try:
        devices = client.portal.call(db.kv_get, "devices")
        hv = next(d for d in devices if d["device_type"] == "heaven_vessel")
        hv["tags"] = ["fleet:logistics"]
        # a second one, far away: a fleet carrier towing a replicant's matrix container stays free for relay runs
        reps = client.portal.call(db.kv_get, "replicants", {}) or {}
        reps["R9"] = {"hosted_device_code": "MC9", "location": "ALPHA-1"}
        client.portal.call(db.kv_set, "replicants", reps)
        devices += [{"device_code": "MF9", "device_type": "mobile_fleet", "location": "ALPHA-1", "status": "idle",
                     "features": ["surge", "cruise", "attach"], "tags": ["fleet:logistics"]},
                    {"device_code": "MC9", "device_type": "matrix_container", "location": "ALPHA-1", "status": "idle",
                     "features": ["cruise", "cradle"], "attached_to_device_code": "MF9"}]
        client.portal.call(db.kv_set, "devices", devices)
        client.portal.call(eng.save_fleets, [{"id": "logistics", "name": "Logistics", "role": "logistics", "home": "SOL",
                                              "wants": {}}])
        for state, held in (({"requests": []}, False),
                            ({"requests": [{"kind": "trade", "id": "trade:x", "to": hv["location"], "skipped": False}]}, True)):
            client.portal.call(db.kv_set, lg.KV_STATE, state)
            try:
                client.portal.call(eng.loadout_plan)
            except RuntimeError:
                pass
            assert (hv["device_code"] in seen["busy"]) is held and "MF9" not in seen["busy"]
    finally:
        lo.plan = real


def test_a_reserved_stock_relay_loses_its_stock_tag(client):
    from rsweb import loadouts as lo
    db, eng = client.app.state.db, client.app.state.worker.automations
    sent = []

    async def fake_send(method, path, body, label):
        sent.append((path, body))
        return True, {}, None
    eng.send = fake_send
    client.portal.call(db.kv_set, "devices", [
        {"device_code": "R1", "device_type": "ftl_relay", "location": "SOL-3", "tags": ["fleet:hub", "stock", "to:ALPHA"]},
        {"device_code": "R2", "device_type": "ftl_relay", "location": "SOL-3", "tags": ["spare", "stock"]},
        {"device_code": "R3", "device_type": "ftl_relay", "location": "SOL-3", "tags": ["spare", "stock", "gather", "to:alpha"]}])
    client.portal.call(eng.stock_pass)
    assert sent == [("/devices/R1", {"configuration": {"remove_tags": ["stock"]}})]   # R2 is still a spare in stock, R3 on its way to a hub's
    st = lo.self_move_steps("R2", "ALPHA", {}, {"device_code": "R2", "tags": ["spare", "stock"], "location": "SOL-3"})
    assert st[0]["body"]["configuration"] == {"add_tags": [lo.to_tag("ALPHA")], "remove_tags": ["spare", "stock"]}


def test_blueprints_show_how_a_device_travels_and_what_it_carries(client):
    from rsweb import fleets as fl
    assert fl.carry_info({"device_type": "mining_drone", "features": ["cruise", "mine", "stow"]})["ways"] == ["stowed", "attached"]
    assert fl.carry_info({"device_type": "transport_hauler", "features": ["cruise", "transport"]})["ways"] == ["attached"]
    assert fl.carry_info({"device_type": "cargo_freighter", "features": ["surge", "cruise", "transport"],
                          "cargo_capacity": 500})["ways"] == ["flies itself"]
    assert fl.carry_info({"device_type": "autofactory", "features": ["print", "modular"]})["ways"] == ["compacted, then attached"]
    cv = fl.carry_info({"device_type": "cargo_vessel", "features": ["surge", "cruise", "attach"], "stow_capacity": 50,
                        "attach_capacity": 3, "cargo_capacity": 200})
    assert cv == {"ways": ["flies itself", "attached"], "hold": 50, "attach": 3, "cargo": 200}
    page = client.get("/blueprints", headers=H).text
    assert "travels: flies itself or attached · hold 50 devices · attaches 3 devices · cargo 200 materials" in page


def test_a_stowable_device_can_be_attached_to_a_carrier_here(client):
    """Live 2026-10-20: a mobile fleet attached an FTL relay; the relay's own commands don't list attach."""
    world = client.app.state.api.http._transport.app.state.world
    sp = next(d for d in world.devices if d["device_type"] == "surge_plate")
    sp["available_commands"] = sp["available_commands"] + ["attach", "detach"]
    rl = next(d for d in world.devices if d["device_type"] == "ftl_relay")
    rl["location"] = sp["location"]
    client.portal.call(client.app.state.worker.sync_devices)
    assert '<option value="attach"' in client.get(f"/devices/{rl['device_code']}", headers=H).text
    form = client.get(f"/devices/{rl['device_code']}/command-form?command=attach", headers=HX).text
    assert "attach me to" in form and f'value="{sp["device_code"]}"' in form
    sent = client.get(f"/devices/{sp['device_code']}/command-form?command=attach", headers=HX).text
    assert f'value="{rl["device_code"]}"' in sent


def test_map_hazard_haze(client):
    """The galaxy map hazes systems an incoming object still threatens (the object's system and its target's), and
    drops the haze once the object is closed."""
    db = client.app.state.db
    client.portal.call(db.insert_event, {"id": "hz1", "event": "system.impact_warning", "location": "SOL-OBJ-1",
                                          "created_at": "2026-10-20T10:00:00+00:00",
                                          "payload": {"destination": "SOL-3", "impact_eta": "2099-10-20T11:00:00Z",
                                                      "device_codes": ["D1"]}})
    hz = client.get("/api/map.json?part=overlay", headers=H).json()["hazards"]
    assert list(hz) == ["SOL"] and hz["SOL"]["objects"] == ["SOL-OBJ-1"] and hz["SOL"]["hours_left"] > 0
    client.portal.call(db.insert_event, {"id": "hz2", "event": "system.object_impacted", "location": "SOL-OBJ-1",
                                          "payload": {"impact_target": "SOL-3"}})
    assert client.get("/api/map.json?part=overlay", headers=H).json()["hazards"] == {}


def test_map_hazardous_tag(client):
    """A system whose scan tags it hazardous (system_tags) is hazed on the galaxy map too."""
    import json
    from rsweb.web import hazard_tags
    db = client.app.state.db
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("SOL", json.dumps({"system_tags": ["binary_system", "hazardous"]}), "2026-10-21T00:00:00+00:00"))
    sol = next(s for s in client.get("/api/map.json?part=core", headers=H).json()["stars"] if s["designation"] == "SOL")
    assert sol["hazardous"] == ["hazardous"]
    assert hazard_tags('["radiation_hazard", "binary_system"]') == ["radiation_hazard"] and hazard_tags(None) == []


def test_systems_page_lists_scan_tags(client):
    """The Systems page lists every system tag in the cached scans with the systems carrying it."""
    import json
    db = client.app.state.db
    for star, tags in (("SOL", ["binary_system", "hazardous"]), ("KELM", ["hazardous"])):
        client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                           (star, json.dumps({"system_tags": tags}), "2026-10-21T00:00:00+00:00"))
    body = client.get("/systems", headers=H).text
    assert 'id="system-tags"' in body and 'href="/systems/KELM">KELM</a>' in body
    assert body.index(">hazardous<") < body.index(">binary system<")   # most common first


def test_map_star_tags(client):
    """The galaxy map gets each scanned system's tags (resource_rich and anomaly have their own markers)."""
    import json
    db = client.app.state.db
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("SOL", json.dumps({"system_tags": ["resource_rich", "anomaly"]}), "2026-10-21T00:00:00+00:00"))
    sol = next(s for s in client.get("/api/map.json?part=core", headers=H).json()["stars"] if s["designation"] == "SOL")
    assert sol["tags"] == ["resource_rich", "anomaly"] and "hazardous" not in sol


def test_logistics_drop_off_keeps_replicants_aboard():
    from rsweb.logistics import stray_aboard
    devs = [{"device_code": "M1", "device_type": "replicant_matrix", "features": ["stow", "matrix"], "stowed_in_device_code": "LV"},
            {"device_code": "C1", "device_type": "matrix_container", "features": ["cruise", "cradle"], "attached_to_device_code": "LV"},
            {"device_code": "M2", "device_type": "replicant_matrix", "stowed_in_device_code": "C1"},
            {"device_code": "P1", "device_type": "odd_pod", "features": [], "stowed_in_device_code": "LV"},
            {"device_code": "M3", "device_type": "empty_replicant_matrix", "stowed_in_device_code": "P1"},
            {"device_code": "D1", "device_type": "survey_drone", "features": ["stow"], "stowed_in_device_code": "LV"}]
    assert stray_aboard({"device_code": "LV"}, devs, set(), set()) == [{"code": "D1", "mode": "stow"}]


# --- 1.189.0: a device's page says what it's doing ------------------------------------------------------------------

def test_device_page_shows_fleet_status_and_current_task(client):
    """Joe 2026-10-21: a fleet member's page shows its fleet's status, a logistics vessel's its current task."""
    eng = client.app.state.worker.automations
    fleet, devices = _fleet_world()
    devices.append({"device_code": "LV000001", "device_type": "cargo_freighter", "location": "AEM-5-L4", "status": "idle",
                    "tags": ["fleet:haulers"], "available_commands": ["travel"]})
    client.portal.call(client.app.state.db.kv_set, "devices", devices)
    fleet["mission"] = {"status": "running", "phase": "work", "idx": 0, "targets": ["XYZ"], "job": "fleets-1-0",
                        "log": [{"at": "2026-10-21T09:00:00+00:00", "text": "mining XYZ-BELT-1"}]}
    client.portal.call(eng.save_fleets, [fleet, {"id": "haulers", "name": "Haulers", "role": "logistics", "home": "AEM"}])
    client.portal.call(eng.save_jobs, [{"id": "logistics-1-0", "rule": "logistics", "title": "logistics: run 3: LV000001 carries 50 iron → BOB",
                                        "device": "LV000001", "idx": 1, "status": "running", "meta": {},
                                        "steps": [{"desc": "LV000001: load 50 iron", "path": "/devices/LV000001", "body": {}},
                                                  {"desc": "LV000001: travel to BOB", "path": "/devices/LV000001", "body": {}}]}])
    page = client.get(f"/devices/{fleet_member_code(devices)}", headers=H).text
    assert 'id="doing"' in page and "on a mission to XYZ (work phase)" in page and "mining XYZ-BELT-1" in page
    page = client.get("/devices/LV000001", headers=H).text
    assert "Current task:</b> run 3: LV000001 carries 50 iron → BOB" in page and "step 2/2: LV000001: travel to BOB" in page
    assert "logistics service: on a run" in page
    client.portal.call(eng.save_jobs, [])
    page = client.get("/devices/LV000001", headers=H).text
    assert "idle, waiting for a task" in page and "Current task" not in page


def fleet_member_code(devices):
    return next(d["device_code"] for d in devices if any(t.startswith("fleet:prospector") for t in d.get("tags") or []))


# --- 1.190.0: Fleets pages keep their plans for an hour ---------------------------------------------------------------

def test_fleet_pages_keep_plans_until_an_edit_or_recompute(client):
    """Joe 2026-10-21: the loadout plan, prospects and destination lists only need rerunning every hour or two."""
    eng = client.app.state.worker.automations
    calls = []
    real = eng.loadout_plan

    async def counted(*a, **k):
        calls.append(1)
        return await real(*a, **k)
    eng.loadout_plan = counted
    assert client.get("/fleets/overview", headers=HX).status_code == 200
    page = client.get("/fleets/details?all=1", headers=H).text
    assert len(calls) == 1 and "kept an hour" in page and "Recompute" in page
    client.post("/fleets/recompute", headers=HX)
    client.get("/fleets/overview", headers=HX)
    assert len(calls) == 2
    client.post("/fleets/nope/station", data={"station": "on"}, headers=HX)   # any change under /fleets clears them too
    client.get("/fleets/overview", headers=HX)
    assert len(calls) == 3


# --- 1.192.0: star catalog lookups are built once per catalog update ----------------------------------------------------

def test_star_lookups_are_shared_until_the_catalog_changes(client):
    """The engine read and copied the whole star catalog (tens of thousands of stars) several times a pass."""
    eng, db = client.app.state.worker.automations, client.app.state.db

    async def go():
        a = await eng.star_lookup("pos")
        assert a and await eng.star_lookup("pos") is a
        cat = await db.kv_get("stars", {})
        cat["stars"] = [{"designation": "NEWSTAR", "position": {"x": 1, "y": 2, "z": 3}}]
        await db.kv_set("stars", cat)
        b = await eng.star_lookup("pos")
        assert b == {"NEWSTAR": {"x": 1, "y": 2, "z": 3}} and await eng.star_lookup("names") == {"NEWSTAR"}
    client.portal.call(go)


def test_survey_carrier_ferries_members_too_far_to_cruise():
    """Live 2026-10-21: 769B37CC stalled at BELZHORII-KUIPER, ≈190 AU from BELZHORII-1; the fleet's carrier takes it."""
    from rsweb import fleets as fl
    fleet = {"id": "s", "name": "S", "role": "explore", "home": "AAA"}
    devs = [{"device_code": "HV", "device_type": "heaven_vessel", "location": "BBB-KUIPER", "status": "idle",
             "features": ["surge"], "stow_capacity": 10, "tags": ["fleet:s"]},
            {"device_code": "SC", "device_type": "ami_survey_controller", "location": "BBB-KUIPER", "status": "idle",
             "features": ["stow"], "tags": ["fleet:s"]},
            {"device_code": "SD", "device_type": "survey_drone", "location": "BBB-KUIPER", "status": "idle",
             "features": ["stow"], "tags": ["fleet:s"]}]
    steps, problems = fl.explore_work_steps(fleet, devs, "BBB-1", {"BBB-1": 1.0, "BBB-KUIPER": 190.0}, 30)
    assert not problems
    cmds = [(s_["path"], (s_["body"] or {}).get("command")) for s_ in steps]
    assert cmds[:5] == [("/devices/SC", "stow"), ("/devices/SD", "stow"), ("/devices/HV", "travel"),
                        ("/devices/SC", "deploy"), ("/devices/SD", "deploy")]
    assert steps[2]["body"]["destination"] == "BBB-1"
    assert not any(c == "travel" and p != "/devices/HV" for p, c in cmds)
    # no fleet carrier in the system: say so instead of sending them on the long cruise
    devs[0]["location"] = "CCC-1"
    steps, problems = fl.explore_work_steps(fleet, devs, "BBB-1", {"BBB-1": 1.0, "BBB-KUIPER": 190.0}, 30)
    assert not steps and "no fleet carrier" in problems[0]


def test_survey_crew_stays_aboard_at_the_kuiper_belt():
    from rsweb import fleets as fl
    fleet = {"id": "s", "name": "S", "role": "explore", "home": "AAA"}
    devs = [{"device_code": "HV", "device_type": "heaven_vessel", "location": "BBB-KUIPER", "status": "idle",
             "features": ["surge"], "stow_capacity": 10, "tags": ["fleet:s"]},
            {"device_code": "SC", "device_type": "ami_survey_controller", "location": None, "stowed_in_device_code": "HV",
             "status": "stowed", "tags": ["fleet:s"]}]
    assert fl.loaded_outer(fleet, devs)
    devs[0]["location"] = "BBB-1-L4"
    assert not fl.loaded_outer(fleet, devs)


def test_a_new_relay_order_sends_the_relay_already_tagged_there():
    """Live 2026-10-21: MOLURNA's order expired with its relay still at the factory; the new order printed another."""
    from rsweb import ward_orders as wo
    devs = [{"device_code": "R1", "device_type": "ftl_relay", "location": "FAB-BELT-1", "status": "inactive",
             "tags": ["to:molurna"]},
            {"device_code": "R2", "device_type": "ftl_relay", "location": "FAB-BELT-1", "status": "inactive", "tags": ["to:valyra"]}]
    assert wo.bound_to(devs, "MOLURNA", "ftl_relay")["device_code"] == "R1"
    assert wo.bound_to(devs, "TIMIRIS", "ftl_relay") is None


def test_a_new_relay_order_waits_for_a_print_already_queued_for_it():
    from rsweb import ward_orders as wo
    fab = {"device_code": "F1", "device_type": "autofactory", "location": "FAB-BELT-1", "available_commands": ["enqueue_print"],
           "print_queue": [{"device_type": "ftl_relay", "tags": ["to:molurna"]}]}
    assert wo.printing_for([fab], "MOLURNA", "ftl_relay")["device_code"] == "F1"
    assert wo.printing_for([fab], "VALYRA", "ftl_relay") is None
    fab2 = {"device_code": "F2", "device_type": "autofactory", "print_queue": [],
            "printing": {"device_type": "ftl_relay", "tags": ["to:valyra"]}}
    assert wo.printing_for([fab, fab2], "VALYRA", "ftl_relay")["device_code"] == "F2"


def test_an_extra_relay_where_one_already_relays_is_made_spare():
    """Owner 2026-10-21: a second relay delivered to a system with a working one becomes a spare."""
    from rsweb import loadouts as lo
    def dev(code, status, **kw):
        return {"device_code": code, "device_type": "ftl_relay", "location": "AAA-1-L4", "status": status,
                "operational_capacity": 100.0, "features": ["stow"], "available_commands": ["travel", "stow", "deploy"], **kw}
    devices = [dev("R1", "relaying"), dev("R2", "inactive"), dev("R3", "inactive", tags=["to:bbb"])]
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in (("AAA", 0), ("BBB", 5))}
    p = lo.plan({"phases": [], "systems": {}, "fleets": []}, devices, [], {}, stars, {}, set(), [], {})
    assert "spare" in p["tag_add"].get("R2", set()) and "spare" not in p["tag_add"].get("R3", set())
    assert "spare" not in p["tag_add"].get("R1", set())


def test_relays_bound_for_a_covered_system_or_twice_for_one_are_made_spare_before_delivery():
    """Owner 2026-10-21: an extra relay is marked spare where it waits instead of being delivered."""
    from rsweb import loadouts as lo
    def dev(code, t, loc, **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": kw.pop("status", "inactive"),
                "operational_capacity": 100.0, "features": kw.pop("features", ["cruise", "stow"]),
                "available_commands": ["travel", "stow", "deploy"], **kw}
    devices = [dev("W1", "ftl_relay", "AAA-1-L4", status="relaying"),
               dev("R1", "ftl_relay", "HUB-1", tags=["to:aaa"]),                       # AAA already relays
               dev("R2", "ftl_relay", "HUB-1", tags=["to:bbb"]), dev("R3", "ftl_relay", "HUB-1", tags=["to:bbb"]),
               dev("HV", "heaven_vessel", "HUB-1", features=["surge", "cruise"], stow_capacity=5,
                   hosting_replicant={"replicant_code": "X"}, tags=["fleet:logistics"])]
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in (("HUB", 0), ("AAA", 5), ("BBB", 9))}
    cfg = {"phases": [], "systems": {}, "fleets": [{"id": "logistics", "name": "Logistics", "role": "logistics",
                                                    "home": "HUB", "wants": {}}]}
    p = lo.plan(cfg, devices, [], {}, stars, {"HV": "X"}, set(), [], {}, commandable={"HUB", "AAA"})
    for code, tag in (("R1", "to:aaa"), ("R3", "to:bbb")):
        assert "spare" in p["tag_add"][code] and tag in p["tag_remove"][code]
    sent = {x for d in p["deliveries"] for x in d["devices"]}
    assert "R2" in sent and not sent & {"R1", "R3"}


def test_a_stalled_mission_retries_by_itself_with_backoff(client):
    """Owner 2026-10-22: a stalled fleet tries the stalled phase again by itself, waiting longer each time
    (Surveyor 3 sat stalled for hours on a survey controller left in LYRRERARN)."""
    from datetime import datetime, timedelta, timezone
    from rsweb import fleets as fl
    db, eng = client.app.state.db, client.app.state.worker.automations
    devs = [{"device_code": "SC1", "device_type": "ami_survey_controller", "location": "LYR-1-L4", "status": "idle",
             "tags": ["fleet:s3"]},
            {"device_code": "HV1", "device_type": "heaven_vessel", "location": "ARF-1-L4", "status": "idle",
             "tags": ["fleet:s3"]}]
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(eng.save_fleets, [{"id": "s3", "name": "S3", "role": "explore", "home": "FAL", "wants": {},
                                          "mission": {"status": "running", "phase": "deploy", "targets": ["ARF"],
                                                      "idx": 0, "departed": True, "log": []}}])
    client.portal.call(eng.run_fleets)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["status"] == "stalled" and m["auto_retry"]["n"] == 0 and m["auto_retry"]["spot"] == [0, "work"]
    first = datetime.fromisoformat(m["auto_retry"]["at"])
    assert timedelta(minutes=14) < first - datetime.now(timezone.utc) <= timedelta(minutes=15)
    assert "retries by itself at" in client.get("/fleets/details?all=1", headers=H).text
    client.portal.call(eng.run_fleets)   # not due yet: stays put
    assert client.portal.call(eng.fleets)[0]["mission"]["auto_retry"]["n"] == 0
    items = client.portal.call(eng.fleets)
    items[0]["mission"]["auto_retry"]["at"] = "2026-01-01T00:00:00+00:00"
    client.portal.call(eng.save_fleets, items)
    client.portal.call(eng.run_fleets)   # due: tries again, stalls again on the same thing, waits twice as long
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["status"] == "stalled" and m["auto_retry"]["n"] == 1
    assert any("retrying by itself (try 1 of" in x["text"] for x in m["log"])
    assert datetime.fromisoformat(m["auto_retry"]["at"]) - datetime.now(timezone.utc) > timedelta(minutes=29)
    # used up: it waits for Retry
    items = client.portal.call(eng.fleets)
    items[0]["mission"]["auto_retry"].update(n=fl.RETRY_TRIES - 1, at="2026-01-01T00:00:00+00:00")
    client.portal.call(eng.save_fleets, items)
    client.portal.call(eng.run_fleets)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["status"] == "stalled" and m["auto_retry"]["at"] is None
    assert "no more automatic retries" in m["log"][-1]["text"]
    # Retry by hand starts the count over
    client.post("/fleets/s3/control", data={"action": "resume"}, headers=HX)
    m = client.portal.call(eng.fleets)[0]["mission"]
    assert m["auto_retry"]["n"] == 0 and m["auto_retry"]["at"]


def test_blueprint_specs_show_set_fields_only():
    from rsweb.web import blueprint_specs
    specs = blueprint_specs({"device_type": "system_hub", "relay_range": 15.0, "current_hubs": 2, "queue_size": 0,
                             "strength": 0, "stow_capacity": 1, "components": {}, "new_field": "x"})
    assert [(l, v) for l, v, _ in specs] == [("relay range", "15 ly"), ("current hubs", "2"), ("new field", "x")]


def test_nothing_to_mine_at_open_sites_is_skipped_quietly(client):
    eng = client.app.state.worker.automations
    from rsweb.automations import step

    async def refuse(method, path, body=None, *a, **k):
        if (body or {}).get("command") == "start_mining":
            return False, None, f"No {body['resource_type']} remaining at any active site"
        return True, {}, None
    eng.send = refuse
    steps = [step(f"M{i}: mine conductive", f"/devices/M{i}", {"command": "start_mining", "resource_type": "conductive"})
             for i in range(4)] + [step("R1: mine rares onboard", "/replicants/R1/mine", {"resource_type": "rares"})]
    job = client.portal.call(eng.create_job, "fleets", "Delta Force: mining", None, steps, {}, True)
    job = next(j for j in client.portal.call(eng.jobs) if j["id"] == job["id"])
    assert job["status"] == "done" and job["steps"][-1]["status"] == "done"
    log = client.portal.call(eng.db.kv_get, "automation_log", [])
    assert not any("stopped:" in e["text"] for e in log) and all(e["level"] == "info" for e in log if "skipped" in e["text"])


def test_restart_idle_miners_skips_a_resource_with_no_open_site(client):
    from unittest import mock
    """Live 2026-10-23: three alerts at ISOLITHOR-BELT-1 for 'No conductive remaining at any active site'."""
    eng = client.app.state.worker.automations
    client.post("/automations/rules/restart_idle_miners", data={"enabled": "on", "resource": "carbon", "cooldown_minutes": "10"}, headers=HX)
    sent = []

    async def send(method, path, body, why=""):
        sent.append((path, body))
        return False, None, "No carbon remaining at any active site"
    db = client.app.state.db
    with mock.patch.object(eng, "send", send):
        client.portal.call(eng.rule_restart_idle_miners)
        assert any(b.get("resource_type") == "carbon" for _, b in sent)
        log = client.portal.call(db.kv_get, "automation_log", [])
        assert not any(e["level"] == "alert" and "could not restart" in e["text"] for e in log)
        assert "carbon" in next(iter(client.portal.call(db.kv_get, "miner_no_site", {}).values()))
        n = len(sent)
        client.portal.call(db.kv_set, "miner_restarts", {})   # past the cooldown: carbon is still skipped
        client.portal.call(eng.rule_restart_idle_miners)
        assert not any(b.get("resource_type") == "carbon" for _, b in sent[n:])


def test_beacon_fetch_skips_a_carrier_with_a_full_hold():
    """Live 2026-10-23: matrix container 10D7491E (1 slot, holding its replicant's matrix, towed) was picked every pass."""
    from rsweb import traffic as tr
    devs = [{"device_code": "MC", "device_type": "matrix_container", "location": "AAA-6-L4", "status": "idle",
             "features": ["cruise", "cradle"], "stow_capacity": 1, "stow_used": 1, "attached_to_device_code": "SP"},
            {"device_code": "B1", "device_type": "ftl_beacon", "location": "AAA-6-L4", "status": "monitoring",
             "tags": ["spare"], "available_commands": ["stow", "deploy"]}]
    p = tr.placement("AAA-3", devs, {}, {}, use_replicant_vessel=True)
    assert p["kind"] not in ("fetch", "carry")
    # owner 2026-10-23: the container rides on surge platform SP; the beacon goes on the platform's attach points
    devs.append({"device_code": "SP", "device_type": "surge_platform", "location": "AAA-6-L4", "status": "idle",
                 "features": ["surge", "cruise", "attach"], "attached_devices": [{"device_code": "MC"}], "attach_used": 1})
    devs[0]["hosting_replicant"] = {"replicant_code": "REP"}
    reps = {"REP": {"name": "Sk3y_17", "hosted_device_code": "MC"}}
    p = tr.placement("AAA-3", devs, reps, {}, use_replicant_vessel=True)
    assert p["kind"] == "fetch" and p["vessel"] == "SP" and p["carrier"] == "SP" and p["mode"] == "attach"
    assert "Sk3y_17" in p["text"]   # the platform with a populated container counts as a replicant aboard
    steps = tr.placement_steps(p, "AAA-3")
    assert steps[0]["path"] == "/devices/SP" and steps[0]["body"] == {"command": "attach", "device": "B1"}
    assert steps[-1]["body"] == {"command": "detach", "device": "B1"}
    assert tr.placement("AAA-3", devs, reps, {}, use_replicant_vessel=False)["kind"] not in ("fetch", "carry")
    devs.append({"device_code": "HV", "device_type": "heaven_vessel", "location": "AAA-1", "status": "idle",
                 "features": ["cruise", "surge"], "stow_capacity": 10, "stow_used": 2})
    p = tr.placement("AAA-3", devs, reps, {}, use_replicant_vessel=True)
    assert p["kind"] == "fetch" and p["vessel"] == "HV" and p["mode"] == "stow"   # no replicant moved: first


def test_a_hold_carrier_takes_beacons_for_several_systems_on_one_run():
    """Live 2026-10-23: 17 ordered beacons waited for a carrier each; they ride multi-drop runs like relays."""
    from rsweb import loadouts as lo
    def dev(code, t, loc, **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": "monitoring", "operational_capacity": 100.0,
                "features": kw.pop("features", ["stow", "comms"]), "available_commands": ["travel", "stow", "deploy"], **kw}
    devices = [dev(f"B{t}", "ftl_beacon", "HUB-BELT-1", tags=[f"to:{t.lower()}"]) for t in ("AAA", "BBB")]
    devices.append(dev("RCCC", "ftl_relay", "HUB-BELT-1", status="inactive", features=["cruise", "relay", "stow"],
                       tags=["to:ccc"]))
    devices.append(dev("HV", "heaven_vessel", "HUB-1", status="idle", features=["surge", "cruise"], stow_capacity=3,
                       hosting_replicant={"replicant_code": "R1"}, tags=["fleet:logistics"]))
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in
             (("HUB", 0), ("AAA", 5), ("BBB", 9), ("CCC", 2))}
    cfg = {"phases": [], "systems": {}, "fleets": [{"id": "logistics", "name": "Logistics", "role": "logistics",
                                                    "home": "HUB", "wants": {}}]}
    p = lo.plan(cfg, devices, [], {}, stars, {"HV": "R1"}, set(), [], {}, commandable={"HUB", "AAA", "BBB", "CCC"})
    dl = [d for d in p["deliveries"] if d["carrier"] == "HV"]
    assert len(dl) == 1 and sorted(dl[0]["devices"]) == ["BAAA", "BBBB", "RCCC"], (p["deliveries"], p["unmet"])


def test_a_surge_plate_attaches_a_beacon_it_flies_to():
    """Owner 2026-10-23: platforms and plates take cargo on their attach points, beacons included."""
    from rsweb import loadouts as lo
    def dev(code, t, loc, **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": kw.pop("status", "idle"),
                "operational_capacity": 100.0, "features": kw.pop("features", ["stow", "comms"]),
                "available_commands": kw.pop("available_commands", ["stow", "deploy"]), **kw}
    devices = [dev("BAAA", "ftl_beacon", "HUB-BELT-1", status="monitoring", tags=["to:aaa"]),
               dev("SP", "surge_plate", "HUB-BELT-1", features=["surge", "cruise", "attach"],
                   available_commands=["travel", "attach", "detach"], tags=["fleet:logistics"])]
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in (("HUB", 0), ("AAA", 5))}
    cfg = {"phases": [], "systems": {}, "fleets": [{"id": "logistics", "name": "Logistics", "role": "logistics",
                                                    "home": "HUB", "wants": {}}]}
    p = lo.plan(cfg, devices, [], {}, stars, {}, set(), [], {}, commandable={"HUB", "AAA"})
    dl = [d for d in p["deliveries"] if d["carrier"] == "SP"]
    assert dl and dl[0]["devices"] == ["BAAA"] and dl[0]["mode"] == "attach", (p["deliveries"], p["unmet"])
    descs = [s["desc"] for s in lo.delivery_steps(dl[0], {d["device_code"]: d for d in devices}, stars, False)]
    assert "SP: attach BAAA" in descs and not any(d.startswith("stow BAAA") for d in descs)


def test_blueprint_unlocked_source_v361(client):
    from rsweb import mock as mockmod
    from rsweb.notify import describe
    ev = {"event": "blueprint.unlocked", "payload": {"device_type": "hydroponic_bay", "source": "decommission"}}
    assert describe(ev) == "Blueprint unlocked: hydroponic bay (by decommissioning)"
    w = client.app.state.worker
    client.portal.call(w.sync_blueprints)   # a first sync takes everything as known
    client.portal.call(w.db.kv_set, "blueprint_sources", {"hydroponic_bay": "decommission"})
    extra = {"device_type": "hydroponic_bay", "short_description": "Bay", "print_time": 60, "resources": {"carbon": 5}}
    mockmod.BLUEPRINTS.append(extra)
    try:
        assert client.portal.call(w.sync_blueprints) == ["hydroponic_bay"]
    finally:
        mockmod.BLUEPRINTS.remove(extra)
    assert "New blueprint unlocked: hydroponic bay (by decommissioning)" in client.get("/notifications", headers=H).text


def test_restart_idle_miners_mines_what_open_sites_hold_for_the_local_factory(client):
    """Live 2026-10-23 ISOLITHOR-BELT-1: the drones were sent for conductive (short somewhere) while the five open sites
    held carbon, silicates and volatiles and the autofactory there waited for rares and silicates."""
    from unittest import mock
    eng = client.app.state.worker.automations
    db = client.app.state.db
    client.post("/automations/rules/restart_idle_miners", data={"enabled": "on", "resource": "same", "cooldown_minutes": "10"}, headers=HX)
    devs = [{"device_code": "MD1", "device_type": "mining_drone", "status": "idle", "location": "ISO-BELT-1",
             "available_commands": ["start_mining"]},
            {"device_code": "AF1", "device_type": "autofactory", "status": "waiting_for_resources", "location": "ISO-BELT-1",
             "waiting_for": {"rares": {"have": 0, "need": 20}, "silicates": {"have": 27, "need": 40}}}]
    client.portal.call(db.kv_set, "loc:ISO-BELT-1", {"resource_sites": [
        {"designation": "ISO-BELT-1-SITE-4", "resources_remaining_pct": {"carbon": 100, "conductive": 0, "rares": 0}},
        {"designation": "ISO-BELT-1-SITE-6", "resources_remaining_pct": {"carbon": 100, "silicates": 100}}]})
    sent = []

    async def send(method, path, body, why=""):
        sent.append((path, body))
        return True, {}, None

    async def needs(devices):
        return {"conductive": 500.0}

    async def devices(*a, **k):
        return devs
    with mock.patch.object(eng, "send", send), mock.patch.object(eng, "print_needs", needs), \
            mock.patch.object(eng, "devices", devices):
        client.portal.call(eng.rule_restart_idle_miners)
    assert sent == [("/devices/MD1", {"command": "start_mining", "resource_type": "silicates"})]


def test_idle_survey_drones_search_a_belt_under_the_site_cap(client):
    """2026-10-23: idle survey drones at ISOLITHOR-BELT-1, whose open sites lacked what the factory waited for; the belt
    wasn't worked out, so nothing sent them searching."""
    eng = _enable(client, "reopen_sites")
    db = client.app.state.db
    devs = [{"device_code": f"S{i}", "device_type": "survey_drone", "status": "idle", "location": "ISO-BELT-1"} for i in range(3)]
    devs.append({"device_code": "S9", "device_type": "survey_drone", "status": "tracking", "location": "ISO-BELT-1"})
    sites = [{"designation": f"ISO-BELT-1-SITE-{i}", "resources_remaining_pct": {"carbon": 100}} for i in range(3)]
    client.portal.call(db.kv_set, "loc:ISO-BELT-1", {"resource_sites": sites})
    client.portal.call(db.kv_set, "belt_reads", {"ISO-BELT-1": "2099-01-01T00:00:00+00:00"})

    async def devices(*a, **k):
        return devs
    eng.devices = devices
    done = client.portal.call(eng.rule_reopen_sites)
    assert done == ["S0 → search ISO-BELT-1", "S1 → search ISO-BELT-1"]   # 3 open of 5
    assert client.portal.call(eng.rule_reopen_sites) == []   # running jobs / cooldown


def test_asteroid_impacts_are_warnings_and_unread_messages_stand_out(client):
    from rsweb import notify
    for e in ("system.object_impacted", "diversion.impacted"):
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


def test_mining_fleets_may_work_a_hub_home_without_miners(client):
    from rsweb import fleets as fl
    hub = {"id": "h", "name": "Hub", "role": "mining", "home": "HUB", "station": True, "wants": {"autofactory": 1}}
    mine = {"id": "k", "name": "Kel", "role": "mining", "home": "KEL", "station": True, "wants": {"mining_drone": 6}}
    devs = [{"device_code": "D1", "device_type": "mining_drone", "location": "HUB-BELT-1", "tags": ["fleet:h"]}]
    assert fl.worked_systems([hub, mine]) == {"HUB", "KEL"}          # explore missions: every stationed home
    assert fl.worked_systems([hub, mine], []) == {"KEL"}             # mining: a hub without miners is open
    assert fl.worked_systems([hub, mine], devs) == {"HUB", "KEL"}    # a miner among its members claims it again
    eng = client.app.state.worker.automations
    client.portal.call(client.app.state.db.kv_set, "stars", {"stars": [{"designation": "SOL"}, {"designation": "HUB"},
                                                                        {"designation": "KEL"}]})
    client.portal.call(eng.save_fleets, [hub, mine, {"id": "m", "name": "M", "role": "mining", "home": "FAL", "wants": {}},
                                         {"id": "e", "name": "E", "role": "explore", "home": "FAL", "wants": {}}])
    r = client.post("/fleets/m/mission", data={"targets": "KEL"}, headers=HX)
    assert "only the stationed fleet works" in r.text
    r = client.post("/fleets/m/mission", data={"targets": "HUB"}, headers=HX)
    assert "only the stationed fleet works" not in r.text
    r = client.post("/fleets/e/mission", data={"targets": "HUB"}, headers=HX)
    assert "only the stationed fleet works" in r.text


def test_data_upload_at_interface(client):
    """v3.6.2: a data-feature device deployed at the interface, with a replicant there, can be uploaded (consumed);
    elsewhere or stowed it only says why not. device.discovered raises a notification."""
    from rsweb import notify, sim
    db, eng = client.app.state.db, client.app.state.worker.automations
    devs = client.portal.call(db.kv_get, "devices")
    reps = client.portal.call(db.kv_get, "replicants") or {}
    rep_loc = next(r.get("location") for r in (reps.values() if isinstance(reps, dict) else reps) if r.get("location"))
    client.portal.call(eng.sim_save, {"interface": {"code": "IF000001", "location": rep_loc}})
    chip = {"device_code": "GC000001", "device_type": "guidance_chip", "location": rep_loc, "status": "idle", "features": ["data"]}
    far = {"device_code": "GC000002", "device_type": "guidance_chip", "location": "NOWHERE-1", "status": "idle", "features": ["data"]}
    client.portal.call(db.kv_set, "devices", devs + [chip, far])
    client.app.state.api.http._transport.app.state.world.devices.append(dict(chip))
    assert "Data uploads" in client.get("/simulations", headers=H).text
    frag = client.get("/simulations/uploads", headers=HX).text
    assert "/simulations/upload/GC000001" in frag and "/simulations/upload/GC000002" not in frag and "not at the interface" in frag
    r = client.post("/simulations/upload/GC000001", headers=HX).text
    assert "analysis chute" in r or "upload" in r.lower()
    world = client.app.state.api.http._transport.app.state.world
    assert not any(d.get("device_code") == "GC000001" for d in world.devices)
    # the cached device list may still hold the consumed chip: it leaves the list, a count stays
    frag = client.get("/simulations/uploads", headers=HX).text
    assert "GC000001" not in frag and "Uploaded before: 1 (1 guidance chip)" in frag and "GC000002" in frag
    # uploads sent before 1.272.0 are recovered from the action log once
    client.portal.call(db.kv_set, "sim:uploaded", None)
    client.portal.call(db.execute, "INSERT INTO actions(at, user, method, path, body, status) VALUES(?,?,?,?,?,?)",
                       ("2026-10-25T00:00:00Z", "x", "POST", "/devices/IF000001/upload", '{"device_code": "GC000002"}', 200))
    frag = client.get("/simulations/uploads", headers=HX).text
    assert "GC000002" not in frag and "Uploaded before: 2" in frag
    # a data device deployed at the datacenter that leaves the game's list was consumed: dropped, not kept unlisted
    from rsweb.sim import DATACENTER
    eaten = {"device_code": "SC000009", "device_type": "sensor_core", "location": DATACENTER, "status": "idle",
             "features": ["stow", "data"]}
    client.portal.call(db.kv_set, "devices", (client.portal.call(db.kv_get, "devices") or []) + [eaten])
    client.portal.call(client.app.state.worker.sync_devices)
    assert not any(d.get("device_code") == "SC000009" for d in client.portal.call(db.kv_get, "devices"))
    assert client.portal.call(db.kv_get, "sim:uploaded")["SC000009"]["type"] == "sensor_core"
    msg = notify.describe({"event": "device.discovered", "location": "MIRFAKA-OBJ-1",
                          "payload": {"device_type": "guidance_chip", "device_code": "A1B2C3D4"}})
    assert msg == "Scan discovered a guidance chip A1B2C3D4 at MIRFAKA-OBJ-1"


def test_snapshot_share_budget():
    from rsweb.snapshot import share_budget
    assert share_budget(30, {"controllers": 24, "belts": 9, "idle": 12}) == {"controllers": 11, "belts": 9, "idle": 10}
    assert share_budget(30, {"controllers": 24, "belts": 0, "idle": 0}) == {"controllers": 24, "belts": 0, "idle": 0}
    assert share_budget(0, {"controllers": 4}) == {"controllers": 0}
    assert sum(share_budget(7, {"a": 10, "b": 10, "c": 10}).values()) == 7


def test_snapshot_sections_checkboxes(client):
    import asyncio
    db = client.app.state.db
    page = client.get("/diagnostics", headers=H).text
    assert 'name="section" value="perf" checked' in page and 'name="sections_form"' in page
    client.post("/diagnostics/snapshot", data={"stars": "", "sections_form": "1",
                                               "section": ["controllers", "belts", "idle", "fleets"]}, headers=HX)
    for _ in range(100):
        st = client.portal.call(db.kv_get, "snapshot_status", {})
        if st.get("state") != "running":
            break
        client.portal.call(asyncio.sleep, 0.05)
    assert st["state"] == "done", st
    snap = client.portal.call(db.kv_get, "snapshot_last", None)
    paths = [c["path"] for c in snap["calls"]]
    assert "/inventory" not in paths and "/events" not in paths and any(p.startswith("/locations/") for p in paths)
    assert "perf" not in snap["app"] and "actions" not in snap["app"] and "fleets" in snap["app"]
    assert "changes" not in snap["app"]["server"] and snap["app"]["server"]["version"]
    assert all("omitted" in c["body"] for c in snap["calls"] if c["path"] == "/devices")
    assert snap["diagnosis"]["drones"]   # read from app.devices when the raw pages are left out
    assert set(snap["sections"]) == {"controllers", "belts", "idle", "fleets"}
    page = client.get("/diagnostics", headers=H).text   # the choice is kept
    assert 'value="perf" checked' not in page and 'value="fleets" checked' in page


def test_live_redraw_is_cache_only_and_not_a_visit(client):
    """Live sections fetch the page again with X-Live: that's no visit (the digest's "since" stays) and the Messages page
    doesn't ask the game for messages then; live pages mark their <main> for live cards."""
    db = client.app.state.db
    calls = []
    real = client.app.state.worker.sync_messages

    async def spy():
        calls.append(1)
        return await real()
    client.app.state.worker.sync_messages = spy
    r = client.get("/messages", headers={**H, "X-Live": "1"})
    assert r.status_code == 200 and "data-live-cards" in r.text and not calls
    assert client.portal.call(db.fetchone, "SELECT * FROM visitors") is None
    client.get("/messages", headers=H)
    assert calls and client.portal.call(db.fetchone, "SELECT * FROM visitors")
    assert 'id="live-switch"' in r.text and 'hx-trigger="sse:state"' in r.text
    assert "data-live-cards" not in client.get("/map", headers=H).text


def test_orders_refresh_the_devices_they_touch(client):
    """After your own order the touched devices (path + codes in the body) are asked for again and patched into the
    cached list; events about them refresh them for a while after."""
    from rsweb.ingest import Worker
    w, db = client.app.state.worker, client.app.state.db
    assert Worker.order_codes("/devices/AB12CD34", {"command": "stow", "cargo": "0011AAFF", "x": "travel"}) == {"AB12CD34", "0011AAFF"}
    assert Worker.order_codes("/replicants/R1/travel", {"destination": "SOL-3"}) == set()
    devices = client.portal.call(db.kv_get, "devices")
    code = devices[0]["device_code"]
    devices[0]["status"] = "stale"
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(w.refresh_devices_now, {code})
    assert next(d for d in client.portal.call(db.kv_get, "devices") if d["device_code"] == code)["status"] != "stale"
    w._watch.clear()
    w.watched_event({"event": "device.stowed", "device_code": code})
    assert not w._bg
    devices = client.portal.call(db.kv_get, "devices")
    devices[0]["status"] = "stale"
    client.portal.call(db.kv_set, "devices", devices)

    async def arrive():   # an arrival refreshes the device straight away
        w.watched_event({"event": "travel.arrived", "device_code": code})
        await asyncio.gather(*w._bg)
    client.portal.call(arrive)
    assert next(d for d in client.portal.call(db.kv_get, "devices") if d["device_code"] == code)["status"] != "stale"


def test_foreign_devices_stay_out_of_the_list(client):
    """GET /devices/{code} answers for another player's device too (with its owner): it is never added to the cached
    list, and one already there (Bill's interface F4791EDF, 2026-10-27) goes on the next sync, not kept as unlisted."""
    from rsweb.ingest import foreign
    w, db = client.app.state.worker, client.app.state.db
    bill = {"name": "Bill", "replicant_code": "A8F48B26"}
    mine = set(client.portal.call(db.kv_get, "replicants") or {})
    assert foreign({"owner": bill}, mine) and not foreign({"owner": None}, mine)
    assert not foreign({"owner": {"replicant_code": next(iter(mine))}}, mine)
    real = w.api.get

    async def fake(path, *a, **kw):
        if path == "/devices/F4791EDF":
            return {"device_code": "F4791EDF", "device_type": "replicant_interface", "owner": bill}
        return await real(path, *a, **kw)
    w.api.get = fake
    try:
        client.portal.call(w.refresh_devices_now, {"F4791EDF"})
    finally:
        w.api.get = real
    assert "F4791EDF" not in {d["device_code"] for d in client.portal.call(db.kv_get, "devices")}
    devices = client.portal.call(db.kv_get, "devices")
    client.portal.call(db.kv_set, "devices", devices + [{"device_code": "F4791EDF", "device_type": "replicant_interface",
                                                          "owner": bill, "location": "MIRFAKA-OBJ-1"}])
    client.portal.call(w.sync_devices)
    assert "F4791EDF" not in {d["device_code"] for d in client.portal.call(db.kv_get, "devices")}


def test_heavy_passes_spread_over_ticks(client):
    eng = client.app.state.worker.automations
    ran = []
    for name in ("rule_salvage", "rule_restart_idle_miners", "rule_reopen_sites"):
        async def fake(n=name):
            ran.append(n)
            return []
        setattr(eng, name, fake)
    for _ in range(4):   # the first tick runs everything, then one heavy pass per tick
        client.portal.call(eng.tick)
    assert sorted(ran[:3]) == ["rule_reopen_sites", "rule_restart_idle_miners", "rule_salvage"]
    assert ran[3:] == ["rule_restart_idle_miners", "rule_reopen_sites", "rule_salvage"], ran


def test_printing_hubs_print_for_each_other_within_sector_range():
    """Owner 2026-10-26: FALQUORYX and SOLARES (~500 ly apart) print for each other when one's stockpile can't cover a
    print (SOLARES relays sat waiting for conductive); a hub beyond the sector range (Delta, ISOLITHOR) stays apart."""
    from rsweb import loadouts as lo, stock, ward_orders as wo

    def fac(code, loc, fid, **kw):
        return {"device_code": code, "device_type": "autofactory", "location": loc, "status": "idle", "print_queue": [],
                "available_commands": ["enqueue_print"], "tags": [f"fleet:{fid}"], **kw}
    devices = [fac("SF", "SOL-BELT-1", "sol"), fac("FF", "FAL-BELT-1", "fal"), fac("DF", "DEL-BELT-1", "del")]
    fleets = [{"id": fid, "name": fid, "home": home, "station": True, "materials": "self", "wants": {}}
              for fid, home in (("sol", "SOL"), ("fal", "FAL"), ("del", "DEL"))]
    fleets[0]["wants"] = {"ftl_relay": 1}
    bps = [{"device_type": "ftl_relay", "resources": {"conductive": 120, "structural": 80}, "print_time": 600}]
    stars = {"SOL": {"position": {"x": 0, "y": 0, "z": 0}}, "FAL": {"position": {"x": 500, "y": 0, "z": 0}},
             "DEL": {"position": {"x": -29000, "y": 0, "z": 0}}}
    cfg = {"phases": [], "fleets": fleets, "fleets_migrated": True}
    rich = {"conductive": 3000, "structural": 9000}
    p = lo.plan(cfg, devices, bps, {"SOL-BELT-1": {"structural": 9000}, "FAL-BELT-1": rich}, stars, {}, set(), [], {})
    assert [(pr["factory"], pr["star"]) for pr in p["prints"]] == [("FF", "SOL")]      # the stocked hub prints it
    p = lo.plan(cfg, devices, bps, {"SOL-BELT-1": {"structural": 9000}, "DEL-BELT-1": rich}, stars, {}, set(), [], {})
    assert [pr["factory"] for pr in p["prints"]] == ["SF"]                               # Delta's isn't lent: waits at home
    p = lo.plan(cfg, devices, bps, {"SOL-BELT-1": rich, "FAL-BELT-1": rich}, stars, {}, set(), [], {})
    assert [pr["factory"] for pr in p["prints"]] == ["SF"]                               # stocked at home: home first

    # keep in stock: partners in range, prints bound here count for here (and not for the hub printing them)
    pos = {k: v["position"] for k, v in stars.items()}
    sol, fal = {**fleets[0], "stock": {"ftl_relay": 1}}, {**fleets[1], "stock": {"ftl_relay": 1}}
    assert stock.partners(sol, fleets, pos, 1728) == [fleets[1]]
    devs = [fac("SF", "SOL-BELT-1", "sol"),
            fac("FF", "FAL-BELT-1", "fal", print_queue=[{"device_type": "ftl_relay", "tags": ["spare", "stock", "gather", "to:sol"]}])]
    assert stock.shortfall(sol, devs, [fal]) == {} and stock.shortfall(fal, devs) == {"ftl_relay": 1}
    devs[1]["print_queue"] = []
    devs.append({"device_code": "R1", "device_type": "ftl_relay", "location": "FAL-BELT-1", "tags": ["spare", "gather", "to:sol"]})
    assert stock.shortfall(sol, devs, [fal]) == {} and stock.shortfall(fal, devs) == {"ftl_relay": 1}   # on its way
    # owner 2026-10-28: a partner's spares beyond its own stock go over before anything is printed (SOLARES printed
    # relays while ~95 sat at FALQUORYX); the hub keeps its own stock, stock-tagged ones first
    spare = lambda c, loc, *t, **kw: {"device_code": c, "device_type": "ftl_relay", "location": loc, "status": "inactive",
                                      "tags": ["spare", *t], **kw}
    devs = [spare("A", "FAL-BELT-1"), spare("B", "FAL-1-L4", "stock"), spare("C", "FAL-BELT-1"),
            spare("D", "FAL-BELT-1", "to:sol"), spare("E", "FAL-BELT-1", "at:fal-1-l4"), spare("F", "FAL-BELT-1", status="relaying"),
            spare("G", "SOL-BELT-1")]
    assert [d["device_code"] for d in stock.surplus(fal, "ftl_relay", devs)] == ["A", "C"]
    assert [d["device_code"] for d in stock.surplus(fal, "ftl_relay", devs, {"A"})] == ["C"]
    # the loadout planner carries a spare sent over (gather + to:) as a spare: it stays spare (and off) there
    devs = [fac("SF", "SOL-BELT-1", "sol"), spare("S", "FAL-BELT-1", "stock", "gather", "to:sol")]
    p = lo.plan({**cfg, "fleets": [{**fleets[0], "wants": {}}, fleets[1]]}, devs, bps, {}, stars, {}, set(), [], {})
    assert p["moves"].get("S") == "SOL" and "S" in p["gathering"]
    assert lo.self_move_steps("S", "SOL", {}, devs[1], gathering=True)[0]["body"]["configuration"].get("remove_tags") is None

    # relay / ward orders: a factory whose stockpile covers the print first, ahead of a shorter queue
    inv = {"SOL-BELT-1": {"structural": 9000}, "FAL-BELT-1": rich}
    devs = [fac("SF", "SOL-BELT-1", "sol"), fac("FF", "FAL-BELT-1", "fal", print_queue=[{"device_type": "x"}])]
    have = wo.stocked(devs, {"conductive": 120, "structural": 80}, inv)
    assert have == {"FF"} and wo.factory_near(devs, "SOL", set(), pos, 1728, None, have)["device_code"] == "FF"
    assert wo.factory_near(devs, "SOL", set(), pos, 1728)["device_code"] == "SF"


def test_colony_space_stations_on_the_map(client):
    """Owner 2026-10-27: colony space stations aren't megastructures (the game reports no station object, only the
    overview's storyline), so they get their own map mark, kept once the storyline ends."""
    from rsweb import megas
    db = client.app.state.db
    ov = {"storylines": [{"title": "Terraforming the Colony Worlds", "locations": ["ROSALIATE", "SOL-3"],
                          "summary": "Humanity is living on space stations, but they need planets."},
                         {"title": "Other", "locations": ["FALQUORYX"], "summary": "Mine things."}]}
    assert megas.stations_in(ov) == {"ROSALIATE", "SOL"}
    client.portal.call(megas.remember_stations, db, ov)
    client.portal.call(db.kv_set, "overview", {})
    assert {"ROSALIATE", "SOL"} <= client.portal.call(megas.stations, db)
    stars = {s["designation"]: s for s in client.get("/api/map.json", headers=H).json()["stars"]}
    assert stars["SOL"].get("station") and not stars.get("FALQUORYX", {}).get("station")
    assert 'id="opt-mark-station"' in client.get("/map", headers=H).text


def test_scanned_megastructures_land_on_their_star(client):
    """Joe 2026-10-27: scans list stations as megastructures at STAR-OBJ-n; the designation names no star."""
    import json as _json
    from rsweb import megas
    db = client.app.state.db
    scan = {"system_objects": [
        {"designation": "SPACE-STATION-006", "location": "ARUCALIS-OBJ-1", "object_type": "megastructure",
         "megastructure_type": "space_station"},
        {"designation": "DERELICT-RING-001", "location": "TWAFFY-OBJ-1", "object_type": "megastructure",
         "megastructure_type": "derelict_ring"}]}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("ARUCALIS", _json.dumps(scan), "2026-10-27T00:00:00+00:00"))
    known = client.portal.call(megas.known, db)
    assert known["ARUCALIS"] == ["ARUCALIS-OBJ-1"] and "SPACE" not in known
    assert "ARUCALIS" in client.portal.call(megas.stations, db)
    assert megas.other_than_stations(["ARUCALIS-OBJ-1", "TWAFFY-OBJ-1"]) == ["TWAFFY-OBJ-1"]


def test_system_page_folds_out_system_objects_and_the_full_scan(client):
    """Joe 2026-10-27: the scan's system objects (a space station) and the whole scan, under the bodies."""
    import json as _json
    db = client.app.state.db
    scan = {"planets": [{"designation": "ARUCALIS-3", "type": "terrestrial"}], "system_objects": [
        {"designation": "SPACE-STATION-006", "location": "ARUCALIS-OBJ-1", "object_type": "megastructure",
         "megastructure_type": "space_station", "title": "Space Station", "stage": "Populated Space Station",
         "status": "completed", "progress_percentage": 100.0, "description": "Fully operational.", "contributors": 1,
         "contributors_memorial": [{"name": "creme-292", "replicant_code": "E25D16DF", "contributions": 206, "total_value": 143615}],
         "requirements": {"hab_module": {"complete": True, "current": 85, "remaining": 0, "required": 85}}}]}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("ARUCALIS", _json.dumps(scan), "2026-10-27T00:00:00+00:00"))
    page = client.get("/systems/ARUCALIS", headers=H).text
    assert "Populated Space Station" in page and "hab module" in page and "creme-292" in page
    assert "Full system scan" in page and "SPACE-STATION-006" in page


def test_device_list_filters_in_the_browser(client):
    import re
    """The page holds every device, the ones outside the URL's filter hidden; the search box asks /fleet/match."""
    db = client.app.state.db
    client.portal.call(db.kv_set, "devices", [
        {"device_code": "D1", "device_type": "mining_drone", "location": "SOL-1", "available_commands": ["travel"]},
        {"device_code": "D2", "device_type": "survey_drone", "location": "SOL-1", "available_commands": ["system_scan"]}])
    page = client.get("/fleet?type=survey_drone", headers=H).text
    assert 'data-code="D1" data-type="mining_drone"' in page and 'data-code="D2"' in page
    assert re.search(r'data-code="D1"[^>]* hidden>', page) and not re.search(r'data-code="D2"[^>]* hidden>', page)
    assert re.search(r'data-label="travel"[^>]* hidden>', page) and "system scan (1 here)" in page
    assert client.get("/fleet/match?q=survey", headers=H).json() == ["D2"]


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


def test_a_hub_short_of_stock_takes_a_partners_extra_spares_before_printing(client):
    """Owner 2026-10-28: SOLARES printed relays for its stock while ~95 spare relays sat at FALQUORYX. The stock pass
    sends a partner hub's spares beyond its own stock over (gather + to:) and prints only what's still missing."""
    db, eng = client.app.state.db, client.app.state.worker.automations
    sent = []

    async def fake_send(method, path, body, label):
        sent.append((path, body))
        return True, {}, None
    eng.send = fake_send
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": "SOL", "position": {"x": 0, "y": 0, "z": 0}},
                                                      {"designation": "FAL", "position": {"x": 500, "y": 0, "z": 0}}]})
    client.portal.call(db.kv_set, "devices", [
        {"device_code": "F1", "device_type": "ftl_relay", "location": "FAL-BELT-1", "status": "inactive", "tags": ["spare", "stock"]},
        {"device_code": "F2", "device_type": "ftl_relay", "location": "FAL-BELT-1", "status": "inactive", "tags": ["spare"]},
        {"device_code": "F3", "device_type": "ftl_relay", "location": "FAL-BELT-1", "status": "inactive", "tags": ["spare"]}])
    client.portal.call(eng.save_fleets, [
        {"id": f, "name": f, "role": "mining", "home": h, "station": True, "materials": "self", "stock": {"ftl_relay": n}}
        for f, h, n in (("sol", "SOL", 3), ("fal", "FAL", 1))])
    out = client.portal.call(eng.stock_pass)
    assert sent == [(f"/devices/{c}", {"configuration": {"add_tags": ["gather", "stock", "to:sol"]}}) for c in ("F2", "F3")]
    assert sum("sent to keep in stock at SOL" in x for x in out) == 2


def test_stow_into_offers_attach_carriers():
    """Stow into lists carriers here with room: holds first, else attach points (up the chain); full ones are left out."""
    devs = [{"device_code": "ME", "device_type": "mining_drone", "location": "X-BELT-1", "features": ["stow"]},
            {"device_code": "HV", "device_type": "heaven_vessel", "location": "X-BELT-1", "stow_capacity": 10, "stow_used": 10},
            {"device_code": "PL", "device_type": "surge_platform", "location": "X-BELT-1", "attach_capacity": 4},
            {"device_code": "CV", "device_type": "cargo_vessel", "location": "X-BELT-1", "stow_capacity": 50},
            {"device_code": "FAR", "device_type": "cargo_vessel", "location": "Y-BELT-1", "stow_capacity": 50}]
    from rsweb import commands as c
    f = c.COMMANDS["stow"][0]
    opts = {o["value"]: o["label"] for o in c.device_options({"devices": devs, "here": "X-BELT-1"}, f, "ME")}
    assert set(opts) == {"PL", "CV"} and "attach (4 free)" in opts["PL"] and "stow (50 free)" in opts["CV"]


def test_stow_into_attach_carrier_sends_attach(client):
    # survey drone D8C2A140 and surge plate SP000001 are both at SOL-3: the plate has no hold, so it attaches the drone
    r = client.post("/devices/D8C2A140/command", data={"command": "stow", "f.target": "SP000001"}, headers=HX)
    assert r.status_code == 200
    row = client.portal.call(client.app.state.db.fetchone, "SELECT path, body FROM actions ORDER BY id DESC LIMIT 1")
    assert row["path"] == "/devices/SP000001" and '"attach"' in row["body"] and "D8C2A140" in row["body"]


def test_bucket_sliding_window():
    # a full minute's worth sent at once leaves no room until the oldest leaves the window (1.293.0)
    b = Bucket(10)
    for _ in range(10):
        assert b.wait_time() == 0
        b.take()
    assert 59 < b.wait_time() <= 60
    for i in range(10):
        b.recent[i] -= 61
    assert b.wait_time() == 0 and b.used_last_minute() == 0


def test_read_patterns():
    from rsweb import perf
    perf.reads.clear()
    perf.note_read("/devices/15DFB014/logs")
    perf.note_read("/locations/FALQUORYX-BELT-1")
    perf.note_read("/devices")
    rows = {r["path"] for r in perf.reads_report()["rows"]}
    assert rows == {"/devices/{code}/logs", "/locations/{place}", "/devices"}


def test_replicant_details_read_only_when_needed(client):
    # 1.293.0: a replicant's details are re-read when it moved, its vessel had an event, an order named it, or after
    # POLL_REPLICANT — not every account sync
    w = client.app.state.worker
    asked: list[str] = []
    real = w.api.get

    async def counting(path, *a, **kw):
        if path.startswith("/replicants/"):
            asked.append(path)
        return await real(path, *a, **kw)
    w.api.get = counting
    try:
        client.portal.call(w.sync_account)
        first = len(asked)
        client.portal.call(w.sync_account)
        assert len(asked) == first                    # nothing changed: summary only
        code = next(iter(client.portal.call(client.app.state.db.kv_get, "replicants")))
        w.after_order(f"/replicants/{code}/travel", {})
        client.portal.call(w.sync_account)
        assert asked[first:] == [f"/replicants/{code}"]
        reps = client.portal.call(client.app.state.db.kv_get, "replicants")
        hosted = reps[code].get("hosted_device_code")
        if hosted:
            client.portal.call(w.process_event, {"id": "1-0", "event": "travel.arrived", "device_code": hosted, "payload": {}})
            client.portal.call(w.sync_account)
            assert asked[-1] == f"/replicants/{code}" and len(asked) == first + 2
    finally:
        w.api.get = real


def test_relay_orders_use_another_hubs_extras_before_a_hubs_own_stock():
    """Owner 2026-10-28 (use up the spare relays at FALQUORYX): a relay / ward order sends the nearest spare in reach,
    but a printing hub's own keep-in-stock spares go last, since taking one only gets it printed again there."""
    from rsweb import stock, ward_orders as wo
    pos = {"SOL": {"x": 0, "y": 0, "z": 0}, "FAL": {"x": 500, "y": 0, "z": 0}, "NEAR": {"x": 5, "y": 0, "z": 0}}
    r = lambda c, loc, *t: {"device_code": c, "device_type": "ftl_relay", "location": loc, "status": "inactive",
                            "tags": ["spare", *t]}
    devs = [r("S1", "SOL-BELT-1", "stock"), r("F1", "FAL-BELT-1", "stock"), r("F2", "FAL-BELT-1"), r("F3", "FAL-1-L4")]
    hubs = [{"id": "sol", "home": "SOL", "stock": {"ftl_relay": 1}}, {"id": "fal", "home": "FAL", "stock": {"ftl_relay": 1}}]
    keep = stock.kept(hubs, "ftl_relay", devs)
    assert keep == {"S1", "F1"}
    assert wo.spare_ward(devs, "NEAR", set(), pos, 1728, "ftl_relay")["device_code"] == "S1"          # nearest
    assert wo.spare_ward(devs, "NEAR", set(), pos, 1728, "ftl_relay", keep)["device_code"] == "F2"    # FAL's extras first
    assert wo.spare_ward(devs[:2], "NEAR", set(), pos, 1728, "ftl_relay", keep)["device_code"] == "S1"  # else still a spare


def test_station_swapped_for_relay_once_a_relay_path_exists(client):
    """Owner 2026-10-29: once a cheaper relay can link a system another way, the long-range station is replaced and
    goes spare."""
    from rsweb import relaynet as rn
    from rsweb import ward_orders as wo
    P = lambda x: {"x": x, "y": 0, "z": 0}  # noqa: E731
    pos = {"A": P(0), "M": P(4), "S": P(8), "B": P(16), "N": P(12)}
    R = lambda c, t, s: {"device_code": c, "device_type": t, "location": f"{s}-4-L4", "status": "relaying"}  # noqa: E731
    devs = [R("R1", "ftl_relay", "A"), R("R2", "ftl_relay", "B"), R("S1", rn.STATION, "S"), R("R3", "ftl_relay", "M")]
    assert rn.swappable_stations(devs, pos) == []   # S–B is 8 ly: only the station's 10 ly links it
    devs.append(R("R4", "ftl_relay", "N"))
    assert rn.swappable_stations(devs, pos) == ["S"]
    db = client.app.state.db
    eng = client.app.state.worker.automations
    client.portal.call(db.kv_set, "stars", {"stars": [{"designation": s, "position": p} for s, p in pos.items()]})
    calls = []

    async def devices():
        return devs

    async def order(kind, star, user=""):
        calls.append(("add", kind, star))
        await db.kv_set(wo.kv_key(kind), {star: {"at": "2026-10-29T00:00:00+00:00", "how": "print"}})
        return "printing"

    async def remove(kind, star, user=""):
        calls.append(("remove", kind, star))
        return "deactivating"
    eng.devices, eng.order_outpost, eng.remove_outpost = devices, order, remove
    client.portal.call(eng.station_swap_pass)
    client.portal.call(eng.station_swap_pass)   # the order is open: not asked twice
    assert calls == [("add", "relay", "S")]
    devs.append(R("R5", "ftl_relay", "S"))      # the relay is up beside the station
    client.portal.call(eng.station_swap_pass)
    assert calls[-1] == ("remove", "station", "S")
