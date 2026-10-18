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
    client.portal.call(e.send, "POST", "/devices/AF00BEEF",
                       {"command": "enqueue_print", "device_type": "biosphere_cultivator", "quantity": 1,
                        "tags": ["contract:x-1", "to:kamuyis", "at:kamuyis-4"]}, "test")
    q = client.app.state.api.http._transport.app.state.world.queues["AF00BEEF"]
    assert [x["device_type"] for x in q][-4:] == ["hydroponic_bay", "hydroponic_bay", "orbital_farm", "biosphere_cultivator"]
    assert q[-4]["tags"] == ["contract:x-1", "to:sol", "at:sol-3-l4"] and "at:kamuyis-4" in q[-1]["tags"]
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
    q = client.app.state.api.http._transport.app.state.world.queues["AF00BEEF"]   # not the blocked factory
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
