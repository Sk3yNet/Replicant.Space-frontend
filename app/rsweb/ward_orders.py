"""Ward orders (Map › Prospects: Add ward): get one of your system wards working in a system — activate one that's
already there, else send a spare one (tagged to:<star>, the loadout pass carries it within the supply range), else
print one on the nearest autofactory in range (tagged the same way). The engine stage ward_orders_pass activates it
once it's deployed there, within the 25-ward cap and never alongside a hub of yours. kv "ward_orders":
{star: {how: activate|send|print, device, factory, at, note}}; an order not done in 3 days is dropped.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import loadouts as lo
from . import wardhub as wh

KV = "ward_orders"
EXPIRE_DAYS = 3


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def ward_here(devices: list[dict], star: str) -> dict | None:
    """One of your wards deployed in `star` (warding first)."""
    ws = [d for d in devices if wh.is_ward(d) and wh.deployed(d) and star_of(d.get("location")) == star]
    return sorted(ws, key=lambda d: (not wh.active(d), d.get("device_code") or ""))[0] if ws else None


def spare_ward(devices: list[dict], star: str, busy: set[str], pos: dict, reach: float) -> dict | None:
    """A ward of yours that isn't working and belongs to no fleet or contract, nearest first, within reach."""
    out = []
    for d in devices:
        tags = d.get("tags") or []
        if (not wh.is_ward(d) or wh.active(d) or d.get("device_code") in busy or d.get("location_stale")
                or any(t.startswith(("fleet:", "contract:", "to:", "boot:")) for t in tags)):
            continue
        here = star_of(d.get("location"))
        if not here:
            host = next((x for x in devices if x.get("device_code") == (d.get("stowed_in_device_code") or "")), {})
            here = star_of(host.get("location"))
        dist = lo._dist(here, star, pos) if here else 1e9
        if reach and reach < dist < 1e9:
            continue
        out.append((dist, d.get("device_code"), d))
    return min(out, key=lambda x: x[:2])[2] if out else None


def factory_near(devices: list[dict], star: str, busy: set[str], pos: dict, reach: float) -> dict | None:
    out = []
    for f in devices:
        if not lo.is_factory(f) or f.get("device_code") in busy or f.get("location_stale") or not f.get("location") \
                or f.get("in_control_range") is False or any(t.startswith("boot:") for t in f.get("tags") or []):
            continue
        dist = lo._dist(star_of(f["location"]), star, pos)
        if reach and reach < dist < 1e9:
            continue
        out.append((dist, f["device_code"], f))
    return min(out, key=lambda x: x[:2])[2] if out else None


def expired(order: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        at = datetime.fromisoformat(str(order.get("at")))
    except ValueError:
        return True
    return now - at > timedelta(days=EXPIRE_DAYS)
