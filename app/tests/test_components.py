from rsweb import components as comp
from test_ops import client  # noqa: F401  (fixture)

BPS = [{"device_type": "biosphere_cultivator", "resources": {"carbon": 10}, "print_time": 60,
        "components": {"hydroponic_bay": 2, "orbital_farm": 1}},
       {"device_type": "hydroponic_bay", "resources": {"carbon": 5, "volatiles": 2}, "print_time": 60},
       {"device_type": "orbital_farm", "resources": {"structural": 7}, "print_time": 60}]


def test_total_resources_include_the_components():
    bps = {b["device_type"]: b for b in BPS}
    assert comp.total_resources("biosphere_cultivator", bps) == {"carbon": 20, "volatiles": 4, "structural": 7}


def test_a_new_print_queues_its_components_first_with_its_tags(client):  # noqa: F811
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "blueprints", BPS)
    client.portal.call(db.kv_set, "devices", [{"device_code": "AF00BEEF", "device_type": "autofactory", "location": "SOL-3-L4",
                                               "status": "idle", "available_commands": ["enqueue_print"]}])   # nothing there yet
    client.portal.call(e.send, "POST", "/devices/AF00BEEF",
                       {"command": "enqueue_print", "device_type": "biosphere_cultivator", "quantity": 1,
                        "tags": ["contract:x-1", "to:kamuyis", "at:kamuyis-4"]}, "test")
    q = client.app.state.api.http._transport.app.state.world.queues["AF00BEEF"]
    af = next(d for d in client.app.state.api.http._transport.app.state.world.devices if d["device_code"] == "AF00BEEF")
    started = ["hydroponic_bay"] if af["status"] == "printing (hydroponic_bay)" and len(q) < 4 else []   # an idle factory starts the head
    assert (started + [x["device_type"] for x in q])[-4:] == ["hydroponic_bay", "hydroponic_bay", "orbital_farm", "biosphere_cultivator"]
    assert q[-2]["tags"] == ["contract:x-1", "component", "to:sol", "at:sol-3-l4"] and "at:kamuyis-4" in q[-1]["tags"]
    page = client.get("/blueprints", headers={"X-Auth-Request-Email": "joe@example.com"}).text
    assert "component device" in page and "all in: 20 carbon, 7 structural, 4 volatiles" in page


def test_a_factory_waiting_for_components_gets_them_printed_elsewhere(client):  # noqa: F811
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "blueprints", BPS)
    devs = client.portal.call(db.kv_get, "devices")
    af = next(d for d in devs if d["device_code"] == "AF00BEEF")
    devs.append({**af, "device_code": "AF00CAFE", "location": "SOL-4-L4", "status": "waiting_for_resources",
                 "print_queue": [{"device_type": "biosphere_cultivator", "tags": ["contract:x-1", "at:kamuyis-4"]}],
                 "waiting_for": {"components": {"hydroponic_bay": {"have": 0, "need": 2}, "orbital_farm": {"have": 1, "need": 1}}}})
    client.portal.call(db.kv_set, "devices", devs)
    gaps = comp.shortfalls(devs)
    assert [(g["type"], g["short"]) for g in gaps] == [("hydroponic_bay", 2)] and "at:sol-4-l4" in gaps[0]["tags"]
    out = client.portal.call(e.component_pass)
    assert out and "2 queued to print" in out[0], out
    q = client.app.state.api.http._transport.app.state.world.queues["AF00BEEF"]
    print("Q", [(x["device_type"], x.get("quantity")) for x in q])   # not the blocked factory
    assert q[-1]["device_type"] == "hydroponic_bay" and "contract:x-1" in q[-1]["tags"] and "at:sol-4-l4" in q[-1]["tags"]


def test_components_print_at_a_factory_in_the_waiting_ones_system_first(client):  # noqa: F811
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "blueprints", BPS)
    devs = client.portal.call(db.kv_get, "devices")
    af = next(d for d in devs if d["device_code"] == "AF00BEEF")   # SOL-3-L4, empty queue: it would win on load
    devs += [{**af, "device_code": "AF00CAFE", "location": "SOL-4-L4", "status": "waiting_for_resources",
              "print_queue": [{"device_type": "biosphere_cultivator", "tags": []}],
              "waiting_for": {"components": {"orbital_farm": {"have": 0, "need": 1}}}},
             {**af, "device_code": "AF00D00D", "location": "SOL-4-L4", "print_queue": [{"device_type": "x"}] * 3}]
    client.portal.call(db.kv_set, "devices", devs)
    pinned, printed, _ = client.portal.call(e.pin_or_print, "orbital_farm", 1, "SOL-4-L4", devs, set(), "t", None, None, {"AF00CAFE"})
    row = client.portal.call(db.fetchone, "SELECT path FROM actions WHERE body LIKE '%orbital_farm%' ORDER BY rowid DESC LIMIT 1")
    assert row["path"] == "/devices/AF00D00D"   # at the location, though busier than AF00BEEF in another spot


def test_a_new_print_uses_components_already_there_and_spares_before_printing(client):  # noqa: F811
    db = client.app.state.db
    client.portal.call(db.kv_set, "blueprints", BPS)
    client.portal.call(db.kv_set, "devices", [
        {"device_code": "AF1", "device_type": "autofactory", "location": "SOL-3-L4", "status": "idle",
         "available_commands": ["enqueue_print"], "print_queue": [{"device_type": "hydroponic_bay", "quantity": 1}]},
        {"device_code": "HB1", "device_type": "hydroponic_bay", "location": "SOL-3-L4", "status": "idle", "tags": []},
        {"device_code": "OF1", "device_type": "orbital_farm", "location": "ABOTEIN-2", "status": "idle", "tags": ["spare"]}])
    sent = []

    class Api:
        async def request(self, method, path, json_body=None, **kw):
            sent.append((method, path, json_body))
    body = {"command": "enqueue_print", "device_type": "biosphere_cultivator", "quantity": 1, "tags": ["contract:x-1"]}
    client.portal.call(comp.before_print, Api(), db, "/devices/AF1", body)
    # one bay deployed there + one in its queue = the two it needs; the farm is a spare, pinned there: nothing printed
    assert sent == [("PATCH", "/devices/OF1", {"configuration": {"add_tags": ["contract:x-1", "component", "to:sol", "at:sol-3-l4"],
                                                                  "remove_tags": ["spare"]}})]
    # a second cultivator: the bays are spoken for now that one is queued
    devs = client.portal.call(db.kv_get, "devices")
    devs[0]["print_queue"].append({"device_type": "biosphere_cultivator", "quantity": 1})
    devs[2]["tags"] = ["spare", "at:sol-3-l4"]
    client.portal.call(db.kv_set, "devices", devs)
    sent.clear()
    client.portal.call(comp.before_print, Api(), db, "/devices/AF1", body)
    assert [(b["device_type"], b["quantity"]) for _, _, b in sent] == [("hydroponic_bay", 2), ("orbital_farm", 1)]


def test_a_component_the_game_wont_print_doesnt_block_the_item(client):  # noqa: F811
    from rsweb.api import ApiError
    db = client.app.state.db
    client.portal.call(db.kv_set, "blueprints", [{"device_type": "climate_processor", "resources": {"carbon": 10},
                                                  "components": {"terraform_monitor": 1}}])
    client.portal.call(db.kv_set, "devices", [
        {"device_code": "AF1", "device_type": "autofactory", "location": "SOLARES-BELT-1", "status": "idle",
         "available_commands": ["enqueue_print"]},
        {"device_code": "TM1", "device_type": "terraform_monitor", "location": "SOLARES-BELT-1", "status": "idle"}])
    sent = []

    class Api:
        async def request(self, method, path, json_body=None, **kw):
            if json_body["device_type"] == "terraform_monitor":
                raise ApiError(400, "Blueprint not yet unlocked: terraform_monitor")
            sent.append(json_body)
    client.portal.call(comp.before_print, Api(), db, "/devices/AF1",
                       {"command": "enqueue_print", "device_type": "climate_processor", "quantity": 2, "tags": []})
    log = client.portal.call(db.kv_get, "automation_log")
    assert "couldn't queue 1 terraform monitor" in log[-1]["text"] and "not yet unlocked" in log[-1]["text"]


def test_prints_split_over_the_autofactories_at_the_stockpile(client):  # noqa: F811
    # a locker's devices all went on one of two autofactories at the same stockpile (2026-10-20)
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "blueprints", BPS)
    devs = client.portal.call(db.kv_get, "devices")
    af = next(d for d in devs if d["device_code"] == "AF00BEEF")
    devs += [{**af, "device_code": "AF00CAFE", "print_queue": []}]
    client.portal.call(db.kv_set, "devices", devs)
    world = client.app.state.api.http._transport.app.state.world
    world.devices.append({**next(d for d in world.devices if d["device_code"] == "AF00BEEF"), "device_code": "AF00CAFE"})
    load: dict = {}
    pinned, printed, note = client.portal.call(e.pin_or_print, "orbital_farm", 4, af["location"], devs, set(), "locker x",
                                               None, None, frozenset(), load)
    assert printed == 4 and note is None, note
    rows = client.portal.call(db.fetchall, "SELECT path, body FROM actions WHERE body LIKE '%orbital_farm%'")
    assert sorted(r["path"] for r in rows) == ["/devices/AF00BEEF", "/devices/AF00CAFE"], rows
    assert set(load) == {"AF00BEEF", "AF00CAFE"}


def test_a_pinned_spare_is_no_longer_spare(client):  # noqa: F811
    # a locker request pinned spares but left their spare tag on (2026-10-21)
    from rsweb.loadouts import assigned
    e = client.app.state.worker.automations
    world = client.app.state.api.http._transport.app.state.world
    src = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    spare = {**src, "device_code": "FA00F00D", "device_type": "orbital_farm", "tags": ["spare", "stock"],
             "location": "SOL-4-L4", "print_queue": []}
    world.devices.append(spare)
    pinned, printed, _ = client.portal.call(e.pin_or_print, "orbital_farm", 1, "SOL-3-1", [dict(spare)], set(), "locker x")
    assert pinned == ["FA00F00D"] and printed == 0
    tags = next(d for d in world.devices if d["device_code"] == "FA00F00D")["tags"]
    assert "spare" not in tags and "stock" not in tags and "at:sol-3-1" in tags, tags
    assert assigned({"tags": ["spare", "at:sol-3-1"]}) and not assigned({"tags": ["spare", "to:sol", "gather"]})


def test_components_are_tagged_and_not_traded_between_hubs(client):  # noqa: F811
    # live 2026-10-28: both printing hubs' vector charge prints sat at 4 of 6 impulse charges; the charges came out of
    # the stock print tagged spare/stock, the hub loadout (impulse charge: 0) kept making them spare, and each hub's
    # component pass sent the other's spares over (each leaving with both at: pins)
    from rsweb import loadouts
    assert comp.component_tags(["spare", "stock", "fleet:x", "to:a", "at:a-1"], "SOLARES-BELT-1") == \
        ["component", "to:solares", "at:solares-belt-1"]
    fac = lambda code, loc: {"device_code": code, "device_type": "autofactory", "location": loc, "status": "waiting_for_resources",  # noqa: E731
                             "print_queue": [{"device_type": "vector_charge", "tags": ["spare", "stock"]}],
                             "waiting_for": {"components": {"impulse_charge": {"have": 2, "need": 3}}}}
    ic = lambda code, loc, tags: {"device_code": code, "device_type": "impulse_charge", "location": loc, "status": "idle",  # noqa: E731
                                  "tags": tags}
    devs = [fac("AF1", "SOLARES-BELT-1"), fac("AF2", "FALQUORYX-BELT-1"),
            ic("S1", "SOLARES-BELT-1", ["at:solares-belt-1", "spare"]), ic("S2", "SOLARES-BELT-1", []),
            ic("F1", "FALQUORYX-BELT-1", ["at:falquoryx-belt-1", "spare"]), ic("F2", "FALQUORYX-BELT-1", ["spare", "stock"]),
            ic("X1", "ABOTEIN-2", ["spare"])]
    claim = comp.claimable(devs)
    assert sorted(d["device_code"] for d, _ in claim) == ["F1", "F2", "S1", "S2"]
    f2 = next((d, t) for d, t in claim if d["device_code"] == "F2")
    assert comp.retag(*f2) == {"add_tags": ["component", "to:falquoryx", "at:falquoryx-belt-1"], "remove_tags": ["spare", "stock"]}
    for d, t in claim:
        d["tags"] = [x for x in d["tags"] if x not in comp.retag(d, t)["remove_tags"] and x not in t] + t
    # another factory's components (or spares pinned elsewhere) aren't spares to take; a free spare is
    assert [d["device_code"] for d in comp.spares_elsewhere(devs, "SOLARES-BELT-1", "impulse_charge")] == ["X1"]
    assert loadouts.is_component({"tags": ["component"]})
    e = client.app.state.worker.automations
    world = client.app.state.api.http._transport.app.state.world
    world.devices += [dict(d) for d in devs[2:]]
    pinned, printed, _ = client.portal.call(e.pin_or_print, "impulse_charge", 2, "SOLARES-BELT-1", devs[4:], set(), "t",
                                            None, comp.component_tags([], "SOLARES-BELT-1"), {"AF1", "AF2"})
    assert pinned == ["X1"]
    assert set(next(d for d in world.devices if d["device_code"] == "X1")["tags"]) == {"component", "to:solares", "at:solares-belt-1"}


def test_spares_pinned_once_per_pass_and_nearest_first(client):  # noqa: F811
    """Snapshot 2026-11-01: two factories in one pass each pinned the same spare mining drones (both at: tags), and the
    SOLARES and FALQUORYX factories took each other's spares instead of their own."""
    db, e = client.app.state.db, client.app.state.worker.automations
    S = lambda c, loc: {"device_code": c, "device_type": "orbital_farm", "location": loc, "status": "idle",  # noqa: E731
                        "tags": ["spare"]}
    world = client.app.state.api.http._transport.app.state.world
    src = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    devs = [S("FA00FA01", "PROCYON-1-L4"), S("FA00FA02", "SOL-3-1")]
    world.devices += [{**src, **d, "print_queue": []} for d in devs]
    pinned, _, _ = client.portal.call(e.pin_or_print, "orbital_farm", 1, "SOL-3-L4", devs, set(), "t")
    assert pinned == ["FA00FA02"]
    assert "at:sol-3-l4" in devs[1]["tags"] and "spare" not in devs[1]["tags"]
    pinned, _, _ = client.portal.call(e.pin_or_print, "orbital_farm", 1, "SOL-4-L4", devs, set(), "t")
    assert pinned == ["FA00FA01"]                               # the near one is taken now


def test_components_never_pinned_or_left_pinned_beyond_the_sector_range(client):  # noqa: F811
    """Snapshot 2026-11-01 20:41: spare mining drones at ANLOREN (Delta sector) were pinned to the FALQUORYX factory, and
    their carriers' trips sat held at the sector range."""
    db, e = client.app.state.db, client.app.state.worker.automations
    cat = client.portal.call(db.kv_get, "stars", {})
    cat["stars"] = (cat.get("stars") or []) + [{"designation": "FARSTAR", "position": {"x": 9000, "y": 0, "z": 0}},
                                                {"designation": "NEARSTAR", "position": {"x": 0, "y": 0, "z": 1}},
                                                {"designation": "HOMESTAR", "position": {"x": 0, "y": 0, "z": 0}}]
    client.portal.call(db.kv_set, "stars", cat)
    S = lambda c, loc, tags=("spare",): {"device_code": c, "device_type": "orbital_farm", "location": loc,  # noqa: E731
                                         "status": "idle", "tags": list(tags)}
    world = client.app.state.api.http._transport.app.state.world
    src = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    devs = [S("FA00FA11", "FARSTAR-1"), S("FA00FA12", "NEARSTAR-1")]
    world.devices += [{**src, **d, "print_queue": []} for d in devs]
    pinned, _, _ = client.portal.call(e.pin_or_print, "orbital_farm", 2, "HOMESTAR-BELT-1", devs, set(), "t", None, None,
                                      {"AF00BEEF"})
    assert pinned == ["FA00FA12"]                          # the far spare stays where it is
    # a component already pinned beyond the range: its held trip is dropped and it is a spare again
    stuck = S("FA00FA13", "FARSTAR-1", ["component", "to:homestar", "at:homestar-belt-1"])
    world.devices.append({**src, **stuck, "print_queue": []})
    devs = client.portal.call(db.kv_get, "devices") + [stuck]
    client.portal.call(db.kv_set, "devices", devs)
    job = {"id": "loadouts-x", "rule": "loadouts", "title": "loadouts: carry 1", "status": "waiting", "device": "CA00CA00",
           "created_at": "2026-11-01T13:39:57+00:00", "meta": {"devices": ["FA00FA13"], "star": "HOMESTAR"},
           "steps": [{"desc": "fly", "status": "confirm"}]}
    client.portal.call(e.save_jobs, client.portal.call(e.jobs) + [job])
    client.portal.call(db.kv_set, "components_last", None)
    out = client.portal.call(e.component_pass)
    assert any("beyond the sector range" in x for x in out), out
    assert next(j for j in client.portal.call(e.jobs) if j["id"] == "loadouts-x")["status"] == "cancelled"
    assert set(next(d for d in world.devices if d["device_code"] == "FA00FA13")["tags"]) == {"spare"}


def test_a_stuck_factory_prints_its_components_first(client):  # noqa: F811
    """Snapshot 2026-11-01 20:41: every factory's head was a mining rig waiting for drones; no factory was free to print
    them, and the drone prints queued sat behind the rigs."""
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "blueprints", BPS)
    world = client.app.state.api.http._transport.app.state.world
    rig = lambda: {"device_type": "biosphere_cultivator", "notify": {"device": None}, "tags": ["fleet:m1"]}  # noqa: E731
    farm = {"device_type": "orbital_farm", "notify": {"device": None}, "tags": comp.component_tags([], "SOL-3-L4")}
    world.queues["AF00BEEF"] = [rig(), rig(), dict(farm)]
    af = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    af.update(status="waiting_for_resources",
              waiting_for={"components": {"hydroponic_bay": {"have": 2, "need": 2}, "orbital_farm": {"have": 0, "need": 1}}})
    kv = {**af, "print_queue": [dict(x) for x in world.queues["AF00BEEF"]]}
    client.portal.call(db.kv_set, "devices", [kv, {"device_code": "FA00FA21", "device_type": "orbital_farm",
                                                   "location": "SOL-3-L4", "status": "mining", "tags": ["fleet:m1"]}])
    assert comp.free_here([kv, {"device_code": "X", "device_type": "orbital_farm", "location": "SOL-3-L4",
                                "status": "idle", "tags": ["fleet:m1"]}], kv, "orbital_farm", {}) == 1   # the queued one
    client.portal.call(db.kv_set, "components_last", None)
    out = client.portal.call(e.component_pass)
    assert any("front of its queue" in x for x in out), out
    q = world.queues["AF00BEEF"]
    assert [x["device_type"] for x in q] == ["orbital_farm", "biosphere_cultivator", "biosphere_cultivator"], q


def test_a_component_pinned_elsewhere_without_its_trip_is_sent_on():
    """Snapshot 2026-11-03 23:43: relays for FALQUORYX's relay stations sat at SOLARES pinned at:falquoryx-belt-1 with no
    to: tag (the relay unbinding made them spare), counted as coming, so both FALQUORYX queues waited."""
    R = lambda c, loc, tags: {"device_code": c, "device_type": "ftl_relay", "location": loc, "status": "inactive",  # noqa: E731
                              "tags": tags}
    devs = [R("R1", "SOLARES-BELT-1", ["at:falquoryx-belt-1", "component", "contract:k-3-evt-010", "gather", "spare"]),
            R("R2", "SOLARES-BELT-1", ["at:falquoryx-belt-1", "component", "to:falquoryx"]),   # still on its way
            R("R3", "FALQUORYX-BELT-1", ["at:falquoryx-belt-1", "component"])]                  # there already
    out = comp.unbound(devs)
    assert [(d["device_code"], cfg) for d, cfg in out] == [
        ("R1", {"add_tags": ["to:falquoryx"], "remove_tags": ["gather", "spare"]})]


def test_relay_components_bound_for_a_relayed_system_keep_their_trip():
    """The loadout pass makes a relay bound for a system that already has one relaying spare; a print's component or a
    contract's relay keeps its to: tag (the snapshot 2026-11-03 relays for FALQUORYX's stations lost theirs)."""
    from rsweb import loadouts as lo

    def dev(code, t, loc, **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": kw.pop("status", "inactive"),
                "operational_capacity": 100.0, "features": kw.pop("features", ["cruise", "stow"]),
                "available_commands": ["travel", "stow", "deploy"], **kw}
    devices = [dev("W1", "ftl_relay", "AAA-1-L4", status="relaying"),
               dev("R1", "ftl_relay", "HUB-1", tags=["to:aaa"]),
               dev("C1", "ftl_relay", "HUB-1", tags=["to:aaa", "at:aaa-belt-1", "component"]),
               dev("K1", "ftl_relay", "HUB-1", tags=["to:aaa", "at:aaa-3", "contract:aaa-3-evt-1"]),
               dev("HV", "heaven_vessel", "HUB-1", features=["surge", "cruise"], stow_capacity=5,
                   hosting_replicant={"replicant_code": "X"}, tags=["fleet:logistics"])]
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in (("HUB", 0), ("AAA", 5))}
    cfg = {"phases": [], "systems": {}, "fleets": [{"id": "logistics", "name": "Logistics", "role": "logistics",
                                                    "home": "HUB", "wants": {}}]}
    p = lo.plan(cfg, devices, [], {}, stars, {"HV": "X"}, set(), [], {}, commandable={"HUB", "AAA"})
    assert "to:aaa" in p["tag_remove"].get("R1", [])
    assert "to:aaa" not in p["tag_remove"].get("C1", []) and "to:aaa" not in p["tag_remove"].get("K1", [])


def test_a_component_aboard_a_replicant_vessel_at_its_pin_is_unloaded():
    """Snapshot 2026-11-03 23:43: the third drone for a SOLARES rig stayed stowed in Wendell's idle vessel at
    SOLARES-BELT-1 (the vessel isn't in the planner's pool, so the drone had no known place); the rig waited at 2 of 3."""
    from rsweb import loadouts as lo

    def dev(code, t, loc, **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": kw.pop("status", "idle"),
                "operational_capacity": 100.0, "features": kw.pop("features", ["cruise", "stow"]),
                "available_commands": ["travel", "stow", "deploy"], **kw}
    hv = dev("HV", "heaven_vessel", "SOLARES-BELT-1", features=["surge", "cruise"], stow_capacity=5,
             hosting_replicant={"replicant_code": "X"}, tags=["fleet:logistics"])
    d1 = dev("D1", "mining_drone", None, status="stowed", stowed_in_device_code="HV",
             tags=["at:solares-belt-1", "component", "to:solares"])
    stars = {s: {"designation": s, "position": {"x": x, "y": 0, "z": 0}} for s, x in (("SOLARES", 0), ("FALQUORYX", 500))}
    cfg = {"phases": [], "systems": {}, "fleets": [{"id": "logistics", "name": "Logistics", "role": "logistics",
                                                    "home": "SOLARES", "wants": {}}]}
    p = lo.plan(cfg, [hv, d1], [], {}, stars, {"HV": "X"}, set(), [], {"HV": ["D1"]}, commandable={"SOLARES"})
    assert p["arrived"] == ["D1"]
    p = lo.plan(cfg, [{**hv, "status": "surging"}, d1], [], {}, stars, {"HV": "X"}, set(), [], {"HV": ["D1"]},
                commandable={"SOLARES"})
    assert p["arrived"] == []   # still on the way


def test_more_components_coming_than_needed_drop_prints_then_the_furthest_pins():
    # snapshot 2026-11-13 03:58: six observatories pinned to FALQUORYX-BELT-1 and one printing for a parallax array (3)
    bps = {"parallax_array": {"device_type": "parallax_array", "components": {"galactic_observatory": 3}}}
    pin = ["component", "to:aaa", "at:aaa-1"]
    af = {"device_code": "AF1", "device_type": "autofactory", "location": "AAA-1", "available_commands": ["enqueue_print"],
          "print_queue": [{"device_type": "parallax_array"}], "waiting_for": {"components": {"galactic_observatory": {"have": 0, "need": 3}}}}
    af2 = {"device_code": "AF2", "device_type": "autofactory", "location": "AAA-1", "available_commands": ["enqueue_print"],
           "printing": {"device_type": "galactic_observatory", "tags": pin},
           "print_queue": [{"device_type": "galactic_observatory", "quantity": 1, "tags": pin}]}
    go = lambda c, loc: {"device_code": c, "device_type": "galactic_observatory", "location": loc, "status": "compacted",  # noqa: E731
                         "tags": list(pin)}
    devs = [af, af2, go("G1", "BBB-4"), go("G2", "CCC-4"), go("G3", "DDD-4"), go("G4", "EEE-4")]
    pos = {s: {"x": x, "y": 0, "z": 0} for s, x in (("AAA", 0), ("BBB", 1), ("CCC", 2), ("DDD", 3), ("EEE", 4))}
    (g,) = comp.surplus(devs, bps, pos)
    assert g["excess"] == 2   # 4 pinned + 1 queued, 3 needed; the started print doesn't count
    assert [f["device_code"] for f, _ in g["prints"]] == ["AF2"]
    assert [d["device_code"] for d in g["pins"]][:1] == ["G4"]   # furthest first
    assert comp.surplus(devs[:5], bps, pos)[0]["excess"] == 1
    assert comp.surplus(devs[:4], bps, pos) == []


LOCKED_BPS = [{"device_type": "atmospheric_regulator", "resources": {"carbon": 10}, "print_time": 60,
               "components": {"aquifer_tap": 1, "bio_seeder": 1}},
              {"device_type": "bio_seeder", "resources": {"carbon": 5}, "print_time": 60},
              {"device_type": "orbital_farm", "resources": {"structural": 7}, "print_time": 60}]


def test_locked_components_with_none_on_hand_mark_a_type_stuck():
    bps = {b["device_type"]: b for b in LOCKED_BPS}   # aquifer_tap isn't on the list: its blueprint is locked
    assert comp.locked_parts("atmospheric_regulator", bps) == {"aquifer_tap"}
    assert comp.locked_parts("atmospheric_regulator", bps, {"bio_seeder"}) == {"aquifer_tap", "bio_seeder"}
    assert comp.stuck_parts("atmospheric_regulator", bps, set(), []) == {"aquifer_tap"}
    spare = [{"device_code": "AQ1", "device_type": "aquifer_tap", "location": "X-1", "tags": ["spare"]}]
    assert comp.stuck_parts("atmospheric_regulator", bps, set(), spare) == set()
    assert "atmospheric_regulator" not in comp.printable_bps(bps, set(), []) and "orbital_farm" in comp.printable_bps(bps, set(), [])
    fac = {"device_code": "F1", "location": "SOLARES-BELT-1", "status": "waiting_for_resources",
           "print_queue": [{"device_type": "atmospheric_regulator", "tags": ["fleet:sol-trader"]}, {"device_type": "orbital_farm"}],
           "waiting_for": {"components": {"aquifer_tap": {"have": 0, "need": 1}, "bio_seeder": {"have": 0, "need": 1}}}}
    heads = comp.stuck_heads([fac], bps, set())
    assert heads == [{"factory": "F1", "location": "SOLARES-BELT-1", "type": "atmospheric_regulator", "missing": ["aquifer_tap"]}]
    assert comp.stuck_heads([fac] + spare, bps, set()) == []


def test_a_print_with_locked_components_is_refused_and_a_stuck_head_dropped(client):  # noqa: F811
    db, e = client.app.state.db, client.app.state.worker.automations
    w = client.app.state.api.http._transport.app.state.world
    client.portal.call(db.kv_set, "blueprints", LOCKED_BPS)
    fac = {"device_code": "AF00BEEF", "device_type": "autofactory", "location": "SOL-3-L4", "status": "idle",
           "available_commands": ["enqueue_print", "dequeue_print"]}
    client.portal.call(db.kv_set, "devices", [fac])
    before = list(w.queues.get("AF00BEEF", []))
    ok, _, err = client.portal.call(e.send, "POST", "/devices/AF00BEEF",
                                    {"command": "enqueue_print", "device_type": "atmospheric_regulator", "quantity": 1}, "test")
    assert not ok and "aquifer tap" in str(err) and w.queues.get("AF00BEEF", []) == before
    plan = client.portal.call(e.stuck_prints, [fac], LOCKED_BPS)
    assert list(plan) == ["atmospheric_regulator"]
    # one already queued before the fix: dequeued so the print behind it goes
    w.queues["AF00BEEF"] = [{"device_type": "atmospheric_regulator"}, {"device_type": "orbital_farm"}]
    stuck = {**fac, "status": "waiting_for_resources", "print_queue": list(w.queues["AF00BEEF"]),
             "waiting_for": {"components": {"aquifer_tap": {"have": 0, "need": 1}, "bio_seeder": {"have": 0, "need": 1}}}}
    out = client.portal.call(e.unstick_heads, [stuck])
    assert out and "dropped the atmospheric regulator" in out[0], out
    assert [x["device_type"] for x in w.queues["AF00BEEF"]] == ["orbital_farm"]


def test_components_are_kept_off_patrol_and_taken_off_a_directive(client):  # noqa: F811
    from rsweb import ami_schedule as ams, upkeep as up
    e = client.app.state.worker.automations
    drone = {"device_code": "MD000001", "device_type": "maintenance_drone", "location": "SOLARES-4-L4", "status": "coordinating",
             "tags": ["at:solares-belt-1", "component"], "ami_directive": {"name": "patrol"}, "ami_directive_status": "active",
             "available_commands": ["clear_directive", "set_directive", "travel"]}
    idle = {**drone, "status": "idle", "location": "SOLARES-BELT-1", "ami_directive": None}
    assert ams.reserved(idle) and up.to_patrol([idle], set()) == [] and ams.wakeups([idle], set(), {}) == []
    client.app.state.api.http._transport.app.state.world.devices.append(dict(drone))
    out = client.portal.call(e.recall_working_components, [drone, idle])
    assert len(out) == 1 and "MD000001" in out[0] and "patrol directive" in out[0]
    row = client.portal.call(client.app.state.db.fetchone, "SELECT body FROM actions WHERE path='/devices/MD000001' ORDER BY rowid DESC LIMIT 1")
    assert "clear_directive" in row["body"]
