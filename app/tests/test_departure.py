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
