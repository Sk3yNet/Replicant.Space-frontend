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
    sent, real = [], e.send

    async def spy(method, path, body, *a, **kw):
        sent.append(body)
        return await real(method, path, body, *a, **kw)
    e.send = spy
    out = client.portal.call(e.trade_post_pass)
    e.send = real
    assert out == ["Post: listed 2× mining_drone"]
    st = client.portal.call(db.kv_get, tp.KV)["tp"]
    assert st["numbers"] == {"SHOP": 1}
    assert any(b and (b.get("configuration") or {}).get("name") == "Post 1" for b in sent)
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


def test_shop_text_limits(client):  # noqa: F811
    from test_ops import H
    r = client.post("/shop/TRD00001/configure", data={"name": "Bits", "announcement": "x" * 81},
                    headers={**H, "HX-Request": "true"})
    assert "announcement is 81 characters, the game takes at most 80" in r.text
    from rsweb.commands import DIRECTIVES
    f = {x.name: x for x in DIRECTIVES["trade"]["trade"]}
    assert f["description"].maxlen == 240 and f["announcement"].maxlen == 80


def test_shop_spot_pins_the_controller_and_waits_for_it():
    f = {**FLEET, "trade_post": {**FLEET["trade_post"], "spot": "sol-5-l4"}}
    devs = [shop(), dev("D1", "mining_drone", "SOL-5-L4", [TAG])]
    pl = tp.plan(f, devs, [], {}, BPS, P, TAG)
    assert pl["location"] == "SOL-5-L4" and pl["pin"] == {"code": "SHOP", "add": ["at:sol-5-l4"], "remove": []}
    assert pl["actions"] == [] and pl["need"] == {"mining_drone": 2} and "on its way to SOL-5-L4" in pl["notes"][0]
    devs[0] = {**shop("SOL-5-L4"), "tags": ["fleet:tp", "at:sol-5-l4"]}
    pl = tp.plan(f, devs, [], {}, BPS, P, TAG)
    assert pl["pin"] is None and pl["actions"][0]["create"]["stock"] == 1


def test_card_spot_run_and_shop_link(client):  # noqa: F811
    from test_ops import H
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "blueprints", list(BPS.values()))
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices")
                       + [shop(), dev("D1", "mining_drone", tags=[TAG])])
    client.portal.call(e.save_fleets, [dict(FLEET)])
    hx = {**H, "HX-Request": "true"}
    assert "not in the home system" in client.post("/fleets/tp/tradepost", data={"action": "spot", "spot": "ALPHA-4-L4"}, headers=hx).text
    assert "pinned to SOL-4-L4" in client.post("/fleets/tp/tradepost", data={"action": "spot", "spot": "sol-4-l4"}, headers=hx).text
    assert client.portal.call(e.fleets)[0]["trade_post"]["spot"] == "SOL-4-L4"
    r = client.post("/fleets/tp/tradepost", data={"action": "run"}, headers=hx)
    assert "listed 1× mining_drone" in r.text
    assert client.portal.call(e.fleets)[0]["trade_post"]["onhand"] == {"mining_drone": 2}
    page = client.get("/shop", headers=H).text
    assert 'id="shop-SHOP"' in page and "Run by the trade post" in page
    assert "/shop#shop-SHOP" in client.get("/fleets/details?all=1", headers=H).text


def test_pretty_keeps_curly_quotes():
    from rsweb.web import f_pretty
    assert "Don’t" in f_pretty({"a": "Don’t"})


def test_plan_spreads_types_over_shops_five_each():
    bps = {f"t{i}": {"device_type": f"t{i}", "print_time": 0, "resources": {"structural": 10 * (i + 1)}} for i in range(7)}
    fleet = {"id": "tp", "name": "Post", "role": "trade_post", "home": "SOL",
             "trade_post": {"onhand": {f"t{i}": 1 for i in range(7)}}}
    devs = [shop()] + [dev(f"D{i}", f"t{i}", tags=[TAG]) for i in range(7)]
    pl = tp.plan(fleet, devs, {"SHOP": []}, {}, bps, P, TAG)
    assert tp.shops_needed(fleet) == 2
    assert sorted(a["type"] for a in pl["actions"]) == ["t2", "t3", "t4", "t5", "t6"]   # priciest first
    assert {a["controller"] for a in pl["actions"]} == {"SHOP"}
    assert "wait for a shop slot" in "; ".join(pl["notes"])
    devs.append(dev("SHOP2", "ami_trade_controller", tags=["fleet:tp"], ami_directive={"name": "trade"}))
    listed = [{"trade_code": f"TRD-{i}", "current_stock": 1, "initial_stock": 1, "rewards": {"devices": {f"t{i}": 1}},
               "criteria": {"resources": tp.formula_price({"structural": 10 * (i + 1)}, 0, P)}} for i in range(2, 7)]
    st = {"listings": {f"t{i}": {"trade_code": f"TRD-{i}", "escrow": [f"D{i}"], "controller": "SHOP"} for i in range(2, 7)}}
    pl = tp.plan(fleet, devs, {"SHOP": listed, "SHOP2": []}, st, bps, P, TAG)
    assert sorted((a["type"], a["controller"]) for a in pl["actions"]) == [("t0", "SHOP2"), ("t1", "SHOP2")]
    assert {s["code"]: len(s["types"]) for s in pl["shops"]} == {"SHOP": 5, "SHOP2": 2}


def test_engine_raises_wanted_controllers(client):  # noqa: F811
    e = client.app.state.worker.automations
    db = client.app.state.db
    bps = [{"device_type": f"t{i}", "print_time": 0, "resources": {"structural": 10}} for i in range(6)]
    client.portal.call(db.kv_set, "blueprints", bps)
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices") + [shop()])
    fleet = {**FLEET, "wants": {"ami_trade_controller": 1}, "trade_post": {"onhand": {f"t{i}": 1 for i in range(6)}}}
    client.portal.call(e.save_fleets, [fleet])
    out = client.portal.call(e.trade_post_pass)
    assert any("2 AMI trade controllers wanted" in x for x in out)
    f = client.portal.call(e.fleets)[0]
    assert f["wants"]["ami_trade_controller"] == 2


def test_spares_beat_queued_stock_prints():
    """Joe 2026-11-13: stock devices taken out of the fleet went spare while their stock was already queued to print.
    A free spare goes to the post, and the queued prints it (or an earlier order) covers come off the app's queue."""
    from datetime import datetime, timezone
    now = datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc).timestamp()
    e = {"designation": "tp:tp", "location": "SOL-4-L4", "star": "SOL", "_tagged_only": True, "_skip": []}
    prog = {"best": {"devices": [{"device_type": "compute_core", "need": 1}, {"device_type": "hull_plate", "need": 2}]}}
    orders = [{"type": "compute_core", "factory": "F1", "n": 1, "at": "2026-11-13T21:00:45+00:00"},
              {"type": "compute_core", "factory": "F1", "n": 1, "at": "2026-11-13T21:04:46+00:00"},
              {"type": "hull_plate", "factory": "F1", "n": 2, "at": "2026-11-13T21:05:00+00:00"}]
    devs = [dev("S1", "compute_core", tags=["spare"]), dev("X1", "hull_plate", tags=["fleet:other"])]
    devs[0]["location"] = "SOL-BELT-1"
    p = cs.plan(e, prog, {"orders": orders}, devs, hosts=set(), busy=set(), active=True, now=now)
    rows = {r["type"]: r for r in p["rows"]}
    assert rows["compute_core"]["sending"] == ["S1"]
    assert [(c["device_type"], c["n"], c["at"]) for c in p["cancel"]] == [
        ("compute_core", 1, "2026-11-13T21:04:46+00:00"), ("compute_core", 1, "2026-11-13T21:00:45+00:00")]
    assert rows["hull_plate"]["sending"] == [] and not rows["hull_plate"].get("cancel")   # no spare: its prints stay
    assert all(c["tag"] == cs.contract_tag("tp:tp") for c in p["cancel"])


def test_shops_numbered_and_kept():
    assert tp.number_shops({}, ["B", "A"]) == {"B": 1, "A": 2}
    assert tp.number_shops({"A": 2, "GONE": 1}, ["A", "C"]) == {"A": 2, "C": 1}
    assert tp.shop_name({"name": "SOL Market"}, 3) == "SOL Market 3"


def test_shop_page_one_card_per_trade_post(client):  # noqa: F811
    from test_ops import H
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices")
                       + [shop(), dev("SHOP2", "ami_trade_controller", tags=["fleet:tp"], ami_directive={"name": "trade"})])
    client.portal.call(e.save_fleets, [dict(FLEET)])
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices")
                       + [dev("LOOSE", "ami_trade_controller"), dev("SALE", "ami_trade_controller", "SOL-3", [TAG]),
                          dev("AWAY", "ami_trade_controller", "SOL-2")])
    page = client.get("/shop", headers=H).text
    assert 'id="shop-SHOP"' in page and 'id="shop-SHOP2"' in page and 'id="shop-LOOSE"' in page
    assert page.count("trade post · 4 shop(s)") == 1 and page.count('class="card"') - page.count("trade post ·") >= 0
    assert "not in the fleet" in page and "stock for sale" in page
    assert 'id="shop-AWAY"' in page and "no shop yet (1)" in page   # elsewhere, not set up: listed apart


def test_shop_texts_first_shop_announces():
    f = {**FLEET, "trade_post": {"description": "Devices of every kind.", "announcement": "Open at SOL-4-L4"}}
    assert tp.shop_config(f, 1) == {"name": "Post 1", "description": "Devices of every kind.", "announcement": "Open at SOL-4-L4"}
    assert tp.shop_config(f, 2) == {"name": "Post 2", "description": "More listings from Post 1."}
    assert tp.shop_config(f, 1, "SOL-4-L4") == {"name": "Post 1", "description": "At SOL-4-L4. Devices of every kind.",
                                                 "announcement": "Open at SOL-4-L4"}
    g = {**FLEET, "trade_post": {"announcement": "x" * 76 + " {spot}"}}
    assert tp.shop_config(g, 1, "SOL-4-L4")["announcement"] == ("x" * 76 + " SOL-4-L4")[:80]
    h = {**FLEET, "trade_post": {"announcement": "y" * 76}}
    assert tp.shop_config(h, 1, "SOL-4-L4") == {"name": "Post 1", "announcement": "y" * 76, "description": "At SOL-4-L4."}
    assert tp.shop_config(f, 2, "SOL-4-L4")["description"] == "More listings from Post 1 at SOL-4-L4."
    assert tp.config_differs({"name": "Post 2"}, tp.shop_config(f, 2))


def test_post_texts_sent_to_every_shop(client):  # noqa: F811
    from test_ops import H
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices")
                       + [shop(), dev("SHOP2", "ami_trade_controller", tags=["fleet:tp"], ami_directive={"name": "trade"})])
    client.portal.call(e.save_fleets, [dict(FLEET)])
    sent, real = [], e.send

    async def spy(method, path, body, *a, **kw):
        sent.append((path, body))
        return await real(method, path, body, *a, **kw)
    e.send = spy
    r = client.post("/fleets/tp/tradepost", data={"action": "texts", "description": "All sorts", "announcement": "Hi"},
                    headers={**H, "HX-Request": "true"})
    e.send = real
    assert "Saved" in r.text
    cfg = {p: b["configuration"] for p, b in sent}
    assert cfg["/devices/SHOP"] == {"name": "Post 1", "description": "At SOL-4-L4. All sorts", "announcement": "Hi @ SOL-4-L4"}
    assert cfg["/devices/SHOP2"] == {"name": "Post 2", "description": "More listings from Post 1 at SOL-4-L4."}
    page = client.get("/shop", headers=H).text
    assert "Update every shop" in page and "Open shop" not in page
