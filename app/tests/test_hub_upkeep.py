"""Hub upkeep: what each hub wants at its own location, and logistics delivering it."""
from rsweb import logistics as lg, wardhub as wh

from test_logistics import BPS, FLEETS, POS, SURGE, dev
from test_ops import H, add, client  # noqa: F401  (fixture)

REQ = [{"quantity_per_20pct": 73, "resource_type": "carbon"}, {"quantity_per_20pct": 154, "resource_type": "structural"}]


def hub(cap=51.06, loc="A-4-L4", status="relaying"):
    return dev("HUB1", "system_hub", loc, tags=[], status=status, operational_capacity=cap, upkeep_requirements=REQ)


def test_upkeep_need_restores_to_full_plus_one_step():
    n = wh.upkeep_need(hub())
    assert n["per_step"] == {"carbon": 73, "structural": 154} and n["steps"] == 4   # 51 % → 3 steps to 100 %, plus 1
    assert n["target"] == {"carbon": 292, "structural": 616}
    assert wh.upkeep_need(hub(cap=100))["target"] == {"carbon": 73, "structural": 154}


def test_upkeep_rows_compare_the_hub_spot_only_and_skip_inactive_hubs():
    maint = dev("MD", "maintenance_drone", "A-BELT-1", tags=[], ami_directive={"name": "patrol"})
    rows = wh.upkeep_rows([hub(), maint], {"A": {"carbon": 9999}, "A-4-L4": {"carbon": 300, "structural": 16}})
    assert rows[0]["short"] == {"structural": 600} and rows[0]["patrol"] and not rows[0]["urgent"]
    assert not wh.upkeep_rows([hub(status="inactive")], {})


def _plan(devs, inv, upkeep):
    return lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory=inv, pos=POS, commandable={"A", "B"},
                   hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[], trades=[], incoming={},
                   replicants={"R1": {"location": "A-1"}}, drop=lambda s: f"{s}-1", upkeep=upkeep)


def test_logistics_delivers_hub_upkeep_to_the_hub_location_and_keeps_it_there():
    devs = [dev("F1", "cargo_freighter", "A-1", cargo_capacity=1000, **SURGE), dev("HV", "heaven_vessel", "A-1", **SURGE), hub()]
    inv = {"A-1": {"carbon": 500, "structural": 900}, "A-4-L4": {"carbon": 100}}
    p = _plan(devs, inv, wh.upkeep_rows(devs, inv))
    r = next(r for r in p["requests"] if r["id"] == "upkeep:HUB1")
    assert r["need"] == {"carbon": 192, "structural": 616} and r["category"] == "upkeep"
    run = next(x for x in p["runs"] if x["req"] == "upkeep:HUB1")
    assert run["to"] == "A-4-L4" and run["carry"] == {"carbon": 192, "structural": 616}
    assert not [x for x in p["requests"] if x.get("from") == "A-4-L4"]   # the hub's pile isn't consolidated away
    # stocked: no request, and the pile stays
    p = _plan(devs, {"A-1": {"carbon": 500}, "A-4-L4": {"carbon": 400, "structural": 700}},
              wh.upkeep_rows(devs, {"A-4-L4": {"carbon": 400, "structural": 700}}))
    assert not [x for x in p["requests"] if x["id"].startswith("upkeep:") or x.get("from") == "A-4-L4"]


def test_upkeep_page_shows_each_hub(client):  # noqa: F811
    add(client, device_code="HUB00001", device_type="system_hub", location="SOL-4-L4", status="relaying",
        operational_capacity=66.2, upkeep_requirements=REQ)
    html = client.get("/maintenance", headers=H).text
    assert "Hub upkeep" in html and "HUB00001" in html and "219 carbon" in html   # 3 steps (2 to 100 %, plus 1) × 73
