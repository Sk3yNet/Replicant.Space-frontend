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
    return {"onhand": onhand, "prices": {t: p for t, p in prices.items() if p}}


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


def controller(fleet: dict, devices: list[dict]) -> dict | None:
    from .fleets import members
    from .shop import is_shop
    return next((d for d in sorted(members(fleet, devices), key=lambda x: x.get("device_code") or "")
                 if is_shop(d) and d.get("location")), None)


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


def plan(fleet: dict, devices: list[dict], trades: list[dict] | None, state: dict, bps: dict[str, dict],
         p: dict, tag: str) -> dict:
    """What to list, delist and supply. Pure. `trades`: the shop's listing as just read (None: unknown).
    Returns {rows, actions: [{type, delete, create, escrow}], release: [codes], need: {type: n}, skip: [codes], notes}."""
    ctrl = controller(fleet, devices)
    out: dict[str, Any] = {"rows": rows(fleet, bps, p), "actions": [], "release": [], "need": {}, "skip": [],
                           "notes": [], "location": (ctrl or {}).get("location")}
    if not ctrl:
        out["notes"].append("no AMI trade controller in the fleet")
        return out
    loc = ctrl["location"]
    listings = state.get("listings") or {}
    at_loc = [d for d in devices if d.get("location") == loc and not d.get("stowed_in_device_code")
              and not d.get("attached_to_device_code")]
    for r in out["rows"]:
        t = r["type"]
        mine = listings.get(t) or {}
        cur = listing_for(trades or [], t, mine.get("trade_code")) if trades is not None else None
        listed = int((cur or {}).get("current_stock") or 0) if trades is not None else int(mine.get("stock") or 0)
        esc = {c for c in mine.get("escrow") or []}
        if trades is not None and not cur:
            esc = set()
        same = [d for d in at_loc if d.get("device_type") == t and d.get("device_code") not in esc
                and not d.get("location_stale")]
        loose = sorted((d for d in same if tag in (d.get("tags") or [])), key=lambda d: d["device_code"])
        others = [d for d in same if tag not in (d.get("tags") or [])]
        out["skip"] += sorted(c for c in esc if c)
        r.update({"listed": listed, "initial": int((cur or {}).get("initial_stock") or 0), "here": len(loose),
                  "trade_code": (cur or {}).get("trade_code"), "listed_price": as_amounts(((cur or {}).get("criteria") or {}).get("resources")),
                  "status": ""})
        want = r["onhand"]
        if not want:
            if cur:
                out["actions"].append({"type": t, "delete": cur["trade_code"], "create": None, "escrow": []})
                r["status"] = "delisting"
            out["release"] += [d["device_code"] for d in loose]
            continue
        out["need"][t] = max(0, want - listed)
        if trades is None:
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
            out["actions"].append({"type": t, "delete": (cur or {}).get("trade_code"),
                                   "create": {"name": t.replace("_", " ").capitalize(), "stock": stock,
                                              "criteria": {"resources": price, "devices": {}},
                                              "rewards": {"resources": {}, "devices": {t: 1}}},
                                   "escrow": sorted(esc | {d["device_code"] for d in loose}), "price": price})
            r["status"] = "repricing" if repriced and not loose else "restocking"
        elif listed < want:
            r["status"] = f"{len(loose)} on hand, waiting for {want - listed - len(loose)} more" if loose else "supplying"
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
                        "title": f"trade post {f.get('name') or f['id']}", "_need": {t: n for t, n in pl["need"].items() if n},
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
            ctrl = controller(f, devices)
            trades = None
            if ctrl:
                try:
                    res = await self.api.get(f"/devices/{ctrl['device_code']}/trades")
                    trades = (res or {}).get("trades") or []
                    st["read_error"] = None
                except ApiError as e:
                    st["read_error"] = e.message
            pl = plan(f, devices, trades, st, bps, p, tag)
            st["rows"] = [{k: r.get(k) for k in ("type", "listed", "initial", "here", "status", "trade_code", "listed_price")}
                          for r in pl["rows"] if r.get("onhand") or r.get("listed") or r.get("trade_code")]
            st["location"] = pl["location"]
            st["controller"] = (ctrl or {}).get("device_code")
            notes = list(pl["notes"])
            if ctrl and trades is None:
                notes.append(f"could not read the listing: {st.get('read_error')}")
            if ctrl and trades is not None and not (ctrl.get("ami_directive") or {}).get("name") == "trade" and not dry:
                ok, _, err = await self.send("POST", f"/devices/{ctrl['device_code']}",
                                             {"command": "set_directive", "directive": "trade",
                                              "configuration": {"name": f.get("name") or "Trade post"}},
                                             f"trade post {f.get('name')}: open the shop on {ctrl['device_code']}")
                if not ok:
                    notes.append(f"shop not opened: {err}")
            listings = st.setdefault("listings", {})
            for a in pl["actions"] if not dry else []:
                t = a["type"]
                if a["delete"]:
                    ok, _, err = await self.send("DELETE", f"/devices/{ctrl['device_code']}/trades/{a['delete']}", None,
                                                 f"trade post {f.get('name')}: take down the {t} listing")
                    if not ok:
                        notes.append(f"{t}: delisting failed: {err}")
                        continue
                    listings.pop(t, None)
                    out.append(f"{f.get('name')}: delisted {t}")
                if a["create"]:
                    ok, res, err = await self.send("POST", f"/devices/{ctrl['device_code']}/trades", a["create"],
                                                   f"trade post {f.get('name')}: list {a['create']['stock']}× {t} for "
                                                   f"{price_text(a['price'])}")
                    if not ok:
                        notes.append(f"{t}: listing failed: {err}")
                        continue
                    listings[t] = {"trade_code": (res or {}).get("trade_code"), "stock": a["create"]["stock"],
                                   "escrow": a["escrow"], "price": a["price"], "at": now_iso()}
                    out.append(f"{f.get('name')}: listed {a['create']['stock']}× {t}")
            by = {d.get("device_code"): d for d in devices}
            busy = self.busy_devices(await self.jobs())
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
