"""Mobile fleets: a named group of devices that travels, works a system, and comes back together.

Membership is a tag, `fleet:<id>`, so a fleet's devices are never confused with a system's own in a
busy system: loadouts ignore them, and the other rules (idle miners, salvage, re-open sites, AMI
schedules, auto-survey) don't recruit them. A fleet has
  • a loadout (`wants`: device type → count) and a home system
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
  trade    load (freighters collect the trade's price at home) → assemble → travel → deliver (freighter to the
           trader's location) → trade (POST /devices/<trader>/trades/<code>) → recall → return → unload
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
    "trade": ["load", "assemble", "gather", "travel", "deliver", "trade", "recall", "return", "unload"],
}
ATTACH_TYPES = ("surge_plate", "surge_platform", "surge_carrier", "mobile_fleet")
CAPACITY = {"surge_plate": 1, "surge_platform": 4, "surge_carrier": 9, "mobile_fleet": 36}


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def fleet_tag(fid: str) -> str:
    return "fleet:" + re.sub(r"[^a-z0-9\-_.]", "", fid.lower())[:26]


def fleet_of(d: dict) -> str | None:
    return next((t[6:] for t in d.get("tags") or [] if t.startswith("fleet:")), None)


def members(fleet: dict, devices: list[dict]) -> list[dict]:
    tag = fleet_tag(fleet["id"])
    return [d for d in devices if tag in (d.get("tags") or [])]


def capacity(d: dict) -> int:
    for k in ("attach_capacity",):
        try:
            v = int(d.get(k) or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return CAPACITY.get(d.get("device_type") or "", 0)


def is_carrier(d: dict) -> bool:
    return "surge" in (d.get("features") or []) and capacity(d) > 0 and \
        any(k in (d.get("device_type") or "") for k in ATTACH_TYPES)


def flies_itself(d: dict) -> bool:
    return "surge" in (d.get("features") or []) and not is_carrier(d)


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
            "capacity": sum(capacity(c) for c in carriers),
            "passengers": [d for d in ms if not is_carrier(d) and not flies_itself(d)]}


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


def _wait_arrive(code: str, dest: str, match: str | None = None) -> dict:
    st = step(f"wait for {code} at {dest}", "", None, method="WAIT", wait=["travel.arrived"],
              match={"destination": match or dest}, timeout=STEP_TIMEOUT)
    st["wait_device"] = code
    return st


def assemble_steps(fleet: dict, devices: list[dict]) -> tuple[list[dict], list[str]]:
    """Get every passenger attached to a fleet carrier. Returns (steps, problems)."""
    r = roster(fleet, devices)
    carriers = sorted(r["carriers"], key=lambda c: -capacity(c))
    if not carriers:
        return [], ["the fleet has no surge carrier (mobile fleet / surge carrier / platform / plate)"]
    room = {c["device_code"]: capacity(c) - len(c.get("attached_devices") or []) for c in carriers}
    loc = {c["device_code"]: c.get("location") for c in carriers}
    steps, problems, moves, attach = [], [], [], []
    for d in r["passengers"]:
        code = d["device_code"]
        if d.get("attached_to_device_code") in room:
            continue  # already on board
        here = star_of(d.get("location"))
        c = next((c for c in carriers if room[c["device_code"]] > 0 and star_of(loc[c["device_code"]]) == here), None)
        if not c:
            if here and not any(star_of(loc[x]) == here for x in loc):
                continue  # in a system with no fleet carrier: the gather phase picks it up
            problems.append(f"{code} ({d.get('device_type')}) is in {here or 'transit'} with no fleet carrier room there")
            continue
        room[c["device_code"]] -= 1
        if d.get("location") != loc[c["device_code"]]:
            if "travel" not in (d.get("available_commands") or ["travel"]):
                problems.append(f"{code} can't travel to {loc[c['device_code']]} to board")
                continue
            moves.append((code, loc[c["device_code"]]))
        attach.append((c["device_code"], code))
    first = len(steps)
    for code, dest in moves:
        steps.append(step(f"{code} → {dest} (board)", f"/devices/{code}", {"command": "travel", "destination": dest}, critical=True))
    for i, (code, dest) in enumerate(moves):
        w = _wait_arrive(code, dest)
        w["seq0_from"] = first + i
        steps.append(w)
    for carrier, code in attach:
        st = step(f"{carrier}: attach {code}", f"/devices/{carrier}", {"command": "attach", "device": code},
                  wait=["device.attached"], timeout=SHORT_TIMEOUT, critical=True)
        st["wait_device"] = carrier
        steps.append(st)
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
    tourer = max(carriers, key=lambda c: (capacity(c) - load[c["device_code"]], c["device_code"]))
    room = capacity(tourer) - load[tourer["device_code"]]
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
    if len(picks) > room:
        for d in picks[room:]:
            problems.append(f"no room on {tourer['device_code']} for {d['device_code']} in {star_of(d.get('location'))}")
        dropped = {d["device_code"] for d in picks[room:]}
        recruit = [d for d in recruit if d["device_code"] not in dropped]
        picks = picks[:room]
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


def gather_steps(fleet: dict, plan: dict, stars: dict) -> list[dict]:
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
        movers = [d for d in ds if d.get("location") != dest]
        for d in movers:
            steps.append(step(f"{d['device_code']} → {dest} (board {carrier})", f"/devices/{d['device_code']}",
                              {"command": "travel", "destination": dest}, critical=True))
        for i, d in enumerate(movers):
            w = _wait_arrive(d["device_code"], dest)
            w["seq0_from"] = first + i
            steps.append(w)
        for d in ds:
            a = step(f"{carrier}: attach {d['device_code']}", f"/devices/{carrier}", {"command": "attach", "device": d["device_code"]},
                     wait=["device.attached"], timeout=SHORT_TIMEOUT, critical=True)
            a["wait_device"] = carrier
            steps.append(a)
    return steps


def travel_steps(fleet: dict, devices: list[dict], star: str, stars: dict) -> list[dict]:
    """Carriers (with whatever is attached) and self-surging members fly to `star`."""
    r = roster(fleet, devices)
    dest = destination(star, stars)
    movers = [d for d in r["members"] if is_carrier(d) or (flies_itself(d) and not d.get("attached_to_device_code"))]
    steps = [step(f"{d['device_code']} → {dest}", f"/devices/{d['device_code']}", {"command": "travel", "destination": dest}, critical=True)
             for d in movers if star_of(d.get("location")) != star]
    n = len(steps)
    for i, d in enumerate([d for d in movers if star_of(d.get("location")) != star]):
        w = _wait_arrive(d["device_code"], dest, star)
        w["seq0_from"] = i
        steps.append(w)
    return steps if n else []


def unload_steps(fleet: dict, devices: list[dict]) -> list[dict]:
    r = roster(fleet, devices)
    carriers = {c["device_code"] for c in r["carriers"]}
    out = []
    for d in r["members"]:
        c = d.get("attached_to_device_code")
        if c in carriers:
            out.append(step(f"{c}: detach {d['device_code']}", f"/devices/{c}", {"command": "detach", "device": d["device_code"]}))
    return out


def richest_belt(star: str, scan: dict | None) -> str:
    belts = ((scan or {}).get("asteroid_belt") or {}).get("belts") or []
    score = {"rich": 4, "high": 3, "moderate": 2, "low": 1, "scarce": 0}
    if not belts:
        return f"{star}-BELT-1"
    best = max(belts, key=lambda b: sum(score.get(v, 0) for v in (b.get("resources") or {}).values()))
    return best.get("designation") or f"{star}-BELT-1"


def mining_work_steps(fleet: dict, devices: list[dict], belt: str, deliver_to: str | None) -> tuple[list[dict], list[str]]:
    r = roster(fleet, devices)
    ms = [d for d in r["members"] if not is_carrier(d)]
    ctrl = {k: next((d for d in ms if kind(d) == k), None) for k in ("mining_controller", "survey_controller", "transport_controller")}
    problems = [] if ctrl["mining_controller"] else ["no AMI mining controller in the fleet"]
    workers = [d for d in ms if not flies_itself(d)]
    steps = []
    for d in workers:
        if d.get("location") != belt:
            steps.append(step(f"{d['device_code']} → {belt}", f"/devices/{d['device_code']}", {"command": "travel", "destination": belt},
                              critical=True))
    for i, d in enumerate([d for d in workers if d.get("location") != belt]):
        w = _wait_arrive(d["device_code"], belt)
        w["seq0_from"] = i
        steps.append(w)

    def put_to_work(c: dict | None, drones: list[str], directive: str, config: dict) -> None:
        if not c:
            return
        code = c["device_code"]
        if drones:
            steps.append(step(f"{code}: adopt {len(drones)}", f"/devices/{code}", {"command": "adopt", "devices": drones}))
        steps.append(step(f"{code}: {directive}", f"/devices/{code}",
                          {"command": "set_directive", "directive": directive, "configuration": config}, critical=True))
        steps.append(step(f"{code}: launch", f"/devices/{code}", {"command": "launch"}))

    def free(t: str, c: dict | None) -> list[str]:
        return [d["device_code"] for d in ms if t in (d.get("device_type") or "") and "controller" not in (d.get("device_type") or "")
                and d.get("controller_device_code") != (c or {}).get("device_code")]
    put_to_work(ctrl["survey_controller"], free("survey_drone", ctrl["survey_controller"]), "belt_search", {})
    put_to_work(ctrl["mining_controller"], free("mining_drone", ctrl["mining_controller"]), "gather_evenly", {})
    if deliver_to and ctrl["transport_controller"]:
        haulers = [d["device_code"] for d in ms if d.get("device_type") in ("cargo_freighter", "transport_drone", "transport_hauler")
                   and d.get("controller_device_code") != ctrl["transport_controller"]["device_code"]]
        put_to_work(ctrl["transport_controller"], haulers, "ferry", {"collect": belt, "deliver": deliver_to})
    return steps, problems


def explore_work_steps(fleet: dict, devices: list[dict]) -> tuple[list[dict], list[str]]:
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
    if drones:
        steps.append(step(f"{code}: adopt {len(drones)}", f"/devices/{code}", {"command": "adopt", "devices": drones}))
    steps.append(step(f"{code}: survey_system", f"/devices/{code}",
                      {"command": "set_directive", "directive": "survey_system",
                       "configuration": {"planets": "all", "moons": "all", "recall": True}}, critical=True))
    steps.append(step(f"{code}: launch", f"/devices/{code}", {"command": "launch"}))
    return steps, []


def recall_steps(fleet: dict, devices: list[dict], inventory: dict[str, dict], haul_from: str | None) -> list[dict]:
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
    board, _ = assemble_steps(fleet, devices)
    return steps + board


def trade_load_steps(fleet: dict, devices: list[dict], price: dict, home_pile: str | None) -> tuple[list[dict], list[str]]:
    r = roster(fleet, devices)
    fr = [d for d in r["members"] if d.get("device_type") == "cargo_freighter"]
    if not fr:
        return [], ["the trade fleet has no cargo freighter to carry the price"]
    if not home_pile:
        return [], ["no stockpile at home to load from"]
    d = fr[0]
    steps = []
    if d.get("location") != home_pile:
        st = step(f"{d['device_code']} → {home_pile}", f"/devices/{d['device_code']}", {"command": "travel", "destination": home_pile},
                  wait=["travel.arrived"], match={"destination": home_pile})
        st["wait_device"] = d["device_code"]
        steps.append(st)
    steps.append(step(f"{d['device_code']}: load the price", f"/devices/{d['device_code']}",
                      {"command": "collect_resources", "resources": {k: int(v) for k, v in as_amounts(price).items()}}, critical=True))
    return steps, []


def trade_deliver_steps(fleet: dict, devices: list[dict], trader_loc: str) -> list[dict]:
    r = roster(fleet, devices)
    out = []
    for d in r["members"]:
        if d.get("device_type") == "cargo_freighter" and d.get("location") != trader_loc:
            st = step(f"{d['device_code']} → {trader_loc}", f"/devices/{d['device_code']}", {"command": "travel", "destination": trader_loc},
                      wait=["travel.arrived"], match={"destination": trader_loc})
            st["wait_device"] = d["device_code"]
            out.append(st)
    return out


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


def short_list(fleet: dict, devices: list[dict]) -> dict[str, int]:
    return {r["type"]: r["short"] for r in roster(fleet, devices)["rows"] if r["short"]}


def summarize(x: Any) -> str:
    return ", ".join(f"{v}× {k.replace('_', ' ')}" for k, v in (x or {}).items())
