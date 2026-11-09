from rsweb import loadouts as lo
from test_ops import H, HX, client  # noqa: F401  (fixture)


def test_delivery_tags_rules():
    d = {"device_code": "A", "location": "SOL-3", "tags": ["spare", "to:old", "at:old-1", "lift", "fleet:miners", "keep"]}
    add, rem, why = lo.delivery_tags(d, "falqueoryx-belt-1", {"fleet:miners": "SOL"})
    assert why is None and add == ["to:falqueoryx", "at:falqueoryx-belt-1"]
    assert rem == ["at:old-1", "fleet:miners", "lift", "spare", "to:old"]
    # same system, a spot: only the pin; the fleet stays when the spot is in its home
    add, rem, why = lo.delivery_tags(d, "SOL-BELT-1", {"fleet:miners": "SOL"})
    assert add == ["at:sol-belt-1"] and "fleet:miners" not in rem
    assert lo.delivery_tags(d, "SOL-3")[2] == "already at SOL-3"
    assert lo.delivery_tags({"location": "SOL-3", "tags": []}, "SOL")[2] == "already at SOL"
    assert "isn't a star" in lo.delivery_tags(d, "fleet:x")[2]
    assert "reserved" in lo.delivery_tags({"location": "SOL-3", "tags": ["contract:x"]}, "VEGA")[2]
    t = {"location": "SOL-3", "tags": ["to:vega"]}
    assert lo.delivery_tags(t, "VEGA")[2] == "already tagged for VEGA"
    assert lo.delivery_clear_tags({"tags": ["to:vega", "at:vega-1", "lift", "spare"]}) == ["at:vega-1", "lift", "to:vega"]


def test_deliver_bulk_and_device_page(client):
    db = client.app.state.db
    devs = client.portal.call(db.kv_get, "devices")
    d = next(x for x in devs if x.get("location") and not x.get("hosting_replicant") and "matrix" not in (x.get("device_type") or "")
             and "vessel" not in (x.get("device_type") or ""))
    code, here = d["device_code"], lo.star_of(d["location"])
    other = "ZZTOP" if here != "ZZTOP" else "ZZTIP"
    devs.append({"device_code": "FAR1", "device_type": "mining_drone", "location": f"{other}-BELT-1", "status": "idle", "tags": []})
    client.portal.call(db.kv_set, "devices", devs)
    page = client.get("/fleet", headers=H).text
    assert 'value="deliver"' in page
    assert 'name="destination"' in client.get("/fleet/bulk-form?command=deliver", headers=HX).text
    r = client.post("/fleet/bulk", data={"command": "deliver", "destination": f"{other}-3", "codes": [code]}, headers=HX).text
    assert f"tagged for delivery to {other}-3: 1 of 1" in r, r
    by = {x["device_code"]: x for x in client.portal.call(db.kv_get, "devices")}
    assert {lo.to_tag(other), lo.at_tag(f"{other}-3")} <= set(by[code]["tags"])
    assert "isn&#39;t a known star" in client.post(f"/devices/{code}/deliver", data={"destination": "NOWHEREX"}, headers=HX).text
    page = client.get(f"/devices/{code}", headers=H).text
    assert "Cancel delivery" in page and 'hx-target="this"' in page
    r = client.post(f"/devices/{code}/deliver", data={"clear": "1"}, headers=HX).text
    assert "delivery canceled: 1 of 1" in r
    by = {x["device_code"]: x for x in client.portal.call(db.kv_get, "devices")}
    assert not any(t.startswith(("to:", "at:")) for t in by[code].get("tags") or [])


def test_hand_delivery_is_planned_and_pinned_relay_not_unbound():
    def dev(code, t, loc, **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": kw.pop("status", "inactive"),
                "operational_capacity": 100.0, "features": kw.pop("features", ["cruise", "stow"]),
                "available_commands": ["travel", "stow", "deploy"], **kw}
    r1 = dev("R1", "ftl_relay", "HUB-1", tags=[])
    add, _, _ = lo.delivery_tags(r1, "AAA-2-L5")
    r1["tags"] = add
    devices = [dev("W1", "ftl_relay", "AAA-1-L4", status="relaying"), r1,
               dev("HV", "heaven_vessel", "HUB-1", features=["surge", "cruise"], stow_capacity=5,
                   hosting_replicant={"replicant_code": "X"}, tags=["fleet:logistics"])]
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in (("HUB", 0), ("AAA", 5))}
    cfg = {"phases": [], "systems": {}, "fleets": [{"id": "logistics", "name": "Logistics", "role": "logistics",
                                                    "home": "HUB", "wants": {}}]}
    p = lo.plan(cfg, devices, [], {}, stars, {"HV": "X"}, set(), [], {}, commandable={"HUB", "AAA"})
    assert "spare" not in p["tag_add"].get("R1", [])
    assert "R1" in {x for d in p["deliveries"] for x in d["devices"]}
