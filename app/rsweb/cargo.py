"""What a transport can load where it stands: stock at its location, hold capacity, free space,
and a "fill it up" allocation."""
from __future__ import annotations

import json
from typing import Any

from .shapes import as_amounts

# Capacities from the game docs, used when neither the device nor its blueprint reports one.
KNOWN_CAPACITY = {"transport_drone": 20, "transport_hauler": 80, "cargo_freighter": 500, "cargo_vessel": 200}


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def capacity_of(device: dict, blueprints: list[dict], last_event: dict | None) -> float | None:
    for v in (device.get("cargo_capacity"), (last_event or {}).get("cargo_capacity")):
        if _num(v):
            return _num(v)
    bp = next((b for b in blueprints if b.get("device_type") == device.get("device_type")), {})
    if _num(bp.get("cargo_capacity")):
        return _num(bp["cargo_capacity"])
    return KNOWN_CAPACITY.get(device.get("device_type") or "")


def used_of(device: dict, last_event: dict | None) -> tuple[float, dict[str, float]]:
    """(total units in the hold, per-resource breakdown if known)."""
    for key in ("cargo", "cargo_contents", "hold", "inventory"):
        if key in device and device[key] is not None:
            amounts = as_amounts(device[key])
            return sum(amounts.values()), amounts
    if last_event:
        after = last_event.get("cargo_after")
        if isinstance(after, (int, float)):
            return float(after), {}
        amounts = as_amounts(after)
        if amounts:
            return sum(amounts.values()), amounts
    return 0.0, {}


def fill_plan(available: dict[str, float], free: float) -> dict[str, int]:
    """Split `free` units across what's available, in proportion to stock, whole units only."""
    avail = {r: int(q) for r, q in available.items() if q and q >= 1}
    total = sum(avail.values())
    room = int(free)
    if room <= 0 or total <= 0:
        return {}
    if total <= room:
        return avail
    plan = {r: min(q, int(room * q / total)) for r, q in avail.items()}
    left = room - sum(plan.values())
    for r in sorted(avail, key=lambda r: avail[r] - plan[r], reverse=True):  # top up from the biggest remaining stock
        if left <= 0:
            break
        add = min(left, avail[r] - plan[r])
        plan[r] += add
        left -= add
    return {r: q for r, q in plan.items() if q > 0}


async def cargo_context(db, api, device: dict, blueprints: list[dict]) -> dict:
    code, loc = device.get("device_code"), device.get("location")
    # fresh device detail (may carry cargo fields the list doesn't)
    try:
        device = {**device, **(await api.request("GET", f"/devices/{code}") or {})}
        loc = device.get("location") or loc
    except Exception:
        pass
    row = await db.fetchone("SELECT payload FROM events WHERE device_code=? AND event IN "
                            "('transport.collected','transport.delivered') ORDER BY seq DESC LIMIT 1", (code,))
    last = json.loads(row["payload"]) if row else None
    # stock at the device's location: live if possible, else the last sync
    available: dict[str, float] = {}
    stale = False
    try:
        body = await api.request("GET", "/inventory", params={"location": loc, "limit": 50})
        for item in (body or {}).get("locations") or []:
            if item.get("location") == loc:
                available = as_amounts(item.get("items"))
    except Exception:
        stale = True
        for item in await db.kv_get("inventory", []) or []:
            if item.get("location") == loc:
                available = as_amounts(item.get("items"))
    cap = capacity_of(device, blueprints, last)
    used, hold = used_of(device, last)
    free = max(0.0, cap - used) if cap is not None else None
    plan = fill_plan(available, free) if free is not None else {}
    return {"location": loc, "available": available, "capacity": cap, "used": used, "hold": hold,
            "free": free, "plan": plan, "stale": stale}
