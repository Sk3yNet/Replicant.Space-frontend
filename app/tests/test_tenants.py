"""Multi-user mode: the manager's registry, routing, walkthrough and the per-user servers it supervises."""
import json
import os
import stat
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from rsweb.tenants import Registry, Supervisor, TenantSettings, create_manager, slug_for

OWNER = "owner@example.com"
ANN = "ann@example.com"
GOOD = "OsiJIqbw_8tj4SLgeo_abcdefgh123"
SAME_ORIGIN = {"Origin": "https://rs.example.com", "Host": "rs.example.com"}


def H(email):
    return {"X-Auth-Request-Email": email, **SAME_ORIGIN}


def game(calls):
    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, req.headers.get("authorization"), req.content))
        if req.url.path.endswith("/accounts") and req.method == "POST":
            return httpx.Response(201, json={"message": "Verification email sent."})
        if req.url.path.endswith("/accounts/recover"):
            return httpx.Response(200, json={"message": "If that email exists, a verification link has been sent"})
        if req.url.path.endswith("/accounts/me"):
            tok = (req.headers.get("authorization") or "")[7:]
            if tok == GOOD:
                return httpx.Response(200, json={"name": "ann", "email": "ann.game@example.com", "timezone": "Europe/London",
                                                 "replicants": [{"name": "ann-1", "replicant_code": "C2AF4A82"}]})
            if tok == GOOD + "x":
                return httpx.Response(200, json={"name": "bob", "email": "ann.game@example.com", "replicants": []})
            return httpx.Response(401, json={"error": "invalid token"})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


class FakeSupervisor(Supervisor):
    """Records starts and stops; every started server is 'running' at once."""

    def start(self, email):
        self.procs[email] = type("P", (), {"port": self.reg.tenants[email]["port"], "state": "running", "restarts": 0,
                                           "last_exit": None, "ready": True, "started_at": time.monotonic(), "log": []})()
        self.started = getattr(self, "started", []) + [email]
        return self.procs[email]

    async def stop(self, email):
        self.procs.pop(email, None)


def settings(tmp_path, **kw):
    base = dict(data_dir=tmp_path, owner_db_path=str(tmp_path / "rsweb.sqlite"), api_base="https://game.test/v1",
                owner_token="", owner_email=OWNER, allowed_emails=[ANN], allowed_domains=["corp.example"],
                open_signup=False, admin_emails=[], base_port=18100, max_tenants=3, default_tz="UTC", dev_user="")
    base.update(kw)
    return TenantSettings(**base)


@pytest.fixture
def mgr(tmp_path):
    calls = []
    app = create_manager(settings(tmp_path), FakeSupervisor, transport=game(calls))
    with TestClient(app) as c:
        c.calls = calls
        yield c


def test_allow_list(tmp_path):
    ts = settings(tmp_path)
    assert ts.allowed(OWNER) and ts.allowed(ANN) and ts.allowed("zed@corp.example")
    assert not ts.allowed("eve@example.com") and not ts.allowed("")
    assert settings(tmp_path, open_signup=True).allowed("eve@example.com")
    assert ts.is_admin(OWNER) and not ts.is_admin(ANN)


def test_route_and_walkthrough_for_new_user(mgr):
    assert mgr.get("/_tenant/route").status_code == 401
    r = mgr.get("/_tenant/route", headers=H(ANN))
    assert r.status_code == 403 and r.headers["x-tenant-state"] == "none"
    page = mgr.get("/_tenant/", headers=H(ANN)).text
    assert "Create a game account" in page and "api_token" in page and "Paste the key here" in page
    # someone not on the list never gets the walkthrough or a server
    assert mgr.get("/_tenant/route", headers=H("eve@example.com")).status_code == 403
    page = mgr.get("/_tenant/", headers=H("eve@example.com")).text
    assert "Not on the list" in page and "Paste the key" not in page
    r = mgr.post("/_tenant/key", data={"token": GOOD}, headers=H("eve@example.com"), follow_redirects=False)
    assert r.status_code == 200 and not mgr.app.state.reg.has_token("eve@example.com")


def test_register_and_recover_call_the_game(mgr):
    r = mgr.post("/_tenant/register", data={"email": "ann.game@example.com", "name": "Ann", "timezone": "Europe/London"},
                 headers=H(ANN))
    assert "Verification email sent." in r.text
    m, path, auth, body = mgr.calls[-1]
    assert (m, path, auth) == ("POST", "/v1/accounts", None)
    assert json.loads(body) == {"email": "ann.game@example.com", "name": "Ann", "timezone": "Europe/London"}
    r = mgr.post("/_tenant/recover", data={"email": "ann.game@example.com"}, headers=H(ANN))
    assert "new key" in r.text and mgr.calls[-1][1] == "/v1/accounts/recover"


def test_key_is_checked_stored_and_routed(mgr, tmp_path):
    reg, sup = mgr.app.state.reg, mgr.app.state.sup
    r = mgr.post("/_tenant/key", data={"token": "not a key"}, headers=H(ANN))
    assert "look like an API key" in r.text
    r = mgr.post("/_tenant/key", data={"token": "WrongWrongWrongWrong123"}, headers=H(ANN))
    assert "accept that key" in r.text and not reg.has_token(ANN)

    r = mgr.post("/_tenant/key", data={"token": f' "{GOOD}" '}, headers=H(ANN), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/_tenant/?step=starting"
    assert mgr.calls[-1][2] == f"Bearer {GOOD}"
    tok = reg.token_path(ANN)
    assert tok.read_text() == GOOD and stat.S_IMODE(os.stat(tok).st_mode) == 0o600
    t = reg.tenants[ANN]
    assert t["replicant"] == "ann-1" and t["tz"] == "Europe/London" and t["slug"] == slug_for(ANN)
    assert GOOD not in (tmp_path / "tenants.json").read_text()

    r = mgr.get("/_tenant/route", headers=H(ANN))
    assert r.status_code == 200 and r.headers["x-tenant-port"] == str(t["port"])
    env = sup.env_for(ANN)
    assert env["ALLOWED_EMAILS"] == ANN and env["RS_API_TOKEN_FILE"] == str(tok) and "RS_API_TOKEN" not in env
    assert env["DB_PATH"] == str(tmp_path / "tenants" / t["slug"] / "rsweb.sqlite") and env["TENANT_MODE"] == "1"

    # one game account can't be connected twice (it would split one rate limit over two servers)
    r = mgr.post("/_tenant/key", data={"token": GOOD + "x"}, headers=H("zed@corp.example"))
    assert "already connected to another user" in r.text

    # disconnect forgets the key but keeps the history
    r = mgr.post("/_tenant/disconnect", headers=H(ANN), follow_redirects=False)
    assert r.status_code == 303 and not tok.exists() and ANN in reg.tenants
    assert mgr.get("/_tenant/route", headers=H(ANN)).status_code == 403


def test_cross_site_posts_refused(mgr):
    r = mgr.post("/_tenant/key", data={"token": GOOD},
                 headers={"X-Auth-Request-Email": ANN, "Origin": "https://evil.example", "Host": "rs.example.com"})
    assert r.status_code == 403 and not mgr.app.state.reg.has_token(ANN)


def test_owner_keeps_stack_token_and_database(tmp_path):
    ts = settings(tmp_path, owner_token="OwnerOwnerOwnerOwner1")
    app = create_manager(ts, FakeSupervisor, transport=game([]))
    with TestClient(app) as c:
        reg, sup = c.app.state.reg, c.app.state.sup
        assert sup.started == [OWNER]
        env = sup.env_for(OWNER)
        assert env["RS_API_TOKEN"] == "OwnerOwnerOwnerOwner1" and env["DB_PATH"] == str(tmp_path / "rsweb.sqlite")
        assert c.get("/_tenant/route", headers=H(OWNER)).status_code == 200
        assert "stack" in c.post("/_tenant/key", data={"token": GOOD}, headers=H(OWNER)).text
        assert c.get("/_tenant/admin", headers=H(OWNER)).status_code == 200
        assert c.get("/_tenant/admin", headers=H(ANN)).status_code == 403
    # ports are stable across restarts
    port = json.loads((tmp_path / "tenants.json").read_text())[OWNER]["port"]
    assert Registry(ts).tenants[OWNER]["port"] == port == 18100


def test_server_limit(tmp_path):
    app = create_manager(settings(tmp_path, max_tenants=1, open_signup=True), FakeSupervisor, transport=game([]))
    with TestClient(app) as c:
        assert c.post("/_tenant/key", data={"token": GOOD}, headers=H(ANN), follow_redirects=False).status_code == 303
        r = c.post("/_tenant/key", data={"token": GOOD}, headers=H("zed@corp.example"))
        assert "server is full" in r.text


def test_real_server_per_user(tmp_path, monkeypatch):
    """Start a real app process for a user and check it serves that user only."""
    monkeypatch.setenv("DISABLE_BACKGROUND", "1")
    ts = settings(tmp_path, base_port=18750)
    app = create_manager(ts, transport=game([]))
    with TestClient(app) as c:
        assert c.post("/_tenant/key", data={"token": GOOD}, headers=H(ANN), follow_redirects=False).status_code == 303
        deadline = time.monotonic() + 60
        while c.get("/_tenant/status", headers=H(ANN)).json()["state"] != "running":
            assert time.monotonic() < deadline, c.app.state.sup.status(ANN)
            time.sleep(0.3)
        port = c.get("/_tenant/route", headers=H(ANN)).headers["x-tenant-port"]
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(f"{base}/healthz").status_code == 200
        assert httpx.get(f"{base}/account", headers={"X-Auth-Request-Email": ANN}).status_code == 200
        assert httpx.get(f"{base}/account", headers={"X-Auth-Request-Email": OWNER}).status_code == 403
        assert (tmp_path / "tenants" / slug_for(ANN) / "rsweb.sqlite").exists()
    # the manager stopped it on the way out
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{base}/healthz", timeout=2)


def test_owner_defaults_to_first_allowed_email(monkeypatch):
    monkeypatch.setenv("OWNER_EMAIL", "")
    monkeypatch.setenv("ALLOWED_EMAILS", "Me@Example.com, friend@example.com")
    ts = TenantSettings()
    assert ts.owner_email == "me@example.com" and ts.allowed("friend@example.com")
