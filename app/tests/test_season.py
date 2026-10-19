from rsweb import automations as au
from rsweb import season
from test_ops import H, HX, client, eng  # noqa: F401  (fixture)


def test_a_new_season_pauses_automations_until_resumed(client):
    e, db, w = eng(client), client.app.state.db, client.app.state.worker
    client.portal.call(w.sync_catalogue)                   # stars, and the overview: season 3 learned, no change
    assert client.portal.call(db.kv_get, season.SEASON_KV)["number"] == 3 and not client.portal.call(e.held)
    client.portal.call(e.season_snapshot_pass)
    snap = client.portal.call(db.kv_get, season.SNAP_KV)
    assert snap["devices"] and "SOL" in snap["stars"]
    client.portal.call(w.season_check, {"number": 4, "title": "Next"})
    hold = client.portal.call(db.kv_get, season.HOLD_KV)
    assert hold["to"]["number"] == 4 and client.portal.call(e.held)
    assert "paused for the season change" in client.get("/", headers=H).text
    # held: no new automated job, running ones wait; a job you start yourself still runs
    assert client.portal.call(e.create_job, "fleets", "t", None, [au.step("x", "/devices/V1", {"command": "deploy"})]) is None
    sent = []

    async def fake_send(method, path, body, label):
        sent.append(path)
        return True, {}, None
    e.send = fake_send
    client.portal.call(e.create_job, "chain", "mine", None, [au.step("x", "/devices/V2", {"command": "deploy"})], {}, True)
    assert sent == ["/devices/V2"]
    client.portal.call(e.season_snapshot_pass)             # frozen while held
    assert client.portal.call(db.kv_get, season.SNAP_KV)["at"] == snap["at"]
    client.post("/season/hold", data={"action": "resume"}, headers=HX)
    assert not client.portal.call(e.held)


def test_pause_by_hand_takes_a_fresh_copy(client):
    db = client.app.state.db
    client.post("/season/hold", data={"action": "pause"}, headers=HX)
    assert client.portal.call(db.kv_get, season.HOLD_KV)["by"] == "joe@example.com"
    assert client.portal.call(db.kv_get, season.SNAP_KV)["devices"]
    page = client.get("/partials/story", headers=H).text
    assert "Resume automations" in page and "Copy of the galaxy" in page
