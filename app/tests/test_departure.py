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


def test_new_home_moves_from_wherever_the_fleet_is_gathered(client):
    """Seen live 2026-10-11: a fleet back from a mission in LYRHYRAN's direction got a new home and waited for its
    loadout in FALQUORYX, its old home — all in one place anywhere is enough."""
    e = eng(client)
    devs = [dev("C", "surge_carrier", "LERNA-OORT", features=["surge"], attach_capacity=9),
            dev("M1", "mining_drone", None, attached_to_device_code="C"), dev("M2", "mining_drone", "LERNA-BELT-1")]
    r = fl.readiness_anywhere({"id": "f", "home": "SOL", "station": True, "wants": {"mining_drone": 2, "surge_carrier": 1}},
                              devs, prefer="SOL")
    assert r["ready"] and r["at"] == "LERNA"
    _fleet(e, client, devs)
    items = client.portal.call(e.fleets)
    f = items[0]
    f["home"] = "ABOTEIN"
    how = client.portal.call(e.start_relocation, f, "SOL", "ABOTEIN", devs)
    assert "pending" not in how and f["home"] == "ABOTEIN" and not f.get("pending_home")
    assert f["mission"]["from"] == "LERNA"


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


def test_mission_boards_everyone_before_it_leaves(client):
    """Seen live 2026-10-11: Surveyor 3's gather failed, a retry flew the empty vessel off — in the system isn't aboard."""
    e = eng(client)
    devs = [dev("C", "surge_carrier", "SOL-OORT", features=["surge"], attach_capacity=9),
            dev("M1", "mining_drone", None, attached_to_device_code="C"), dev("M2", "mining_drone", "SOL-BELT-1")]
    m = {"status": "running", "phase": "gather", "idx": 0, "targets": ["ABOTEIN"], "log": [], "opts": {}}
    _fleet(e, client, devs, mission=m)
    client.portal.call(e.run_fleets)
    m = client.portal.call(e.fleets)[0]["mission"]
    assert not m.get("departed") and "not aboard yet (M2)" in m["watch_note"] and m["phase"] == "assemble"
    assert any(j["title"] == "F: board before leaving" for j in client.portal.call(e.jobs))


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


def test_trip_eta_and_busy_errors():
    from rsweb import automations as au
    eta = au._trip_eta({"route": [{"time_seconds": 3600}, {"time_seconds": 18000}]})
    assert 21500 < (eta - au._now()).total_seconds() <= 21600
    assert au._trip_eta({"arrives_at": "2030-01-01T00:00:00Z"}).year == 2030
    assert au.BUSY_RE.search("Cannot deploy while replicant is travelling")
    assert au.BUSY_RE.search("Device is already in motion") and not au.BUSY_RE.search("Unknown star designation")


def test_long_trip_wait_and_busy_retry(client):
    from datetime import timedelta
    from rsweb import automations as au
    e = eng(client)
    db = client.app.state.db
    sent = []

    async def fake_send(method, path, body, label):
        sent.append((path, (body or {}).get("command")))
        if (body or {}).get("command") == "travel":
            return True, {"route": [{"time_seconds": 6 * 3600}]}, None
        if (body or {}).get("command") == "deploy" and len([x for x in sent if x[1] == "deploy"]) == 1:
            return False, None, "Cannot deploy while replicant is travelling"
        return True, {}, None
    e.send = fake_send
    steps = [au.step("V → FAR", "/devices/V1", {"command": "travel", "destination": "FAR"}),
             {**au.step("wait for V1 at FAR", "", None, method="WAIT", wait=["travel.arrived"]), "wait_device": "V1"},
             au.step("deploy D1", "/devices/D1", {"command": "deploy"})]
    job = client.portal.call(e.create_job, "fleets", "trip", "V1", steps, {}, True)
    job = next(j for j in client.portal.call(e.jobs) if j["id"] == job["id"])
    assert job["steps"][1]["status"] == "waiting" and job["steps"][1]["timeout"] > 6 * 3600   # waits out the ETA
    # past the timeout but the vessel is still surging: keep waiting
    client.portal.call(db.kv_set, "devices", [{"device_code": "V1", "status": "surging", "location": None, "unlisted": True}])
    jobs = client.portal.call(e.jobs)
    j = next(x for x in jobs if x["id"] == job["id"])
    j["steps"][1]["started_at"] = (au._now() - timedelta(hours=8)).isoformat()
    client.portal.call(e.save_jobs, jobs)
    client.portal.call(e.tick)
    j = next(x for x in client.portal.call(e.jobs) if x["id"] == job["id"])
    assert j["steps"][1]["status"] == "waiting" and j["steps"][1].get("extended") == 1
    # it arrives; the deploy is refused once (still travelling) and retried, not skipped
    client.portal.call(db.kv_set, "devices", [{"device_code": "V1", "status": "idle", "location": "FAR-OORT"}])
    j["steps"][1]["status"] = "done"
    j["idx"], j["status"] = 2, "running"
    jobs = [x if x["id"] != j["id"] else j for x in client.portal.call(e.jobs)]
    client.portal.call(e.save_jobs, jobs)
    client.portal.call(e._advance, j["id"])
    j = next(x for x in client.portal.call(e.jobs) if x["id"] == job["id"])
    assert j["steps"][2]["status"] == "retrying" and j["status"] == "waiting"
    j["steps"][2]["retry_at"] = au._now().isoformat()
    client.portal.call(e.save_jobs, [x if x["id"] != j["id"] else j for x in client.portal.call(e.jobs)])
    client.portal.call(e.tick)
    j = next(x for x in client.portal.call(e.jobs) if x["id"] == job["id"])
    assert j["steps"][2]["status"] == "done" and j["status"] == "done"


def _trip_job(client, e, extra_steps):
    from rsweb import automations as au

    async def fake_send(method, path, body, label):
        if (body or {}).get("command") == "travel":
            return True, {"arrives_at": (au._now()).isoformat()}, None
        if (body or {}).get("command") == "deploy":
            return False, None, "Cannot deploy while replicant is travelling"
        return True, {}, None
    e.send = fake_send
    steps = [au.step("V → FAR", "/devices/V1", {"command": "travel", "destination": "FAR"}),
             {**au.step("wait for V1 at FAR", "", None, method="WAIT", wait=["travel.arrived"], match={"destination": "FAR"}),
              "wait_device": "V1"}] + extra_steps
    job = client.portal.call(e.create_job, "fleets", "trip", "V1", steps, {}, True)
    return job["id"]


def test_canceled_trip_stops_the_job(client):
    from rsweb import automations as au
    e = eng(client)
    jid = _trip_job(client, e, [au.step("deploy D1", "/devices/D1", {"command": "deploy"})])
    client.portal.call(e.on_event, {"event": "travel.cancelled", "device_code": "V1", "payload": {"origin": "HOME", "return_time_seconds": 600}})
    j = next(x for x in client.portal.call(e.jobs) if x["id"] == jid)
    assert j["status"] == "failed" and "travel canceled" in j["steps"][1]["error"] and j["steps"][2]["status"] == "pending"


def test_timed_out_arrival_somewhere_else_stops_the_job(client):
    from datetime import timedelta
    from rsweb import automations as au
    e = eng(client)
    jid = _trip_job(client, e, [au.step("deploy D1", "/devices/D1", {"command": "deploy"})])
    client.portal.call(client.app.state.db.kv_set, "devices", [{"device_code": "V1", "status": "idle", "location": "HOME-OORT"}])
    jobs = client.portal.call(e.jobs)
    j = next(x for x in jobs if x["id"] == jid)
    j["steps"][1]["started_at"] = (au._now() - timedelta(hours=10)).isoformat()
    j["steps"][1]["timeout"] = 60
    client.portal.call(e.save_jobs, jobs)
    client.portal.call(e.tick)
    j = next(x for x in client.portal.call(e.jobs) if x["id"] == jid)
    assert j["status"] == "failed" and "didn't arrive at FAR" in j["steps"][1]["error"]


def test_busy_retry_stops_when_the_carrier_turned_back(client):
    from rsweb import automations as au
    e = eng(client)
    db = client.app.state.db
    client.portal.call(db.kv_set, "devices", [{"device_code": "V1", "status": "idle", "location": "FAR-OORT"},
                                              {"device_code": "D1", "status": "stowed", "stowed_in_device_code": "V1"}])
    jid = _trip_job(client, e, [au.step("deploy D1", "/devices/D1", {"command": "deploy"})])
    j = next(x for x in client.portal.call(e.jobs) if x["id"] == jid)
    j["steps"][1]["status"], j["idx"], j["status"] = "done", 2, "running"
    client.portal.call(e.save_jobs, [x if x["id"] != jid else j for x in client.portal.call(e.jobs)])
    client.portal.call(e._advance, jid)
    j = next(x for x in client.portal.call(e.jobs) if x["id"] == jid)
    assert j["steps"][2]["status"] == "retrying"
    # the vessel turned back home
    client.portal.call(db.kv_set, "devices", [{"device_code": "V1", "status": "travelling", "location": "FAR-OORT",
                                               "travel": {"destination": "HOME-OORT"}},
                                              {"device_code": "D1", "status": "stowed", "stowed_in_device_code": "V1"}])
    client.portal.call(e.tick)
    j = next(x for x in client.portal.call(e.jobs) if x["id"] == jid)
    assert j["status"] == "failed" and "heading to HOME, not FAR" in j["steps"][2]["error"]


def test_bulk_add_and_remove_tags(client):
    from rsweb import web_bulk as wb
    assert wb.clean_tags("Spare, to:SOL  bad*tag") == ["spare", "to:sol", "badtag"]
    db = client.app.state.db
    devs = client.portal.call(db.kv_get, "devices")
    codes = [d["device_code"] for d in devs[:2]]
    page = client.get("/fleet", headers=H).text
    assert "Device list" in page and 'value="add_tags"' in page
    assert "Tags to add" in client.get("/fleet/bulk-form?command=add_tags", headers=HX).text
    r = client.post("/fleet/bulk", data={"command": "add_tags", "tags": "keep, Spare", "codes": codes}, headers=HX)
    assert "added: 2 of 2" in r.text
    by = {d["device_code"]: d for d in client.portal.call(db.kv_get, "devices")}
    assert all({"keep", "spare"} <= set(by[c].get("tags") or []) for c in codes)
    r = client.post("/fleet/bulk", data={"command": "remove_tags", "tags": "keep", "codes": codes}, headers=HX)
    assert "removed: 2 of 2" in r.text
    by = {d["device_code"]: d for d in client.portal.call(db.kv_get, "devices")}
    assert all("keep" not in (by[c].get("tags") or []) and "spare" in by[c]["tags"] for c in codes)


def test_prospects_ward_column_add_and_remove(client):
    e = eng(client)
    db = client.app.state.db
    client.get("/systems/SOL", headers=H)   # a scanned system on the prospects list
    devs = client.portal.call(db.kv_get, "devices")
    devs.append({"device_code": "WD1", "device_type": "system_ward", "location": "SOL-3", "status": "inactive",
                 "available_commands": ["activate"], "tags": []})
    client.portal.call(db.kv_set, "devices", devs)
    page = client.get("/systems/prospects", headers=H).text
    assert "Prospects" in page and "add ward" in page and "WD1 idle" in page
    r = client.post("/systems/SOL/ward", data={"action": "add"}, headers=HX)
    assert "activating WD1" in r.text
    jobs = client.portal.call(e.jobs)
    assert any("activate ward WD1" in j["title"] for j in jobs)
    # once active: shown as yours, and remove deactivates it
    for d in devs:
        if d["device_code"] == "WD1":
            d["status"] = "warding"
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(e.ward_orders_pass)
    assert "SOL" not in (client.portal.call(db.kv_get, "ward_orders") or {})
    assert "⛨ yours" in client.get("/systems/prospects", headers=H).text
    r = client.post("/systems/SOL/ward", data={"action": "remove"}, headers=HX)
    assert "deactivating WD1" in r.text


def test_add_ward_sends_a_spare_or_prints(client):
    e = eng(client)
    db = client.app.state.db
    client.get("/systems/SOL", headers=H)
    devs = [d for d in client.portal.call(db.kv_get, "devices") if d.get("device_type") != "system_ward"]
    client.portal.call(db.kv_set, "devices", devs + [{"device_code": "WD2", "device_type": "system_ward", "location": "SOL-BELT-1",
                                                     "status": "stowed", "tags": ["spare"]}])
    msg = client.portal.call(e.order_ward, "ABOTEIN", "joe")
    assert "sending spare ward WD2" in msg
    job = next(j for j in client.portal.call(e.jobs) if "WD2 → ABOTEIN" in j["title"])
    assert job["steps"][0]["body"]["configuration"]["add_tags"] == ["to:abotein"]
    client.portal.call(db.kv_set, "devices", devs)
    msg = client.portal.call(e.order_ward, "LERNA", "joe")
    assert "print" in msg or "no spare ward and no autofactory" in msg


def test_ward_prints_spread_over_factories_and_skip_compacted_ones():
    from rsweb import ward_orders as wo
    pos = {"A": {"x": 0, "y": 0, "z": 0}, "B": {"x": 1, "y": 0, "z": 0}}
    f1 = {"device_code": "F1", "device_type": "autofactory", "location": "A-1", "tags": ["fleet:hub"],
          "print_queue": [{"device_type": "system_ward", "tags": ["to:b"]}]}
    f2 = {"device_code": "F2", "device_type": "autofactory", "location": "A-2", "tags": ["fleet:hub"]}
    f3 = {"device_code": "F3", "device_type": "autofactory", "location": "A-3", "status": "compacted"}
    assert wo.factory_near([f1, f2, f3], "B", set(), pos, 100)["device_code"] == "F2"
    assert wo.factory_near([f1, f2, f3], "B", set(), pos, 100, {"F2": 2})["device_code"] == "F1"
    f4 = {"device_code": "F4", "device_type": "autofactory", "location": "B-4", "print_queue": [{}, {}]}
    assert wo.factory_near([f1, f2, f4], "B", set(), pos, 100)["device_code"] == "F2"   # queue before distance
    assert wo.queued_at(f1, "B") == 0 and wo.queued_at(f2, "B") is None


def test_queued_ward_print_gives_way_to_a_spare(client):
    e = eng(client)
    db = client.app.state.db
    devs = [d for d in client.portal.call(db.kv_get, "devices") if d.get("device_type") not in ("system_ward", "autofactory")]
    fac = {"device_code": "FX1", "device_type": "autofactory", "location": "SOL-1", "status": "printing (system_ward)",
           "printing": {"device_type": "system_ward", "tags": ["to:lerna"]},
           "print_queue": [{"device_type": "system_ward", "tags": ["to:abotein"]}]}
    spare = {"device_code": "WD9", "device_type": "system_ward", "location": "SOL-BELT-1", "status": "inactive", "tags": []}
    client.portal.call(db.kv_set, "devices", devs + [fac, spare])
    client.portal.call(db.kv_set, "ward_orders", {"ABOTEIN": {"at": "2099-01-01T00:00:00+00:00", "how": "print", "factory": "FX1"},
                                                  "LERNA": {"at": "2099-01-01T00:00:00+00:00", "how": "print", "factory": "FX1"}})
    client.portal.call(e.ward_orders_pass)
    orders = client.portal.call(db.kv_get, "ward_orders")
    assert orders["ABOTEIN"]["how"] == "send" and orders["ABOTEIN"]["device"] == "WD9"
    assert orders["LERNA"]["how"] == "print"          # already printing: left alone
    job = next(j for j in client.portal.call(e.jobs) if "WD9 → ABOTEIN" in j["title"])
    assert job["steps"][0]["body"] == {"command": "dequeue_print", "index": 0}
    assert job["steps"][1]["body"]["configuration"]["add_tags"] == ["to:abotein"]


def test_to_tag_naming_a_fleet_means_its_home():
    from rsweb import loadouts as lo
    d = {"tags": ["to:sol-printing-hub"]}
    assert lo.bound_for(d, {"SOLARES"}, {"sol-printing-hub": "SOLARES"}) == "SOLARES"
    assert lo.bound_for({"tags": ["to:solares"]}, {"SOLARES"}) == "SOLARES"
    steps = lo.arrived_steps("X1", {"device_code": "X1", "location": "SOLARES-2", "tags": ["to:sol-printing-hub"]}, {})
    assert "to:sol-printing-hub" in steps[-1]["body"]["configuration"]["remove_tags"]


def test_removing_a_ward_order_drops_its_queued_print(client):
    e = eng(client)
    db = client.app.state.db
    devs = [d for d in client.portal.call(db.kv_get, "devices") if d.get("device_type") not in ("system_ward", "autofactory")]
    fac = {"device_code": "FX2", "device_type": "autofactory", "location": "SOL-1",
           "print_queue": [{"device_type": "system_ward", "tags": ["to:lerna"]}, {"device_type": "system_ward", "tags": ["to:abotein"]}]}
    client.portal.call(db.kv_set, "devices", devs + [fac])
    client.portal.call(db.kv_set, "ward_orders", {"ABOTEIN": {"at": "2099-01-01T00:00:00+00:00", "how": "print", "factory": "FX2"}})
    msg = client.portal.call(e.remove_ward, "ABOTEIN", "joe")
    assert "queued print on FX2 dropped" in msg
    job = next(j for j in client.portal.call(e.jobs) if "drop the queued ward for ABOTEIN" in j["title"])
    assert job["steps"][0]["body"] == {"command": "dequeue_print", "index": 1}


def test_systems_page_requests_relays_and_beacons_and_lists_fleets(client):
    e = eng(client)
    db = client.app.state.db
    devs = [d for d in client.portal.call(db.kv_get, "devices") if d.get("device_type") not in ("ftl_relay", "ftl_beacon")]
    client.portal.call(db.kv_set, "devices", devs + [
        {"device_code": "RL1", "device_type": "ftl_relay", "location": "SOL-BELT-1", "status": "stowed", "tags": ["spare"]},
        {"device_code": "BC1", "device_type": "ftl_beacon", "location": "SOL-2", "status": "monitoring", "tags": []}])
    client.portal.call(e.save_fleets, [{"id": "h", "name": "Homers", "role": "mining", "home": "SOL", "station": True, "wants": {}},
                                       {"id": "x", "name": "Scouts", "role": "explore", "home": "LERNA", "wants": {},
                                        "mission": {"status": "running", "targets": ["SOL-3"]}}])
    page = client.get("/systems?outposts=1", headers=H).text
    row = page[page.index('href="/systems/SOL"'):].split("</tr>")[0]
    assert "⌂ Homers" in row and "→ Scouts" in row
    assert '"kind": "relay"' in row and '"kind": "beacon"' in row
    r = client.post("/systems/ABOTEIN/ward", data={"action": "add", "kind": "relay"}, headers=HX)
    assert "sending spare FTL relay RL1 to ABOTEIN" in r.text
    assert client.portal.call(db.kv_get, "relay_requests")["ABOTEIN"]["device"] == "RL1"
    r = client.post("/systems/SOL/ward", data={"action": "remove", "kind": "beacon"}, headers=HX)
    assert "deactivating BC1 in SOL" in r.text
    r = client.post("/systems/ABOTEIN/ward", data={"action": "remove", "kind": "relay"}, headers=HX)
    assert "order canceled" in r.text and "ABOTEIN" not in client.portal.call(db.kv_get, "relay_requests")


def test_requested_beacon_goes_to_the_local_civilization(client):
    e = eng(client)
    db = client.app.state.db
    devs = [d for d in client.portal.call(db.kv_get, "devices") if d.get("device_type") != "ftl_beacon"]
    beacon = {"device_code": "BC7", "device_type": "ftl_beacon", "location": "LOR-1", "status": "monitoring", "tags": [],
              "available_commands": ["stow", "deploy"]}
    vessel = {"device_code": "CV7", "device_type": "cargo_vessel", "location": "LOR-1", "status": "idle", "features": ["cruise", "stow"],
              "stow_capacity": 4, "tags": []}
    client.portal.call(db.kv_set, "devices", devs + [beacon, vessel])
    client.portal.call(db.kv_set, "beacon_requests", {"LOR": {"at": "2099-01-01T00:00:00+00:00", "how": "send", "device": "BC7"}})

    async def cov():
        return [{"location": "LOR-3", "star": "LOR", "open": [{"designation": "E1"}], "completed": [], "life": None}]
    e.civ_coverage = cov
    client.portal.call(e.ward_orders_pass)
    job = next(j for j in client.portal.call(e.jobs) if j["title"].startswith("beacon to the civilization at LOR-3"))
    assert [s["desc"] for s in job["steps"]][-2:] == ["CV7: travel LOR-1 → LOR-3", "deploy beacon BC7 at LOR-3"]
    assert "LOR" in client.portal.call(db.kv_get, "beacon_requests")         # not done until it works at LOR-3
    page = client.get("/systems", headers=H).text
    assert "local civilization at LOR-3" in page
    beacon.update(location="LOR-3")
    client.portal.call(db.kv_set, "devices", devs + [beacon, vessel])
    client.portal.call(e.ward_orders_pass)
    assert "LOR" not in client.portal.call(db.kv_get, "beacon_requests")


def test_resumed_recall_does_not_wait_on_a_cleared_survey_controller(client):
    """Seen live 2026-10-11: Surveyors stalled in recall, was resumed, and sat in watch for hours — its controller was
    aboard with the directive cleared, so it never reported no_targets."""
    e = eng(client)
    devs = [dev("V", "cargo_vessel", "SOL-6-L4", features=["surge"], stow_capacity=20),
            dev("SC", "ami_survey_controller", None, status="stowed", stowed_in_device_code="V"),
            dev("D1", "survey_drone", None, status="stowed", stowed_in_device_code="V")]
    assert fl.watch_done({"role": "explore", "id": "f"}, {}, devs, "2026-10-11T00:00:00+00:00")[0]
    m = {"status": "stalled", "phase": "recall", "idx": 0, "targets": ["SOL"], "log": [], "opts": {}, "departed": True}
    _fleet(e, client, devs, role="explore", wants={"ami_survey_controller": 1, "cargo_vessel": 1, "survey_drone": 1}, mission=m)
    client.post("/fleets/f/control", data={"action": "resume"}, headers=HX)
    m = client.portal.call(e.fleets)[0]["mission"]
    assert m["phase"] != "watch" and m["status"] in ("running", "done")


def test_a_wait_for_an_arrival_ends_when_the_device_is_already_there(client):
    """Seen live 2026-10-11: Surveyors' assemble waited half an hour for its controller to land at SOLARES-BELT-1,
    where it had landed before the job began."""
    e = eng(client)
    client.portal.call(client.app.state.db.kv_set, "devices", [dev("SC", "ami_survey_controller", "SOL-BELT-1")])
    w = fl._wait_arrive("SC", "SOL-BELT-1")
    job = client.portal.call(e.create_job, "fleets", "F: assemble", None, [w], {"devices": ["SC"]}, True)
    assert client.portal.call(e._get, job["id"])["status"] == "done"


def test_member_aboard_another_fleets_vessel_in_flight_is_left_out_not_fatal():
    """Seen live 2026-10-11: a Surveyors drone rode off in Surveyor 2's vessel; assemble and recall kept failing
    'Cannot deploy while container is in transit'."""
    fleet = {"id": "f", "role": "explore", "home": "SOL", "wants": {}}
    devs = [dev("V", "cargo_vessel", "SOL-4-L4", features=["surge"], stow_capacity=20),
            dev("D1", "survey_drone", "SOL-4-L4", features=["stow"]),
            dev("D2", "survey_drone", None, status="stowed", stowed_in_device_code="H", features=["stow"]),
            dev("H", "heaven_vessel", None, status="travelling", tags=["fleet:other"],
                travel={"destination": "NOSOXA-OORT", "arrives_at": "2099-01-01T00:00:00+00:00"})]
    steps, problems = fl.assemble_steps(fleet, devs)
    assert not any("deploy out of H" in s["desc"] for s in steps) and any("D2 is aboard H" in p for p in problems)
    assert any(s["desc"] == "stow D1 in V" for s in steps)


def test_survey_crew_skips_a_surveyed_target_and_stays_aboard_at_home(client):
    e = eng(client)
    client.portal.call(client.app.state.db.kv_set, "ami_surveyed", {"LERNA": "2026-10-11T00:00:00+00:00"})
    devs = [dev("V", "cargo_vessel", "LERNA-4-L4", features=["surge"], stow_capacity=20),
            dev("D1", "survey_drone", None, status="stowed", stowed_in_device_code="V")]
    fleet = {"id": "f", "name": "F", "role": "explore", "home": "SOL", "station": True, "wants": {}}
    m = {"status": "running", "phase": "travel", "idx": 0, "targets": ["LERNA"], "log": [], "opts": {}}
    steps, _ = client.portal.call(e.fleet_phase_steps, fleet, m, "deploy", devs)
    assert steps == [] and "already surveyed" in m["log"][-1]["text"]
    steps, _ = client.portal.call(e.fleet_phase_steps, fleet, m, "work", devs)
    assert steps == []
    devs[0]["location"] = "SOL-4-L4"
    steps, _ = client.portal.call(e.fleet_phase_steps, fleet, m, "unload", devs)
    assert not any(s["desc"].startswith("deploy D1") for s in steps)


def test_no_automated_long_cruise_for_a_drone(client):
    """Seen live 2026-10-11: a survey drone at an Oort cloud was sent on a ~320 min cruise to the inner system."""
    from rsweb.automations import step, chain_step
    e = eng(client)
    client.portal.call(client.app.state.db.kv_set, "devices", [dev("D1", "survey_drone", "SOL-OORT")])
    st = step("D1 → SOL-3", "/devices/D1", {"command": "travel", "destination": "SOL-3"})
    job = client.portal.call(e.create_job, "reopen_sites", "send D1", None, [st], {"devices": ["D1"]}, True)
    s0 = client.portal.call(e._get, job["id"])["steps"][0]
    assert s0["status"] in ("skipped", "failed") and "would cruise" in s0["error"]
    assert chain_step("D1", "travel", "SOL-3")["allow_far"]     # one you ask for yourself still goes


def test_manual_long_cruise_asks_first(client):
    db = client.app.state.db
    devs = client.portal.call(db.kv_get, "devices") + [dev("D9", "survey_drone", "SOL-OORT", available_commands=["travel"])]
    client.portal.call(db.kv_set, "devices", devs)
    r = client.post("/devices/D9/command", data={"command": "travel", "f.destination": "SOL-3"}, headers=HX)
    assert "would cruise" in r.text and "Send anyway" in r.text and 'name="confirm_far"' in r.text
    r = client.post("/devices/D9/command", data={"command": "travel", "f.destination": "SOL-3", "confirm_far": "1"}, headers=HX)
    assert "would cruise" not in r.text
    r = client.post("/devices/D9/command", data={"command": "travel", "f.destination": "SOL-OORT"}, headers=HX)
    assert "would cruise" not in r.text


def test_manual_long_cruise_doesnt_ask_for_a_surge_capable_device(client):
    db = client.app.state.db
    devs = client.portal.call(db.kv_get, "devices") + [dev("V9", "cargo_vessel", "SOL-OORT", features=["surge", "stow"],
                                                            available_commands=["travel"])]
    client.portal.call(db.kv_set, "devices", devs)
    r = client.post("/devices/V9/command", data={"command": "travel", "f.destination": "SOL-3"}, headers=HX)
    assert "would cruise" not in r.text


def test_shops_from_a_system_scan_are_offered_as_trades(client):
    """Seen live 2026-10-11: Bill's Skunkworks at NOSOXA-OORT was in the system scan but not in Fleets' trade list."""
    import json
    from rsweb.db import now_iso
    db = client.app.state.db
    scan = {"shops": [{"controller_code": "3BF0ED09", "location": "NOSOXA-OORT", "owner_name": "Bill", "shop_name": "The Skunkworks",
                       "trades": [{"trade_code": "TRD-023B34", "name": "Service Bot Upgrade", "current_stock": 5,
                                   "criteria": {"devices": {"maintenance_drone": 1}}, "rewards": {"devices": {"service_bot": 1}}}]}]}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("NOSOXA", json.dumps(scan), now_iso()))
    e = eng(client)
    client.portal.call(e.save_fleets, [{"id": "t", "name": "T", "role": "trade", "home": "SOL", "wants": {}}])
    page = client.get("/fleets", headers=H).text
    assert "The Skunkworks @ NOSOXA-OORT — Service Bot Upgrade: pay 1× maintenance_drone" in page


def test_trade_paid_in_devices_gets_them_delivered(client):
    e = eng(client)
    db = client.app.state.db
    spare = dev("MD1", "maintenance_drone", "SOL-BELT-1", tags=["spare"])
    client.portal.call(db.kv_set, "devices", [spare])
    trade = {"controller": "3BF0ED09", "trade_code": "TRD-023B34", "name": "Service Bot Upgrade", "star": "SOL",
             "location": "SOL-OORT", "price": {}, "rewards": {}, "devices": {"maintenance_drone": 1}}
    m = {"status": "running", "phase": "wait", "idx": 0, "targets": ["SOL"], "log": [], "opts": {}, "trade": trade}
    client.portal.call(e.save_fleets, [{"id": "t", "name": "T", "role": "trade", "home": "SOL", "wants": {}, "mission": m}])
    client.portal.call(e.contract_supply_pass, True)
    job = next(j for j in client.portal.call(e.jobs) if "send MD1" in j["title"])
    tags = job["steps"][0]["body"]["configuration"]["add_tags"]
    assert "at:sol-oort" in tags and any(t.startswith("contract:trade:") for t in tags)
    assert client.portal.call(e._deal_devices_short, m, [spare]) == "1× maintenance drone"
    assert client.portal.call(e._deal_devices_short, m, [{**spare, "location": "SOL-OORT"}]) == ""


def test_bills_trail_can_be_picked_up_at_sol(client):
    """Bill always passes through SOL: with nothing to follow (or on request) the trail starts there."""
    from rsweb import trail as tl
    db = client.app.state.db
    client.portal.call(db.kv_set, tl.KV, {"target_name": "Bill", "target_code": tl.BILL_CODE, "beacons": {}, "audit": {}})
    assert "restart from SOL" in client.get("/trail", headers=H).text
    r = client.post("/trail/follow", data={"action": "start"}, headers=HX)
    assert "first stop SOL" in r.text
    fo = client.portal.call(db.kv_get, tl.KV)["follow"]
    assert fo["star"] == "SOL" and fo["active"] and fo["sol_tried"]
    s = client.portal.call(db.kv_get, tl.KV)                  # restart from SOL clears the old trail first
    s.update(beacons={"B1": {"location": "LERNA-OORT"}}, audit={"B1": [{"star": "LERNA"}]}, last_beacon="B1")
    client.portal.call(db.kv_set, tl.KV, s)
    assert "first stop SOL" in client.post("/trail/follow", data={"action": "sol"}, headers=HX).text
    s = client.portal.call(db.kv_get, tl.KV)
    assert s["beacons"] == {} and s["audit"] == {} and "last_beacon" not in s and s["follow"]["star"] == "SOL"


def test_newest_sighting_of_a_shop_wins_and_a_failed_trade_sets_it_aside(client):
    import json
    from rsweb import shop
    db = client.app.state.db
    for star, at in (("LERNA", "2026-10-10T00:00:00+00:00"), ("ABOTEIN", "2026-10-11T00:00:00+00:00")):
        scan = {"shops": [{"controller_code": "SK1", "location": f"{star}-OORT", "trades": []}]}
        client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                           (star, json.dumps(scan), at))
    assert client.portal.call(shop.known_traders, db)["SK1"]["location"] == "ABOTEIN-OORT"   # an old scan stays usable
    client.portal.call(shop.mark_gone, db, "SK1", "ABOTEIN-OORT", "2026-10-11T12:00:00+00:00")
    assert "SK1" not in client.portal.call(shop.known_traders, db)                            # until seen again


def test_gather_doesnt_fetch_spares_beyond_the_supply_range():
    """Seen live 2026-10-11: Surveyors' cargo vessel was sent from SOLARES across ~500 ly to FALQUORYX for one spare drone."""
    stars = {"SOL": {"position": {"x": 0, "y": 0, "z": 0}}, "FAR": {"position": {"x": 500, "y": 0, "z": 0}},
             "NEAR": {"position": {"x": 5, "y": 0, "z": 0}}}
    fleet = {"id": "f", "role": "explore", "home": "SOL", "station": True, "wants": {"survey_drone": 2, "cargo_vessel": 1}}
    devs = [dev("V", "cargo_vessel", "SOL-4-L4", features=["surge"], stow_capacity=20),
            dev("D1", "survey_drone", None, status="stowed", stowed_in_device_code="V"),
            dev("S1", "survey_drone", "FAR-BELT-1", tags=["spare"])]
    assert fl.gather_plan(fleet, devs, stars, set(), 100)["recruit"] == []
    devs.append(dev("S2", "survey_drone", "NEAR-BELT-1", tags=["spare"]))
    assert [d["device_code"] for d in fl.gather_plan(fleet, devs, stars, set(), 100)["recruit"]] == ["S2"]


def test_launch_anyway_still_boards_members_standing_by(client):
    e = eng(client)
    devs = [dev("C", "surge_carrier", "SOL-OORT", features=["surge"], attach_capacity=9),
            dev("M1", "mining_drone", None, attached_to_device_code="C"), dev("M2", "mining_drone", "SOL-OORT")]
    m = {"status": "running", "phase": "gather", "idx": 0, "targets": ["ABOTEIN"], "log": [], "opts": {"partial": True}}
    _fleet(e, client, devs, mission=m, wants={"mining_drone": 3, "surge_carrier": 1})
    client.portal.call(e.run_fleets)
    m = client.portal.call(e.fleets)[0]["mission"]
    assert not m.get("departed") and m["phase"] == "assemble"


def test_a_carried_device_hasnt_arrived_while_its_carrier_travels():
    """Seen live 2026-10-11: 9E13498F counted as arrived in SOLARES mid-surge; the detach was refused for 3 h."""
    from rsweb import loadouts as lo
    car = dev("C", "surge_carrier", "SOLARES-4-L4", status="travelling",
              travel={"destination": "SOLARES-4-L4", "arrives_at": "2099-01-01T00:00:00+00:00"})
    af = dev("AF", "autofactory", "SOLARES-4-L4", attached_to_device_code="C")
    assert lo.in_transit(af, {"C": car, "AF": af})
    car.update(status="idle", travel=None)
    assert not lo.in_transit(af, {"C": car, "AF": af})


def test_trips_log_their_expected_arrival():
    from datetime import timedelta
    from rsweb import automations as au
    t = au._now() + timedelta(hours=4, minutes=12, seconds=30)
    assert "UTC (in 4 h 12 min)" in au.eta_text(t)


def test_a_refusal_while_the_carrier_travels_waits_for_its_eta(client):
    from datetime import timedelta
    from rsweb import automations as au
    e = eng(client)
    eta = (au._now() + timedelta(hours=3)).isoformat(timespec="seconds")
    client.portal.call(client.app.state.db.kv_set, "devices", [
        dev("C", "surge_carrier", "SOL-4-L4", status="travelling", travel={"destination": "SOL-4-L4", "arrives_at": eta})])
    st = au.step("C: detach X", "/devices/C", {"command": "detach", "device": "X"})
    assert client.portal.call(e._moving_eta, st).isoformat(timespec="seconds") == eta


def test_detach_lists_the_carriers_attached_devices():
    from rsweb import commands as cmdspec
    f = cmdspec.COMMANDS["detach"][0]
    devs = [dev("MF", "mobile_fleet", "SOL-4-L4"), dev("A1", "autofactory", "SOL-4-L4", attached_to_device_code="MF"),
            dev("X1", "mining_drone", "SOL-4-L4")]
    opts = cmdspec.device_options({"devices": devs, "here": "SOL-4-L4"}, f, "MF")
    assert [o["value"] for o in opts] == ["A1"]


def test_stow_attach_and_detach_work_from_either_side(client):
    from rsweb import commands as cmdspec
    db = client.app.state.db
    devs = client.portal.call(db.kv_get, "devices") + [
        dev("CV", "cargo_vessel", "SOL-4-L4", stow_capacity=10, available_commands=["stow", "attach", "detach", "travel"]),
        dev("MF", "mobile_fleet", "SOL-4-L4", available_commands=["attach", "detach"]),
        dev("D7", "survey_drone", "SOL-4-L4", available_commands=["stow", "attach", "detach"]),
        dev("D8", "survey_drone", "SOL-4-L4", attached_to_device_code="MF", available_commands=["detach"])]
    client.portal.call(db.kv_set, "devices", devs)
    sugg = {"devices": devs, "here": "SOL-4-L4"}
    into, cargo = cmdspec.COMMANDS["stow"]
    assert "CV" in [o["value"] for o in cmdspec.device_options(sugg, into, "D7")]
    assert "D7" in [o["value"] for o in cmdspec.device_options(sugg, cargo, "CV")]
    sent = lambda: [(r["path"], r["body"]) for r in client.portal.call(db.fetchall, "SELECT path, body FROM actions ORDER BY id DESC LIMIT 1")]  # noqa: E731
    client.post("/devices/CV/command", data={"command": "stow", "f.cargo": "D7"}, headers=HX)
    assert sent()[0][0] == "/devices/D7" and '"target": "CV"' in sent()[0][1]
    client.post("/devices/D7/command", data={"command": "attach", "f.carrier": "MF"}, headers=HX)
    assert sent()[0][0] == "/devices/MF" and '"device": "D7"' in sent()[0][1]
    client.post("/devices/D8/command", data={"command": "detach"}, headers=HX)
    assert sent()[0][0] == "/devices/MF" and '"device": "D8"' in sent()[0][1]


def test_only_the_side_a_device_fits_is_shown_and_deploy_works_from_the_carrier(client):
    from rsweb import commands as cmdspec
    db = client.app.state.db
    devs = client.portal.call(db.kv_get, "devices") + [
        dev("CV", "cargo_vessel", "SOL-4-L4", stow_capacity=10, available_commands=["stow", "deploy", "travel"]),
        dev("D7", "survey_drone", "SOL-4-L4"), dev("D9", "survey_drone", None, status="stowed", stowed_in_device_code="CV")]
    client.portal.call(db.kv_set, "devices", devs)
    sugg = {"devices": devs, "here": "SOL-4-L4"}
    shown = lambda cmd, me: [f.name for f in cmdspec.COMMANDS[cmd] if cmdspec.field_applies(sugg, f, me)]  # noqa: E731
    assert shown("stow", "D7") == ["target"] and shown("stow", "CV") == ["cargo"]
    assert shown("deploy", "D7") == [] and shown("deploy", "CV") == ["cargo"]
    assert shown("attach", "D7") == ["carrier"] and shown("detach", "D7") == []
    assert [o["value"] for o in cmdspec.device_options(sugg, cmdspec.COMMANDS["deploy"][0], "CV")] == ["D9"]
    client.post("/devices/CV/command", data={"command": "deploy", "f.cargo": "D9"}, headers=HX)
    row = client.portal.call(db.fetchall, "SELECT path, body FROM actions ORDER BY id DESC LIMIT 1")[0]
    assert row["path"] == "/devices/D9" and '"deploy"' in row["body"]


def test_carrier_card_on_an_attach_carrier():
    from rsweb import carrier as cm
    mf = dev("MF", "mobile_fleet", "SOL-4-L4", features=["surge"])
    devs = [mf, dev("A1", "autofactory", "SOL-4-L4", attached_to_device_code="MF", status="attached"),
            dev("D1", "mining_drone", "SOL-4-L4"), dev("AF2", "autofactory", "SOL-4-L4", features=["modular"]),
            dev("V", "cargo_vessel", "SOL-4-L4")]
    assert cm.is_carrier(mf, [])
    rows = {r["code"]: r for r in cm.carrier_rows(mf, [], devs, can_hold=False, can_attach=True)}
    assert rows["A1"]["carried"] and rows["A1"]["mode"] == "attach"
    assert rows["D1"]["can_stow"] and rows["D1"]["mode"] == "attach"
    assert not rows["AF2"]["can_stow"] and "compact" in rows["AF2"]["note"]
    assert "V" not in rows                                         # a vessel isn't cargo
    steps = cm.plan("MF", "SOL-4-L4", list(rows.values()), {"A1"}, {"D1"}, set(), "/devices/MF")
    assert [(s["path"], s["body"]) for s in steps] == [("/devices/MF", {"command": "detach", "device": "A1"}),
                                                       ("/devices/MF", {"command": "attach", "device": "D1"})]


def test_bills_move_post_restarts_the_trail_where_he_left(client):
    from rsweb import shop, trail as tl
    e = eng(client)
    db = client.app.state.db
    cat = client.portal.call(db.kv_get, "stars") or {}
    cat.setdefault("stars", []).append({"designation": "NOSOXA", "position": {"x": 7, "y": -39, "z": 15}})
    client.portal.call(db.kv_set, "stars", cat)
    from datetime import datetime, timedelta, timezone
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
    old_row = {"replicant_code": tl.BILL_CODE, "travel_type": "departure", "logged_at": "2026-10-10T01:00:00+00:00"}
    new_row = {**old_row, "logged_at": fresh}
    client.portal.call(db.kv_set, tl.KV, {"target_name": "Bill", "target_code": tl.BILL_CODE, "scanner": "REP1",
                                          "beacons": {"B1": {"star": "SOL"}}, "audit": {"B1": [old_row, new_row]},
                                          "follow": {"active": False}})
    client.portal.call(db.kv_set, "traders_cache", {"3BF0ED09": {"location": "NOSOXA-OORT", "owner_name": "Bill",
                                                                 "owner_replicant_code": tl.BILL_CODE, "trades": []}})
    post = {"replicant_code": tl.BILL_CODE, "replicant_name": "Bill", "channel": "#general",
            "message": "Skunkworks is on the move. NOSOXA is in the rear-view mirror. Tell no one."}
    text = client.portal.call(e.bill_moved, post)
    assert "left NOSOXA" in text and "following him again from NOSOXA" in text
    s = client.portal.call(db.kv_get, tl.KV)
    assert s["follow"]["active"] and s["follow"]["star"] == "NOSOXA" and s["moved"]["from"] == "NOSOXA"
    assert s["audit"]["B1"] == [new_row] and "B1" in s["beacons"]           # the old route is dropped, the beacon kept
    assert "3BF0ED09" not in client.portal.call(shop.known_traders, db)        # set aside until seen again
    assert client.portal.call(e.bill_moved, {**post, "message": "o7 all"}) is None
    assert client.portal.call(e.bill_moved, {**post, "replicant_code": "X", "replicant_name": "Ann"}) is None


def test_contracts_page_lists_known_shops_and_trades(client):
    import json
    from rsweb.db import now_iso
    db = client.app.state.db
    scan = {"shops": [{"controller_code": "3BF0ED09", "location": "NOSOXA-OORT", "owner_name": "Bill", "shop_name": "The Skunkworks",
                       "trades": [{"trade_code": "TRD-023B34", "name": "Service Bot Upgrade", "current_stock": 4,
                                   "criteria": {"devices": {"maintenance_drone": 1}}, "rewards": {"devices": {"service_bot": 1}}}]}]}
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("NOSOXA", json.dumps(scan), now_iso()))
    page = client.get("/game-events", headers=H).text
    assert "Trades" in page and "The Skunkworks" in page and "Service Bot Upgrade" in page and "NOSOXA-OORT" in page
    assert "The Skunkworks" in client.get("/shop", headers=H).text


def test_a_new_device_types_directives_come_from_the_game_and_are_remembered(client):
    db = client.app.state.db
    world = client.app.state.api.http._transport.app.state.world
    bot = {"device_code": "SB0001", "device_type": "service_bot", "location": "SOL-4-L4", "status": "idle",
           "features": ["ami"], "available_commands": ["set_directive", "clear_directive"],
           "available_directives": ["repair_nearby", "escort"], "replicant_code": "77F75255"}
    world.devices.append(bot)
    devs = client.portal.call(db.kv_get, "devices") + [{k: v for k, v in bot.items() if k != "available_directives"}]
    client.portal.call(db.kv_set, "devices", devs)
    r = client.get("/devices/SB0001/command-form?command=set_directive", headers=HX)
    assert "repair_nearby" in r.text and "escort" in r.text
    assert client.portal.call(db.kv_get, "known_directives")["service_bot"] == ["repair_nearby", "escort"]


def test_relay_networks_links_and_bridges():
    from rsweb import relaynet as rn
    P = lambda x: {"x": x, "y": 0, "z": 0}  # noqa: E731
    pos = {"A": P(0), "B": P(7), "C": P(28), "D": P(37), "E": P(14), "F": P(21), "H": P(80), "FAR": P(500)}
    devs = [dev("R1", "ftl_relay", "A-4-L4", status="relaying"), dev("R2", "ftl_relay", "B-4-L4", status="relaying"),
            dev("R3", "ftl_relay", "C-4-L4", status="relaying"), dev("H1", "system_hub", "D-4-L4", status="active"),
            dev("R4", "ftl_relay", "FAR-4-L4", status="relaying"), dev("R5", "ftl_relay", "H-4-L4", status="inactive")]
    net = rn.networks(devs, pos)
    assert [n["stars"] for n in net["networks"]] == [["A", "B"], ["C", "D"], ["FAR"]]
    assert {(l["a"], l["b"]) for l in net["links"]} == {("A", "B"), ("C", "D")}
    br = rn.bridges(net, pos)
    # A–B and C–D (C–D 9 ly apart: the hub's 15 ly links them) are joined by new relays at E (14) and F (21)
    assert br and (br[0]["a"], br[0]["b"]) == (0, 1) and br[0]["stars"] in (["E", "F"], ["E"]) and br[0]["n"] <= 2
    assert not any(1 in (b["a"], b["b"]) and 2 in (b["a"], b["b"]) for b in br)   # FAR is beyond 10 relays


def test_relays_page_and_route(client):
    db = client.app.state.db
    cat = client.portal.call(db.kv_get, "stars") or {"stars": []}
    cat["stars"] += [{"designation": d, "position": {"x": x, "y": 0, "z": 0}} for d, x in
                     (("RA", 1000), ("RB", 1007), ("RE", 1014), ("RF", 1021), ("RC", 1028))]
    client.portal.call(db.kv_set, "stars", cat)
    devs = client.portal.call(db.kv_get, "devices") + [
        dev("Q1", "ftl_relay", "RA-4-L4", status="relaying"), dev("Q2", "ftl_relay", "RC-4-L4", status="relaying")]
    client.portal.call(db.kv_set, "devices", devs)
    page = client.get("/map/relays", headers=H).text
    assert "Relay network" in page and "Add route" in page
    data = client.get("/api/map.json?part=overlay", headers=H).json()
    rl = data["relay_net"]
    assert {n["star"] for n in rl["nodes"]} >= {"RA", "RC"}
    assert not any(n["active"] for n in rl["nodes"] if n["star"] in ("RA", "RC"))   # no replicant there: inactive
    assert any(b["stars"] and {b["from"], b["to"]} == {"RA", "RC"} for b in rl["suggested"])
    assert "no replicant" in page
    reps = client.portal.call(db.kv_get, "replicants") or {}
    reps["RQ"] = {"location": "RA-4-L4"}
    client.portal.call(db.kv_set, "replicants", reps)
    client.portal.call(db.kv_set, "relay_requests", {"RE": {"star": "RE"}})
    rl = client.get("/api/map.json?part=overlay", headers=H).json()["relay_net"]
    assert next(n for n in rl["nodes"] if n["star"] == "RA")["active"]
    assert not next(n for n in rl["nodes"] if n["star"] == "RC")["active"]
    assert "RE" in rl["planned"]
    assert 'id="opt-relaynet" checked' not in client.get("/map", headers=H).text


def test_deliveries_outside_an_active_network_ride_a_replicant_vessel():
    from rsweb import loadouts as lo, relaynet as rn
    pos = {"A": {"x": 0, "y": 0, "z": 0}, "B": {"x": 5, "y": 0, "z": 0}, "Z": {"x": 50, "y": 0, "z": 0}}
    devs = [dev("R1", "ftl_relay", "A-4-L4", status="relaying")]
    net = rn.networks(devs, pos)
    assert rn.coverage(net, pos, set())["commandable"] == set() and not net["networks"][0]["active"]   # no replicant: inactive
    cov = rn.coverage(net, pos, {"A"})
    assert {"A", "B"} <= cov["commandable"] and "Z" not in cov["commandable"] and net["networks"][0]["active"]
    from test_core import _lo_world
    cfg, devices, bps, inv, stars = _lo_world()
    p_open = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    p_shut = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {}, commandable=set())
    carried = lambda p: {c for dl in p["deliveries"] for c in dl["devices"]}  # noqa: E731
    assert all(dl["carrier"] == "HV" for dl in p_shut["deliveries"])   # only the replicant's vessel may go
    assert carried(p_shut) <= carried(p_open)
