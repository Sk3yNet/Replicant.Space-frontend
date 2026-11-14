from rsweb import reroute as rr
from test_ops import client  # noqa: F401  (fixture)

POS = {"SOLARES": {"x": 0, "y": 0, "z": 0}, "FALQUORYX": {"x": 500, "y": 0, "z": 0},
       "ISOLITHOR": {"x": 29000, "y": 0, "z": 0}}
BPS = {"bio_seeder": {"device_type": "bio_seeder", "resources": {"volatiles": 1450, "carbon": 100}, "print_time": 600},
       "small_thing": {"device_type": "small_thing", "resources": {"volatiles": 300}, "print_time": 60}}
FLEETS = [{"id": "sol-hub", "home": "SOLARES", "station": True, "materials": "self"},
          {"id": "falq-hub", "home": "FALQUORYX", "station": True, "materials": "self"},
          {"id": "delta-hub", "home": "ISOLITHOR", "station": True, "materials": "self"},
          {"id": "terraformer-1", "home": "SOLARES", "role": "terraformer"}]


def fac(code, loc, queue, fleet, status="waiting_for_resources"):
    return {"device_code": code, "device_type": "autofactory", "location": loc, "status": status,
            "tags": [f"fleet:{fleet}"], "print_queue": queue, "available_commands": ["enqueue_print", "dequeue_print"]}


SEEDER = {"device_type": "bio_seeder", "tags": ["fleet:terraformer-1"], "notify": {"device": None}}


def world(sol_vol=690, falq_vol=10000, extra=()):
    devs = [fac("SOL00001", "SOLARES-BELT-1", [dict(SEEDER), dict(SEEDER)], "sol-hub"),
            fac("FAL00001", "FALQUORYX-BELT-1", [], "falq-hub", "idle")] + list(extra)
    inv = {"SOLARES-BELT-1": {"volatiles": sol_vol, "carbon": 5000}, "FALQUORYX-BELT-1": {"volatiles": falq_vol, "carbon": 5000}}
    return devs, inv


def test_a_print_short_of_volatiles_moves_to_the_hub_that_has_them():
    devs, inv = world()
    out = rr.plan(devs, BPS, inv, POS, FLEETS, 1728)
    assert [(m["factory"], m["to"], m["dest"]) for m in out] == [("SOL00001", "FAL00001", "SOLARES")] * 2
    first = out[0]
    assert first["lack"] == {"volatiles": 760} and first["loads"] == 2 and first["device_ly"] == 500 and first["materials_ly"] == 1000
    assert "bio seeder for SOLARES" in rr.describe(first) and "2 freighter load(s)" in rr.describe(first)


def test_a_print_one_load_short_stays_and_so_does_one_the_donor_cannot_pay():
    devs, inv = world(sol_vol=1100)   # 350 short: one load, the device would fly as far
    assert rr.plan(devs, BPS, inv, POS, FLEETS, 1728) == []
    devs, inv = world(falq_vol=1000)  # the other hub can't pay for it either
    assert rr.plan(devs, BPS, inv, POS, FLEETS, 1728) == []
    devs, inv = world(falq_vol=2000)  # it can pay for one; the second stays
    assert len(rr.plan(devs, BPS, inv, POS, FLEETS, 1728)) == 1


def test_the_donor_keeps_what_its_own_queue_needs():
    devs, inv = world(falq_vol=3000)
    devs[1]["print_queue"] = [dict(SEEDER, tags=["fleet:falq-hub"])]
    assert len(rr.plan(devs, BPS, inv, POS, FLEETS, 1728)) == 1   # 3000 - 1450 left: one more


def test_sector_range_components_contracts_and_untagged_prints_stay():
    far = fac("DEL00001", "ISOLITHOR-BELT-1", [], "delta-hub", "idle")
    devs, inv = world(falq_vol=0)
    inv["ISOLITHOR-BELT-1"] = {"volatiles": 99999, "carbon": 99999}
    assert rr.plan(devs + [far], BPS, inv, POS, FLEETS, 1728) == []   # 29,000 ly: beyond the sector range
    devs, inv = world()
    devs[0]["print_queue"] = [{"device_type": "bio_seeder", "tags": ["component", "at:solares-belt-1"]},
                              {"device_type": "bio_seeder", "tags": ["contract:abc"]},
                              {"device_type": "bio_seeder", "tags": []}]
    assert rr.plan(devs, BPS, inv, POS, FLEETS, 1728) == []
    # another fleet's own factory isn't lent; unknown positions are left alone
    devs, inv = world()
    devs[1]["tags"] = ["fleet:terraformer-1"]
    assert rr.plan(devs, BPS, inv, POS, FLEETS, 1728) == []
    devs, inv = world()
    assert rr.plan(devs, BPS, inv, {"SOLARES": POS["SOLARES"]}, FLEETS, 1728) == []


def test_a_to_tag_is_the_destination():
    devs, inv = world()
    devs[0]["print_queue"] = [{"device_type": "bio_seeder", "tags": ["spare", "to:falquoryx"]}]
    out = rr.plan(devs, BPS, inv, POS, FLEETS, 1728)
    assert out and out[0]["dest"] == "FALQUORYX" and out[0]["device_ly"] < 0   # printed where it goes


def test_the_pass_queues_on_the_stocked_hub_then_takes_the_print_off(client):  # noqa: F811
    db, e = client.app.state.db, client.app.state.worker.automations
    w = client.app.state.api.http._transport.app.state.world
    devs, inv = world()
    for d in devs:
        w.devices.append({k: v for k, v in d.items() if k != "print_queue"})
    w.queues["SOL00001"] = [dict(SEEDER), dict(SEEDER)]
    w.queues["FAL00001"] = []
    client.portal.call(db.kv_set, "blueprints", list(BPS.values()))
    client.portal.call(db.kv_set, "inventory", [{"location": k, "items": v} for k, v in inv.items()])
    client.portal.call(db.kv_set, "devices", devs)
    client.portal.call(db.kv_set, "loadout_orders", [{"factory": "SOL00001", "device_type": "bio_seeder", "fleet": "terraformer-1"}])

    async def stars(kind):
        return POS

    async def fleets():
        return FLEETS
    e.star_lookup, e.fleets = stars, fleets
    s = client.portal.call(e.settings)
    s["rules"]["print_reroute"] = {"enabled": True}
    s["dry_run"] = False
    client.portal.call(e.save_settings, s)
    out = client.portal.call(e.print_reroute_pass)
    assert len(out) == 2, out
    assert w.queues["SOL00001"] == []
    fal = next(d for d in w.devices if d["device_code"] == "FAL00001")
    assert len(w.queues["FAL00001"]) + str(fal.get("status")).startswith("printing") == 2, (w.queues["FAL00001"], fal)
    assert all("fleet:terraformer-1" in q.get("tags", []) for q in w.queues["FAL00001"])
    assert client.portal.call(db.kv_get, "loadout_orders")[0]["factory"] == "FAL00001"
    assert client.portal.call(e.print_reroute_pass) == []   # not again within its interval
