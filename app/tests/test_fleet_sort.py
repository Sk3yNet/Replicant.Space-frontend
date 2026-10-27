"""Fleets sort themselves out (owner 2026-10-27): other fleets' devices are released, surplus deliveries are let go
where they wait, and an unstationed fleet's extras are dropped as spares when idle at a hub."""
from rsweb import fleets as fl
from rsweb import loadouts as lo
from rsweb.automations import AutomationEngine as Engine


def D(code, t, loc, tags=(), **kw):
    return {"device_code": code, "device_type": t, "location": loc, "status": "idle", "tags": list(tags), **kw}


def test_other_fleets_devices_are_released():
    devices = [D("C9", "ami_mining_controller", "SOL-BELT-1", ["fleet:m9"], features=["ami"], status="coordinating"),
               D("C8", "ami_mining_controller", "SOL-BELT-1", ["fleet:m8"], features=["ami"], status="coordinating"),
               D("A1", "mining_drone", "SOL-BELT-1", ["fleet:m8"], controller_device_code="C9", status="mining (carbon)"),
               D("A2", "mining_drone", "SOL-BELT-1", ["fleet:m9"], controller_device_code="C9", status="mining (carbon)"),
               D("A3", "mining_drone", "SOL-BELT-1", [], controller_device_code="C9", status="mining (carbon)")]
    w = {"ami_mining_controller": 1, "mining_drone": 2}
    cfg = {"fleets": [{"id": "m8", "name": "M8", "role": "mining", "home": "SOL", "station": True, "wants": w},
                      {"id": "m9", "name": "M9", "role": "mining", "home": "SOL", "station": True, "wants": w}]}
    p = lo.plan(cfg, devices, [], {}, {}, {}, set(), [], {})
    assert p["releases"].get("C9") == ["A1"]           # only the other fleet's drone; its own and fleetless ones stay
    rows = lo.audit(cfg, devices, {}, p)
    assert any(r["code"] == "A1" and r["fixed"] and "another" not in r["issue"] for r in rows)
    assert lo.plan(cfg, devices, [], {}, {}, {}, {"A1"}, [], {})["releases"].get("C9") is None   # busy: left alone


def test_surplus_on_its_way_home_is_let_go_where_it_waits():
    home = [D(f"H{i}", "mining_drone", "ANL-BELT-1", ["fleet:df"], status="mining (rares)") for i in range(7)]
    waiting = [D(f"W{i}", "mining_drone", "ISO-BELT-1", ["fleet:df", "to:anl"]) for i in range(4)]
    cfg = {"fleets": [{"id": "df", "name": "DF", "role": "mining", "home": "ANL", "station": True,
                       "wants": {"mining_drone": 8}}]}
    stars = {"ANL": {"position": {"x": 0, "y": 0, "z": 0}}, "ISO": {"position": {"x": 1, "y": 0, "z": 0}}}
    p = lo.plan(cfg, home + waiting, [], {}, stars, {}, set(), [], {})
    let_go = sorted(c for c, t in p["tag_add"].items() if "spare" in t)
    assert let_go == ["W0", "W1", "W2"]                 # one of the four still goes: 7 + 1 = 8
    assert all({"fleet:df", "to:anl"} <= set(p["tag_remove"][c]) for c in let_go)
    assert "W3" in p["moves"] and not set(let_go) & set(p["moves"])


def _logistics():
    f = {"id": "lg", "name": "Logistics", "role": "logistics", "home": "FAL", "mission": {},
         "wants": {"maintenance_drone": 1, "heaven_vessel": 1}, "zero": ["cargo_vessel"]}
    devices = [
        D("HUB", "system_hub", "FAL-1-L4", ["fleet:hub"], status="relaying"),
        D("MD1", "maintenance_drone", "FAL-BELT-1", ["fleet:lg"], operational_capacity=90.0),
        D("MD2", "maintenance_drone", None, ["fleet:lg"], status="stowed", stowed_in_device_code="CV1",
          operational_capacity=60.0),
        D("MD3", "maintenance_drone", "XYZ-BELT-1", ["fleet:lg"], operational_capacity=10.0),   # not at a hub
        D("CV1", "cargo_vessel", "FAL-1-L4", ["fleet:lg"]),                                     # carries MD2: kept
        D("HV1", "heaven_vessel", "FAL-1-L4", ["fleet:lg"], hosting_replicant={"name": "A", "replicant_code": "R1"}),
        D("HV2", "heaven_vessel", "FAL-1-L4", ["fleet:lg"], hosting_replicant={"name": "B", "replicant_code": "R2"}),
        D("CV2", "cargo_vessel", "FAL-1-L4", ["fleet:lg", "contract:abc"]),                    # reserved
        D("MC1", "matrix_container", "FAL-1-L4", ["fleet:lg"], features=["cradle"]),         # blank: don't care
    ]
    return f, devices


def test_extras_drop_at_a_hub_and_spare_the_protected():
    f, devices = _logistics()
    hubs = Engine.hub_stars(devices)
    assert hubs == {"FAL"}
    ex = {d["device_code"] for d in fl.extras(f, devices, set(), hubs)}
    assert ex == {"MD1", "MD2"}    # 3 drones, 1 wanted: the ones at a hub go first (most worn first); vessels kept
    drop = Engine.droppable_extras(f, devices, set(), hubs, set())
    assert [d["device_code"] for d in drop] == ["MD2", "MD1"]
    bodies = [(s["path"], s["body"]) for s in fl.drop_steps(f, drop)]
    assert bodies == [("/devices/MD2", {"command": "deploy"}),
                      ("/devices/MD2", {"configuration": {"add_tags": ["spare"], "remove_tags": ["fleet:lg"]}}),
                      ("/devices/MD1", {"configuration": {"add_tags": ["spare"], "remove_tags": ["fleet:lg"]}})]
    # its carrier on the way somewhere, or the drone in a job: not now
    moving = [dict(d, status="travelling") if d["device_code"] == "CV1" else d for d in devices]
    assert [d["device_code"] for d in Engine.droppable_extras(f, moving, set(), hubs, set())] == ["MD1"]
    assert [d["device_code"] for d in Engine.droppable_extras(f, devices, {"MD2"}, hubs, set())] == ["MD1"]
    assert fl.kept_reason(next(d for d in devices if d["device_code"] == "MC1"), devices) == "holds a replicant or matrix"
