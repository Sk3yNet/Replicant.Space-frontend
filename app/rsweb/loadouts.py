"""System loadouts: how many of each device a system should have, by its development phase.

Config (kv "loadouts"):
    phases:  [{id, name, order, wants: {device_type: n}}]     e.g. "1 Survey", "2 Mining outpost"
    systems: {STAR: phase_id}                                 the phase a system is at
    ignore_tags: ["keep", ...]   devices with any of these are invisible to loadouts (never counted, tagged or moved)
    settings: print_missing, need_stock, carriers_return, use_replicant_vessels, every_minutes

How a pass works (plan() is pure; the engine turns the plan into jobs):
  1. Count each phased system's devices by type. Devices tagged `to:<star>` count for that star.
  2. Above the loadout → the extras get the `spare` tag. At or below it → local spares lose the tag.
  3. Short → take `spare` devices from other systems (nearest first): they're tagged `to:<star>`
     and the `spare` tag is removed. Still short → print on an autofactory whose stock covers the
     cost (the system's own first); the print is tagged `to:<star>` so it is routed when it comes out.
  4. Delivery: a device tagged for another system flies there itself if it can surge; otherwise a
     surge-capable carrier in its system stows it, flies, and deploys it. On arrival the `to:` tag goes.
Systems without a phase are left alone, except that `spare` devices there can be sent elsewhere.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any

from .automations import SHORT_TIMEOUT, STEP_TIMEOUT, step
from .shapes import as_amounts

SPARE = "spare"
DEFAULT_SETTINGS = {"print_missing": True, "need_stock": True, "carriers_return": True,
                    "use_replicant_vessels": False, "every_minutes": 15}


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def to_tag(star: str) -> str:
    return "to:" + re.sub(r"[^a-z0-9\-_:.]", "", star.lower())[:29]


def home_tag(star: str) -> str:
    return "home:" + re.sub(r"[^a-z0-9\-_:.]", "", star.lower())[:27]


def home_of(d: dict, stars: set[str]) -> str | None:
    """The system a device belongs to (`home:<star>` tag), matched back to a known star code.
    With more than one home tag (shouldn't happen), the one for the system it's in wins."""
    homes = []
    for t in d.get("tags") or []:
        if t.startswith("home:"):
            want = t[5:]
            homes.append(next((s for s in stars if home_tag(s)[5:] == want), want.upper()))
    if not homes:
        return None
    here = star_of(d.get("location"))
    return here if here in homes else homes[0]


def bound_for(d: dict, stars: set[str]) -> str | None:
    """The star a device is tagged to go to (`to:<star>`), matched back to a known star code."""
    for t in d.get("tags") or []:
        if t.startswith("to:"):
            want = t[3:]
            return next((s for s in stars if to_tag(s)[3:] == want), want.upper())
    return None


def normalize(cfg: Any) -> dict:
    cfg = dict(cfg or {})
    cfg.setdefault("phases", [])
    cfg.setdefault("systems", {})
    cfg.setdefault("ignore_tags", [])
    cfg.setdefault("roles", {})          # STAR -> "source" | "destination" (materials)
    s = {**DEFAULT_SETTINGS, **(cfg.get("settings") or {})}
    cfg["settings"] = s
    cfg["phases"] = sorted(cfg["phases"], key=lambda p: (p.get("order", 0), p.get("name", "")))
    return cfg


def phase_of(cfg: dict, star: str) -> dict | None:
    pid = cfg["systems"].get(star)
    return next((p for p in cfg["phases"] if p["id"] == pid), None) if pid else None


def _dist(a: str, b: str, pos: dict[str, dict]) -> float:
    if a == b:
        return 0.0
    pa, pb = pos.get(a), pos.get(b)
    if not pa or not pb:
        return 1e9
    return math.dist([pa.get(k, 0) for k in "xyz"], [pb.get(k, 0) for k in "xyz"])


def _idle(d: dict) -> bool:
    return str(d.get("status") or "").startswith(("idle", "stowed", "inactive", "stationary"))


def _cap(d: dict) -> float:
    try:
        v = float(d.get("operational_capacity"))
        return v * 100 if v <= 1 else v
    except (TypeError, ValueError):
        return 100.0


def can_surge(d: dict) -> bool:
    return "surge" in (d.get("features") or []) and "travel" in (d.get("available_commands") or ["travel"])


def is_factory(d: dict) -> bool:
    return "enqueue_print" in (d.get("available_commands") or []) or "autofactory" in (d.get("device_type") or "")


def plan(cfg: dict, devices: list[dict], blueprints: list[dict], inventory: dict[str, dict], stars: dict[str, dict],
         replicant_hosts: dict[str, str], busy: set[str], orders: list[dict], stowed_map: dict[str, list[str]],
         only: set[str] | None = None) -> dict:
    """Work out what to tag, print and move. `only`: limit shortfall filling to these stars."""
    cfg = normalize(cfg)
    s = cfg["settings"]
    ignore = set(cfg["ignore_tags"])
    known_stars = set(stars) | {star_of(d.get("location")) for d in devices} | set(cfg["systems"])
    pos = {k: (v or {}).get("position") or {} for k, v in stars.items()}
    bps = {b["device_type"]: b for b in blueprints}
    stowed_in = {c: carrier for carrier, kids in (stowed_map or {}).items() for c in kids}
    for d in devices:  # the device list says it directly
        if d.get("stowed_in_device_code") or d.get("attached_to_device_code"):
            stowed_in[d["device_code"]] = d.get("stowed_in_device_code") or d.get("attached_to_device_code")

    def visible(d: dict) -> bool:
        tags = set(d.get("tags") or [])
        return not (ignore & tags) and d.get("device_code") not in replicant_hosts and not any(t.startswith("fleet:") for t in tags)

    pool = [d for d in devices if visible(d)]
    loc_of = {d.get("device_code"): d.get("location") for d in devices}
    for d in devices:
        if d.get("in_control_range") is False:  # out of comms range: can't be commanded right now
            busy = set(busy) | {d.get("device_code")}
        if str(d.get("status") or "").startswith(("tracking", "searching")):  # moving it would close its site
            busy = set(busy) | {d.get("device_code")}

    def ctrl_star(d: dict) -> str | None:
        c = d.get("controller_device_code")
        return star_of(loc_of.get(c)) if c and loc_of.get(c) else None

    by_code_all = {d.get("device_code"): d for d in devices}

    def is_ferry_ctrl(code: str | None) -> bool:
        c = by_code_all.get(code) or {}
        return "transport" in (c.get("device_type") or "") and (
            ((c.get("ami_directive") or {}).get("name") == "ferry") or FERRY_TAG in (c.get("tags") or []))

    def effective_home(d: dict) -> str | None:
        cs = ctrl_star(d)
        if cs and is_ferry_ctrl(d.get("controller_device_code")):
            return cs  # a ferry's freighters/drones/plates belong to the controller's system wherever the run takes them
        if cs and cs == star_of(d.get("location")):
            return cs  # adopted by a controller where it is: it works there now
        return home_of(d, known_stars)
    report: dict[str, dict] = {}
    tag_add: dict[str, set] = defaultdict(set)
    tag_remove: dict[str, set] = defaultdict(set)
    moves: dict[str, str] = {}        # device -> destination star
    unmet: list[dict] = []

    # incoming to each star: devices already tagged to it, plus prints ordered for it
    incoming: dict[str, Counter] = defaultdict(Counter)
    for d in pool:
        dest = bound_for(d, known_stars)
        if dest and star_of(d.get("location")) != dest:
            incoming[dest][d.get("device_type")] += 1
            moves[d["device_code"]] = dest
    for o in orders:
        incoming[o["star"]][o["device_type"]] += 1

    def local(star: str) -> list[dict]:
        """Devices that belong to `star`: tagged home:<star> wherever they are right now (a carrier out on a
        delivery still counts at home), or untagged and sitting in it. Devices bound elsewhere don't count."""
        out = []
        for d in pool:
            dest = bound_for(d, known_stars)
            if dest:  # on its way somewhere: it counts there once it has arrived (until it is re-homed)
                if dest == star and star_of(d.get("location")) == star:
                    out.append(d)
                continue
            if SPARE in (d.get("tags") or []) and not home_of(d, known_stars):
                continue  # spare = belongs to no system until it's sent somewhere (or reclaimed)
            home = effective_home(d) or star_of(d.get("location"))
            if home == star:
                out.append(d)
        return out

    # 1-2: count, mark extras as spare, un-spare what's needed
    donors: dict[str, list[dict]] = defaultdict(list)  # type -> spare devices anywhere
    for star, pid in cfg["systems"].items():
        ph = phase_of(cfg, star)
        if not ph:
            continue
        here = local(star)
        by_type: dict[str, list[dict]] = defaultdict(list)
        for d in here:
            by_type[d.get("device_type") or "device"].append(d)
        rows = []
        wants = ph.get("wants") or {}
        for t in sorted(wants):  # types the phase doesn't mention are "don't care": never spare, never filled
            want = int(wants.get(t) or 0)
            have = by_type.get(t, [])
            inc = incoming[star][t]
            surplus = max(0, len(have) - want)
            # who stays: non-spare first, busy ones (can't be moved anyway), then idle-less-healthy last
            # away from home (e.g. out delivering) is never picked as spare
            ranked = sorted(have, key=lambda d: (SPARE in (d.get("tags") or []), not d.get("controller_device_code"),
                                                 star_of(d.get("location")) == star,
                                                 d["device_code"] not in busy, _idle(d), -_cap(d), d["device_code"]))
            keep, extra = ranked[:len(have) - surplus], ranked[len(have) - surplus:]
            pinned = [d for d in extra if d["device_code"] in busy]  # busy (tracking a site, mid-job, out of range): never spare
            if pinned:
                extra = [d for d in extra if d["device_code"] not in busy]
                keep = keep + pinned
            for d in keep:
                if SPARE in (d.get("tags") or []):
                    tag_remove[d["device_code"]].add(SPARE)
            for d in extra:  # spare: drop its home, so it belongs to no system until it's assigned again
                tags = set(d.get("tags") or [])
                if SPARE not in tags:
                    tag_add[d["device_code"]].add(SPARE)
                tag_remove[d["device_code"]].update(t for t in tags if t.startswith("home:"))
            for d in keep:  # every device counted for this system carries exactly one home tag: this one
                tags = set(d.get("tags") or [])
                if home_tag(star) not in tags:
                    tag_add[d["device_code"]].add(home_tag(star))
                tag_remove[d["device_code"]].update(t for t in tags if t.startswith("home:") and t != home_tag(star))
            away = [d["device_code"] for d in keep if star_of(d.get("location")) != star]
            spares_here = [d["device_code"] for d in pool if d.get("device_type") == t and star_of(d.get("location")) == star
                           and SPARE in (d.get("tags") or []) and not home_of(d, known_stars)]
            rows.append({"type": t, "want": want, "have": len(have), "incoming": inc, "away": away, "spares_here": spares_here,
                         "short": max(0, want - len(have) - inc), "surplus": surplus,
                         "spare": [d["device_code"] for d in extra]})
        report[star] = {"star": star, "phase": ph, "rows": rows,
                        "short": sum(r["short"] for r in rows), "surplus": sum(r["surplus"] for r in rows)}

    for d in pool:
        code = d["device_code"]
        tags = set(d.get("tags") or [])
        spare_now = (SPARE in tags or SPARE in tag_add[code]) and SPARE not in tag_remove[code]
        if spare_now and code not in moves and code not in busy:
            donors[d.get("device_type") or "device"].append(d)

    # 3: fill shortfalls — spares first, then prints
    reserved: dict[str, Counter] = defaultdict(Counter)  # location -> resources set aside in this plan
    prints: list[dict] = []
    factories = [d for d in pool if is_factory(d) and d["device_code"] not in busy]

    qcap_default = {b["device_type"]: int(b.get("queue_size") or 0) for b in blueprints}
    queue_free: dict[str, int] = {}
    for f in factories:
        cap = int(qcap_default.get(f.get("device_type")) or f.get("queue_capacity") or 10)
        used_q = len(f.get("print_queue") or []) + (1 if f.get("printing") or str(f.get("status") or "").startswith("printing") else 0)
        queue_free[f["device_code"]] = max(0, cap - used_q)

    def factory_for(t: str, star: str) -> tuple[dict | None, str]:
        bp = bps.get(t)
        if not bp:
            return None, f"no blueprint for {t}"
        cost = as_amounts(bp.get("resources"))
        if not factories:
            return None, "no autofactory"
        best = None
        open_f = [f for f in factories if queue_free.get(f["device_code"], 1) > 0]
        if not open_f:
            return None, "every autofactory's print queue is full"
        for f in sorted(open_f, key=lambda f: (star_of(f.get("location")) != star, _dist(star_of(f.get("location")), star, pos))):
            stock = as_amounts(inventory.get(f.get("location")) or {})
            free = {r: stock.get(r, 0.0) - reserved[f.get("location")][r] for r in cost}
            if all(free[r] >= v for r, v in cost.items()):
                return f, ""
            best = best or f
        if s["need_stock"]:
            return None, f"no autofactory has the materials for {t}"
        return best, "queued without enough stock (it waits for materials)"

    stars_order = sorted(report, key=lambda st: (-report[st]["short"], st))
    for star in stars_order:
        if only and star not in only:
            continue
        for row in report[star]["rows"]:
            need = row["short"]
            if need <= 0:
                continue
            cands = sorted(donors.get(row["type"], []),
                           key=lambda d: (_dist(star_of(d.get("location")), star, pos), not _idle(d), -_cap(d), d["device_code"]))
            for d in cands[:need]:
                donors[row["type"]].remove(d)
                code = d["device_code"]
                tag_remove[code].add(SPARE)
                tag_add[code].discard(SPARE)
                moves[code] = star
                row.setdefault("from_spares", []).append(code)
                need -= 1
            if need > 0 and s["print_missing"]:
                f, why = factory_for(row["type"], star)
                if f:
                    cost = as_amounts((bps.get(row["type"]) or {}).get("resources"))
                    n = need
                    if s["need_stock"]:  # only as many as the stock covers
                        stock = as_amounts(inventory.get(f.get("location")) or {})
                        n = min(need, min((int((stock.get(r, 0) - reserved[f["location"]][r]) // v) for r, v in cost.items() if v > 0),
                                          default=need))
                    n = min(n, queue_free.get(f["device_code"], n))
                    queue_free[f["device_code"]] = queue_free.get(f["device_code"], n) - n
                    for r, v in cost.items():
                        reserved[f["location"]][r] += v * n
                    prints.append({"factory": f["device_code"], "factory_star": star_of(f.get("location")),
                                   "device_type": row["type"], "n": n, "star": star, "note": why})
                    row["printing"] = n
                    need -= n
                    if need > 0:
                        unmet.append({"star": star, "type": row["type"], "n": need,
                                      "why": f"room/materials for only {n} on {f['device_code']} this pass"})
                else:
                    unmet.append({"star": star, "type": row["type"], "n": need, "why": why})
            elif need > 0:
                unmet.append({"star": star, "type": row["type"], "n": need, "why": "no spares (printing is off)"})

    # devices away from their home system with nothing to do there (e.g. printed on another system's autofactory,
    # or left behind) go home — by surging themselves or on a carrier, like any other delivery
    returning = []
    for d in pool:
        code, home = d["device_code"], home_of(d, known_stars)
        here = star_of(d.get("location"))
        if (home and here and here != home and code not in moves and code not in busy and not d.get("controller_device_code")
                and SPARE not in (d.get("tags") or []) and not bound_for(d, known_stars)
                and str(d.get("status") or "").startswith(("idle", "stowed"))):
            moves[code] = home
            returning.append(code)

    # tag hygiene, for every device (whether or not its type is in a phase):
    #  • run by a controller (working) or a taxi plate → never spare
    #  • spare → no home tag (spare = belongs to no system)
    for d in pool:
        code, tags = d["device_code"], set(d.get("tags") or [])
        if code in moves or SPARE not in tags or SPARE in tag_remove[code]:
            continue
        if d.get("controller_device_code") or d.get("taxi_mode") == "taxi" or "taxi" in tags:
            tag_remove[code].add(SPARE)
            tag_add[code].discard(SPARE)
        else:
            homes = {t for t in tags if t.startswith("home:")}
            if homes:
                tag_remove[code].update(homes)

    # a device run by a controller in another system (e.g. delivered without being released) is let go
    releases: dict[str, list[str]] = defaultdict(list)
    for d in pool:
        cs = ctrl_star(d)
        if is_ferry_ctrl(d.get("controller_device_code")) or d.get("taxi_mode") == "taxi":
            continue  # a ferry's drones, freighters and taxi plates are meant to be in other systems
        # anything else (incl. an in-system transport controller on delivery/shuttle/consolidate) can't use a device
        # that's in another system: release it so it can work where it is
        if cs and cs != star_of(d.get("location")) and d["device_code"] not in busy and d["device_code"] not in moves:
            releases[d["controller_device_code"]].append(d["device_code"])

    # 4: deliveries for everything bound somewhere else
    by_code = {d["device_code"]: d for d in pool}
    arrived, self_moves, batches = [], [], defaultdict(list)
    for code, dest in moves.items():
        d = by_code.get(code)
        if not d or code in busy:
            continue
        here = star_of(d.get("location"))
        if here == dest:
            arrived.append(code)
        elif can_surge(d) and code not in stowed_in:
            self_moves.append((code, dest))
        else:
            batches[(here, dest)].append(code)
    for d in pool:  # tagged earlier and already there
        dest = bound_for(d, known_stars)
        if dest and star_of(d.get("location")) == dest and d["device_code"] not in busy and d["device_code"] not in arrived:
            arrived.append(d["device_code"])

    carriers = [d for d in devices if d["device_code"] not in busy and can_surge(d) and _carrier_cap(d, bps) > 0
                and not (ignore & set(d.get("tags") or [])) and (s["use_replicant_vessels"] or d["device_code"] not in replicant_hosts)
                and not str(d.get("status") or "").startswith(("travel", "cruis", "surg", "stowed"))
                and not d.get("controller_device_code")]   # run by a controller (e.g. a ferry's taxi plates) = busy
    moving = set(moves) | {c for c, _ in self_moves}
    carriers = [c for c in carriers if c["device_code"] not in moving and not bound_for(c, known_stars)]
    used: set[str] = set()
    deliveries = []
    for (here, dest), codes in sorted(batches.items()):
        codes = sorted(codes)
        while codes:
            stowable = all("stow" in (by_code[x].get("available_commands") or ["stow"]) for x in codes)
            options = [c for c in carriers if star_of(c.get("location")) == here and c["device_code"] not in used
                       and c["device_code"] not in codes and (stowable or carry_mode(c, bps) == "attach")]
            if not options:
                unmet.append({"star": dest, "type": ", ".join(sorted({by_code[c].get('device_type') for c in codes})),
                              "n": len(codes), "why": f"waiting for a surge-capable carrier in {here}"})
                break
            c = max(options, key=lambda c: (_free(c, bps, stowed_map), c["device_code"]))
            room = int(_free(c, bps, stowed_map))
            if room <= 0:
                used.add(c["device_code"])
                continue
            load, codes = codes[:room], codes[room:]
            used.add(c["device_code"])
            deliveries.append({"carrier": c["device_code"], "carrier_loc": c.get("location"), "from": here, "to": dest,
                               "devices": load, "replicant": replicant_hosts.get(c["device_code"]), "mode": carry_mode(c, bps)})

    return {"returning": returning, "releases": {k: sorted(v) for k, v in releases.items()}, "report": report, "tag_add": {k: sorted(v) for k, v in tag_add.items() if v},
            "tag_remove": {k: sorted(v) for k, v in tag_remove.items() if v}, "moves": moves, "prints": prints,
            "self_moves": self_moves, "deliveries": deliveries, "arrived": sorted(set(arrived)), "unmet": unmet,
            "by_code": by_code}


ATTACH_CARRIERS = ("surge_plate", "surge_platform", "surge_carrier", "mobile_fleet")


def carry_mode(d: dict, bps: dict) -> str:
    """"attach" for surge plates/platforms/carriers/fleets (devices attach to them and ride along on a surge),
    "stow" for vessels with a hold."""
    t = d.get("device_type") or ""
    if any(k in t for k in ATTACH_CARRIERS):
        return "attach"
    for src in (d, bps.get(t) or {}):
        try:
            if float(src.get("attach_capacity") or 0) > 0 and not float(src.get("stow_capacity") or 0):
                return "attach"
        except (TypeError, ValueError):
            pass
    return "stow"


def _carrier_cap(d: dict, bps: dict) -> float:
    for src in (d, bps.get(d.get("device_type")) or {}):
        for key in ("stow_capacity", "attach_capacity"):
            try:
                v = float(src.get(key))
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
    t = d.get("device_type") or ""
    return {"surge_plate": 1, "surge_platform": 4, "surge_carrier": 9, "mobile_fleet": 36}.get(t, 0)


def _free(d: dict, bps: dict, stowed_map: dict) -> float:
    return _carrier_cap(d, bps) - len((stowed_map or {}).get(d["device_code"], []))


def destination(star: str, stars: dict[str, dict]) -> str:
    """Where to send things in a system: its entry point when the catalogue knows it, else the star."""
    return (stars.get(star) or {}).get("entry_point") or star


# --- turning a plan into job steps -----------------------------------------------------------------------
def tag_step(code: str, add: list[str] | None = None, remove: list[str] | None = None) -> dict:
    cfg: dict = {}
    if add:
        cfg["add_tags"] = add
    if remove:
        cfg["remove_tags"] = remove
    what = " ".join([f"+{t}" for t in add or []] + [f"−{t}" for t in remove or []])
    return step(f"tag {code} {what}", f"/devices/{code}", {"configuration": cfg}, method="PATCH")


def tag_steps(p: dict) -> list[dict]:
    """Spare marking only — tags for moves are set by the delivery jobs themselves."""
    out = []
    for code in sorted(set(p["tag_add"]) | set(p["tag_remove"])):
        if code in p["moves"]:
            continue
        add, rem = p["tag_add"].get(code, []), p["tag_remove"].get(code, [])
        if add or rem:
            out.append(tag_step(code, add, rem))
    return out


def print_steps(pr: dict) -> list[dict]:
    return [step(f"print {pr['n']}× {pr['device_type']} on {pr['factory']} for {pr['star']}", f"/devices/{pr['factory']}",
                 {"command": "enqueue_print", "device_type": pr["device_type"], "quantity": pr["n"], "tags": [to_tag(pr["star"])]})]


def release_step(d: dict) -> list[dict]:
    """A device leaving for another system is released from its AMI controller first, or the controller keeps
    commanding it from the old system."""
    c = d.get("controller_device_code")
    if not c:
        return []
    return [step(f"{c}: release {d['device_code']}", f"/devices/{c}", {"command": "release", "devices": [d["device_code"]]})]


def self_move_steps(code: str, dest_star: str, stars: dict, d: dict) -> list[dict]:
    tags = set(d.get("tags") or [])
    steps = release_step(d)
    if to_tag(dest_star) not in tags or SPARE in tags:
        steps.append(tag_step(code, [to_tag(dest_star)] if to_tag(dest_star) not in tags else None,
                              [SPARE] if SPARE in tags else None))
    dest = destination(dest_star, stars)
    st = step(f"{code} → {dest}", f"/devices/{code}", {"command": "travel", "destination": dest},
              wait=["travel.arrived"], match={"destination": dest_star}, critical=True)
    st["wait_device"] = code
    steps.append(st)
    steps.append(rehome_step(code, d, dest_star))
    return steps


def rehome_step(code: str, d: dict, star: str) -> dict:
    """On arrival: drop the to: tag (and any spare / old home), and make the new system its home."""
    old = [t for t in d.get("tags") or [] if (t.startswith("home:") and t != home_tag(star)) or t == SPARE]
    return tag_step(code, [home_tag(star)], sorted(set(old) | {to_tag(star)}))


def delivery_steps(dl: dict, by_code: dict, stars: dict, carriers_return: bool) -> list[dict]:
    carrier, cloc, dest_star = dl["carrier"], dl["carrier_loc"], dl["to"]
    dest = destination(dest_star, stars)
    rep = dl.get("replicant")
    travel_path = f"/replicants/{rep}/travel" if rep else f"/devices/{carrier}"

    def move(to: str, why: str, match: str) -> dict:
        body = {"destination": to} if rep else {"command": "travel", "destination": to}
        st = step(f"{carrier} → {to} ({why})", travel_path, body, wait=["travel.arrived"], match={"destination": match},
                  critical=True)
        st["wait_device"] = carrier
        return st

    steps: list[dict] = []
    for code in dl["devices"]:
        steps += release_step(by_code.get(code, {}))
    for code in dl["devices"]:  # mark them as on their way (and not spare any more)
        tags = set(by_code.get(code, {}).get("tags") or [])
        add = [to_tag(dest_star)] if to_tag(dest_star) not in tags else None
        rem = [SPARE] if SPARE in tags else None
        if add or rem:
            steps.append(tag_step(code, add, rem))
    first = len(steps)
    fly = [c for c in dl["devices"] if by_code.get(c, {}).get("location") != cloc]
    for code in fly:
        steps.append(step(f"{code} → {cloc} (to board {carrier})", f"/devices/{code}", {"command": "travel", "destination": cloc}))
    for i, code in enumerate(fly):
        w = step(f"wait for {code} at {cloc}", "", None, method="WAIT", wait=["travel.arrived"], match={"destination": cloc},
                 timeout=STEP_TIMEOUT)
        w["wait_device"] = code
        w["seq0_from"] = first + i
        steps.append(w)
    attach = dl.get("mode") == "attach"
    # Boarding and unloading are critical: if a device doesn't get on, the carrier must not fly off without it
    # (and it must not be re-homed to a system it never reached); if it can't get off, it must not ride back.
    for code in dl["devices"]:
        if attach:  # the carrier does the attaching: POST /devices/<carrier> {"command": "attach", "device": <cargo>}
            st = step(f"{carrier}: attach {code}", f"/devices/{carrier}", {"command": "attach", "device": code},
                      wait=["device.attached"], timeout=SHORT_TIMEOUT, critical=True)
            st["wait_device"] = carrier
            steps.append(st)
            continue
        else:
            st = step(f"stow {code} in {carrier}", f"/devices/{code}", {"command": "stow", "target": carrier},
                      wait=["device.stowed"], timeout=SHORT_TIMEOUT, critical=True)
        st["wait_device"] = code
        steps.append(st)
    steps.append(move(dest, f"deliver {len(dl['devices'])} to {dest_star}", dest_star))
    for code in dl["devices"]:
        if attach:
            st = step(f"{carrier}: detach {code} in {dest_star}", f"/devices/{carrier}", {"command": "detach", "device": code},
                      critical=True)
        else:
            st = step(f"deploy {code} in {dest_star}", f"/devices/{code}", {"command": "deploy"}, wait=["device.deployed"],
                      timeout=SHORT_TIMEOUT, critical=True)
            st["wait_device"] = code
        steps.append(st)
        steps.append(rehome_step(code, by_code.get(code, {}), dest_star))
    if carriers_return and cloc:
        steps.append(move(cloc, "return", star_of(cloc)))
    return steps


def arrived_steps(code: str, d: dict, stowed_in: dict) -> list[dict]:
    steps = []
    status = str(d.get("status") or "")
    if d.get("attached_to_device_code") or "attach" in status:
        carrier = d.get("attached_to_device_code") or stowed_in.get(code)
        steps.append(step(f"{carrier}: detach {code}", f"/devices/{carrier}", {"command": "detach", "device": code}, critical=True))
    elif code in stowed_in or status.startswith("stowed"):
        st = step(f"deploy {code}", f"/devices/{code}", {"command": "deploy"}, wait=["device.deployed"], timeout=SHORT_TIMEOUT,
                  critical=True)
        st["wait_device"] = code
        steps.append(st)
    dest = next((t[3:].upper() for t in d.get("tags") or [] if t.startswith("to:")), None) or star_of(d.get("location"))
    steps.append(rehome_step(code, d, dest))
    return steps


def describe(p: dict) -> list[str]:
    """Plain-language list of what a pass would do."""
    out = []
    for ctrl, codes in sorted((p.get("releases") or {}).items()):
        out.append(f"{ctrl} releases {', '.join(codes)} (run from another system)")
    homes = Counter(t for code, tags in p["tag_add"].items() if code not in p["moves"] for t in tags if t.startswith("home:"))
    for t, n in sorted(homes.items()):
        out.append(f"tag {n} device(s) {t} (they count for that system wherever they go)")
    for code, tags in sorted(p["tag_add"].items()):
        if code not in p["moves"] and SPARE in tags:
            d = p["by_code"].get(code, {})
            out.append(f"mark {code} ({d.get('device_type')}, {star_of(d.get('location'))}) as spare")
    for code, tags in sorted(p["tag_remove"].items()):
        if code not in p["moves"] and SPARE in tags:
            out.append(f"{code} is needed where it is: remove spare")
    for pr in p["prints"]:
        out.append(f"print {pr['n']}× {pr['device_type']} on {pr['factory']} ({pr['factory_star']}) for {pr['star']}"
                   + (f" — {pr['note']}" if pr.get("note") else ""))
    for code, dest in p["self_moves"]:
        out.append(f"{code} ({p['by_code'][code].get('device_type')}) flies to {dest}")
    for dl in p["deliveries"]:
        how = "attaches" if dl.get("mode") == "attach" else "stows"
        out.append(f"{dl['carrier']} {how} {', '.join(dl['devices'])} and carries them from {dl['from']} to {dl['to']}")
    for code in p.get("returning") or []:
        d = p["by_code"].get(code, {})
        out.append(f"{code} ({d.get('device_type')}) is away from home in {star_of(d.get('location'))}: send it back to {p['moves'].get(code)}")
    for code in p["arrived"]:
        out.append(f"{code} has arrived: deploy if stowed, clear its to:/spare tags")
    for r in p.get("routes") or []:
        bits = []
        if r.get("adopt"):
            bits.append(f"adopts freighter(s) {', '.join(a['code'] for a in r['adopt'])}")
        if r.get("resend", True):
            bits.append(f"ferries {r['collect']} → {r['deliver']}")
        fleet = len(r.get("fleet") or []) + len(r.get("adopt") or [])
        out.append(f"{r['controller']} {' and '.join(bits)} ({r['source']} → nearest destination {r['dest']}, {fleet} freighter(s))")
    for u in p["unmet"]:
        if u["type"] == "materials":
            out.append(f"materials from {u['star']}: {u['why']}")
        else:
            out.append(f"can't fill {u['n']}× {u['type']} in {u['star']}: {u['why']}")
    return out


# --- materials: source systems ferry to the nearest destination ------------------------------------------
def _stock_total(items: dict) -> float:
    return sum(as_amounts(items).values())


def drop_point(star: str, devices: list[dict], inventory: dict[str, dict], stars: dict[str, dict]) -> str:
    """Where materials should land in a destination: its autofactory, else its biggest stockpile, else its entry point."""
    fac = sorted(d.get("location") for d in devices if star_of(d.get("location")) == star and is_factory(d) and d.get("location"))
    if fac:
        return fac[0]
    piles = sorted(((loc, _stock_total(items)) for loc, items in inventory.items() if star_of(loc) == star), key=lambda kv: -kv[1])
    if piles:
        return piles[0][0]
    return destination(star, stars)


def pickup_point(star: str, inventory: dict[str, dict]) -> str | None:
    piles = sorted(((loc, _stock_total(items)) for loc, items in inventory.items() if star_of(loc) == star and _stock_total(items) > 0),
                   key=lambda kv: -kv[1])
    return piles[0][0] if piles else None


FERRY_TAG = "ferry"
IN_SYSTEM_HAULERS = ("transport_drone", "transport_hauler")


def is_freighter(d: dict) -> bool:
    return "freighter" in (d.get("device_type") or "")


def is_transport_controller(d: dict) -> bool:
    t = d.get("device_type") or ""
    return ("transport" in t and "controller" in t) or (
        "transport" in t and "set_directive" in (d.get("available_commands") or []) and not any(h in t for h in IN_SYSTEM_HAULERS))


def material_routes(cfg: dict, devices: list[dict], inventory: dict[str, dict], stars: dict[str, dict], busy: set[str],
                    current: dict[str, dict], managed: dict[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    """([route], [unmet]). One ferry per source system, to the nearest destination.

    Interstellar hauling is done by cargo freighters (surge drive) under a *ferry controller*: an AMI
    transport controller in the source that is tagged `ferry`, or already runs freighters, or manages
    nothing else. Transport drones and haulers stay on in-system work, so the controller that runs them
    is never given the ferry. Each pass, idle unmanaged freighters in the source are adopted by the
    ferry controller (flying to it first if they are elsewhere in the system).

    current: controller -> its latest directive {"directive", "configuration", "finished"}.
    managed: device -> the controller that manages it.
    """
    cfg = normalize(cfg)
    managed = managed or {}
    ignore = set(cfg["ignore_tags"])
    pos = {k: (v or {}).get("position") or {} for k, v in stars.items()}
    sources = sorted(s for s, r in cfg["roles"].items() if r == "source")
    dests = sorted(s for s, r in cfg["roles"].items() if r == "destination")
    by_code = {d.get("device_code"): d for d in devices}
    routes, unmet = [], []

    def runs(ctrl: str) -> list[dict]:
        return [by_code[d] for d, c in managed.items() if c == ctrl and d in by_code]

    for src in sources:
        cands = [d for d in dests if d != src]
        if not cands:
            unmet.append({"star": src, "type": "materials", "n": 0, "why": "no destination system set"})
            continue
        dest = min(cands, key=lambda d: (_dist(src, d, pos), d))
        ctrls = [d for d in devices if star_of(d.get("location")) == src and is_transport_controller(d)
                 and not (ignore & set(d.get("tags") or []))]
        if not ctrls:
            unmet.append({"star": src, "type": "materials", "n": 0, "why": f"no AMI transport controller in {src} to ferry to {dest}"})
            continue

        IN_SYSTEM = ("delivery", "shuttle", "consolidate")

        def directive_of(c: dict) -> str | None:
            cur = current.get(c["device_code"]) or {}
            return None if cur.get("finished") else cur.get("directive")

        # never the controller on in-system work (delivery/shuttle/consolidate): a ferry would replace that job.
        # A controller already ferrying keeps its fleet as it is — the game's ferry also uses transport
        # drones riding surge plates in taxi mode, so drones under a ferry controller are fine.
        ferry_ctrls = [c for c in ctrls if directive_of(c) not in IN_SYSTEM or FERRY_TAG in (c.get("tags") or [])]
        if not ferry_ctrls:
            busy_c = ", ".join(f"{c['device_code']} ({directive_of(c)})" for c in ctrls)
            unmet.append({"star": src, "type": "materials", "n": 0,
                          "why": f"every transport controller in {src} is on in-system work [{busy_c}] — add one for ferrying"})
            continue

        def rank(c: dict) -> tuple:
            fleet = runs(c["device_code"])
            return (FERRY_TAG not in (c.get("tags") or []), directive_of(c) != "ferry",
                    not any(is_freighter(x) for x in fleet), c["device_code"])
        ctrl = sorted(ferry_ctrls, key=rank)[0]
        code = ctrl["device_code"]
        fleet = [x for x in runs(code) if is_freighter(x)]
        held = [d for d in devices if is_freighter(d) and star_of(d.get("location")) == src
                and managed.get(d["device_code"]) not in (None, code) and str(d.get("status") or "").startswith("idle")
                and directive_of(by_code.get(managed[d["device_code"]], {"device_code": ""})) in IN_SYSTEM]
        release: dict[str, list[str]] = {}
        for d in held:  # a freighter stuck under the in-system controller moves over to the ferry controller
            release.setdefault(managed[d["device_code"]], []).append(d["device_code"])
        adopt = sorted((d for d in devices if is_freighter(d) and star_of(d.get("location")) == src
                        and str(d.get("status") or "").startswith("idle")
                        and (d["device_code"] not in managed or d in held)
                        and d["device_code"] not in busy and not (ignore & set(d.get("tags") or []))
                        and bound_for(d, set(stars) | {src, dest}) in (None, src)), key=lambda d: d["device_code"])
        if not fleet and not adopt and not runs(code):
            unmet.append({"star": src, "type": "materials", "n": 0, "why": f"no cargo freighter in {src} for {code} to ferry with"})
            continue
        collect = pickup_point(src, inventory)
        if not collect:
            unmet.append({"star": src, "type": "materials", "n": 0, "why": "nothing stockpiled to send yet"})
            continue
        deliver = drop_point(dest, devices, inventory, stars)
        conf = {"collect": collect, "deliver": deliver}
        cur = current.get(code) or {}
        cc = cur.get("configuration") or {}
        same_route = (star_of(cc.get("collect")) == src and star_of(cc.get("deliver")) == dest
                      and _stock_total(inventory.get(cc.get("collect")) or {}) > 0 or cc == conf)
        running = cur.get("directive") == "ferry" and not cur.get("finished") and same_route
        if running:  # keep its current pick-up while that still has stock — re-sending just resets the run
            conf = {"collect": cc.get("collect"), "deliver": cc.get("deliver")}
            collect, deliver = conf["collect"], conf["deliver"]
        if running and not adopt:
            continue  # already ferrying this route with everything it can use
        if code in busy:
            unmet.append({"star": src, "type": "materials", "n": 0, "why": f"{code} is busy with another job"})
            continue
        routes.append({"controller": code, "source": src, "dest": dest, "collect": collect, "deliver": deliver,
                       "distance": _dist(src, dest, pos), "controller_loc": ctrl.get("location"),
                       "adopt": [{"code": d["device_code"], "location": d.get("location")} for d in adopt],
                       "fleet": [x["device_code"] for x in fleet], "resend": not running, "release": release,
                       "tag": FERRY_TAG not in (ctrl.get("tags") or [])})
    return routes, unmet


def ferry_steps(r: dict) -> list[dict]:
    code, cloc = r["controller"], r.get("controller_loc")
    steps = []
    if r.get("tag"):
        steps.append(tag_step(code, [FERRY_TAG]))  # marks it as the interstellar controller; in-system planners skip it
    away = [a for a in r.get("adopt") or [] if cloc and a["location"] != cloc]
    first = len(steps)
    for a in away:
        steps.append(step(f"{a['code']} → {cloc} (join {code})", f"/devices/{a['code']}", {"command": "travel", "destination": cloc}))
    for i, a in enumerate(away):
        w = step(f"wait for {a['code']} at {cloc}", "", None, method="WAIT", wait=["travel.arrived"], match={"destination": cloc},
                 timeout=STEP_TIMEOUT)
        w["wait_device"] = a["code"]
        w["seq0_from"] = first + i
        steps.append(w)
    for other, codes in sorted((r.get("release") or {}).items()):
        steps.append(step(f"{other}: release {len(codes)} cargo freighter(s) to {code}", f"/devices/{other}",
                          {"command": "release", "devices": codes}))
    if r.get("adopt"):
        codes = [a["code"] for a in r["adopt"]]
        steps.append(step(f"{code}: adopt {len(codes)} cargo freighter(s)", f"/devices/{code}", {"command": "adopt", "devices": codes}))
    if r.get("resend", True):
        steps.append(step(f"{code}: ferry {r['collect']} → {r['deliver']}", f"/devices/{code}",
                          {"command": "set_directive", "directive": "ferry",
                           "configuration": {"collect": r["collect"], "deliver": r["deliver"]}}, critical=True))
    steps.append(step(f"{code}: launch", f"/devices/{code}", {"command": "launch"}))
    return steps