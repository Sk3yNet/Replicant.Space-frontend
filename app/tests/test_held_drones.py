"""A fleet's drones run by a controller outside it (Surveyor 6, 2026-11-12: 8 survey drones stayed under the old
controller E085567D, made spare at SOLARES, while the fleet's new one coordinated at AELTHORION alone)."""
from rsweb import fleets as fl
from rsweb import loadouts as lo


def D(code, t, loc, tags=(), **kw):
    return {"device_code": code, "device_type": t, "location": loc, "status": "idle", "tags": list(tags), **kw}


FLEET = {"id": "s6", "name": "Surveyor 6", "role": "explore", "home": "SOL", "station": True,
         "wants": {"ami_survey_controller": 1, "survey_drone": 2, "heaven_vessel": 1}}


def devices():
    return [D("NEW", "ami_survey_controller", "AEL-6-L4", ["fleet:s6"], status="coordinating",
              ami_directive={"name": "survey_system"}),
            D("OLD", "ami_survey_controller", "SOL-BELT-1", ["spare"]),
            D("S1", "survey_drone", "AEL-6-L4", ["fleet:s6"], controller_device_code="OLD"),
            D("S2", "survey_drone", "AEL-6-L4", ["fleet:s6"], controller_device_code="OLD"),
            D("HV", "heaven_vessel", "AEL-6-L4", ["fleet:s6"])]


def test_watch_repair_releases_and_adopts():
    steps = fl.held_elsewhere(FLEET, devices())
    assert [s["path"] for s in steps] == ["/devices/OLD", "/devices/NEW"]
    assert steps[0]["body"] == {"command": "release", "devices": ["S1", "S2"]}
    assert steps[1]["body"] == {"command": "adopt", "devices": ["S1", "S2"]}
    assert fl.held_elsewhere(FLEET, devices(), {"S1", "S2"}) == []          # busy elsewhere: left alone
    ds = devices()
    for d in ds:
        if d["device_type"] == "survey_drone":
            d["controller_device_code"] = "NEW"
    assert fl.held_elsewhere(FLEET, ds) == []                                # already ours
    ferry = devices()
    ferry[1]["tags"] = ["ferry"]
    assert fl.held_elsewhere(FLEET, ferry) == []                             # a ferry keeps its own


def test_work_phase_releases_before_adopting():
    steps, problems = fl.explore_work_steps(FLEET, devices())
    assert not problems
    bodies = [(s["path"], s["body"].get("command")) for s in steps]
    assert bodies.index(("/devices/OLD", "release")) < bodies.index(("/devices/NEW", "adopt"))


def test_dropped_controller_lets_go_of_its_drones():
    ds = devices()
    steps = fl.drop_steps(FLEET, [ds[0]], [dict(d, controller_device_code="NEW") if d["device_type"] == "survey_drone"
                                           else d for d in ds])
    assert steps[0]["body"] == {"command": "release", "devices": ["S1", "S2"]}


def test_loadout_keeps_the_controller_that_runs_drones():
    ds = [D("EMPTY", "ami_survey_controller", "SOL-BELT-1", ["fleet:s6"]),
          D("BUSY", "ami_survey_controller", "SOL-BELT-1", ["fleet:s6"]),
          D("S1", "survey_drone", "SOL-BELT-1", ["fleet:s6"], controller_device_code="BUSY"),
          D("S2", "survey_drone", "SOL-BELT-1", ["fleet:s6"], controller_device_code="BUSY")]
    p = lo.plan({"fleets": [dict(FLEET, wants={"ami_survey_controller": 1, "survey_drone": 2})]}, ds, [], {}, {}, {},
                set(), [], {})
    assert "EMPTY" in p["made_spare"] and "BUSY" not in p["made_spare"]
    # a controller the loadout no longer wants lets its drones go as it's made spare
    p = lo.plan({"fleets": [dict(FLEET, wants={"ami_survey_controller": 0, "survey_drone": 2})]}, ds[1:], [], {}, {},
                {}, set(), [], {})
    assert "BUSY" in p["made_spare"] and p["releases"].get("BUSY") == ["S1", "S2"]
