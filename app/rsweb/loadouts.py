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
    """The system a device belongs to (`home:<star>` tag), matched back to a known star code."""
    for t in d.get("tags") or []:
        if t.startswith("home:"):
            want = t[5:]
            return next((s for s in stars if home_tag(s)[5:] == want), want.upper())
    return None


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

    def visible(d: dict) -> bool:
        return not (ignore & set(d.get("tags") or [])) and d.get("device_code") not in replicant_hosts

    pool = [d for d in devices if visible(d)]
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
            home = home_of(d, known_stars) or star_of(d.get("location"))
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
            ranked = sorted(have, key=lambda d: (SPARE in (d.get("tags") or []), star_of(d.get("location")) == star,
                                                 d["device_code"] not in busy, _idle(d), -_cap(d), d["device_code"]))
            keep, extra = ranked[:len(have) - surplus], ranked[len(have) - surplus:]
            for d in keep:
                if SPARE in (d.get("tags") or []):
                    tag_remove[d["device_code"]].add(SPARE)
            for d in extra:
                if SPARE not in (d.get("tags") or []):
                    tag_add[d["device_code"]].add(SPARE)
            for d in have:  # every device counted for this system carries its home tag
                tags = set(d.get("tags") or [])
                if home_tag(star) not in tags:
                    tag_add[d["device_code"]].add(home_tag(star))
                    tag_remove[d["device_code"]].update(t for t in tags if t.startswith("home:"))
            away = [d["device_code"] for d in have if star_of(d.get("location")) != star]
            rows.append({"type": t, "want": want, "have": len(have), "incoming": inc, "away": away,
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

    def factory_for(t: str, star: str) -> tuple[dict | None, str]:
        bp = bps.get(t)
        if not bp:
            return None, f"no blueprint for {t}"
        cost = as_amounts(bp.get("resources"))
        if not factories:
            return None, "no autofactory"
        best = None
        for f in sorted(factories, key=lambda f: (star_of(f.get("location")) != star, _dist(star_of(f.get("location")), star, pos))):
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
                    for r, v in cost.items():
                        reserved[f["location"]][r] += v * n
                    prints.append({"factory": f["device_code"], "factory_star": star_of(f.get("location")),
                                   "device_type": row["type"], "n": n, "star": star, "note": why})
                    row["printing"] = n
                    need -= n
                    if need > 0:
                        unmet.append({"star": star, "type": row["type"], "n": need, "why": f"materials for only {n}"})
                else:
                    unmet.append({"star": star, "type": row["type"], "n": need, "why": why})
            elif need > 0:
                unmet.append({"star": star, "type": row["type"], "n": need, "why": "no spares (printing is off)"})

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
                and not str(d.get("status") or "").startswith(("travel", "cruis", "surg", "stowed"))]
    used: set[str] = set()
    deliveries = []
    for (here, dest), codes in sorted(batches.items()):
        codes = sorted(codes)
        while codes:
            options = [c for c in carriers if star_of(c.get("location")) == here and c["device_code"] not in used
                       and c["device_code"] not in codes]
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

    return {"report": report, "tag_add": {k: sorted(v) for k, v in tag_add.items() if v},
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


def self_move_steps(code: str, dest_star: str, stars: dict, d: dict) -> list[dict]:
    tags = set(d.get("tags") or [])
    steps = []
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
        if attach:
            st = step(f"attach {code} to {carrier}", f"/devices/{code}", {"command": "attach", "device": carrier},
                      wait=["device.attached"], timeout=SHORT_TIMEOUT, critical=True)
        else:
            st = step(f"stow {code} in {carrier}", f"/devices/{code}", {"command": "stow", "target": carrier},
                      wait=["device.stowed"], timeout=SHORT_TIMEOUT, critical=True)
        st["wait_device"] = code
        steps.append(st)
    steps.append(move(dest, f"deliver {len(dl['devices'])} to {dest_star}", dest_star))
    for code in dl["devices"]:
        if attach:
            st = step(f"detach {code} in {dest_star}", f"/devices/{code}", {"command": "detach"}, critical=True)
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
    if "attach" in status:
        steps.append(step(f"detach {code}", f"/devices/{code}", {"command": "detach"}, critical=True))
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
    for code in p["arrived"]:
        out.append(f"{code} has arrived: deploy if stowed, clear its to:/spare tags")
    for r in p.get("routes") or []:
        out.append(f"{r['controller']} ferries materials {r['collect']} → {r['deliver']} ({r['source']} → nearest destination {r['dest']})")
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


def material_routes(cfg: dict, devices: list[dict], inventory: dict[str, dict], stars: dict[str, dict], busy: set[str],
                    current: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """([route], [unmet]). One ferry per source system, to the nearest destination.

    current: controller -> its latest directive {"directive", "configuration", "finished"} so an
    unchanged, still-running ferry isn't re-sent every pass.
    """
    cfg = normalize(cfg)
    ignore = set(cfg["ignore_tags"])
    pos = {k: (v or {}).get("position") or {} for k, v in stars.items()}
    sources = sorted(s for s, r in cfg["roles"].items() if r == "source")
    dests = sorted(s for s, r in cfg["roles"].items() if r == "destination")
    routes, unmet = [], []
    for src in sources:
        cands = [d for d in dests if d != src]
        if not cands:
            unmet.append({"star": src, "type": "materials", "n": 0, "why": "no destination system set"})
            continue
        dest = min(cands, key=lambda d: (_dist(src, d, pos), d))
        collect = pickup_point(src, inventory)
        if not collect:
            unmet.append({"star": src, "type": "materials", "n": 0, "why": "nothing stockpiled to send yet"})
            continue
        ctrls = [d for d in devices if star_of(d.get("location")) == src and "transport" in (d.get("device_type") or "")
                 and ("set_directive" in (d.get("available_commands") or []) or "ami" in (d.get("features") or []))
                 and not (ignore & set(d.get("tags") or []))]
        if not ctrls:
            unmet.append({"star": src, "type": "materials", "n": 0, "why": f"no AMI transport controller in {src} to ferry to {dest}"})
            continue
        deliver = drop_point(dest, devices, inventory, stars)
        conf = {"collect": collect, "deliver": deliver}
        ctrl = next((c for c in ctrls if (current.get(c["device_code"]) or {}).get("directive") == "ferry"
                     and (current[c["device_code"]].get("configuration") or {}) == conf
                     and not current[c["device_code"]].get("finished")), None)
        if ctrl:
            continue  # already ferrying this route
        free = [c for c in ctrls if c["device_code"] not in busy]
        if not free:
            unmet.append({"star": src, "type": "materials", "n": 0, "why": "transport controller busy with another job"})
            continue
        c = sorted(free, key=lambda c: (not str(c.get("status") or "").startswith("idle"), c["device_code"]))[0]
        routes.append({"controller": c["device_code"], "source": src, "dest": dest, "collect": collect, "deliver": deliver,
                       "distance": _dist(src, dest, pos)})
    return routes, unmet


def ferry_steps(r: dict) -> list[dict]:
    code = r["controller"]
    return [step(f"{code}: ferry {r['collect']} → {r['deliver']}", f"/devices/{code}",
                 {"command": "set_directive", "directive": "ferry",
                  "configuration": {"collect": r["collect"], "deliver": r["deliver"]}}, critical=True),
            step(f"{code}: launch", f"/devices/{code}", {"command": "launch"})]
