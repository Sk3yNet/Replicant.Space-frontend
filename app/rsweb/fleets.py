"""Fleets: a named group of devices with a home system. Stationed at home, it's that system's own devices, kept at its
loadout; on a mission, it travels, works a system, and comes back together.

Membership is a tag, `fleet:<id>`. A *stationed* fleet (`station`) that isn't on a mission is kept at its loadout in its
home system by the loadout pass (loadouts.plan), and its devices there work that system: the in-system rules (idle
miners, salvage, re-open sites, AMI schedules …) use them like any of the system's devices. A fleet's devices anywhere
else, or while it's on a mission, are left to the fleet: the rules don't recruit them. A fleet has
  • a loadout (`wants`: device type → count, or a `template` it follows) and a home system
  • `materials`: "" | "self" (takes materials in) | another fleet's id (its home's stockpile is ferried there)
  • a role: mining | explore | trade
  • carriers: any surge-capable members that carry others (mobile fleet 36, surge carrier 9, platform 4,
    plate 1 — devices attach to them); surge-capable cargo (freighters) flies itself
A mission is a list of phases; each phase becomes one job, and the next phase starts when it finishes:

  mining   assemble → travel → deploy → work → watch (until the mining controller reports exhausted and the
           survey drones stop finding sites, for N minutes) → recall → return → unload
           work: everyone flies to the target belt; the survey controller runs belt_search, the mining controller
           gather_evenly, and (deliver mode) the transport controller ferries to the nearest destination system.
           Haul mode: freighters fill up at the belt on recall and bring it home.
  explore  for each target: assemble → travel → deploy → survey_system → watch (no_targets) → recall; then home
  trade    contracts (civilisation events) and trades: load (freighters collect what the site is still short of, from
           the nearest stockpiles) → assemble → travel → deliver (deposit at the site; a vessel hosting a replicant goes
           too) → wait (for a replicant at the site) → trade (fulfil the event / execute the trade) → collect (the
           rewards) → recall → return (to the nearest system whose fleet takes materials in) → unload → home
Unload, back home: cargo is deposited at the home stockpile. A stationed fleet's devices also come off the carriers
(they work the home system); any other fleet stays aboard, ready for its next mission.
"""
from __future__ import annotations

import re
from typing import Any

from .automations import SHORT_TIMEOUT, STEP_TIMEOUT, step
from .shapes import as_amounts

ROLES = ("mining", "explore", "trade")
PHASES = {
    "mining": ["assemble", "gather", "travel", "deploy", "work", "watch", "recall", "return", "unload"],
    "explore": ["assemble", "gather", "travel", "deploy", "work", "watch", "recall"],   # travel…recall per target, then return/unload
    "trade": ["load", "assemble", "gather", "travel", "deliver", "wait", "trade", "collect", "recall", "return", "unload",
              "home"],
}
ATTACH_TYPES = ("surge_plate", "surge_platform", "surge_carrier", "mobile_fleet")
CAPACITY = {"surge_plate": 1, "surge_platform": 4, "surge_carrier": 9, "mobile_fleet": 36, "cargo_vessel": 3}
HOLD = {"cargo_vessel": 50, "heaven_vessel": 10, "racing_vessel": 5}   # stow capacity (devices in the hold)


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def fleet_tag(fid: str) -> str:
    return "fleet:" + re.sub(r"[^a-z0-9\-_.]", "", fid.lower())[:26]


def fleet_of(d: dict) -> str | None:
    return next((t[6:] for t in d.get("tags") or [] if t.startswith("fleet:")), None)


def members(fleet: dict, devices: list[dict]) -> list[dict]:
    tag = fleet_tag(fleet["id"])
    return [d for d in devices if tag in (d.get("tags") or [])]


def _int(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def capacity(d: dict) -> int:
    """Attach points (devices ride on the outside: plates, platforms, carriers, mobile fleets, cargo vessel 3)."""
    return _int(d.get("attach_capacity")) or CAPACITY.get(d.get("device_type") or "", 0)


def hold(d: dict) -> int:
    """Hold slots (devices stowed inside: cargo vessel 50, heaven vessel 10, racing vessel 5)."""
    return _int(d.get("stow_capacity")) or HOLD.get(d.get("device_type") or "", 0)


def is_carrier(d: dict) -> bool:
    t = d.get("device_type") or ""
    if "surge" not in (d.get("features") or []):
        return False
    return (capacity(d) > 0 and (any(k in t for k in ATTACH_TYPES) or hold(d) > 0 or _int(d.get("attach_capacity")) > 0)) \
        or hold(d) > 0


def flies_itself(d: dict) -> bool:
    return "surge" in (d.get("features") or []) and not is_carrier(d)


def stowable(d: dict) -> bool:
    """Can ride in a hold: the device itself has the `stow` feature (or stow/deploy commands). In live data
    (2026-10-02) drones, AMI controllers, maintenance drones, beacons, relays, wards and surge plates do;
    transport drones and haulers don't, so those need attach points."""
    if "stow" in (d.get("features") or []):
        return True
    cmds = d.get("available_commands") or []
    if "stow" in cmds or "deploy" in cmds:
        return True
    if d.get("features") or cmds:
        return False
    t = d.get("device_type") or ""        # nothing known about it: only the transport types are known not to stow
    return not ("transport_drone" in t or "transport_hauler" in t)


def seats(c: dict, devices: list[dict] | None = None) -> dict:
    """Free hold slots and attach points on a carrier right now."""
    inside = len(c.get("stowed_devices") or []) or _int(c.get("stow_used"))
    outside = len(c.get("attached_devices") or [])
    if devices is not None:
        inside = max(inside, sum(1 for d in devices if d.get("stowed_in_device_code") == c.get("device_code")))
        outside = max(outside, sum(1 for d in devices if d.get("attached_to_device_code") == c.get("device_code")))
    return {"hold": max(0, hold(c) - inside), "attach": max(0, capacity(c) - outside)}


def take_seat(free: dict, d: dict) -> str | None:
    """Pick 'stow' (hold first, keeps attach points for devices that can't stow) or 'attach'; mutates free."""
    if stowable(d) and free["hold"] > 0:
        free["hold"] -= 1
        return "stow"
    if free["attach"] > 0:
        free["attach"] -= 1
        return "attach"
    return None


def board_step(carrier: str, code: str, mode: str) -> dict:
    if mode == "stow":   # the cargo stows itself into the carrier
        st = step(f"stow {code} in {carrier}", f"/devices/{code}", {"command": "stow", "target": carrier},
                  wait=["device.stowed"], timeout=SHORT_TIMEOUT, critical=True)
        st["wait_device"] = code
        return st
    st = step(f"{carrier}: attach {code}", f"/devices/{carrier}", {"command": "attach", "device": code},
              wait=["device.attached"], timeout=SHORT_TIMEOUT, critical=True)
    st["wait_device"] = carrier
    return st


def aboard(d: dict, carriers: set[str]) -> str | None:
    c = d.get("attached_to_device_code") or d.get("stowed_in_device_code")
    return c if c in carriers else None


def type_profile(t: str, bps: dict[str, dict], devices: list[dict]) -> dict:
    """How a device type fits a fleet: {"carrier": attach points it brings, "hold": hold slots it brings,
    "flies": surges itself, "stowable": can ride in a hold}. From a device of that type, else its blueprint,
    else the known sizes."""
    sample = next((d for d in devices if d.get("device_type") == t), None) or {}
    bp = bps.get(t) or {}
    probe = {"device_type": t, "features": sample.get("features") or bp.get("features") or [],
             "attach_capacity": sample.get("attach_capacity") or bp.get("attach_capacity"),
             "stow_capacity": sample.get("stow_capacity") or bp.get("stow_capacity"),
             "available_commands": sample.get("available_commands")}
    if t in CAPACITY or t in HOLD or any(k in t for k in ATTACH_TYPES):
        probe["features"] = list(set(probe["features"]) | {"surge"})   # known carriers surge even if the blueprint says less
    carrier = is_carrier(probe)
    return {"carrier": capacity(probe) if carrier else 0, "hold": hold(probe) if carrier else 0,
            "flies": bool(not carrier and "surge" in probe["features"]), "stowable": stowable(probe)}


def _budget(hold_av: int, att_av: int, stow_need: int, att_only: int) -> dict:
    in_hold = min(hold_av, stow_need)
    att_need = att_only + (stow_need - in_hold)          # what doesn't fit in the holds rides outside
    gap = max(0, att_need - att_av)
    return {"available": hold_av + att_av, "needed": stow_need + att_only, "short": gap,
            "hold": {"available": hold_av, "needed": stow_need, "used": in_hold},
            "attach": {"available": att_av, "needed": att_need},
            "fix": (f"{-(-gap // 9)} surge carrier(s), {-(-gap // 4)} surge platform(s) or {-(-gap // 3)} cargo vessel(s)"
                    if gap else "")}


def attach_points(fleet: dict, devices: list[dict], bps: dict[str, dict], rows: list[dict] | None = None) -> dict:
    """Carrying budget — hold slots and attach points the fleet's carriers offer vs what its riders need, now (members)
    and at full loadout (each type at max(want, have)). Riders are members that aren't carriers and can't surge.
    Stowable riders go in holds first; the rest (and hold overflow) need attach points."""
    rows = rows if rows is not None else roster(fleet, devices)["rows"]
    ms = members(fleet, devices)
    cars = [d for d in ms if is_carrier(d)]
    riders = [d for d in ms if not is_carrier(d) and not flies_itself(d)]
    codes = {c["device_code"] for c in cars}
    now = _budget(sum(hold(c) for c in cars), sum(capacity(c) for c in cars),
                  sum(1 for d in riders if stowable(d)), sum(1 for d in riders if not stowable(d)))
    now["attached"] = sum(1 for d in ms if d.get("attached_to_device_code") in codes)
    now["stowed"] = sum(1 for d in ms if d.get("stowed_in_device_code") in codes)
    h = a = sn = an = 0
    for r in rows:
        prof = type_profile(r["type"], bps, devices)
        n = max(int(r.get("want") or 0), int(r.get("have") or 0))
        r["profile"] = prof
        if prof["carrier"] or prof["hold"]:
            h += prof["hold"] * n
            a += prof["carrier"] * n
        elif not prof["flies"]:
            if prof["stowable"]:
                sn += n
            else:
                an += n
    return {"now": now, "plan": _budget(h, a, sn, an)}


def kind(d: dict) -> str:
    t = d.get("device_type") or ""
    if "controller" in t:
        for k in ("mining", "survey", "transport", "trade"):
            if k in t:
                return f"{k}_controller"
    return t


def roster(fleet: dict, devices: list[dict]) -> dict:
    """What the fleet has vs its loadout, where everything is, and what's riding where."""
    ms = members(fleet, devices)
    by_type: dict[str, list[dict]] = {}
    for d in ms:
        by_type.setdefault(d.get("device_type") or "device", []).append(d)
    rows = []
    for t in sorted(set(by_type) | set(fleet.get("wants") or {})):
        want = int((fleet.get("wants") or {}).get(t) or 0)
        have = by_type.get(t, [])
        rows.append({"type": t, "want": want, "have": len(have), "short": max(0, want - len(have)),
                     "codes": [d["device_code"] for d in have]})
    carriers = [d for d in ms if is_carrier(d)]
    riding = {d["device_code"]: d.get("attached_to_device_code") or d.get("stowed_in_device_code") for d in ms
              if d.get("attached_to_device_code") or d.get("stowed_in_device_code")}
    stars = sorted({star_of(d.get("location")) for d in ms if d.get("location")} |
                   {star_of(next((c.get("location") for c in ms if c["device_code"] == v), "")) for v in riding.values()} - {""})
    return {"members": ms, "rows": rows, "carriers": carriers, "riding": riding, "stars": stars,
            "capacity": sum(capacity(c) for c in carriers), "hold": sum(hold(c) for c in carriers),
            "passengers": [d for d in ms if not is_carrier(d) and not flies_itself(d)],
            "unlisted": {d["device_code"] for d in ms if d.get("unlisted")},
            "loose": [d for d in ms if not is_carrier(d) and not flies_itself(d)
                      and not aboard(d, {c["device_code"] for c in carriers})]}


def next_phase(role: str, m: dict) -> str | None:
    """The phase after m["phase"] (mutates m["idx"] when an explore fleet moves on to its next target)."""
    cur = m.get("phase")
    if role == "explore":
        seq = PHASES["explore"]
        if cur is None:
            return seq[0]
        if cur in seq[:-1]:
            return seq[seq.index(cur) + 1]
        if cur == "recall":
            if m.get("idx", 0) + 1 < len(m.get("targets") or []):
                m["idx"] = m.get("idx", 0) + 1
                return "travel"   # recall already re-boarded everyone
            return "return"
        return {"return": "unload"}.get(cur)
    seq = PHASES[role]
    if cur is None:
        return seq[0]
    i = seq.index(cur) if cur in seq else len(seq)
    return seq[i + 1] if i + 1 < len(seq) else None


def destination(star: str, stars: dict) -> str:
    return (stars.get(star) or {}).get("entry_point") or star


def _wait_arrive(code: str, dest: str, match: str | None = None, timeout: int | None = None) -> dict:
    st = step(f"wait for {code} at {dest}", "", None, method="WAIT", wait=["travel.arrived"],
              match={"destination": match or dest}, timeout=max(STEP_TIMEOUT, timeout or 0))
    st["wait_device"] = code
    return st


MOVING = ("moving", "travel", "cruis", "surg", "recalling", "returning")


def in_flight(d: dict) -> dict | None:
    """The trip a device is on right now ({"destination", "seconds_left"}), or None. The game refuses any new order
    while a device is in motion ("Device is already in motion") — seen live 2026-10-06: a survey controller's
    `survey_system` with recall on had already sent its drones back to the vessel when the fleet's recall ordered
    them there too, and the mission stalled."""
    st = str(d.get("status") or "").lower()
    tr = d.get("travel") or {}
    if not st.startswith(MOVING) or not (tr.get("final_destination") or tr.get("destination")):
        return None
    from datetime import datetime, timezone
    left = int(tr.get("eta_seconds") or 0)
    try:
        at = datetime.fromisoformat(str(tr.get("final_arrives_at") or tr.get("arrives_at")))
        left = max(left, int((at - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        pass
    return {"destination": tr.get("final_destination") or tr.get("destination"), "seconds_left": max(0, left)}


# --- in-system distances: a cruise-only device never makes a long trip just to board -----------------------------
# Cruising across a system is slow (seen live 2026-10-06: survey drones took 2 h 20 min to cruise 1395 AU out to the Oort
# cloud to board their vessel). A surge-capable carrier fetches them instead.
MAX_CRUISE_AU = 30.0
GUESS_AU = {"OORT": 2000.0, "KUIPER": 40.0, "BELT": 3.0}   # when the system hasn't been scanned
INNER_AU = 5.0                                              # a body we know nothing about: somewhere in the inner system


def system_radii(scan: dict | None) -> dict[str, float]:
    """Distance from the star (AU) of each place a system scan names: planets, belts (mid-radius), Kuiper, Oort."""
    out: dict[str, float] = {}
    if not isinstance(scan, dict):
        return out
    for p in scan.get("planets") or []:
        if p.get("designation") and p.get("orbital_distance_au") is not None:
            out[p["designation"]] = float(p["orbital_distance_au"])
    for b in (scan.get("asteroid_belt") or {}).get("belts") or []:
        lo_, hi = b.get("inner_radius_au"), b.get("outer_radius_au")
        if b.get("designation") and lo_ is not None and hi is not None:
            out[b["designation"]] = (float(lo_) + float(hi)) / 2
    for k in ("kuiper", "oort"):
        o = (scan.get("outer_system") or {}).get(k) or {}
        if o.get("designation") and o.get("distance_au") is not None:
            out[o["designation"]] = float(o["distance_au"])
    return out


def radius_au(loc: str | None, radii: dict[str, float] | None = None) -> float | None:
    """How far from its star a location is: from the scan, else its body's (moons, L-points, salvage), else a guess
    from the code (…-OORT, …-KUIPER, …-BELT-n), else None."""
    if not loc:
        return None
    radii = radii or {}
    if loc in radii:
        return radii[loc]
    parts = loc.split("-")
    if len(parts) == 1:
        return 0.0
    for n in range(len(parts) - 1, 1, -1):
        if "-".join(parts[:n]) in radii:
            return radii["-".join(parts[:n])]
    return GUESS_AU.get(parts[1])


def cruise_au(a: str | None, b: str | None, radii: dict[str, float] | None = None) -> float:
    """Roughly how far a cruise from a to b is (the radial gap; positions on the orbits aren't known)."""
    if not a or not b or a == b:
        return 0.0
    if star_of(a) != star_of(b):
        return float("inf")
    ra, rb = radius_au(a, radii), radius_au(b, radii)
    if ra is None and rb is None:
        return 0.0
    return abs((INNER_AU if ra is None else ra) - (INNER_AU if rb is None else rb))


def far_apart(a: str | None, b: str | None, radii: dict[str, float] | None = None, limit: float | None = None) -> bool:
    return cruise_au(a, b, radii) > (MAX_CRUISE_AU if limit is None else limit)


def pickup_tour(carrier: str, start: str | None, stops: dict[str, list[tuple[str, str]]],
                radii: dict[str, float] | None = None) -> tuple[list[dict], str | None]:
    """The carrier flies to each pick-up spot (nearest first) and takes its devices aboard there.
    stops: location -> [(device, "stow" | "attach")]. Returns (steps, where the carrier ends up)."""
    steps, cur, stops = [], start, dict(stops)
    while stops:
        loc = min(stops, key=lambda x: (cruise_au(cur, x, radii), x))
        group = stops.pop(loc)
        if loc != cur:
            st = step(f"{carrier} → {loc} (pick up {', '.join(c for c, _ in group)})", f"/devices/{carrier}",
                      {"command": "travel", "destination": loc}, wait=["travel.arrived"], match={"destination": loc},
                      critical=True)
            st["wait_device"] = carrier
            steps.append(st)
            cur = loc
        for code, mode in group:
            steps.append(board_step(carrier, code, mode))
    return steps, cur


def assemble_steps(fleet: dict, devices: list[dict], radii: dict[str, float] | None = None,
                   limit: float | None = None) -> tuple[list[dict], list[str]]:
    """Get every passenger aboard a fleet carrier — stowed in a hold when it can be, else attached. Returns (steps, problems).
    A passenger close to its carrier flies over and boards; one far from it (more than `limit` AU of cruising, e.g. out
    at the Oort cloud while the carrier is inside the system) waits, and the carrier fetches it after the others are aboard."""
    r = roster(fleet, devices)
    carriers = sorted(r["carriers"], key=lambda c: (-(hold(c) + capacity(c)), c["device_code"]))
    if not carriers:
        return [], ["the fleet has no surge carrier (mobile fleet / surge carrier / platform / plate / cargo vessel)"]
    codes = {c["device_code"] for c in carriers}
    free = {c["device_code"]: seats(c, devices) for c in carriers}
    loc = {c["device_code"]: c.get("location") for c in carriers}
    steps, problems, moves, board = [], [], [], []
    arriving: list[tuple[str, dict]] = []   # passengers already flying to their carrier
    landing: list[tuple[str, dict]] = []    # passengers flying somewhere else: they land, then board
    fetch: dict[str, dict[str, list[tuple[str, str]]]] = {}   # carrier -> pick-up spot -> [(device, mode)]
    # devices that can't stow first, so they get the attach points before stowable ones overflow onto them
    for d in sorted(r["passengers"], key=lambda d: (stowable(d), d["device_code"])):
        code = d["device_code"]
        if aboard(d, codes):
            continue
        trip = in_flight(d)
        here = star_of(d.get("location") or (trip or {}).get("destination"))
        mode, c = None, None
        for cand in carriers:
            if star_of(loc[cand["device_code"]]) != here:
                continue
            mode = take_seat(free[cand["device_code"]], d)
            if mode:
                c = cand
                break
        if not c:
            if here and not any(star_of(loc[x]) == here for x in loc):
                continue  # in a system with no fleet carrier: the gather phase picks it up
            problems.append(f"{code} ({d.get('device_type')}) is in {here or 'transit'} with no "
                            + ("hold or attach" if stowable(d) else "attach") + " room on a fleet carrier there")
            continue
        spot = (trip or {}).get("destination") or d.get("location")
        if trip and trip["destination"] == loc[c["device_code"]]:
            arriving.append((code, trip))   # already on its way to the carrier: just wait for it
        elif spot and far_apart(spot, loc[c["device_code"]], radii, limit):
            if trip:
                landing.append((code, trip))
            fetch.setdefault(c["device_code"], {}).setdefault(spot, []).append((code, mode))
            continue                        # the carrier comes for it
        elif d.get("location") != loc[c["device_code"]] or trip:
            if "travel" not in (d.get("available_commands") or ["travel"]):
                problems.append(f"{code} can't travel to {loc[c['device_code']]} to board")
                continue
            if trip:   # in flight somewhere else: it can't be redirected, so it lands first
                landing.append((code, trip))
            moves.append((code, loc[c["device_code"]]))
        board.append((c["device_code"], code, mode))
    for code, trip in landing:
        w = _wait_arrive(code, trip["destination"], timeout=trip["seconds_left"] + 1800)
        w["seq0_from"] = 0   # it may land while earlier steps run: count from the start of the job
        steps.append(w)
    first = len(steps)
    for code, dest in moves:
        steps.append(step(f"{code} → {dest} (board)", f"/devices/{code}", {"command": "travel", "destination": dest}, critical=True))
    for i, (code, dest) in enumerate(moves):
        w = _wait_arrive(code, dest)
        w["seq0_from"] = first + i
        steps.append(w)
    for code, trip in arriving:
        w = _wait_arrive(code, trip["destination"], timeout=trip["seconds_left"] + 1800)
        w["seq0_from"] = 0
        steps.append(w)
    for carrier, code, mode in board:
        steps.append(board_step(carrier, code, mode))
    for carrier in sorted(fetch):   # then each carrier fetches the far ones
        tour, _ = pickup_tour(carrier, loc[carrier], fetch[carrier], radii)
        steps += tour
    return steps, problems


def _dist(a: str, b: str, pos: dict) -> float:
    import math
    if a == b:
        return 0.0
    pa, pb = pos.get(a) or {}, pos.get(b) or {}
    if not pa or not pb:
        return 1e9
    return math.dist([pa.get(k, 0) for k in "xyz"], [pb.get(k, 0) for k in "xyz"])


def gather_plan(fleet: dict, devices: list[dict], stars: dict, busy: set[str]) -> dict:
    """Who the fleet picks up on its way out: its own members stranded in other systems (where it has no
    carrier), plus spare devices that fill gaps in its loadout (nearest first). One carrier — the one with
    the most room — tours those systems, collecting as it goes; the travel phase then flies everyone on."""
    pos = {k: (v or {}).get("position") or {} for k, v in stars.items()}
    r = roster(fleet, devices)
    carriers = r["carriers"]
    if not carriers:
        return {"carrier": None, "recruit": [], "tour": [], "problems": []}
    carrier_stars = {star_of(c.get("location")) for c in carriers}
    load = {c["device_code"]: sum(1 for d in r["passengers"] if d.get("attached_to_device_code") == c["device_code"]
                                  or (star_of(d.get("location")) == star_of(c.get("location")) and not d.get("attached_to_device_code")))
            for c in carriers}
    tourer = max(carriers, key=lambda c: (hold(c) + capacity(c) - load[c["device_code"]], c["device_code"]))
    free = seats(tourer, devices)
    # passengers already in the tourer's system will take seats first (assemble boards them)
    for d in sorted((d for d in r["passengers"] if not d.get("attached_to_device_code") and not d.get("stowed_in_device_code")
                     and star_of(d.get("location")) == star_of(tourer.get("location"))), key=lambda d: (stowable(d), d["device_code"])):
        take_seat(free, d)
    base = star_of(tourer.get("location"))
    picks: list[dict] = []
    problems: list[str] = []
    # 1. stranded members (a self-surging member just flies to the target later)
    for d in r["passengers"]:
        here = star_of(d.get("location"))
        if here and here not in carrier_stars and not d.get("attached_to_device_code"):
            picks.append(d)
    # 2. spares that fill gaps in the loadout
    recruit = []
    short = {row["type"]: row["short"] for row in r["rows"] if row["short"]}
    if short:
        cands = [d for d in devices if "spare" in (d.get("tags") or []) and not fleet_of(d) and d["device_code"] not in busy
                 and not d.get("controller_device_code") and d.get("location")
                 and str(d.get("status") or "").startswith(("idle", "stowed")) and short.get(d.get("device_type"), 0) > 0]
        cands.sort(key=lambda d: (_dist(base, star_of(d.get("location")), pos), d["device_code"]))
        for d in cands:
            t = d.get("device_type")
            if short.get(t, 0) <= 0:
                continue
            short[t] -= 1
            recruit.append(d)
            if not flies_itself(d):
                picks.append(d)   # boards the touring carrier (also when it's in a system the fleet already has a carrier in)
    seated, dropped = [], set()
    for d in sorted(picks, key=lambda d: (stowable(d), d["device_code"])):   # attach-only devices claim attach points first
        mode = take_seat(free, d)
        if mode:
            seated.append({**d, "_board": mode})
        else:
            dropped.add(d["device_code"])
            problems.append(f"no room on {tourer['device_code']} for {d['device_code']} in {star_of(d.get('location'))}")
    recruit = [d for d in recruit if d["device_code"] not in dropped]
    picks = sorted(seated, key=lambda d: d["device_code"])
    # visit the systems nearest-first from the tourer's system
    by_star: dict[str, list[dict]] = {}
    for d in picks:
        by_star.setdefault(star_of(d.get("location")), []).append(d)
    tour, cur = [], base
    while by_star:
        nxt = min(by_star, key=lambda s: (_dist(cur, s, pos), s))
        tour.append((nxt, by_star.pop(nxt)))
        cur = nxt
    return {"carrier": tourer["device_code"], "carrier_loc": tourer.get("location"), "recruit": recruit, "tour": tour,
            "problems": problems}


def gather_steps(fleet: dict, plan: dict, stars: dict, radii: dict[str, float] | None = None,
                 limit: float | None = None) -> list[dict]:
    tag = fleet_tag(fleet["id"])
    steps = []
    for d in plan["recruit"]:  # spares join the fleet: fleet tag in, spare/home/to out
        rem = [t for t in d.get("tags") or [] if t == "spare" or t.startswith(("home:", "to:"))]
        steps.append(step(f"{d['device_code']} ({d.get('device_type')}) joins {fleet['name']}", f"/devices/{d['device_code']}",
                          {"configuration": {"add_tags": [tag], **({"remove_tags": rem} if rem else {})}}, method="PATCH"))
    carrier, here_loc = plan["carrier"], plan.get("carrier_loc")
    for star, ds in plan["tour"]:
        if here_loc and star == star_of(here_loc):
            dest = here_loc  # already here: just board
        else:
            dest = destination(star, stars)
            st = step(f"{carrier} → {dest} (pick up {len(ds)})", f"/devices/{carrier}", {"command": "travel", "destination": dest},
                      wait=["travel.arrived"], match={"destination": star}, critical=True)
            st["wait_device"] = carrier
            steps.append(st)
            here_loc = dest
        first = len(steps)
        far = [d for d in ds if far_apart(d.get("location"), dest, radii, limit)]   # the carrier fetches these
        near = [d for d in ds if d not in far]
        movers = [d for d in near if d.get("location") != dest]
        for d in movers:
            steps.append(step(f"{d['device_code']} → {dest} (board {carrier})", f"/devices/{d['device_code']}",
                              {"command": "travel", "destination": dest}, critical=True))
        for i, d in enumerate(movers):
            w = _wait_arrive(d["device_code"], dest)
            w["seq0_from"] = first + i
            steps.append(w)
        for d in near:
            steps.append(board_step(carrier, d["device_code"], d.get("_board") or "attach"))
        if far:
            stops: dict[str, list[tuple[str, str]]] = {}
            for d in far:
                stops.setdefault(d["location"], []).append((d["device_code"], d.get("_board") or "attach"))
            tour, here_loc = pickup_tour(carrier, dest, stops, radii)
            steps += tour
    return steps


def fill_plan(fleet: dict, devices: list[dict], stars: dict, busy: set[str], radii: dict[str, float] | None = None,
              limit: float | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """Between missions: fill the fleet's gaps from spares. (steps, recruits, notes)
    With a carrier, it's the mission's gather tour (nearest spares first), then the carrier flies back to where it
    started. Without one, only spares that can fly themselves, or are already where the fleet is, can join."""
    r = roster(fleet, devices)
    plan = gather_plan(fleet, devices, stars, busy)
    if plan["carrier"]:
        recruits = plan["recruit"]
        steps = gather_steps(fleet, plan, stars, radii, limit) if recruits else []
        if recruits and plan["tour"] and star_of(plan["tour"][-1][0]) != star_of(plan["carrier_loc"]):
            back = plan["carrier_loc"]
            st = step(f"{plan['carrier']} → {back} (back with the recruits)", f"/devices/{plan['carrier']}",
                      {"command": "travel", "destination": back}, wait=["travel.arrived"], match={"destination": star_of(back)})
            st["wait_device"] = plan["carrier"]
            steps.append(st)
        base = plan["carrier_loc"]
        notes = plan["problems"]
    else:
        here = r["stars"][0] if r["stars"] else fleet.get("home") or ""
        base = destination(here, stars)
        short = {row["type"]: row["short"] for row in r["rows"] if row["short"]}
        recruits = []
        for d in sorted(devices, key=lambda d: d["device_code"]):
            t = d.get("device_type")
            if (short.get(t, 0) > 0 and "spare" in (d.get("tags") or []) and not fleet_of(d) and d["device_code"] not in busy
                    and not d.get("controller_device_code") and str(d.get("status") or "").startswith(("idle", "stowed"))
                    and (star_of(d.get("location")) == here or flies_itself(d))):
                short[t] -= 1
                recruits.append(d)
        steps = gather_steps(fleet, {"recruit": recruits, "carrier": None, "tour": []}, stars) if recruits else []
        notes = ["no carrier in the fleet: only spares that surge themselves or are already here can join"] if short and any(short.values()) else []
    # recruits that fly themselves come to where the fleet is
    for d in recruits:
        if flies_itself(d) and star_of(d.get("location")) != star_of(base):
            st = step(f"{d['device_code']} → {base} (joins {fleet['name']})", f"/devices/{d['device_code']}",
                      {"command": "travel", "destination": base}, wait=["travel.arrived"], match={"destination": star_of(base)})
            st["wait_device"] = d["device_code"]
            steps.append(st)
    return steps, recruits, notes


def travel_steps(fleet: dict, devices: list[dict], star: str, stars: dict) -> list[dict]:
    """Carriers (with whatever is attached) and self-surging members fly to `star`."""
    r = roster(fleet, devices)
    dest = destination(star, stars)
    movers = [d for d in r["members"] if (is_carrier(d) or flies_itself(d))
              and not d.get("attached_to_device_code") and not d.get("stowed_in_device_code")]
    steps = [step(f"{d['device_code']} → {dest}", f"/devices/{d['device_code']}", {"command": "travel", "destination": dest}, critical=True)
             for d in movers if star_of(d.get("location")) != star]
    n = len(steps)
    for i, d in enumerate([d for d in movers if star_of(d.get("location")) != star]):
        w = _wait_arrive(d["device_code"], dest, star)
        w["seq0_from"] = i
        steps.append(w)
    return steps if n else []


OUTER = ("KUIPER", "OORT")


def deploy_spot(geo: dict) -> str | None:
    """Where a carrier unloads in a system: a Lagrange point, else a planet — never the Kuiper belt or Oort cloud.
    Seen live (2026-10-06): the Surveyors' carrier arrived at LORQELYR-KUIPER (the entry point) and unloaded there."""
    lp = [x for x in geo.get("lagrange") or [] if not any(o in x for o in OUTER)]
    return (lp or geo.get("inner") or [None])[0]


def unload_steps(fleet: dict, devices: list[dict], spot: str | None = None) -> list[dict]:
    """Deploy / detach every passenger. `spot`: carriers in the outer system fly there first."""
    r = roster(fleet, devices)
    carriers = {c["device_code"] for c in r["carriers"]}
    out = []
    if spot:
        loaded = {d.get("attached_to_device_code") or d.get("stowed_in_device_code") for d in r["members"]} & carriers
        movers = [c for c in r["carriers"] if c["device_code"] in loaded and c.get("location") != spot
                  and any(o in (c.get("location") or "") for o in OUTER)]
        for c in movers:
            out.append(step(f"{c['device_code']} → {spot} (to unload inside the system)", f"/devices/{c['device_code']}",
                            {"command": "travel", "destination": spot}, critical=True))
        for i, c in enumerate(movers):
            w = _wait_arrive(c["device_code"], spot)
            w["seq0_from"] = i
            out.append(w)
    for d in r["members"]:
        if d.get("device_type") in ("ftl_relay", "ftl_beacon"):
            continue   # dropped one per system by a survey crew (outposts.py), not all at once
        c = d.get("attached_to_device_code")
        if c in carriers:
            out.append(step(f"{c}: detach {d['device_code']}", f"/devices/{c}", {"command": "detach", "device": d["device_code"]}))
        elif d.get("stowed_in_device_code") in carriers:
            st = step(f"deploy {d['device_code']} from {d['stowed_in_device_code']}", f"/devices/{d['device_code']}",
                      {"command": "deploy"}, wait=["device.deployed"], timeout=SHORT_TIMEOUT)
            st["wait_device"] = d["device_code"]
            out.append(st)
    return out


def belts_in(scan: dict | None) -> list[dict]:
    return [b for b in ((scan or {}).get("asteroid_belt") or {}).get("belts") or [] if isinstance(b, dict) and b.get("designation")]


def richest_belt(star: str, scan: dict | None) -> str | None:
    """The system's richest belt, or None when it has none (KELMONENT, seen live 2026-10-06: the code we used to make up,
    KELMONENT-1-BELT-1, was refused with "Invalid destination format")."""
    belts = belts_in(scan)
    score = {"rich": 4, "high": 3, "moderate": 2, "low": 1, "scarce": 0}
    if not belts:
        return None
    best = max(belts, key=lambda b: sum(score.get(v, 0) for v in (b.get("resources") or {}).values()))
    return best["designation"]


def salvage_body(code: str) -> str:
    """KELMONENT-2-SAL-1 → KELMONENT-2: the game wants the body a salvage belongs to."""
    import re
    return re.sub(r"-SAL-\d+$", "", code or "")


def mining_work_steps(fleet: dict, devices: list[dict], belt: str | None, deliver_to: str | None,
                      salvage: dict | None = None) -> tuple[list[dict], list[str]]:
    """Put the fleet to work on `belt` — or, with `salvage` (a system with no belt), on that salvage: the drones fly to
    its body and the mining controller gets `gather_salvage` there (no belt search: there's no belt to search)."""
    r = roster(fleet, devices)
    ms = [d for d in r["members"] if not is_carrier(d)]
    ctrl = {k: next((d for d in ms if kind(d) == k), None) for k in ("mining_controller", "survey_controller", "transport_controller")}
    problems = [] if ctrl["mining_controller"] else ["no AMI mining controller in the fleet"]
    place = salvage_body(salvage["code"]) if salvage else belt
    workers = [d for d in ms if not flies_itself(d)]
    steps = []
    for d in workers:
        if d.get("location") != place:
            steps.append(step(f"{d['device_code']} → {place}", f"/devices/{d['device_code']}", {"command": "travel", "destination": place},
                              critical=True))
    for i, d in enumerate([d for d in workers if d.get("location") != place]):
        w = _wait_arrive(d["device_code"], place)
        w["seq0_from"] = i
        steps.append(w)

    def put_to_work(c: dict | None, drones: list[str], directive: str, config: dict) -> None:
        if not c:
            return
        code = c["device_code"]
        if drones:
            steps.append(step(f"{code}: adopt {len(drones)}", f"/devices/{code}", {"command": "adopt", "devices": drones}))
        steps.append(step(f"{code}: {directive}" + (f" at {place} ({salvage['code']})" if directive == "gather_salvage" else ""),
                          f"/devices/{code}", {"command": "set_directive", "directive": directive, "configuration": config},
                          critical=True))
        steps.append(step(f"{code}: launch", f"/devices/{code}", {"command": "launch"}))

    def free(t: str, c: dict | None) -> list[str]:
        return [d["device_code"] for d in ms if t in (d.get("device_type") or "") and "controller" not in (d.get("device_type") or "")
                and d.get("controller_device_code") != (c or {}).get("device_code")]
    if salvage:
        put_to_work(ctrl["mining_controller"], free("mining_drone", ctrl["mining_controller"]), "gather_salvage",
                    {"location": place, "recall": False})
    else:
        put_to_work(ctrl["survey_controller"], free("survey_drone", ctrl["survey_controller"]), "belt_search", {})
        put_to_work(ctrl["mining_controller"], free("mining_drone", ctrl["mining_controller"]), "gather_evenly", {})
    if deliver_to and ctrl["transport_controller"]:
        haulers = [d["device_code"] for d in ms if d.get("device_type") in ("cargo_freighter", "transport_drone", "transport_hauler")
                   and d.get("controller_device_code") != ctrl["transport_controller"]["device_code"]]
        put_to_work(ctrl["transport_controller"], haulers, "ferry", {"collect": place, "deliver": deliver_to})
    return steps, problems


def explore_work_steps(fleet: dict, devices: list[dict], spot: str | None = None) -> tuple[list[dict], list[str]]:
    """`spot`: where the survey controller should work (the belt, or the inner system without one — placement.py);
    the controller and its drones go there first."""
    r = roster(fleet, devices)
    ms = [d for d in r["members"] if not is_carrier(d)]
    ctrl = next((d for d in ms if kind(d) == "survey_controller"), None)
    drones = [d["device_code"] for d in ms if "survey_drone" in (d.get("device_type") or "")
              and d.get("controller_device_code") != (ctrl or {}).get("device_code")]
    if not ctrl:
        # no controller: each drone just scans where it is (the system scan already maps the bodies)
        return [step(f"{c}: scan", f"/devices/{c}", {"command": "scan"}) for c in drones], \
            [] if drones else ["no survey controller or drones in the fleet"]
    code = ctrl["device_code"]
    steps = []
    if spot:
        go = [d for d in [ctrl] + [x for x in ms if x["device_code"] in drones] if d.get("location") != spot]
        for d in go:
            steps.append(step(f"{d['device_code']} → {spot}", f"/devices/{d['device_code']}",
                              {"command": "travel", "destination": spot}, critical=d is ctrl))
        for i, d in enumerate(go):
            w = _wait_arrive(d["device_code"], spot)
            w["seq0_from"] = i
            steps.append(w)
    if drones:
        steps.append(step(f"{code}: adopt {len(drones)}", f"/devices/{code}", {"command": "adopt", "devices": drones}))
    steps.append(step(f"{code}: survey_system", f"/devices/{code}",
                      {"command": "set_directive", "directive": "survey_system",
                       "configuration": {"planets": "all", "moons": "all", "recall": True}}, critical=True))
    steps.append(step(f"{code}: launch", f"/devices/{code}", {"command": "launch"}))
    return steps, []


def recall_steps(fleet: dict, devices: list[dict], inventory: dict[str, dict], haul_from: str | None,
                 radii: dict[str, float] | None = None, limit: float | None = None) -> list[dict]:
    """Stop the controllers, (haul) fill freighters, and bring every passenger back to a carrier."""
    r = roster(fleet, devices)
    ms = r["members"]
    steps = []
    for d in ms:
        if "controller" in (d.get("device_type") or "") and (d.get("ami_directive") or {}).get("name"):
            steps.append(step(f"{d['device_code']}: clear directive", f"/devices/{d['device_code']}", {"command": "clear_directive"}))
    if haul_from:
        stock = dict(as_amounts(inventory.get(haul_from) or {}))
        for d in ms:
            if d.get("device_type") == "cargo_freighter" or "cargo_capacity" in d and flies_itself(d):
                free = int(d.get("cargo_capacity") or 0) - int(d.get("cargo_used") or 0)
                take: dict[str, int] = {}
                for res, q in sorted(stock.items(), key=lambda kv: -kv[1]):
                    n = int(min(q, free))
                    if n > 0:
                        take[res], free, stock[res] = n, free - n, q - n
                if take:
                    if d.get("location") != haul_from:
                        steps.append(step(f"{d['device_code']} → {haul_from}", f"/devices/{d['device_code']}",
                                          {"command": "travel", "destination": haul_from}, wait=["travel.arrived"],
                                          match={"destination": haul_from}))
                        steps[-1]["wait_device"] = d["device_code"]
                    steps.append(step(f"{d['device_code']}: load {sum(take.values())}", f"/devices/{d['device_code']}",
                                      {"command": "collect_resources", "resources": take}))
    board, _ = assemble_steps(fleet, devices, radii, limit)
    return steps + board


def ownership(fleet: dict, devices: list[dict]) -> dict:
    """Members not owned by the fleet's `owner` replicant: {"move": [devices], "hosts": [devices hosting a replicant,
    left alone]}. A replicant can only command its own devices, so a fleet works best with one owner."""
    owner = fleet.get("owner")
    out: dict[str, list[dict]] = {"move": [], "hosts": []}
    if not owner:
        return out
    for d in members(fleet, devices):
        if not d.get("replicant_code") or d.get("replicant_code") == owner:
            continue
        out["hosts" if d.get("hosting_replicant") else "move"].append(d)
    return out


def owner_steps(fleet: dict, devices: list[dict], skip: set[str] | None = None) -> list[dict]:
    """change_owner for every member that the fleet's owner doesn't own yet (not devices hosting a replicant)."""
    owner = fleet.get("owner")
    return [step(f"{d['device_code']} ({d.get('device_type')}): owner {d.get('replicant_code')} → {owner}",
                 f"/devices/{d['device_code']}", {"command": "change_owner", "target": owner})
            for d in ownership(fleet, devices)["move"] if d["device_code"] not in (skip or set())
            and "change_owner" in (d.get("available_commands") or ["change_owner"])]


def outside_controllers(fleet: dict, devices: list[dict]) -> list[dict]:
    """Controllers that run members of this fleet but aren't in it themselves (seen live 2026-10-06: the Surveyors'
    survey controller had lost its fleet tag, so the recall left it out and it would have been left behind)."""
    ms = members(fleet, devices)
    mine = {d["device_code"] for d in ms}
    runs = {d.get("controller_device_code") for d in ms if d.get("controller_device_code")}
    return [d for d in devices if d.get("device_code") in runs - mine]


def watch_done(fleet: dict, mission: dict, devices: list[dict], now_iso: str) -> tuple[bool, str, dict]:
    """(done?, why, updates to mission). Mining: exhausted and no site being searched for N minutes.
    Explore: the survey controller reports no_targets."""
    r = roster(fleet, devices)
    ms = r["members"]
    role = fleet.get("role")
    if role == "explore":
        ctrl = next((d for d in ms if kind(d) == "survey_controller"), None)
        st = str(((ctrl or {}).get("ami_directive") or {}).get("_eval_state") or "")
        if not ctrl:
            busy = [d for d in ms if str(d.get("status") or "").startswith(("scanning", "searching", "travel", "cruis"))]
            return (not busy), ("drones finished" if not busy else "drones still scanning"), {}
        return st.startswith(("no_targets", "done", "idle")), (st or "surveying"), {}
    ctrl = next((d for d in ms if kind(d) == "mining_controller"), None)
    st = str(((ctrl or {}).get("ami_directive") or {}).get("_eval_state") or "")
    searching = [d for d in ms if str(d.get("status") or "").startswith("searching")]
    if not st.startswith("exhausted") or searching:
        return False, (st.split(":")[0] or "mining") + (f", {len(searching)} searching" if searching else ""), {"exhausted_since": None}
    since = mission.get("exhausted_since") or now_iso
    from datetime import datetime
    try:
        mins = (datetime.fromisoformat(now_iso) - datetime.fromisoformat(since)).total_seconds() / 60
    except ValueError:
        mins = 0
    limit = int((mission.get("opts") or {}).get("exhausted_minutes") or 30)
    return mins >= limit, f"exhausted for {int(mins)}/{limit} min", {"exhausted_since": since}


def template_wants(phase: dict | None) -> dict[str, int]:
    """A template's (loadout phase's) counts as a fleet loadout: blank ("don't care") and 0 mean none."""
    out = {}
    for t, n in ((phase or {}).get("wants") or {}).items():
        try:
            if t and int(str(n).strip() or 0) > 0:
                out[t] = int(str(n).strip())
        except ValueError:
            pass
    return out


def resolve_template(fleet: dict, cfg: dict) -> dict:
    """A fleet that follows a template takes its loadout from it, so editing the template updates every fleet using it.
    If the template is gone, the fleet keeps the loadout it had. `zero`: the types the template sets to 0 — a stationed
    fleet makes any it has of those spare (blank stays "don't care")."""
    tid = fleet.get("template")
    fleet.setdefault("station", False)
    fleet.setdefault("materials", "")
    if tid:
        ph = next((p for p in cfg.get("phases") or [] if p.get("id") == tid), None)
        if ph:
            fleet["wants"] = template_wants(ph)
            fleet["zero"] = sorted(t for t, n in (ph.get("wants") or {}).items() if t and str(n).strip() == "0")
    return fleet


# --- stationed fleets: a fleet kept at its loadout in its home system ------------------------------------------
AWAY = ("running", "stalled", "ended", "stopped")   # mission states in which the fleet isn't at its station


def away(fleet: dict) -> bool:
    """On a mission (or one that was ended or stopped where it is): its devices are the mission's, not the home
    system's, until the mission is done or cleared ("Back to station")."""
    return (fleet.get("mission") or {}).get("status") in AWAY


def stationed(fleet: dict) -> bool:
    """Kept at its loadout in its home system by the loadout pass, and its devices there work that system."""
    return bool(fleet.get("station")) and bool(fleet.get("home")) and not away(fleet)


def station_wants(fleet: dict) -> dict[str, int]:
    """The loadout a stationed fleet is kept at: its wants, plus 0 for the types its template sets to 0."""
    out = {t: 0 for t in fleet.get("zero") or []}
    out.update({t: int(n) for t, n in (fleet.get("wants") or {}).items()})
    return out


def worked_systems(fleets: list[dict]) -> set[str]:
    """Systems that are home to a stationed fleet: only that fleet works them, so mining and explore missions
    can't target them (fleets may still pass through or wait there)."""
    return {f["home"] for f in fleets if f.get("station") and f.get("home")}


def materials_target(fleet: dict, fleets: list[dict]) -> dict | None:
    """The fleet this fleet's materials go to (its `materials` names another fleet), else None."""
    m = fleet.get("materials") or ""
    if not m or m == "self":
        return None
    return next((f for f in fleets if f.get("id") == m and f.get("id") != fleet.get("id")), None)


def destinations(fleets: list[dict]) -> list[dict]:
    """Fleets that take materials in (`materials: self`)."""
    return [f for f in fleets if f.get("materials") == "self" and f.get("home")]


def _slug(text: str, taken: set[str]) -> str:
    fid = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:20] or "fleet"
    while fid in taken:
        fid += "-2"
    return fid


def migrate(cfg: dict, fleets: list[dict], pos: dict[str, dict] | None = None) -> tuple[dict, list[dict], bool]:
    """One-time move from home fleets to stationed fleets (1.18.0). Each system with a loadout phase becomes a
    stationed fleet homed there, following that phase as its template; the materials roles become fleet settings
    (a destination system's fleet takes materials in, a source system's fleet sends to the nearest destination's).
    Existing mobile fleets keep working as before: not stationed. The devices' `home:` tags are converted by the
    next loadout pass. Returns (cfg, fleets, changed)."""
    import math
    cfg, fleets = dict(cfg or {}), [dict(f) for f in fleets or []]
    if cfg.get("fleets_migrated"):
        return cfg, fleets, False
    for f in fleets:
        f.setdefault("station", False)
        f.setdefault("materials", "")
    taken = {f["id"] for f in fleets}
    phases = {p.get("id") for p in cfg.get("phases") or []}

    def fleet_at(star: str, create: bool) -> dict | None:
        f = next((x for x in fleets if x.get("home") == star and x.get("station")), None)
        if f or not create:
            return f
        f = {"id": _slug(f"{star}-home", taken), "name": f"{star.title()} home", "role": "mining", "home": star,
             "wants": {}, "station": True, "materials": ""}
        taken.add(f["id"])
        fleets.append(f)
        return f

    for star, pid in sorted((cfg.get("systems") or {}).items()):
        if pid in phases:
            f = fleet_at(star, True)
            f["template"] = pid
    roles = cfg.get("roles") or {}
    dests = [fleet_at(s, True) for s, r in sorted(roles.items()) if r == "destination"]
    for f in dests:
        f["materials"] = "self"
    pos = pos or {}

    def dist(a: str, b: str) -> float:
        pa, pb = (pos.get(a) or {}), (pos.get(b) or {})
        if not pa or not pb:
            return 1e9
        return math.dist([pa.get(k, 0) for k in "xyz"], [pb.get(k, 0) for k in "xyz"])
    for s, r in sorted(roles.items()):
        if r == "source":
            f = fleet_at(s, True)
            cands = [d for d in dests if d["home"] != s]
            if cands:
                f["materials"] = min(cands, key=lambda d: (dist(s, d["home"]), d["home"]))["id"]
    cfg.pop("systems", None)
    cfg.pop("roles", None)
    cfg["fleets_migrated"] = True
    return cfg, fleets, True


def short_list(fleet: dict, devices: list[dict]) -> dict[str, int]:
    return {r["type"]: r["short"] for r in roster(fleet, devices)["rows"] if r["short"]}


def summarize(x: Any) -> str:
    return ", ".join(f"{v}× {k.replace('_', ' ')}" for k, v in (x or {}).items())


# --- contracts & trades -----------------------------------------------------------------------------------------------
def deal(m: dict) -> dict:
    """The mission's deal, contract or trade: {"kind", "location", "star", "price" {res: n}, "rewards" {res: n},
    "label"}."""
    c, t = m.get("contract") or {}, m.get("trade") or {}
    if c:
        return {"kind": "contract", "location": c.get("location") or "", "star": star_of(c.get("location")),
                "price": as_amounts(c.get("price") or {}), "rewards": as_amounts(c.get("rewards") or {}),
                "label": f"contract {c.get('title') or c.get('designation')}", "designation": c.get("designation")}
    return {"kind": "trade", "location": t.get("location") or "", "star": star_of(t.get("location")) or t.get("star") or "",
            "price": as_amounts(t.get("price") or {}), "rewards": as_amounts(t.get("rewards") or {}),
            "label": f"trade {t.get('name') or t.get('trade_code')}", "controller": t.get("controller"),
            "trade_code": t.get("trade_code")}


def freighters(fleet: dict, devices: list[dict]) -> list[dict]:
    """Members that carry cargo and fly themselves (cargo freighters)."""
    return [d for d in members(fleet, devices) if flies_itself(d)
            and (d.get("device_type") == "cargo_freighter" or _int(d.get("cargo_capacity")) > 0)]


def site_short(price: dict, at_site: dict) -> dict[str, int]:
    """What the site still lacks: the price minus what's already stockpiled there."""
    have = as_amounts(at_site or {})
    return {r: int(n - have.get(r, 0) + 0.999) for r, n in as_amounts(price).items() if n - have.get(r, 0) > 0}


def pickup_plan(fleet: dict, devices: list[dict], need: dict[str, int], inventory: dict[str, dict], site: str,
                dist) -> tuple[list[dict], dict[str, int], list[str]]:
    """Which freighter collects what from which stockpile: piles nearest the site first (then the biggest), each
    freighter filled up to its free cargo space. Returns (legs [{freighter, pile, take}], still missing, problems)."""
    frs = sorted(freighters(fleet, devices), key=lambda d: d["device_code"])
    if not frs:
        return [], dict(need), ["the fleet has no cargo freighter to carry the materials"]
    left = {r: n for r, n in need.items() if n > 0}
    free = {d["device_code"]: _int(d.get("cargo_capacity")) - _int(d.get("cargo_used")) for d in frs}
    piles = sorted(((loc, as_amounts(items)) for loc, items in inventory.items() if loc != site),
                   key=lambda kv: (dist(star_of(kv[0]), star_of(site)), -sum(kv[1].values()), kv[0]))
    legs = []
    for loc, stock in piles:
        if not left:
            break
        avail = {r: int(stock.get(r, 0)) for r in left if stock.get(r, 0) >= 1}
        while avail and left:
            fr = next((d for d in frs if free[d["device_code"]] > 0), None)
            if not fr:
                break
            code, take = fr["device_code"], {}
            for r in sorted(avail, key=lambda r: -left.get(r, 0)):
                n = min(avail[r], left.get(r, 0), free[code])
                if n > 0:
                    take[r] = n
                    avail[r] -= n
                    left[r] -= n
                    free[code] -= n
            avail = {r: q for r, q in avail.items() if q > 0 and left.get(r, 0) > 0}
            left = {r: q for r, q in left.items() if q > 0}
            if take:
                leg = next((x for x in legs if x["freighter"] == code and x["pile"] == loc), None)
                if leg:
                    for r, q in take.items():
                        leg["take"][r] = leg["take"].get(r, 0) + q
                else:
                    legs.append({"freighter": code, "pile": loc, "take": take})
            if free[code] <= 0:
                continue
            break
    problems = []
    if left:
        if all(f <= 0 for f in free.values()):
            problems.append("the freighters are full: " + ", ".join(f"{q} {r}" for r, q in left.items()) + " left behind")
        else:
            problems.append("not enough in any stockpile: " + ", ".join(f"{q} {r}" for r, q in left.items()) + " missing")
    return legs, left, problems


def pickup_steps(legs: list[dict], devices: list[dict]) -> list[dict]:
    by = {d["device_code"]: d for d in devices}
    steps, at = [], {}
    for leg in legs:
        code, pile = leg["freighter"], leg["pile"]
        cur = at.get(code, (by.get(code) or {}).get("location"))
        if cur != pile:
            st = step(f"{code} → {pile} (pick up)", f"/devices/{code}", {"command": "travel", "destination": pile},
                      wait=["travel.arrived"], match={"destination": pile}, critical=True)
            st["wait_device"] = code
            steps.append(st)
            at[code] = pile
        steps.append(step(f"{code}: load {', '.join(f'{q} {r}' for r, q in leg['take'].items())} at {pile}", f"/devices/{code}",
                          {"command": "collect_resources", "resources": leg["take"]}, critical=True))
    return steps


def site_deliver_steps(fleet: dict, devices: list[dict], site: str, replicant_hosts: set[str],
                       loaded: set[str] | None = None) -> list[dict]:
    """Loaded freighters (`loaded`: the ones the load phase filled; the device list can lag) fly to the site and
    deposit; a member vessel hosting a replicant goes too (it fulfils)."""
    steps = []
    for d in freighters(fleet, devices):
        if _int(d.get("cargo_used")) <= 0 and d["device_code"] not in (loaded or set()):
            continue
        code = d["device_code"]
        if d.get("location") != site:
            st = step(f"{code} → {site}", f"/devices/{code}", {"command": "travel", "destination": site},
                      wait=["travel.arrived"], match={"destination": site}, critical=True)
            st["wait_device"] = code
            steps.append(st)
        steps.append(step(f"{code}: deposit at {site}", f"/devices/{code}", {"command": "deposit_resources"}, critical=True))
    for d in members(fleet, devices):
        if d["device_code"] in replicant_hosts and d.get("location") != site and not d.get("stowed_in_device_code") \
                and not d.get("attached_to_device_code"):
            st = step(f"{d['device_code']} → {site} (brings its replicant)", f"/devices/{d['device_code']}",
                      {"command": "travel", "destination": site}, wait=["travel.arrived"], match={"destination": site})
            st["wait_device"] = d["device_code"]
            steps.append(st)
    return steps


def fulfil_step(dl: dict) -> dict:
    if dl["kind"] == "contract":
        return step(f"fulfil {dl['label']} at {dl['location']}", f"/locations/{dl['location']}/events/{dl['designation']}",
                    None, critical=True)
    return step(f"execute {dl['label']} at {dl['controller']}", f"/devices/{dl['controller']}/trades/{dl['trade_code']}",
                None, critical=True)


def collect_steps(fleet: dict, devices: list[dict], site: str, goods: dict) -> tuple[list[dict], list[str]]:
    """Freighters at the site load the received goods (up to their free space)."""
    left = {r: int(q) for r, q in as_amounts(goods).items() if q >= 1}
    if not left:
        return [], []
    steps = []
    for d in freighters(fleet, devices):
        free = _int(d.get("cargo_capacity")) - _int(d.get("cargo_used"))
        take = {}
        for r in sorted(left, key=lambda r: -left[r]):
            n = min(left[r], free)
            if n > 0:
                take[r], free, left[r] = n, free - n, left[r] - n
        left = {r: q for r, q in left.items() if q > 0}
        if not take:
            continue
        code = d["device_code"]
        if d.get("location") != site:
            st = step(f"{code} → {site}", f"/devices/{code}", {"command": "travel", "destination": site},
                      wait=["travel.arrived"], match={"destination": site})
            st["wait_device"] = code
            steps.append(st)
        steps.append(step(f"{code}: load {', '.join(f'{q} {r}' for r, q in take.items())} (received)", f"/devices/{code}",
                          {"command": "collect_resources", "resources": take}))
    return steps, ([f"no room for {', '.join(f'{q} {r}' for r, q in left.items())}: left at {site}"] if left else [])


def nearest_drop_star(fleets: list[dict], star: str, home: str, dist) -> str:
    """Where received goods go: the nearest system whose fleet takes materials in, else home."""
    dests = sorted({f["home"] for f in destinations(fleets)}, key=lambda s: (dist(s, star), s))
    return dests[0] if dests else home
