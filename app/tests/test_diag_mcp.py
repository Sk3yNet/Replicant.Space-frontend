"""Account › Claude connector: read-only diagnostics over MCP with the app's own OAuth (diagmcp.py)."""
import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

from tests.corelib import *  # noqa: F401,F403

CB = "https://claude.ai/api/mcp/auth_callback"


def _secret(client):
    r = client.post("/diag-mcp-settings", data={"action": "create"}, headers=HX)
    assert r.status_code == 200
    return re.search(r'id="dm-new-secret"[^>]*value="(rsd_[^"]+)"', r.text).group(1)


def _rpc(client, token, method, params=None, mid=1):
    return client.post("/mcp/me", json={"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}},
                       headers={"Authorization": f"Bearer {token}"})


def test_connector_is_off_until_a_secret_is_made(client):
    assert client.post("/mcp/me", json={}).status_code == 404
    assert client.get("/.well-known/oauth-authorization-server/mcp/me").status_code == 404
    page = client.get("/account", headers=H).text
    assert "Claude connector" in page and "replicant-diagnostics" in page and "/mcp/me" in page


def test_oauth_flow_and_read_only_tools(client):
    secret = _secret(client)
    # unauthenticated: 401 pointing at the resource metadata, which names the authorization server
    r = client.post("/mcp/me", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401 and "resource_metadata=" in r.headers["www-authenticate"]
    meta_url = re.search(r'resource_metadata="([^"]+)"', r.headers["www-authenticate"]).group(1)
    res = client.get(urlsplit(meta_url).path).json()
    issuer = res["authorization_servers"][0]
    srv = client.get("/.well-known/oauth-authorization-server" + urlsplit(issuer).path).json()
    assert srv["issuer"] == issuer and "S256" in srv["code_challenge_methods_supported"]
    # authorize: PKCE, known redirect only
    verifier = "v" * 50
    chal = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = {"response_type": "code", "client_id": "replicant-diagnostics", "redirect_uri": CB, "state": "xyz",
         "code_challenge": chal, "code_challenge_method": "S256"}
    assert client.get("/mcp/me/authorize", params={**q, "redirect_uri": "https://evil.example/cb"},
                      follow_redirects=False).status_code == 400
    r = client.get("/mcp/me/authorize", params=q, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(CB)
    back = parse_qs(urlsplit(r.headers["location"]).query)
    assert back["state"] == ["xyz"]
    code = back["code"][0]
    form = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": CB,
            "client_id": "replicant-diagnostics"}
    assert client.post("/mcp/me/token", data={**form, "client_secret": "rsd_wrong"}).status_code == 401
    # a wrong secret is refused before the code is checked, so the code still works
    tok = client.post("/mcp/me/token", data={**form, "client_secret": secret}).json()
    assert tok["token_type"] == "Bearer"
    assert client.post("/mcp/me/token", data={**form, "client_secret": secret}).status_code == 400   # code used
    # refresh (basic auth), the old refresh token is spent
    basic = "Basic " + base64.b64encode(f"replicant-diagnostics:{secret}".encode()).decode()
    tok2 = client.post("/mcp/me/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]},
                       headers={"Authorization": basic}).json()
    assert tok2["access_token"] != tok["access_token"]
    assert client.post("/mcp/me/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]},
                       headers={"Authorization": basic}).status_code == 400
    at = tok2["access_token"]
    init = _rpc(client, at, "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["capabilities"]["tools"]
    assert client.post("/mcp/me", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                       headers={"Authorization": f"Bearer {at}"}).status_code == 202
    tools = _rpc(client, at, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"overview", "devices", "jobs", "fleets", "logistics", "performance", "kv_get"} <= names
    assert all(t["annotations"]["readOnlyHint"] for t in tools)
    client.portal.call(client.app.state.worker.sync_devices)
    sent_before = client.portal.call(client.app.state.db.fetchone, "SELECT COUNT(*) AS n FROM actions")["n"]
    for name, args in (("overview", {}), ("devices", {"limit": 2}), ("jobs", {"active": True}), ("automation_log", {}),
                       ("notifications", {}), ("fleets", {}), ("logistics", {}), ("game_events", {"limit": 3}),
                       ("actions", {}), ("performance", {}), ("kv_keys", {})):
        out = _rpc(client, at, "tools/call", {"name": name, "arguments": args}).json()["result"]
        assert not out["isError"], (name, out)
        json.loads(out["content"][0]["text"])
    devs = json.loads(_rpc(client, at, "tools/call", {"name": "devices", "arguments": {"limit": 2}}).json()
                      ["result"]["content"][0]["text"])
    assert devs["count"] > 0 and len(devs["devices"]) == 2
    one = devs["devices"][0]["device_code"]
    full = json.loads(_rpc(client, at, "tools/call", {"name": "devices", "arguments": {"code": one}}).json()
                      ["result"]["content"][0]["text"])
    assert full["devices"][0]["device_code"] == one
    # secrets never readable; nothing was sent to the game
    hidden = json.loads(_rpc(client, at, "tools/call", {"name": "kv_get", "arguments": {"key": "diag_mcp"}}).json()
                        ["result"]["content"][0]["text"])
    assert hidden == {"error": "not readable"}
    keys = json.loads(_rpc(client, at, "tools/call", {"name": "kv_keys", "arguments": {}}).json()["result"]["content"][0]["text"])
    assert not any("diag_mcp" in k["key"] or "wallpaper" in k["key"] for k in keys["keys"])
    assert client.portal.call(client.app.state.db.fetchone, "SELECT COUNT(*) AS n FROM actions")["n"] == sent_before
    # the secret works as a plain bearer token; a new secret ends the old tokens
    assert _rpc(client, secret, "ping").status_code == 200
    secret2 = _secret(client)
    assert _rpc(client, at, "ping").status_code == 401 and _rpc(client, secret, "ping").status_code == 401
    assert _rpc(client, secret2, "ping").status_code == 200
    client.post("/diag-mcp-settings", data={"action": "disable"}, headers=HX)
    assert _rpc(client, secret2, "ping").status_code == 404
