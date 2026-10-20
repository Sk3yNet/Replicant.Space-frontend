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

The logistics devices: every member of a logistics fleet. Materials ride in cargo devices: freighters and cargo vessels fly anywhere;
transport drones and haulers work inside their system, or are towed (attached) to another by a logistics carrier (plate,
platform, carrier, cargo vessel). Anywhere outside an active relay network (relaynet.coverage)
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

Devices go the same way: deliveries of devices (loadouts.plan) take logistics-fleet carriers first.

Drop-offs (owner 2026-10-20): an idle logistics carrier inside the network with leftover cargo, or with devices aboard
that aren't its work (stray_aboard), goes to the nearest receiving point (a fleet that takes materials in, or a working
system hub of yours), deposits the cargo and lets those devices off.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from .automations import STEP_TIMEOUT, step
from .shapes import as_amounts

KV_STATE = "logistics_state"        # the last pass: requests, runs, unmet
KV_PREFS = "logistics_prefs"        # {"bump": {request id: tier}, "skip": [request ids]}
KV_PICKUPS = "logistics_pickups"   # {deal location: {"types": {type: n}, "to": gather star, "at"}} — devices paid out
KV_MOVES = "logistics_moves"   # {old home star: {"to": new home star, "fleet", "at"}} — a destination fleet's stock follows it
KV_TRADES = "trade_approvals"  # {"trades": {controller:trade_code: iso}, "done": {key: iso}}
ROLE = "logistics"
MIN_RETURN = 25.0              # a stray pile smaller than this isn't worth a trip
TIERS = {0: "evacuation", 1: "queued prints", 2: "local delivery", 3: "long-range transfer", 4: "stockpile return", 5: "rescue"}
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
             no_consolidate: set[str] | None = None, evacuate: dict[str, str] | None = None) -> tuple[list[dict], dict, dict]:
    """(requests by priority, spare per pile, reserve per system). `gather`: gather star → its drop point. `moved`: a
    fleet's old home → (its new home, the drop point there): what's left there follows the fleet."""
    moved = moved or {}
    prefs = prefs or {}
    costs = print_costs(devices, bps)
    spare, reserve = surplus(inventory, costs)
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
                    "designation": c["designation"], "devices_short": c.get("devices_short", False),
                    "rewards": as_amounts(c.get("rewards") or {}), "reward_devices": c.get("reward_devices") or {}})
    for t in trades:
        out.append({"id": f"trade:{t['key']}", "kind": "trade", "to": t["location"], "price": t["price"],
                    "need": _short(t["price"], have(t["location"])), "label": f"trade {t.get('name') or t['trade_code']}",
                    "controller": t["controller"], "trade_code": t["trade_code"], "once": t.get("once", False),
                    "devices_short": t.get("devices_short", False),
                    "rewards": as_amounts(t.get("rewards") or {}), "reward_devices": t.get("reward_devices") or {}})
    for r in out:
        if r["kind"] != "print":   # local when the system itself can spare a needed resource
            here = star_of(r["to"])
            local = any(spare.get(l, {}).get(x, 0) >= 1 for l in spare if star_of(l) == here and l != r["to"] for x in r["need"])
            r["tier"] = 2 if local else 3
    for loc, to in sorted((evacuate or {}).items()):   # an asteroid is due there: its pile goes first (evacuate.py)
        items = stock.get(loc) or {}
        if sum(items.values()) >= 1 and to:
            out.append({"id": f"evacuate:{loc}", "kind": "return", "to": to, "from": loc, "tier": 0,
                        "need": {r: q for r, q in items.items() if q >= 1},
                        "label": f"evacuate the {loc} stockpile to {to} before impact"})
    asked = {r["to"] for r in out} | set(costs) | set(evacuate or {})
    homes = {f.get("home") for f in fleets if f.get("home") and (f.get("station") or f.get("materials") == "self")}
    for loc, items in sorted(stock.items()):
        total = sum(items.values())
        star = star_of(loc)
        if total < MIN_RETURN or loc in asked or star in busy_stars:
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
    return out, spare, reserve


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


def plan(*, fleets: list[dict], devices: list[dict], bps: dict[str, dict], inventory: dict[str, dict],
         pos: dict[str, Any], commandable: set[str], hosts: dict[str, str], busy: set[str], reach: float = math.inf,
         contracts: list[dict], trades: list[dict], incoming: dict[str, dict], replicants: dict[str, dict],
         drop: Any, busy_stars: set[str] | frozenset = frozenset(), prefs: dict | None = None,
         ami: list[dict] | None = None, moved: dict[str, str] | None = None,
         dark_nets: list[set[str]] | None = None, away_fleets: set[str] | frozenset = frozenset(),
         no_consolidate: set[str] | frozenset = frozenset(), evacuate: dict[str, str] | None = None) -> dict:
    """What the logistics devices do this pass. `hosts`: device → the replicant it hosts. `drop(star)`: where materials land in a
    system. Returns {"requests", "runs", "unmet", "spare", "reserve", "fleet_devices"}."""
    from . import cargo as cg
    from .loadouts import can_surge
    gather = {f["home"]: drop(f["home"]) for f in fleets or [] if f.get("materials") == "self" and f.get("home")}
    reqs, spare, reserve = requests(devices=devices, bps=bps, inventory=inventory, pos=pos, contracts=contracts,
                                    trades=trades, incoming=incoming, fleets=fleets, gather=gather,
                                    busy_stars=set(busy_stars), prefs=prefs,
                                    moved={o: (n, drop(n)) for o, n in (moved or {}).items()},
                                    home_drop={f["home"]: drop(f["home"]) for f in fleets or [] if f.get("home")
                                               and (f.get("station") or f.get("materials") == "self") and f["home"] in commandable},
                                    no_consolidate=set(no_consolidate), evacuate=evacuate)
    hosts = riding_hosts(devices, hosts)   # a carrier with a replicant's matrix aboard escorts like its host
    # the vessel the replicant's matrix is in travels as the replicant; a carrier towing that vessel flies by its own travel
    # (live 2026-10-20: /replicants/…/travel moved the towed matrix container: "Device does not have surge capability")
    towed = {c: d.get("attached_to_device_code") or d.get("stowed_in_device_code") for d in devices
             if (c := d.get("device_code")) in hosts}
    direct = {c: r for c, r in hosts.items() if not any(towed.get(x) == c and hosts[x] == r for x in hosts)}
    ami_free = {c["device_code"]: c for c in ami or []}
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
    for r in reqs:
        give_back()
        if r["skipped"]:
            continue
        need = {x: q for x, q in r["need"].items() if q >= 1}
        dest, dstar = r["to"], star_of(r["to"])
        if r["kind"] == "print" and dest:
            for l, h in kept.items():
                if star_of(l) == dstar and l != dest:
                    for x, q in h.items():
                        spare.setdefault(l, {})[x] = spare.get(l, {}).get(x, 0.0) + q
                        released[(l, x)] = released.get((l, x), 0.0) + q
                    kept[l] = {}
            for (l, x), q in (shares.get(dest) or {}).items():   # what another hub shares with this one's queue
                q = min(q, kept.get(l, {}).get(x, 0.0))
                if q > 0:
                    kept[l][x] -= q
                    spare.setdefault(l, {})[x] = spare.get(l, {}).get(x, 0.0) + q
                    released[(l, x)] = released.get((l, x), 0.0) + q
        if not dest:
            unmet.append({"req": r["id"], "why": "no gather point: give a fleet 'takes materials in' (Fleets) to bring it back"})
            continue
        if not need:
            if r["kind"] in ("contract", "trade") and r.get("devices_short"):
                unmet.append({"req": r["id"], "why": f"materials are there; waiting for the devices it asks for at {dest} "
                                                     "(delivered by the contract's device supply)"})
            elif r["kind"] in ("contract", "trade"):   # stocked: a replicant fulfills it
                rep = ready_replicant(r, None, None)
                esc = None
                if not rep:
                    esc = escort_for([dest], set())
                    if not esc:
                        unmet.append({"req": r["id"], "why": f"{dest} holds the price; waiting for a free logistics device hosting a replicant to fulfill it"})
                        continue
                    used.add(esc["device_code"])
                    rep = hosts[esc["device_code"]]
                runs.append({"req": r["id"], "kind": r["kind"], "cargos": [], "escort": esc and esc["device_code"],
                             "legs": [], "to": dest, "carry": {}, "fulfil": _deal(r), "replicant": rep,
                             "back": wait_point(esc, dstar) if esc and dstar not in commandable else None, "label": r["label"]})
            continue
        def can_escort() -> bool:
            return any(c not in used and c in hosts and can_surge(d) for c, d in free.items())

        def sources_ok(l: str) -> bool:
            if r["kind"] == "return":
                return l == r.get("from")
            # piles beyond the network count while a replicant is free to go along
            return l != dest and (star_of(l) in commandable or can_escort())
        assigned = False
        while need:
            piles = sorted((l for l, s in spare.items() if sources_ok(l) and any(s.get(x, 0) >= 1 for x in need)),
                           key=lambda l: (_dist(star_of(l), dstar, pos), -sum(spare[l].get(x, 0) for x in need), l))
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
                    runs.append({"req": r["id"], "kind": r["kind"], "ami": ctrl, "cargos": [], "escort": None,
                                 "legs": [{"pile": first, "take": take}], "to": dest, "carry": take, "fulfil": None,
                                 "replicant": None, "back": None, "label": r["label"]})
                    continue
            # one run per source system: cargo devices load there, travel together (riding a carrier when one is free
            # for a long trip) and share one escort
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
                                               and (same or (ride and seats >= 1))))
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
                    unmet.append({"req": r["id"], "why": f"no free logistics cargo device (freighter, cargo vessel, or a hauler in {fstar} "
                                                         f"with a logistics carrier to tow it) {_within(reach, first)}"})
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
                         "carry": taken, "fulfil": _deal(r) if rep else None, "replicant": rep, "back": back, "label": r["label"],
                         "escort_pickup": any(star_of(x) not in commandable for x in stops[:-1]),
                         "rewards": rewards if home else {}, "home": home,
                         "reward_devices": (r.get("reward_devices") or {}) if rep else {}})
    give_back()
    # rescues: your devices out of command range (beyond an active relay network, no replicant with them), last tier.
    # A device that can surge home is reached by a replicant (in its system, or anywhere its own dark relay network
    # covers, nearest first) and commanded home; one that can't is picked up by a carrier, with a replicant along.
    from . import fleets as fl
    from .loadouts import placed
    fleet_home = {fl.fleet_tag(f["id"]): f.get("home") for f in fleets or [] if f.get("id")}
    rescue_reqs = []
    for d in devices:
        code, loc = d.get("device_code"), d.get("location")
        tag = next((t for t in d.get("tags") or [] if t.startswith("fleet:")), None)
        if (not loc or code in hosts or code in busy or code in used or placed(d) or (d.get("device_type") or "") in KEPT_OUT
                or d.get("stowed_in_device_code") or d.get("attached_to_device_code") or tag in away_fleets
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
        rescue_reqs.append((req, d, fleet_home.get(tag)))
    reqs.extend(r for r, _, _ in rescue_reqs)
    for r, d, home in sorted(rescue_reqs, key=lambda x: (x[0]["tier"], x[0]["id"])):
        if r["skipped"]:
            continue
        code, loc, star = d["device_code"], d["location"], star_of(d["location"])
        if not (home and home in commandable):   # fleetless (or its home is dark): the nearest covered home system
            homes = [f["home"] for f in fleets or [] if f.get("home") in commandable and (f.get("station") or f.get("materials") == "self")]
            home = min(homes, key=lambda h: (_dist(star, h, pos), h), default=None)
        home_loc = drop(home) if home else wait_point({"_fleet": {}}, star)
        if not home_loc:
            unmet.append({"req": r["id"], "why": "no system inside an active relay network to bring it to"})
            continue
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
                unmet.append({"req": r["id"], "why": f"no free logistics device hosting a replicant {_within(reach, star)} "
                                                     "or its relay network"})
                continue
            _, esc, sp = best
            used.add(esc["device_code"])
            point = loc if sp == star else drop(sp)
            runs.append({"req": r["id"], "kind": "rescue", "cargos": [], "ride": None, "escort": esc["device_code"],
                         "rescued": code, "mode": "self", "point": point, "to": home_loc, "carry": {},
                         "back": wait_point(esc, sp) if star_of(point) not in commandable else None, "label": r["label"]})
            continue
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
            continue
        cands.sort(key=lambda x: (not x[0], x[1], x[2]))   # one hosting a replicant needs no escort
        _, _, c, carrier, mode = cands[0]
        esc = None
        if c not in hosts:
            esc = escort_for([loc, home_loc], {c})
            if not esc:
                unmet.append({"req": r["id"], "why": f"{star} is outside an active relay network: the pick-up needs a logistics "
                                                     "device hosting a replicant alongside (none free)"})
                continue
            used.add(esc["device_code"])
        used.add(c)
        runs.append({"req": r["id"], "kind": "rescue", "cargos": [], "ride": c, "escort": esc and esc["device_code"],
                     "rescued": code, "mode": mode, "point": loc, "to": home_loc, "carry": {}, "back": None, "label": r["label"]})

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
    for c, d in sorted(free.items()):
        here = star_of(d.get("location"))
        if c in used or here not in commandable or not docks:
            continue
        cargo = {r: q for r, q in as_amounts(d.get("cargo")).items() if q > 0}
        extra = stray_aboard(d, devices, own, busy)
        if not cargo and not extra:
            continue
        to_star = min(docks, key=lambda h: (_dist(here, h, pos), h))
        used.add(c)
        runs.append({"req": None, "kind": "dump", "cargos": [], "escort": c, "legs": [], "to": receive[to_star],
                     "carry": cargo, "dump": extra, "fulfil": None, "replicant": None, "back": None,
                     "label": f"{c} drops off what isn't its work at {to_star}"})
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
                       "replicant": hosts.get(d["device_code"]), "covered": star_of(d.get("location")) in commandable}
                      for d in pool]}


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
        if not how or d.get("device_code") in own or d.get("device_code") in busy or d.get("hosting_replicant") \
                or "matrix" in (d.get("device_type") or "") or GATHER in tags \
                or any(str(t).startswith(("to:", "at:")) for t in tags):
            continue
        out.append({"code": d["device_code"], "mode": how})
    return out


def _deal(r: dict) -> dict | None:
    if r["kind"] == "contract":
        return {"kind": "contract", "designation": r["designation"], "location": r["to"], "label": r["label"]}
    if r["kind"] == "trade":
        return {"kind": "trade", "controller": r["controller"], "trade_code": r["trade_code"], "location": r["to"],
                "label": r["label"]}
    return None


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
                  "configuration": {"route": {"collect": g["pile"], "deliver": run["to"]}, "requirement": g["take"]}},
                 critical=True),
            step(f"{c}: launch", f"/devices/{c}", {"command": "launch"})]


def run_devices(run: dict) -> list[str]:
    return [c["code"] for c in run.get("cargos") or []] + [c for c in (run.get("ride"), run.get("escort"), run.get("rescued")) if c] \
        + [x["code"] for x in run.get("dump") or []]


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

    def go(loc: str, why: str = "") -> None:
        """Everyone to `loc`: riders attach at one meeting point first, then the carrier, the flyers and the escort go."""
        if ride and riders:
            meet = at[riders[0]]
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
            hop(g["pile"], [c["code"]] + ([esc] if esc and outside_pickup else []), " (pick up)")
            steps.append(step(f"{c['code']}: load {', '.join(f'{q:g} {r}' for r, q in g['take'].items())} at {g['pile']}",
                              f"/devices/{c['code']}", {"command": "collect_resources", "resources": g["take"]}, critical=True))
    go(run["to"])
    for c in cargos:
        if c.get("carry"):
            steps.append(step(f"{c['code']}: deposit at {run['to']}", f"/devices/{c['code']}", {"command": "deposit_resources"},
                              critical=True))
    if run.get("fulfil"):
        steps.append(fl.fulfil_step(run["fulfil"], run.get("replicant"), template))
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
        s += ", then fulfills"
    if run.get("rewards") and run.get("home"):
        s += f" and brings the payout to {run['home']}"
    return s
