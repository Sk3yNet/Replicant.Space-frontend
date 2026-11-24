import pytest
from rsweb import vqueue
from rsweb.vqueue import VirtualQueue
from test_components import BPS
from test_ops import client  # noqa: F401  (fixture)

@pytest.fixture
def keep2(monkeypatch):
    """These tests were written when each game queue held two waiting prints (KEEP 2 before 1.499.0): the mechanics
    they check don't depend on the number."""
    monkeypatch.setattr(vqueue, "KEEP", 2)


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


@pytest.mark.usefixtures("keep2")
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


@pytest.mark.usefixtures("keep2")
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
    assert vqueue.priority({"device_type": "ftl_relay", "tags": ["to:a", "rollout"]}) == 5   # after everything else


@pytest.mark.usefixtures("keep2")
def test_rollout_relays_wait_behind_every_other_print(client):  # noqa: F811
    db, e, world = setup(client)
    client.portal.call(db.kv_set, "relay_rollout", {"on": True, "ordered": {"KELMORNEA": "2026-11-16T00:00:00Z"}})
    for body in ({"device_type": "ftl_relay", "tags": ["to:kelmornea"]},            # queued by the rollout before 1.444
                 {"device_type": "ftl_relay", "tags": ["to:vega", "rollout"]},
                 {"device_type": "ftl_relay", "tags": ["to:sol"]},                  # a relay ordered by hand
                 {"device_type": "orbital_farm", "tags": ["spare", "stock"]}):
        ok, _, _ = client.portal.call(e.send, "POST", "/devices/AF00BEEF",
                                      {"command": "enqueue_print", "quantity": 1, **body}, "t")
        assert ok
    feed(client)
    assert [x["tags"] for x in world.queues["AF00BEEF"]] == [["to:sol"], ["spare", "stock"]]
    kept = client.portal.call(VirtualQueue(db).orders, "AF00BEEF")
    assert [o["prio"] for o in kept] == [5, 5] and all("rollout" in o["body"]["tags"] for o in kept)



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
    assert any("4 hydroponic bay for the prints waiting there: 4 queued to print" in x for x in out) \
        and any("2 orbital farm for the prints waiting there: 2 queued to print" in x for x in out), out
    kept = client.portal.call(VirtualQueue(db).orders, "AF00BEEF")
    assert sorted((o["body"]["device_type"], o["body"]["quantity"]) for o in kept) == [("hydroponic_bay", 4), ("orbital_farm", 2)]
    sync_queue(client)
    client.portal.call(db.kv_set, "components_last", None)
    out = client.portal.call(e.component_pass)
    assert not any("for the prints waiting there" in x for x in out), out   # counted as coming now


def test_jump_rules():
    bps = {b["device_type"]: b for b in BPS}
    cur = {"device_type": "biosphere_cultivator"}
    bay = {"device_type": "hydroponic_bay", "tags": ["x"]}
    # a component the waiting print needs may use its reserve
    assert vqueue.jump(cur, [bay], [], {"carbon": 6, "volatiles": 2}, bps) == ("real", bay)
    assert vqueue.jump({"device_type": "orbital_farm"}, [bay], [], {"carbon": 4, "volatiles": 2}, bps) is None
    assert vqueue.jump(cur, [{"device_type": "biosphere_cultivator", "tags": ["x"]}], [], {"carbon": 99}, bps) is None


@pytest.mark.usefixtures("keep2")
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


@pytest.mark.usefixtures("keep2")
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


def test_every_general_factory_at_a_spot_shares_one_queue_and_refusals_are_noted(client):  # noqa: F811
    """Joe 2026-11-20: autofactories, orbital foundries and structural fabricators at one spot share the app queue; a
    print one kind refuses is noted and handed to another kind, and that kind isn't offered it again."""
    db, e, world = setup(client)
    af = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    world.devices.append({**af, "device_code": "F0F0F0F0", "device_type": "orbital_foundry",
                          "available_commands": ["enqueue_print", "dequeue_print"], "status": "idle"})
    world.queues["F0F0F0F0"] = []
    world.print_refuse["autofactory"] = {"orbital_farm"}
    for _ in range(3):
        ok, _, _ = client.portal.call(e.send, "POST", "/devices/AF00BEEF",
                                      {"command": "enqueue_print", "device_type": "orbital_farm", "quantity": 1}, "t")
        assert ok
    feed(client)
    ref = client.portal.call(db.kv_get, vqueue.REFUSE_KV)
    assert "orbital_farm" in ref["autofactory"], ref
    log = client.portal.call(db.kv_get, "automation_log")
    assert any("refused a orbital farm print" in x["text"] for x in log)
    left = client.portal.call(VirtualQueue(db).orders)
    assert left and all(o["factory"] == "F0F0F0F0" for o in left), left
    feed(client)   # the foundry takes them; the autofactory isn't offered them again
    assert [x["device_type"] for x in world.queues["F0F0F0F0"]] == ["orbital_farm"], world.queues
    assert not world.queues["AF00BEEF"]
    assert [o["factory"] for o in client.portal.call(VirtualQueue(db).orders)] == ["F0F0F0F0"]
    from rsweb.loadouts import queue_pool
    fab = {"device_code": "C4522788", "device_type": "structural_fabricator", "location": "X-1", "features": ["cruise", "print"],
           "available_commands": ["enqueue_print"], "queue_size": 0}
    assert queue_pool(fab) == queue_pool({**af, "location": "X-1"}) == ("X-1", "general")


def test_prints_split_by_hand_stay_on_their_factories(client):  # noqa: F811
    """Live 2026-11-17: 10 matrices planned 2 each on 5 autofactories went 4/4/2/0/0, the first factories fed taking
    their idle neighbors' orders. A neighbor's order is taken only once that neighbor's game queue is full."""
    db, e, world = setup(client)
    af = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    codes = ["AF00BEEF"] + [f"AF0{i}CAFE" for i in range(4)]
    for c in codes[1:]:
        world.devices.append({**af, "device_code": c})
        world.queues[c] = []
    client.portal.call(client.app.state.worker.sync_devices)
    for c in codes:
        ok, _, _ = client.portal.call(e.send, "POST", f"/devices/{c}",
                                      {"command": "enqueue_print", "device_type": "orbital_farm", "quantity": 2}, "t")
        assert ok
    feed(client)
    assert all(world.queues[c] for c in codes), world.queues


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
    assert out == ["CC00CAFE: dropped 1 kept print(s) it can't make (exotic blueprints only)",
                   "CC00CAFE: 1 print(s) into the game's queue"]   # the relay dropped, the print by hand sent


def test_epitaxy_printers_take_exotic_blueprints_only(client):  # noqa: F811
    """Joe 2026-11-12: printers with the epitaxy feature are limited to blueprints with the exotic feature (1476F94F:
    "This printer can only fabricate epitaxy-class devices"), and get their own card on the Blueprints page."""
    from rsweb import loadouts as lo
    from rsweb.api import ApiError
    lab = {"device_code": "CC00CAFE", "device_type": "crystal_synthesis_lab", "features": ["cruise", "print", "epitaxy"],
           "available_commands": ["enqueue_print", "dequeue_print"], "location": "DELTA-OBJ-2", "status": "idle"}
    seed = {"device_type": "crystal_seed", "features": ["stow", "exotic", "seed"], "resources": {"metals": 1}, "print_time": 60}
    relay = {"device_type": "ftl_relay", "features": ["relay"], "resources": {"metals": 1}}
    assert lo.can_make(lab, seed) and not lo.can_make(lab, relay) and not lo.can_make(lab, None)
    assert lo.can_make({"device_type": "autofactory"}, seed) and lo.can_make({"device_type": "autofactory"}, relay)
    barge = {"device_type": "resonance_barge", "features": []}   # grown by epitaxial deposition (Joe 2026-11-18)
    assert lo.can_make(lab, barge) and lo.is_class_bp(barge)
    assert not lo.can_make({"device_type": "autofactory"}, barge)
    db, e, world = setup(client)
    client.portal.call(db.kv_set, "blueprints", BPS + [seed])
    world.devices.append(lab)
    world.queues["CC00CAFE"] = []
    vq = client.app.state.api.vq
    # an exotic print kept for it by an automation stays and is fed; a hand print of anything else is refused up front
    client.portal.call(vq.add, "CC00CAFE", {"device_type": "crystal_seed", "quantity": 1}, 2)
    out = feed(client)
    assert out == ["CC00CAFE: 1 print(s) into the game's queue"] and client.portal.call(vq.orders, "CC00CAFE") == []
    client.portal.call(db.kv_set, "devices", client.portal.call(db.kv_get, "devices") + [lab])
    try:
        client.portal.call(vq.intercept, "/devices/CC00CAFE", {"command": "enqueue_print", "device_type": "orbital_farm"})
        raise AssertionError("not refused")
    except ApiError as err:
        assert "exotic" in err.message
    # the Blueprints page: its own card with only exotic blueprints, and it's no choice in the general Plan
    r = client.get("/blueprints", headers={"X-Auth-Request-Email": "joe@example.com"})
    card = r.text.split('id="epitaxy-printers"')[1].split('<h2>Queue a print')[0]
    assert "Epitaxy printers" in card and 'value="crystal_seed"' in card and 'value="orbital_farm"' not in card
    plan = r.text.split("<h2>Plan</h2>")[1].split("</select>")[0]
    assert "CC00CAFE" not in plan


def test_planner_sends_exotic_prints_to_an_epitaxy_printer_and_nothing_else():
    from rsweb import loadouts as lo
    from test_core import _lo_world
    cfg, devices, bps, inv, stars = _lo_world()
    devices.append({"device_code": "LAB", "device_type": "crystal_synthesis_lab", "features": ["print", "epitaxy"],
                    "available_commands": ["enqueue_print"], "location": "AAA-3-L4", "status": "idle"})
    bps.append({"device_type": "crystal_seed", "features": ["exotic"], "resources": {"structural": 10}, "print_time": 60})
    cfg["phases"][0]["wants"]["crystal_seed"] = 1
    p = lo.plan(cfg, devices, bps, inv, stars, {"HV": "R1"}, set(), [], {})
    on = {(pr["factory"], pr["device_type"]) for pr in p["prints"]}
    assert ("LAB", "crystal_seed") in on and ("AF", "crystal_seed") not in on, on
    assert not any(f == "LAB" and t != "crystal_seed" for f, t in on) and ("AF", "survey_drone") in on, on

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


@pytest.mark.usefixtures("keep2")
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


def test_clear_wipes_a_groups_queue_and_the_orders_behind_it(client):  # noqa: F811
    """Joe 2026-11-17: Clear queue per group (131 relays queued at FALQUORYX-BELT-1); the relay orders behind the
    prints are dropped and the rollout skips their systems, so nothing prints them straight back."""
    db, e, world = setup(client)
    for star in ("AAA", "BBB"):
        client.portal.call(e.send, "POST", "/devices/AF00BEEF",
                           {"command": "enqueue_print", "device_type": "ftl_relay", "quantity": 1, "tags": [f"to:{star.lower()}", "rollout"]}, "t")
    client.portal.call(db.kv_set, "relay_requests", {"AAA": {"how": "print", "factory": "AF00BEEF"},
                                                    "BBB": {"how": "print", "factory": "AF00BEEF"},
                                                    "CCC": {"how": "send", "device": "X"}})
    client.portal.call(db.kv_set, "relay_rollout", {"on": True, "ordered": {"AAA": "t", "CCC": "t"}, "skipped": {}})
    world.queues["AF00BEEF"] = [{"device_type": "nutrient_synthesizer", "quantity": 1}]
    r = client.get("/print-queue/group?codes=AF00BEEF", headers=HX)
    assert "Clear app queue (2)" in r.text and "Clear game queues too (3)" in r.text
    r = client.post("/print-queue/group/clear", data={"codes": "AF00BEEF", "scope": "app"}, headers=HX)
    assert r.status_code == 200
    assert client.portal.call(VirtualQueue(db).orders, "AF00BEEF") == []
    assert len(world.queues["AF00BEEF"]) == 1                    # app queue only: the game's print stays
    assert set(client.portal.call(db.kv_get, "relay_requests")) == {"CCC"}
    roll = client.portal.call(db.kv_get, "relay_rollout")
    assert set(roll["ordered"]) == {"CCC"} and "AAA" in roll["skipped"]
    r = client.post("/print-queue/group/clear", data={"codes": "AF00BEEF", "scope": "all"}, headers=HX)
    assert r.status_code == 200 and world.queues["AF00BEEF"] == []


def test_prints_kept_for_a_compacted_factory_go_to_a_working_neighbor(client):  # noqa: F811
    """Live 2026-11-17: 7C5D7A36, compacted at SOLARES-BELT-1 as a contract's payment, held two contract prints while
    its neighbors stood idle."""
    import copy
    from rsweb import contractsupply, loadouts
    db, e, world = setup(client)
    src = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    box = copy.deepcopy(src)
    box.update(device_code="AF00C0DE", status="compacted", tags=["contract:rastuban-2-evt-003", "to:rastuban"])
    world.devices.append(box)
    world.queues["AF00C0DE"] = []
    assert not loadouts.in_service(box) and loadouts.in_service(src)
    vq = VirtualQueue(db)
    client.portal.call(vq.add, "AF00C0DE", {"device_type": "orbital_farm", "quantity": 1, "tags": ["to:sol"]}, 3)
    out = feed(client)
    assert any("AF00C0DE can't print now" in x for x in out), out
    assert client.portal.call(vq.orders, "AF00C0DE") == []
    assert world.queues["AF00C0DE"] == []
    # and no contract print picks it in the first place
    f, _ = contractsupply.factory_for("orbital_farm", 1, [box], "SOL", set(), {}, {}, {}, 0, {})
    assert f is None


def test_queueless_lab_gets_one_print_at_a_time(client):  # noqa: F811
    """Joe 2026-11-18: crystal synthesis labs (queue_size 0) answered "Printer is busy" to crystal_seed x3 every pass,
    idle as they were. The feeder sends them one print, only while they hold none."""
    lab = {"device_code": "CC00CAFE", "device_type": "crystal_synthesis_lab", "features": ["cruise", "print", "epitaxy"],
           "available_commands": ["enqueue_print", "dequeue_print"], "location": "DELTA-OBJ-2", "status": "idle",
           "queue_size": 0}
    seed = {"device_type": "crystal_seed", "features": ["stow", "exotic", "seed"], "resources": {"metals": 1}, "print_time": 60}
    db, e, world = setup(client)
    client.portal.call(db.kv_set, "blueprints", BPS + [seed])
    world.devices.append(lab)
    world.queues["CC00CAFE"] = []
    vq = client.app.state.api.vq
    client.portal.call(vq.add, "CC00CAFE", {"device_type": "crystal_seed", "quantity": 3}, 0)
    out = feed(client)
    assert "CC00CAFE: 1 crystal_seed into the game (no queue: one at a time)" in out
    assert [o["body"]["quantity"] for o in client.portal.call(vq.orders, "CC00CAFE")] == [2]
    # while it holds that print nothing more is sent (no "busy" refusals)
    e._vq_fed = {}
    lab["status"] = "printing (crystal_seed)"
    out = feed(client)
    assert not any("CC00CAFE" in x for x in out)
    assert [o["body"]["quantity"] for o in client.portal.call(vq.orders, "CC00CAFE")] == [2]


def test_next_up_leaves_out_the_waiting_print():
    """Owner 2026-11-19: 9E13498F waiting on components showed its one orbital foundry as current and as next up."""
    from rsweb import printqueue as pq
    dev = {"status": "waiting_for_resources", "print_queue": [{"device_type": "orbital_foundry"}]}
    cur = {"state": "waiting", "device_type": "orbital_foundry"}
    assert pq.after_current(cur, pq.items(dev)) == []
    dev["print_queue"] = [{"device_type": "ftl_relay", "quantity": 3}, {"device_type": "ftl_beacon"}]
    rest = pq.after_current(cur, pq.items(dev))
    assert [(x["device_type"], x["quantity"], x.get("rest_of_current", False)) for x in rest] == \
        [("ftl_relay", 2, True), ("ftl_beacon", None, False)]
    # a printing factory's current print isn't in the game's list: nothing is left out
    assert len(pq.after_current({"state": "printing"}, pq.items(dev))) == 2
    assert vqueue.head_waits(dev) == 1 and vqueue.head_waits({"status": "printing (x)", "print_queue": [{}]}) == 0


def test_game_queue_holds_one_behind_the_current_print():
    real = [{"device_type": "a", "tags": []}]
    kept = [{"prio": 3, "seq": 1, "body": {"device_type": "b"}}]
    assert vqueue.KEEP == 1
    assert vqueue.plan(real, kept) == (1, [], [])                       # printing: one waiting behind it is enough
    k, back, send = vqueue.plan(real, kept, keep=vqueue.KEEP + 1)       # waiting: the head is the current print
    assert k == 1 and [x[0] for x in send] == ["kept"]


def test_a_refusal_about_the_printers_state_teaches_nothing(client):  # noqa: F811
    """Live 2026-11-22: 11C05A70 at NIO-3-L4 answered "Device is unfurling" to a beacon print, noted as autofactories
    refusing beacons: nine beacon prints then sat in the app's queue while all five NIO autofactories stood idle."""
    db, e, world = setup(client)
    client.portal.call(db.kv_set, vqueue.REFUSE_KV, {"autofactory": {
        "ftl_beacon": {"err": "Device is unfurling", "at": "2026-11-22T23:43:23+00:00", "code": "AF00BEEF"},
        "orbital_farm": {"err": "This printer can't make that", "at": "2026-11-22T23:43:23+00:00", "code": "AF00BEEF"}}})
    next(d for d in world.devices if d["device_code"] == "AF00BEEF")["status"] = "idle"
    vq = client.app.state.api.vq
    client.portal.call(vq.add, "AF00BEEF", {"device_type": "ftl_beacon", "quantity": 1, "tags": ["civ"]}, 2)
    out = feed(client)
    assert out == ["AF00BEEF: 1 print(s) into the game's queue"] and client.portal.call(vq.orders, "AF00BEEF") == []
    assert client.portal.call(db.kv_get, vqueue.REFUSE_KV) == {"autofactory": {"orbital_farm": {
        "err": "This printer can't make that", "at": "2026-11-22T23:43:23+00:00", "code": "AF00BEEF"}}}
    assert vqueue.TRANSIENT_RE.search("Device is unfurling") and vqueue.TRANSIENT_RE.search("Device is compacting")


def test_prints_kept_for_a_printer_that_is_gone_are_dropped_after_an_hour(client):  # noqa: F811
    """Live 2026-11-24: 8 relay prints kept since 11-17 for structural fabricator 94700A36, no longer in the list."""
    db, e, world = setup(client)
    vq = client.app.state.api.vq
    client.portal.call(vq.add, "DEADBEEF", {"device_type": "ftl_relay", "quantity": 1, "tags": ["to:sol"]}, 2)
    feed(client)
    st = client.portal.call(vq.state)
    assert len(st["orders"]) == 1 and "DEADBEEF" in st["gone"]          # noted, kept for now
    st["gone"]["DEADBEEF"] = "2026-01-01T00:00:00+00:00"
    client.portal.call(db.kv_set, vqueue.KV, st)
    out = feed(client)
    assert "DEADBEEF: dropped 1 kept print(s): that printer is gone" in out
    st = client.portal.call(vq.state)
    assert st["orders"] == [] and st["gone"] == {}


def test_civ_beacon_prints_waiting_in_a_system_are_counted():
    from rsweb.ops_rules import civ_prints
    devs = [{"device_code": "A1", "location": "NIO-3-L4", "print_queue": [
                {"device_type": "ftl_beacon", "quantity": 1, "tags": ["civ"]},
                {"device_type": "ftl_beacon", "quantity": 1, "tags": ["civ"], "virtual": True}]},
            {"device_code": "A2", "location": "NIO-3-L4"}]
    kept = [{"factory": "A1", "seq": 5, "body": {"device_type": "ftl_beacon", "tags": ["civ"]}},
            {"factory": "A2", "seq": 9, "body": {"device_type": "ftl_beacon", "tags": ["civ"]}},
            {"factory": "A2", "seq": 7, "body": {"device_type": "ftl_relay", "tags": ["civ"]}},
            {"factory": "ZZ", "seq": 8, "body": {"device_type": "ftl_beacon", "tags": ["civ"]}}]
    q = civ_prints(devs, kept)
    assert list(q) == ["NIO"] and q["NIO"]["game"] == 1 and [o["seq"] for o in q["NIO"]["kept"]] == [9, 5]


def test_a_contracts_printer_is_no_destination_for_prints(client):  # noqa: F811
    """Joe 2026-11-24: structural fabricator 94700A36 was a contract print and still took relay prints."""
    from rsweb import loadouts, ward_orders
    db, e, world = setup(client)
    src = next(d for d in world.devices if d["device_code"] == "AF00BEEF")
    goods = dict(src, device_code="AF00C0DE", tags=["contract:rastuban-2-evt-003"], status="idle")
    assert loadouts.is_factory(src) and not loadouts.is_factory(goods)
    assert not ward_orders.factory_near([goods], "SOL", set(), {}, 9999)
    world.devices.append(goods)
    world.queues["AF00C0DE"] = []
    out = feed(client)   # the device list now has it
    ok, resp, _ = client.portal.call(e.send, "POST", "/devices/AF00C0DE",
                                     {"command": "enqueue_print", "device_type": "orbital_farm", "quantity": 1}, "t")
    assert ok and resp["virtual"]                                       # kept in the app, never the game's
    out = feed(client)
    assert any("AF00C0DE can't print now" in x for x in out), out
    assert client.portal.call(client.app.state.api.vq.orders, "AF00C0DE") == [] and world.queues["AF00C0DE"] == []
