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
PER_SHOP = 5                   # the game caps a trade controller at 5 listings (owner 2026-11-13)


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
    return {"onhand": onhand, "prices": {t: p for t, p in prices.items() if p}, "spot": spot}


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
    skip: [codes], notes, pins: [{code, add, remove}], shops: [{code, location, types, read}]}."""
    ctrls = controllers(fleet, devices)
    ctrl = ctrls[0] if ctrls else None
    if isinstance(trades, list) or trades is None:
        trades = {ctrl["device_code"]: trades} if ctrl else {}
    spot = settings(fleet)["spot"]
    out: dict[str, Any] = {"rows": rows(fleet, bps, p), "actions": [], "release": [], "need": {}, "skip": [],
                           "notes": [], "location": spot or (ctrl or {}).get("location"), "pin": None, "pins": [],
                           "shops": []}
    if not ctrl:
        out["notes"].append("no AMI trade controller in the fleet")
        return out
    loc = out["location"]
    from .loadouts import at_tag
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
    away = not ready
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
    slot: dict[str, str] = {}
    for r in sorted(out["rows"], key=lambda r: (-value(r["price"]), r["type"])):
        t = r["type"]
        if not r["onhand"]:
            continue
        if t in cur_of:
            slot[t] = cur_of[t][0]
            continue
        free = next((c for c in sorted(read) if read[c] is not None and used[c] < PER_SHOP), None)
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
        known = c is not None and read.get(c) is not None
        listed = int((cur or {}).get("current_stock") or 0) if known else (int(mine.get("stock") or 0) if t in cur_of else 0)
        esc = {x for x in mine.get("escrow") or []}
        if known and not cur:
            esc = set()
        same = [d for d in at_loc if d.get("device_type") == t and d.get("device_code") not in esc
                and not d.get("location_stale")]
        loose = sorted((d for d in same if tag in (d.get("tags") or [])), key=lambda d: d["device_code"])
        others = [d for d in same if tag not in (d.get("tags") or [])]
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
        repriced = bool(cur) and listed > 0 and {k: int(v) for k, v in r["listed_price"].items()} != price
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
            for c in ctrls:
                try:
                    res = await self.api.get(f"/devices/{c['device_code']}/trades")
                    trades[c["device_code"]] = (res or {}).get("trades") or []
                except ApiError as e:
                    trades[c["device_code"]] = None
                    errs.append(f"{c['device_code']}: {e.message}")
            st["read_error"] = "; ".join(errs) or None
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
            from .shop import shop_config
            for c in ctrls:
                code = c["device_code"]
                name = shop_name(f, nums[code])
                is_open = (c.get("ami_directive") or {}).get("name") == "trade"
                if trades.get(code) is None or dry or (is_open and shop_config(c).get("name") == name):
                    continue
                if is_open and trades.get(code):   # renaming resets the directive: only while it lists nothing
                    continue
                ok, _, err = await self.send("POST", f"/devices/{code}",
                                             {"command": "set_directive", "directive": "trade",
                                              "configuration": {"name": name}},
                                             f"trade post {f.get('name')}: {'rename' if is_open else 'open'} the shop "
                                             f"on {code} as {name}")
                if not ok:
                    notes.append(f"shop {code} not opened: {err}")
            listings = st.setdefault("listings", {})
            for a in pl["actions"] if not dry else []:
                t, code = a["type"], a["controller"]
                if a["delete"]:
                    ok, _, err = await self.send("DELETE", f"/devices/{code}/trades/{a['delete']}", None,
                                                 f"trade post {f.get('name')}: take down the {t} listing")
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
