"""Scheduled deliveries to megastructures (Joe 2026-11-20).

A megastructure (STAR-OBJ-n, e.g. "Frontier Assembly" at LAOWAOI-OBJ-2) takes devices only, given at its location
(POST /locations/{code}/contribute). An order (kv "mega_orders", {location: order}) says what to bring:

    types      {device_type: n}  how many to give; 0 means everything the structure still needs of that type
    print      print what no spare covers (nearest autofactory within the sector range); off by default
    status     active | paused | done
    given      {device_type: n}  contributed for this order so far (devices that left your list after a contribute)
    pending    {code: {type, at}}  sent in a contribute job, not yet seen gone
    want       {device_type: n}  still to bring (last check), the supply's need
    remaining  {device_type: n}  what the structure still needs (game, last read)

The device side is the contract supply (contractsupply.py, engine contract_supply_pass, key mega:<location>): spares
nearest first are tagged to:<star> at:<location> contract:mega:<location> and the loadouts pass carries them there.
Engine stage mega_order_pass reads the structure every 15 minutes while an order is active and contributes the order's
devices once they sit at the location. Paused keeps tagged devices where they are; done (or removed) makes the
leftovers spare again.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

KV = "mega_orders"
READ_S = 900            # how old a kept read of the structure may be
PENDING_MIN = 30        # a contributed device still listed after this long was refused: free to try again


def key(loc: str) -> str:
    return f"mega:{str(loc).upper()}"


def _ts(s: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def mega_of(data: Any) -> dict:
    return ((data or {}).get("megastructure") or {}) if isinstance(data, dict) else {}


def remaining(mega: dict) -> dict[str, int]:
    """{type: still needed} from the structure's requirements (current fields, or the docs' needed / contributed)."""
    out = {}
    for t, r in (mega.get("requirements") or {}).items():
        if not isinstance(r, dict):
            continue
        if r.get("remaining") is not None:
            n = r["remaining"]
        else:
            need = r.get("required") if r.get("required") is not None else r.get("needed")
            have = r.get("current") if r.get("current") is not None else r.get("contributed")
            n = int(need or 0) - int(have or 0)
        out[t] = max(0, int(n or 0))
    return out


def title(mega: dict, loc: str) -> str:
    return str(mega.get("title") or mega.get("name") or loc)


def new_order(loc: str, types: dict[str, int], print_: bool, mega: dict, now: str) -> dict:
    return {"location": str(loc).upper(), "title": title(mega, loc), "types": {t: max(0, int(n)) for t, n in types.items()},
            "print": bool(print_), "status": "active", "given": {}, "pending": {}, "created_at": now,
            "remaining": remaining(mega) if mega else {}, "want": {}}


def settle(order: dict, devices: list[dict], now: float | None = None) -> None:
    """Contributed devices gone from the list count as given; ones still listed long after were refused."""
    now = now or datetime.now(timezone.utc).timestamp()
    listed = {d.get("device_code") for d in devices}
    given = order.setdefault("given", {})
    pend = order.setdefault("pending", {})
    for c, p in list(pend.items()):
        if c not in listed:
            given[p.get("type")] = int(given.get(p.get("type")) or 0) + 1
            pend.pop(c)
        elif (_ts(p.get("at")) or 0) < now - PENDING_MIN * 60:
            pend.pop(c)


def want(order: dict) -> dict[str, int]:
    """Still to bring per type: the order's amount less what was given, never more than the structure still needs."""
    rem = order.get("remaining") or {}
    given = order.get("given") or {}
    out = {}
    for t, n in (order.get("types") or {}).items():
        left = rem.get(t) if not n else max(0, int(n) - int(given.get(t) or 0))
        if left is None:
            continue   # everything it needs, and the structure wasn't read yet
        if t in rem:
            left = min(left, rem[t])
        if left > 0:
            out[t] = left
    return out


def finished(order: dict) -> bool:
    """Nothing left to bring, judged on a read of the structure (an unread one is never finished)."""
    return bool(order.get("read_at")) and not want(order) and not order.get("pending")


def to_contribute(order: dict, devices: list[dict], tag: str, busy: set[str]) -> list[dict]:
    """The order's devices sitting at the structure, idle, not already given, up to what is still wanted."""
    loc = order["location"]
    w = dict(want(order))
    for p in (order.get("pending") or {}).values():
        if p.get("type") in w:
            w[p["type"]] -= 1
    out = []
    for d in sorted(devices, key=lambda d: d.get("device_code") or ""):
        t = d.get("device_type")
        if (d.get("location") == loc and tag in (d.get("tags") or []) and w.get(t, 0) > 0
                and d.get("device_code") not in busy and d.get("device_code") not in (order.get("pending") or {})
                and not d.get("hosting_replicant") and not d.get("stowed_in")):
            out.append(d)
            w[t] -= 1
    return out


def user_fields(o: dict) -> dict:
    """What the page changes on an order."""
    return {k: o.get(k) for k in ("types", "print", "status")}


def supply_entry(order: dict) -> dict:
    """The contract supply's event-like entry (see engine_fleets.contract_supply_pass)."""
    from .shop import star_of
    loc = order["location"]
    return {"designation": key(loc), "location": loc, "star": star_of(loc), "status": "open",
            "title": f"megastructure {order.get('title') or loc}",
            "_need": {} if order.get("status") == "paused" else want(order),
            "_tagged_only": True, "_no_print": not order.get("print")}


class MegaOrdersMixin:
    """Engine stage mega_order_pass, and the supply entries contract_supply_pass serves."""

    async def mega_order_supply(self) -> dict[str, dict]:
        orders = await self.db.kv_get(KV, {}) or {}
        return {key(loc): supply_entry(o) for loc, o in orders.items() if o.get("status") in ("active", "paused")}

    async def mega_order_pass(self, force: bool = False) -> list[str]:
        from . import locread
        from .api import ApiError
        from .contractsupply import contract_tag
        from .db import now_iso
        from .engine_base import step
        orders = await self.db.kv_get(KV, {}) or {}
        if not any(o.get("status") == "active" for o in orders.values()):
            return []
        last = _ts(await self.db.kv_get("mega_orders_last", None))
        if not force and last and datetime.now(timezone.utc).timestamp() - last < 240:
            return []
        await self.db.kv_set("mega_orders_last", now_iso())
        devices = await self.devices()
        busy = self.busy_devices(await self.jobs())
        out: list[str] = []
        user0 = {loc: user_fields(o) for loc, o in orders.items()}
        for loc, o in orders.items():
            if o.get("status") != "active":
                continue
            settle(o, devices)
            try:
                data = await locread.read(self.db, self.api, loc, READ_S, background=True)
            except ApiError as e:
                o["error"] = e.message
                continue
            mega = mega_of(data)
            if mega.get("requirements") is not None:
                o.update(remaining=remaining(mega), title=title(mega, loc), progress=mega.get("progress_percentage"),
                         read_at=now_iso())
                o.pop("error", None)
            tag = contract_tag(key(loc))
            give = to_contribute(o, devices, tag, busy)
            if give:
                codes = [d["device_code"] for d in give]
                if await self.create_job("contracts", f"megastructure {o.get('title') or loc}: contribute {len(codes)} device(s)",
                                         None, [step(f"contribute {', '.join(codes)} to {loc}", f"/locations/{loc}/contribute",
                                                     {"devices": codes})], {"devices": codes, "megastructure": loc}):
                    for d in give:
                        o.setdefault("pending", {})[d["device_code"]] = {"type": d.get("device_type"), "at": now_iso()}
                    busy |= set(codes)
                    out.append(f"{loc}: contributing {len(codes)}")
            o["want"] = want(o)
            o["checked_at"] = now_iso()
            if finished(o):
                o.update(status="done", done_at=now_iso())
                gave = ", ".join(f"{n} {t.replace('_', ' ')}" for t, n in sorted((o.get("given") or {}).items()) if n)
                await self.log("contracts", f"megastructure delivery to {o.get('title') or loc} ({loc}) finished"
                                            + (f": gave {gave}" if gave else ""), notify=True)
        # edits made on the page while this pass read the game (pause, remove, amounts) win over this pass's copy
        fresh = await self.db.kv_get(KV, {}) or {}
        merged = {}
        for loc, f in fresh.items():
            o = orders.get(loc)
            if not o or o.get("created_at") != f.get("created_at"):
                merged[loc] = f
            else:
                merged[loc] = {**o, **{k: v for k, v in user_fields(f).items() if v != user0[loc].get(k)}}
        await self.db.kv_set(KV, merged)
        return out
