"""Our system hubs and system wards: Map › Wards & hubs.

From the game's docs (API_REFERENCE.md §9):
  • System hub — `activate`; a 7-day shield after activation, then −10 % capacity a day without maintenance.
    Events: hub.activated {star, location}, hub.maintained {resources_consumed, capacity},
    hub.warning {capacity, warning_type}, hub.destroyed {star, location}.
  • System ward — `activate` (the response may include `evicted_miners`) / `deactivate`; at most 25 per account;
    incompatible with hubs (one system doesn't take both).
How a hub's maintenance draws its resources isn't documented. `hub.maintained` says what it consumed, so the tracker
shows that next to the stockpile in the hub's system, and warns when the system holds less than one maintenance's worth.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

WARD_CAP = 25
SHIELD_DAYS = 7
DECAY_PER_DAY = 10.0          # % of capacity a day once the shield is down and nothing maintains it
EVICTIONS_KV = "ward_evictions"


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _cap(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x * 100 if 0 < x <= 1 else x


def is_hub(d: dict) -> bool:
    return d.get("device_type") == "system_hub"


def is_ward(d: dict) -> bool:
    return d.get("device_type") == "system_ward"


def deployed(d: dict) -> bool:
    """Placed in a system (not stowed aboard something, not folded up)."""
    st = str(d.get("status") or "")
    return bool(d.get("location")) and not d.get("stowed_in_device_code") and not st.startswith(("stowed", "compact"))


def active(d: dict) -> bool:
    """A ward that's warding / a hub that's running: deployed and not inactive."""
    st = str(d.get("status") or "")
    return deployed(d) and st not in ("inactive", "idle", "") and not st.startswith(("travel", "cruis", "surg", "unfurl"))


def _payload(row: dict) -> dict:
    try:
        return json.loads(row.get("payload") or "{}") or {}
    except (TypeError, ValueError):
        return {}


def hubs(devices: list[dict], hub_events: list[dict], stock: dict[str, dict[str, float]],
         now: datetime | None = None) -> list[dict]:
    """One row per hub of ours: its shield and upkeep state.
    `hub_events`: stored hub.* events ({event, device_code, star, location, payload, created_at}), oldest first.
    `stock`: {star: {resource: qty}} — the stockpile in each system."""
    now = now or datetime.now(timezone.utc)
    by_dev: dict[str, list[dict]] = {}
    for e in hub_events:
        by_dev.setdefault(e.get("device_code") or "", []).append(e)
    out = []
    for d in devices:
        if not is_hub(d):
            continue
        code, star = d.get("device_code"), star_of(d.get("location"))
        evs = by_dev.get(code, []) + [e for e in by_dev.get("", []) if star and star_of(e.get("location")) == star]
        evs.sort(key=lambda e: str(e.get("created_at") or ""))
        act = next((e for e in reversed(evs) if e.get("event") == "hub.activated"), None)
        maint = next((e for e in reversed(evs) if e.get("event") == "hub.maintained"), None)
        warn = next((e for e in reversed(evs) if e.get("event") == "hub.warning"), None)
        activated_at = _ts(act.get("created_at")) if act else None
        shield_until = activated_at + timedelta(days=SHIELD_DAYS) if activated_at else None
        mp, wp = _payload(maint) if maint else {}, _payload(warn) if warn else {}
        consumed = {k: float(v) for k, v in ((mp.get("resources_consumed") or {}).items()
                                              if isinstance(mp.get("resources_consumed"), dict) else []) if _num(v)}
        dev_cap = _cap(d.get("operational_capacity"))
        reported = [(_ts(e.get("created_at")), _cap(_payload(e).get("capacity"))) for e in (maint, warn) if e]
        reported = [r for r in reported if r[0] and r[1] is not None]
        last_cap_at, last_cap = max(reported, key=lambda r: r[0]) if reported else (None, None)
        shielded = bool(shield_until and now < shield_until)
        # the hub's capacity: the latest the game reported (hub.maintained / hub.warning), less 10 %/day since then
        # once the shield is down; without a report, the device's own operational capacity
        cap = last_cap if last_cap is not None else dev_cap
        projected = cap
        if not shielded and last_cap is not None:
            since = max(last_cap_at, shield_until) if shield_until else last_cap_at
            projected = max(0.0, last_cap - DECAY_PER_DAY * max(0.0, (now - since).total_seconds() / 86400))
        days_left = (projected / DECAY_PER_DAY) if (projected is not None and not shielded) else None
        here = stock.get(star) or {}
        short = {k: round(v - float(here.get(k) or 0), 1) for k, v in consumed.items() if float(here.get(k) or 0) < v}
        level = "ok"
        if not shielded and projected is not None and projected < 50:
            level = "alert"
        elif (not shielded and consumed and short) or (warn and (not maint or str(warn.get("created_at")) > str(maint.get("created_at")))):
            level = "warn"
        out.append({"code": code, "star": star, "location": d.get("location"), "status": d.get("status"), "name": d.get("name"),
                    "capacity": cap, "projected": None if projected is None else round(projected, 1),
                    "activated_at": activated_at.isoformat() if activated_at else None,
                    "shield_until": shield_until.isoformat() if shield_until else None, "shielded": shielded,
                    "shield_left_h": round((shield_until - now).total_seconds() / 3600, 1) if shielded else None,
                    "last_maintained": maint.get("created_at") if maint else None, "consumed": consumed,
                    "last_warning": ({"at": warn.get("created_at"), "type": wp.get("warning_type"),
                                      "capacity": _cap(wp.get("capacity"))} if warn else None),
                    "days_left": None if days_left is None else round(days_left, 1),
                    "stock": {k: here.get(k, 0) for k in consumed} if consumed else {}, "short": short, "level": level,
                    "ward_too": False})
    return out


def _num(v: Any) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def wards(devices: list[dict]) -> dict:
    """Our wards against the 25 cap: {rows, active, total, cap, free, clash: [stars with a hub of ours too]}."""
    rows = []
    hub_stars = {star_of(d.get("location")) for d in devices if is_hub(d) and deployed(d)}
    for d in devices:
        if not is_ward(d):
            continue
        star = star_of(d.get("location"))
        rows.append({"code": d.get("device_code"), "star": star if deployed(d) else None, "location": d.get("location"),
                     "status": d.get("status"), "name": d.get("name"), "warding": active(d), "deployed": deployed(d),
                     "carrier": d.get("stowed_in_device_code"), "capacity": _cap(d.get("operational_capacity")),
                     "commands": d.get("available_commands") or [], "hub_here": deployed(d) and star in hub_stars})
    rows.sort(key=lambda r: (not r["warding"], not r["deployed"], r["star"] or "", r["code"] or ""))
    n_active = sum(1 for r in rows if r["warding"])
    return {"rows": rows, "active": n_active, "total": len(rows), "cap": WARD_CAP, "free": max(0, WARD_CAP - n_active),
            "clash": sorted({r["star"] for r in rows if r["hub_here"]})}


def evicted(resp: Any) -> list[dict]:
    """`evicted_miners` from a ward's activate answer, as [{replicant/owner, device, ...}] (shape undocumented: a list
    of codes or of objects)."""
    ev = (resp or {}).get("evicted_miners") if isinstance(resp, dict) else None
    if not ev:
        return []
    out = []
    for x in ev if isinstance(ev, list) else [ev]:
        out.append(x if isinstance(x, dict) else {"device_code": str(x)})
    return out


def eviction_text(rows: list[dict]) -> str:
    bits = []
    for r in rows:
        who = r.get("owner_name") or r.get("replicant_name") or r.get("owner_replicant_code") or r.get("replicant_code")
        what = r.get("device_code") or r.get("code") or "?"
        bits.append(f"{what}" + (f" ({who})" if who else ""))
    return ", ".join(bits)


def stock_by_star(inventory: list[dict]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for loc in inventory:
        for k, v in (loc.get("items") or {}).items():
            try:
                out[star_of(loc.get("location"))][k] += float(v or 0)
            except (TypeError, ValueError):
                pass
    return {s: dict(v) for s, v in out.items()}


async def hub_events(db) -> list[dict]:
    rows = await db.fetchall("SELECT event, device_code, star, location, payload, created_at FROM events "
                             "WHERE event LIKE 'hub.%' ORDER BY seq")
    return [dict(r) for r in rows]
