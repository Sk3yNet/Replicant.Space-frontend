"""The logistics service: materials delivered on demand by your logistics fleets (Fleets: role logistics; Fleets › Logistics).

Demand, tracked every pass (engine stage logistics_pass), one request per destination:
  print     an autofactory's queue (the print waiting for resources and every print queued behind it): their blueprint
            cost minus what's stocked at the factory and on its way
  contract  an open contract you approved, or one from a civilization you pre-approved (Contracts page): its price minus
            what's at its location
  trade     a trade you approved (Shop: approve; fulfilled once): its price minus what's at the shop's location. Shop
            listings nobody approved are never fulfilled.
  return    a stray stockpile (not in a home system, not where something is printed or asked for): brought back to the
            nearest gather point (a fleet that takes materials in) when a logistics device has nothing better to do; in a
            system a destination fleet (one that takes materials in) moved away from, it follows that fleet instead

Priority (owner's order, 2026-10-12): 1 queued prints (the ones waiting for resources first), 2 local deliveries (the
stock is in the same system), 3 long-range transfers, 4 stockpile returns. A request can be bumped or skipped on the
Logistics page. Each home system keeps what its own prints need (a reserve): only the surplus is taken.

Task categories (owner 2026-10-23): each request falls in one category (CATEGORIES: prints, trades & contracts, rescues,
shop listings, repair patrols, hub upkeep, stockpile returns). Every free logistics carrier is given one category that has work and
keeps it, pass after pass, until that category runs dry; a category with work and no carrier takes one from the
biggest. Priority orders the requests within a category. Evacuations (priority 0) go first with every carrier; after
each category has worked with its own carriers, the ones left over help any category, stockpile returns last.

The logistics devices: every member of a logistics fleet. Materials ride in cargo devices: freighters and cargo vessels fly anywhere;
transport drones and haulers work inside their system, or are towed (attached) to another by a logistics carrier (plate,
platform, carrier, cargo vessel), which also fetches them from another system to the stockpile first. Anywhere outside an active relay network (relaynet.coverage)
a logistics device hosting a replicant flies alongside — a cargo vessel hosting one carries its own escort — or nothing goes.
After a delivery outside the network the logistics devices come back to wait in it: their fleet's home when that's covered, else
the nearest covered system. Inside the network an idle logistics device gathers at the nearest printing hub (a fleet that
takes materials in): it flies there, or a free carrier tows it (members of a stationed logistics fleet stay at its home). When a delivery completes a contract's or trade's price, the escort's replicant (or one
already there) fulfills it at once; a request already stocked waits for a replicant logistics device to come and fulfill it.

Inside one system the system's own free AMI transport controller (drones / haulers) takes the delivery when there is
one (a `delivery` directive); the logistics devices cover the rest. A replicant counts wherever its matrix rides: a fleet carrier
holding a matrix container with the replicant's matrix in it is that replicant's escort.

Rescues (last tier): a device of yours out of command range — beyond an active relay network with no replicant in its
system; placed relays and beacons, wards, hubs and fleets on a mission excepted — is brought home (its fleet's home,
else the nearest covered system). One that can surge is reached by a logistics device hosting a replicant, in its own
system or anywhere its dark relay network covers (nearest first), and commanded home; one that can't is picked up from
its last known location by a logistics carrier with a free seat, with a replicant alongside unless the carrier hosts one.
A rescue whose trips add up to more than the sector range (SECTOR_RANGE_H, a day at the game's surge pace) waits for your
Confirm on the Logistics page (owner 2026-10-22). Carriers only serve stars within the sector range (SECTOR_RANGE_LY), so a
sector of its own gets a logistics fleet of its own; a star with no known position is out of range.

Repair patrols (owner 2026-10-22): repair devices of the logistics fleets are taken to the systems with the most wear
and too few repair devices (one per six worn devices), worst first, by a free logistics carrier (or on their own, if
they can surge), and set to patrol there. Like rescues, a trip of more than a day waits for your Confirm.

Devices go the same way: deliveries of devices (loadouts.plan) take logistics-fleet carriers first.

Drop-offs (owner 2026-10-20): an idle logistics carrier inside the network with leftover cargo, or with devices aboard
that aren't its work (stray_aboard), goes to the nearest receiving point (a fleet that takes materials in, or a working
system hub of yours), deposits the cargo and lets those devices off.
"""
from __future__ import annotations

import json

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .automations import STEP_TIMEOUT, step
from .shapes import as_amounts

KV_STATE = "logistics_state"        # the last pass: requests, runs, unmet
KV_PREFS = "logistics_prefs"        # {"bump": {request id: tier}, "skip": [request ids], "confirm": {request id: iso}}
KV_RESCUE_PAUSE = "logistics_rescue_pause"   # {"all": {"until": iso | None}, "fleets": {fleet id: {"until": iso | None}}}
SURGE_S_PER_LY = 50.0               # the game's surge pace between systems (hubroute: 37.5 into a hub of ours)
SECTOR_RANGE_H = 24.0               # the sector range (owner 2026-10-22): an automated trip longer than this waits for your OK
SECTOR_RANGE_LY = SECTOR_RANGE_H * 3600 / SURGE_S_PER_LY   # 1728 ly: logistics carriers serve only stars this near
KV_PICKUPS = "logistics_pickups"   # {deal location: {"types": {type: n}, "to": gather star, "at"}} — devices paid out
KV_MOVES = "logistics_moves"   # {old home star: {"to": new home star, "fleet", "at"}} — a destination fleet's stock follows it
KV_TRADES = "trade_approvals"  # {"trades": {controller:trade_code: iso}, "counts": {key: n}, "done": {key: iso}}


def trade_times(ok: dict, key: str, stock=None) -> int:
    """Units of an approved trade to buy (Shop: approve ×n; default 1), at most the listed stock. The game's buy call
    fulfills one unit and takes no quantity, so a delivery pays n prices and executes the trade n times."""
    try:
        n = max(1, int((ok.get("counts") or {}).get(key) or 1))
    except (TypeError, ValueError):
        n = 1
    if isinstance(stock, int) and stock > 0:
        n = min(n, stock)
    return n


def times(amounts: dict, n: int) -> dict:
    return {k: v * n for k, v in amounts.items()} if n > 1 else dict(amounts)
ROLE = "logistics"
MIN_RETURN = 25.0              # a stray pile smaller than this isn't worth a trip
TIERS = {0: "evacuation", 1: "queued prints", 2: "local delivery", 3: "long-range transfer", 4: "stockpile return", 5: "rescue",
         6: "shop listing", 7: "repair patrol"}
# task categories (owner 2026-10-23): each free logistics carrier works one category until it runs dry, so a small
# category isn't starved by a big one; stockpile returns (reallocations) get no carrier of their own and come last
CATEGORIES = {"prints": "queued prints", "deals": "trades & contracts", "rescues": "rescues", "shop": "shop listings",
              "repairs": "repair patrols", "upkeep": "hub upkeep", "returns": "stockpile returns",
              "urgent": "evacuations & priority 0"}
LANES = ("prints", "deals", "rescues", "shop", "repairs", "upkeep")
PATROL_LOAD = 6                # worn devices one patrolling repair device looks after (upkeep.report's "covered")
KEPT_OUT = ("system_ward", "system_hub")   # at work where they stand, like placed relays and beacons: never "rescued"
MIN_LOAD = 0.25               # a cross-system print delivery leaves with its hold at least this full (or all that's wanted)
LONG_HAUL_LY = 15.0            # beyond this a part load waits even when a print is stalled for materials
RELAY_HOP = 7.5                # distances compared in relay hops: anything in the same hop counts as equally near
MOVING = ("travel", "cruis", "surg", "moving", "recall", "returning", "collecting", "depositing", "mining", "printing")


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def logistics_fleets(fleets: list[dict]) -> list[dict]:
    return [f for f in fleets or [] if f.get("role") == ROLE]


def _within(reach: float, star: str) -> str:
    """How far an unmet reason looked: 'within N ly of STAR', or 'to reach STAR' when logistics reaches everywhere (the default)."""
    return f"within {reach:g} ly of {star}" if math.isfinite(reach) else f"to reach {star}"


def _dist(a: str, b: str, pos: dict[str, Any]) -> float:
    if a == b:
        return 0.0
    pa, pb = pos.get(a) or {}, pos.get(b) or {}
    if not pa or not pb:
        return 1e9
    return math.dist([float(pa.get(k) or 0) for k in "xyz"], [float(pb.get(k) or 0) for k in "xyz"])


def _trip_hours(legs: list[tuple[str | None, str | None]], pos: dict[str, Any]) -> float:
    """Hours of surging for (from star, to star) legs at the game's pace. A star with no known position makes it
    infinite: that trip asks for your OK (live 2026-10-22: DELTA, about 28,900 ly from SOLARES, isn't in the catalog)."""
    ly = sum(_dist(a, b, pos) for a, b in legs if a and b)
    return math.inf if ly >= 1e9 else ly * SURGE_S_PER_LY / 3600


def rescue_pauses(raw: dict | None, now: datetime | None = None) -> dict:
    """The rescue pauses still in force (owner 2026-10-26): {"all": entry | None, "fleets": {fleet id: entry}}. An entry
    with no "until" holds until resumed by hand; a timed one lapses by itself."""
    now = now or datetime.now(timezone.utc)
    def live(e: Any) -> bool:
        if not isinstance(e, dict):
            return False
        if not e.get("until"):
            return True
        try:
            return datetime.fromisoformat(e["until"]) > now
        except (TypeError, ValueError):
            return False
    raw = raw or {}
    return {"all": raw.get("all") if live(raw.get("all")) else None,
            "fleets": {f: e for f, e in (raw.get("fleets") or {}).items() if live(e)}}


def _needs_ok(req: dict, hours: float, prefs: dict | None, unmet: list[dict]) -> bool:
    """A rescue whose trips add up to more than SECTOR_RANGE_H waits for your OK on the Logistics page (owner
    2026-10-22: a heaven vessel was sent from SOLARES toward DELTA, the round trip was well over a day)."""
    req["trip_h"] = round(hours, 1) if math.isfinite(hours) else None
    ok = ((prefs or {}).get("confirm") or {}).get(req["id"])
    try:   # an OK holds for two days: a device stranded again later asks again
        fresh = bool(ok) and (datetime.now(timezone.utc) - datetime.fromisoformat(ok)).total_seconds() < 2 * 86400
    except (TypeError, ValueError):
        fresh = False
    if hours <= SECTOR_RANGE_H or fresh:
        return False
    req["confirm"] = True
    long = (f"about {hours:.0f} h of travel (beyond the {SECTOR_RANGE_H:.0f} h sector range)" if math.isfinite(hours)
            else "a trip of unknown length (a star's position isn't known)")
    unmet.append({"req": req["id"], "why": f"waits for your OK: {long}; Confirm to send it"})
    return True


def _short(price: dict, have: dict) -> dict[str, float]:
    return {r: n - have.get(r, 0.0) for r, n in as_amounts(price).items() if n - have.get(r, 0.0) >= 1}


def _add(a: dict, b: dict, sign: float = 1.0) -> dict:
    out = dict(a)
    for r, q in (b or {}).items():
        out[r] = out.get(r, 0.0) + sign * float(q)
    return out


def print_costs(devices: list[dict], bps: dict[str, dict]) -> dict[str, dict]:
    """{factory location: {"cost": {res: n}, "waiting": bool, "types": {type: n}}} — every print still to be paid for: the
    one waiting for resources and those queued behind it (a print already running has been paid)."""
    from .loadouts import is_factory, queued_counts
    out: dict[str, dict] = {}
    for d in devices:
        loc = d.get("location")
        if not loc or not is_factory(d):
            continue
        counts, _ = queued_counts(d)
        st = str(d.get("status") or "")
        running = (d.get("printing") or {}).get("device_type") if isinstance(d.get("printing"), dict) else None
        if st.startswith("printing"):
            running = running or (st[st.find("(") + 1:st.rfind(")")] if "(" in st else None)
            if running and counts.get(running):
                counts[running] -= 1
        e = out.setdefault(loc, {"cost": {}, "waiting": False, "types": defaultdict(int), "factories": [], "stock_only": True})
        e["waiting"] |= st.startswith("waiting_for_resources")
        # only keep-in-stock prints to pay for here (stock.py): supplied last
        from . import printqueue as pq
        from .stock import is_stock
        pr = d.get("printing") if isinstance(d.get("printing"), dict) else {}
        if any(not is_stock(it) for it in pq.items(d)) or (st.startswith("waiting_for_resources") and not is_stock(pr)):
            e["stock_only"] = False
        e["factories"].append(d.get("device_code"))
        for t, n in counts.items():
            if n <= 0:
                continue
            e["types"][t] += n
            e["cost"] = _add(e["cost"], {r: q * n for r, q in as_amounts((bps.get(t) or {}).get("resources") or {}).items()})
    for e in out.values():
        e["types"] = dict(e["types"])
    return {k: v for k, v in out.items() if v["cost"]}


def take_from(inventory: dict[str, Any], pile: str, take: dict[str, float]) -> None:
    """Count `take` as gone from a stockpile (a trip already on its way to pick it up)."""
    have = dict(as_amounts(inventory.get(pile) or {}))
    for r, q in (take or {}).items():
        have[r] = max(0.0, have.get(r, 0.0) - float(q))
    inventory[pile] = {r: q for r, q in have.items() if q > 0}


def queue_items(devices: list[dict], bps: dict[str, dict]) -> dict[str, dict]:
    """{star: {"items": [(seconds, cost)], "factories": n, "dest": factory location}} — each system's prints still to pay
    for, in the order they'll run (factories interleaved by when each item starts; a print already running is paid)."""
    from .loadouts import is_factory
    from . import printqueue as pq
    per: dict[str, list] = defaultdict(list)
    dests: dict[str, str] = {}
    nfac: dict[str, int] = defaultdict(int)
    for d in devices:
        loc = d.get("location")
        if not loc or not is_factory(d):
            continue
        star = star_of(loc)
        nfac[star] += 1
        st = str(d.get("status") or "")
        types = []
        cur = (d.get("printing") or {}).get("device_type") if isinstance(d.get("printing"), dict) else None
        if st.startswith("waiting_for_resources") and cur:
            types.append(cur)
        for it in pq.items(d):
            types += [it["device_type"]] * max(1, int(it.get("quantity") or 1))
        t0 = 0.0
        for t in types:
            secs = float((bps.get(t) or {}).get("print_time") or 600)
            per[star].append((t0, secs, as_amounts((bps.get(t) or {}).get("resources") or {}), loc))
            t0 += secs
        if types:
            dests.setdefault(star, loc)
    out = {}
    for star, items in per.items():
        items.sort(key=lambda x: (x[0], x[3]))
        out[star] = {"items": [(secs, cost) for _, secs, cost, _ in items], "factories": max(1, nfac[star]), "dest": dests[star]}
    return out


def funded_hours(q: dict, stock: dict[str, float]) -> tuple[float, int]:
    """(hours of printing the stock pays for, in queue order, until the first print it can't pay; index of that print)."""
    left, secs = dict(stock), 0.0
    for i, (s_, cost) in enumerate(q["items"]):
        if any(left.get(r, 0.0) + 1e-9 < n for r, n in cost.items()):
            return secs / 3600 / q["factories"], i
        for r, n in cost.items():
            left[r] = left.get(r, 0.0) - n
        secs += s_
    return secs / 3600 / q["factories"], len(q["items"])


def balance(queues: dict[str, dict], kept: dict[str, dict], spare: dict[str, dict], incoming: dict[str, dict],
            max_steps: int = 200) -> dict[str, dict[tuple[str, str], float]]:
    """Share hubs' reserves so every system's queue is paid for about as many hours ahead: while the system with the
    fewest funded hours can't pay its next print from its own stock (plus everything spare anywhere), the system with
    the most funded hours gives what that print lacks, as long as it still has at least as many hours left afterwards.
    Returns {destination factory location: {(donor pile, resource): amount}}."""
    total_spare: dict[str, float] = defaultdict(float)
    for l, s_ in spare.items():
        for r, q in s_.items():
            total_spare[r] += q
    own: dict[str, dict[str, float]] = {}
    for star in queues:
        st: dict[str, float] = defaultdict(float)
        for l, k in kept.items():
            if star_of(l) == star:
                for r, q in k.items():
                    st[r] += q
        for l, inc in incoming.items():
            if star_of(l) == star:
                for r, q in inc.items():
                    st[r] += q
        own[star] = dict(st)
    shares: dict[str, dict[tuple[str, str], float]] = defaultdict(lambda: defaultdict(float))
    kept = {l: dict(k) for l, k in kept.items()}
    for _ in range(max_steps):
        hours = {s_: funded_hours(q, {r: own[s_].get(r, 0.0) + total_spare.get(r, 0.0) for r in set(own[s_]) | set(total_spare)})
                 for s_, q in queues.items()}
        needy = [s_ for s_, (h, i) in hours.items() if i < len(queues[s_]["items"])]
        if not needy:
            break
        d = min(needy, key=lambda s_: (hours[s_][0], s_))
        cost = queues[d]["items"][hours[d][1]][1]
        mine = own[d]
        # what the next print lacks once the system's own stock has paid for everything before it
        paid: dict[str, float] = defaultdict(float)
        for s_, c in queues[d]["items"][:hours[d][1]]:
            for r, n in c.items():
                paid[r] += n
        lack = {r: n - max(0.0, mine.get(r, 0.0) + total_spare.get(r, 0.0) - paid[r]) for r, n in cost.items()}
        lack = {r: math.ceil(q) for r, q in lack.items() if q > 1e-9}
        best = None
        for s_, q in queues.items():
            if s_ == d or any(own[s_].get(r, 0.0) < n for r, n in lack.items()):
                continue
            after = funded_hours(q, {r: own[s_].get(r, 0.0) - lack.get(r, 0.0) for r in own[s_]})[0]
            d_after = funded_hours(queues[d], {r: mine.get(r, 0.0) + total_spare.get(r, 0.0) + lack.get(r, 0.0)
                                                for r in set(mine) | set(total_spare) | set(lack)})[0]
            if after + 1e-9 >= d_after and (best is None or after > best[0]):
                best = (after, s_)
        if not best:
            break
        donor = best[1]
        for r, n in lack.items():   # out of the donor's piles, biggest first
            for l in sorted((l for l in kept if star_of(l) == donor), key=lambda l: -kept[l].get(r, 0.0)):
                q = min(n, kept[l].get(r, 0.0))
                if q > 0:
                    kept[l][r] -= q
                    shares[queues[d]["dest"]][(l, r)] += q
                    n -= q
                if n <= 0:
                    break
            own[donor][r] = own[donor].get(r, 0.0) - lack[r]
            mine[r] = mine.get(r, 0.0) + lack[r]
    return {k: dict(v) for k, v in shares.items()}


def surplus(inventory: dict[str, dict], costs: dict[str, dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """(what each pile can spare, reserve per system). A system keeps what its own prints need: the reserve is taken
    from the factory's own pile first, then the system's biggest piles."""
    reserve: dict[str, dict] = defaultdict(dict)
    for loc, e in costs.items():
        reserve[star_of(loc)] = _add(reserve[star_of(loc)], e["cost"])
    spare = {loc: dict(as_amounts(items)) for loc, items in inventory.items()}
    for star, res in reserve.items():
        piles = sorted((l for l in spare if star_of(l) == star),
                       key=lambda l: (l not in costs, -sum(spare[l].values()), l))
        left = dict(res)
        for l in piles:
            for r in list(left):
                take = min(left[r], spare[l].get(r, 0.0))
                if take > 0:
                    spare[l][r] -= take
                    left[r] -= take
    return {l: {r: q for r, q in s.items() if q >= 1} for l, s in spare.items()}, dict(reserve)


def requests(*, devices: list[dict], bps: dict[str, dict], inventory: dict[str, dict], pos: dict[str, Any],
             contracts: list[dict], trades: list[dict], incoming: dict[str, dict], fleets: list[dict],
             gather: dict[str, str], busy_stars: set[str], prefs: dict | None = None,
             moved: dict[str, tuple[str, str]] | None = None, home_drop: dict[str, str] | None = None,
             no_consolidate: set[str] | None = None, evacuate: dict[str, str] | None = None,
             upkeep: list[dict] | None = None) -> tuple[list[dict], dict, dict]:
    """(requests by priority, spare per pile, reserve per system). `gather`: gather star → its drop point. `moved`: a
    fleet's old home → (its new home, the drop point there): what's left there follows the fleet. `upkeep`: our active
    hubs' upkeep (wardhub.upkeep_rows: {code, location, target, urgent}): the game draws it from the stockpile at the
    hub's own location (owner 2026-10-25), so that pile is kept and topped up to `target`."""
    moved = moved or {}
    prefs = prefs or {}
    costs = print_costs(devices, bps)
    upkeep = [u for u in upkeep or [] if u.get("location") and u.get("target")]
    held = {l: dict(e) for l, e in costs.items()}
    for u in upkeep:   # a hub's pile is spoken for like a factory's
        e = held.setdefault(u["location"], {"cost": {}})
        e["cost"] = _add(e.get("cost") or {}, u["target"])
    spare, reserve = surplus(inventory, held)
    stock = {l: as_amounts(i) for l, i in inventory.items()}
    out: list[dict] = []

    def have(loc: str) -> dict:
        return _add(stock.get(loc) or {}, incoming.get(loc) or {})
    for loc, e in sorted(costs.items()):
        need = _short(e["cost"], have(loc))
        types = ", ".join(f"{n}× {t.replace('_', ' ')}" for t, n in sorted(e["types"].items()))
        for_stock = e.get("stock_only", False)
        out.append({"id": f"print:{loc}", "kind": "print", "to": loc, "need": need, "price": e["cost"],
                    "tier": 4 if for_stock else 1, "waiting": e["waiting"],
                    "label": f"prints at {loc}: {types}" + (" (keep in stock)" if for_stock else "")})
    for c in contracts:
        out.append({"id": f"contract:{c['designation']}", "kind": "contract", "to": c["location"], "price": c["price"],
                    "need": _short(c["price"], have(c["location"])), "label": f"contract {c.get('title') or c['designation']}",
                    "on_way": _short(c["price"], stock.get(c["location"]) or {}),
                    "designation": c["designation"], "devices_short": c.get("devices_short", False),
                    "fulfilling": c.get("fulfilling", False),
                    "rewards": as_amounts(c.get("rewards") or {}), "reward_devices": c.get("reward_devices") or {}})
    for t in trades:
        out.append({"id": f"trade:{t['key']}", "kind": "trade", "to": t["location"], "price": t["price"],
                    "need": _short(t["price"], have(t["location"])), "label": f"trade {t.get('name') or t['trade_code']}",
                    "on_way": _short(t["price"], stock.get(t["location"]) or {}),
                    "controller": t["controller"], "trade_code": t["trade_code"], "once": t.get("once", False),
                    "times": t.get("times", 1),
                    "devices_short": t.get("devices_short", False),
                    "rewards": as_amounts(t.get("rewards") or {}), "reward_devices": t.get("reward_devices") or {}})
    for u in upkeep:
        need = _short(u["target"], have(u["location"]))
        if need:
            out.append({"id": f"upkeep:{u['code']}", "kind": "upkeep", "to": u["location"], "need": need, "price": u["target"],
                        "tier": 1 if u.get("urgent") else 2, "hub": u["code"],
                        "label": f"hub upkeep for {u['code']} at {u['location']}"})
    for r in out:
        if r["kind"] not in ("print", "upkeep"):   # local when the system itself can spare a needed resource
            here = star_of(r["to"])
            local = any(spare.get(l, {}).get(x, 0) >= 1 for l in spare if star_of(l) == here and l != r["to"] for x in r["need"])
            r["tier"] = 2 if local else 3
    for loc, to in sorted((evacuate or {}).items()):   # an asteroid is due there: its pile goes first (evacuate.py)
        items = stock.get(loc) or {}
        if sum(items.values()) >= 1 and to:
            out.append({"id": f"evacuate:{loc}", "kind": "return", "to": to, "from": loc, "tier": 0,
                        "need": {r: q for r, q in items.items() if q >= 1},
                        "label": f"evacuate the {loc} stockpile to {to} before impact"})
    asked = {r["to"] for r in out} | set(costs) | set(evacuate or {}) | {u["location"] for u in upkeep}
    homes = {f.get("home") for f in fleets if f.get("home") and (f.get("station") or f.get("materials") == "self")}
    # a bootstrap fleet builds from what it mines at its home: its piles stay (live 2026-10-22: Delta Force's ISOLITHOR
    # stock was sent for, 28 874 ly from SOLARES)
    building = {f.get("home") for f in fleets if f.get("home") and f.get("role") == "bootstrap"}
    # systems an AMI ferry (Loadouts › materials) already empties: a return trip there finds the pile gone (live
    # 2026-10-20, ALPHARDAN-BELT-1: six freighters arrived after the ferry's hauler)
    ferried = {star_of(d.get("location")) for d in devices if "transport" in (d.get("device_type") or "")
               and "controller" in (d.get("device_type") or "") and d.get("location")
               and ("ferry" in (d.get("tags") or []) or (d.get("ami_directive") or {}).get("name") == "ferry")}
    for loc, items in sorted(stock.items()):
        total = sum(items.values())
        star = star_of(loc)
        if total < MIN_RETURN or loc in asked or star in busy_stars or star in building:
            continue
        if star in homes:   # a home system's piles gather at its drop point (its autofactory, else its biggest pile)
            to = (home_drop or {}).get(star)
            if not to or to == loc or star in (no_consolidate or set()):
                continue   # already there, or the Consolidate rule's AMI controller has this system
            lacks = {x for r_ in out if r_["kind"] == "print" and star_of(r_["to"]) == star for x in r_["need"]}
            out.append({"id": f"consolidate:{loc}", "kind": "return", "to": to, "from": loc,
                        "need": {r: q for r, q in items.items() if q >= 1}, "tier": 4,
                        "waiting": bool(lacks & set(items)),   # holds what the system's prints lack: first
                        "label": f"consolidate the {loc} stockpile at {to}"})
            continue
        if star in ferried:
            continue
        if star in moved:   # a fleet moved away from here: its stock follows it to the new home
            to_star, to = moved[star]
            label = f"move the {loc} stockpile to {to_star} (its fleet's new home)"
        elif gather and star not in gather:
            to_star = min(gather, key=lambda g: (_dist(star, g, pos), g))
            to, label = gather[to_star], f"return the {loc} stockpile to {to_star}"
        else:   # nowhere to take it: shown on the Logistics page, not silently skipped
            to = None
            label = (f"return the {loc} stockpile: no gather point — set a fleet to take materials in"
                     if not gather else f"the {loc} stockpile is in a gather system already")
            if gather:
                continue
        out.append({"id": f"return:{loc}", "kind": "return", "to": to, "from": loc,
                    "need": {r: q for r, q in items.items() if q >= 1}, "tier": 4, "label": label})
    for r in out:
        r["base_tier"] = r["tier"]
        if r["id"] in (prefs.get("bump") or {}):
            r["tier"] = int(prefs["bump"][r["id"]])
        r["skipped"] = r["id"] in set(prefs.get("skip") or [])
    out.sort(key=lambda r: (r["tier"], not r.get("waiting"), r["id"]))
    _batch_deals(out, stock, incoming)
    return out, spare, reserve


def _batch_deals(out: list[dict], stock: dict, incoming: dict) -> None:
    """Contracts and trades at the same location share one delivery (owner 2026-10-22): the stock there pays the
    first ones in priority order; the first one it can't pay (the head) asks for what all the rest still lack, so one
    trip loads it all (as far as the carriers hold), and its replicant fulfills them all on arrival. The others wait
    for it (`with`)."""
    by_loc: dict[str, list[dict]] = defaultdict(list)
    for r in out:
        if r["kind"] in ("contract", "trade") and not r["skipped"] and not r.get("devices_short"):
            by_loc[r["to"]].append(r)
    for loc, deals in by_loc.items():
        if len(deals) < 2:
            continue
        st, hv = dict(stock.get(loc) or {}), _add(stock.get(loc) or {}, incoming.get(loc) or {})
        rest = []
        for r in deals:   # what's there pays these in turn
            if not rest and not _short(r["price"], st):
                for x, q in as_amounts(r["price"]).items():
                    st[x] = st.get(x, 0.0) - q
                    hv[x] = hv.get(x, 0.0) - q
            else:
                rest.append(r)
        if len(rest) < 2:
            continue
        head, others = rest[0], rest[1:]
        total: dict[str, float] = {}
        for r in rest:
            total = _add(total, as_amounts(r["price"]))
        head["need"], head["on_way"] = _short(total, hv), _short(total, st)
        head["also"] = [{**_deal(r), "req": r["id"]} for r in others]
        head["label"] += f" (+{len(others)} more at {loc})"
        for r in others:
            head["rewards"] = _add(head.get("rewards") or {}, r.get("rewards") or {})
            rd = dict(head.get("reward_devices") or {})
            for t, n in (r.get("reward_devices") or {}).items():
                rd[t] = rd.get(t, 0) + n
            head["reward_devices"] = rd
            r["need"], r["with"] = {}, head["id"]


def category(r: dict) -> str:
    """A request's task category (CATEGORIES). Priority 0 (evacuations, or bumped there) goes before every category."""
    if r.get("tier") == 0:
        return "urgent"
    return {"print": "prints", "contract": "deals", "trade": "deals", "rescue": "rescues", "listing": "shop",
            "repair": "repairs", "upkeep": "upkeep"}.get(r["kind"], "returns")


def _free(d: dict) -> bool:
    st = str(d.get("status") or "").lower()
    return (bool(d.get("location")) and not d.get("location_stale") and not d.get("stowed_in_device_code")
            and not d.get("controller_device_code")   # run by an AMI controller: released first (logistics_pass)
            and not d.get("attached_to_device_code") and not st.startswith(MOVING))


def fleet_devices(fleets: list[dict], devices: list[dict]) -> list[dict]:
    from . import fleets as fl
    out = []
    for f in logistics_fleets(fleets):
        for d in fl.members(f, devices):
            out.append({**d, "_fleet": f})
    return out


def repairs(d: dict) -> bool:
    return "repair" in (d.get("features") or [])


def repair_runs(*, reqs: list[dict], runs: list[dict], unmet: list[dict], used: set[str], free: dict[str, dict],
                pool: list[dict], devices: list[dict], pos: dict[str, Any], commandable: set[str], hosts: dict[str, str],
                reach: float, prefs: dict | None, threshold: float, incoming: dict[str, int],
                evacuate: dict[str, str], targets: set[str] | None = None) -> list[tuple[dict, Any]]:
    """Repair patrols (owner 2026-10-22): the logistics fleets' own repair devices go to the systems with the most wear
    that lack one and patrol there. A system wants one repair device per PATROL_LOAD worn devices (below `threshold`);
    those it has already (any owner's, placed there, or on their way: `incoming`) count. Worst first: the most missing
    capacity, then the lowest device. A repair device leaves only a system that has enough without it, and one at work
    (repairing) stays until it's done. `targets`: the systems a patrol may be sent to (default: all commandable). One that can surge flies itself; the others ride a free logistics carrier (stowed or
    attached). Trips over SECTOR_RANGE_H wait for your OK, like rescues. All of them are kept out of the idle passes:
    a patrolling repair device stays where it works. Mutates reqs, runs, unmet and used; returns (request, work) pairs:
    work() sends what it can for that request with the carriers in `free` at the time of the call."""
    from . import fleets as fl
    from . import upkeep as up
    from .loadouts import can_surge
    mine = {c: d for c, d in free.items() if repairs(d) and c not in used}
    if not any(repairs(d) for d in pool):
        return []
    report = {r["star"]: r for r in up.report(devices, threshold)}
    there: dict[str, list[str]] = defaultdict(list)   # placed repair devices per star
    for d in devices:
        if repairs(d) and up.placed(d) and d.get("location"):
            there[star_of(d["location"])].append(d["device_code"])

    def want(star: str) -> int:
        n = len((report.get(star) or {}).get("damaged") or [])
        return -(-n // PATROL_LOAD)
    spare: dict[str, dict] = {}
    for star, codes in there.items():   # what a system can do without: ours beyond its need, not mid-repair
        extra = len(codes) - want(star)
        for c in sorted((c for c in codes if c in mine), key=lambda c: (up.patrolling(mine[c]), c)):
            if extra <= 0:
                break
            if not up.repairing(mine[c]):
                spare[c] = mine[c]
                extra -= 1
    for c, d in mine.items():   # a repair device of ours staying in a system that needs it: patrolling there
        star = star_of(d.get("location"))
        if c not in spare and want(star) and not up.patrolling(d) and up.placed(d) and star in commandable \
                and "set_directive" in (d.get("available_commands") or ["set_directive"]):
            runs.append({"req": None, "kind": "repair", "cargos": [], "ride": None, "escort": None, "repairer": c,
                         "mode": "stay", "point": d["location"], "to": d["location"], "carry": {}, "back": None,
                         "label": f"{c} patrols {star}"})
    used |= set(mine)
    danger = {star_of(l) for l in evacuate}
    needs = []
    for star, r in report.items():
        short = want(star) - len(there.get(star) or []) - int(incoming.get(star) or 0)
        if short > 0 and star in (commandable if targets is None else targets & commandable) and star not in danger:
            wear = sum(100 - x["capacity"] for x in r["damaged"])
            needs.append((star, short, wear, r))
    needs.sort(key=lambda x: (-x[2], x[3]["min"], x[0]))
    tasks = []

    def work(req: dict, star: str, worst: dict, left: list[int]) -> None:
        rid = req["id"]
        while left[0] > 0:
            best = None   # (ly, repairer, carrier or None, mode)
            for c, d in spare.items():
                here = star_of(d.get("location"))
                if can_surge(d) and here in commandable and _dist(here, star, pos) <= reach:
                    cand = (_dist(here, star, pos), c, "", "self")
                    best = min(best or cand, cand)
                    continue
                for k, e in free.items():
                    there_k = star_of(e.get("location"))
                    if k in used or not can_surge(e) or (here not in commandable and k not in hosts) \
                            or (there_k not in commandable and k not in hosts) or _dist(there_k, here, pos) > reach:
                        continue
                    mode = fl.take_seat(dict(fl.seats(e, devices)), d)
                    if mode:
                        cand = (_dist(there_k, here, pos) + _dist(here, star, pos), c, k, mode)
                        best = min(best or cand, cand)
            if not best:
                if not any(u["req"] == rid for u in unmet):
                    unmet.append({"req": rid, "why": "no spare logistics repair device" if not spare else
                                  f"no free logistics carrier with a free seat {_within(reach, star)} to bring one"})
                return
            _, c, k, mode = best
            d = spare[c]
            legs = [(star_of(d.get("location")), star)]
            if k:
                legs.insert(0, (star_of(free[k].get("location")), star_of(d.get("location"))))
            if _needs_ok(req, _trip_hours(legs, pos), prefs, unmet):
                return
            spare.pop(c)
            left[0] -= 1
            if k:
                used.add(k)
            runs.append({"req": rid, "kind": "repair", "cargos": [], "ride": k or None, "escort": None, "repairer": c, "mode": mode,
                         "point": worst["location"], "to": worst["location"], "carry": {}, "back": None,
                         "patrolling": up.patrolling(d), "label": req["label"]})
    for star, short, wear, r in needs:
        rid = f"repair:{star}"
        worst = r["damaged"][0]
        req = {"id": rid, "kind": "repair", "to": worst["location"], "need": {}, "tier": 7, "base_tier": 7,
               "label": f"repair patrol for {star}: {len(r['damaged'])} worn, worst {worst['code']} at {worst['capacity']:g} %"}
        if rid in ((prefs or {}).get("bump") or {}):
            req["tier"] = int(prefs["bump"][rid])
        req["skipped"] = rid in set((prefs or {}).get("skip") or [])
        reqs.append(req)
        if not req["skipped"]:
            tasks.append((req, lambda req=req, star=star, worst=worst, left=[short]: work(req, star, worst, left)))
    return tasks


def plan(*, fleets: list[dict], devices: list[dict], bps: dict[str, dict], inventory: dict[str, dict],
         pos: dict[str, Any], commandable: set[str], hosts: dict[str, str], busy: set[str], reach: float = math.inf,
         contracts: list[dict], trades: list[dict], incoming: dict[str, dict], replicants: dict[str, dict],
         drop: Any, busy_stars: set[str] | frozenset = frozenset(), prefs: dict | None = None,
         ami: list[dict] | None = None, moved: dict[str, str] | None = None,
         dark_nets: list[set[str]] | None = None, away_fleets: set[str] | frozenset = frozenset(),
         no_consolidate: set[str] | frozenset = frozenset(), evacuate: dict[str, str] | None = None,
         listings: list[dict] | None = None, repair_threshold: float = 80.0,
         repair_to: dict[str, int] | None = None, repair_stars: set[str] | None = None,
         lanes: dict[str, str] | None = None, upkeep: list[dict] | None = None,
         rescue_pause: dict | None = None) -> dict:
    """What the logistics devices do this pass. `hosts`: device → the replicant it hosts. `drop(star)`: where materials land in a
    system. `listings`: shops whose trades you asked for ({controller, location, label}): a logistics device hosting a
    replicant goes there, last of all (the engine reads the trades once a replicant is in the system). `repair_to`: repair
    devices already on their way, per star. `repair_stars`: where repair patrols may go (default: commandable). `lanes`:
    the task category each carrier worked last pass (device → CATEGORIES key). `rescue_pause`: rescue_pauses() (paused
    rescues are listed, not run).
    Returns {"requests", "runs", "unmet", "spare", "reserve", "fleet_devices", "lanes"}."""
    from . import cargo as cg
    from .loadouts import can_surge
    gather = {f["home"]: drop(f["home"]) for f in fleets or [] if f.get("materials") == "self" and f.get("home")}
    reqs, spare, reserve = requests(devices=devices, bps=bps, inventory=inventory, pos=pos, contracts=contracts,
                                    trades=trades, incoming=incoming, fleets=fleets, gather=gather,
                                    busy_stars=set(busy_stars), prefs=prefs,
                                    moved={o: (n, drop(n)) for o, n in (moved or {}).items()},
                                    home_drop={f["home"]: drop(f["home"]) for f in fleets or [] if f.get("home")
                                               and (f.get("station") or f.get("materials") == "self") and f["home"] in commandable},
                                    no_consolidate=set(no_consolidate), evacuate=evacuate, upkeep=upkeep)
    hosts = riding_hosts(devices, hosts)   # a carrier with a replicant's matrix aboard escorts like its host
    # the vessel the replicant's matrix is in travels as the replicant; a carrier towing that vessel flies by its own travel
    # (live 2026-10-20: /replicants/…/travel moved the towed matrix container: "Device does not have surge capability")
    towed = {c: d.get("attached_to_device_code") or d.get("stowed_in_device_code") for d in devices
             if (c := d.get("device_code")) in hosts}
    direct = {c: r for c, r in hosts.items() if not any(towed.get(x) == c and hosts[x] == r for x in hosts)}
    ami_free = {c["device_code"]: c for c in ami or []}
    ami_level: dict[str, dict[str, float]] = {}   # per destination: what AMI deliveries this pass bring it up to
    pool = fleet_devices(fleets, devices)
    bpl = list(bps.values())
    free = {d["device_code"]: d for d in pool if d["device_code"] not in busy and _free(d)}
    runs, unmet, used = [], [], set()
    rep_at = defaultdict(list)
    for code, r in (replicants or {}).items():
        rep_at[r.get("location") or r.get("current_location")].append(code)

    def room(d: dict) -> float:
        cap = cg.capacity_of(d, bpl, None) or 0.0
        return max(0.0, float(cap) - float(d.get("cargo_used") or 0))

    def held(d: dict) -> dict[str, float]:
        return cg.used_of(d, None)[1]

    def no_cargo_why(first: str, fstar: str, dest: str, same: bool) -> str:
        """Why no cargo device could take a request: counts of the logistics cargo devices that are on jobs, moving or
        taken by a higher request this pass, and why the nearest free one was passed over."""
        carriers = [d for d in pool if (cg.capacity_of(d, bpl, None) or 0) > 0]
        head = f"no logistics cargo device free to load at {first} for {dest}"
        if not carriers:
            return f"{head}: your logistics fleets have no cargo devices (freighter, cargo vessel or hauler)"
        on_job = [d for d in carriers if d["device_code"] in busy]
        away = [d for d in carriers if d["device_code"] not in busy and d["device_code"] not in free]
        taken = [d for d in carriers if d["device_code"] in used]
        parts = [f"{len(on_job)} on jobs" if on_job else "", f"{len(away)} moving, stowed or attached" if away else "",
                 f"{len(taken)} taken by higher requests this pass" if taken else ""]
        rest = [d for d in carriers if d["device_code"] in free and d["device_code"] not in used]
        out = f"{head} ({len(carriers)} cargo devices: " + ", ".join(x for x in parts if x) + ")" if any(parts) else head
        if rest:
            d = min(rest, key=lambda d: (_dist(star_of(d.get("location")), fstar, pos), d["device_code"]))
            at, ly = star_of(d.get("location")), _dist(star_of(d.get("location")), fstar, pos)
            if room(d) < 1 and not held(d):
                why = "its hold is full"
            elif at not in commandable and d["device_code"] not in hosts:
                why = f"it is at {at}, outside the relay network"
            elif ly > reach:
                why = f"it is {ly:.0f} ly away, beyond the {reach:g} ly reach"
            elif not can_surge(d):
                why = f"it can't surge and is at {at}" + ("" if same else ", with no free carrier there to tow it")
            else:
                why = "nothing else fits"
            out += f"; the nearest free one, {d['device_code']} ({(d.get('device_type') or '').replace('_', ' ')}): {why}"
        return out

    def no_escort_why(star: str) -> str:
        """Why no logistics device hosting a replicant could go to `star`: each one and what keeps it."""
        mine = [d for d in pool if d["device_code"] in hosts]
        head = f"no free logistics device hosting a replicant to reach {star} or its relay network"
        if not mine:
            return f"{head}: none of your logistics fleets' devices has a replicant aboard"
        notes = []
        for d in sorted(mine, key=lambda d: (_dist(star_of(d.get("location")), star, pos), d["device_code"]))[:4]:
            c, ly = d["device_code"], _dist(star_of(d.get("location")), star, pos)
            why = ("on a job" if c in busy else "taken by a higher request this pass" if c in used
                   else "moving, stowed or attached" if c not in free else "can't surge" if not can_surge(d)
                   else f"{ly:.0f} ly away, beyond the {reach:g} ly reach" if ly > reach else "free")
            notes.append(f"{c} ({(d.get('device_type') or '').replace('_', ' ')}): {why}")
        return f"{head} — " + "; ".join(notes)

    def attach_free(d: dict) -> int:
        from . import fleets as fl
        return fl.seats(d, devices)["attach"]   # attach_used (v3.5.1) or what our list shows attached

    def wait_point(d: dict, near: str) -> str | None:
        """Where a logistics device waits after a trip that ended outside the network: its fleet's home if covered, else the
        nearest covered system."""
        home = (d.get("_fleet") or {}).get("home")
        if home and home in commandable:
            return drop(home)
        cands = [s for s in commandable if pos.get(s)]
        return drop(min(cands, key=lambda s: (_dist(near, s, pos), s))) if cands else None

    def escort_for(stops: list[str], exclude: set[str]) -> dict | None:
        first = star_of(stops[0])
        one_system = len({star_of(s) for s in stops}) == 1
        cands = [d for c, d in free.items() if c not in used and c not in exclude and c in hosts
                 and (can_surge(d) or (one_system and star_of(d.get("location")) == first))
                 and (_dist(star_of(d.get("location")), first, pos) <= reach or star_of(d.get("location")) == first)]
        return min(cands, key=lambda d: (_dist(star_of(d.get("location")), first, pos), d["device_code"]), default=None)

    def ready_replicant(r: dict, escort: dict | None, cargo: dict | None) -> str | None:
        for d in (escort, cargo):
            if d and d["device_code"] in hosts:
                return hosts[d["device_code"]]
        mine = rep_at.get(r["to"]) or []
        return mine[0] if mine else None

    # a home system's reserve only keeps its stock from leaving the system: moving it to that system's own printing
    # factory is what it's for, so a print request may draw on the reserve in its own system (returned afterwards)
    kept = {l: {x: q - spare.get(l, {}).get(x, 0.0) for x, q in as_amounts(items).items() if q - spare.get(l, {}).get(x, 0.0) >= 1}
            for l, items in inventory.items()}
    released: dict[tuple[str, str], float] = {}
    shares = balance(queue_items(devices, bps), kept, spare, incoming)   # hubs keep their queues equally busy

    def give_back() -> None:
        for (l, x), q in released.items():
            back = min(q, spare.get(l, {}).get(x, 0.0))
            if back > 0:
                spare[l][x] -= back
                kept.setdefault(l, {})[x] = kept.get(l, {}).get(x, 0.0) + back
        released.clear()
    def deliver(r: dict) -> None:
        if r["skipped"]:
            return
        left = r.get("_left")   # a second try (with every free carrier) takes on what the first left
        need = {x: q for x, q in (r["need"] if left is None else left).items() if q >= 1}
        if left is not None and not need:
            return
        dest, dstar = r["to"], star_of(r["to"])
        if r["kind"] in ("print", "upkeep") and dest:
            for l, h in kept.items():
                if star_of(l) == dstar and l != dest:
                    for x, q in h.items():
                        spare.setdefault(l, {})[x] = spare.get(l, {}).get(x, 0.0) + q
                        released[(l, x)] = released.get((l, x), 0.0) + q
                    kept[l] = {}
            for (l, x), q in (shares.get(dest) or {}).items() if r["kind"] == "print" else ():   # what another hub shares with this one's queue
                q = min(q, kept.get(l, {}).get(x, 0.0))
                if q > 0:
                    kept[l][x] -= q
                    spare.setdefault(l, {})[x] = spare.get(l, {}).get(x, 0.0) + q
                    released[(l, x)] = released.get((l, x), 0.0) + q
        if not dest:
            unmet.append({"req": r["id"], "why": "no gather point: give a fleet 'takes materials in' (Fleets) to bring it back"})
            return
        if not need:
            if r.get("with"):   # delivered and fulfilled together with the first deal at its location
                head = next((h for h in reqs if h["id"] == r["with"]), None)
                unmet.append({"req": r["id"], "why": f"delivered together with {head['label'] if head else r['with']}"})
            elif r["kind"] in ("contract", "trade") and r.get("devices_short"):
                unmet.append({"req": r["id"], "why": f"materials are there; waiting for the devices it asks for at {dest} "
                                                     "(delivered by the contract's device supply)"})
            elif r["kind"] in ("contract", "trade") and {x: q for x, q in (r.get("on_way") or {}).items() if q >= 1}:
                # covered only with deliveries still on their way: fulfilling now fails "Event criteria not met"
                # (live 2026-10-20, Construction Boom at PERHALIEL-4 retried every pass with 0 of 400 there)
                way = ", ".join(f"{q:g} {x}" for x, q in sorted(r["on_way"].items()) if q >= 1)
                unmet.append({"req": r["id"], "why": f"waiting for {way} on its way to {dest}"})
            elif r.get("fulfilling"):   # a replicant is already on its way to fulfill it
                unmet.append({"req": r["id"], "why": "a logistics job is on its way to fulfill it"})
            elif r["kind"] in ("contract", "trade"):   # stocked: a replicant fulfills it
                rep = ready_replicant(r, None, None)
                esc = None
                if not rep:
                    esc = escort_for([dest], set())
                    if not esc:
                        unmet.append({"req": r["id"], "why": f"{dest} holds the price; waiting for a free logistics device hosting a replicant to fulfill it"})
                        return
                    used.add(esc["device_code"])
                    rep = hosts[esc["device_code"]]
                runs.append({"req": r["id"], "kind": r["kind"], "cargos": [], "escort": esc and esc["device_code"],
                             "legs": [], "to": dest, "carry": {}, "fulfil": _deal(r), "also": r.get("also") or [], "replicant": rep,
                             "back": wait_point(esc, dstar) if esc and dstar not in commandable else None, "label": r["label"]})
            return
        def can_escort() -> bool:
            return any(c not in used and c in hosts and can_surge(d) for c, d in free.items())

        def sources_ok(l: str) -> bool:
            if r["kind"] == "return":
                return l == r.get("from")
            # piles beyond the network count while a replicant is free to go along
            return l != dest and (star_of(l) in commandable or can_escort())
        assigned = bool(r.get("_assigned"))
        while need:
            piles = [l for l, s in spare.items() if sources_ok(l) and any(s.get(x, 0) >= 1 for x in need)]
            # a system that can spare all of it first (the destination's own system before any): the rest comes from
            # farther anyway, so a near sliver only adds a trip (live 2026-10-28: three freighters for one Geothermal
            # Surplus contract, two of them for 19 and 10 units from KELCERUM-BELT-1)
            has = defaultdict(lambda: defaultdict(float))
            for l in piles:
                for x in need:
                    has[star_of(l)][x] += spare[l].get(x, 0.0)
            covers = {st_: all(h[x] >= q for x, q in need.items()) for st_, h in has.items()}
            piles.sort(key=lambda l: (star_of(l) != dstar, not covers[star_of(l)], _dist(star_of(l), dstar, pos),
                                      -sum(spare[l].get(x, 0) for x in need), l))
            if not piles:
                if not assigned:
                    unmet.append({"req": r["id"], "why": "no stockpile can spare " + ", ".join(f"{q:g} {x}" for x, q in sorted(need.items()))
                                  + " (home systems keep what their own prints need)"})
                break
            first = piles[0]
            fstar = star_of(first)
            same = fstar == dstar
            ctrl = next((c for c in sorted(ami_free) if star_of(ami_free[c].get("location")) == dstar), None) \
                if same and dstar in commandable else None
            if ctrl:   # in-system: the system's free AMI transport controller takes it (one pile per controller)
                take = {x: math.floor(min(q, spare[first].get(x, 0.0))) for x, q in need.items()}
                take = {x: q for x, q in take.items() if q >= 1}
                if take:
                    ami_free.pop(ctrl)
                    for x, q in take.items():
                        spare[first][x] -= q
                        need[x] -= q
                    need = {x: q for x, q in need.items() if q >= 1}
                    assigned = True
                    # the game's `requirement` is what the destination should hold ("deliver until"): what's there,
                    # plus what this pass already sent there, plus this take (live 2026-10-28: SOLARES-4-L4 held
                    # 69/158 and a requirement of 57/130 completed at once, moving nothing, every 5 min for hours)
                    at = ami_level.setdefault(dest, dict(as_amounts(inventory.get(dest) or {})))
                    for x, q in take.items():
                        at[x] = at.get(x, 0.0) + q
                    runs.append({"req": r["id"], "kind": r["kind"], "ami": ctrl, "cargos": [], "escort": None,
                                 "require": {x: math.ceil(at[x] - 1e-9) for x in take},
                                 "legs": [{"pile": first, "take": take}], "to": dest, "carry": take, "fulfil": None,
                                 "replicant": None, "back": None, "label": r["label"]})
                    continue
            # one run per source system: cargo devices load there, travel together (riding a carrier when one is free
            # for a long trip) and share one escort; the carrier also fetches haulers and drones from another system
            # (owner 2026-10-27)
            ride = None
            if not same:
                rides = [d for c, d in free.items() if c not in used and can_surge(d) and attach_free(d) >= 1
                         and (star_of(d.get("location")) in commandable or c in hosts)
                         and (star_of(d.get("location")) == fstar or _dist(star_of(d.get("location")), fstar, pos) <= reach)]
                ride = min(rides, key=lambda d: (_dist(star_of(d.get("location")), fstar, pos) > 0,
                                                 round(_dist(star_of(d.get("location")), fstar, pos) / RELAY_HOP),
                                                 -attach_free(d), _dist(star_of(d.get("location")), fstar, pos),
                                                 d["device_code"]), default=None)
            seats = attach_free(ride) if ride else 0
            group, taken = [], {}
            while need:
                left = sum(min(q, sum(spare[l].get(x, 0.0) for l in piles if star_of(l) == fstar)) for x, q in need.items())
                if left < 1:
                    break
                cands = [d for c, d in free.items() if c not in used and c != (ride or {}).get("device_code")
                         and (room(d) >= 1 or any(held(d).get(x, 0.0) >= 1 for x in need))
                         and (can_surge(d) or (star_of(d.get("location")) == fstar and fstar in commandable
                                               and (same or (ride and seats >= 1)))
                              or (ride and seats >= 1 and star_of(d.get("location")) != fstar))
                         and (star_of(d.get("location")) in commandable or c in hosts)
                         and (star_of(d.get("location")) == fstar or _dist(star_of(d.get("location")), fstar, pos) <= reach)]
                if not cands:
                    break
                # one already holding what's needed first (e.g. loaded on a trip that failed), then nearest (by relay hop),
                # then the smallest that takes the rest in one go, else the biggest
                cargo = min(cands, key=lambda d: (-sum(min(q, held(d).get(x, 0.0)) for x, q in need.items()),
                                                  round(_dist(star_of(d.get("location")), fstar, pos) / RELAY_HOP),
                                                  room(d) < left, room(d) if room(d) >= left else -room(d), d["device_code"]))
                code = cargo["device_code"]
                cap, legs, carry = room(cargo), [], {}
                for x, h in held(cargo).items():   # what it already carries counts first: nothing to pick up for that
                    q = math.floor(min(need.get(x, 0.0), h))
                    if q >= 1:
                        need[x] -= q
                        carry[x] = carry.get(x, 0) + q
                        taken[x] = taken.get(x, 0) + q
                need = {x: q for x, q in need.items() if q >= 1}
                for l in piles:
                    if star_of(l) != fstar or cap < 1:
                        continue
                    take = {}
                    for x in sorted(need, key=lambda x: -need[x]):
                        q = math.floor(min(need[x], spare[l].get(x, 0.0), cap))
                        if q >= 1:
                            take[x] = q
                            cap -= q
                    if take:
                        legs.append({"pile": l, "take": take})
                        for x, q in take.items():
                            spare[l][x] -= q
                            need[x] -= q
                            carry[x] = carry.get(x, 0) + q
                            taken[x] = taken.get(x, 0) + q
                        need = {x: q for x, q in need.items() if q >= 1}
                if not carry:
                    break
                used.add(code)
                # haulers and drones ride; freighters fly themselves ("Cannot attach a large device", live 2026-10-12)
                rides_it = bool(ride) and seats >= 1 and not can_surge(cargo)
                if rides_it:
                    seats -= 1
                group.append({"code": code, "legs": legs, "carry": carry, "rides": rides_it, "dev": cargo, "room": room(cargo)})
            if not group:
                if not assigned:
                    unmet.append({"req": r["id"], "why": no_cargo_why(first, fstar, dest, same)})
                break
            if ride and not any(g["rides"] for g in group):
                ride = None
            hold = sum(g["room"] for g in group)
            far = _dist(fstar, dstar, pos) > LONG_HAUL_LY
            if r["kind"] == "print" and not same and need and hold and sum(taken.values()) < MIN_LOAD * hold \
                    and (far or not r.get("waiting")):
                # a part load for a print queue: more was wanted but this is all that can be spared now — wait and load
                # more on a later pass instead of sending a near-empty hold (a long haul even when a print waits)
                for g in group:
                    used.discard(g["code"])
                    for g2 in g["legs"]:
                        for x, q in g2["take"].items():
                            spare[g2["pile"]][x] += q
                for x, q in taken.items():
                    need[x] = need.get(x, 0.0) + q
                unmet.append({"req": r["id"], "why": f"waiting for a fuller load: {sum(taken.values()):g} of {hold:g} units can be "
                                                     f"spared from {fstar} now (a trip leaves once it's {MIN_LOAD:.0%} full)"})
                break
            stops = sorted({g2["pile"] for g in group for g2 in g["legs"]} | {g["dev"].get("location") for g in group if not g["legs"]}) + [dest]
            locs = [g["dev"].get("location") for g in group] + [(ride or {}).get("location")]
            outside = [x for x in stops + locs if x and star_of(x) not in commandable]
            esc = None
            members = [g["code"] for g in group] + ([ride["device_code"]] if ride else [])
            if outside and not any(c in hosts for c in members):
                esc = escort_for(stops, set(members))
                if not esc:
                    for g in group:   # undo: nothing goes unaccompanied
                        used.discard(g["code"])
                        for g2 in g["legs"]:
                            for x, q in g2["take"].items():
                                spare[g2["pile"]][x] += q
                    for x, q in taken.items():
                        need[x] = need.get(x, 0.0) + q
                    unmet.append({"req": r["id"], "why": f"{', '.join(sorted({star_of(x) for x in outside}))} is outside an active "
                                                         "relay network: it needs a logistics device hosting a replicant alongside (none free)"})
                    break
                used.add(esc["device_code"])
            if ride:
                used.add(ride["device_code"])
            assigned = True
            done = not need
            host = next((by_c for by_c in [esc, ride] + [g["dev"] for g in group] if by_c and by_c["device_code"] in hosts), None)
            rep = ready_replicant(r, host, None) if done and r["kind"] in ("contract", "trade") and not r.get("devices_short") else None
            back = wait_point(ride or group[0]["dev"], dstar) if dstar not in commandable else None
            # what the deal pays out: the convoy loads it there and then and takes it to the nearest gather point
            rewards = {x: q for x, q in (r.get("rewards") or {}).items() if q >= 1} if rep else {}
            home = None
            if rewards and gather:
                gstar = min(gather, key=lambda g_: (_dist(dstar, g_, pos), g_))
                home = gather[gstar] if gather[gstar] != dest else None
            runs.append({"req": r["id"], "kind": r["kind"],
                         "cargos": [{k: g[k] for k in ("code", "legs", "carry", "rides", "room")} for g in group],
                         "ride": ride and ride["device_code"], "escort": esc and esc["device_code"], "to": dest,
                         "carry": taken, "fulfil": _deal(r) if rep else None, "also": (r.get("also") or []) if rep else [],
                         "replicant": rep, "back": back, "label": r["label"],
                         "escort_pickup": any(star_of(x) not in commandable for x in stops[:-1]),
                         "rewards": rewards if home else {}, "home": home,
                         "reward_devices": (r.get("reward_devices") or {}) if rep else {}})
        r["_left"], r["_assigned"] = need, assigned
    # rescues: your devices out of command range (beyond an active relay network, no replicant with them), last tier.
    # A device that can surge home is reached by a replicant (in its system, or anywhere its own dark relay network
    # covers, nearest first) and commanded home; one that can't is picked up by a carrier, with a replicant along.
    from . import fleets as fl
    from .ami_schedule import in_transit
    from .loadouts import at_tag, for_contract, placed
    fleet_home = {fl.fleet_tag(f["id"]): f.get("home") for f in fleets or [] if f.get("id")}
    rescue_reqs = []
    paused_tags = {fl.fleet_tag(f): e for f, e in ((rescue_pause or {}).get("fleets") or {}).items()}
    for d in devices:
        code, loc = d.get("device_code"), d.get("location")
        tag = next((t for t in d.get("tags") or [] if t.startswith("fleet:")), None)
        if (not loc or code in hosts or code in busy or code in used or placed(d) or (d.get("device_type") or "") in KEPT_OUT
                or at_tag(loc) in (d.get("tags") or [])    # stocked there on purpose (an at: tag for where it is)
                or d.get("stowed_in_device_code") or d.get("attached_to_device_code") or tag in away_fleets
                or any(t.startswith("boot:") for t in d.get("tags") or [])   # a bootstrap's own: out there on purpose
                or for_contract(d) or in_transit(d)   # reserved: a contract's, or on its way (to:) to another system
                or str(d.get("status") or "").lower().startswith(MOVING)):
            continue
        out_of_range = d.get("in_control_range") is False or (d.get("in_control_range") is None and star_of(loc) not in commandable)
        if not out_of_range:
            continue
        rid = f"rescue:{code}"
        req = {"id": rid, "kind": "rescue", "to": loc, "need": {}, "tier": 5, "base_tier": 5, "device": code,
               "label": f"rescue {code} ({(d.get('device_type') or '').replace('_', ' ')}) stranded at {loc}"}
        if rid in ((prefs or {}).get("bump") or {}):
            req["tier"] = int(prefs["bump"][rid])
        req["skipped"] = rid in set((prefs or {}).get("skip") or [])
        pause = (rescue_pause or {}).get("all") or paused_tags.get(tag or "")
        if pause:
            req["skipped"] = True
            req["paused"] = "all rescues" if (rescue_pause or {}).get("all") else "its fleet"
            req["paused_until"] = pause.get("until")
        rescue_reqs.append((req, d, fleet_home.get(tag)))
    reqs.extend(r for r, _, _ in rescue_reqs)
    def rescue(r: dict, d: dict, home: str | None) -> None:
        if r["skipped"]:
            return
        code, loc, star = d["device_code"], d["location"], star_of(d["location"])
        if code in used:   # taken as a carrier this pass
            return
        if not (home and home in commandable):   # fleetless (or its home is dark): the nearest covered home system
            homes = [f["home"] for f in fleets or [] if f.get("home") in commandable and (f.get("station") or f.get("materials") == "self")]
            home = min(homes, key=lambda h: (_dist(star, h, pos), h), default=None)
        home_loc = drop(home) if home else wait_point({"_fleet": {}}, star)
        if not home_loc:
            unmet.append({"req": r["id"], "why": "no system inside an active relay network to bring it to"})
            return
        if can_surge(d) and "travel" in (d.get("available_commands") or ["travel"]):
            # a replicant where it can command it: its own system, or any system its dark network covers
            spots = sorted({star} | next((n for n in dark_nets or [] if star in n), set()))
            best = None
            for c, e in free.items():
                if c in used or c not in hosts or not can_surge(e):
                    continue
                here = star_of(e.get("location"))
                for sp in spots:
                    dd = _dist(here, sp, pos)
                    if dd <= reach and (best is None or dd < best[0]):
                        best = (dd, e, sp)
            if not best:
                unmet.append({"req": r["id"], "why": no_escort_why(star)})
                return
            _, esc, sp = best
            point = loc if sp == star else drop(sp)
            back = wait_point(esc, sp) if star_of(point) not in commandable else None
            hours = _trip_hours([(star_of(esc.get("location")), sp), (sp, star_of(home_loc)), (sp, star_of(back) if back else sp)], pos)
            if _needs_ok(r, hours, prefs, unmet):
                return
            used.add(esc["device_code"])
            runs.append({"req": r["id"], "kind": "rescue", "cargos": [], "ride": None, "escort": esc["device_code"],
                         "rescued": code, "mode": "self", "point": point, "to": home_loc, "carry": {},
                         "back": back, "label": r["label"]})
            return
        # it can't fly home: a carrier picks it up (stowed or attached; large devices are compacted first)
        cands = []
        for c, e in free.items():
            if c in used or not can_surge(e) or not (star_of(e.get("location")) == star or _dist(star_of(e.get("location")), star, pos) <= reach):
                continue
            if star_of(e.get("location")) not in commandable and c not in hosts:
                continue
            mode = fl.take_seat(dict(fl.seats(e, devices)), d)
            if mode:
                cands.append((c in hosts, _dist(star_of(e.get("location")), star, pos), c, e, mode))
        if not cands:
            unmet.append({"req": r["id"], "why": f"no free logistics carrier with a free seat {_within(reach, star)}"})
            return
        cands.sort(key=lambda x: (not x[0], x[1], x[2]))   # one hosting a replicant needs no escort
        _, _, c, carrier, mode = cands[0]
        if _needs_ok(r, _trip_hours([(star_of(carrier.get("location")), star), (star, star_of(home_loc))], pos), prefs, unmet):
            return
        esc = None
        if c not in hosts:
            esc = escort_for([loc, home_loc], {c})
            if not esc:
                unmet.append({"req": r["id"], "why": f"{star} is outside an active relay network: the pick-up needs a logistics "
                                                     "device hosting a replicant alongside (none free)"})
                return
            used.add(esc["device_code"])
        used.add(c)
        runs.append({"req": r["id"], "kind": "rescue", "cargos": [], "ride": c, "escort": esc and esc["device_code"],
                     "rescued": code, "mode": mode, "point": loc, "to": home_loc, "carry": {}, "back": None, "label": r["label"]})

    # shop listings you asked for: a replicant to the shop's system, lowest priority (owner, 2026-10-20)
    listing_tasks = []
    for li in listings or []:
        rid = f"listing:{li['controller']}"
        req = {"id": rid, "kind": "listing", "to": li["location"], "need": {}, "tier": 6, "base_tier": 6,
               "controller": li["controller"], "label": li["label"]}
        if rid in ((prefs or {}).get("bump") or {}):
            req["tier"] = int(prefs["bump"][rid])
        req["skipped"] = rid in set((prefs or {}).get("skip") or [])
        reqs.append(req)
        listing_tasks.append((req, li))

    def shop(req: dict, li: dict) -> None:
        rid = req["id"]
        star = star_of(li["location"])
        best = None
        for c, e in free.items():
            if c in used or c not in hosts or not can_surge(e):
                continue
            dd = _dist(star_of(e.get("location")), star, pos)
            if dd <= reach and (best is None or (dd, c) < best[:2]):
                best = (dd, c, e)
        if not best:
            unmet.append({"req": rid, "why": f"no free logistics device hosting a replicant {_within(reach, star)}"})
            return
        used.add(best[1])
        runs.append({"req": rid, "kind": "listing", "cargos": [], "escort": best[1], "point": li["location"],
                     "to": li["location"], "carry": {}, "controller": li["controller"], "label": li["label"]})

    repair_tasks = repair_runs(reqs=reqs, runs=runs, unmet=unmet, used=used, free=free, pool=pool, devices=devices, pos=pos,
                               commandable=commandable, hosts=hosts, reach=reach, prefs=prefs, threshold=repair_threshold,
                               incoming=repair_to or {}, evacuate=evacuate or {}, targets=repair_stars)

    # Task categories (owner 2026-10-23): every free carrier is given one category (LANES) that has work, and keeps it
    # pass after pass until that category runs dry; a category with work and no carrier takes one from the biggest.
    # First evacuations (priority 0) with every free carrier, then each category with its own carriers, then whatever
    # is left over with every carrier still free (categories in turn, stockpile returns last).
    tasks = [(r, lambda r=r: deliver(r)) for r in reqs if r["kind"] in ("print", "contract", "trade", "return", "upkeep")]
    tasks += [(r, lambda r=r, d=d, h=h: rescue(r, d, h)) for r, d, h in sorted(rescue_reqs, key=lambda x: (x[0]["tier"], x[0]["id"]))]
    tasks += [(r, lambda r=r, li=li: shop(r, li)) for r, li in listing_tasks]
    tasks += repair_tasks
    for r in reqs:
        r["category"] = category(r)
    tasks = sorted(((r, fn) for r, fn in tasks if not r["skipped"]), key=lambda t: t[0]["tier"])
    every = dict(free)
    prev = lanes or {}
    pending = {r["category"] for r, _ in tasks} & set(LANES)

    def fits(d: dict, cat: str) -> bool:
        c, s = d["device_code"], fl.seats(d, devices)
        if cat in ("prints", "deals", "upkeep"):   # cargo, a ride for haulers, or an escort
            return room(d) >= 1 or bool(held(d)) or c in hosts or (can_surge(d) and s["attach"] > 0)
        if cat == "shop":
            return can_surge(d) and c in hosts
        return can_surge(d) and (s["hold"] + s["attach"] > 0 or (cat == "rescues" and c in hosts))
    count: dict[str, int] = defaultdict(int)
    for d in pool:   # carriers on a job still belong to their category
        if d["device_code"] not in every and prev.get(d["device_code"]) in pending:
            count[prev[d["device_code"]]] += 1
    lane: dict[str, str] = {}
    open_ = sorted(c for c in every if c not in used)
    for c in open_:
        if prev.get(c) in pending and fits(every[c], prev[c]):
            lane[c] = prev[c]
            count[lane[c]] += 1
    for c in open_:
        opts = [k for k in LANES if k in pending and fits(every[c], k)]
        if c not in lane and opts:
            lane[c] = min(opts, key=lambda k: (count[k], LANES.index(k)))
            count[lane[c]] += 1
    for k in LANES:   # a category with work and no carrier at all takes one from the biggest
        donors = [c for c in lane if k in pending and not count[k] and count[lane[c]] > 1 and fits(every[c], k)]
        if donors:
            c = min(donors, key=lambda c: (-count[lane[c]], c))
            count[lane[c]] -= 1
            lane[c] = k
            count[k] += 1

    def view(cat: str | None) -> None:   # the carriers a category may take: `free` is what every helper looks at
        free.clear()
        free.update({c: d for c, d in every.items() if cat is None or lane.get(c) == cat})
    tried: set[str] = set()

    def attempt(r: dict, fn: Any) -> None:
        unmet[:] = [u for u in unmet if u["req"] != r["id"]]   # a second try replaces the first one's reasons
        tried.add(r["id"])
        fn()
        give_back()
    view(None)
    for r, fn in tasks:
        if r["category"] == "urgent":
            attempt(r, fn)
    cats = sorted({r["category"] for r, _ in tasks} - {"urgent"},
                  key=lambda k: (k == "returns", min(r["tier"] for r, _ in tasks if r["category"] == k),
                                 (LANES + ("returns",)).index(k)))
    for k in cats:
        if k in LANES:
            view(k)
            for r, fn in tasks:
                if r["category"] == k:
                    attempt(r, fn)
    view(None)
    for k in cats:
        for r, fn in tasks:
            if r["category"] == k and (r["id"] not in tried or any(u["req"] == r["id"] for u in unmet)):
                attempt(r, fn)
    by_id = {r["id"]: r for r in reqs}
    worked = {c: prev[c] for c in prev if c in {d["device_code"] for d in pool} and c not in every}   # still on a job
    worked.update(lane)
    for run in runs:   # a carrier that took another category's request in the leftover round works that one now
        k = (by_id.get(run.get("req")) or {}).get("category")
        if k in LANES:
            for c in run_devices(run):
                if c in every and c not in (run.get("rescued"), run.get("repairer")):
                    worked[c] = k
    for r in reqs:
        r.pop("_left", None)
        r.pop("_assigned", None)

    # logistics devices hosting a replicant idle outside the network come back to wait in it (the rest can't be commanded there)
    for c, d in free.items():
        if c in used or star_of(d.get("location")) in commandable or c not in hosts or not can_surge(d):
            continue
        w = wait_point(d, star_of(d.get("location")))
        if w:
            runs.append({"req": None, "kind": "reposition", "cargos": [], "escort": c, "legs": [], "to": w, "carry": {},
                         "fulfil": None, "replicant": None, "back": None, "label": f"{c} back to wait in the relay network"})
    # idle logistics carriers dump what doesn't belong to their work at the nearest receiving point (owner 2026-10-20):
    # leftover cargo is deposited, and devices aboard that aren't theirs are let off
    from . import fleets as fl
    receive = {**gather, **{star_of(d.get("location")): drop(star_of(d.get("location"))) for d in devices
                            if d.get("device_type") == "system_hub" and d.get("location")
                            and str(d.get("status") or "").startswith("relaying")}}
    docks = sorted(s for s in receive if s in commandable and receive[s])
    own = {d["device_code"] for d in pool}
    tag_of = {e.get("device_code"): next((t for t in e.get("tags") or [] if str(t).startswith("fleet:")), None)
              for e in devices}
    for c, d in sorted(free.items()):
        here = star_of(d.get("location"))
        if c in used or here not in commandable:
            continue
        cargo = {r: q for r, q in as_amounts(d.get("cargo")).items() if q > 0}
        extra = stray_aboard(d, devices, own, busy)
        # another fleet's members aboard go to their fleet's home, never to a drop-off point (owner 2026-10-21); one
        # home per trip, the nearest (a tag of a fleet that's gone counts as none); members of a fleet away on a mission or with a dark home stay aboard for now
        homes: dict[str, list[dict]] = {}
        for x in [x for x in extra if fleet_home.get(tag_of.get(x["code"]) or "")]:
            extra.remove(x)
            tag = tag_of[x["code"]]
            h = fleet_home.get(tag)
            if h and h in commandable and tag not in away_fleets:
                homes.setdefault(h, []).append(x)
        if homes:
            h = min(homes, key=lambda h: (_dist(here, h, pos), h))
            used.add(c)
            runs.append({"req": None, "kind": "dump", "cargos": [], "escort": c, "legs": [], "to": drop(h),
                         "carry": cargo if h in receive else {}, "dump": homes[h], "fulfil": None, "replicant": None,
                         "back": None, "label": f"{c} brings {', '.join(x['code'] for x in homes[h])} home to {h}"})
            continue
        if (not cargo and not extra) or not docks:
            continue
        to_star = min(docks, key=lambda h: (_dist(here, h, pos), h))
        used.add(c)
        runs.append({"req": None, "kind": "dump", "cargos": [], "escort": c, "legs": [], "to": receive[to_star],
                     "carry": cargo, "dump": extra, "fulfil": None, "replicant": None, "back": None,
                     "label": f"{c} drops off what isn't its work at {to_star}"})
    # the way back (owner 2026-10-28: "a lower priority run would be better than a wasted trip"): a delivery's ships
    # would fly home empty, so they load a stray pile near the drop-off and bring it to the nearest printing hub
    backhaul(runs, spare, gather, reqs, pool, commandable, pos, bpl)
    # the rest of the idle logistics devices gather at the nearest printing hub (a fleet that takes materials in, inside
    # the network; owner's request 2026-10-17): those that can surge fly there, the others are towed by a free carrier
    hubs = sorted(s for s in gather if s in commandable and gather[s])
    for c, d in sorted(free.items()):
        here = star_of(d.get("location"))
        if not hubs or c in used or here not in commandable or here in gather or fl.stationed(d.get("_fleet") or {}):
            continue   # a stationed logistics fleet is kept at its own home by the loadout pass: it stays there
        to_star = min(hubs, key=lambda h: (_dist(here, h, pos), h))
        to = gather[to_star]
        label = f"{c} idle: gathers at the printing hub in {to_star}"
        if can_surge(d) and "travel" in (d.get("available_commands") or ["travel"]):
            used.add(c)
            runs.append({"req": None, "kind": "reposition", "cargos": [], "escort": c, "legs": [], "to": to, "carry": {},
                         "fulfil": None, "replicant": None, "back": None, "label": label})
            continue
        cands = []
        for k, e in free.items():
            there = star_of(e.get("location"))
            if k == c or k in used or not can_surge(e) or there not in commandable \
                    or not (there == here or _dist(there, here, pos) <= reach):
                continue
            mode = fl.take_seat(dict(fl.seats(e, devices)), d)
            if mode:
                cands.append((there != here, _dist(there, here, pos), k, mode))
        if not cands:
            continue   # nothing free to tow it: it waits where it is
        _, _, k, mode = min(cands)
        used |= {c, k}
        runs.append({"req": None, "kind": "rescue", "cargos": [], "ride": k, "escort": None, "rescued": c, "mode": mode,
                     "point": d["location"], "to": to, "carry": {}, "back": None, "label": label})
    for run in runs:
        run["direct"] = {c: direct[c] for c in run_devices(run) if c in direct}
    return {"requests": reqs, "runs": runs, "unmet": unmet, "reserve": reserve,
            "fleet_devices": [{"code": d["device_code"], "type": d.get("device_type"), "fleet": d["_fleet"].get("name"),
                       "location": d.get("location"), "status": d.get("status"), "free": d["device_code"] in free,
                       "replicant": hosts.get(d["device_code"]), "covered": star_of(d.get("location")) in commandable,
                       "lane": worked.get(d["device_code"])}
                      for d in pool], "lanes": worked}


BACKHAUL_KINDS = ("print", "contract", "trade", "upkeep")


def backhaul(runs: list[dict], spare: dict[str, dict[str, float]], gather: dict[str, str], reqs: list[dict],
             pool: list[dict], commandable: set[str], pos: dict[str, Any], bpl: list[dict]) -> None:
    """Sets run["backhaul"] = {"pile", "to", "takes": {code: {resource: qty}}} on delivery runs (ships flying
    themselves, no ride) that end outside a printing hub's system: after the drop-off they fly to a stray stockpile
    (not where any request or run of this pass delivers, not in a hub system, at least MIN_RETURN,
    in the drop-off's system or within a relay hop of it, no farther from the hub than the drop-off is plus a hop),
    load what they hold, and deposit it at the hub. What the hub's prints lack goes first, then the biggest amounts.
    Takes from `spare`, so the stockpile returns of this pass don't fetch it too."""
    from . import cargo as cg
    by = {d["device_code"]: d for d in pool}
    hubs = sorted(h for h in gather if h in commandable and gather[h])
    if not hubs:
        return
    lacks: dict[str, dict[str, float]] = defaultdict(dict)
    for r in reqs:
        if r["kind"] == "print" and r.get("to"):
            for x, q in (r.get("need") or {}).items():
                lacks[star_of(r["to"])][x] = lacks[star_of(r["to"])].get(x, 0.0) + q
    # a pile some request delivers to is no stray (live 2026-10-28: F10B1A2F dropped 200 silicates at RILXORAE-3 for
    # Dyson Swarm Seed and took the 500 conductive an earlier trip had left there back to FALQUORYX, again and again)
    kept = {r["to"] for r in reqs if r.get("to") and r["kind"] != "return"} | {r["to"] for r in runs if r.get("to")}
    for run in runs:
        cargos = [c for c in run.get("cargos") or [] if not c.get("rides")]
        dstar = star_of(run.get("to"))
        if (run.get("kind") not in BACKHAUL_KINDS or run.get("ami") or not cargos or run.get("back") or run.get("ride")
                or dstar in gather
                or (run.get("rewards") and run.get("home")) or dstar not in commandable):
            continue
        hub = min(hubs, key=lambda h: (_dist(dstar, h, pos), h))
        home_d = _dist(dstar, hub, pos)
        piles = [l for l, s_ in spare.items() if l not in kept and star_of(l) not in gather and star_of(l) in commandable
                 and sum(q for q in s_.values() if q >= 1) >= MIN_RETURN
                 and (star_of(l) == dstar or _dist(dstar, star_of(l), pos) <= RELAY_HOP)
                 and _dist(star_of(l), hub, pos) <= home_d + RELAY_HOP]
        if not piles:
            continue
        want = lacks.get(hub, {})
        pile = min(piles, key=lambda l: (star_of(l) != dstar, -sum(min(q, want.get(x, 0.0)) for x, q in spare[l].items()),
                                         -sum(spare[l].values()), l))
        takes = {}
        for c in cargos:
            d = by.get(c["code"]) or {}
            room_ = float(cg.capacity_of(d, bpl, None) or c.get("room") or 0.0)
            take = {}
            for x, q in sorted(spare[pile].items(), key=lambda kv: (-min(kv[1], want.get(kv[0], 0.0)), -kv[1], kv[0])):
                n = math.floor(min(q, room_))
                if n >= 1:
                    take[x] = n
                    room_ -= n
                    spare[pile][x] -= n
            if take:
                takes[c["code"]] = take
        if takes:
            run["backhaul"] = {"pile": pile, "to": gather[hub], "takes": takes}


def holds_replicant(d: dict, devices: list[dict]) -> bool:
    """A replicant's vessel, a matrix (filled or empty), a matrix container (the `cradle` feature), or anything with a
    matrix inside it. The drop-off never lets these off: a replicant stays with its carrier (owner 2026-10-21)."""
    if d.get("hosting_replicant") or "matrix" in (d.get("device_type") or "") \
            or {"matrix", "cradle"} & set(d.get("features") or []):
        return True
    code = d.get("device_code")
    return any("matrix" in (x.get("device_type") or "") for x in devices
               if code and code in (x.get("stowed_in_device_code"), x.get("attached_to_device_code")))


def stray_aboard(carrier: dict, devices: list[dict], own: set[str], busy: set[str]) -> list[dict]:
    """Devices riding a logistics carrier that have nothing to do with its work: not a replicant or its matrix (its
    escort), not a logistics device, not on its way somewhere (to: / at: / gather tags), not in a job.
    [{"code", "mode": "attach" | "stow"}]."""
    from .loadouts import GATHER
    out = []
    code = carrier.get("device_code")
    for d in devices:
        how = "attach" if d.get("attached_to_device_code") == code else "stow" if d.get("stowed_in_device_code") == code else None
        tags = d.get("tags") or []
        if not how or d.get("device_code") in own or d.get("device_code") in busy or holds_replicant(d, devices) \
                or GATHER in tags or any(str(t).startswith(("to:", "at:")) for t in tags):
            continue
        out.append({"code": d["device_code"], "mode": how})
    return out


def _deal(r: dict) -> dict | None:
    if r["kind"] == "contract":
        return {"kind": "contract", "designation": r["designation"], "location": r["to"], "label": r["label"]}
    if r["kind"] == "trade":
        n = r.get("times", 1)
        return {"kind": "trade", "controller": r["controller"], "trade_code": r["trade_code"], "location": r["to"],
                "label": r["label"] + (f" ×{n}" if n > 1 else ""), "times": n}
    return None


def keep_escorts(wanted: dict[str, int], fleets: list[dict], devices: list[dict], hosts: dict[str, str], busy: set[str],
                 bps: dict[str, dict], pos: dict[str, Any], reach: float) -> dict[str, str]:
    """Relays go first (owner 2026-10-19): for each system where relays or beacons wait for an escort beyond the network
    (loadouts.escort_wanted), the nearest free logistics vessel hosting a replicant within the loadouts' supply range
    stays out of this pass, so the loadout pass can take it. {device: star}."""
    from . import loadouts as lo
    from .fleets import members
    pool = [d for f in logistics_fleets(fleets) for d in members(f, devices)
            if d["device_code"] in hosts and d["device_code"] not in busy and lo.can_surge(d) and lo._carrier_cap(d, bps) > 0
            and d.get("location") and not d.get("controller_device_code")
            and not str(d.get("status") or "").startswith(("travel", "cruis", "surg", "stowed"))]
    kept: dict[str, str] = {}
    for star in sorted(wanted, key=lambda s: (-wanted[s], s)):
        near = [(lo._dist(lo.star_of(d.get("location")), star, pos), d["device_code"]) for d in pool
                if d["device_code"] not in kept]
        near = [x for x in near if x[0] <= (reach or 0)]
        if near:
            kept[min(near)[1]] = star
    return kept


def riding_hosts(devices: list[dict], hosts: dict[str, str]) -> dict[str, str]:
    """Replicant hosts plus the carriers they ride in: a fleet carrier with a matrix container aboard, itself holding the
    replicant's matrix, brings that replicant wherever it flies (each level up the attach / stow chain counts)."""
    by = {d.get("device_code"): d for d in devices}
    out = dict(hosts)
    for code, rep in hosts.items():
        seen, c = {code}, code
        while True:
            up = (by.get(c) or {}).get("attached_to_device_code") or (by.get(c) or {}).get("stowed_in_device_code")
            if not up or up in seen:
                break
            out.setdefault(up, rep)
            seen.add(up)
            c = up
    return out


def ami_steps(run: dict) -> list[dict]:
    g = run["legs"][0]
    c = run["ami"]
    return [step(f"{c}: delivery {g['pile']} → {run['to']} (logistics request)", f"/devices/{c}",
                 {"command": "set_directive", "directive": "delivery",
                  "configuration": {"route": {"collect": g["pile"], "deliver": run["to"]}, "requirement": run.get("require") or g["take"]}},
                 critical=True),
            step(f"{c}: launch", f"/devices/{c}", {"command": "launch"})]


def run_devices(run: dict) -> list[str]:
    return [c["code"] for c in run.get("cargos") or []] + [c for c in (run.get("ride"), run.get("escort"), run.get("rescued"), run.get("repairer")) if c] \
        + [x["code"] for x in run.get("dump") or []]


def stale_rescues(jobs: list[dict], devices: list[dict]) -> list[tuple[dict, str]]:
    """Rescue jobs whose device no longer needs them, before any step has commanded it: it's in command range again
    without the escort beside it (a replicant reached it some other way — DELTA 2026-10-22: a heaven vessel stranded
    there got a replicant by slingshot and flew on with its fleet while the rescue escort was still on its 800 h trip),
    or a bootstrap took it over. [(job, why)]"""
    by = {d.get("device_code"): d for d in devices}
    out = []
    for j in jobs:
        req = str(((j.get("meta") or {}).get("logistics") or {}).get("req") or "")
        if j.get("status") not in ("running", "waiting") or not req.startswith("rescue:"):
            continue
        code = req.split(":", 1)[1]
        d = by.get(code)
        if not d:
            continue
        sent = j["steps"][:j.get("idx", 0)]
        if any(code in str(st.get("path") or "") or code in json.dumps(st.get("body") or {}) for st in sent):
            continue   # under way: the device itself is being moved
        if any(t.startswith("boot:") for t in d.get("tags") or []):
            out.append((j, f"{code} belongs to a bootstrap now"))
            continue
        esc_path = str((j["steps"][0] if j["steps"] else {}).get("path") or "")
        esc = by.get(esc_path.split("/")[2]) if esc_path.startswith("/devices/") else None
        if d.get("in_control_range") is True and star_of((esc or {}).get("location")) != star_of(d.get("location")):
            out.append((j, f"{code} is in command range again at {d.get('location')}"))
    return out


def run_steps(run: dict, by_code: dict[str, dict], template: str | None = None) -> list[dict]:
    """A convoy: each cargo device loads at its stockpiles; those riding attach to the carrier (`ride`), which flies them
    with the others flying alongside; one escort for all. At every hop the trips are sent cargo first, the escort last
    (so everyone is still in its company when theirs goes out), and every arrival is awaited, the escort's first, before
    anyone loads, unloads or detaches there."""
    from . import fleets as fl
    if run.get("ami"):
        return ami_steps(run)
    steps: list[dict] = []
    cargos, ride, esc = run.get("cargos") or [], run.get("ride"), run.get("escort")
    at = {c: (by_code.get(c) or {}).get("location") for c in run_devices(run)}

    def hop(loc: str, who: list[str], why: str = "") -> None:
        sent = []
        for c in who:
            if c and at.get(c) != loc:
                rep = (run.get("direct") or {}).get(c)   # a replicant's own vessel moves by the replicant's travel
                steps.append(step(f"{c} → {loc}{why}", f"/replicants/{rep}/travel" if rep else f"/devices/{c}",
                                  {"destination": loc} if rep else {"command": "travel", "destination": loc}))
                sent.append((c, len(steps) - 1))
                at[c] = loc
        for c, i in sorted(sent, key=lambda x: x[0] != esc):   # the escort's arrival first
            w = step(f"wait for {c} at {loc}", "", None, method="WAIT", wait=["travel.arrived"], match={"destination": loc},
                     timeout=STEP_TIMEOUT)
            w["wait_device"], w["seq0_from"] = c, i
            steps.append(w)
    if run.get("kind") == "dump":
        hop(run["to"], [esc], " (drop-off)")
        if run.get("carry"):
            steps.append(step(f"{esc}: deposit {', '.join(f'{q:g} {r}' for r, q in sorted(run['carry'].items()))} at {run['to']}",
                              f"/devices/{esc}", {"command": "deposit_resources"}))
        for x in run.get("dump") or []:
            if x["mode"] == "attach":
                steps.append(step(f"{esc}: detach {x['code']}", f"/devices/{esc}", {"command": "detach", "device": x["code"]}))
            else:
                st = step(f"deploy {x['code']} from {esc}", f"/devices/{x['code']}", {"command": "deploy"},
                          wait=["device.deployed"], timeout=STEP_TIMEOUT)
                st["wait_device"] = x["code"]
                steps.append(st)
        return steps
    if run.get("kind") == "listing":   # the engine reads the shop's trades once the replicant is in its system
        hop(run["point"], [esc], " (to read the shop's trades)")
        return steps
    if run.get("kind") == "repair":   # repair_runs: to the worst worn device's spot, then the patrol directive
        code, point = run["repairer"], run["point"]
        patrol = step(f"{code}: set_directive patrol", f"/devices/{code}", {"command": "set_directive", "directive": "patrol"})
        if run["mode"] == "stay":
            return [patrol]
        if run.get("patrolling"):   # a patrolling device cruises off to its next job: stop it where it is first
            steps.append(step(f"{code}: clear_directive (leaves for {star_of(point)})", f"/devices/{code}",
                              {"command": "clear_directive"}))
        if run["mode"] == "self":
            hop(point, [code], " (repair patrol)")
            return steps + [patrol]
        ride = run["ride"]
        hop(at[code], [ride], " (pick up)")
        steps.append(fl.board_step(ride, code, run["mode"]))
        hop(point, [ride], " (repair patrol)")
        if run["mode"] == "attach":
            steps.append(step(f"{ride}: detach {code}", f"/devices/{ride}", {"command": "detach", "device": code}))
        else:
            st = step(f"deploy {code} from {ride}", f"/devices/{code}", {"command": "deploy"}, wait=["device.deployed"],
                      timeout=STEP_TIMEOUT)
            st["wait_device"] = code
            steps.append(st)
        return steps + [patrol]
    if run.get("kind") == "rescue":
        code, home, point = run["rescued"], run["to"], run["point"]
        if run["mode"] == "self":   # a replicant where the device can hear it, then the device flies itself home
            hop(point, [esc], " (to command the stranded device)")
            hop(home, [code], " (home)")
            if run.get("back"):
                hop(run["back"], [esc], " (waits in the relay network)")
            return steps
        hop(point, [ride] + ([esc] if esc else []), " (pick up)")   # the carrier fetches it, a replicant along
        steps.append(fl.board_step(ride, code, run["mode"]))
        hop(home, [ride] + ([esc] if esc else []), " (home)")
        if run["mode"] == "attach":
            steps.append(step(f"{ride}: detach {code}", f"/devices/{ride}", {"command": "detach", "device": code}))
        else:
            st = step(f"deploy {code} from {ride}", f"/devices/{code}", {"command": "deploy"}, wait=["device.deployed"],
                      timeout=STEP_TIMEOUT)
            st["wait_device"] = code
            steps.append(st)
        return steps
    riders = [c["code"] for c in cargos if c.get("rides")]
    flyers = [c["code"] for c in cargos if not c.get("rides")]

    def fetch(c: str, loc: str) -> None:
        """A rider in another system than `loc` is fetched there by the carrier (it can't surge)."""
        if ride and at.get(c) and star_of(at[c]) != star_of(loc):
            hop(at[c], [ride] + ([esc] if esc else []), " (fetch)")
            steps.append(fl.board_step(ride, c, "attach"))
            hop(loc, [ride] + ([esc] if esc else []), " (pick up)")
            steps.append(step(f"{ride}: detach {c}", f"/devices/{ride}", {"command": "detach", "device": c}))
            at[c] = loc

    def go(loc: str, why: str = "") -> None:
        """Everyone to `loc`: riders attach at one meeting point first, then the carrier, the flyers and the escort go."""
        if ride and riders:
            meet = at[riders[0]]
            for c in riders[1:]:
                fetch(c, meet)
            hop(meet, riders)
            hop(meet, [ride])
            for c in riders:
                steps.append(fl.board_step(ride, c, "attach"))
        hop(loc, flyers + ([ride] if ride else []) + ([esc] if esc else []), why)
        if ride and riders:
            for c in riders:
                steps.append(step(f"{ride}: detach {c}", f"/devices/{ride}", {"command": "detach", "device": c}))
                at[c] = loc
    outside_pickup = bool(esc and run.get("escort_pickup"))   # stockpiles beyond the network: the escort is there first
    for c in cargos:
        for g in c["legs"]:
            if c.get("rides"):
                fetch(c["code"], g["pile"])
            hop(g["pile"], [c["code"]] + ([esc] if esc and outside_pickup else []), " (pick up)")
            at[c["code"]] = g["pile"]
            steps.append(step(f"{c['code']}: load {', '.join(f'{q:g} {r}' for r, q in g['take'].items())} at {g['pile']}",
                              f"/devices/{c['code']}", {"command": "collect_resources", "resources": g["take"]}, critical=True))
    n0 = len(steps)
    go(run["to"])
    if not esc:   # planned inside comms coverage: check again before leaving (automations._dark_destination)
        for st in steps[n0:]:
            if (st.get("body") or {}).get("command") == "travel" and st["body"].get("destination") == run["to"]:
                st["need_comms"] = star_of(run["to"])
    for c in cargos:
        if c.get("carry"):
            steps.append(step(f"{c['code']}: deposit at {run['to']}", f"/devices/{c['code']}", {"command": "deposit_resources"},
                              critical=True))
    if run.get("fulfil"):
        for dl in [run["fulfil"]] + (run.get("also") or []):   # and the other deals at the same location it paid for
            steps += [fl.fulfil_step(dl, run.get("replicant"), template) for _ in range(dl.get("times") or 1)]
    if run.get("rewards") and run.get("home") and cargos:
        # load what the deal paid out (each ship up to its hold; not critical: the payout can differ from what was listed)
        left, loaded = dict(run["rewards"]), []
        for c in cargos:
            room_ = float(c.get("room") or 0)
            take = {}
            for r_, q in sorted(left.items(), key=lambda kv: -kv[1]):
                n = math.floor(min(q, room_))
                if n >= 1:
                    take[r_] = n
                    room_ -= n
                    left[r_] -= n
            left = {r_: q for r_, q in left.items() if q >= 1}
            if take:
                steps.append(step(f"{c['code']}: load {', '.join(f'{q:g} {r_}' for r_, q in take.items())} (the payout) at {run['to']}",
                                  f"/devices/{c['code']}", {"command": "collect_resources", "resources": take}))
                loaded.append(c["code"])
        go(run["home"], " (with the payout)")
        for c in loaded:
            steps.append(step(f"{c}: deposit the payout at {run['home']}", f"/devices/{c}", {"command": "deposit_resources"}))
        return steps
    bh = run.get("backhaul")
    if bh:   # the way back: a stray stockpile to the printing hub instead of an empty trip (not critical: extra)
        hop(bh["pile"], list(bh["takes"]), " (on the way back: pick up)")
        for c, take in bh["takes"].items():
            steps.append(step(f"{c}: load {', '.join(f'{q:g} {r_}' for r_, q in take.items())} at {bh['pile']} (on the way back)",
                              f"/devices/{c}", {"command": "collect_resources", "resources": take}))
        hop(bh["to"], list(bh["takes"]), " (on the way back)")
        for c in bh["takes"]:
            steps.append(step(f"{c}: deposit at {bh['to']}", f"/devices/{c}", {"command": "deposit_resources"}))
    if run.get("back"):
        go(run["back"], " (waits in the relay network)")
    return steps


def describe(run: dict) -> str:
    what = ", ".join(f"{q:g} {r}" for r, q in sorted((run.get("carry") or {}).items()))
    if run.get("kind") == "rescue":
        how = (f"{run['escort']} goes to {run['point']} to command it, it flies home to {run['to']}" if run["mode"] == "self"
               else f"{run['ride']} picks it up ({run['mode']}) and brings it to {run['to']}"
               + (f", {run['escort']} alongside" if run.get("escort") else ""))
        return f"{run['label']}: {how}"
    if run.get("kind") == "repair":
        if run["mode"] == "stay":
            return run["label"]
        how = f"{run['repairer']} flies" if run["mode"] == "self" else f"{run['ride']} carries {run['repairer']} ({run['mode']})"
        return f"{run['label']}: {how} to {run['point']} and patrols there"
    if run.get("kind") == "listing":
        return f"{run['label']}: {run['escort']} → {run['point']}"
    if run.get("kind") == "dump":
        what_ = [what] if what else []
        if run.get("dump"):
            what_.append(", ".join(x["code"] for x in run["dump"]))
        return f"{run['label']}: {run['escort']} → {run['to']}, leaves {' and '.join(what_)}"
    codes = [c["code"] for c in run.get("cargos") or []]
    who = ", ".join(codes) or run.get("escort") or (f"AMI transport {run['ami']}" if run.get("ami") else "?")
    s = f"{run['label']}: {who}" + (f" carry {what}" if what and len(codes) > 1 else f" carries {what}" if what else "") + f" → {run['to']}"
    riders = [c["code"] for c in run.get("cargos") or [] if c.get("rides")]
    if run.get("ride") and riders:
        s += f", {', '.join(riders)} riding {run['ride']}"
    if run.get("escort") and codes:
        s += f", {run['escort']} alongside"
    if run.get("fulfil"):
        s += ", then fulfills" + (f" all {len(run['also']) + 1}" if run.get("also") else "")
    if run.get("rewards") and run.get("home"):
        s += f" and brings the payout to {run['home']}"
    if run.get("backhaul"):
        bh = run["backhaul"]
        tot = {}
        for take in bh["takes"].values():
            for x, q in take.items():
                tot[x] = tot.get(x, 0) + q
        s += f"; on the way back {', '.join(f'{q:g} {x}' for x, q in sorted(tot.items()))} from {bh['pile']} → {bh['to']}"
    return s
