from rsweb import contractsupply as cs
from rsweb import tradepost as tp
from test_ops import client  # noqa: F401  (fixture)

BPS = {
    "mining_drone": {"device_type": "mining_drone", "print_time": 1800, "resources": {"structural": 100, "conductive": 50}},
    "hydroponic_bay": {"device_type": "hydroponic_bay", "print_time": 3600, "resources": {"carbon": 10}},
    "biosphere_cultivator": {"device_type": "biosphere_cultivator", "print_time": 3600, "resources": {"structural": 20},
                             "components": {"hydroponic_bay": 2}},
    "system_hub": {"device_type": "system_hub", "print_time": 99999, "resources": {"structural": 1}},
}
P = tp.pricing({})
TAG = cs.contract_tag(tp.supply_key("tp"))


def dev(code, t, loc="SOL-4-L4", tags=None, **kw):
    return {"device_code": code, "device_type": t, "location": loc, "status": "idle", "tags": tags or [], **kw}


def shop(loc="SOL-4-L4"):
    return dev("SHOP", "ami_trade_controller", loc, ["fleet:tp"], ami_directive={"name": "trade"})


FLEET = {"id": "tp", "name": "Post", "role": "trade_post", "home": "SOL", "trade_post": {"onhand": {"mining_drone": 2}}}


def test_price_counts_components_and_print_time():
    assert tp.formula_price({"structural": 100, "conductive": 50}, 3 * 3600, P) == {"structural": 230, "conductive": 115}
    bps = {k: {**v, "resources": dict(v["resources"])} for k, v in BPS.items()}
    rows = {r["type"]: r for r in tp.rows({}, bps, P)}
    assert "system_hub" not in rows
    r = rows["biosphere_cultivator"]
    assert r["materials"] == {"structural": 20, "carbon": 20} and r["seconds"] == 3 * 3600
    assert r["formula"] == {"structural": 46, "carbon": 46}   # × (2 + 0.1 × 3 h)
    assert tp.formula_price({"rares": 10}, 0, tp.pricing({"factor": 3, "pct_per_hour": 0})) == {"rares": 30}


def test_parse_price():
    assert tp.parse_price("200 structural, 50 cond") == {"structural": 200, "conductive": 50}
    assert tp.parse_price("rares: 5") == {"rares": 5}
    assert tp.parse_price("") == {}
    assert tp.parse_price("five gold") is None


def test_plan_lists_once_everything_is_there_and_relists_after_sales():
    st = {}
    devs = [shop(), dev("D1", "mining_drone", tags=[TAG])]
    pl = tp.plan(FLEET, devs, [], st, BPS, P, TAG)
    a = pl["actions"]
    assert len(a) == 1 and a[0]["create"]["stock"] == 1   # nothing listed yet: list what is there
    assert a[0]["create"]["criteria"]["resources"] == {"structural": 205, "conductive": 103}
    assert pl["need"] == {"mining_drone": 2}
    listed = [{"trade_code": "TRD-1", "current_stock": 1, "initial_stock": 1, "rewards": {"devices": {"mining_drone": 1}},
               "criteria": {"resources": a[0]["price"]}}]
    st = {"listings": {"mining_drone": {"trade_code": "TRD-1", "escrow": ["D1"]}}}
    pl = tp.plan(FLEET, devs, listed, st, BPS, P, TAG)
    assert pl["actions"] == [] and pl["need"] == {"mining_drone": 1} and pl["skip"] == ["D1"]
    devs.append(dev("D2", "mining_drone", tags=[TAG]))
    pl = tp.plan(FLEET, devs, listed, st, BPS, P, TAG)   # back to full: delete and list both
    assert pl["actions"][0]["delete"] == "TRD-1" and pl["actions"][0]["create"]["stock"] == 2
    assert pl["actions"][0]["escrow"] == ["D1", "D2"]


def test_plan_reprices_and_delists():
    devs = [shop()]
    listed = [{"trade_code": "TRD-1", "current_stock": 2, "initial_stock": 2, "rewards": {"devices": {"mining_drone": 1}},
               "criteria": {"resources": {"structural": 1}}}]
    pl = tp.plan(FLEET, devs, listed, {}, BPS, P, TAG)
    assert pl["actions"][0]["delete"] == "TRD-1" and pl["actions"][0]["create"]["stock"] == 2
    off = {**FLEET, "trade_post": {"onhand": {}}}
    pl = tp.plan(off, devs + [dev("D3", "mining_drone", tags=[TAG])], listed, {}, BPS, P, TAG)
    assert pl["actions"][0]["delete"] == "TRD-1" and pl["actions"][0]["create"] is None and pl["release"] == ["D3"]


def test_plan_never_lists_beside_other_devices_or_a_replicant():
    devs = [shop(), dev("D1", "mining_drone", tags=[TAG]), dev("X", "mining_drone")]
    pl = tp.plan(FLEET, devs, [], {}, BPS, P, TAG)
    assert pl["actions"] == [] and "escrow could take" in pl["rows"][1]["status"] + pl["rows"][2]["status"]
    f = {**FLEET, "trade_post": {"onhand": {"matrix_container": 1}}}
    bps = {**BPS, "matrix_container": {"device_type": "matrix_container", "print_time": 60, "resources": {"structural": 5}}}
    devs = [shop(), dev("MC", "matrix_container", tags=[TAG]), dev("M", "replicant_matrix", None, stowed_in_device_code="MC")]
    pl = tp.plan(f, devs, [], {}, bps, P, TAG)
    assert pl["actions"] == []
    assert tp.plan(f, devs[:2], [], {}, bps, P, TAG)["actions"]   # an empty container may be sold


def test_supply_counts_only_unlisted_stock():
    e = {"designation": "tp:tp", "location": "SOL-4-L4", "star": "SOL", "_tagged_only": True, "_skip": ["D1"]}
    prog = {"best": {"devices": [{"device_type": "mining_drone", "need": 2}]}}
    devs = [dev("D1", "mining_drone", tags=[TAG]), dev("D2", "mining_drone", tags=[TAG]), dev("X", "mining_drone")]
    p = cs.plan(e, prog, {}, devs, hosts=set(), busy=set())
    assert p["rows"][0]["here"] == 1


def test_trade_post_home_piles_go_to_a_hub():
    from rsweb import logistics as lg
    fleets = [{"id": "tp", "role": "trade_post", "home": "ALPHA", "station": True},
              {"id": "hub", "role": "mining", "home": "SOL", "station": True, "materials": "self"}]
    reqs, _, _ = lg.requests(devices=[], bps={}, inventory={"ALPHA-4-L4": {"structural": 500}}, pos={}, contracts=[],
                             trades=[], incoming={}, fleets=fleets, gather={"SOL": "SOL-BELT-1"}, busy_stars=set())
    assert [(r["kind"], r["from"], r["to"]) for r in reqs] == [("return", "ALPHA-4-L4", "SOL-BELT-1")]


def test_engine_lists_and_card_saves(client):  # noqa: F811
    from test_ops import H
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "blueprints", list(BPS.values()))
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices")
                       + [shop(), dev("D1", "mining_drone", tags=[TAG]), dev("D2", "mining_drone", tags=[TAG])])
    client.portal.call(e.save_fleets, [dict(FLEET)])
    out = client.portal.call(e.trade_post_pass)
    assert out == ["Post: listed 2× mining_drone"]
    st = client.portal.call(db.kv_get, tp.KV)["tp"]
    assert st["listings"]["mining_drone"]["stock"] == 2 and st["listings"]["mining_drone"]["escrow"] == ["D1", "D2"]
    sup = client.portal.call(e.trade_post_supply, client.portal.call(e.devices))
    assert sup["tp:tp"]["_need"] == {} and sup["tp:tp"]["location"] == "SOL-4-L4"
    page = client.get("/fleets/details?all=1", headers=H).text
    assert "Trade post" in page and 'name="onhand"' in page and "205 structural" in page
    r = client.post("/fleets/tp/tradepost", data={"action": "pricing", "factor": "3", "pct_per_hour": "0"},
                    headers={**H, "HX-Request": "true"})
    assert "Saved" in r.text and client.portal.call(db.kv_get, tp.KV_PRICING) == {"factor": 3.0, "pct_per_hour": 0.0}
    types = [r["type"] for r in tp.rows({}, BPS, P)]
    data = {"action": "stock", "type": types, "onhand": ["3" if t == "mining_drone" else "" for t in types],
            "price": ["90 rares" if t == "mining_drone" else "" for t in types]}
    r = client.post("/fleets/tp/tradepost", data=data, headers={**H, "HX-Request": "true"})
    assert "1 device type(s) stocked, 1 override(s)" in r.text
    f = client.portal.call(e.fleets)[0]
    assert f["trade_post"] == {"onhand": {"mining_drone": 3}, "prices": {"mining_drone": {"rares": 90}}}
