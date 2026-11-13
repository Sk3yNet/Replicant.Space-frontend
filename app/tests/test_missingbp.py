from rsweb import missingbp as mb
from test_ops import client  # noqa: F401  (fixture)

H = {"X-Auth-Request-Email": "joe@example.com"}
HX = {**H, "HX-Request": "true"}
BPS = [{"device_type": "atmospheric_regulator", "resources": {"carbon": 10}, "print_time": 60,
        "components": {"aquifer_tap": 1, "bio_seeder": 1}},
       {"device_type": "terraform_hub", "resources": {"carbon": 1}, "print_time": 60,
        "components": {"atmospheric_regulator": 1}},
       {"device_type": "autofactory", "resources": {"structural": 7}, "print_time": 60}]


def test_rows_rank_components_and_routes():
    bps = {b["device_type"]: b for b in BPS}
    s = mb.normalize({})
    found = mb.sightings(set(bps), {"MINE0001"}, {"R1"},
                         {"stars": {"ZED": {"scanned_at": "2026-11-13T10:00:00+00:00", "raw": [
                             {"device_code": "AAAA0001", "device_type": "bio_seeder", "location": "ZED-1",
                              "owner_replicant_code": "R9", "owner_name": "Vex"},
                             {"device_code": "MINE0001", "device_type": "warp_lens", "location": "ZED-1",
                              "owner_replicant_code": "R1"},
                             {"device_code": "AAAA0002", "device_type": "autofactory", "location": "ZED-1",
                              "owner_replicant_code": "R9"}]}}},
                         {"entries": [{"device_code": "BBBB0001", "device_type": "quantum_lathe", "replicant_code": "R8",
                                       "star": "ZED", "logged_at": "2026-11-13T09:00:00+00:00", "travel_type": "arrival"}]},
                         {"ZED-2": {"devices": [{"device_code": "CCCC0001", "device_type": "story_obelisk"}]}},
                         {"T1": {"location": "ZED-3", "owner_name": "Bill", "trades": [
                             {"name": "Seeds", "trade_code": "x", "rewards": {"devices": {"bio_seeder": 1}},
                              "criteria": {"resources": {"rares": 40}}}]}},
                         [], {"R8": {"name": "Lathe_Guy"}})
    assert {t for t, _, _ in found} == {"bio_seeder", "quantum_lathe", "story_obelisk"}
    mb.merge(s, found, set(bps))
    devices = [{"device_code": "DDDD0001", "device_type": "atmospheric_regulator"}]
    rows = {r["type"]: r for r in mb.rows(s, bps, devices, {"aquifer_tap": "2026-11-13T13:00:00+00:00"})}
    assert set(rows) == {"aquifer_tap", "bio_seeder", "quantum_lathe", "story_obelisk"}
    assert rows["bio_seeder"]["needed_by"] == ["atmospheric_regulator"]
    assert rows["bio_seeder"]["blocks"] == ["atmospheric_regulator", "terraform_hub"]
    assert rows["bio_seeder"]["route"] == "decommission a holder" and rows["bio_seeder"]["players"] == ["Vex"]
    assert rows["bio_seeder"]["shops"][0]["note"] == "Seeds: 1 for 40 rares"
    assert rows["quantum_lathe"]["players"] == ["Lathe_Guy"] and rows["quantum_lathe"]["route"] == "trade"
    order = [r["type"] for r in mb.rows(s, bps, [], {"aquifer_tap": "2026-11-13T13:00:00+00:00"})]
    assert order[:2] == ["aquifer_tap", "bio_seeder"]   # needed by blueprints, refused as locked, first
    mb.merge(s, [], set(bps) | {"bio_seeder"})   # unlocked: off the list
    assert "bio_seeder" not in s["types"]


def test_page_add_and_hide(client):  # noqa: F811
    db = client.app.state.db
    client.portal.call(db.kv_set, "blueprints", BPS)
    page = client.get("/blueprints/missing", headers=H).text
    assert "Aquifer Tap" in page or "aquifer tap" in page.lower()
    client.post("/blueprints/missing/add", data={"device_type": "Warp Lens", "note": "BobNet rumor"}, headers=HX)
    page = client.get("/blueprints/missing", headers=H).text
    assert "warp_lens" in page and "BobNet rumor" in page
    client.post("/blueprints/missing/hide", data={"device_type": "warp_lens"}, headers=HX)
    assert "warp_lens" not in client.get("/blueprints/missing", headers=H).text
    assert "warp_lens" in client.get("/blueprints/missing?hidden=1", headers=H).text
