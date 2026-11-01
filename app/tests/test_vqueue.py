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
    """Owner 2026-11-01: while the current print waits, one that doesn't use what it needs goes ahead; it is canceled
    and queued right behind."""
    db, e, world = setup(client)
    af = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    af.update(status="waiting_for_resources", printing={"device_type": "orbital_farm", "tags": ["fleet:m1"]})
    world.queues["AF00BEEF"] = [{"device_type": "biosphere_cultivator", "notify": {"device": None}, "tags": ["fleet:m2"]},
                                {"device_type": "hydroponic_bay", "notify": {"device": None}, "tags": ["fleet:m2"]}]
    # the farm waits for structural; the cultivator needs components of its own; the bay uses carbon and volatiles
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-3-L4", "items": {"carbon": 12, "structural": 5,
                                                                                     "volatiles": 5}}])
    out = feed(client)
    assert any("hydroponic_bay goes ahead of the waiting orbital_farm" in x for x in out), out
    assert af["status"].startswith("printing (hydroponic_bay)")        # the bay started at once
    assert [x["device_type"] for x in world.queues["AF00BEEF"]] == ["orbital_farm"]
    assert world.queues["AF00BEEF"][0]["tags"] == ["fleet:m1"]
    kept = client.portal.call(VirtualQueue(db).orders, "AF00BEEF")
    assert [o["body"]["device_type"] for o in kept] == ["biosphere_cultivator"]
    # nothing fits: the waiting print stays
    af.update(status="waiting_for_resources", printing={"device_type": "orbital_farm", "tags": []})
    world.queues["AF00BEEF"] = [{"device_type": "hydroponic_bay", "notify": {"device": None}, "tags": ["fleet:m2"]}]
    client.portal.call(db.kv_set, "inventory", [{"location": "SOL-3-L4", "items": {"carbon": 2}}])
    e._vq_jumped = {}
    out = feed(client)
    assert not any("goes ahead" in x for x in out), out
    assert af["printing"]["device_type"] == "orbital_farm"


def test_jump_rules():
    bps = {b["device_type"]: b for b in BPS}
    cur = {"device_type": "biosphere_cultivator"}
    bay = {"device_type": "hydroponic_bay", "tags": ["x"]}
    # a component the waiting print needs may use its reserve
    assert vqueue.jump(cur, [bay], [], {"carbon": 6, "volatiles": 2}, bps) == ("real", bay)
    assert vqueue.jump({"device_type": "orbital_farm"}, [bay], [], {"carbon": 4, "volatiles": 2}, bps) is None
    assert vqueue.jump(cur, [{"device_type": "biosphere_cultivator", "tags": ["x"]}], [], {"carbon": 99}, bps) is None
