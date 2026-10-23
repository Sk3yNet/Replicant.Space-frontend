"""Logistics carriers spread over the hubs (balance.py, owner 2026-10-23)."""
from rsweb import balance as bal
from test_ops import H, client, eng  # noqa: F401  (fixture)

POS = {"FAL": {"x": 0, "y": 0, "z": 0}, "AEM": {"x": 5, "y": 0, "z": 0}, "SOL": {"x": 500, "y": 0, "z": 0},
       "BAD": {"x": 505, "y": 0, "z": 0}, "DEL": {"x": 29000, "y": 0, "z": 0}}
FLEETS = [{"id": "logistics", "role": "logistics", "home": "FAL"},
          {"id": "fal-hub", "role": "mining", "home": "FAL", "station": True, "materials": "self"},
          {"id": "sol-hub", "role": "mining", "home": "SOL", "station": True, "materials": "self"},
          {"id": "delta-hub", "role": "mining", "home": "DEL", "station": True, "materials": "self"},
          {"id": "miner-5", "role": "mining", "home": "BAD", "station": True, "materials": "supply"}]
CMD = {"FAL", "AEM", "SOL", "BAD", "DEL"}


def mf(code, loc, status="idle"):
    return {"device_code": code, "device_type": "mobile_fleet", "location": loc, "status": status,
            "tags": ["fleet:logistics"], "features": ["surge", "cruise", "attach"]}


def test_targets_one_each_then_by_weight():
    assert bal.targets(4, {"FAL": 3.0, "SOL": 1.0}) == {"FAL": 3, "SOL": 1}
    assert bal.targets(1, {"FAL": 1.0, "SOL": 5.0}) == {"FAL": 0, "SOL": 1}
    assert bal.targets(3, {"FAL": 0.0, "SOL": 0.0}) in ({"FAL": 2, "SOL": 1}, {"FAL": 1, "SOL": 2})
    assert bal.targets(2, {"FAL": 9.0, "SOL": 0.0}) == {"FAL": 1, "SOL": 1}     # at least one each


def test_hubs_leave_out_what_is_beyond_the_sector_range():
    assert bal.hubs(FLEETS, POS, CMD, 1728) == ["FAL", "SOL"]


def test_an_idle_carrier_moves_to_the_hub_short_of_its_share():
    devices = [mf("M1", "FAL-BELT-1"), mf("M2", "FAL-BELT-1"), mf("M3", "AEM-BELT-1"), mf("M4", "FAL-BELT-1", "travelling")]
    demand = {"a": {"star": "BAD"}, "b": {"star": "SOL"}, "c": {"star": "FAL"}, "d": {"star": "AEM"}}
    idle = {"M1": 0.0, "M2": 0.0, "M3": 3000.0}
    p = bal.plan(FLEETS, devices, [], {}, set(), POS, CMD, 1728, demand, idle, 3600.0)
    assert p["classes"]["attach"]["targets"] == {"FAL": 2, "SOL": 2}
    assert [(m["code"], m["from"], m["to"]) for m in p["moves"]] == [("M1", "FAL", "SOL")]   # idle 30 min; one per pass
    assert "M4" not in idle                                                                   # moving: not idle
    # a busy carrier counts where its job takes it: SOL already has its share
    jobs = [{"status": "running", "device": "M4", "meta": {"devices": ["M4"], "star": "SOL"}},
            {"status": "running", "device": "M3", "meta": {"logistics": {"to": "BAD-3-L4"}}}]
    p = bal.plan(FLEETS, devices, jobs, {}, {"M3", "M4"}, POS, CMD, 1728, demand, idle, 3600.0)
    assert p["classes"]["attach"]["counts"] == {"FAL": 2, "SOL": 2} and not p["moves"]


def test_carriers_that_just_went_idle_wait():
    devices = [mf("M1", "FAL-BELT-1"), mf("M2", "FAL-BELT-1")]
    idle = {}
    p = bal.plan(FLEETS, devices, [], {}, set(), POS, CMD, 1728, {}, idle, 100.0)
    assert not p["moves"] and idle == {"M1": 100.0, "M2": 100.0}
    p = bal.plan(FLEETS, devices, [], {}, set(), POS, CMD, 1728, {}, idle, 100.0 + 31 * 60)
    assert [m["code"] for m in p["moves"]] == ["M1"]


def test_logistics_pass_rebalances_and_the_page_shows_the_shares(client):  # noqa: F811
    from datetime import timedelta
    from rsweb.automations import _now
    db, e = client.app.state.db, eng(client)
    old = (_now() - timedelta(hours=1)).isoformat(timespec="seconds")
    client.portal.call(db.kv_set, bal.KV_IDLE, {"M1": old, "M2": old})
    client.portal.call(db.kv_set, bal.KV_DEMAND, {"x": {"star": "SOL", "at": old}})
    out = client.portal.call(e.balance_pass, FLEETS, [mf("M1", "FAL-BELT-1"), mf("M2", "FAL-BELT-1")], {}, POS, CMD,
                             {"SOL": {"designation": "SOL", "entry_point": "SOL-3-L4"}}, {}, set())
    assert out == ["M1 rebalances FAL → SOL"]
    assert any(j["title"] == "logistics: M1 rebalances FAL → SOL" for j in client.portal.call(e.jobs))
    r = client.get("/fleets/logistics", headers=H)
    assert r.status_code == 200 and "Carriers per hub" in r.text and "1 / 1" in r.text
