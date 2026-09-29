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
            await w.sync_account(); await w.sync_devices(); await w.sync_inventory(); await w.sync_catalogue()
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
