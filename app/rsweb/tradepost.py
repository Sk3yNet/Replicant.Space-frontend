"""Trade posts (owner 2026-11-12): a fleet (role trade_post) whose AMI trade controller lists one of each device you
have a blueprint for (system hubs left out: their print time scales), with a number kept on hand and a price each.

Price (formula, kv "trade_pricing", set on the fleet card and shared by every trade post):
    materials  the blueprint's resources plus its components', all the way down (components.total_resources)
    hours      the device's print time plus its components' (one print each, as many as it takes)
    price      per resource: ceil(materials × (factor + pct_per_hour / 100 × hours)), factor 2 and 10 % to start.
               The buyer pays in the same resources the device costs. A device's override replaces the formula.

Stock: the fleet's `trade_post: {onhand: {type: n}, prices: {type: {resource: n}}}`. Devices for the shop are supplied
like a contract's (contractsupply.py, engine contract_supply_pass, key tp:<fleet id>): spares first, else printed on
an autofactory in range, tagged to:/at: the shop's location and contract:tp:<fleet id>. The loadouts pass delivers them.
The game escrows a listing's whole stock at the shop's location, so the pass (engine trade_post_pass) lists what is
there: a listing that sold out, or that the devices on hand bring back to full, is deleted (escrow returned) and
created again with everything there. A device type is listed only when every device of it at the shop is stock (the
game picks which devices go into escrow), and never one carrying a replicant or a matrix.

Proceeds (the resources buyers pay) pile up at the shop's location: a trade post's home is no home stockpile for the
logistics service, so its returns bring them to the nearest printing hub (in a printing hub's system they are
consolidated there anyway).

Unverified live (2026-11-12): whether escrowed devices stay in /devices (escrow codes are remembered, so both work),
and whether creating a listing needs a replicant in the system (reading one does, per the API docs).
"""
from __future__ import annotations

import math
import re
from typing import Any

from .components import components, total_resources
from .shapes import as_amounts

ROLE = "trade_post"
KV_PRICING = "trade_pricing"   # {"factor": 2.0, "pct_per_hour": 10.0}
KV = "trade_posts"             # {fleet id: {listings: {type: {trade_code, stock, escrow: [codes], price}}, note, at, rows}}
DEFAULT_PRICING = {"factor": 2.0, "pct_per_hour": 10.0}
EXCLUDED = {"system_hub"}      # never offered (owner: manufacturing time scales with the hubs you have)
RESOURCES = ["structural", "conductive", "silicates", "carbon", "volatiles", "rares"]
EVERY_S = 300
MOVES_PER_PASS = 12            # consolidation: listings moved to another shop per pass (2 game calls each)
PER_SHOP = 10                  # the game caps a trade controller at 10 listings (season 4 patch, 2026-11-17; 5 before)


REREAD_S = 1800               # a shop's listings are re-read this often, or after a trade event names it (1.473.0)


async def fresh_listings(db, cache: dict, codes: list[str], now) -> dict[str, list]:
    """{controller: its cached listings} for the shops read within REREAD_S with no trade.* event about them since:
    listings only change by a trade or by our own listing (which sends trade.created), so those need no game read
    (1.473.0: every 5 minutes for 16 shops was 3 reads a minute)."""
    from datetime import datetime
    keep: dict[str, str] = {}
    for code in codes:
        e = cache.get(code) or {}
        try:
            if isinstance(e.get("trades"), list) and (now - datetime.fromisoformat(e["at"])).total_seconds() < REREAD_S:
                keep[code] = e["at"]
        except (KeyError, TypeError, ValueError):
            continue
    if not keep:
        return {}
    rows = await db.fetchall(
        f"SELECT device_code, MAX(received_at) AS last FROM events WHERE event LIKE 'trade.%' "
        f"AND device_code IN ({','.join('?' * len(keep))}) GROUP BY device_code", tuple(keep))
    touched = {r["device_code"] for r in rows if (r["last"] or "") >= keep[r["device_code"]]}
    return {c: cache[c]["trades"] for c in keep if c not in touched}


def supply_key(fid: str) -> str:
    return f"tp:{fid}"


def pricing(raw: Any) -> dict:
    out = dict(DEFAULT_PRICING)
    for k in out:
        try:
            v = float((raw or {}).get(k))
            if v >= 0:
                out[k] = v
        except (TypeError, ValueError):
            pass
    return out


def settings(fleet: dict) -> dict:
    tp = fleet.get("trade_post") if isinstance(fleet.get("trade_post"), dict) else {}
    onhand = {}
    for t, n in (tp.get("onhand") or {}).items():
        try:
            if int(n) > 0:
                onhand[t] = int(n)
        except (TypeError, ValueError):
            pass
    prices = {t: {r: int(q) for r, q in as_amounts(p).items() if q > 0} for t, p in (tp.get("prices") or {}).items()}
    spot = str(tp.get("spot") or "").strip().upper()
    return {"onhand": onhand, "prices": {t: p for t, p in prices.items() if p}, "spot": spot,
            "description": str(tp.get("description") or "").strip(), "announcement": str(tp.get("announcement") or "").strip()}


def print_seconds(device_type: str, bps: dict[str, dict], depth: int = 0) -> float:
    """One print's time, its components' prints included (seconds)."""
    bp = bps.get(device_type) or {}
    s = float(bp.get("print_time") or 0)
    if depth < 3:
        for t, n in components(bp).items():
            s += print_seconds(t, bps, depth + 1) * n
    return s


def formula_price(materials: dict[str, float], seconds: float, p: dict) -> dict[str, int]:
    mult = p["factor"] + p["pct_per_hour"] / 100.0 * seconds / 3600.0
    return {r: int(math.ceil(q * mult - 1e-9)) for r, q in materials.items() if q > 0}


_PAIR = re.compile(r"([a-z_]+)\s*[:=]?\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*([a-z_]+)", re.I)


def parse_price(s: str) -> dict[str, int] | None:
    """'200 structural, 50 conductive' (or 'structural: 200 …') → {resource: n}; {} for blank; None when unreadable."""
    s = (s or "").strip().lower()
    if not s:
        return {}
    out: dict[str, int] = {}
    for m in _PAIR.finditer(s):
        name, num = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        name = next((r for r in RESOURCES if r.startswith(name)), None) if name else None
        if not name:
            return None
        out[name] = out.get(name, 0) + int(math.ceil(float(num)))
    return {r: q for r, q in out.items() if q > 0} or None


def price_text(p: dict | None) -> str:
    return ", ".join(f"{q:g} {r}" for r, q in sorted((p or {}).items(), key=lambda x: RESOURCES.index(x[0])
                                                     if x[0] in RESOURCES else 99))


def offered(bps: dict[str, dict]) -> list[str]:
    return sorted(t for t in bps if t and t != "?" and t not in EXCLUDED)


def rows(fleet: dict, bps: dict[str, dict], p: dict) -> list[dict]:
    """Every offered device: materials, print time, formula price, override, price used, on hand wanted."""
    s = settings(fleet)
    out = []
    for t in offered(bps):
        mats = total_resources(t, bps)
        secs = print_seconds(t, bps)
        f = formula_price(mats, secs, p)
        over = s["prices"].get(t)
        out.append({"type": t, "materials": {r: q for r, q in mats.items() if q > 0}, "seconds": secs,
                    "formula": f, "override": over or {}, "price": over or f, "onhand": s["onhand"].get(t, 0),
                    "components": components(bps.get(t))})
    return out


def controllers(fleet: dict, devices: list[dict]) -> list[dict]:
    """The fleet's AMI trade controllers (each a shop of its own, PER_SHOP listings), by code."""
    from .fleets import members
    from .shop import is_shop
    return [d for d in sorted(members(fleet, devices), key=lambda x: x.get("device_code") or "")
            if is_shop(d) and d.get("location")]


def controller(fleet: dict, devices: list[dict]) -> dict | None:
    return next(iter(controllers(fleet, devices)), None)


def shops_needed(fleet: dict) -> int:
    """Controllers it takes to list every stocked type, PER_SHOP each."""
    return max(1, -(-len(settings(fleet)["onhand"]) // PER_SHOP))


def number_shops(known: dict, codes: list[str]) -> dict[str, int]:
    """Each controller's shop number: kept once given, a new one takes the lowest free number (owner 2026-11-14)."""
    out = {c: int(n) for c, n in (known or {}).items() if c in codes and str(n).isdigit()}
    for c in codes:
        if c not in out:
            out[c] = next(i for i in range(1, len(codes) + 2) if i not in out.values())
    return out


def shop_name(fleet: dict, n: int) -> str:
    return f"{(fleet.get('name') or 'Trade post').strip()} {n}"


def shop_config(fleet: dict, n: int, location: str | None = None) -> dict:
    """The trade directive's configuration for shop `n` (owner 2026-11-14): the first shop carries the post's description
    and its BobNet announcement, the others only a generic description pointing at it. BobNet names only the star of
    the replicant that posts it, so the shop's exact location goes into the texts: in place of {spot}, else added where
    it fits (announcement ≤ 80, description ≤ 240)."""
    s = settings(fleet)
    cfg = {"name": shop_name(fleet, n)}
    if n == 1:
        for k, lim, add in (("announcement", 80, lambda t, l: f"{t} @ {l}"), ("description", 240, lambda t, l: f"At {l}. {t}")):
            t = s[k]
            if not t:
                continue
            if location:
                if "{spot}" in t:
                    t = t.replace("{spot}", location)
                elif location not in t and len(add(t, location)) <= lim:
                    t = add(t, location)
            cfg[k] = t.replace("{spot}", "")[:lim]
        if location and "announcement" in cfg and location not in cfg["announcement"] and "description" not in cfg:
            cfg["description"] = f"At {location}."
    else:
        cfg["description"] = f"More listings from {shop_name(fleet, 1)}{' at ' + location if location else ''}."[:240]
    return cfg


def config_differs(have: dict, want: dict) -> bool:
    return any((have.get(k) or "") != (want.get(k) or "") for k in ("name", "description", "announcement"))


def value(price: dict) -> int:
    return sum(int(q) for q in (price or {}).values())


def carries_replicant(d: dict, devices: list[dict]) -> bool:
    """A replicant's vessel, a filled replicant matrix, or anything with a matrix inside: never sold (an empty matrix
    container or a vessel without a replicant may be)."""
    t = d.get("device_type") or ""
    if d.get("hosting_replicant") or t == "replicant_matrix":
        return True
    code = d.get("device_code")
    return any("matrix" in (x.get("device_type") or "") for x in devices
               if code and code in (x.get("stowed_in_device_code"), x.get("attached_to_device_code")))


def overage(fleet: dict, state: dict) -> dict[str, int]:
    """Listed beyond the fleet's stock number, not claimed yet: {type: n}. Spares a fleet may ask for (owner 2026-11-17);
    otherwise it stays for sale."""
    onhand = settings(fleet)["onhand"]
    claims = state.get("claims") or {}
    out = {}
    for r in state.get("rows") or []:
        t = r.get("type")
        n = int(r.get("listed") or 0) - int(onhand.get(t) or 0) - sum(int(x.get("n") or 0) for x in claims.get(t) or [])
        if t and n > 0 and r.get("trade_code") and int(onhand.get(t) or 0) > 0:
            out[t] = n
    return out


def claimed(state: dict) -> dict[str, dict[str, int]]:
    """Overage claimed and not handed over yet: {fleet id: {type: n}} (the loadout counts it as on its way)."""
    out: dict[str, dict[str, int]] = {}
    for t, cl in (state.get("claims") or {}).items():
        for x in cl:
            f = out.setdefault(x.get("fleet") or "", {})
            f[t] = f.get(t, 0) + int(x.get("n") or 0)
    return out


def free_here(d: dict, loc: str | None, devices: list[dict]) -> bool:
    """A device at the shop that a listing may escrow in place of the stock: idle, nobody's (no fleet, contract or trip),
    not pinned elsewhere, carrying no replicant."""
    from .loadouts import pinned_at
    tags = d.get("tags") or []
    if any(str(x).startswith(("fleet:", "contract:", "to:")) for x in tags) or pinned_at(d) not in (None, str(loc or "").upper()):
        return False
    if d.get("controller_device_code") or d.get("hosting_replicant") or carries_replicant(d, devices):
        return False
    return str(d.get("status") or "").startswith(("idle", "inactive"))


def listing_for(trades: list[dict], t: str, known: str | None) -> dict | None:
    """Our listing of `t`: the one we created (its code remembered), else one that gives exactly one `t` for resources."""
    by = {x.get("trade_code"): x for x in trades or []}
    if known and known in by:
        return by[known]
    for x in trades or []:
        rw = x.get("rewards") or {}
        if (rw.get("devices") or {}) == {t: 1} and not (rw.get("resources") or {}):
            return x
    return None


def plan(fleet: dict, devices: list[dict], trades: dict | list | None, state: dict, bps: dict[str, dict],
         p: dict, tag: str) -> dict:
    """What to list, delist and supply. Pure. `trades`: {controller code: its listing as just read, None: unknown}
    (a plain list stands for the first controller's). Every controller is a shop of PER_SHOP listings: a listed type
    keeps its shop, the others take free slots priciest first, and the rest wait for a slot (the pass raises the
    fleet's wanted controllers to shops_needed, so Loadouts brings more).
    Returns {rows, actions: [{type, controller, delete, create, escrow}], release: [codes], need: {type: n},
    skip: [codes], notes, pins: [{code, add, remove}], shops: [{code, location, types, read}],
    unpin: [codes of controllers no longer in the fleet still pinned to its spot]}."""
    ctrls = controllers(fleet, devices)
    ctrl = ctrls[0] if ctrls else None
    if isinstance(trades, list) or trades is None:
        trades = {ctrl["device_code"]: trades} if ctrl else {}
    spot = settings(fleet)["spot"]
    out: dict[str, Any] = {"rows": rows(fleet, bps, p), "actions": [], "release": [], "need": {}, "skip": [],
                           "notes": [], "location": spot or (ctrl or {}).get("location"), "pin": None, "pins": [],
                           "shops": [], "unpin": [], "give": [], "claims_done": [], "drop": []}
    if not ctrl:
        out["notes"].append("no AMI trade controller in the fleet")
        return out
    loc = out["location"]
    from .loadouts import at_tag
    from .shop import is_shop as is_shop_
    ready = []   # shops at the shop's location: the ones that list
    for c in ctrls:
        if spot:
            tags = c.get("tags") or []
            if at_tag(spot) not in tags:
                out["pins"].append({"code": c["device_code"], "add": [at_tag(spot)],
                                    "remove": [t for t in tags if str(t).startswith("at:")]})
        if c.get("location") == loc:
            ready.append(c)
        elif spot:
            out["notes"].append(f"shop {c['device_code']} is at {c.get('location')}, on its way to {spot}: "
                                "it lists once it is there")
    out["pin"] = next(iter(out["pins"]), None)
    if spot:   # a controller that left the fleet still pinned to its spot (owner 2026-11-15: B3F801E9 kept at:<spot> after
        # Loadouts let it go, so its spare tag flapped every pass and it showed as an unnamed shop): unpinned, spare
        mine = {c["device_code"] for c in ctrls}
        out["unpin"] = sorted(d["device_code"] for d in devices if d.get("device_code") not in mine and is_shop_(d)
                              and at_tag(spot) in (d.get("tags") or []) and tag not in (d.get("tags") or [])
                              and not any(str(t).startswith("fleet:") for t in d.get("tags") or []))
    away = not ready
    from .fleets import members
    members_ = {d.get("device_code") for d in members(fleet, devices)}
    listings = state.get("listings") or {}
    read = {c["device_code"]: trades.get(c["device_code"]) for c in ready}
    # where each type is listed now (or was, when that shop's listing could not be read)
    cur_of: dict[str, tuple[str, dict | None]] = {}
    for r in out["rows"]:
        t = r["type"]
        mine = listings.get(t) or {}
        order = sorted(read, key=lambda c: c != mine.get("controller"))
        for c in order:
            if read[c] is None:
                if mine.get("controller") == c or (not mine.get("controller") and c == ctrl["device_code"] and mine):
                    cur_of[t] = (c, None)
                    break
                continue
            x = listing_for(read[c], t, mine.get("trade_code") if mine.get("controller") in (c, None) else None)
            if x:
                cur_of[t] = (c, x)
                break
    used = {c: 0 for c in read}
    for t, (c, _) in cur_of.items():
        if (settings(fleet)["onhand"].get(t) or 0) > 0:
            used[c] += 1
    # consolidation (owner 2026-11-22, controllers went from 5 to 10 listings): the lowest-numbered shops needed stay,
    # the listings of the others move over (delisted and listed again on a keeper) and the emptied ones are let go
    cons = bool(state.get("consolidate"))
    keepers: list[str] = []
    moves: dict[str, str] = {}   # type -> the shop it leaves
    if cons:
        nums = number_shops(state.get("numbers") or {}, [c["device_code"] for c in ctrls])
        keepers = sorted(read, key=lambda c: (nums.get(c, 999), c))[:shops_needed(fleet)]
        for c in keepers:
            used[c] = max(used[c], len(read[c] or []))
    slot: dict[str, str] = {}
    for r in sorted(out["rows"], key=lambda r: (-value(r["price"]), r["type"])):
        t = r["type"]
        if not r["onhand"]:
            continue
        if t in cur_of:
            slot[t] = cur_of[t][0]
            if cons and slot[t] not in keepers and cur_of[t][1] and len(moves) < MOVES_PER_PASS:
                to = next((c for c in keepers if read[c] is not None and used[c] < PER_SHOP), None)
                if to:
                    moves[t] = slot[t]
                    used[slot[t]] -= 1
                    slot[t] = to
                    used[to] += 1
            continue
        free = next((c for c in (keepers or sorted(read)) if read[c] is not None and used[c] < PER_SHOP), None)
        if free:
            slot[t] = free
            used[free] += 1
    at_loc = [d for d in devices if d.get("location") == loc and not d.get("stowed_in_device_code")
              and not d.get("attached_to_device_code")]
    waiting = 0
    for r in out["rows"]:
        t = r["type"]
        mine = listings.get(t) or {}
        c, cur = cur_of.get(t, (slot.get(t), None))
        to = slot.get(t) if t in moves else None
        known = c is not None and read.get(c) is not None
        listed = int((cur or {}).get("current_stock") or 0) if known else (int(mine.get("stock") or 0) if t in cur_of else 0)
        esc = {x for x in mine.get("escrow") or []}
        if known and not cur:
            esc = set()
        same = [d for d in at_loc if d.get("device_type") == t and d.get("device_code") not in esc
                and not d.get("location_stale")]
        loose = sorted((d for d in same if tag in (d.get("tags") or [])), key=lambda d: d["device_code"])
        others = [d for d in same if tag not in (d.get("tags") or [])]
        # an idle spare (or untagged) one beside the stock is as good as stock: the escrow may take it (2026-11-17)
        extra = sorted((d for d in others if free_here(d, loc, devices)), key=lambda d: d["device_code"])
        others = [d for d in others if d not in extra]
        own = [d for d in others if d.get("device_code") in members_]
        out["skip"] += sorted(x for x in esc if x)
        r.update({"listed": listed, "initial": int((cur or {}).get("initial_stock") or 0), "here": len(loose),
                  "trade_code": (cur or {}).get("trade_code"), "listed_price": as_amounts(((cur or {}).get("criteria") or {}).get("resources")),
                  "status": "", "controller": c if (cur or t in cur_of) else slot.get(t)})
        want = r["onhand"]
        if not want:
            if cur:
                out["actions"].append({"type": t, "controller": c, "delete": cur["trade_code"], "create": None, "escrow": []})
                r["status"] = "delisting"
            out["release"] += [d["device_code"] for d in loose]
            continue
        if own:   # the shop's own controllers / relay sit there: a listing could escrow one of them
            r["status"] = (f"can't be sold here: the post's own {len(own)} {t.replace('_', ' ')}(s) sit at {loc} and the "
                           "game could escrow one; take it out of the stock")
            out["release"] += [d["device_code"] for d in loose]
            continue
        cl = (state.get("claims") or {}).get(t) or []
        if cl and not away and known:   # a fleet asked for the overage (owner 2026-11-17): delist, then hand it over
            who = ", ".join(sorted({x.get("fleet") or "?" for x in cl}))
            if cur:
                out["actions"].append({"type": t, "controller": c, "delete": cur["trade_code"], "create": None, "escrow": []})
                r["status"] = f"delisting: overage claimed by {who}"
                continue
            pool = sorted(loose + extra, key=lambda d: d["device_code"])
            room = max(0, len(pool) - want)
            if pool:
                left = room
                for x in cl:
                    k = min(int(x.get("n") or 0), left)
                    for d in pool[room - left:room - left + k]:
                        out["give"].append({"code": d["device_code"], "fleet": x.get("fleet"), "type": t,
                                            "tags": d.get("tags") or []})
                    left -= k
                out["claims_done"].append(t)
                r["status"] = f"overage handed to {who}" if room else "claimed overage gone (nothing over the stock number)"
                continue
            r["status"] = f"delisted for {who}: waiting for the devices to show"
            out["need"][t] = 0
            continue
        out["need"][t] = max(0, want - listed)
        if away:
            r["status"] = "waiting for the shop to reach its spot" if spot else "listing not read"
            continue
        if c is None:
            r["status"] = f"waiting for a shop slot ({len(ctrls)} shop(s), {PER_SHOP} listings each)"
            waiting += 1
            continue
        if not known:
            r["status"] = "listing not read"
            continue
        bad = [d for d in loose + others if carries_replicant(d, devices)]
        if bad:
            r["status"] = f"not listed: {bad[0]['device_code']} at the shop carries a replicant or matrix"
            continue
        if others:
            r["status"] = (f"not listed: {len(others)} other {t.replace('_', ' ')} at {loc} the escrow could take "
                           f"({', '.join(d['device_code'] for d in others[:3])})")
            continue
        price = r["price"]
        if to and cur and listed > 0:   # consolidation: taken down here, listed again on the keeping shop at once
            out["actions"].append({"type": t, "controller": to, "from": c, "delete": cur["trade_code"],
                                   "create": {"name": t.replace("_", " ").capitalize(), "stock": listed + len(loose),
                                              "criteria": {"resources": price, "devices": {}},
                                              "rewards": {"resources": {}, "devices": {t: 1}}},
                                   "escrow": sorted(esc | {d["device_code"] for d in loose}), "price": price})
            r["status"] = f"moving to shop {to}"
            r["controller"] = to
            continue
        repriced = bool(cur) and listed > 0 and {k: int(v) for k, v in r["listed_price"].items()} != price
        loose = sorted(loose + extra, key=lambda d: d["device_code"])
        if loose and (listed == 0 or listed + len(loose) >= want) or repriced:
            stock = listed + len(loose)
            if stock < 1:
                continue
            out["actions"].append({"type": t, "controller": c, "delete": (cur or {}).get("trade_code"),
                                   "create": {"name": t.replace("_", " ").capitalize(), "stock": stock,
                                              "criteria": {"resources": price, "devices": {}},
                                              "rewards": {"resources": {}, "devices": {t: 1}}},
                                   "escrow": sorted(esc | {d["device_code"] for d in loose}), "price": price})
            r["status"] = "repricing" if repriced and not loose else "restocking"
        elif listed < want:
            r["status"] = f"{len(loose)} on hand, waiting for {want - listed - len(loose)} more" if loose else "supplying"
    if waiting:
        out["notes"].append(f"{waiting} type(s) wait for a shop slot: {shops_needed(fleet)} AMI trade controllers "
                            f"needed, {len(ctrls)} in the fleet")
    if cons:   # shops left empty (or never at the spot) are let go once every keeper is in place
        out["moves"] = len(moves)
        if len(keepers) == shops_needed(fleet):
            out["drop"] = sorted(c["device_code"] for c in ctrls if c["device_code"] not in keepers
                                 and (c["device_code"] not in read or read[c["device_code"]] == [])
                                 and not any(x == c["device_code"] for x in slot.values()))
        left = [c for c in read if c not in keepers and c not in out.get("drop", []) and read[c]]
        out["consolidating"] = {"keep": keepers, "left": left}
        if moves or left:
            out["notes"].append(f"consolidating into {len(keepers)} shop(s): {len(moves)} listing(s) moved this pass, "
                                f"{len(left)} shop(s) still to empty")
    out["shops"] = [{"code": c["device_code"], "location": c.get("location"), "read": read.get(c["device_code"]) is not None,
                     "types": sorted(t for t, x in slot.items() if x == c["device_code"])} for c in ctrls]
    return out


def hours_text(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 60:.0f} min" if seconds >= 60 else f"{seconds:.0f} s"


class TradePostMixin:
    """Engine stage trade_post_pass: keep each trade post's listings in line with its stock settings."""

    async def trade_posts_state(self) -> dict:
        return await self.db.kv_get(KV, {}) or {}

    async def trade_post_supply(self, devices: list[dict]) -> dict[str, dict]:
        """Supply entries for contract_supply_pass: {key: event-like dict with _need, _skip, _tagged_only}."""
        from .contractsupply import contract_tag
        from .shapes import normalize_blueprints
        out: dict[str, dict] = {}
        fleets_ = [f for f in await self.fleets() if f.get("role") == ROLE and f.get("id")]
        if not fleets_:
            return out
        state = await self.trade_posts_state()
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        stuck = await self.stuck_prints(devices, bps)   # never printed: its components are locked (2026-11-13)
        p = pricing(await self.db.kv_get(KV_PRICING, {}))
        from .fleets import held
        for f in fleets_:
            if held(f):
                continue
            key = supply_key(f["id"])
            pl = plan(f, devices, None, state.get(f["id"]) or {}, bps, p, contract_tag(key))
            if not pl["location"]:
                continue
            from .shop import star_of
            out[key] = {"designation": key, "location": pl["location"], "star": star_of(pl["location"]), "status": "open",
                        "title": f"trade post {f.get('name') or f['id']}", "_need": {t: n for t, n in pl["need"].items() if n and t not in stuck},
                        "_skip": pl["skip"], "_tagged_only": True}
        return out

    async def trade_post_pass(self, force: bool = False) -> list[str]:
        from datetime import datetime, timezone

        from .api import ApiError
        from .contractsupply import contract_tag
        from .db import now_iso
        from .fleets import held
        from .loadouts import tag_step
        from .shapes import normalize_blueprints
        fleets_ = [f for f in await self.fleets() if f.get("role") == ROLE and f.get("id")]
        state = await self.trade_posts_state()
        if not fleets_ and not state:
            return []
        now = datetime.now(timezone.utc)
        devices = await self.devices()
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        p = pricing(await self.db.kv_get(KV_PRICING, {}))
        dry = (await self.settings()).get("dry_run")
        out: list[str] = []
        for fid in [k for k in state if k not in {f["id"] for f in fleets_}]:
            state.pop(fid, None)   # the fleet is gone (or no trade post any more): its stock is released by contract supply
        for f in fleets_:
            st = state.setdefault(f["id"], {})
            last = st.get("at")
            if not force and last:
                try:
                    if (now - datetime.fromisoformat(last)).total_seconds() < EVERY_S:
                        continue
                except ValueError:
                    pass
            st["at"] = now_iso()
            if held(f):
                st["note"] = "recalled: listings left as they are until Resume"
                continue
            tag = contract_tag(supply_key(f["id"]))
            ctrls = controllers(f, devices)
            trades: dict[str, list | None] = {}
            errs = []
            cache = await self.db.kv_get("shop_trades", {}) or {}   # the Shop page and the BobNet roll call read it
            fresh = {} if force else await fresh_listings(self.db, cache, [c["device_code"] for c in ctrls], now)
            for c in ctrls:
                if c["device_code"] in fresh:
                    trades[c["device_code"]] = fresh[c["device_code"]]
                    continue
                try:
                    res = await self.api.get(f"/devices/{c['device_code']}/trades")
                    trades[c["device_code"]] = (res or {}).get("trades") or []
                except ApiError as e:
                    trades[c["device_code"]] = None
                    errs.append(f"{c['device_code']}: {e.message}")
            st["read_error"] = "; ".join(errs) or None
            for code, tr in trades.items():
                if tr is not None and code not in fresh:
                    cache[code] = {"trades": tr, "at": now_iso()}
            await self.db.kv_set("shop_trades", cache)
            pl = plan(f, devices, trades, st, bps, p, tag)
            st["rows"] = [{k: r.get(k) for k in ("type", "listed", "initial", "here", "status", "trade_code", "listed_price",
                                                  "controller")}
                          for r in pl["rows"] if r.get("onhand") or r.get("listed") or r.get("trade_code")]
            st["location"] = pl["location"]
            st["controller"] = ctrls[0]["device_code"] if ctrls else None
            st["shops"] = pl["shops"]
            notes = list(pl["notes"])
            if errs:
                notes.append(f"could not read the listing: {st['read_error']}")
            need_shops = shops_needed(f)
            wants = f.get("wants") or {}
            if need_shops > max(1, int(wants.get("ami_trade_controller") or 0)) and not dry:   # Loadouts brings the extra shops
                items = await self.fleets()
                for x in items:
                    if x.get("id") == f["id"]:
                        x.setdefault("wants", {})["ami_trade_controller"] = need_shops
                await self.save_fleets(items)
                out.append(f"{f.get('name')}: {need_shops} AMI trade controllers wanted for "
                           f"{len(settings(f)['onhand'])} device types ({PER_SHOP} listings each)")
            nums = st["numbers"] = number_shops(st.get("numbers") or {}, [c["device_code"] for c in ctrls])
            from .shop import shop_config as have_config
            for c in ctrls:
                code = c["device_code"]
                if pl["location"] and c.get("location") != pl["location"]:
                    continue   # opened at the shop spot only, so its announcement points at the right place
                want = shop_config(f, nums[code], pl["location"])
                name = want["name"]
                is_open = (c.get("ami_directive") or {}).get("name") == "trade"
                if trades.get(code) is None or dry or (is_open and not config_differs(have_config(c), want)):
                    continue
                if is_open and trades.get(code):   # re-setting the directive may reset it: only while it lists nothing
                    continue
                ok, _, err = await self.send("POST", f"/devices/{code}",
                                             {"command": "set_directive", "directive": "trade",
                                              "configuration": want},
                                             f"trade post {f.get('name')}: {'rename' if is_open else 'open'} the shop "
                                             f"on {code} as {name}")
                if not ok:
                    notes.append(f"shop {code} not opened: {err}")
            listings = st.setdefault("listings", {})
            for a in pl["actions"] if not dry else []:
                t, code = a["type"], a["controller"]
                if a["delete"]:
                    ok, _, err = await self.send("DELETE", f"/devices/{a.get('from') or code}/trades/{a['delete']}", None,
                                                 f"trade post {f.get('name')}: take down the {t} listing"
                                                 + (f" on {a['from']} to list it on {code}" if a.get("from") else ""))
                    if not ok:
                        notes.append(f"{t}: delisting failed: {err}")
                        continue
                    listings.pop(t, None)
                    out.append(f"{f.get('name')}: delisted {t}")
                if a["create"]:
                    ok, res, err = await self.send("POST", f"/devices/{code}/trades", a["create"],
                                                   f"trade post {f.get('name')}: list {a['create']['stock']}× {t} for "
                                                   f"{price_text(a['price'])} on {code}")
                    if not ok:
                        notes.append(f"{t}: listing failed: {err}")
                        continue
                    listings[t] = {"trade_code": (res or {}).get("trade_code"), "stock": a["create"]["stock"],
                                   "escrow": a["escrow"], "price": a["price"], "at": now_iso(), "controller": code}
                    out.append(f"{f.get('name')}: listed {a['create']['stock']}× {t}")
            by = {d.get("device_code"): d for d in devices}
            busy = self.busy_devices(await self.jobs())
            for pin in pl["pins"]:   # the shop spot: Loadouts carries each controller there
                if pin["code"] in busy or dry:
                    continue
                if await self.create_job("trade_post", f"trade post {f.get('name')}: pin shop {pin['code']} to "
                                         f"{pl['location']}", None, [tag_step(pin["code"], pin["add"], pin["remove"] or None)],
                                         {"devices": [pin["code"]]}):
                    out.append(f"{f.get('name')}: shop pinned to {pl['location']}")
            loose = [by[c] for c in pl.get("unpin") or [] if c in by and c not in busy]
            if loose and not dry:
                steps = [tag_step(d["device_code"], ["spare"], [x for x in d.get("tags") or [] if x.startswith(("to:", "at:"))])
                         for d in loose]
                if await self.create_job("trade_post", f"trade post {f.get('name')}: {len(loose)} controller(s) no longer in "
                                         "the fleet unpinned, spare", None, steps, {"devices": [d["device_code"] for d in loose]}):
                    out.append(f"{f.get('name')}: {len(loose)} controller(s) left the fleet: spare")
            if st.get("consolidate") and not dry:
                await self._trade_post_consolidate(f, st, pl, by, busy, out)
            claims = st.setdefault("claims", {})
            gives = [g for g in pl["give"] if g["code"] in by and g["code"] not in busy]
            if gives and not dry:
                from .fleets import fleet_tag
                steps = [tag_step(g["code"], [fleet_tag(g["fleet"])],
                                  [x for x in g["tags"] if x in (tag, "spare", "stock", "component")
                                   or str(x).startswith(("to:", "at:", "fleet:", "home:"))] or None) for g in gives]
                if await self.create_job("trade_post", f"trade post {f.get('name')}: overage to "
                                         f"{', '.join(sorted({g['fleet'] for g in gives}))}: "
                                         f"{', '.join(g['code'] + ' ' + g['type'] for g in gives)}", None, steps,
                                         {"devices": [g["code"] for g in gives]}):
                    out.append(f"{f.get('name')}: overage handed over ({len(gives)})")
            if not dry:
                for t in pl["claims_done"]:
                    if not any(g["type"] == t and g not in gives for g in pl["give"]):
                        claims.pop(t, None)
            for t in list(claims):   # a claim the shop couldn't serve in 2 h is dropped: the fleet asks again or prints
                claims[t] = [x for x in claims[t] if (now - datetime.fromisoformat(x.get("at") or now_iso())).total_seconds() < 7200]
                if not claims[t]:
                    claims.pop(t)
            rel = [by[c] for c in pl["release"] if c in by and c not in busy]
            if rel and not dry:
                steps = [tag_step(d["device_code"], ["spare"], [x for x in d.get("tags") or []
                                                               if x == tag or x.startswith(("to:", "at:"))]) for d in rel]
                if await self.create_job("trade_post", f"trade post {f.get('name')}: {len(rel)} device(s) no longer "
                                         "stocked back to spare", None, steps, {"devices": [d["device_code"] for d in rel]}):
                    out.append(f"{f.get('name')}: released {len(rel)}")
            st["note"] = "; ".join(notes)
        await self.db.kv_set(KV, state)
        for line in out:
            await self.log("trade_post", line)
        return out

    async def _trade_post_consolidate(self, f: dict, st: dict, pl: dict, by: dict, busy: set, out: list[str]) -> None:
        """Consolidation's second half (owner 2026-11-22): the emptied shops close (clear_directive) and leave the fleet
        as spares, the fleet wants only the shops it needs, and once no other shop lists anything it is over."""
        from .db import now_iso
        from .engine_base import step
        from .fleets import fleet_tag
        from .loadouts import tag_step
        need = shops_needed(f)
        items = await self.fleets()
        for x in items:
            if x.get("id") == f["id"] and int((x.get("wants") or {}).get("ami_trade_controller") or 0) != need:
                x.setdefault("wants", {})["ami_trade_controller"] = need
                await self.save_fleets(items)
                out.append(f"{f.get('name')}: {need} AMI trade controllers wanted ({PER_SHOP} listings each)")
        drop = [by[c] for c in pl.get("drop") or [] if c in by and c not in busy]
        if drop:
            steps = []
            for d in drop:
                if (d.get("ami_directive") or {}).get("name"):
                    steps.append(step(f"{d['device_code']}: close the shop", f"/devices/{d['device_code']}",
                                      {"command": "clear_directive"}))
                steps.append(tag_step(d["device_code"], ["spare"],
                                      [x for x in d.get("tags") or [] if x == fleet_tag(f["id"]) or x.startswith(("to:", "at:"))]))
            if await self.create_job("trade_post", f"trade post {f.get('name')}: {len(drop)} emptied shop(s) closed, spare: "
                                     f"{', '.join(d['device_code'] for d in drop)}", None, steps,
                                     {"devices": [d["device_code"] for d in drop]}):
                out.append(f"{f.get('name')}: {len(drop)} emptied shop(s) closed and made spare")
                st.setdefault("consolidated", {"at": now_iso(), "freed": []})["freed"] += [d["device_code"] for d in drop]
        c = pl.get("consolidating") or {}
        if c.get("keep") and not c.get("left") and not pl.get("moves") and not pl.get("drop"):
            done = st.pop("consolidate")
            freed = (st.get("consolidated") or {}).get("freed") or []
            st["consolidated"] = {"at": now_iso(), "by": (done or {}).get("by"), "freed": freed, "shops": len(c["keep"])}
            out.append(f"{f.get('name')}: consolidated into {len(c['keep'])} shop(s), {len(freed)} controller(s) freed")

    async def shop_rollcall_text(self, devices: list[dict] | None = None, intro: str | None = None) -> str:
        from . import shop as sh
        devices = devices if devices is not None else await self.devices()
        cfg = sh.rollcall_settings(await self.db.kv_get(sh.ROLLCALL_KV, {}))
        return sh.roll_call(sh.shops(devices, {}, await self.db.kv_get("shop_trades", {}) or {}),
                            cfg["intro"] if intro is None else intro)

    async def shop_rollcall_default(self, devices: list[dict] | None = None) -> str:
        """The roll call's poster when none is chosen (Joe 2026-11-16): an active relay at a trade post's shop spot,
        else one in its system, as "relay:<code>" (no location shown); "" when no trade post has one."""
        from .chatter import RELAY_TYPES
        from .shop import star_of
        from .wardhub import active
        devices = devices if devices is not None else await self.devices()
        relays = [d for d in devices if d.get("device_type") in RELAY_TYPES and active(d)]
        for f in await self.fleets():
            if f.get("role") != ROLE:
                continue
            spot = settings(f)["spot"] or next((c.get("location") for c in controllers(f, devices)), None)
            if not spot:
                continue
            here = sorted((d for d in relays if star_of(d.get("location")) == star_of(spot)),
                          key=lambda d: (d.get("location") != spot, d.get("device_type") != "ftl_relay", d["device_code"]))
            if here:
                return f"relay:{here[0]['device_code']}"
        return ""

    async def shop_rollcall_post(self, by: str, channel: str, text: str, why: str) -> tuple[bool, str | None]:
        """Post the roll call as `by` (a replicant code, or "relay:<code>" to post through that relay with no location)
        and remember it (kv shop_rollcall.last)."""
        from . import shop as sh
        from .db import now_iso
        try:
            if by.startswith("relay:"):   # straight to a relay: BobNet shows no poster's star (Joe 2026-11-15)
                await self.api.post(f"/devices/{by[6:]}", {"command": "message", "channel": channel, "text": text},
                                    background=True)
            else:
                await self.api.post(f"/replicants/{by}/message", {"channel": channel, "text": text}, background=True)
            ok, err = True, None
        except Exception as e:
            ok, err = False, str(getattr(e, "message", None) or e)[:200]
        raw = await self.db.kv_get(sh.ROLLCALL_KV, {}) or {}
        raw["last"] = {"at": now_iso(), "by": by, "channel": channel, "text": text, "ok": ok, "error": err, "why": why}
        await self.db.kv_set(sh.ROLLCALL_KV, raw)
        await self.log("shop", f"BobNet shop roll call on {channel} by {by} ({why})" + ("" if ok else f" refused: {err}"),
                       "info" if ok else "warning")
        return ok, err

    async def shop_rollcall_pass(self) -> list[str]:
        """Engine stage: the shop roll call every `hours` (0 = by hand only), when there is something to list and a
        poster is chosen; a refused post waits the same interval."""
        from datetime import datetime, timezone

        from . import shop as sh
        cfg = sh.rollcall_settings(await self.db.kv_get(sh.ROLLCALL_KV, {}))
        if not cfg["hours"] or (await self.settings()).get("dry_run"):
            return []
        by = cfg["by"] or await self.shop_rollcall_default()
        if not by:
            return []
        last = (cfg["last"] or {}).get("at")
        if last:
            try:
                if (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < cfg["hours"] * 3600:
                    return []
            except ValueError:
                pass
        text = await self.shop_rollcall_text()
        if not text:
            return []
        ok, err = await self.shop_rollcall_post(by, cfg["channel"], text, f"every {cfg['hours']:g} h")
        return [f"shop roll call posted on {cfg['channel']}" if ok else f"shop roll call refused: {err}"]
