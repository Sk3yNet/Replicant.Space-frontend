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

import re

from typing import Any

from . import rockmining as rm

ROLE = "rock_chaser"
SIZES = ["small", "medium", "large"]
MODES = ("hub", "roam")
DEFAULTS: dict[str, Any] = {"mode": "roam", "radius": 100.0, "min_size": "small", "priority": "", "min_hours": 2.0,
                            "leave_minutes": 80.0, "ship_min": 50.0, "drop": "", "keep_pct": 5.0, "launched_resources": "",
                            "search_hours": 24.0, "launch_size": "medium", "launch_at": "", "auto_launch": False,
                            "follow_need": True, "stage_kuiper": True, "slow_rocks": True}
SURGE_S_PER_LY = 50.0   # rough surge pace for ranking; the chosen trip is checked with the game's preview
CRUISE_H = 0.25         # rough in-system trip for ranking
KUIPER_HOURS = 24.0     # a detected Kuiper object stays this long (docs)


PENDING_KV = "chase_pending"   # [{fid, form, user, at}]: card changes made while the engine held its lock


def apply_form(f: dict, form: dict, user: str, at: str) -> tuple[str, str | None]:
    """One rock chaser card change on fleet `f` (in place): (message, error)."""
    run = f.setdefault("chase_run", {})
    action = form.get("action") or "settings"
    if action == "settings":
        c = dict(f.get("chase") or {})
        for k, v in DEFAULTS.items():
            if isinstance(v, bool):
                c[k] = form.get(k) == "on"
            elif k in form:
                raw = str(form.get(k) or "").strip()
                if isinstance(v, float):
                    try:
                        c[k] = max(0.0, float(raw)) if raw else v
                    except ValueError:
                        return "", f"{k} must be a number"
                else:
                    c[k] = raw.upper() if k in ("drop", "launch_at") else raw
        c["leave_minutes"] = max(60.0, c.get("leave_minutes") or 0)   # never later than the defense evacuation's hour
        f["chase"] = c
        f["chase"] = settings(f)
        return "Saved.", None
    if action == "launch":
        rock, dest = str(form.get("rock") or "").strip().upper(), str(form.get("at") or "").strip().upper()
        if not rock or not dest:
            return "", "Pick the object and the planet or moon to launch it at."
        run["launch"] = {"rock": rock, "at": dest, "by": user}
        return f"{rock} will be launched at {dest} once a fleet vector charge is there.", None
    if action == "unlaunch":
        run.pop("launch", None)
        return "Launch called off.", None
    if action == "drop":
        if run.get("target"):
            run.setdefault("skip", {})[run["target"]] = at
        run.pop("target", None)
        run["phase"] = "idle"
        return "Target dropped: the fleet picks another one on the next pass.", None
    return "", "Unknown action."


ROCK_RE = re.compile(r"-(?:OBJ|KUIPER|OORT)-\d+$")


def is_rock(spot: str | None) -> bool:
    """An asteroid's own location (STAR-OBJ-n, STAR-KUIPER-n): never a drop or a pick-up, it is gone at impact (live
    2026-11-16: three Chaser_1 freighters waiting at RENGRELOR-OBJ-1 to collect were destroyed with it)."""
    return bool(spot and ROCK_RE.search(spot))


def near_target(spot: str | None, target: str | None) -> bool:
    """`spot` is the impact target or one of its moons or L-points (Joe 2026-11-16: never drop at the impact target)."""
    return bool(spot and target and (spot == target or spot.startswith(target + "-")))


def drop_candidates(star: str, target: str | None, entry: str | None, scan: dict | None, avoid: set[str]) -> list[str]:
    """Permanent spots in the system to drop shipped materials: entry point, planets and their L4/L5, belts. Not the impact
    target (or its moons and L-points), no object, site or salvage, nothing in `avoid`."""
    out: list[str] = []

    def add(c: str | None) -> None:
        if c and c not in out and c not in avoid and not near_target(c, target) and c.split("-")[0] == star:
            out.append(c)
    add(entry)
    for p in (scan or {}).get("planets") or []:
        des = p.get("designation") if isinstance(p, dict) else None
        if des:
            add(des)
            add(f"{des}-L4")
            add(f"{des}-L5")
    for b in ((scan or {}).get("asteroid_belt") or {}).get("belts") or []:
        add(b.get("designation") if isinstance(b, dict) else None)
    return out[:16]


def launch_spot(des: str) -> str:
    """Where a vector charge launches Kuiper object `des` from: the belt it was found in (RENGRELOR-KUIPER-2 →
    RENGRELOR-KUIPER). Live 2026-11-16: travel to the object itself is refused ("Invalid destination format");
    detonate names the object as `target`. Unverified live that detonate works from the belt."""
    m = re.match(r"^(.+-(?:KUIPER|OORT))-\d+$", des or "")
    return m.group(1) if m else des


LAUNCHED_KV = "launched_rocks"   # {object_designation: detonate response + at}: rocks you sent; defense leaves them be


async def remember_launch(db, resp, by: str, at: str | None = None) -> bool:
    """Keep a detonate answer ({object_designation: STAR-OBJ-n, destination, impact_eta, ...}) so defense treats the rock
    as yours. Every launch path calls it (live 2026-11-16: one sent from the device command form wasn't kept)."""
    if not isinstance(resp, dict) or not resp.get("object_designation"):
        return False
    from .db import now_iso
    rocks = await db.kv_get(LAUNCHED_KV, {}) or {}
    rocks[resp["object_designation"]] = {**rocks.get(resp["object_designation"], {}), **resp, "at": at or now_iso(), "by": by}
    await db.kv_set(LAUNCHED_KV, rocks)
    return True


def can_launch(charge: dict, des: str) -> bool:
    """A deployed charge (not stowed or towed) at the object's belt, or at the object itself."""
    return charge.get("location") in (des, launch_spot(des)) and not charge.get("stowed_in_device_code") \
        and not charge.get("attached_to_device_code")
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
    out: dict[str, list[dict]] = {k: [] for k in ("rigs", "carriers", "shippers", "surveyors", "sensors", "charges", "ferried", "propulsors")}
    for d in sorted(ms, key=lambda d: d.get("device_code") or ""):
        t = d.get("device_type") or ""
        if rm.is_rig(d):
            out["rigs"].append(d)
        elif fl.is_carrier(d):
            out["carriers"].append(d)
        elif "transport" in (d.get("features") or []) or fl._int(d.get("cargo_capacity")) > 0:
            # live 2026-11-16: 661BF2A6 ran the fleet controller's ferry (RENGRELOR-3-L4 → SOLARES-BELT-1) while the app
            # gave it rock trips too; a shipper under a controller is the controller's
            out["ferried" if d.get("controller_device_code") else "shippers"].append(d)
        elif is_surveyor(d):
            out["surveyors"].append(d)
        elif t == "sensor_array":
            out["sensors"].append(d)
        elif t == "vector_charge":
            out["charges"].append(d)
        elif t == "propulsor":
            out["propulsors"].append(d)
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
                    "mine_h": round(mine, 2), "size": SIZES.index(size_of(o)) if size_of(o) in SIZES else 0})
    here_star = rm.star_of(here)
    out.sort(key=lambda c: (c["star"] != here_star, prio.index(c["resource"]) if c["resource"] in prio else len(prio),
                            -c["size"], -c["mine_h"], c["rock"]))   # Joe 2026-11-19: larger rocks hold more
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


# What a rock of each composition holds (game docs, concepts/mining "What's inside", Joe 2026-11-19). Mixed rocks hold
# three random resources, so they are never assumed; a composition the docs don't list is learned from the first rock
# seen (kv rock_yields, learn_yields).
YIELDS: dict[str, list[str]] = {"ice": ["volatiles", "carbon", "silicates"],
                                "rock": ["silicates", "structural", "rares"],
                                "carbonaceous": ["carbon", "volatiles", "silicates", "rares"],
                                "metallic": ["structural", "conductive", "rares"]}
RANDOM_YIELDS = ("mixed",)
YIELDS_KV = "rock_yields"   # {composition: [resources]} seen on rocks' resource sites
LOW_COUNT = 3               # "low on": the stockpile's three smallest resources


def learn_yields(objs: dict[str, dict], known: dict[str, list[str]]) -> bool:
    """Adds the resources of every tracked rock with a composition to `known` (in place). True when something changed."""
    changed = False
    for o in (objs or {}).values():
        comp = str((o or {}).get("composition") or "").lower()
        res = sorted(r for r in rm.resources_left(o) if r in rm.RESOURCES) if comp else []
        if comp in YIELDS or comp in RANDOM_YIELDS:
            continue   # documented, or random per rock
        if res and sorted(known.get(comp) or []) != res:
            known[comp] = res
            changed = True
    return changed


def yields(comp: Any, known: dict[str, list[str]] | None = None) -> list[str]:
    c = str(comp or "").lower()
    if c in RANDOM_YIELDS:
        return []
    return list(YIELDS.get(c) or (known or {}).get(c) or [])


def need_order(stock: dict[str, float]) -> list[str]:
    """Every resource, the one the stockpile holds least of first (ties in the standard order)."""
    return sorted(rm.RESOURCES, key=lambda r: (float((stock or {}).get(r) or 0), rm.RESOURCES.index(r)))


def launch_choice(marks: dict[str, dict], star: str, need: list[str], known: dict[str, list[str]] | None = None,
                  ) -> tuple[str | None, str]:
    """The marked Kuiper object in `star` to launch next: (designation or None, why). One that holds a resource the
    stockpile is low on (its `LOW_COUNT` smallest) comes first, the lowest one first, then the bigger (Joe 2026-11-19:
    larger rocks hold more), then by name; a mixed object (three random resources) or one of an unknown composition comes
    after those, bigger first;
    one known to hold none of the low resources is never launched."""
    low = need[:LOW_COUNT]
    best, why, skipped = None, "", []
    for des, m in sorted((marks or {}).items()):
        if m.get("star") != star:
            continue
        ys = yields(m.get("composition"), known)
        if ys:
            hits = [r for r in low if r in ys]
            if not hits:
                skipped.append(f"{des} ({m.get('composition')})")
                continue
            rank = (0, low.index(hits[0]))
            reason = f"{m.get('composition')}: {', '.join(hits)} (low at the stockpile)"
        else:
            rank, reason = (1, 0), f"{m.get('composition') or 'unknown'} composition: it may hold anything"
        key = (rank, -SIZES.index(size_of(m)) if size_of(m) in SIZES else 0, des)
        if best is None or key < best[0]:
            best = (key, des, reason)
    if best:
        return best[1], best[2]
    if skipped:
        why = f"none holds what the stockpile is low on ({', '.join(low)}): {', '.join(skipped)} left alone"
    return None, why


def stage_spot(scan: dict | None) -> str | None:
    """The system's Kuiper belt from its scan (Joe 2026-11-20: a chaser waits there, its sensor array sweeping it and close
    to a rock when it launches). None: no Kuiper belt, or the system isn't scanned yet."""
    return ((((scan or {}).get("outer_system") or {}).get("kuiper") or {}).get("designation") or None)


STAGED_OUT = ("ami_", )   # controllers run the supply ferry from the drop: they stay there with the freighters


def stages(d: dict) -> bool:
    """A member that waits at the Kuiper belt: everything but the haulers / freighters and the AMI controllers."""
    t = d.get("device_type") or ""
    return not t.startswith(STAGED_OUT) and not ("transport" in (d.get("features") or []) or rm_cargo(d))


def rm_cargo(d: dict) -> bool:
    try:
        return int(d.get("cargo_capacity") or 0) > 0
    except (TypeError, ValueError):
        return False


SLOW_DIR = "decrease"   # first guess for lengthening a launched rock's flight; flipped when the ETA moves earlier
SLOW_STOP_H = 1.5       # no new slowing this close to the ETA: the evacuation takes the propulsors off at 45 min


def marked(kuiper: list[dict], cfg: dict, star: str, at: str) -> dict[str, dict]:
    """Kuiper objects worth launching into the inner system: at least `launch_size`. {designation: {...}}."""
    out = {}
    for k in kuiper or []:
        des = str((k or {}).get("designation") or "").upper()
        if des and big_enough(k, cfg["launch_size"]):
            out[des] = {"star": star, "mass_class": size_of(k), "composition": k.get("composition"), "found_at": at}
    return out
