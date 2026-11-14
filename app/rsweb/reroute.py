"""Print rerouting (owner 2026-11-14): a queued print its factory's stockpile can't pay for moves to another printing
hub's autofactory that can, when shipping the finished device is cheaper than shipping the missing materials.

Seen live 2026-11-14: two SOLARES autofactories held 3 bio seeders (1,450 volatiles each, the belt had 690) while 7 of
10 logistics freighters ferried volatiles over from FALQUORYX, 500 at a time. One carrier seat moves the device.

The comparison, in ly flown: materials cost one freighter load per LOAD units missing, each flown from the other hub;
the device costs one trip from the other hub to where it's going, less the trip it makes anyway from the waiting
factory. A print one load short and as far away stays put (a tie keeps it). Only prints that name where they go are
moved (fleet:, to: or at: tags); components (they must be at their factory) and contract prints stay. Every factory
used is within the sector range of the print's destination, and destinations or factories without a known position
are left alone. Pure planning; Engine.print_reroute_pass carries it out."""
from __future__ import annotations

import math
from collections import defaultdict

from . import printqueue as pq
from .shapes import as_amounts

LOAD = 500.0   # what a cargo freighter carries (logistics, live): one trip per this many units missing
MAX_PER_PASS = 6
STAY_TAGS = ("component",)


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _cost(bps: dict, t: str, q: int) -> dict[str, float]:
    return {r: v * q for r, v in as_amounts((bps.get(t) or {}).get("resources")).items() if v > 0}


def destination(tags: list[str], fleets: dict[str, dict], stars: set[str]) -> str | None:
    """The star a print is for: its to: tag, else its fleet's home, else its at: pin's star."""
    from .loadouts import bound_for, pinned_at
    homes = {fid: f.get("home") for fid, f in fleets.items()}
    d = {"tags": tags}
    dest = bound_for(d, stars, homes)
    if dest:
        return dest
    for t in tags:
        if t.startswith("fleet:"):
            f = fleets.get(t[6:])
            if f and f.get("home"):
                return f["home"]
    pin = pinned_at(d)
    return star_of(pin).upper() if pin else None


def usable(d: dict) -> bool:
    st = str(d.get("status") or "")
    return bool(d.get("location")) and d.get("in_control_range") is not False and not d.get("stowed_in_device_code") \
        and not d.get("attached_to_device_code") and not st.startswith(("compact", "stowed", "attach", "travel", "moving"))


def plan(devices: list[dict], bps: dict[str, dict], inventory: dict[str, dict], pos: dict[str, dict],
         fleets: list[dict], reach: float, limit: int = MAX_PER_PASS) -> list[dict]:
    """[{factory, location, item, to, to_location, dest, lack, loads, device_ly, materials_ly}] — prints to move."""
    from .fleets import fleet_of
    from .loadouts import _dist, can_make, for_contract, is_factory
    by_id = {f["id"]: f for f in fleets if f.get("id")}
    hub_ids = {f["id"] for f in fleets if f.get("id") and f.get("materials") == "self"
                and f.get("role") not in ("logistics", "bootstrap")}
    stars = set(pos) | {star_of(d.get("location")).upper() for d in devices if d.get("location")}
    facs = sorted((d for d in devices if is_factory(d) and usable(d)), key=lambda d: d["device_code"])
    # a factory a hub may lend: fleetless, or a printing hub's (another fleet's own factory stays its own)
    lend = [f for f in facs if not fleet_of(f) or fleet_of(f) in hub_ids]
    # what each stockpile has left once every print queued there is paid, in queue order (round-robin over the
    # factories at one spot); prints it can't pay are the candidates
    free: dict[str, dict[str, float]] = {}
    short: list[tuple[dict, dict, dict[str, float]]] = []
    by_loc: dict[str, list[dict]] = defaultdict(list)
    for f in facs:
        by_loc[f["location"]].append(f)
    for loc, fs in by_loc.items():
        left = dict(as_amounts(inventory.get(loc) or {}))
        queues = [(f, pq.items(f)) for f in fs]
        for i in range(max((len(q) for _, q in queues), default=0)):
            for f, q in queues:
                if i >= len(q):
                    continue
                it = q[i]
                cost = _cost(bps, it["device_type"], max(1, int(it.get("quantity") or 1)))
                if all(left.get(r, 0.0) >= v for r, v in cost.items()):
                    for r, v in cost.items():
                        left[r] = left.get(r, 0.0) - v
                else:
                    short.append((f, it, cost))
        free[loc] = left
    # what a stockpile can give away: it also keeps what its own waiting prints need
    give = {loc: dict(left) for loc, left in free.items()}
    for f, it, cost in short:
        for r, v in cost.items():
            give[f["location"]][r] = give[f["location"]].get(r, 0.0) - v
    load = {f["device_code"]: pq.load_seconds(f, bps) for f in lend}
    out: list[dict] = []
    for f, it, cost in short:
        if len(out) >= limit:
            break
        tags = list(it.get("tags") or [])
        if not cost or any(t in tags for t in STAY_TAGS) or for_contract({"tags": tags}):
            continue
        dest = destination(tags, by_id, stars)
        here = star_of(f["location"]).upper()
        if not dest or _dist(here, dest, pos) >= 1e9:
            continue
        have = free.get(f["location"]) or {}
        lack = {r: v - max(0.0, have.get(r, 0.0)) for r, v in cost.items() if v - max(0.0, have.get(r, 0.0)) > 1e-9}
        if not lack:
            continue
        loads = math.ceil(sum(lack.values()) / LOAD)
        best = None
        for g in lend:
            there = star_of(g["location"]).upper()
            if g["location"] == f["location"] or not can_make(g, bps.get(it["device_type"])):
                continue
            d_dest, d_mat = _dist(there, dest, pos), _dist(there, here, pos)
            if d_dest > reach or d_mat >= 1e9:
                continue
            gl = give.get(g["location"]) or {}
            if not all(gl.get(r, 0.0) >= v for r, v in cost.items()):
                continue
            # an in-system hop counts as 1 ly either way
            device_ly = max(d_dest, 1.0 if there != dest else 0.0) - _dist(here, dest, pos)
            materials_ly = loads * max(d_mat, 1.0)
            if device_ly >= materials_ly:
                continue
            key = (device_ly, load.get(g["device_code"], 0.0), g["device_code"])
            if best is None or key < best[0]:
                best = (key, g, device_ly, materials_ly)
        if not best:
            continue
        _, g, device_ly, materials_ly = best
        for r, v in cost.items():   # the donor pays it now; the waiting factory no longer needs it
            give[g["location"]][r] -= v
            give[f["location"]][r] = give[f["location"]].get(r, 0.0) + v
        load[g["device_code"]] = load.get(g["device_code"], 0.0) + float((bps.get(it["device_type"]) or {}).get("print_time") or 0)
        out.append({"factory": f["device_code"], "location": f["location"], "item": it, "to": g["device_code"],
                    "to_location": g["location"], "dest": dest, "lack": {r: math.ceil(v) for r, v in lack.items()},
                    "loads": loads, "device_ly": round(device_ly), "materials_ly": round(materials_ly)})
    return out


def describe(r: dict) -> str:
    lack = ", ".join(f"{v:g} {k}" for k, v in sorted(r["lack"].items()))
    t = r["item"]["device_type"].replace("_", " ")
    return (f"{t} for {r['dest']}: moved from {r['factory']} ({r['location']}, short {lack}) to {r['to']} "
            f"({r['to_location']}): the device flies ~{r['device_ly']:,} ly more instead of {r['loads']} freighter "
            f"load(s) over ~{r['materials_ly']:,} ly")
