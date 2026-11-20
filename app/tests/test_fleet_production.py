"""Fleets › Production (fleetyield.py): pile gains where each fleet mines, plus the game's mining.stopped reports."""
from datetime import datetime, timedelta, timezone

from rsweb import fleetyield
from tests.corelib import H, client, iso  # noqa: F401 (client is a fixture)

NOW = datetime(2026, 11, 20, 12, 30, tzinfo=timezone.utc)
FLEETS = [{"id": "miner-1", "name": "Miner_1", "role": "mining", "home": "DELRTHUS"},
          {"id": "chaser-1", "name": "Chaser_1", "role": "rock_chaser", "home": "RENGRELOR"},
          {"id": "surveyor-1", "name": "Surveyor 1", "role": "explore", "home": "FAL"}]
DEVS = [{"device_code": "R1", "device_type": "mining_rig", "tags": ["fleet:chaser-1"], "status": "mining", "location": "ROCK"},
        {"device_code": "R2", "device_type": "mining_rig", "tags": ["fleet:chaser-1"], "status": "idle", "location": "ROCK"},
        {"device_code": "D1", "device_type": "mining_drone", "tags": ["fleet:miner-1"], "status": "mining (carbon)", "location": "BELT"},
        {"device_code": "R3", "device_type": "mining_rig", "tags": ["fleet:chaser-1"], "status": "mining", "location": "BELT"},
        {"device_code": "D2", "device_type": "mining_drone", "tags": [], "status": "mining", "location": "BELT"},
        {"device_code": "S1", "device_type": "survey_drone", "tags": ["fleet:surveyor-1"], "status": "searching", "location": "BELT"}]


def test_gains_split_by_miners_at_the_pile():
    prev = {"ROCK": {"rares": 100}, "BELT": {"carbon": 10, "volatiles": 5}}
    cur = {"ROCK": {"rares": 160}, "BELT": {"carbon": 45, "volatiles": 1}, "HUB": {"carbon": 999}}
    g = {(f, loc, r): q for f, loc, r, q in fleetyield.gains(prev, cur, fleetyield.shares(DEVS))}
    # BELT: drone 1 + rig 1.5 + fleetless drone 1 share 35 carbon; volatiles fell (taken away), the hub mines nothing
    assert g == {("chaser-1", "ROCK", "rares"): 60, ("miner-1", "BELT", "carbon"): 10, ("chaser-1", "BELT", "carbon"): 15,
                 ("", "BELT", "carbon"): 10}


def test_report_sums_per_fleet_and_window():
    def h(fleet, hours_ago, q, res="carbon", loc="ROCK"):
        return {"hour": iso(NOW - timedelta(hours=hours_ago))[:13], "fleet": fleet, "location": loc, "res": res, "q": q}
    hours = [h("chaser-1", 2, 1000), h("chaser-1", 30, 500, "rares"), h("miner-1", 5, 60, "volatiles", "BELT"),
             h("miner-1", 24 * 10, 40, "volatiles", "BELT"), h("", 3, 7), h("surveyor-1", 1, 99)]
    reported = [{"device_code": "R1", "q": 1215, "n": 2}, {"device_code": "GONE", "q": 5, "n": 1}]
    rep = fleetyield.report(FLEETS, DEVS, hours, reported, days=7, now=NOW)
    by = {f["id"]: f for f in rep["fleets"]}
    assert set(by) == {"miner-1", "chaser-1", fleetyield.NO_FLEET}          # survey fleets left out
    c = by["chaser-1"]
    assert c["totals"] == {1: 1000, 7: 1500, 30: 1500} and c["res"] == {"carbon": 1000, "rares": 500}
    assert c["miners"] == 3 and c["mining_now"] == 2 and c["reported"] == 1215 and c["sessions"] == 2
    m = by["miner-1"]
    assert m["totals"] == {1: 60, 7: 60, 30: 100} and m["locs"] == {"BELT": 60}
    assert by[fleetyield.NO_FLEET]["totals"][1] == 106 and by[fleetyield.NO_FLEET]["reported"] == 5
    assert rep["fleets"][0]["id"] == "chaser-1" and rep["fleets"][-1]["id"] == fleetyield.NO_FLEET
    assert rep["grand"][30] == 1706


def test_production_page(client):
    app = client.app

    async def seed():
        db = app.state.db
        await db.kv_set("fleets", [{"id": "chaser-1", "name": "Chaser_1", "role": "rock_chaser", "home": "SOL"}])
        devs = await db.kv_get("devices", [])
        devs[0].update(tags=["fleet:chaser-1"], status="mining", location="SOL-OBJ-1")
        await db.kv_set("devices", devs)
        await db.insert_event({"id": "m1", "event": "mining.stopped", "device_code": devs[0]["device_code"],
                               "payload": {"quantity_mined": 1215, "resource_type": "rares"}, "created_at": iso(NOW)})
        await fleetyield.accrue(db, {"SOL-OBJ-1": {"rares": 0}}, {"SOL-OBJ-1": {"rares": 4321}},
                                fleetyield.shares(devs))
    client.portal.call(seed)
    for days in ("", "?days=1", "?days=30", "?days=5"):
        r = client.get("/fleets/production" + days, headers=H)
        assert r.status_code == 200 and "Chaser_1" in r.text and "1215" in r.text and "4321" in r.text
    assert 'href="/fleets/production"' in client.get("/fleets", headers=H).text
