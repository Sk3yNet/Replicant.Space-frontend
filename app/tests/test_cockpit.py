"""Devices › Cockpit: the swoop debris run flown by hand."""
import json

from rsweb import web_cockpit as wc

from test_ops import H, HX, client  # noqa: F401  (fixture)

HOST = "11ADA230"   # the mock's heaven vessel, which lists swoop


def test_device_page_and_command_form_link_to_the_cockpit(client):  # noqa: F811
    assert f'href="/cockpit/{HOST}"' in client.get(f"/devices/{HOST}", headers=H).text
    form = client.get(f"/devices/{HOST}/command-form?command=swoop", headers=HX).text
    assert f'href="/cockpit/{HOST}"' in form and "boost" in form
    pick = client.get("/cockpit", headers=H).text
    assert f"/cockpit/{HOST}" in pick


def test_swoop_start_and_action_reach_the_game_and_are_logged(client):  # noqa: F811
    page = client.get(f"/cockpit/{HOST}", headers=H).text
    assert "Start swoop" in page and '"actions"' in page
    r = client.post(f"/cockpit/{HOST}/swoop", json={}, headers=HX).json()
    assert r["ok"] and r["response"]["status"] == "swooping" and isinstance(r["ms"], int)
    r = client.post(f"/cockpit/{HOST}/swoop", json={"action": "dive"}, headers=HX).json()
    assert r["ok"] and r["action"] == "dive" and r["response"]["result"] in ("dodged", "hit")
    rows = client.portal.call(client.app.state.db.fetchall, "SELECT body FROM actions ORDER BY id")
    bodies = [json.loads(x["body"]) for x in rows[-2:]]
    assert bodies == [{"command": "swoop"}, {"command": "swoop", "action": "dive"}]
    bad = client.post(f"/cockpit/{HOST}/swoop", json={"action": "jump"}, headers=HX)
    assert bad.status_code == 400


def test_events_about_the_vessel_are_marked_mine():
    ev = {"event": "swoop.debris", "device_code": "OTHER", "payload": json.dumps({"target": HOST, "direction": "left"})}
    out = wc.slim(ev, HOST)
    assert out["mine"] and out["payload"]["direction"] == "left"
    assert not wc.slim({"event": "x", "device_code": "OTHER", "payload": {}}, HOST)["mine"]
