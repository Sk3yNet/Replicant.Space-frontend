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
