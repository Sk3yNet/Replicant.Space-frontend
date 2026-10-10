from rsweb import fleets as fl
from test_ops import H, HX, client, eng  # noqa: F401  (fixture)


def dev(code, t, loc=None, **kw):
    return {"device_code": code, "device_type": t, "location": loc, "status": kw.pop("status", "idle"),
            "tags": kw.pop("tags", ["fleet:f"]), **kw}


def test_readiness_counts_only_what_is_in_the_home_system():
    f = {"id": "f", "home": "A", "station": True, "wants": {"mining_drone": 3, "surge_carrier": 1}}
    devs = [dev("C", "surge_carrier", "A-OORT"),
            dev("M1", "mining_drone", None, attached_to_device_code="C"),     # aboard the carrier in A
            dev("M2", "mining_drone", "A-BELT-1"),
            dev("M3", "mining_drone", "B-BELT-1")]                             # elsewhere: doesn't count
    r = fl.readiness(f, devs)
    assert not r["ready"] and r["short"] == {"mining_drone": 1} and r["elsewhere"] == {"mining_drone": ["M3"]}
    assert "1× mining drone (1 elsewhere)" in fl.readiness_text(r)
    devs[3]["location"] = "A-BELT-1"
    assert fl.readiness(f, devs)["ready"]


def _fleet(e, client, devs, **f):
    client.portal.call(client.app.state.db.kv_set, "devices", devs)
    base = {"id": "f", "name": "F", "role": "mining", "home": "SOL", "station": True,
            "wants": {"mining_drone": 2, "surge_carrier": 1}}
    client.portal.call(e.save_fleets, [{**base, **f}])


def test_new_home_waits_for_a_complete_loadout(client):
    e = eng(client)
    devs = [dev("C", "surge_carrier", "SOL-OORT", features=["surge"], attach_capacity=9),
            dev("M1", "mining_drone", "SOL-BELT-1")]
    _fleet(e, client, devs)
    items = client.portal.call(e.fleets)
    f = items[0]
    f["home"] = "ABOTEIN"
    how = client.portal.call(e.start_relocation, f, "SOL", "ABOTEIN", devs)
    assert "pending" in how and f["home"] == "SOL" and f["pending_home"] == "ABOTEIN" and not f.get("mission")
    client.portal.call(e.save_fleets, items)
    client.portal.call(e.pending_home_pass)
    assert client.portal.call(e.fleets)[0]["pending_home"] == "ABOTEIN"           # still short: stays
    devs.append(dev("M2", "mining_drone", "SOL-BELT-1"))
    client.portal.call(client.app.state.db.kv_set, "devices", devs)
    client.portal.call(e.pending_home_pass)
    f = client.portal.call(e.fleets)[0]
    assert f["home"] == "ABOTEIN" and not f.get("pending_home")
    # Move anyway
    _fleet(e, client, devs[:2], pending_home="ABOTEIN")
    client.post("/fleets/f/control", data={"action": "partial"}, headers=HX)
    client.portal.call(e.pending_home_pass)
    assert client.portal.call(e.fleets)[0]["home"] == "ABOTEIN"


def test_mission_waits_at_the_gate_until_complete_or_sent_anyway(client):
    e = eng(client)
    devs = [dev("C", "surge_carrier", "SOL-OORT"), dev("M1", "mining_drone", None, attached_to_device_code="C")]
    m = {"status": "running", "phase": "gather", "idx": 0, "targets": ["ABOTEIN"], "log": [], "opts": {}}
    _fleet(e, client, devs, mission=m)
    client.portal.call(e.run_fleets)
    m = client.portal.call(e.fleets)[0]["mission"]
    assert m["phase"] == "gather" and m["watch_note"].startswith("waiting to leave") and not m.get("departed")
    assert "Launch anyway" in client.get("/fleets", headers=H).text
    client.post("/fleets/f/control", data={"action": "partial"}, headers=HX)
    client.portal.call(e.run_fleets)
    m = client.portal.call(e.fleets)[0]["mission"]
    assert m.get("departed") and m["phase"] != "gather"


def test_add_devices_lists_other_fleets_members_grouped(client):
    e = eng(client)
    devs = [dev("M1", "mining_drone", "SOL-BELT-1", tags=["fleet:other"]), dev("M2", "mining_drone", "SOL-BELT-1", tags=[]),
            dev("M3", "mining_drone", "SOL-BELT-1", tags=["fleet:f"])]
    client.portal.call(client.app.state.db.kv_set, "devices", devs)
    client.portal.call(e.save_fleets, [{"id": "f", "name": "F", "role": "mining", "home": "SOL", "wants": {}},
                                       {"id": "other", "name": "Other", "role": "mining", "home": "SOL", "wants": {}}])
    page = client.get("/fleets", headers=H).text
    card = page[page.index('id="fleet-f"'):page.index('id="fleet-other"')]
    assert "in no fleet" in card and "in Other" in card
    assert card.index("in no fleet") < card.index("in Other")          # fleetless first
    assert 'name="add" value="M3"' not in card                        # its own members aren't offered
