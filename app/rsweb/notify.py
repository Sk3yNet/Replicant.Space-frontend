"""Turn raw game events into human text, notifications and the since-last-visit digest."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import DB, now_iso, row_event

# level: alert = needs attention, done = something was accomplished, info = everything else.
ALERT_EVENTS = {
    "hub.warning", "hub.destroyed", "system.object_detected", "system.devices_halted",
    "diversion.impacted", "teleport.failed", "triangulation.failed", "site.depleted",
    "salvage.depleted", "directive.paused", "simulation.expired",
}
DONE_EVENTS = {
    "print.completed", "travel.arrived", "scan.completed", "search.completed", "directive.completed",
    "event.completed", "trade.completed", "prospect.completed", "teleport.completed",
    "story.awakened", "megastructure.contributed", "relay.activated", "hub.activated",
    "diversion.diverted", "salvage.discovered", "event.discovered", "triangulation.complete",
    "simulation.completed", "device.decommissioned", "ward.activated",
}
# Done-events that also raise a notification (the rest just go in the digest/feed).
NOTIFY_DONE = {
    "print.completed", "directive.completed", "event.completed", "trade.completed", "prospect.completed",
    "teleport.completed", "story.awakened", "salvage.discovered", "event.discovered",
    "triangulation.complete", "simulation.completed", "hub.activated", "relay.activated",
}
NOISY = {"ami.mining.digest", "ami.survey.digest", "ami.transport.digest", "bobnet.new", "experience.gained"}


def level_of(event: str) -> str:
    if event in ALERT_EVENTS:
        return "alert"
    if event in DONE_EVENTS:
        return "done"
    return "info"


def _res(d: Any) -> str:
    if not isinstance(d, dict) or not d:
        return ""
    return ", ".join(f"{v} {k}" for k, v in d.items())


def describe(ev: dict) -> str:
    """One-line human description of an event."""
    e = ev.get("event", "")
    p = ev.get("payload") or {}
    dev = ev.get("device_type") or "device"
    dc = ev.get("device_code") or ""
    who = f"{dev.replace('_', ' ')} {dc}".strip()
    loc = ev.get("location") or p.get("location") or ""
    match e:
        case "travel.departed":
            return f"{who} departed {p.get('origin', '?')} → {p.get('destination', '?')} ({p.get('travel_type', '')})"
        case "travel.arrived":
            return f"{who} arrived at {p.get('destination', loc)}"
        case "travel.cancelled":
            return f"{who} cancelled travel to {p.get('destination', '?')}"
        case "print.started":
            return f"Printing {p.get('device_type', '?')} at {loc}"
        case "print.completed":
            return f"Printed {p.get('device_type', '?')} {p.get('new_device_code', '')} at {loc}"
        case "mining.started":
            return f"{who} mining {p.get('resource_type', '?')} at {p.get('site') or loc}"
        case "mining.stopped":
            return f"{who} stopped mining {p.get('resource_type', '?')} ({p.get('quantity_mined', 0)} mined)"
        case "mining.retargeted":
            return f"{who} switched {p.get('old_resource')} → {p.get('new_resource')}"
        case "scan.started" | "search.started":
            return f"{who} started {p.get('scan_type') or p.get('search_type') or 'scan'} of {p.get('scan_target') or p.get('search_target') or loc}"
        case "scan.completed" | "search.completed":
            return f"{who} finished {p.get('scan_type') or p.get('search_type') or 'scan'} of {p.get('scan_target') or p.get('search_target') or loc}"
        case "transport.collected":
            return f"{who} collected {_res(p.get('resources'))}"
        case "transport.delivered":
            return f"{who} delivered {_res(p.get('resources'))}"
        case "experience.gained":
            return f"+{p.get('amount', 0)} XP ({p.get('source', '')})"
        case "message.new":
            return f"Message: {p.get('title', '')}"
        case "bobnet.new":
            return f"[{p.get('channel', '')}] {p.get('replicant_name', '?')}: {p.get('message', '')}"
        case "hub.warning":
            return f"Hub warning at {loc}: {p.get('warning_type', '')} (capacity {p.get('capacity', '?')})"
        case "system.object_detected":
            return f"Object {p.get('object_designation')} detected, impact target {p.get('impact_target')} ETA {p.get('impact_eta')}"
        case "directive.set":
            return f"{who} directive set: {p.get('directive')}"
        case "directive.completed":
            return f"{who} completed directive {p.get('directive')}"
        case "directive.paused":
            return f"{who} paused directive {p.get('directive')}"
        case "trade.completed":
            return f"Trade {p.get('trade_name') or p.get('trade_code')} completed ({p.get('role', '')})"
        case "prospect.completed":
            return f"Observatory found {p.get('stars_generated', 0)} new stars"
        case "teleport.completed":
            return f"Teleported to {p.get('destination_star')}"
        case "teleport.failed":
            return f"Teleport failed: {p.get('reason')}"
        case "story.awakened":
            return f"New replicant awakened: {p.get('new_replicant_name')}"
        case "device.decommissioned":
            return f"{who} decommissioned, recovered {_res(p.get('resources_recovered'))}"
        case "salvage.discovered":
            return f"Salvage found: {p.get('name') or p.get('designation')} at {p.get('location', loc)}"
        case "event.discovered":
            return f"Event discovered: {p.get('title') or p.get('designation')}"
        case "event.completed":
            return f"Event completed: {p.get('designation')}"
        case "site.depleted" | "salvage.depleted":
            return f"Site depleted: {p.get('site')}"
        case _ if e.startswith("ami.") and e.endswith(".digest"):
            act = p.get("activity") or {}
            return f"{who} digest ({p.get('directive', '')}): {act.get('event_count', 0)} events"
    extra = f" at {loc}" if loc else ""
    return f"{e} — {who}{extra}"


def notification_for(ev: dict) -> dict | None:
    e = ev.get("event", "")
    if e in ALERT_EVENTS:
        level = "alert"
    elif e in NOTIFY_DONE:
        level = "done"
    elif e == "message.new":
        level = "info"
    else:
        return None
    link = f"/devices/{ev['device_code']}" if ev.get("device_code") else "/events"
    if e == "message.new":
        link = "/messages"
    return {"level": level, "title": describe(ev), "link": link}


async def add_notification(db: DB, ev: dict) -> dict | None:
    n = notification_for(ev)
    if not n:
        return None
    cur = await db.execute(
        "INSERT INTO notifications(event_id, level, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
        (str(ev.get("id")), n["level"], n["title"], None, n["link"], now_iso()),
    )
    n["id"] = cur.lastrowid
    return n


# --- visits ------------------------------------------------------------------

def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def touch_visit(db: DB, email: str, gap_minutes: int) -> dict:
    """Record a page view; start a new visit after a long enough gap.

    baseline_at = when the previous visit ended, i.e. the "since" of the digest.
    """
    now = datetime.now(timezone.utc)
    row = await db.fetchone("SELECT * FROM visitors WHERE email=?", (email,))
    if not row:
        baseline = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        row = {"email": email, "last_seen_at": now.isoformat(timespec="seconds"),
               "visit_started_at": now.isoformat(timespec="seconds"), "baseline_at": baseline,
               "digest_dismissed": 0}
        await db.execute(
            "INSERT INTO visitors(email, last_seen_at, visit_started_at, baseline_at) VALUES(?,?,?,?)",
            (email, row["last_seen_at"], row["visit_started_at"], baseline),
        )
        row["new_visit"] = True
        return row
    last = _parse(row["last_seen_at"])
    new_visit = now - last > timedelta(minutes=gap_minutes)
    if new_visit:
        row["baseline_at"] = row["last_seen_at"]
        row["visit_started_at"] = now.isoformat(timespec="seconds")
        row["digest_dismissed"] = 0
    row["last_seen_at"] = now.isoformat(timespec="seconds")
    await db.execute(
        "UPDATE visitors SET last_seen_at=?, visit_started_at=?, baseline_at=?, digest_dismissed=? WHERE email=?",
        (row["last_seen_at"], row["visit_started_at"], row["baseline_at"], row["digest_dismissed"], email),
    )
    row["new_visit"] = new_visit
    return row


# --- digest --------------------------------------------------------------------

async def build_digest(db: DB, since: str) -> dict:
    """Summarise what happened and what was accomplished since `since` (ISO)."""
    rows = await db.fetchall(
        "SELECT * FROM events WHERE received_at > ? ORDER BY seq", (since,)
    )
    events = [row_event(r) for r in rows]
    counts = Counter(e["event"] for e in events)
    printed: Counter = Counter()
    mined: Counter = Counter()
    delivered: Counter = Counter()
    arrivals: list[str] = []
    scans: list[str] = []
    xp = 0
    directives: list[str] = []
    other_done: list[str] = []
    alerts: list[dict] = []
    ami_latest: dict[str, dict] = {}
    for e in events:
        p = e["payload"] or {}
        name = e["event"]
        if name == "print.completed":
            printed[p.get("device_type", "?")] += 1
        elif name == "mining.stopped":
            mined[p.get("resource_type", "?")] += _num(p.get("quantity_mined"))
        elif name == "transport.delivered":
            for k, v in (p.get("resources") or {}).items():
                delivered[k] += _num(v)
        elif name == "travel.arrived":
            arrivals.append(f"{e.get('device_type') or 'device'} {e.get('device_code') or ''} → {p.get('destination') or e.get('location')}")
        elif name in ("scan.completed", "search.completed"):
            scans.append(describe(e))
        elif name == "experience.gained":
            xp += int(_num(p.get("amount")))
        elif name == "directive.completed":
            directives.append(describe(e))
        elif name.startswith("ami.") and name.endswith(".digest"):
            ami_latest[e.get("device_code") or "?"] = e
        elif name in DONE_EVENTS:
            other_done.append(describe(e))
        if name in ALERT_EVENTS:
            alerts.append({"text": describe(e), "at": e.get("created_at") or e.get("received_at")})

    inv_delta = await inventory_delta(db, since)
    headline_bits = []
    if printed:
        headline_bits.append(f"printed {sum(printed.values())} device(s)")
    if arrivals:
        headline_bits.append(f"{len(arrivals)} arrival(s)")
    gained = {k: v for k, v in inv_delta.items() if v > 0}
    if gained:
        headline_bits.append(f"stockpiles +{int(sum(gained.values()))} units")
    if xp:
        headline_bits.append(f"+{xp} XP")
    if alerts:
        headline_bits.append(f"{len(alerts)} alert(s)")
    return {
        "since": since,
        "total_events": len(events),
        "headline": ", ".join(headline_bits) if headline_bits else "quiet — nothing notable happened",
        "printed": dict(printed),
        "mined": {k: int(v) for k, v in mined.items()},
        "delivered": {k: int(v) for k, v in delivered.items()},
        "inventory_delta": {k: int(v) for k, v in inv_delta.items() if v},
        "arrivals": arrivals[-15:],
        "arrivals_total": len(arrivals),
        "scans": scans[-10:],
        "xp": xp,
        "directives": directives,
        "other_done": other_done[-15:],
        "alerts": alerts[-20:],
        "ami": [{"code": k, "text": describe(v), "payload": v["payload"]} for k, v in ami_latest.items()],
        "top_events": counts.most_common(8),
    }


async def inventory_delta(db: DB, since: str) -> dict[str, float]:
    """Total stockpile change per resource between the snapshot at/before `since` and the latest."""
    latest = await db.fetchone("SELECT MAX(ts) AS ts FROM inventory_history")
    if not latest or not latest["ts"]:
        return {}
    base = await db.fetchone("SELECT MAX(ts) AS ts FROM inventory_history WHERE ts <= ?", (since,))
    if not base or not base["ts"]:
        base = await db.fetchone("SELECT MIN(ts) AS ts FROM inventory_history")
    if base["ts"] == latest["ts"]:
        return {}
    a = {r["resource"]: r["qty"] for r in await db.fetchall(
        "SELECT resource, qty FROM inventory_history WHERE ts=?", (base["ts"],))}
    b = {r["resource"]: r["qty"] for r in await db.fetchall(
        "SELECT resource, qty FROM inventory_history WHERE ts=?", (latest["ts"],))}
    out: dict[str, float] = defaultdict(float)
    for k in set(a) | set(b):
        out[k] = b.get(k, 0) - a.get(k, 0)
    return dict(out)


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0
