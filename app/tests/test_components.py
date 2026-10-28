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
