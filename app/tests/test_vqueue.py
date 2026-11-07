from rsweb import vqueue
from rsweb.vqueue import VirtualQueue
from test_components import BPS
from test_ops import client  # noqa: F401  (fixture)

HX = {"X-Auth-Request-Email": "joe@example.com", "HX-Request": "true"}


def setup(client):  # noqa: F811
    db, e = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.kv_set, "blueprints", BPS)
    client.app.state.api.vq = VirtualQueue(db)
    world = client.app.state.api.http._transport.app.state.world
    world.queues["AF00BEEF"] = []
    next(d for d in world.devices if d["device_code"] == "AF00BEEF")["status"] = "printing (x)"   # nothing starts
    return db, e, world


def sync_queue(client):  # noqa: F811
    """The mock's device list has no print_queue (the game's has): take the factory's own read."""
    db = client.app.state.db
    dev = client.portal.call(client.app.state.api.get, "/devices/AF00BEEF")
    devs = [dev if d.get("device_code") == "AF00BEEF" else d for d in client.portal.call(db.kv_get, "devices")]
    client.portal.call(db.kv_set, "devices", devs)


def feed(client):  # noqa: F811
    w, e = client.app.state.worker, client.app.state.worker.automations
    client.portal.call(w.sync_devices)
    sync_queue(client)
    e._vq_fed = {}
    out = client.portal.call(e.vqueue_pass)
    return out


def test_prints_wait_in_the_app_and_components_go_first(client):  # noqa: F811
    db, e, world = setup(client)
    for _ in range(3):
        ok, resp, _ = client.portal.call(e.send, "POST", "/devices/AF00BEEF",
                                         {"command": "enqueue_print", "device_type": "biosphere_cultivator", "quantity": 1,
                                          "tags": ["fleet:m1"]}, "t")
        assert ok and resp["virtual"]
    assert world.queues["AF00BEEF"] == []                       # nothing sent to the game yet
    dev = client.portal.call(client.app.state.api.get, "/devices/AF00BEEF")
    q = dev["print_queue"]
    assert all(x["virtual"] for x in q) and [x["device_type"] for x in q][-1] == "biosphere_cultivator"
    assert [x["priority"] for x in q][:3] == ["component"] * 3   # 2 bays and a farm before any cultivator
    feed(client)
    assert [x["device_type"] for x in world.queues["AF00BEEF"]] == ["hydroponic_bay", "hydroponic_bay", "orbital_farm"], world.queues
    left = client.portal.call(VirtualQueue(db).orders, "AF00BEEF")
    assert len(left) == 7 and left[-1]["body"]["device_type"] == "biosphere_cultivator"
    # an index past the game's queue takes a print off the app's queue
    sync_queue(client)
    ok, _, _ = client.portal.call(e.send, "POST", "/devices/AF00BEEF", {"command": "dequeue_print", "index": 8}, "t")
    assert ok and len(client.portal.call(VirtualQueue(db).orders, "AF00BEEF")) == 6


def test_a_long_game_queue_is_cut_down_and_better_prints_go_ahead(client):  # noqa: F811
    db, e, world = setup(client)
    rig = lambda: {"device_type": "biosphere_cultivator", "notify": {"device": None}, "tags": ["fleet:m1"]}  # noqa: E731
    world.queues["AF00BEEF"] = [rig() for _ in range(5)]
    feed(client)
    assert len(world.queues["AF00BEEF"]) == 2                    # 3 back to the app's queue
    kept = client.portal.call(VirtualQueue(db).orders, "AF00BEEF")
    assert [o["prio"] for o in kept] == [3, 3, 3]
    ok, _, _ = client.portal.call(e.send, "POST", "/devices/AF00BEEF",
                                  {"command": "enqueue_print", "device_type": "orbital_farm", "quantity": 1,
                                   "tags": ["component", "to:sol", "at:sol-3-l4"]}, "t")
    feed(client)
    assert [x["device_type"] for x in world.queues["AF00BEEF"]] == ["orbital_farm", "biosphere_cultivator"]
    assert len(client.portal.call(VirtualQueue(db).orders, "AF00BEEF")) == 4
    # a print you queue by hand goes ahead of all of them
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "add", "device_type": "orbital_farm", "quantity": 1},
                    headers=HX)
    assert r.status_code == 200
    kept = client.portal.call(VirtualQueue(db).orders, "AF00BEEF")
    assert kept[0]["prio"] == 0
    feed(client)
    assert [x["device_type"] for x in world.queues["AF00BEEF"]] == ["orbital_farm", "orbital_farm"]
    assert world.queues["AF00BEEF"][0]["tags"] == []


def test_priorities():
    assert vqueue.priority({"device_type": "x"}) == 0
    assert vqueue.priority({"device_type": "ftl_relay", "tags": ["to:a"]}) == 2
    assert vqueue.priority({"device_type": "mining_drone", "tags": ["component"]}) == 1
    assert vqueue.priority({"device_type": "x", "tags": ["spare", "stock"]}) == 4
    assert vqueue.priority({"device_type": "mining_rig", "tags": ["fleet:m"]}) == 3



def test_a_print_that_fits_goes_ahead_of_a_waiting_one(client):  # noqa: F811
    """Owner 2026-11-01: while the first print waits, one that doesn't use what it needs goes ahead of it. Live
    2026-11-02: the waiting print is still first in the game's queue (deactivate: "Nothing to deactivate")."""
    db, e, world = setup(client)
    af = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    af.update(status="waiting_for_resources")
    item = lambda t, tags: {"device_type": t, "notify": {"device": None}, "tags": tags}  # noqa: E731
    world.queues["AF00BEEF"] = [item("orbital_farm", ["fleet:m1"]), item("biosphere_cultivator", ["fleet:m2"]),
                                item("hydroponic_bay", ["fleet:m2"])]
    # the farm waits for structural; the cultivator needs components of its own; the bay uses carbon and volatiles
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-3-L4", "items": {"carbon": 12, "structural": 5,
                                                                                     "volatiles": 5}}])
    out = feed(client)
    assert any("hydroponic_bay goes ahead of the waiting orbital_farm" in x for x in out), out
    assert [x["device_type"] for x in world.queues["AF00BEEF"]] == ["hydroponic_bay", "orbital_farm"]
    assert world.queues["AF00BEEF"][1]["tags"] == ["fleet:m1"]
    kept = client.portal.call(VirtualQueue(db).orders, "AF00BEEF")
    assert [o["body"]["device_type"] for o in kept] == ["biosphere_cultivator"]
    # nothing fits: the queue stays
    world.queues["AF00BEEF"] = [item("orbital_farm", ["fleet:m1"]), item("hydroponic_bay", ["fleet:m2"])]
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-3-L4", "items": {"carbon": 2}}])
    e._vq_jumped = {}
    out = feed(client)
    assert not any("goes ahead" in x for x in out), out
    assert [x["device_type"] for x in world.queues["AF00BEEF"]] == ["orbital_farm", "hydroponic_bay"]
    # Cancel current print takes a waiting print off the queue
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "cancel"}, headers=HX)
    assert r.status_code == 200 and [x["device_type"] for x in world.queues["AF00BEEF"]] == ["hydroponic_bay"]


def test_components_for_every_queued_print_are_ordered(client):  # noqa: F811
    db, e, world = setup(client)
    item = lambda t: {"device_type": t, "notify": {"device": None}, "tags": ["fleet:m1"]}  # noqa: E731
    world.queues["AF00BEEF"] = [item("biosphere_cultivator") for _ in range(2)]
    client.portal.call(client.app.state.worker.sync_devices)
    sync_queue(client)
    client.portal.call(db.kv_set, "components_last", None)
    out = client.portal.call(e.component_pass)
    assert any("4 hydroponic bay queued" in x for x in out) and any("2 orbital farm queued" in x for x in out), out
    kept = client.portal.call(VirtualQueue(db).orders, "AF00BEEF")
    assert sorted((o["body"]["device_type"], o["body"]["quantity"]) for o in kept) == [("hydroponic_bay", 4), ("orbital_farm", 2)]
    sync_queue(client)
    client.portal.call(db.kv_set, "components_last", None)
    out = client.portal.call(e.component_pass)
    assert not any("queued for the prints" in x for x in out), out   # counted as coming now


def test_jump_rules():
    bps = {b["device_type"]: b for b in BPS}
    cur = {"device_type": "biosphere_cultivator"}
    bay = {"device_type": "hydroponic_bay", "tags": ["x"]}
    # a component the waiting print needs may use its reserve
    assert vqueue.jump(cur, [bay], [], {"carbon": 6, "volatiles": 2}, bps) == ("real", bay)
    assert vqueue.jump({"device_type": "orbital_farm"}, [bay], [], {"carbon": 4, "volatiles": 2}, bps) is None
    assert vqueue.jump(cur, [{"device_type": "biosphere_cultivator", "tags": ["x"]}], [], {"carbon": 99}, bps) is None


def test_a_big_order_goes_to_the_game_a_few_at_a_time(client):  # noqa: F811
    """Live 2026-11-02: 28 drones in one enqueue_print got "Not enough queue space (10 available, 28 requested)"."""
    db, e, world = setup(client)
    world.queues["AF00BEEF"] = [{"device_type": "biosphere_cultivator", "notify": {"device": None}, "tags": ["fleet:m1"]}]
    ok, _, _ = client.portal.call(e.send, "POST", "/devices/AF00BEEF",
                                  {"command": "enqueue_print", "device_type": "orbital_farm", "quantity": 28,
                                   "tags": ["component", "to:sol", "at:sol-3-l4"]}, "t")
    assert ok
    feed(client)
    assert [x["device_type"] for x in world.queues["AF00BEEF"]] == ["orbital_farm"] * 3 + ["biosphere_cultivator"]
    kept = client.portal.call(VirtualQueue(db).orders, "AF00BEEF")
    assert [(o["body"]["device_type"], o["body"]["quantity"]) for o in kept] == [("orbital_farm", 25)]


def test_prints_kept_in_the_app_take_no_queue_slots():
    from rsweb import printqueue as pq, ward_orders
    f = {"print_queue": [{"device_type": "a"}] + [{"device_type": "b", "virtual": True}] * 20}
    assert pq.slots_used(f) == 1 and ward_orders.room(f) == 9


def test_factories_at_one_spot_share_their_kept_prints(client):  # noqa: F811
    """Owner 2026-11-03: a second factory at the spot takes prints kept for its busy neighbor."""
    db, e, world = setup(client)
    af = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    world.devices.append({**af, "device_code": "AF00CAFE"})
    world.queues["AF00CAFE"] = []
    for _ in range(4):
        ok, _, _ = client.portal.call(e.send, "POST", "/devices/AF00BEEF",
                                      {"command": "enqueue_print", "device_type": "orbital_farm", "quantity": 1,
                                       "tags": ["fleet:m1"]}, "t")
        assert ok
    feed(client)
    assert len(world.queues["AF00BEEF"]) == 2 and len(world.queues["AF00CAFE"]) == 2, world.queues
    assert client.portal.call(VirtualQueue(db).orders, "AF00BEEF") == []


def test_group_time_counts_every_factory():
    from rsweb import printqueue as pq
    bps = {"a": {"print_time": 100}}
    it = lambda n: {"device_type": "a", "quantity": n}  # noqa: E731
    # one factory busy with 3 of its own, one idle; 4 shared prints go 3 to the idle one, 1 to the busy one
    secs, exact = pq.group_seconds([(None, [it(3)]), (None, [])], [it(4)], bps)
    assert secs == 400 and exact
    assert pq.group_seconds([(None, []), (None, [])], [], bps) == (None, True)


def test_blueprints_page_merges_factories_at_one_spot(client):  # noqa: F811
    db, e, world = setup(client)
    af = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    world.devices.append({**af, "device_code": "AF00CAFE"})
    world.queues["AF00CAFE"] = []
    client.portal.call(client.app.state.worker.sync_devices)
    ok, _, _ = client.portal.call(e.send, "POST", "/devices/AF00BEEF",
                                  {"command": "enqueue_print", "device_type": "orbital_farm", "quantity": 4,
                                   "tags": ["fleet:m1"]}, "t")
    assert ok
    r = client.get("/blueprints", headers={"X-Auth-Request-Email": "joe@example.com"})
    assert "/print-queue/group?codes=AF00BEEF,AF00CAFE" in r.text or "/print-queue/group?codes=AF00CAFE,AF00BEEF" in r.text
    r = client.get("/print-queue/group?codes=AF00BEEF,AF00CAFE", headers=HX)
    assert r.status_code == 200 and "Shared app queue" in r.text and "with all 2 factories" in r.text, r.text


def test_class_limited_printers_are_no_factory_and_lose_the_apps_prints(client):  # noqa: F811
    """Live 2026-11-03: a crystal synthesis lab (features print + epitaxy) got relay and beacon prints, which the game
    refused ("This printer can only fabricate epitaxy-class devices"), and logistics hauled materials to it."""
    from rsweb import loadouts as lo
    lab = {"device_code": "CC00CAFE", "device_type": "crystal_synthesis_lab", "features": ["cruise", "print", "epitaxy"],
           "available_commands": ["enqueue_print", "dequeue_print"], "location": "DELTA-OBJ-2", "status": "idle"}
    assert not lo.is_factory(lab) and lo.is_printer(lab)
    assert lo.is_factory({"device_type": "autofactory"})
    db, e, world = setup(client)
    world.devices.append(lab)
    world.queues["CC00CAFE"] = []
    vq = client.app.state.api.vq
    client.portal.call(vq.add, "CC00CAFE", {"device_type": "ftl_relay", "quantity": 1, "tags": ["to:x"]}, 2)
    client.portal.call(vq.add, "CC00CAFE", {"device_type": "some_crystal", "quantity": 1}, 0)
    out = feed(client)
    assert client.portal.call(VirtualQueue(db).orders, "CC00CAFE") == []
    assert out == ["CC00CAFE: dropped 1 kept print(s): it is no general factory",
                   "CC00CAFE: 1 print(s) into the game's queue"]   # the relay dropped, the print by hand sent


def test_a_print_with_its_components_free_there_jumps_a_waiting_one():
    """Snapshot 2026-11-03 23:43: mining rigs waited behind relay stations (waiting for a relay) with 18 drones idle there."""
    bps = {"relay_station": {"device_type": "relay_station", "resources": {"metals": 5}, "components": {"ftl_relay": 1}},
           "rig": {"device_type": "rig", "resources": {"metals": 2}, "components": {"drone": 3}},
           "ftl_relay": {"device_type": "ftl_relay", "resources": {"metals": 1}}}
    cur = {"device_type": "relay_station"}
    rig = {"factory": "F", "body": {"device_type": "rig", "quantity": 1}, "prio": 3, "seq": 1}
    assert vqueue.jump(cur, [], [rig], {"metals": 99}, bps) is None                      # nothing known free
    assert vqueue.jump(cur, [], [rig], {"metals": 99}, bps, free={"drone": 2}) is None   # too few drones
    assert vqueue.jump(cur, [], [rig], {"metals": 99}, bps, free={"drone": 3}) == ("kept", rig)
    other = {"factory": "F", "body": {"device_type": "relay_station", "quantity": 1}, "prio": 3, "seq": 2}
    assert vqueue.jump(cur, [], [other], {"metals": 99}, bps, free={"ftl_relay": 5}) is None   # it wants the same kind


def test_remove_takes_a_kept_print_off_by_its_order(client):  # noqa: F811
    """Joe 2026-11-07: Remove works on the app's queue too, even when the saved device list is out of date."""
    db, e, world = setup(client)
    for t in ("orbital_farm", "hydroponic_bay", "orbital_farm"):
        client.portal.call(e.send, "POST", "/devices/AF00BEEF", {"command": "enqueue_print", "device_type": t, "quantity": 1}, "t")
    world.queues["AF00BEEF"] = [{"device_type": "nutrient_synthesizer", "quantity": 1}]   # the saved list doesn't know it
    dev = client.portal.call(client.app.state.api.get, "/devices/AF00BEEF")
    q = dev["print_queue"]
    assert not q[0].get("virtual") and q[2]["virtual"]
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "remove", "index": 2, "order": q[2]["order"]}, headers=HX)
    assert r.status_code == 200
    left = client.portal.call(VirtualQueue(db).orders, "AF00BEEF")
    assert [o["seq"] for o in left] == [x["order"] for x in q[1:] if x["order"] != q[2]["order"]]
    assert len(world.queues["AF00BEEF"]) == 1                     # the game's print stays
    # the shared app queue on the Blueprints page has its own Remove
    r = client.get("/print-queue/group?codes=AF00BEEF", headers=HX)
    assert "/print-queue/group/remove" in r.text
    r = client.post("/print-queue/group/remove", data={"codes": "AF00BEEF", "factory": "AF00BEEF", "order": left[0]["seq"]},
                    headers=HX)
    assert r.status_code == 200 and len(client.portal.call(VirtualQueue(db).orders, "AF00BEEF")) == 1
    r = client.post("/print-queue/group/remove", data={"codes": "AF00BEEF", "factory": "AF00BEEF", "order": 999}, headers=HX)
    assert "any more" in r.text


def test_moving_a_kept_print_to_the_front_puts_it_in_the_games_queue(client):  # noqa: F811
    """Joe 2026-11-07: "move #3 autofactory to #1 … dequeue failed: Invalid index: 0" (the saved device list was stale)."""
    db, e, world = setup(client)
    world.queues["AF00BEEF"] = [{"device_type": "orbital_farm", "quantity": 1, "tags": ["fleet:m1"]}]
    for t, tags in (("hydroponic_bay", ["stock"]), ("nutrient_synthesizer", ["fleet:m1"])):
        client.portal.call(e.send, "POST", "/devices/AF00BEEF", {"command": "enqueue_print", "device_type": t, "quantity": 1,
                                                                 "tags": tags}, "t")
    client.portal.call(db.kv_set, "devices", [])   # stale: knows nothing of the game's queue
    dev = client.portal.call(client.app.state.api.get, "/devices/AF00BEEF")
    assert [x["device_type"] for x in dev["print_queue"]] == ["orbital_farm", "nutrient_synthesizer", "hydroponic_bay"]
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "top", "index": 2}, headers=HX)
    assert r.status_code == 200 and "Invalid" not in r.text, r.text
    assert [x["device_type"] for x in world.queues["AF00BEEF"]] == ["hydroponic_bay"]
    dev = client.portal.call(client.app.state.api.get, "/devices/AF00BEEF")
    assert [x["device_type"] for x in dev["print_queue"]] == ["hydroponic_bay", "orbital_farm", "nutrient_synthesizer"]
    # the feeder fills the game's queue in that order and doesn't move the stock print back
    feed(client)
    assert [x["device_type"] for x in world.queues["AF00BEEF"]] == ["hydroponic_bay", "orbital_farm"]
    # and moving a kept print up among the kept ones re-ranks it
    client.portal.call(e.send, "POST", "/devices/AF00BEEF", {"command": "enqueue_print", "device_type": "orbital_farm",
                                                             "quantity": 1, "tags": ["stock"]}, "t")
    dev = client.portal.call(client.app.state.api.get, "/devices/AF00BEEF")
    q = [x["device_type"] for x in dev["print_queue"]]
    assert q == ["hydroponic_bay", "orbital_farm", "nutrient_synthesizer", "orbital_farm"], q
    r = client.post("/devices/AF00BEEF/print-queue", data={"action": "up", "index": 3}, headers=HX)
    assert r.status_code == 200
    dev = client.portal.call(client.app.state.api.get, "/devices/AF00BEEF")
    assert [x.get("tags") for x in dev["print_queue"]][2:] == [["stock"], ["fleet:m1"]]
