"""Rock chaser fleets (owner 2026-10-25): a fleet that mines asteroids (game v3.6.0 resource sites) inside relay coverage.

A rock chaser works its home system only (mode `hub`) or roams to any system the relay network covers within `radius` ly
of home (mode `roam`, never beyond the sector range). Each pass (engine stage rock_chase_pass, chase_engine.py):
  • target: the best open rock meeting the fleet's criteria (mass class, a wanted resource above its floor, `min_hours` of
    mining left after the trip and before the fleet leaves), its own system first, then the wanted resource, then the
    most hours; another chaser's target is never taken. The rigs ride the fleet carrier (or fly themselves inside a
    system), are deployed at the rock and mine it with the rock mining rules (rockmining.plan: one resource at a time,
    rocks you launched keep `keep_pct`); the fleet's survey drone stays and scans it
  • shipping: the fleet's haulers / freighters take the rock's pile out as soon as it reaches `ship_min`, to `drop`
    (else the system's safe spot, evacuate.safe_spot); logistics also hauls worked rocks' piles (evacuation request)
  • leaving: `leave_minutes` (80) before the ETA everything boards the carrier at once and flies to the next target (or
    the safe spot) with the last load shipped out beside it; the asteroid defense evacuation at the hour stays the backstop
  • search: a sensor array in the fleet sweeps the Kuiper belt of the system it is in every `search_hours`; objects of at
    least `launch_size` are marked for launch (Fleet details: launch at a planet or moon with a fleet vector charge, or
    `auto_launch` at `launch_at`)

Unverified live (no rig or sensor run seen yet, 2026-10-25): start_mining / retarget on a rig, the "slots full" error text,
the rock scan layout (see rockmining.py), where a sensor array must be for detect_object, and the kuiper_objects fields.
"""
from __future__ import annotations

from typing import Any

from . import rockmining as rm

ROLE = "rock_chaser"
SIZES = ["small", "medium", "large"]
MODES = ("hub", "roam")
DEFAULTS: dict[str, Any] = {"mode": "roam", "radius": 100.0, "min_size": "small", "priority": "", "min_hours": 2.0,
                            "leave_minutes": 80.0, "ship_min": 50.0, "drop": "", "keep_pct": 5.0, "launched_resources": "",
                            "search_hours": 24.0, "launch_size": "medium", "launch_at": "", "auto_launch": False}
SURGE_S_PER_LY = 50.0   # rough surge pace for ranking; the chosen trip is checked with the game's preview
CRUISE_H = 0.25         # rough in-system trip for ranking
KUIPER_HOURS = 24.0     # a detected Kuiper object stays this long (docs)
SEARCH_MINUTES = 65     # a detect_object sweep takes about an hour


def settings(f: dict) -> dict:
    out = {**DEFAULTS, **(f.get("chase") or {})}
    out["mode"] = out["mode"] if out["mode"] in MODES else "roam"
    for k in ("min_size", "launch_size"):
        out[k] = out[k] if out[k] in SIZES else DEFAULTS[k]
    return out


def leave_h(cfg: dict) -> float:
    """Hours before the ETA the fleet leaves: never later than the defense evacuation's hour."""
    try:
        return max(rm.CUTOFF_H, float(cfg.get("leave_minutes") or 0) / 60)
    except (TypeError, ValueError):
        return DEFAULTS["leave_minutes"] / 60


def size_of(o: dict) -> str:
    return str(o.get("mass_class") or o.get("size_class") or "").lower()


def big_enough(o: dict, least: str) -> bool:
    s = size_of(o)
    return (SIZES.index(s) if s in SIZES else 0) >= SIZES.index(least if least in SIZES else "small")


def kinds(ms: list[dict]) -> dict[str, list[dict]]:
    """The fleet's members by job: rigs, carriers (attach points or a hold, and surge), shippers (carry materials),
    surveyors (belt surveyors first), sensors (sensor arrays), charges (vector charges)."""
    from . import fleets as fl
    from .sites import is_surveyor, surveyors_first
    out: dict[str, list[dict]] = {k: [] for k in ("rigs", "carriers", "shippers", "surveyors", "sensors", "charges")}
    for d in sorted(ms, key=lambda d: d.get("device_code") or ""):
        t = d.get("device_type") or ""
        if rm.is_rig(d):
            out["rigs"].append(d)
        elif fl.is_carrier(d):
            out["carriers"].append(d)
        elif "transport" in (d.get("features") or []) or fl._int(d.get("cargo_capacity")) > 0:
            out["shippers"].append(d)
        elif is_surveyor(d):
            out["surveyors"].append(d)
        elif t == "sensor_array":
            out["sensors"].append(d)
        elif t == "vector_charge":
            out["charges"].append(d)
    out["surveyors"] = surveyors_first(out["surveyors"])
    out["carriers"].sort(key=lambda c: -(fl.capacity(c) + fl.hold(c)))
    return out


def area(cfg: dict, home: str, commandable: set[str], pos: dict) -> set[str]:
    """Systems the fleet may work: home (mode hub), else every system the relay network covers within `radius` ly of home
    (and the sector range)."""
    from .bootstrap import dist
    from .logistics import SECTOR_RANGE_LY
    if cfg["mode"] == "hub":
        return {home}
    r = min(float(cfg.get("radius") or 0), SECTOR_RANGE_LY)
    return {s for s in commandable if s == home or dist(home, s, pos) <= r}


def trip_hours(here: str | None, star: str, pos: dict) -> float:
    from .bootstrap import dist
    a = rm.star_of(here)
    if not a or a == star:
        return CRUISE_H
    d = dist(a, star, pos)
    return CRUISE_H + (d * SURGE_S_PER_LY / 3600 if d < 1e8 else 1e3)


def candidates(objs: dict[str, dict], cfg: dict, stars: set[str], here: str | None, pos: dict,
               taken: set[str] | frozenset = frozenset(), full: set[str] | frozenset = frozenset(),
               now: float | None = None) -> list[dict]:
    """Rocks the fleet could chase, best first: [{rock, star, resource, hours, trip_h, mine_h}]. `here`: where the fleet
    is; `taken`: other chasers' targets; `full`: rocks whose slots the game said were taken."""
    import time
    now = now if now is not None else time.time()
    prio = rm.parse_list(cfg.get("priority"))
    out = []
    for des, o in rm.open_rocks(objs, now).items():
        star = rm.star_of(des)
        if star not in stars or des in taken or des in full or not big_enough(o, cfg["min_size"]):
            continue
        res = rm.pick(o, cfg)
        if not res:
            continue
        hours = rm.hours_left(o, now) or 0
        trip = trip_hours(here, star, pos)
        mine = hours - trip - leave_h(cfg)
        if mine < float(cfg.get("min_hours") or 0):
            continue
        out.append({"rock": des, "star": star, "resource": res, "hours": round(hours, 2), "trip_h": round(trip, 2),
                    "mine_h": round(mine, 2)})
    here_star = rm.star_of(here)
    out.sort(key=lambda c: (c["star"] != here_star, prio.index(c["resource"]) if c["resource"] in prio else len(prio),
                            -c["mine_h"], c["rock"]))
    return out


def trip_ok(o: dict, travel_s: float | None, cfg: dict, now: float | None = None) -> bool:
    """With the game's preview of the trip: `min_hours` of mining remain before the fleet leaves."""
    import time
    if travel_s is None:
        return True
    now = now if now is not None else time.time()
    return (rm.hours_left(o, now) or 0) - travel_s / 3600 - leave_h(cfg) >= float(cfg.get("min_hours") or 0)


def due_to_leave(o: dict | None, cfg: dict, now: float | None = None) -> bool:
    import time
    if not o or o.get("closed"):
        return True
    now = now if now is not None else time.time()
    h = rm.hours_left(o, now)
    return h is None or h <= leave_h(cfg)


def ship_take(pile: dict[str, float], shipper: dict, order: list[str] | None = None) -> dict[str, int]:
    """What one shipper loads from a pile: the wanted resources first (`order`), then the biggest, up to its free room."""
    from . import fleets as fl
    free = fl._int(shipper.get("cargo_capacity")) - fl._int(shipper.get("cargo_used"))
    if "cargo_capacity" not in shipper:
        free = 10 ** 6   # capacity unknown: ask for the lot, the game loads what fits
    take: dict[str, int] = {}
    rank = {r: i for i, r in enumerate(order or [])}
    for res, q in sorted(pile.items(), key=lambda kv: (rank.get(kv[0], len(rank)), -kv[1])):
        n = int(min(q, free))
        if n > 0:
            take[res], free = n, free - n
    return take


def marked(kuiper: list[dict], cfg: dict, star: str, at: str) -> dict[str, dict]:
    """Kuiper objects worth launching into the inner system: at least `launch_size`. {designation: {...}}."""
    out = {}
    for k in kuiper or []:
        des = str((k or {}).get("designation") or "").upper()
        if des and big_enough(k, cfg["launch_size"]):
            out[des] = {"star": star, "mass_class": size_of(k), "composition": k.get("composition"), "found_at": at}
    return out
