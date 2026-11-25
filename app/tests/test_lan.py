"""Local network access (Joe 2026-11-25): the tunnel sends the owner at home to the LAN address; port 7443 is the owner."""
import httpx
import pytest
from fastapi.testclient import TestClient

from rsweb import lan
from rsweb.config import Settings
from rsweb.main import create_app
from rsweb.mock import create_mock

HOME = "203.0.113.7"
H = {"X-Auth-Request-Email": "joe@example.com", "Accept": "text/html", "X-Real-IP": HOME, "Host": "rs.example.com"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LAN_URL", "http://docker.local:7443/")
    monkeypatch.setitem(lan.state, "ip", HOME)
    monkeypatch.setitem(lan.state, "ips", [HOME, "2001:db8:5:6::1"])
    monkeypatch.setitem(lan.state, "at", 9e18)   # no lookup in tests
    s = Settings(api_token="dev", api_base="http://mock/v1", db_path=str(tmp_path / "app.sqlite"),
                 allowed_emails=["joe@example.com"], disable_background=True, virtual_queue=False)
    app = create_app(s, transport=httpx.ASGITransport(app=create_mock()))
    with TestClient(app) as c:
        yield c


def test_at_home_pages_move_to_the_lan_address_keeping_the_path(client):
    r = client.get("/fleets?x=1", headers=H, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "http://docker.local:7443/fleets?x=1"


def test_away_fragments_lan_requests_and_opted_out_browsers_stay(client):
    def stays(path="/fleets", **extra):
        return client.get(path, headers={**H, **extra}, follow_redirects=False).status_code != 302
    assert stays(**{"X-Real-IP": "198.51.100.9"})                       # away from home
    assert stays(**{"X-Via-Lan": "1", "X-Real-IP": "192.168.1.20"})    # already on the LAN entry
    assert stays(**{"HX-Request": "true"}) and stays(**{"X-Window": "1"}) and stays(**{"X-Live": "1"})
    assert stays("/api/map.json") and stays(**{"Accept": "application/json"})
    r = client.get("/lan/off?rd=/fleets", headers=H, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/fleets" and "lan=off" in r.headers["set-cookie"]
    assert client.get("/lan/off?rd=//evil.example", headers=H, follow_redirects=False).headers["location"] == "/"
    client.cookies.set("lan", "off")
    assert stays()
    page = client.get("/account", headers=H)
    assert page.status_code == 200 and "stays on the tunnel" in page.text and "http://docker.local:7443" in page.text


def test_lan_off_without_lan_url(monkeypatch):
    monkeypatch.delenv("LAN_URL", raising=False)
    assert lan.redirect_for("GET", "/fleets", "", {"accept": "text/html", "x-real-ip": HOME}) is None


def test_ipv6_counts_as_home_within_the_same_64():
    homes = [HOME, "2001:db8:5:6::1"]
    assert lan.at_home("2001:db8:5:6:abcd::9", homes) and lan.at_home(HOME, homes)
    assert not lan.at_home("2001:db8:5:7::9", homes) and not lan.at_home("203.0.113.8", homes) and not lan.at_home("", homes)
