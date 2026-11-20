from rsweb import contractsupply as cs
from rsweb import megaorders as mo
from test_ops import H, HX, client  # noqa: F401  (fixture)

LOC = "DELTA-OBJ-2"   # the mock's wormhole gate: 256 precursor crystal seeds needed
TAG = cs.contract_tag(mo.key(LOC))


def dev(code, t="precursor_crystal_seed", loc="SOL-3", tags=None, **kw):
    return {"device_code": code, "device_type": t, "location": loc, "status": "idle", "tags": tags or [], **kw}


def test_want_caps_amount_by_what_the_structure_still_needs():
    o = {"types": {"a": 5, "b": 0, "c": 0}, "remaining": {"a": 3, "b": 7}, "given": {"a": 1}}
    assert mo.want(o) == {"a": 3, "b": 7}          # c: never read yet, not guessed
    o["remaining"] = {"a": 10, "b": 0}
    assert mo.want(o) == {"a": 4}                   # 5 asked, 1 given; b complete


def test_settle_counts_gone_devices_as_given_and_forgets_refused():
    o = {"pending": {"X": {"type": "a", "at": "2026-11-20T00:00:00+00:00"},
                     "Y": {"type": "a", "at": "2026-01-01T00:00:00+00:00"}}}
    mo.settle(o, [dev("Y", "a")], now=mo._ts("2026-11-20T00:05:00+00:00"))
    assert o["given"] == {"a": 1} and o["pending"] == {}   # X gone: given; Y still listed long after: refused


def test_contributes_only_tagged_idle_devices_at_the_location_up_to_the_need():
    o = {"location": LOC, "types": {"precursor_crystal_seed": 2}, "remaining": {"precursor_crystal_seed": 256}}
    devs = [dev("A", loc=LOC, tags=[TAG]), dev("B", loc=LOC, tags=[TAG]), dev("C", loc=LOC, tags=[TAG]),
            dev("D", loc=LOC), dev("E", tags=[TAG])]
    assert [d["device_code"] for d in mo.to_contribute(o, devs, TAG, busy={"A"})] == ["B", "C"]


def test_schedule_send_contribute_and_finish(client):  # noqa: F811
    eng, db = client.app.state.worker.automations, client.app.state.db
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices")
                       + [dev("S1", tags=["spare"]), dev("S2", tags=["fleet:x"])])
    r = client.post(f"/locations/{LOC}/deliveries", data={"bring": ["precursor_crystal_seed"], "n:precursor_crystal_seed": "2"},
                    headers=HX)
    assert "Saved" in r.text and "Scheduled delivery" in r.text
    o = client.portal.call(db.kv_get, mo.KV)[LOC]
    assert o["want"] == {"precursor_crystal_seed": 2} and o["title"] == "Wormhole Gate (Delta)" and not o["print"]
    client.portal.call(eng.mega_order_pass, True)
    client.portal.call(eng.contract_supply_pass, True)
    jobs = [j for j in client.portal.call(eng.jobs) if j["rule"] == "contracts"]
    assert len(jobs) == 1 and "S1" in jobs[0]["title"]          # the spare goes, the fleet's doesn't, nothing printed
    assert set(jobs[0]["steps"][0]["body"]["configuration"]["add_tags"]) == {"to:delta", "at:delta-obj-2", TAG}
    # S1 arrives: it is contributed
    devices = [d for d in client.portal.call(db.kv_get, "devices") if d["device_code"] != "S1"] + [dev("S1", loc=LOC, tags=[TAG])]
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(eng.mega_order_pass, True)
    cj = [j for j in client.portal.call(eng.jobs) if "contribute" in j["title"]]
    assert len(cj) == 1 and cj[0]["steps"][0]["path"] == f"/locations/{LOC}/contribute"
    assert cj[0]["steps"][0]["body"] == {"devices": ["S1"]}
    assert "S1" in client.portal.call(db.kv_get, mo.KV)[LOC]["pending"]
    # it leaves the list: one given, one still to bring; the page lists it
    client.portal.call(db.kv_set, "devices", [d for d in devices if d["device_code"] != "S1"])
    client.portal.call(eng.mega_order_pass, True)
    o = client.portal.call(db.kv_get, mo.KV)[LOC]
    assert o["given"] == {"precursor_crystal_seed": 1} and o["want"] == {"precursor_crystal_seed": 1} and o["status"] == "active"
    page = client.get("/megastructures", headers=H).text
    assert "Scheduled deliveries" in page and "Wormhole Gate (Delta)" in page
    # pause keeps it, remove drops it
    r = client.post(f"/megastructures/orders/{LOC}/pause", headers=HX)
    assert "paused" in r.text and client.portal.call(eng.mega_order_supply)[mo.key(LOC)]["_need"] == {}
    client.post(f"/megastructures/orders/{LOC}/remove", headers=HX)
    assert LOC not in client.portal.call(db.kv_get, mo.KV)


def test_order_finishes_when_nothing_is_left():
    o = {"types": {"a": 2}, "given": {"a": 2}, "remaining": {"a": 50}, "read_at": "x", "pending": {}}
    assert mo.finished(o)
    assert not mo.finished({**o, "read_at": None})


def test_location_page_offers_a_schedule(client):  # noqa: F811
    page = client.get(f"/locations/{LOC}", headers=H).text
    assert "Schedule a delivery" in page and f'hx-post="/locations/{LOC}/deliveries"' in page


def test_devices_go_to_an_owner_at_the_structure_first():
    reps = {"R1": {"location": "SOL-3"}, "R2": {"hosted_device_code": "V1"}}
    devs = [dev("V1", "racing_vessel", loc=LOC), dev("A", loc=LOC, replicant_code="R2"), dev("B", loc=LOC, replicant_code="R1")]
    present = mo.present_replicants(LOC, reps, devs)
    assert present == ["R2"]
    steps, why = mo.contribute_steps(LOC, devs[1:], present)
    assert why is None and [s["body"] for s in steps] == [{"command": "change_owner", "target": "R2"}, {"devices": ["A", "B"]}]
    assert steps[0]["path"] == "/devices/B"
    steps, why = mo.contribute_steps(LOC, devs[1:], [])
    assert len(steps) == 1 and "none of your replicants" in why
