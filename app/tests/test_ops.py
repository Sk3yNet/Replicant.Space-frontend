"""Traffic & civilisation contact, asteroid defence, maintenance and the trade shop — against the mock game."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from rsweb import defence as dfn, traffic as tr, upkeep as up
from rsweb.config import Settings
from rsweb.main import create_app
from rsweb.mock import create_mock

H = {"X-Auth-Request-Email": "joe@example.com"}
HX = {**H, "HX-Request": "true"}
REP = "77F75255"


@pytest.fixture()
def client(tmp_path):
    mock = create_mock()
    s = Settings(api_token="dev", api_base="http://mock/v1", db_path=str(tmp_path / "app.sqlite"),
                 allowed_emails=["joe@example.com"], disable_background=True)
    app = create_app(s, transport=httpx.ASGITransport(app=mock))
    with TestClient(app) as c:
        w = app.state.worker

        async def sync():
            await w.sync_account(); await w.sync_devices(); await w.sync_inventory(); await w.sync_blueprints()
        c.portal.call(sync)
        yield c


def world(c):
    return c.app.state.api.http._transport.app.state.world


def eng(c):
    return c.app.state.worker.automations


def iso(dt):
    return dt.isoformat(timespec="seconds")


def add(c, **d):
    d.setdefault("replicant_code", REP)
    d.setdefault("operational_capacity", 100.0)
    world(c).devices.append(d)
    c.portal.call(c.app.state.worker.sync_devices)


# --- traffic ------------------------------------------------------------------------------------------------
def test_beacon_audit_baseline_then_visitor_alert(client):
    add(client, device_code="BCN00002", device_type="ftl_beacon", location="SOL-KUIPER", status="monitoring",
        features=["stow", "audit", "comms"], available_commands=["deploy", "stow"])
    now = datetime.now(timezone.utc)
    w = world(client)
    w.audit["BCN00002"] = [{"id": 1, "device_code": "2AC61210", "device_type": "mining_drone", "replicant_code": REP,
                            "travel_type": "arrival", "location": "SOL-BELT-1", "logged_at": iso(now - timedelta(hours=2)), "vector": None}]
    out = client.portal.call(eng(client).sync_traffic)
    assert out["beacons"] == 1 and out["new"] == 0 and not out["alerts"]      # first read = baseline
    w.profiles["F00D0001"] = {"name": "Riker", "replicant_code": "F00D0001", "is_npc": False}
    w.audit["BCN00002"].append({"id": 2, "device_code": "AAAA0001", "device_type": "surge_plate", "replicant_code": "F00D0001",
                                "travel_type": "arrival", "location": "SOL-5-L4", "logged_at": iso(now), "vector": "-0.3,0.1,-0.9"})
    out = client.portal.call(eng(client).sync_traffic)
    assert out["alerts"] == ["Visitor in SOL: Riker — 1× surge plate arrived"]
    assert "Visitor in SOL: Riker" in client.get("/notifications", headers=H).text
    out = client.portal.call(eng(client).sync_traffic)                         # nothing new, no repeat
    assert out["new"] == 0 and not out["alerts"]
    page = client.get("/traffic?others=1", headers=H).text
    assert "Riker" in page and "AAAA0001" in page and "2AC61210" not in page


def test_visitor_alert_respects_repeat_window_and_npcs():
    state = {}
    b = {"device_code": "B1", "location": "SOL-KUIPER"}
    tr.merge(state, b, [])
    new = tr.merge(state, b, [{"id": 5, "replicant_code": "NPC1", "travel_type": "arrival", "device_type": "x", "location": "SOL-3"}])
    assert tr.visitors(new, {"ME"}, include_npcs=False, profiles={"NPC1": {"is_npc": True}}) == {}
    assert list(tr.visitors(new, {"ME"}, include_npcs=True, profiles={})) == [("NPC1", "SOL")]


# --- civilisation contact ---------------------------------------------------------------------------------
def test_civ_coverage_needs_beacon_at_the_event_body(client):
    w = client.app.state.worker
    client.portal.call(w.handle_event, {"id": "e1", "event": "event.discovered", "created_at": iso(datetime.now(timezone.utc)),
                                        "payload": {"designation": "EV-1", "location": "SOL-3", "title": "Help the locals", "tier": 1}})
    client.portal.call(w.handle_event, {"id": "e2", "event": "event.completed", "created_at": iso(datetime.now(timezone.utc)),
                                        "payload": {"designation": "EV-1", "location": "SOL-3", "tier": 1}})
    add(client, device_code="BCN00002", device_type="ftl_beacon", location="SOL-KUIPER", status="monitoring",
        features=["stow", "audit"], available_commands=["deploy"])
    cov = client.portal.call(eng(client).civ_coverage)
    row = next(r for r in cov if r["location"] == "SOL-3")
    assert row["needs_beacon"] and row["system_beacons"][0]["code"] == "BCN00002"   # Kuiper beacon doesn't count
    page = client.get("/traffic", headers=H).text
    assert "needs a beacon" in page and "Place beacon" in page
    # the replicant's vessel (at SOL-BELT-1) carries beacon FB000001: placing it flies the vessel there and deploys it
    r = client.post("/traffic/beacon", data={"location": "SOL-3"}, headers=HX)
    assert "fly heaven vessel" in r.text and "deploy beacon FB000001" in r.text
    job = next(j for j in client.portal.call(eng(client).jobs) if j["rule"] == "civ_beacons")
    assert job["steps"][0]["body"] == {"command": "travel", "destination": "SOL-3"}
    assert job["steps"][1]["body"] == {"command": "deploy"} and job["steps"][1]["path"] == "/devices/FB000001"


def test_beacon_placement_kinds():
    reps = {"R1": {"hosted_device_code": "V1", "location": "X-3", "name": "Bob"}}
    vessel = {"device_code": "V1", "device_type": "heaven_vessel", "location": "X-3", "features": ["cruise", "print"]}
    beacon = {"device_code": "B1", "device_type": "ftl_beacon", "location": "X-3", "status": "stowed", "stowed_in_device_code": "V1"}
    assert tr.placement("X-3", [vessel, beacon], reps, {})["kind"] == "deploy"
    assert tr.placement("X-3", [vessel], reps, {})["kind"] == "print"
    steps = tr.placement_steps(tr.placement("X-3", [vessel], reps, {}), "X-3")
    assert steps[0]["path"] == "/replicants/R1/print" and steps[0]["body"] == {"device_type": "ftl_beacon"}
    assert tr.placement("Y-2", [vessel], reps, {})["kind"] == "none"


def test_printed_beacon_is_deployed_where_it_was_wanted(client):
    e = eng(client)
    client.portal.call(e.db.kv_set, "beacon_wanted", {"SOL-BELT-1": iso(datetime.now(timezone.utc))})
    client.portal.call(e.civ_on_event, {"event": "print.completed", "device_code": "HOSTX", "location": "SOL-BELT-1",
                                        "payload": {"device_type": "ftl_beacon", "new_device_code": "NEWB0001"}})
    job = next(j for j in client.portal.call(e.jobs) if j["rule"] == "civ_beacons")
    assert job["steps"][0]["path"] == "/devices/NEWB0001" and job["steps"][0]["body"] == {"command": "deploy"}


# --- asteroid defence ---------------------------------------------------------------------------------------
def test_asteroid_estimate_send_and_print(client):
    now = datetime.now(timezone.utc)
    w = world(client)
    w.objects["SOL-OBJ-3"] = {"designation": "SOL-OBJ-3", "object_type": "incoming_asteroid", "status": "active",
                              "impact_target": "SOL-3", "impact_eta": iso(now + timedelta(hours=20)), "impact_likelihood": 100.0,
                              "required_strength": 168.0, "active_propulsors": 0, "current_thrust_per_hour": 0.0,
                              "progress_pct": 0.0, "mass_class": "large"}
    add(client, device_code="PP000001", device_type="propulsor_plate", location="SOL-BELT-1", status="idle",
        features=["cruise", "divert", "stow"], available_commands=["travel", "activate", "stow"])
    client.portal.call(client.app.state.db.insert_event, {"id": "o1", "event": "system.object_detected", "created_at": iso(now),
                       "payload": {"object_designation": "SOL-OBJ-3", "impact_target": "SOL-3", "impact_eta": iso(now + timedelta(hours=20)),
                                   "size_class": "large"}})
    e = eng(client)

    async def run(**kw):
        async with e.lock:
            return await e.sync_objects(**kw)
    out = client.portal.call(run)
    assert out["read"] == 1
    rep = client.portal.call(e.defence_report)[0]
    assert rep["needed"] == 3 and rep["thrust_source"].startswith("assumed")
    jobs = [j for j in client.portal.call(e.jobs) if j["rule"] == "asteroid_defence"]
    assert [j["title"] for j in jobs] == ["defence: PP000001 → SOL-OBJ-3"]          # sent; printing is off by default
    assert any("printing is off" in x for x in out["lines"])
    # "Defend now" prints the rest (2), straight to the asteroid, pinned there
    r = client.post("/defence/SOL-OBJ-3/act", headers=HX)
    assert "print 2× propulsor_plate" in r.text
    pj = next(j for j in client.portal.call(e.jobs) if "print" in j["title"])
    body = pj["steps"][0]["body"]
    assert body["oncomplete"] == {"command": "travel", "destination": "SOL-OBJ-3"} and "at:sol-obj-3" in body["tags"]
    assert "~3" in client.get("/defence", headers=H).text


def test_asteroid_assess_uses_game_thrust_and_verdicts():
    now = datetime.now(timezone.utc)
    obj = {"designation": "D-OBJ-1", "impact_eta": iso(now + timedelta(hours=10)), "required_strength": 100, "progress_pct": 50,
           "active_propulsors": 2, "current_thrust_per_hour": 6.0, "impact_likelihood": 60, "status": "active"}
    a = dfn.assess(obj, [], {})
    assert a["thrust_each"] == 3.0 and a["needed"] == 2 and a["verdict"] == "on track"
    assert dfn.assess({**obj, "active_propulsors": 1, "current_thrust_per_hour": 3.0}, [], {})["verdict"] == "short by 1"
    assert dfn.assess({**obj, "impact_likelihood": 0}, [], {})["verdict"] == "diverted"
    assert dfn.assess({**obj, "impact_eta": iso(now - timedelta(minutes=5))}, [], {})["verdict"] == "too late"


# --- maintenance --------------------------------------------------------------------------------------------
def test_maintenance_sets_patrol_and_reports(client):
    add(client, device_code="MD000001", device_type="maintenance_drone", location="SOL-BELT-1", status="idle",
        features=["cruise", "repair", "ami", "stow"], available_commands=["set_directive", "clear_directive", "repair", "travel"])
    rep = up.report(client.portal.call(eng(client).devices))
    sol = next(r for r in rep if r["star"] == "SOL")
    assert sol["verdict"] == "drone not patrolling" and sol["damaged"][0]["code"] == "2AC61213"
    r = client.post("/maintenance/run", data={"star": "SOL"}, headers=HX)
    assert "MD000001 at SOL-BELT-1 set to patrol" in r.text
    assert world(client).devices[-1]["ami_directive"]["name"] == "patrol"
    client.portal.call(client.app.state.worker.sync_devices)
    sol = next(r for r in up.report(client.portal.call(eng(client).devices)) if r["star"] == "SOL")
    assert sol["verdict"] == "covered"
    page = client.get("/maintenance", headers=H).text
    assert "Repair now" in page and "patrolling" in page
    r = client.post("/maintenance/repair", data={"target": "2AC61213", "drone": "MD000001"}, headers=HX)
    assert "result ok" in r.text


def test_maintenance_skips_fleet_and_bound_drones():
    d = {"device_code": "M1", "device_type": "maintenance_drone", "location": "A-1", "status": "idle", "features": ["ami", "repair"],
         "tags": ["fleet:abc"]}
    assert up.to_patrol([d], set()) == []
    assert [x["device_code"] for x in up.to_patrol([{**d, "tags": []}], set())] == ["M1"]


# --- trade shop ---------------------------------------------------------------------------------------------
def test_shop_configure_add_remove_and_buy(client):
    add(client, device_code="TRD00001", device_type="ami_trade_controller", location="SOL-3-L4", status="idle",
        features=["cruise", "ami", "stow"], available_directives=["trade"],
        available_commands=["set_directive", "clear_directive", "travel"])
    page = client.get("/shop", headers=H).text
    assert "not set up" in page
    r = client.post("/shop/TRD00001/configure", data={"name": "Bob's Bits", "announcement": "Rares at SOL-3-L4"}, headers=HX)
    assert "result ok" in r.text
    page = client.get("/shop", headers=H).text
    assert "Bob&#39;s Bits" in page and "listed via BCN00001" in page        # the mock has a relay in SOL
    # 12 rares at the shop: 2 × 10 can't go in escrow
    r = client.post("/shop/TRD00001/trades", data={"name": "Rares for structural", "stock": "2", "cr_structural": "100", "rw_rares": "10"},
                    headers=HX)
    assert "Not enough to put in escrow" in r.text and "20 rares" in r.text
    r = client.post("/shop/TRD00001/trades", data={"name": "Rares for structural", "stock": "1", "cr_structural": "100", "rw_rares": "10"},
                    headers=HX)
    assert r.headers.get("HX-Refresh") == "true"
    page = client.get("/shop", headers=H).text
    assert "Rares for structural" in page and "100 structural" in page
    code = world(client).trades["TRD00001"][0]["trade_code"]
    client.portal.call(client.app.state.db.kv_set, "traders_cache",
                       {"TRD00001": {"shop_name": "Bob's Bits", "location": "SOL-3-L4", "owner_replicant_code": REP,
                                     "trades": world(client).trades["TRD00001"]}})
    r = client.post("/shop/buy", data={"controller": "TRD00001", "trade_code": code}, headers=HX)
    assert "result ok" in r.text
    r = client.post(f"/shop/TRD00001/trades/{code}/delete", headers=HX)
    assert r.headers.get("HX-Refresh") == "true" and world(client).trades["TRD00001"] == []


def test_event_discovered_by_survey_gets_a_beacon_printed_then_fetched(client):
    e = eng(client)
    s = client.portal.call(e.settings)
    s["rules"]["civ_beacons"]["enabled"] = True
    client.portal.call(e.save_settings, s)
    w = client.app.state.worker
    client.portal.call(w.handle_event, {"id": "d1", "event": "event.discovered", "created_at": iso(datetime.now(timezone.utc)),
                                        "payload": {"designation": "EV-9", "location": "SOL-3", "title": "First contact", "tier": 1}})
    # the only vessel hosts the replicant (off by default) → print one on SOL's autofactory, tagged civ
    job = next(j for j in client.portal.call(e.jobs) if j["rule"] == "civ_beacons")
    assert job["steps"][0]["body"] == {"command": "enqueue_print", "device_type": "ftl_beacon", "quantity": 1, "tags": ["civ"]}
    lines = client.portal.call(e.civ_beacon_pass, "SOL-3")
    assert "being printed" in lines[0]
    # the print is out and a cargo vessel is in the system: it picks the beacon up and takes it to SOL-3
    add(client, device_code="CV000001", device_type="cargo_vessel", location="SOL-BELT-1", status="idle",
        features=["surge", "cruise", "attach"], stow_capacity=50, available_commands=["travel"])
    add(client, device_code="NB000001", device_type="ftl_beacon", location="SOL-3-L4", status="monitoring", tags=["civ"],
        features=["stow", "audit"], available_commands=["stow", "deploy"])
    lines = client.portal.call(e.civ_beacon_pass, "SOL-3")
    assert "cargo vessel CV000001 picks up beacon NB000001 at SOL-3-L4" in lines[0]
    job = [j for j in client.portal.call(e.jobs) if j["rule"] == "civ_beacons"][-1]
    assert [st["body"].get("command") or "tags" for st in job["steps"]] == ["travel", "stow", "tags", "travel", "deploy"]
    assert job["steps"][1]["body"] == {"command": "stow", "target": "CV000001"}


def test_placement_prefers_a_carrying_vessel_and_spares_replicant_vessels():
    reps = {"R1": {"hosted_device_code": "HV", "location": "X-5", "name": "Bob"}}
    hv = {"device_code": "HV", "device_type": "heaven_vessel", "location": "X-5", "features": ["cruise", "print"]}
    cv = {"device_code": "CV", "device_type": "cargo_vessel", "location": "X-BELT-1", "features": ["cruise"], "stow_capacity": 50}
    b1 = {"device_code": "B1", "device_type": "ftl_beacon", "status": "stowed", "stowed_in_device_code": "HV", "location": "X-5"}
    b2 = {"device_code": "B2", "device_type": "ftl_beacon", "status": "stowed", "stowed_in_device_code": "CV", "location": "X-BELT-1"}
    p = tr.placement("X-3", [hv, cv, b1, b2], reps, {}, use_replicant_vessel=False)
    assert p["kind"] == "carry" and p["vessel"] == "CV"
    assert tr.placement("X-3", [hv, b1], reps, {}, use_replicant_vessel=False)["kind"] == "none"
    assert tr.placement("X-3", [hv, b1], reps, {}, use_replicant_vessel=True)["vessel"] == "HV"
    # a monitoring beacon at another event body is never taken
    loose = {"device_code": "B3", "device_type": "ftl_beacon", "location": "X-2", "status": "monitoring", "tags": ["spare"]}
    assert tr.placement("X-3", [cv, loose], {}, {}, keep={"X-2"})["kind"] == "none"
    assert tr.placement("X-3", [cv, loose], {}, {})["kind"] == "fetch"


# --- redundant beacons → spare → gathered at the depot -------------------------------------------------------
def test_redundant_beacons():
    rows = [{"location": "X-3", "open": [{"designation": "E"}], "completed": [], "life": None}]
    devs = [{"device_code": "K1", "device_type": "ftl_beacon", "location": "X-KUIPER", "status": "monitoring"},
            {"device_code": "C1", "device_type": "ftl_beacon", "location": "X-3", "status": "monitoring"},
            {"device_code": "Y1", "device_type": "ftl_beacon", "location": "Y-OORT", "status": "monitoring"},
            {"device_code": "Y2", "device_type": "ftl_beacon", "location": "Y-KUIPER", "status": "monitoring"},
            {"device_code": "Y3", "device_type": "ftl_beacon", "location": "Y-5", "status": "monitoring", "tags": ["spare"]},
            {"device_code": "Z1", "device_type": "ftl_beacon", "location": "Z-KUIPER", "status": "monitoring"}]
    red = {r["code"]: r["why"] for r in tr.redundant_beacons(devs, rows)}
    assert set(red) == {"K1", "Y2"}                      # Kuiper beacon next to a civ beacon; Y's second beacon; Z keeps its only one
    assert "civilisation" in red["K1"]


def test_spares_are_gathered_at_the_depot_and_stay_spare():
    from rsweb import loadouts as lo
    stars = {"DEP": {"position": {"x": 0, "y": 0, "z": 0}}, "OTH": {"position": {"x": 1, "y": 0, "z": 0}}}
    devices = [
        {"device_code": "AF1", "device_type": "autofactory", "location": "DEP-3-L4", "status": "idle",
         "available_commands": ["enqueue_print"], "features": ["print"]},
        {"device_code": "CV1", "device_type": "cargo_vessel", "location": "OTH-5", "status": "idle", "stow_capacity": 50,
         "features": ["surge", "cruise", "attach"], "available_commands": ["travel"]},
        {"device_code": "BK1", "device_type": "ftl_beacon", "location": "OTH-KUIPER", "status": "monitoring", "tags": ["spare"],
         "features": ["stow", "audit"], "available_commands": ["deploy", "stow", "decommission"]},
        {"device_code": "MD1", "device_type": "mining_drone", "location": "OTH-BELT-1", "status": "idle", "tags": ["spare"],
         "features": ["cruise", "mine", "stow"], "available_commands": ["travel", "stow", "start_mining"]},
    ]
    cfg = {"phases": [], "systems": {}, "roles": {"DEP": "destination"}}
    assert lo.spare_depot(lo.normalize(cfg), devices) == "DEP"
    p = lo.plan(cfg, devices, [], {}, stars, {}, set(), [], {})
    assert p["gathering"] == ["BK1", "MD1"] and p["depot"] == "DEP"
    dl = next(d for d in p["deliveries"] if d["carrier"] == "CV1")
    assert sorted(dl["devices"]) == ["BK1", "MD1"] and dl["mode"] == "stow"
    steps = lo.delivery_steps(dl, p["by_code"], stars, True, p["managed"], gathering=set(p["gathering"]))
    descs = [s["desc"] for s in steps]
    # the beacon can't fly: the vessel goes to it and takes it aboard; the drone flies to the vessel
    i_go = next(i for i, d in enumerate(descs) if "CV1 → OTH-KUIPER" in d)
    assert "stow BK1 in CV1" in descs[i_go + 1]
    assert not any(d.startswith("BK1 → ") for d in descs)
    tags_out = next(s for s in steps if s["path"] == "/devices/BK1" and s["method"] == "PATCH")
    assert tags_out["body"] == {"configuration": {"add_tags": ["to:dep", "gather"]}}           # stays spare on the way
    arrive = [s for s in steps if s["path"] == "/devices/BK1" and s["method"] == "PATCH"][-1]
    assert arrive["body"] == {"configuration": {"remove_tags": ["gather", "to:dep"]}}            # still spare, no home
    # the regular arrival handling keeps it spare too
    st = lo.arrived_steps("BK1", {"device_code": "BK1", "location": "DEP-5", "tags": ["spare", "to:dep", "gather"]}, {})
    assert st[-1]["body"] == {"configuration": {"remove_tags": ["gather", "to:dep"]}}
    assert any("gather 2 idle spare(s) at the depot DEP" in x for x in lo.describe(p))


def test_redundant_beacon_marked_spare_via_traffic_page(client):
    add(client, device_code="BCN00002", device_type="ftl_beacon", location="SOL-KUIPER", status="monitoring",
        features=["stow", "audit"], available_commands=["deploy", "stow"])
    add(client, device_code="BCN00003", device_type="ftl_beacon", location="SOL-OORT", status="monitoring",
        features=["stow", "audit"], available_commands=["deploy", "stow"])
    assert "Redundant beacons" in client.get("/traffic", headers=H).text
    r = client.post("/traffic/spare-redundant", headers=HX)
    assert "BCN00003 at SOL-OORT marked spare" in r.text
    assert "spare" in next(d for d in world(client).devices if d["device_code"] == "BCN00003")["tags"]


def test_existing_system_beacon_is_moved_to_the_civ_body_before_printing_one():
    cv = {"device_code": "CV", "device_type": "cargo_vessel", "location": "X-BELT-1", "status": "idle", "features": ["cruise"],
          "stow_capacity": 50, "available_commands": ["travel"]}
    kb = {"device_code": "KB", "device_type": "ftl_beacon", "location": "X-KUIPER", "status": "monitoring", "tags": ["home:x"],
          "available_commands": ["deploy", "stow"]}
    af = {"device_code": "AF", "device_type": "autofactory", "location": "X-3-L4", "available_commands": ["enqueue_print"]}
    p = tr.placement("X-3", [cv, kb, af], {}, {}, keep={"X-3"})
    assert p["kind"] == "fetch" and p["beacon"] == "KB" and p["beacon_at"] == "X-KUIPER"
    steps = tr.placement_steps(p, "X-3")
    assert [s["body"].get("command") or "tags" for s in steps] == ["travel", "stow", "tags", "travel", "deploy"]
    # the vessel is busy: wait for it rather than print a second beacon
    p = tr.placement("X-3", [cv, kb, af], {}, {}, busy={"CV"}, keep={"X-3"})
    assert p["kind"] == "wait" and "CV busy" in p["text"]
    # no vessel with a hold at all: say so (a printed beacon couldn't get there either)
    assert "needs a vessel with a hold" in tr.placement("X-3", [kb, af], {}, {}, keep={"X-3"})["text"]
    # a beacon already at another civilisation's body is never taken
    assert tr.placement("X-3", [cv, {**kb, "location": "X-2"}, af], {}, {}, keep={"X-3", "X-2"})["kind"] == "factory"
