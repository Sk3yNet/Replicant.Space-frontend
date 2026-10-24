"""Bootstrap fleets: from a bare heaven vessel (three mining drones and a replicant matrix aboard) to an autofactory hub
with three mining outposts feeding it. A fleet with role "bootstrap" on the Fleets page; its state is `fleet["boot"]`.

Stages (each waits on a gate; ★ = stops for your OK on the Fleets page):
  home          in the vessel's home system (where it was created): deploy the drones at the best belt and compound —
                the drones mine the belt, the vessel mines (onboard, its pick of resource) what the drones get little of,
                and the replicant prints survey drones (they open the sites miners need) and mining drones, until the
                hold-sized kit (the vessel stows 10) is built. A home warded by another player (Sol is) can't be mined:
                straight on to the survey.
  survey        the vessel visits and scans every unscanned system within the radius (10 ly), nearest first.
  hub_choice ★  the hub: the best system by mining score (rares rate high) plus how many good outpost systems it has
                around it.
  move          the vessel fetches its drones (they stow in its hold) and moves to the hub's best belt.
  hub_compound  compound again, then the vessel prints the autofactory there (it can't be stowed or carried by a
                heaven vessel, so it's built where it stays); the hub becomes a stationed fleet that takes materials in.
  hub           the hub's own autofactory builds it up (relay, ward when worth it, transport controller, haulers, more
                drones, a surge carrier for kits, a maintenance drone) — the loadout pass, restricted to this bootstrap's devices.
                A dry hub belt (the autofactory waits for a resource no open site there holds, and the belt yields
                it low or less, for 3 h): ★ a supply system within a relay's reach whose belt has it. The vessel
                prints a relay for each system, a surge plate and a transport hauler, ferries part of the miners (and
                a survey drone) there; the hauler rides the plate and brings what the hub waits for (supply_step,
                haul_plan).
  outposts ★    three mining outposts within the radius of the hub that keep the relay chain (each within a relay's
                7.5 ly of the hub or an outpost); each a stationed fleet sending its materials to the hub by freighter.
  operate       handed over to the usual automation. A system another player wards: ★ move to the next best.

Self-reliant: everything it prints is tagged `boot:<id>`; the loadout pass never lends it others' spares, factories or
carriers, nor lends its own to others. Materials only move on transport-feature devices (haulers, freighters); a heaven
vessel carries devices, not materials.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .prospects import RARITY, YIELD, score as prospect_score

STAGES = ["home", "survey", "hub_choice", "move", "hub_compound", "hub", "outposts", "operate"]
LABEL = {"home": "Home: compounding", "survey": "Survey trip", "hub_choice": "Choose the hub", "move": "Moving to the hub",
         "hub_compound": "Hub: compounding to an autofactory", "hub": "Hub: building up", "outposts": "Outposts",
         "operate": "Operating"}
RELAY_LY = 7.5
DEFAULTS = {"radius": 10.0, "ward_hours": 6.0, "hold": 10, "hub_miners": 8, "survey_per_miner": 0.5, "survey_max": 12,
            "min_score": 35, "outposts": 3, "supply_miners": 4}
COST_TYPES = ["survey_drone", "mining_drone", "ftl_relay", "system_ward", "autofactory", "ami_mining_controller",
              "ami_survey_controller", "ami_transport_controller",
              "transport_hauler", "cargo_freighter", "surge_carrier", "maintenance_drone"]
OUTPOST_KIT = {"mining_drone": 4, "survey_drone": 2, "ami_mining_controller": 1, "ami_survey_controller": 1, "ftl_relay": 1,
               "ami_transport_controller": 1, "cargo_freighter": 1, "maintenance_drone": 1}
HUB_KIT = {"ami_mining_controller": 1, "ami_survey_controller": 1, "ftl_relay": 1, "ami_transport_controller": 1,
           "transport_hauler": 2, "surge_carrier": 1, "maintenance_drone": 1}
RESOURCES = ["structural", "conductive", "silicates", "carbon", "volatiles", "rares"]
SUPPLY_KIT = ["ftl_relay", "surge_plate", "transport_hauler"]   # a dry hub's supply system (owner 2026-10-23), in print order
DRY_HOURS = 3.0   # how long the hub belt must lack a resource its factory waits for before a supply system is proposed
DRY_YIELD = YIELD["low"]   # a hub belt yielding a resource this well or worse doesn't count on new sites bringing it


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def family_tag(fid: str) -> str:
    return f"boot:{fid}"


def new_state(vessel: str, replicant: str, home: str) -> dict:
    return {"stage": "home", "vessel": vessel, "replicant": replicant, "home": home, "hub": None, "visited": [],
            "candidates": [], "decision": None, "outposts": [], "children": {}, "log": [], "settings": dict(DEFAULTS)}


def settings(b: dict) -> dict:
    return {**DEFAULTS, **(b.get("settings") or {})}


def _xyz(p: Any) -> list[float] | None:
    return [float(p.get(k) or 0) for k in "xyz"] if isinstance(p, dict) else None


def dist(a: str, b: str, pos: dict) -> float:
    pa, pb = _xyz(pos.get(a)), _xyz(pos.get(b))
    return math.dist(pa, pb) if pa and pb else 1e9


def amounts(items: Any) -> dict[str, float]:
    from .shapes import as_amounts
    return as_amounts(items or {})


def members(f: dict, devices: list[dict]) -> list[dict]:
    """The bootstrap's own devices: its vessel and everything tagged boot:<id> (or fleet:<id>)."""
    tags = {family_tag(f["id"]), f"fleet:{f['id']}"}
    v = (f.get("boot") or {}).get("vessel")
    return [d for d in devices if d.get("device_code") == v or tags & set(d.get("tags") or [])]


def in_transit(d: dict) -> bool:
    return str(d.get("status") or "").startswith(("travel", "cruis", "surg"))


def cost_of(t: str, bps: dict) -> dict[str, float]:
    return amounts((bps.get(t) or {}).get("resources"))


def short_of(cost: dict[str, float], stock: dict[str, float]) -> dict[str, float]:
    return {r: v - stock.get(r, 0.0) for r, v in cost.items() if v > stock.get(r, 0.0)}


def best_belt(scan: dict | None) -> dict | None:
    """The belt with the best mining rate (yield × rarity × density), as the prospect score rates it."""
    best, val = None, -1.0
    for b in ((scan or {}).get("asteroid_belt") or {}).get("belts") or []:
        one = prospect_score("X", {"asteroid_belt": {"belts": [b]}}, None, [], None, True)
        v = (one.get("parts") or {}).get("richness") or 0
        if b.get("designation") and v > val:
            best, val = b, v
    return best


def mine_choice(belt: dict | None, need: dict[str, float], skip: set[str] | frozenset = frozenset()) -> str | None:
    """What a drone mines: the resource the next print is shortest of, weighted by how much this belt yields of it;
    nothing short → the belt's best by yield × rarity. `skip`: resources the belt's open sites lack right now (the game
    refused them); None when that leaves nothing."""
    levels = (belt or {}).get("resources") or {}
    y = {r: YIELD.get(levels.get(r), 0) for r in RARITY if r not in skip}
    need = {r: q for r, q in (need or {}).items() if r not in skip}
    if need:
        pick = max(need, key=lambda r: (need[r] * max(1, y.get(r, 0)), r))
        if y.get(pick, 0):
            return pick
    if any(y.values()):
        return max(y, key=lambda r: (y[r] * RARITY[r], r))
    return None if "structural" in skip else "structural"


NO_SITE_HOURS = 3   # a resource the game said no open site has is left alone that long (survey drones open new sites)


def no_site(b: dict, belt: str, now: datetime | None = None) -> set[str]:
    """Resources the game refused at this belt within NO_SITE_HOURS ("No conductive remaining at any active site")."""
    now = now or datetime.now(timezone.utc)
    out = set()
    for r, at in ((b.get("no_site") or {}).get(belt) or {}).items():
        try:
            t = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if now - (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) < timedelta(hours=NO_SITE_HOURS):
            out.add(r)
    return out


def vessel_choice(short: dict[str, float], cost: dict[str, float], belt: dict | None) -> str | None:
    """What the vessel mines onboard: of what's short, what the drones get least of at this belt (rares and volatiles,
    usually) — the share still missing × rarity ÷ the belt's yield of it."""
    if not short:
        return None
    levels = (belt or {}).get("resources") or {}
    return max(short, key=lambda r: (short[r] / max(1.0, cost.get(r, 1.0)) * RARITY.get(r, 1.0)
                                     / max(1, YIELD.get(levels.get(r), 0)), r))


def compound_target(miners: int, surveys: int, cap: int, per_miner: float, autofactory: bool, have_factory: bool) -> str | None:
    """The next thing to print while compounding: survey drones open the sites miners need (one per 1/per_miner miners),
    mining drones up to the cap, then (at the hub) the autofactory."""
    if surveys < 1:
        return "survey_drone"
    if surveys < math.ceil(miners * per_miner) and miners + surveys < cap:
        return "survey_drone"
    if miners + surveys < cap:
        return "mining_drone"
    if autofactory and not have_factory:
        return "autofactory"
    return None


def hold_cap(f: dict, w: dict) -> int:
    """How many drones the home kit can hold: the hold setting (at most the vessel's stow capacity), less the room what
    else rides aboard takes (a replicant matrix, a slingshot …) — else drones are left behind at the move."""
    b, s = f["boot"], settings(f["boot"])
    v = next((d for d in w["devices"] if d.get("device_code") == b.get("vessel")), None) or {}
    cap = min(int(s["hold"]), int(v.get("stow_capacity") or s["hold"]))
    other = [d for d in w["devices"] if d.get("stowed_in_device_code") == b.get("vessel")
             and d.get("device_type") not in ("mining_drone", "survey_drone")]
    return max(1, cap - len(other))


def compound(f: dict, w: dict, star: str, cap: int, autofactory: bool) -> dict:
    """One step of compounding in `star`: {actions, gate, next, done}."""
    b, s = f["boot"], settings(f["boot"])
    devs = members(f, w["devices"])
    v = next((d for d in devs if d["device_code"] == b["vessel"]), None) or {}
    scan = w["scans"].get(star)
    belt = best_belt(scan)
    if not belt:
        return {"actions": [], "gate": f"no asteroid belt known in {star}", "blocked": True,
                "next": "needs a belt to mine" + (" — set another home below" if f["boot"].get("stage") == "home" else "")}
    bloc = belt["designation"]
    if v.get("location") != bloc:
        return {"actions": [{"kind": "travel", "device": v.get("device_code"), "to": bloc}], "gate": "vessel at the belt",
                "next": f"vessel → {bloc}"}
    here = [d for d in devs if d["device_code"] != b["vessel"]]
    aboard = [d for d in here if d.get("stowed_in_device_code") == b["vessel"]]
    deployed = [d for d in here if d.get("location") == bloc and not d.get("stowed_in_device_code")]
    miners = [d for d in here if d.get("device_type") == "mining_drone"]
    surveys = [d for d in here if d.get("device_type") in ("survey_drone", "belt_surveyor")]   # belt surveyors count too
    factory = next((d for d in here if d.get("device_type") == "autofactory"), None)
    stock = amounts(w["inventory"].get(bloc))
    target = compound_target(len(miners), len(surveys), cap, s["survey_per_miner"], autofactory, bool(factory))
    need = short_of(cost_of(target, w["bps"]), stock) if target else {}
    acts: list[dict] = []
    for d in aboard:
        if d.get("device_type") in ("mining_drone", "survey_drone", "autofactory"):
            acts.append({"kind": "deploy", "device": d["device_code"]})
    skip = no_site(b, bloc)
    have = (w.get("open_res") or {}).get(bloc)
    if have is not None:   # what no open site holds is no use asking for (ISOLITHOR-BELT-1 2026-10-23)
        skip = skip | {r for r in RESOURCES if r not in have}
    for d in deployed:
        st = str(d.get("status") or "")
        if d.get("device_type") == "mining_drone" and st.startswith("idle"):
            res = mine_choice(belt, need, skip)
            if res:   # nothing minable at the open sites: it waits for the survey drones
                acts.append({"kind": "start_mining", "device": d["device_code"], "resource": res})
        elif d.get("device_type") in ("survey_drone", "belt_surveyor") and st.startswith("idle"):
            acts.append({"kind": "search", "device": d["device_code"]})
    gate = (f"{len(miners)} mining + {len(surveys)} survey drones of {cap}"
            + ("" if not autofactory else " · autofactory " + ("✓" if factory else "to print")))
    if not target:
        return {"actions": acts, "gate": gate, "next": "compounding done", "done": True, "stock": stock}
    vst = str(v.get("status") or "")
    if not need:
        if vst.startswith("mining"):
            acts.append({"kind": "vessel_stop_mining"})
        if not vst.startswith("printing"):
            acts.append({"kind": "print", "device_type": target})
        return {"actions": acts, "gate": gate, "next": f"print {target.replace('_', ' ')} on the vessel", "stock": stock}
    pick = vessel_choice(need, cost_of(target, w["bps"]), belt)
    if pick and not vst.startswith(("mining", "printing")):
        acts.append({"kind": "vessel_mine", "resource": pick})
    left = ", ".join(f"{int(math.ceil(q))} {r}" for r, q in sorted(need.items(), key=lambda x: -x[1]))
    return {"actions": acts, "gate": gate, "stock": stock,
            "next": f"mining for {'an' if target[0] in 'aeiou' else 'a'} {target.replace('_', ' ')} — short {left}"
                    + (f" (vessel mines {pick})" if pick else "")
                    + (f" · no open site has {', '.join(sorted(skip))}" if skip else "")}


def survey_queue(f: dict, w: dict) -> list[str]:
    """Unscanned systems within the radius of home, nearest first; not ones another player has warded."""
    b, s = f["boot"], settings(f["boot"])
    home, pos = b["home"], w["pos"]
    cands = [x for x in pos if x != home and x not in w["scans"] and x not in w["warded"] and x not in b.get("visited", [])
             and dist(home, x, pos) <= s["radius"]]
    return sorted(cands, key=lambda x: (dist(home, x, pos), x))[: s["survey_max"]]


def system_score(star: str, w: dict, origin: str) -> dict:
    """The mining score at full potential: another player's drones don't count against it (your ward removes them);
    only someone else's ward rules a system out."""
    return prospect_score(star, w["scans"].get(star), None, [], dist(origin, star, w["pos"]) if origin else None, True,
                          None, star in w["warded"], None)


HAZARD_PENALTY, ROCK_PENALTY = 15, 10


def risk(star: str, w: dict) -> dict:
    """Asteroid and hazard risk of a system: {open: an incoming object there now (ruled out), penalty, reasons}. A past
    asteroid counts against it (designations recur: FALQUORYX-OBJ-2 came twice), as do hazard system tags in its scan —
    an autofactory can't be stowed to flee an impact."""
    r = (w.get("rocks") or {}).get(star) or {}
    tags = [str(t) for t in ((w["scans"].get(star) or {}).get("system_tags") or []) if "hazard" in str(t).lower()]
    past = min(3, int(r.get("past") or 0))
    reasons = ([f"{r['open']} incoming object(s) now"] if r.get("open") else []) + \
        ([f"{r['past']} past asteroid(s)"] if past else []) + ([f"hazardous: {', '.join(tags)}"] if tags else [])
    return {"open": bool(r.get("open")), "penalty": ROCK_PENALTY * past + (HAZARD_PENALTY if tags else 0), "reasons": reasons}


def hub_candidates(f: dict, w: dict) -> list[dict]:
    """Scanned systems within the radius of home ranked as a hub: its own score plus how many good outpost systems it
    has within the radius (and relay reach for the chain)."""
    b, s = f["boot"], settings(f["boot"])
    home, pos = b["home"], w["pos"]
    pool = [x for x in w["scans"] if x in pos and dist(home, x, pos) <= s["radius"] and x not in w["warded"]
            and x not in w.get("homes", set())]
    if home in w["scans"] and home not in w["warded"] and home not in pool:
        pool.append(home)
    scored = {x: system_score(x, w, home) for x in pool}
    out = []
    for x, sc in scored.items():
        if sc.get("score") is None or not best_belt(w["scans"].get(x)):
            continue
        good = [y for y, o in scored.items() if y != x and (o.get("score") or 0) >= s["min_score"]
                and dist(x, y, pos) <= s["radius"]]
        chained = [y for y in good if dist(x, y, pos) <= RELAY_LY]
        rk = risk(x, w)
        if rk["open"]:
            continue   # never a hub where a rock is coming
        value = sc["score"] + 4 * min(3, len(good)) + 2 * min(3, len(chained)) - rk["penalty"]
        out.append({"star": x, "score": sc["score"], "value": round(value), "reasons": rk["reasons"] + sc["reasons"][:4],
                    "good_near": sorted(good, key=lambda y: dist(x, y, pos))[:6], "in_relay": len(chained),
                    "distance": round(dist(home, x, pos), 1) if dist(home, x, pos) < 1e9 else None})
    return sorted(out, key=lambda c: (-c["value"], c["distance"] or 0, c["star"]))[:6]


def chain(f: dict) -> list[str]:
    b = f["boot"]
    return [x for x in [b.get("hub")] + [o["star"] for o in b.get("outposts") or []] + list(b.get("waypoints") or [])
            + [(b.get("supply") or {}).get("star")] if x]


def waiting_short(devices: list[dict], stars: set[str]) -> dict[str, float]:
    """What the factories in `stars` wait for right now ({res: need − have} from their `waiting_for`)."""
    out: dict[str, float] = {}
    for d in devices:
        if star_of(d.get("location")) not in stars or not isinstance(d.get("waiting_for"), dict):
            continue
        for r, v in d["waiting_for"].items():
            if isinstance(v, dict) and r != "components":
                gap = float(v.get("need") or 0) - float(v.get("have") or 0)
                if gap > 0:
                    out[r] = out.get(r, 0.0) + gap
    return out


def dry_needs(f: dict, w: dict) -> dict[str, float]:
    """What the hub's factories wait for that no open site at the hub belt holds and the belt yields poorly (ISOLITHOR
    2026-10-23: the autofactory waited for 20 rares; rares scarce, at none of the five open sites)."""
    hub = f["boot"].get("hub")
    belt = best_belt(w["scans"].get(hub)) if hub else None
    have = (w.get("open_res") or {}).get((belt or {}).get("designation"))
    if not belt or have is None:
        return {}
    levels = belt.get("resources") or {}
    return {r: q for r, q in waiting_short(w["devices"], {hub}).items()
            if r not in have and YIELD.get(levels.get(r), 0) <= DRY_YIELD}


def dry_for(b: dict, dry: dict[str, float], now: datetime | None = None) -> list[str]:
    """Keep when each resource went dry (b["dry"]); those dry for DRY_HOURS."""
    now = now or datetime.now(timezone.utc)
    seen = {r: at for r, at in (b.get("dry") or {}).items() if r in dry}
    for r in dry:
        seen.setdefault(r, now.isoformat(timespec="seconds"))
    if seen:
        b["dry"] = seen
    else:
        b.pop("dry", None)
    return sorted(r for r, at in seen.items() if now - datetime.fromisoformat(at) >= timedelta(hours=DRY_HOURS))


def supply_candidates(f: dict, w: dict, need: list[str]) -> list[dict]:
    """Scanned systems within a relay's reach of the hub whose best belt yields what the hub lacks (better than low)."""
    b = f["boot"]
    hub, pos = b["hub"], w["pos"]
    taken = set(chain(f)) | set(w.get("homes", set()))
    rows = []
    for x in w["scans"]:
        if x not in pos or x in taken or x in w["warded"] or dist(hub, x, pos) > RELAY_LY or risk(x, w)["open"]:
            continue
        belt = best_belt(w["scans"].get(x))
        lv = (belt or {}).get("resources") or {}
        good = [r for r in need if YIELD.get(lv.get(r), 0) > DRY_YIELD]
        if not good:
            continue
        rows.append({"star": x, "score": round(sum(YIELD[lv[r]] * RARITY.get(r, 1.0) for r in good), 1),
                     "reasons": [f"{r} {lv.get(r) or 'none'}" for r in need], "distance": round(dist(hub, x, pos), 1)})
    return sorted(rows, key=lambda r: (-r["score"], r["distance"], r["star"]))[:5]


def _stowable(d: dict) -> bool:
    return "stow" in (d.get("features") or []) or "stow" in (d.get("available_commands") or []) \
        or d.get("device_type") in ("mining_drone", "survey_drone", "ftl_relay")


def supply_step(f: dict, w: dict) -> dict | None:
    """The dry hub's supply system: propose one (★), then print its kit on the vessel at the hub, deploy the hub's
    relay, put the hauler on the plate, ferry the kit and the miners picked for it, and bring the vessel home once both
    relays are on. None: nothing to do (the hub carries on as usual); {"note"}: only something to say."""
    b = f["boot"]
    sp = b.get("supply")
    hub = b.get("hub")
    if not sp:
        dry = dry_for(b, dry_needs(f, w))
        if not dry or b.get("decision"):
            return None
        opts = supply_candidates(f, w, dry)
        if not opts:
            return {"note": f"{hub}'s belt lacks {', '.join(dry)} and no scanned system within {RELAY_LY:g} ly has it"}
        b["decision"] = {"kind": "supply", "for": dry, "options": opts}
        return {"gate": "waiting for your OK", "next": f"{hub}'s belt lacks {', '.join(dry)}: pick a supply system below",
                "actions": [], "decision": b["decision"]}
    devs = members(f, w["devices"])
    v = next((d for d in devs if d["device_code"] == b.get("vessel")), None) or {}
    sstar, stag = sp["star"], f"fleet:{sp['fleet']}"
    htag = f"fleet:{(b.get('children') or {}).get('hub') or ''}"
    hub_belt = (best_belt(w["scans"].get(hub)) or {}).get("designation")
    sbelt = (best_belt(w["scans"].get(sstar)) or {}).get("designation")
    if not hub_belt or not sbelt:
        return {"note": f"no belt known in {hub if not hub_belt else sstar}"}
    mine = [d for d in devs if stag in (d.get("tags") or [])]
    hub_relays = [d for d in devs if htag in (d.get("tags") or []) and d.get("device_type") == "ftl_relay"]
    gate = f"supply {sstar} for {', '.join(sp.get('for') or [])}"
    loose = [d for d in devs if d.get("device_type") in SUPPLY_KIT and d["device_code"] != v.get("device_code")
             and not any(t.startswith("fleet:") for t in d.get("tags") or [])]
    if loose:   # a kit piece the vessel printed: the hub's relay first, the rest the supply system's
        d = loose[0]
        to = htag if d["device_type"] == "ftl_relay" and not hub_relays else stag
        return {"gate": gate, "next": f"{d['device_code']} ({d['device_type'].replace('_', ' ')}) joins "
                                      + ("the hub" if to == htag else f"the {sstar} supply"),
                "actions": [{"kind": "tag", "device": d["device_code"], "add": [to], "type": d["device_type"]}]}
    have = {t: sum(1 for d in mine if d.get("device_type") == t) for t in SUPPLY_KIT}
    lack = (["ftl_relay"] if not hub_relays else []) + [t for t in SUPPLY_KIT if not have[t]]
    vst = str(v.get("status") or "")
    if lack:
        t = lack[0]
        if v.get("location") != hub_belt:
            return {"gate": gate, "next": f"vessel → {hub_belt} to print the supply kit",
                    "actions": [{"kind": "travel", "device": v.get("device_code"), "to": hub_belt}]}
        if vst.startswith("printing"):
            return {"gate": gate, "next": f"the vessel prints the supply kit ({len(lack)} to go)", "actions": []}
        cost = cost_of(t, w["bps"])
        if not cost:
            return {"gate": gate, "next": f"no blueprint known for {t.replace('_', ' ')}", "actions": [], "blocked": True}
        short = short_of(cost, amounts(w["inventory"].get(hub_belt)))
        if short:
            dry = sorted(r for r in short if r in (sp.get("for") or []))
            left = ", ".join(f"{int(math.ceil(q))} {r}" for r, q in sorted(short.items(), key=lambda x: -x[1]))
            return {"gate": gate, "actions": [], "blocked": bool(dry),
                    "next": f"{t.replace('_', ' ')} for the supply kit: short {left}"
                            + (f" — the hub belt can't give {', '.join(dry)}" if dry else "")}
        acts = [{"kind": "vessel_stop_mining"}] if vst.startswith("mining") else []
        return {"gate": gate, "next": f"print {t.replace('_', ' ')} on the vessel (supply kit)",
                "actions": acts + [{"kind": "print", "device_type": t}]}
    for r in hub_relays:   # the loadout pass takes a deployed relay to L4/L5 and switches it on
        if r.get("stowed_in_device_code") == v.get("device_code"):
            if star_of(v.get("location")) != hub:
                return {"gate": gate, "next": f"vessel → {hub_belt} to drop the hub's relay",
                        "actions": [{"kind": "travel", "device": v["device_code"], "to": hub_belt}]}
            return {"gate": gate, "next": f"deploy the hub's relay {r['device_code']}",
                    "actions": [{"kind": "deploy", "device": r["device_code"]}]}
    plate = next((d for d in mine if d.get("device_type") == "surge_plate"), None)
    hauler = next((d for d in mine if d.get("device_type") == "transport_hauler"), None)
    if plate and plate.get("stowed_in_device_code"):
        return {"gate": gate, "next": f"deploy the surge plate {plate['device_code']}",
                "actions": [{"kind": "deploy", "device": plate["device_code"]}]}
    if plate and hauler and hauler.get("attached_to_device_code") != plate["device_code"]:
        if in_transit(plate) or in_transit(hauler):
            return {"gate": gate, "next": "waiting for the plate and hauler to meet", "actions": []}
        if plate.get("location") != hauler.get("location"):
            return {"gate": gate, "next": f"surge plate → {hauler.get('location')} to take the hauler",
                    "actions": [{"kind": "travel", "device": plate["device_code"], "to": hauler.get("location")}]}
        return {"gate": gate, "next": f"the hauler {hauler['device_code']} rides the plate {plate['device_code']}",
                "actions": [{"kind": "attach", "carrier": plate["device_code"], "device": hauler["device_code"]}]}
    ride = {x["device_code"] for x in (plate, hauler) if x}
    aboard = [d for d in mine if d.get("stowed_in_device_code") == v.get("device_code")]
    outside = [d for d in mine if d["device_code"] not in ride and not d.get("stowed_in_device_code")
               and not d.get("attached_to_device_code") and d.get("location") and star_of(d["location"]) != sstar
               and _stowable(d) and not in_transit(d)]
    if outside:
        at = sorted(outside, key=lambda d: (d["location"] != v.get("location"), d["location"]))[0]["location"]
        if v.get("location") != at:
            return {"gate": gate, "next": f"vessel → {at} to take {len(outside)} device(s) to {sstar}",
                    "actions": [{"kind": "travel", "device": v.get("device_code"), "to": at}]}
        return {"gate": gate, "next": f"{len(outside)} device(s) board the vessel for {sstar}",
                "actions": [{"kind": "stow", "device": d["device_code"]} for d in outside if d["location"] == at]}
    if aboard:
        if v.get("location") != sbelt:
            return {"gate": gate, "next": f"vessel → {sbelt} with {len(aboard)} device(s) for the supply",
                    "actions": [{"kind": "travel", "device": v.get("device_code"), "to": sbelt}]}
        return {"gate": gate, "next": f"{len(aboard)} device(s) come off the vessel at {sbelt}",
                "actions": [{"kind": "deploy", "device": d["device_code"]} for d in aboard]}
    if plate and not in_transit(plate) and star_of(plate.get("location")) != sstar:
        return {"gate": gate, "next": f"surge plate {plate['device_code']} → {sbelt} with the hauler",
                "actions": [{"kind": "travel", "device": plate["device_code"], "to": sbelt}]}
    if star_of(v.get("location")) == sstar:   # home once both relays are on (the network then reaches the supply)
        on = [d for d in mine + hub_relays if d.get("device_type") == "ftl_relay" and str(d.get("status") or "").startswith("relaying")]
        if len(on) < 2:
            return {"gate": gate, "next": "waiting for both relays to switch on before the vessel goes home", "actions": []}
        return {"gate": gate, "next": f"vessel → {hub_belt}: {sstar} is in the relay network",
                "actions": [{"kind": "travel", "device": v["device_code"], "to": hub_belt}]}
    return {"note": f"{sstar} supplies {', '.join(sp.get('for') or [])}"}


def haul_plan(f: dict, w: dict) -> dict | None:
    """One supply run: the hauler loads at the supply belt what the hub's factories wait for and the plate flies it to
    them, once that covers all of it the supply belt can give, or fills the hold. None: nothing worth a trip yet."""
    b = f["boot"]
    sp, hub = b.get("supply"), b.get("hub")
    if not sp or not hub:
        return None
    stag = f"fleet:{sp['fleet']}"
    mine = [d for d in members(f, w["devices"]) if stag in (d.get("tags") or [])]
    plate = next((d for d in mine if d.get("device_type") == "surge_plate"), None)
    hauler = next((d for d in mine if d.get("device_type") == "transport_hauler"), None)
    sbelt = (best_belt(w["scans"].get(sp["star"])) or {}).get("designation")
    if not plate or not hauler or not sbelt or plate.get("location") != sbelt or in_transit(plate) \
            or hauler.get("attached_to_device_code") != plate["device_code"] or not str(plate.get("status") or "").startswith("idle"):
        return None
    factory = next((d for d in w["devices"] if star_of(d.get("location")) == hub and isinstance(d.get("waiting_for"), dict)), None)
    want = waiting_short(w["devices"], {hub})
    if not factory or not want:
        return None
    offered = (w.get("open_res") or {}).get(sbelt)
    if offered is None:
        offered = {r for r, lv in ((best_belt(w["scans"].get(sp["star"])) or {}).get("resources") or {}).items() if YIELD.get(lv)}
    want = {r: q for r, q in want.items() if r in offered}
    stock = amounts(w["inventory"].get(sbelt))
    room = float(hauler.get("cargo_capacity") or (w["bps"].get("transport_hauler") or {}).get("cargo_capacity") or 100)
    take: dict[str, int] = {}
    for r, q in sorted(want.items(), key=lambda x: -x[1]):
        n = math.floor(min(q, stock.get(r, 0.0), room - sum(take.values())))
        if n >= 1:
            take[r] = n
    full = sum(take.values()) >= room - 1
    if not take or not (full or all(take.get(r, 0) >= math.ceil(q) for r, q in want.items())):
        return None
    return {"plate": plate["device_code"], "hauler": hauler["device_code"], "from": sbelt, "to": factory["location"],
            "take": take}


def outpost_candidates(f: dict, w: dict) -> dict:
    """Systems for the next outpost: scanned, within the radius of the hub, good enough, and within a relay's reach of
    the chain (hub, outposts, waypoints). None in reach but good ones further out: a waypoint system to relay through."""
    b, s = f["boot"], settings(f["boot"])
    hub, pos = b["hub"], w["pos"]
    links = chain(f)
    taken = set(links) | set(w.get("homes", set()))
    pool = [x for x in w["scans"] if x in pos and x not in taken and x not in w["warded"] and dist(hub, x, pos) <= s["radius"]]
    rows = []
    for x in pool:
        sc = system_score(x, w, hub)
        rk = risk(x, w)
        if (sc.get("score") or 0) - rk["penalty"] < s["min_score"] or rk["open"] or not best_belt(w["scans"].get(x)):
            continue
        reach = min((dist(x, c, pos) for c in links), default=1e9)
        rows.append({"star": x, "score": sc["score"], "reasons": rk["reasons"] + sc["reasons"][:4], "chain_ly": round(reach, 1),
                     "in_chain": reach <= RELAY_LY, "distance": round(dist(hub, x, pos), 1)})
    ok = sorted((r for r in rows if r["in_chain"]), key=lambda r: (-r["score"], r["distance"]))
    if ok:
        return {"kind": "outpost", "options": ok[:5]}
    far = sorted(rows, key=lambda r: (-r["score"], r["distance"]))
    for r in far:   # a system within relay reach of both the chain and the candidate
        via = sorted((y for y in pos if y not in taken and y not in w["warded"] and dist(y, r["star"], pos) <= RELAY_LY
                      and min((dist(y, c, pos) for c in links), default=1e9) <= RELAY_LY),
                     key=lambda y: (dist(y, r["star"], pos), y))
        if via:
            return {"kind": "waypoint", "options": [{"star": y, "for": r["star"], "score": r["score"],
                                                     "reasons": [f"relay link to {r['star']} ({r['score']})"],
                                                     "distance": round(dist(hub, y, pos), 1)} for y in via[:3]]}
    return {"kind": None, "options": []}


def ratios(short_types: dict[str, int], bps: dict) -> dict[str, float]:
    """maintain_ratios for the mining controllers: the mix of resources the family's missing devices cost (else what
    an autofactory costs), as decimals summing to 1."""
    tot: dict[str, float] = {}
    for t, n in (short_types or {"autofactory": 1}).items():
        for r, v in cost_of(t, bps).items():
            tot[r] = tot.get(r, 0.0) + v * n
    if not tot:
        for r, v in cost_of("autofactory", bps).items():
            tot[r] = v
    s = sum(tot.values()) or 1.0
    out = {r: round(tot.get(r, 0.0) / s, 2) for r in RESOURCES if tot.get(r)}
    return out or {"structural": 1.0}


def costs(w: dict, stock: dict[str, float]) -> list[dict]:
    rows = []
    for t in COST_TYPES:
        c = cost_of(t, w["bps"])
        if not c:
            continue
        cover = min((stock.get(r, 0.0) / v for r, v in c.items() if v > 0), default=0.0)
        rows.append({"type": t, "cost": c, "cover": round(min(1.0, cover) * 100), "print_min": round(
            float((w["bps"].get(t) or {}).get("print_time") or 0) / 60, 1)})
    return rows


def strays(v: dict, devs: list[dict], star: str) -> list[dict]:
    """The bootstrap's devices the vessel must take aboard before it moves the operation to `star`: stowable members
    outside it, neither aboard nor riding something else (Delta Force 2026-10-22: a new home was set and the vessel
    left its drones behind at the old one)."""
    return [d for d in devs if d["device_code"] != v["device_code"] and not d.get("stowed_in_device_code")
            and not d.get("attached_to_device_code") and star_of(d.get("location")) != star
            and ("stow" in (d.get("features") or []) or "stow" in (d.get("available_commands") or [])
                 or d.get("device_type") in ("mining_drone", "survey_drone"))]


def fetch(v: dict, devs: list[dict], star: str) -> dict | None:
    """Actions to fetch the strays (nearest place first: where the vessel is), or None when all are aboard."""
    left = strays(v, devs, star)
    if not left:
        return None
    moving = [d for d in left if not d.get("location")]
    placed = sorted((d for d in left if d.get("location")), key=lambda d: (d["location"] != v.get("location"), d["location"]))
    if not placed:
        return {"gate": "everyone aboard", "next": f"waiting for {len(moving)} device(s) on their way", "actions": []}
    at = placed[0]["location"]
    if v.get("location") != at:
        return {"gate": "everyone aboard", "next": f"vessel → {at} to fetch {len(left)} device(s) first",
                "actions": [{"kind": "travel", "device": v["device_code"], "to": at}]}
    return {"gate": "everyone aboard", "next": f"{len(placed)} device(s) board the vessel before it moves to {star}",
            "actions": [{"kind": "stow", "device": d["device_code"]} for d in placed if d["location"] == at]}


def evaluate(f: dict, w: dict) -> dict:
    """Where the bootstrap is and what it does next — no side effects except the stage moving on when a gate is met
    (the caller saves the fleet). {stage, label, gate, next, actions, decision, costs, chain, blocked}."""
    b = f["boot"]
    s = settings(b)
    devs = members(f, w["devices"])
    v = next((d for d in devs if d["device_code"] == b.get("vessel")), None)
    out: dict[str, Any] = {"actions": [], "decision": b.get("decision"), "chain": chain(f), "blocked": False}
    stock: dict[str, float] = {}

    def done(stage: str, **kw) -> dict:
        out.update(stage=stage, label=LABEL.get(stage, stage), costs=costs(w, stock), **kw)
        return out

    if b.get("paused"):
        return done(b["stage"], gate="paused", next="resume to carry on")
    if not v:
        return done(b["stage"], gate="vessel not found", next=f"the vessel {b.get('vessel')} isn't in the device list",
                    blocked=True)
    if in_transit(v):
        stock = amounts(w["inventory"].get(v.get("location")))
        return done(b["stage"], gate="vessel in transit", next=f"waiting for {v['device_code']} to arrive")
    here = star_of(v.get("location"))
    stage = b["stage"]
    # the replicant rides along: a matrix of the bootstrap's left out of the hold (deployed by a mission before a
    # conversion, DELTA 2026-10-22) boards before anything else, or the vessel would fly off out of command range
    loose = [d for d in devs if "matrix" in (d.get("device_type") or "") and d["device_code"] != v["device_code"]
             and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code") and d.get("location")
             and stage not in ("hub", "outposts", "operate")]
    if loose:
        m = loose[0]
        if m.get("location") != v.get("location"):
            return done(stage, gate="replicant aboard", next=f"vessel → {m['location']} to take {m['device_code']} aboard",
                        actions=[{"kind": "travel", "device": v["device_code"], "to": m["location"]}])
        return done(stage, gate="replicant aboard", next=f"{m['device_code']} boards the vessel",
                    actions=[{"kind": "stow", "device": m["device_code"]}])
    if stage == "home":
        home = b["home"]
        if home in w["warded"]:
            b["stage"] = "survey"
            return evaluate(f, w)
        g = fetch(v, devs, home) if here != home else None   # a new home: nobody is left behind (before it leaves)
        if g:
            return done(stage, **g)
        if home not in w["scans"]:
            return done(stage, gate=f"{home} scanned", next=f"system scan of {home}",
                        actions=[{"kind": "scan", "star": home}] if here == home else [{"kind": "travel", "device": v["device_code"], "to": home}])
        c = compound(f, w, home, hold_cap(f, w), autofactory=False)
        stock = c.get("stock") or {}
        if c.get("done"):
            b["stage"] = "survey"
            return evaluate(f, w)
        return done(stage, gate=c["gate"], next=c["next"], actions=c["actions"], blocked=c.get("blocked", False))
    if stage == "survey":
        q = survey_queue(f, w)
        if here not in w["scans"] and here and here not in w["warded"]:
            return done(stage, gate=f"{len(q)} system(s) left within {s['radius']:g} ly", next=f"system scan of {here}",
                        actions=[{"kind": "scan", "star": here}])
        if q:
            return done(stage, gate=f"{len(q)} system(s) left within {s['radius']:g} ly", next=f"vessel → {q[0]}",
                        actions=[{"kind": "travel", "device": v["device_code"], "to": q[0], "visit": q[0]}])
        b["candidates"] = hub_candidates(f, w)
        b["stage"] = "hub_choice"
        b["decision"] = {"kind": "hub", "options": b["candidates"]} if b["candidates"] else None
        return evaluate(f, w)
    if stage == "hub_choice":
        if b.get("hub"):
            b["stage"], b["decision"] = "move", None
            return evaluate(f, w)
        if not b.get("decision"):
            b["candidates"] = hub_candidates(f, w)
            b["decision"] = {"kind": "hub", "options": b["candidates"]} if b["candidates"] else None
            out["decision"] = b["decision"]
        return done(stage, gate="waiting for your OK", next="pick the hub below" if b.get("decision")
                    else f"no minable system within {s['radius']:g} ly — widen the radius", blocked=not b.get("decision"))
    hub = b.get("hub")
    if stage == "move":
        drones = [d for d in devs if d["device_code"] != v["device_code"] and d.get("device_type") in ("mining_drone", "survey_drone")]
        g = fetch(v, devs, hub)   # every device outside the hub boards first (the survey left them at home), not only one
        if g:
            return done(stage, **g)
        if here != hub:
            return done(stage, gate=f"vessel at {hub}", next=f"vessel → {hub} with {len(drones)} drone(s) aboard",
                        actions=[{"kind": "travel", "device": v["device_code"], "to": hub}])
        if hub not in w["scans"]:
            return done(stage, gate=f"{hub} scanned", next=f"system scan of {hub}", actions=[{"kind": "scan", "star": hub}])
        b["stage"] = "hub_compound"
        return evaluate(f, w)
    if stage == "hub_compound":
        c = compound(f, w, hub, s["hub_miners"] + max(1, math.ceil(s["hub_miners"] * s["survey_per_miner"])), autofactory=True)
        stock = c.get("stock") or {}
        factory = next((d for d in devs if d.get("device_type") == "autofactory" and star_of(d.get("location")) == hub
                        and not d.get("stowed_in_device_code")), None)
        if factory:
            out["actions"] = [{"kind": "make_hub", "factory": factory["device_code"]}]
            return done(stage, gate="autofactory deployed", next=f"the hub fleet takes over at {hub}", actions=out["actions"])
        acts = c["actions"]
        if risk(hub, w)["open"] and any(a["kind"] == "print" and a.get("device_type") == "autofactory" for a in acts):
            # a rock is coming: no autofactory until it has passed (the drones keep compounding meanwhile)
            return done(stage, gate="no incoming object at the hub", next=f"autofactory waits: an asteroid is heading for {hub}",
                        actions=[a for a in acts if a["kind"] != "print"], blocked=True)
        return done(stage, gate=c["gate"], next=c["next"], actions=acts, blocked=c.get("blocked", False))
    rep = (w.get("reports") or {})
    hub_fleet = (b.get("children") or {}).get("hub")
    if stage == "hub":
        r = rep.get(hub_fleet) or {}
        sp = supply_step(f, w) if hub_fleet else None
        if sp and "note" not in sp:
            if sp.get("decision"):
                out["decision"] = sp.pop("decision")
            return done(stage, **sp)
        if hub_fleet and r and not r.get("short"):
            b["stage"] = "outposts"
            return evaluate(f, w)
        return done(stage, gate="hub at its loadout", next=(f"hub short {r.get('short')} device(s): its autofactory prints them"
                                                            if r else "the loadout pass builds the hub up")
                    + (f" · {sp['note']}" if sp else ""))
    if stage == "outposts":
        if len(b.get("outposts") or []) >= s["outposts"]:
            b["stage"] = "operate"
            return evaluate(f, w)
        if not b.get("decision"):
            oc = outpost_candidates(f, w)
            if oc["kind"]:
                b["decision"] = {"kind": oc["kind"], "options": oc["options"]}
                out["decision"] = b["decision"]
        n = len(b.get("outposts") or [])
        return done(stage, gate=f"{n} of {s['outposts']} outposts", next="pick the next outpost below" if b.get("decision")
                    else f"no good system within {s['radius']:g} ly of {hub} — survey further or lower the minimum score",
                    blocked=not b.get("decision"))
    if stage == "operate":
        hit = [x for x in chain(f) if x in w["warded"]]
        if hit and not b.get("decision"):
            oc = outpost_candidates(f, w)
            if oc["kind"] == "outpost":
                b["decision"] = {"kind": "replace", "for": hit[0], "options": oc["options"]}
                out["decision"] = b["decision"]
        return done(stage, gate="handed over", next=(f"{hit[0]} was warded by another player — pick a replacement below"
                                                     if hit else "the stationed fleets run themselves"))
    return done(stage, gate="?", next="unknown stage", blocked=True)
