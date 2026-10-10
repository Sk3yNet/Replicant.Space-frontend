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
                drones, a surge carrier for kits) — the loadout pass, restricted to this bootstrap's devices.
  outposts ★    three mining outposts within the radius of the hub that keep the relay chain (each within a relay's
                7.5 ly of the hub or an outpost); each a stationed fleet sending its materials to the hub by freighter.
  operate       handed over to the usual automation. A system another player wards: ★ move to the next best.

Self-reliant: everything it prints is tagged `boot:<id>`; the loadout pass never lends it others' spares, factories or
carriers, nor lends its own to others. Materials only move on transport-feature devices (haulers, freighters); a heaven
vessel carries devices, not materials.
"""
from __future__ import annotations

import math
from typing import Any

from .prospects import RARITY, YIELD, score as prospect_score

STAGES = ["home", "survey", "hub_choice", "move", "hub_compound", "hub", "outposts", "operate"]
LABEL = {"home": "Home: compounding", "survey": "Survey trip", "hub_choice": "Choose the hub", "move": "Moving to the hub",
         "hub_compound": "Hub: compounding to an autofactory", "hub": "Hub: building up", "outposts": "Outposts",
         "operate": "Operating"}
RELAY_LY = 7.5
DEFAULTS = {"radius": 10.0, "ward_hours": 6.0, "hold": 10, "hub_miners": 8, "survey_per_miner": 0.5, "survey_max": 12,
            "min_score": 35, "outposts": 3}
COST_TYPES = ["survey_drone", "mining_drone", "ftl_relay", "system_ward", "autofactory", "ami_mining_controller",
              "ami_survey_controller", "ami_transport_controller",
              "transport_hauler", "cargo_freighter", "surge_carrier"]
OUTPOST_KIT = {"mining_drone": 4, "survey_drone": 2, "ami_mining_controller": 1, "ami_survey_controller": 1, "ftl_relay": 1,
               "ami_transport_controller": 1, "cargo_freighter": 1}
HUB_KIT = {"ami_mining_controller": 1, "ami_survey_controller": 1, "ftl_relay": 1, "ami_transport_controller": 1,
           "transport_hauler": 2, "surge_carrier": 1}
RESOURCES = ["structural", "conductive", "silicates", "carbon", "volatiles", "rares"]


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


def mine_choice(belt: dict | None, need: dict[str, float]) -> str:
    """What a drone mines: the resource the next print is shortest of, weighted by how much this belt yields of it;
    nothing short → the belt's best by yield × rarity."""
    levels = (belt or {}).get("resources") or {}
    y = {r: YIELD.get(levels.get(r), 0) for r in RARITY}
    if need:
        pick = max(need, key=lambda r: (need[r] * max(1, y.get(r, 0)), r))
        if y.get(pick, 0):
            return pick
    return max(y, key=lambda r: (y[r] * RARITY[r], r)) if any(y.values()) else "structural"


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


def compound(f: dict, w: dict, star: str, cap: int, autofactory: bool) -> dict:
    """One step of compounding in `star`: {actions, gate, next, done}."""
    b, s = f["boot"], settings(f["boot"])
    devs = members(f, w["devices"])
    v = next((d for d in devs if d["device_code"] == b["vessel"]), None) or {}
    scan = w["scans"].get(star)
    belt = best_belt(scan)
    if not belt:
        return {"actions": [], "gate": f"no asteroid belt known in {star}", "next": "needs a belt to mine", "blocked": True}
    bloc = belt["designation"]
    if v.get("location") != bloc:
        return {"actions": [{"kind": "travel", "device": v.get("device_code"), "to": bloc}], "gate": "vessel at the belt",
                "next": f"vessel → {bloc}"}
    here = [d for d in devs if d["device_code"] != b["vessel"]]
    aboard = [d for d in here if d.get("stowed_in_device_code") == b["vessel"]]
    deployed = [d for d in here if d.get("location") == bloc and not d.get("stowed_in_device_code")]
    miners = [d for d in here if d.get("device_type") == "mining_drone"]
    surveys = [d for d in here if d.get("device_type") == "survey_drone"]
    factory = next((d for d in here if d.get("device_type") == "autofactory"), None)
    stock = amounts(w["inventory"].get(bloc))
    target = compound_target(len(miners), len(surveys), cap, s["survey_per_miner"], autofactory, bool(factory))
    need = short_of(cost_of(target, w["bps"]), stock) if target else {}
    acts: list[dict] = []
    for d in aboard:
        if d.get("device_type") in ("mining_drone", "survey_drone", "autofactory"):
            acts.append({"kind": "deploy", "device": d["device_code"]})
    for d in deployed:
        st = str(d.get("status") or "")
        if d.get("device_type") == "mining_drone" and st.startswith("idle"):
            acts.append({"kind": "start_mining", "device": d["device_code"], "resource": mine_choice(belt, need)})
        elif d.get("device_type") == "survey_drone" and st.startswith("idle"):
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
            "next": f"mining for a {target.replace('_', ' ')} — short {left}" + (f" (vessel mines {pick})" if pick else "")}


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
        value = sc["score"] + 4 * min(3, len(good)) + 2 * min(3, len(chained))
        out.append({"star": x, "score": sc["score"], "value": round(value), "reasons": sc["reasons"][:4],
                    "good_near": sorted(good, key=lambda y: dist(x, y, pos))[:6], "in_relay": len(chained),
                    "distance": round(dist(home, x, pos), 1) if dist(home, x, pos) < 1e9 else None})
    return sorted(out, key=lambda c: (-c["value"], c["distance"] or 0, c["star"]))[:6]


def chain(f: dict) -> list[str]:
    b = f["boot"]
    return [x for x in [b.get("hub")] + [o["star"] for o in b.get("outposts") or []] + list(b.get("waypoints") or []) if x]


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
        if (sc.get("score") or 0) < s["min_score"] or not best_belt(w["scans"].get(x)):
            continue
        reach = min((dist(x, c, pos) for c in links), default=1e9)
        rows.append({"star": x, "score": sc["score"], "reasons": sc["reasons"][:4], "chain_ly": round(reach, 1),
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
    if stage == "home":
        home = b["home"]
        if home in w["warded"]:
            b["stage"] = "survey"
            return evaluate(f, w)
        if home not in w["scans"]:
            return done(stage, gate=f"{home} scanned", next=f"system scan of {home}",
                        actions=[{"kind": "scan", "star": home}] if here == home else [{"kind": "travel", "device": v["device_code"], "to": home}])
        c = compound(f, w, home, s["hold"], autofactory=False)
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
        out_there = [d for d in drones if d.get("stowed_in_device_code") != v["device_code"]]
        if out_there and star_of(out_there[0].get("location")) != hub:
            at = out_there[0].get("location")
            if v.get("location") != at:
                return done(stage, gate="drones aboard", next=f"vessel → {at} to fetch its drones",
                            actions=[{"kind": "travel", "device": v["device_code"], "to": at}])
            return done(stage, gate="drones aboard", next=f"{len(out_there)} drone(s) board the vessel",
                        actions=[{"kind": "stow", "device": d["device_code"]} for d in out_there if d.get("location") == at])
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
        return done(stage, gate=c["gate"], next=c["next"], actions=c["actions"], blocked=c.get("blocked", False))
    rep = (w.get("reports") or {})
    hub_fleet = (b.get("children") or {}).get("hub")
    if stage == "hub":
        r = rep.get(hub_fleet) or {}
        if hub_fleet and r and not r.get("short"):
            b["stage"] = "outposts"
            return evaluate(f, w)
        return done(stage, gate="hub at its loadout", next=(f"hub short {r.get('short')} device(s): its autofactory prints them"
                                                            if r else "the loadout pass builds the hub up"))
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
