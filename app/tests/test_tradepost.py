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
    devs = [shop(), dev("D1", "mining_drone", tags=[TAG]), dev("X", "mining_drone", tags=["fleet:other"])]
    pl = tp.plan(FLEET, devs, [], {}, BPS, P, TAG)
    assert pl["actions"] == [] and "escrow could take" in pl["rows"][1]["status"] + pl["rows"][2]["status"]
    # an idle untagged one beside the stock goes into the escrow with it (2026-11-17)
    devs[2] = dev("X", "mining_drone", tags=["spare"])
    a = tp.plan(FLEET, devs, [], {}, BPS, P, TAG)["actions"]
    assert a[0]["create"]["stock"] == 2 and a[0]["escrow"] == ["D1", "X"]
    devs[2] = dev("X", "mining_drone", status="mining")
    assert tp.plan(FLEET, devs, [], {}, BPS, P, TAG)["actions"] == []


def test_plan_never_sells_the_posts_own_controller_type():
    f = {**FLEET, "trade_post": {"onhand": {"ami_trade_controller": 1}}}
    bps = {**BPS, "ami_trade_controller": {"device_type": "ami_trade_controller", "print_time": 60, "resources": {"structural": 5}}}
    devs = [shop(), dev("T1", "ami_trade_controller", tags=[TAG])]
    pl = tp.plan(f, devs, [], {}, bps, P, TAG)
    row = next(r for r in pl["rows"] if r["type"] == "ami_trade_controller")
    assert pl["actions"] == [] and "can't be sold here" in row["status"] and pl["release"] == ["T1"]
    assert "ami_trade_controller" not in pl["need"]


def test_supply_drops_released_devices_and_repins_strays():
    e = {"designation": "tp:tp", "location": "SOL-4-L4", "star": "SOL", "_tagged_only": True}
    prog = {"best": {"devices": [{"device_type": "mining_drone", "need": 1}]}}
    entry = {"printed": ["S1", "P1", "P2", "C1"]}
    devs = [dev("S1", "mining_drone", tags=["spare"]),            # printed for it once, released since
            dev("P1", "mining_drone", tags=[TAG]),                # still tagged, pinned nowhere
            dev("P2", "mining_drone", tags=[TAG]),
            dev("C1", "mining_drone", tags=[TAG, "component", "at:sol-3"])]   # a part for a print
    for d in devs:
        d["location"] = "SOL-3"
    p = cs.plan(e, prog, entry, devs, hosts=set(), busy=set(), active=True)
    assert [d["device_code"] for d in p["send"]] == ["P1"] and [d["device_code"] for d in p["release"]] == ["P2"]
    assert p["rows"][0]["coming"] == ["P1"] and "C1" not in p["printed"]
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


def test_plan_spreads_types_over_shops_ten_each():
    bps = {f"t{i}": {"device_type": f"t{i}", "print_time": 0, "resources": {"structural": 10 * (i + 1)}} for i in range(12)}
    fleet = {"id": "tp", "name": "Post", "role": "trade_post", "home": "SOL",
             "trade_post": {"onhand": {f"t{i}": 1 for i in range(12)}}}
    devs = [shop()] + [dev(f"D{i}", f"t{i}", tags=[TAG]) for i in range(12)]
    pl = tp.plan(fleet, devs, {"SHOP": []}, {}, bps, P, TAG)
    assert tp.shops_needed(fleet) == 2
    assert sorted(a["type"] for a in pl["actions"]) == sorted(f"t{i}" for i in range(2, 12))   # priciest first
    assert {a["controller"] for a in pl["actions"]} == {"SHOP"}
    assert "wait for a shop slot" in "; ".join(pl["notes"])
    devs.append(dev("SHOP2", "ami_trade_controller", tags=["fleet:tp"], ami_directive={"name": "trade"}))
    listed = [{"trade_code": f"TRD-{i}", "current_stock": 1, "initial_stock": 1, "rewards": {"devices": {f"t{i}": 1}},
               "criteria": {"resources": tp.formula_price({"structural": 10 * (i + 1)}, 0, P)}} for i in range(2, 12)]
    st = {"listings": {f"t{i}": {"trade_code": f"TRD-{i}", "escrow": [f"D{i}"], "controller": "SHOP"} for i in range(2, 12)}}
    pl = tp.plan(fleet, devs, {"SHOP": listed, "SHOP2": []}, st, bps, P, TAG)
    assert sorted((a["type"], a["controller"]) for a in pl["actions"]) == [("t0", "SHOP2"), ("t1", "SHOP2")]
    assert {s["code"]: len(s["types"]) for s in pl["shops"]} == {"SHOP": 10, "SHOP2": 2}


def test_engine_raises_wanted_controllers(client):  # noqa: F811
    e = client.app.state.worker.automations
    db = client.app.state.db
    bps = [{"device_type": f"t{i}", "print_time": 0, "resources": {"structural": 10}} for i in range(11)]
    client.portal.call(db.kv_set, "blueprints", bps)
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices") + [shop()])
    fleet = {**FLEET, "wants": {"ami_trade_controller": 1}, "trade_post": {"onhand": {f"t{i}": 1 for i in range(11)}}}
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


def test_controller_that_left_the_post_is_unpinned():
    # owner 2026-11-15: B3F801E9 kept at:<spot> after leaving the fleet and showed as an unnamed shop
    f = {**FLEET, "trade_post": {**FLEET["trade_post"], "spot": "sol-5-l4"}}
    devs = [{**shop("SOL-5-L4"), "tags": ["fleet:tp", "at:sol-5-l4"]},
            dev("OLD", "ami_trade_controller", "SOL-4-L4", ["at:sol-5-l4", "spare"]),
            dev("STOCK", "ami_trade_controller", "SOL-5-L4", ["at:sol-5-l4", TAG])]
    assert tp.plan(f, devs, [], {}, BPS, P, TAG)["unpin"] == ["OLD"]


def _trade(n, devices, stock=1):
    return {"name": n, "trade_code": n, "current_stock": stock, "initial_stock": 2, "criteria": {}, "rewards": {"devices": devices}}


def test_roll_call_lists_every_shop_with_its_controller_code():
    # owner 2026-11-15: one replicant announces every shop with its trade controller's code
    from rsweb import shop as sh
    cache = {"SHOP": {"trades": [_trade("a", {"mining_drone": 1}), _trade("b", [{"device_type": "ftl_relay"}])]},
             "SHOP2": {"trades": [_trade("c", {"survey_drone": 1}), _trade("gone", {"hauler": 1}, stock=0)]},
             "EMPTY": {"trades": [_trade("d", {"hauler": 1}, stock=0)]}}
    devs = [shop(), dev("SHOP2", "ami_trade_controller", "ALGOL-OORT", ami_directive={"name": "trade"}),
            dev("EMPTY", "ami_trade_controller", ami_directive={"name": "trade"}),
            dev("NOSHOP", "ami_trade_controller")]
    text = sh.roll_call(sh.shops(devs, {}, {**cache, "NOSHOP": cache["SHOP"]}), "Shops:")
    assert text == "Shops: ALGOL-OORT: SHOP2. SOL-4-L4: SHOP."
    assert sh.roll_call(sh.shops(devs[2:], {}, cache)) == ""
    assert sh.roll_call(sh.shops(devs, {}, cache), "Find us at SOL-4-L4, by controller:") == \
        "Find us at SOL-4-L4, by controller: ALGOL-OORT: SHOP2. SHOP."   # a location the opening names isn't repeated
    assert sh.rollcall_settings({"channel": "trade", "hours": "x"})["channel"] == "#trade"


def test_roll_call_posted_by_hand_and_on_schedule(client):  # noqa: F811
    from test_chatter import _world
    from test_ops import HX, REP
    from rsweb import shop as sh
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices") + [shop()])
    client.portal.call(db.kv_set, "shop_trades", {"SHOP": {"trades": [_trade("a", {"mining_drone": 1})], "at": "2099-01-01T00:00:00+00:00"}})
    assert "SOL-4-L4: SHOP." in client.get("/shop", headers=HX).text
    r = client.post("/shop/rollcall", data={"action": "post", "by": "", "channel": "#trade", "hours": "", "intro": "Hi", "text": ""},
                    headers=HX)
    assert "choose who posts it" in r.text
    r = client.post("/shop/rollcall", data={"action": "post", "by": REP, "channel": "#trade", "hours": "24", "intro": "Hi", "text": ""},
                    headers=HX)
    assert "Posted" in r.text
    assert _world(client).bobnet_posts[-1] == {"code": REP, "channel": "#trade", "text": "Hi SOL-4-L4: SHOP."}
    n = len(_world(client).bobnet_posts)
    assert client.portal.call(e.shop_rollcall_pass) == []   # posted under 24 h ago
    raw = client.portal.call(db.kv_get, sh.ROLLCALL_KV)
    raw["last"]["at"] = "2020-01-01T00:00:00+00:00"
    client.portal.call(db.kv_set, sh.ROLLCALL_KV, raw)
    assert client.portal.call(e.shop_rollcall_pass) == ["shop roll call posted on #trade"]
    assert len(_world(client).bobnet_posts) == n + 1


def test_roll_call_through_a_relay_shows_no_location(client):  # noqa: F811
    # Joe 2026-11-15: POST /devices/{relay} {"command": "message"} posts with no poster's star
    from test_chatter import _world
    from test_ops import HX
    db = client.app.state.db
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices")
                       + [shop(), dev("RLY1", "ftl_relay", "SOL-4-L4", status="active")])
    client.portal.call(db.kv_set, "shop_trades", {"SHOP": {"trades": [_trade("a", {"mining_drone": 1})], "at": "2099-01-01T00:00:00+00:00"}})
    _world(client).devices.append(dev("RLY1", "ftl_relay", "SOL-4-L4", status="active", available_commands=[]))
    page = client.get("/shop", headers=HX).text
    assert 'value="relay:RLY1"' in page
    r = client.post("/shop/rollcall", data={"action": "post", "by": "relay:RLY1", "channel": "#trade", "hours": "", "intro": "Hi",
                                            "text": ""}, headers=HX)
    assert "Posted" in r.text
    assert _world(client).bobnet_posts[-1] == {"code": "RLY1", "channel": "#trade", "text": "Hi SOL-4-L4: SHOP."}


def test_roll_call_defaults_to_the_relay_at_the_trade_post(client):  # noqa: F811
    # Joe 2026-11-16: the relay at the trade post announces, unless another poster is chosen
    from test_chatter import _world
    from test_ops import HX
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices")
                       + [shop(), dev("RLY2", "ftl_relay", "SOL-5-L4", status="active"),
                          dev("RLY1", "ftl_relay", "SOL-4-L4", status="active")])
    client.portal.call(e.save_fleets, [{**FLEET, "trade_post": {**FLEET["trade_post"], "spot": "SOL-4-L4"}}])
    assert client.portal.call(e.shop_rollcall_default) == "relay:RLY1"
    client.portal.call(db.kv_set, "shop_trades", {"SHOP": {"trades": [_trade("a", {"mining_drone": 1})], "at": "2099-01-01T00:00:00+00:00"}})
    _world(client).devices.append(dev("RLY1", "ftl_relay", "SOL-4-L4", status="active", available_commands=[]))
    page = client.get("/shop", headers=HX).text
    assert "the relay at the trade post (RLY1)" in page and "from a replicant's location" in page
    r = client.post("/shop/rollcall", data={"action": "post", "by": "", "channel": "#trade", "hours": "", "intro": "Hi",
                                            "text": ""}, headers=HX)
    assert "Posted" in r.text and _world(client).bobnet_posts[-1]["code"] == "RLY1"


def test_supply_old_sold_prints_dont_answer_a_new_order():
    e = {"designation": "tp:tp", "location": "SOL-4-L4", "star": "SOL", "_tagged_only": True}
    prog = {"best": {"devices": [{"device_type": "mining_drone", "need": 1}]}}
    entry = {"printed": ["OLD"], "printed_types": {"OLD": "mining_drone"}, "printed_seen": {"OLD": "2026-11-15T00:00:00+00:00"},
             "orders": [{"type": "mining_drone", "factory": "AF", "n": 1, "at": "2026-11-16T00:00:00+00:00"}]}
    now = cs._ts("2026-11-16T01:00:00+00:00")
    p = cs.plan(e, prog, entry, [], hosts=set(), busy=set(), now=now)
    assert p["rows"][0]["printing"] == 1 and p["rows"][0]["short"] == 0   # OLD was sold before the order: still printing
    entry["printed_seen"]["OLD"] = "2026-11-16T00:30:00+00:00"            # came out of this order, then went
    assert cs.plan(e, prog, entry, [], hosts=set(), busy=set(), now=now)["rows"][0]["printing"] == 0


def test_claimed_overage_is_delisted_then_handed_over():
    f = {**FLEET, "trade_post": {"onhand": {"mining_drone": 1}}}
    st = {"rows": [{"type": "mining_drone", "listed": 3, "trade_code": "TRD-1"}], "listings": {"mining_drone": {
        "trade_code": "TRD-1", "controller": "SHOP", "stock": 3, "escrow": ["E1", "E2", "E3"]}}}
    assert tp.overage(f, st) == {"mining_drone": 2}
    st["claims"] = {"mining_drone": [{"fleet": "m1", "n": 1, "at": "2026-11-17T00:00:00+00:00"}]}
    assert tp.overage(f, st) == {"mining_drone": 1} and tp.claimed(st) == {"m1": {"mining_drone": 1}}
    listing = {"trade_code": "TRD-1", "current_stock": 3, "initial_stock": 3, "criteria": {"resources": {}}}
    trades = {"SHOP": [{**listing, "rewards": {"devices": {"mining_drone": 1}}}]}
    pl = tp.plan(f, [shop()], trades, st, BPS, P, TAG)
    assert pl["actions"] == [{"type": "mining_drone", "controller": "SHOP", "delete": "TRD-1", "create": None, "escrow": []}]
    st["listings"] = {}
    devs = [shop()] + [dev(c, "mining_drone", tags=[TAG]) for c in ("E1", "E2", "E3")]   # the escrow came back
    pl = tp.plan(f, devs, {"SHOP": []}, st, BPS, P, TAG)
    assert pl["give"] == [{"code": "E1", "fleet": "m1", "type": "mining_drone", "tags": [TAG]}]
    assert pl["claims_done"] == ["mining_drone"] and pl["actions"] == []


def _three_shops():
    """12 types listed 5 / 5 / 2 over three shops (the old 5-listing cap): two shops of 10 are enough."""
    bps = {f"t{i}": {"device_type": f"t{i}", "print_time": 0, "resources": {"structural": 10 * (i + 1)}} for i in range(12)}
    fleet = {"id": "tp", "name": "Post", "role": "trade_post", "home": "SOL",
             "trade_post": {"onhand": {f"t{i}": 1 for i in range(12)}}}
    codes = ["SHOP", "SHOP2", "SHOP3"]
    devs = [dev(c, "ami_trade_controller", tags=["fleet:tp", "at:sol-4-l4"], ami_directive={"name": "trade"}) for c in codes]

    def lst(i):
        return {"trade_code": f"TRD-{i}", "current_stock": 1, "initial_stock": 1, "rewards": {"devices": {f"t{i}": 1}},
                "criteria": {"resources": tp.formula_price({"structural": 10 * (i + 1)}, 0, P)}}
    where = {"SHOP": range(0, 5), "SHOP2": range(5, 10), "SHOP3": range(10, 12)}
    trades = {c: [lst(i) for i in where[c]] for c in codes}
    st = {"numbers": {"SHOP": 1, "SHOP2": 2, "SHOP3": 3},
          "listings": {f"t{i}": {"trade_code": f"TRD-{i}", "escrow": [], "controller": c} for c in codes for i in where[c]}}
    return fleet, devs, trades, st, bps


def test_consolidation_moves_listings_to_the_lowest_numbered_shops():
    """Owner 2026-11-22: controllers hold 10 listings now, so the old 5-per-shop spread is packed into fewer shops."""
    fleet, devs, trades, st, bps = _three_shops()
    pl = tp.plan(fleet, devs, trades, st, bps, P, TAG)
    assert pl["actions"] == [] and not pl["drop"]   # nothing moves unless asked
    st["consolidate"] = {"at": "2026-11-22T00:00:00+00:00"}
    pl = tp.plan(fleet, devs, trades, st, bps, P, TAG)
    moves = sorted((a["type"], a["from"], a["controller"], a["delete"]) for a in pl["actions"])
    assert moves == [("t10", "SHOP3", "SHOP", "TRD-10"), ("t11", "SHOP3", "SHOP", "TRD-11")]
    assert all(a["create"]["stock"] == 1 for a in pl["actions"])
    assert pl["drop"] == []   # SHOP3 still lists until the moves are read back
    trades = {"SHOP": trades["SHOP"] + trades["SHOP3"], "SHOP2": trades["SHOP2"], "SHOP3": []}
    pl = tp.plan(fleet, devs, trades, st, bps, P, TAG)
    assert pl["actions"] == [] and pl["drop"] == ["SHOP3"] and pl["consolidating"]["left"] == []


def test_consolidation_closes_emptied_shops_and_lowers_wants(client):  # noqa: F811
    from test_ops import H
    db, e = client.app.state.db, client.app.state.worker.automations
    fleet, devs, trades, st, bps = _three_shops()
    client.portal.call(db.kv_set, "blueprints", list(bps.values()))
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices") + devs)
    client.portal.call(e.save_fleets, [{**fleet, "wants": {"ami_trade_controller": 3}, "trade_post": {
        **fleet["trade_post"], "spot": "SOL-4-L4"}}])
    trades["SHOP"] += trades.pop("SHOP3")
    trades["SHOP3"] = []
    client.portal.call(db.kv_set, tp.KV, {"tp": st})
    sent = []

    async def fake_get(path, *a, **k):
        return {"trades": trades[path.split("/")[2]]}

    async def fake_send(method, path, body, why, *a, **k):
        sent.append((method, path, body))
        return True, {}, None
    e.api.get, e.send = fake_get, fake_send
    r = client.post("/fleets/tp/tradepost", data={"action": "consolidate"}, headers={**H, "HX-Request": "true"})
    assert "Consolidating into 2 shop(s)" in r.text
    assert client.portal.call(e.fleets)[0]["wants"]["ami_trade_controller"] == 3   # lowered only once it is over
    job = next(j for j in client.portal.call(e.jobs) if "emptied shop" in (j.get("title") or ""))
    bodies = [s.get("body") for s in job["steps"]]
    assert {"command": "clear_directive"} in bodies and "SHOP3" in job["title"]
    assert not any(m == "DELETE" for m, _, _ in sent)
    devs2 = client.portal.call(db.kv_get, "devices")
    for d in devs2:
        if d.get("device_code") == "SHOP3":
            d["tags"], d["ami_directive"] = ["spare"], None
    client.portal.call(db.kv_set, "devices", devs2)
    out = client.portal.call(e.trade_post_pass, True)
    assert any("consolidated into 2 shop(s), 1 controller(s) freed" in x for x in out)
    st = client.portal.call(db.kv_get, tp.KV)["tp"]
    assert "consolidate" not in st and st["consolidated"]["freed"] == ["SHOP3"]
    assert client.portal.call(e.fleets)[0]["wants"]["ami_trade_controller"] == 2
    assert "Consolidate shops" in client.get("/fleets/details?all=1", headers=H).text


def test_shop_that_left_while_listing_rejoins(client):  # noqa: F811
    """Live 2026-11-22: Loadouts let go of 6 shops that still listed when consolidation lowered the wanted count, so
    their types looked unlisted and stock was printed again. Such a shop rejoins and consolidation carries on."""
    db, e = client.app.state.db, client.app.state.worker.automations
    fleet, devs, trades, st, bps = _three_shops()
    devs[2]["tags"] = ["spare"]   # SHOP3 left the fleet with its 2 listings
    client.portal.call(db.kv_set, "blueprints", list(bps.values()))
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices") + devs)
    client.portal.call(e.save_fleets, [{**fleet, "wants": {"ami_trade_controller": 2}, "trade_post": {
        **fleet["trade_post"], "spot": "SOL-4-L4"}}])
    client.portal.call(db.kv_set, tp.KV, {"tp": st})
    client.portal.call(db.kv_set, "shop_trades", {"SHOP3": {"trades": trades["SHOP3"], "at": "2026-11-22T00:00:00+00:00"}})

    async def fake_get(path, *a, **k):
        return {"trades": trades[path.split("/")[2]]}

    async def fake_send(method, path, body, why, *a, **k):
        return True, {}, None
    e.api.get, e.send = fake_get, fake_send
    out = client.portal.call(e.trade_post_pass, True)
    assert any("1 shop(s) that still list rejoined" in x for x in out)
    assert client.portal.call(e.fleets)[0]["wants"]["ami_trade_controller"] == 3
    job = next(j for j in client.portal.call(e.jobs) if "rejoin" in (j.get("title") or ""))
    assert job["steps"][0]["body"]["configuration"]["add_tags"] == ["fleet:tp", "at:sol-4-l4"]
    assert client.portal.call(db.kv_get, tp.KV)["tp"]["consolidate"]["by"] == "rejoin"


def test_plan_never_lists_a_vessel_with_cargo_and_delists_one_escrowed():
    # live 2026-11-24: relays bound for ULGRELIIL and CAELOARAX sat for days in a racing vessel and a heaven vessel on sale
    devs = [shop(), dev("D1", "mining_drone", tags=[TAG]),
            {"device_code": "R1", "device_type": "ftl_relay", "status": "stowed", "tags": ["to:ulgreliil"],
             "stowed_in_device_code": "D1"}]
    assert tp.cargo("D1", devs) == ["R1"]
    pl = tp.plan(FLEET, devs, [], {}, BPS, P, TAG)
    assert pl["actions"] == [] and pl["unload"] == ["R1"]
    assert "R1 aboard" in next(r for r in pl["rows"] if r["type"] == "mining_drone")["status"]
    listed = [{"trade_code": "TRD-1", "current_stock": 1, "initial_stock": 1, "rewards": {"devices": {"mining_drone": 1}},
               "criteria": {"resources": {}}}]
    st = {"listings": {"mining_drone": {"trade_code": "TRD-1", "escrow": ["D1"]}}}
    devs = [shop(), devs[2]]   # in escrow: gone from the list, its cargo still names it
    pl = tp.plan(FLEET, devs, listed, st, BPS, P, TAG)
    assert pl["actions"] == [{"type": "mining_drone", "controller": "SHOP", "delete": "TRD-1", "create": None, "escrow": []}]


def test_duplicate_listing_of_a_type_is_taken_down():
    """Owner 2026-11-25: mining drone, propulsor, system ward and FTL beacon were each listed on two shops (left over
    from the 1.529.0 consolidation). The listing the app remembers stays; the other is deleted, the remembered one kept."""
    fleet = {**FLEET, "trade_post": {"onhand": {"mining_drone": 1}}}
    devs = [shop(), dev("SHOP2", "ami_trade_controller", tags=["fleet:tp"], ami_directive={"name": "trade"})]
    price = tp.formula_price({"structural": 100, "conductive": 50}, 1800, P)
    def lst(code):
        return [{"trade_code": code, "current_stock": 1, "initial_stock": 1, "rewards": {"devices": {"mining_drone": 1}},
                 "criteria": {"resources": price}}]
    st = {"listings": {"mining_drone": {"trade_code": "TRD-NEW", "escrow": ["D1"], "controller": "SHOP2"}}}
    pl = tp.plan(fleet, devs, {"SHOP": lst("TRD-OLD"), "SHOP2": lst("TRD-NEW")}, st, BPS, P, TAG)
    assert [(a["controller"], a["delete"], a["create"], a.get("duplicate")) for a in pl["actions"]] == \
        [("SHOP", "TRD-OLD", None, True)]
    assert "duplicate" in "; ".join(pl["notes"])
