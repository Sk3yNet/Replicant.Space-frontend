"""The command screen (1.359.0): the same pages, drawn classic, inside the command screen, or alone for its window."""
import httpx
import pytest
from fastapi.testclient import TestClient

from rsweb.config import Settings
from rsweb.main import create_app
from rsweb.mock import create_mock

H = {"X-Auth-Request-Email": "joe@example.com"}


@pytest.fixture()
def client(tmp_path):
    s = Settings(api_token="dev", api_base="http://mock/v1", db_path=str(tmp_path / "app.sqlite"),
                 allowed_emails=["joe@example.com"], disable_background=True, virtual_queue=False)
    app = create_app(s, transport=httpx.ASGITransport(app=create_mock()))
    with TestClient(app) as c:
        c.portal.call(app.state.worker.sync_account)
        c.portal.call(app.state.worker.sync_devices)
        yield c


def test_classic_by_default_command_screen_by_cookie_and_window_alone(client):
    r = client.get("/fleets", headers=H)
    assert r.status_code == 200 and '<nav class="main">' in r.text and 'id="win"' not in r.text
    r = client.get("/bridge/on", headers=H, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/map" and "ui=bridge" in r.headers["set-cookie"]
    client.cookies.set("ui", "bridge")
    client.cookies.set("ui_accent", "%2300ff88")
    r = client.get("/fleets", headers=H)
    assert 'class="bridge" style="--accent: #00ff88"' in r.text and 'id="win"' in r.text and 'id="outliner"' in r.text
    assert '<nav class="main">' not in r.text and 'id="map"' in r.text and 'data-key="fleets"' in r.text
    w = client.get("/fleets", headers={**H, "X-Window": "1"})
    assert w.text.lstrip().startswith("<main") and 'data-title="Fleets"' in w.text and "<html" not in w.text
    m = client.get("/map", headers=H)   # the galaxy is the screen's backdrop: the window starts closed, one map only
    assert 'class="win metal closed"' in m.text and m.text.count('id="map"') == 1
    client.cookies.set("ui_accent", "red;x")   # not a color: the default
    assert "--accent: #e0332f" in client.get("/", headers=H).text
    r = client.get("/bridge/off", headers=H, follow_redirects=False)
    assert r.status_code == 303 and 'ui=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]


def test_outliner_lists_needs_fleets_and_replicants(client):
    async def note():
        await client.app.state.db.execute("INSERT INTO notifications(event_id, level, title, body, link, created_at) "
                                          "VALUES(NULL,'error','Trip held: 10-day surge',NULL,'/fleets/logistics','2026-11-09T20:00:00+00:00')")
    client.portal.call(note)
    r = client.get("/bridge/outliner", headers=H)
    assert r.status_code == 200 and "Trip held: 10-day surge" in r.text and 'href="/fleets/logistics"' in r.text
    assert "Needs you (1)" in r.text and "/replicants/" in r.text
