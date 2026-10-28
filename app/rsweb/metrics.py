"""Logistics backlog over time (Dashboard › Logistics backlog, owner 2026-10-22: is logistics keeping up with demand?).

The logistics pass samples, at most every SAMPLE_S, from what it already has in hand:
  res_away     resources in stockpiles that aren't a receiving stockpile (a fleet home's drop point, where piles
               gather: fleets that take materials in or are stationed)
  res_holds    resources aboard logistics-fleet devices (loaded, not yet unloaded)
  dev_waiting  devices waiting to be moved: fleet members away from their stationed fleet's home (Fleets › Strays),
               devices tagged to: / at: that aren't there yet, and devices logistics is asked to rescue
  req_open     open logistics requests (skipped ones left out)
  req_unmet    requests the last pass couldn't serve (Fleets › Logistics, unmet)
  req_age_max_h, req_age_med_h   how long the open requests have been open (a request counts as open until no
               request and no running logistics job names it any more: delivered, or dropped); lead_med_h: the median
               time from first seen to closed of the requests closed in the last 24 h (track(), every pass)
  carriers_busy_pct   logistics-fleet devices in a job; hold_fill_pct: cargo / capacity of those in a job
  missions_stalled, missions_retrying   fleet missions stalled, and those waiting on an auto-retry
  expired_24h  ward / relay / beacon orders dropped in the last 24 h for nothing getting there (note("expired"))
  role:<role>  devices per fleet role (role:none: in no fleet)
  mine:<resource>   mining output per hour: what piles where something is mining gained between inventory polls
               (an estimate: a delivery there counts, a pick-up there hides that poll's gain; mining_gain())
  cmd_systems, relay_systems   systems you can command, and those an active relay network covers
Rows older than KEEP_DAYS are dropped.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .shapes import as_amounts

import statistics

SAMPLE_S = 600
KEEP_DAYS = 30


def backlog(*, inventory: dict[str, dict], receiving: set[str], carriers: list[dict], devices: list[dict],
            strays: list[dict], fleet_homes: dict[str, str], requests: list[dict],
            unmet: list[dict]) -> dict[str, float]:
    """One sample. `inventory`: {location: {resource: qty}}; `receiving`: receiving stockpile locations; `carriers`:
    logistics-fleet devices; `strays`: fleets.strays(...); `fleet_homes`: fleet id → home, for to:<fleet id> tags."""
    from .fleets import where_star
    from .loadouts import pinned_at, to_tag
    # piles at a contract's or trade's location, up to what the open deals there still pay: set aside, not backlog;
    # anything beyond that (a trade's payout, leftovers) waits to move to a stockpile like any other pile
    owed: dict[str, dict[str, float]] = {}
    for r in requests:
        if r.get("kind") in ("contract", "trade") and not r.get("skipped") and r.get("to"):
            o = owed.setdefault(r["to"], {})
            for res, q in as_amounts(r.get("price") or {}).items():
                o[res] = o.get(res, 0) + q
    away = deal = 0.0
    for loc, items in inventory.items():
        if loc in receiving:
            continue
        for res, q in (items or {}).items():
            if isinstance(q, (int, float)) and q > 0:
                kept = min(q, max(0.0, (owed.get(loc) or {}).get(res, 0)))
                deal += kept
                away += q - kept
    holds = sum(q for d in carriers for q in as_amounts(d.get("cargo")).values() if q > 0)
    by = {d.get("device_code"): d for d in devices}
    waiting = {s["code"] for s in strays}
    for d in devices:
        tags = d.get("tags") or []
        if not any(str(t).startswith(("to:", "at:")) for t in tags):
            continue
        here = to_tag(where_star(d, by) or "?")
        pin = pinned_at(d)
        # to:<star slug> (or to:<fleet id>, meaning that fleet's home) compared slug to slug: no star catalog needed
        want = [to_tag(fleet_homes[t[3:]]) if t[3:] in fleet_homes else t for t in tags if str(t).startswith("to:")]
        off = any(w != here for w in want if w != "to:")
        if off or (pin and d.get("location") != pin):
            waiting.add(d["device_code"])
    waiting |= {r["device"] for r in requests if r.get("kind") == "rescue" and r.get("device")}
    return {"res_away": round(away, 1), "res_deals": round(deal, 1), "res_holds": round(holds, 1), "dev_waiting": len(waiting),
            "req_open": sum(1 for r in requests if not r.get("skipped")), "req_unmet": len(unmet)}


KV_TRACK = "metric_requests"   # {"seen": {request id: first seen}, "leads": [[closed at, hours], ...]}
KV_GAIN = "metric_mining"      # {"since": iso, "res": {resource: qty gained}}
KV_NOTES = "metric_notes"      # {name: [iso, ...]} — things counted per day (expired orders)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _h(a: str, now: datetime) -> float:
    return (now - datetime.fromisoformat(a)).total_seconds() / 3600


def track(state: dict | None, open_ids: set[str], now: datetime) -> dict:
    """Request ages: first-seen times of the open requests; a request no longer open gives a lead time."""
    state = dict(state or {})
    seen, leads = dict(state.get("seen") or {}), list(state.get("leads") or [])
    for rid in [r for r in seen if r not in open_ids]:
        leads.append([_iso(now), round(_h(seen.pop(rid), now), 2)])
    for rid in open_ids:
        seen.setdefault(rid, _iso(now))
    cut = _iso(now - timedelta(days=KEEP_DAYS))
    return {"seen": seen, "leads": [x for x in leads if x[0] >= cut][-3000:]}


def ages(state: dict, now: datetime) -> dict[str, float]:
    a = [_h(t, now) for t in (state.get("seen") or {}).values()]
    day = _iso(now - timedelta(hours=24))
    leads = [h for t, h in state.get("leads") or [] if t >= day]
    return {"req_age_max_h": round(max(a, default=0), 2), "req_age_med_h": round(statistics.median(a), 2) if a else 0,
            "lead_med_h": round(statistics.median(leads), 2) if leads else 0}


def carrier_use(carriers: list[dict], busy: set[str]) -> dict[str, float]:
    if not carriers:
        return {}
    on = [d for d in carriers if d.get("device_code") in busy]
    cap = sum(float(d.get("cargo_capacity") or 0) for d in on)
    load = sum(q for d in on for q in as_amounts(d.get("cargo")).values() if q > 0)
    return {"carriers_busy_pct": round(100 * len(on) / len(carriers), 1),
            "hold_fill_pct": round(100 * min(load, cap) / cap, 1) if cap else 0}


def missions(fleets: list[dict]) -> dict[str, float]:
    ms = [f.get("mission") or {} for f in fleets]
    stalled = [m for m in ms if m.get("status") == "stalled"]
    return {"missions_stalled": len(stalled), "missions_retrying": sum(1 for m in stalled if (m.get("auto_retry") or {}).get("at"))}


def roles(fleets: list[dict], devices: list[dict]) -> dict[str, float]:
    from .fleets import members
    out: dict[str, float] = {}
    seen: set[str] = set()
    for f in fleets:
        for d in members(f, devices):
            if d.get("device_code") not in seen:
                seen.add(d.get("device_code"))
                k = f"role:{f.get('role') or 'none'}"
                out[k] = out.get(k, 0) + 1
    out["role:none"] = out.get("role:none", 0) + sum(1 for d in devices if d.get("device_code") not in seen)
    return out


def pile_gains(prev: dict[str, dict], cur: dict[str, dict], where: set[str]) -> dict[str, float]:
    """What the piles at `where` (locations something is mining at) gained since the last poll, per resource."""
    out: dict[str, float] = {}
    for loc in where:
        old, new = prev.get(loc) or {}, cur.get(loc) or {}
        for r, q in new.items():
            if isinstance(q, (int, float)) and q > float(old.get(r) or 0):
                out[r] = out.get(r, 0) + q - float(old.get(r) or 0)
    return out


async def mining_gain(db, prev: dict[str, dict], cur: dict[str, dict], where: set[str]) -> None:
    """Called on each inventory poll (ingest.sync_inventory)."""
    gain = pile_gains(prev, cur, where)
    if not gain:
        return
    g = await db.kv_get(KV_GAIN, {}) or {}
    res = g.get("res") or {}
    for r, q in gain.items():
        res[r] = round(float(res.get(r) or 0) + q, 2)
    await db.kv_set(KV_GAIN, {"since": g.get("since") or _iso(datetime.now(timezone.utc) - timedelta(minutes=2)), "res": res})


async def mining_rates(db, now: datetime) -> dict[str, float]:
    """mine:<resource> per hour since the last sample; starts the next window."""
    g = await db.kv_get(KV_GAIN, {}) or {}
    await db.kv_set(KV_GAIN, {"since": _iso(now), "res": {}})
    hours = _h(g["since"], now) if g.get("since") else 0
    if hours < 0.05:
        return {}
    from .commands import RESOURCES
    res = {r: 0.0 for r in RESOURCES} | (g.get("res") or {})
    return {f"mine:{r}": round(q / hours, 1) for r, q in res.items()}


async def note(db, name: str) -> None:
    n = await db.kv_get(KV_NOTES, {}) or {}
    now = datetime.now(timezone.utc)
    day = _iso(now - timedelta(hours=24))
    n[name] = [t for t in n.get(name) or [] if t >= day] + [_iso(now)]
    await db.kv_set(KV_NOTES, n)


async def noted(db, name: str, now: datetime) -> int:
    day = _iso(now - timedelta(hours=24))
    return sum(1 for t in ((await db.kv_get(KV_NOTES, {}) or {}).get(name) or []) if t >= day)


async def due(db) -> bool:
    row = await db.fetchone("SELECT MAX(ts) AS ts FROM metric_history")
    if not row or not row["ts"]:
        return True
    last = datetime.fromisoformat(row["ts"])
    return (datetime.now(timezone.utc) - last).total_seconds() >= SAMPLE_S


async def record(db, values: dict[str, float]) -> None:
    now = datetime.now(timezone.utc)
    ts = now.isoformat(timespec="seconds")
    for k, v in values.items():
        await db.execute("INSERT OR REPLACE INTO metric_history(ts, metric, value) VALUES(?,?,?)", (ts, k, float(v)))
    await db.execute("DELETE FROM metric_history WHERE ts < ?",
                     ((now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds"),))


async def series(db, hours: float) -> dict[str, list[tuple[str, float]]]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    out: dict[str, list[tuple[str, float]]] = {}
    for r in await db.fetchall("SELECT ts, metric, value FROM metric_history WHERE ts >= ? ORDER BY ts", (since,)):
        out.setdefault(r["metric"], []).append((r["ts"], r["value"]))
    return out


def _r(v: float) -> float:
    return round(v) if abs(v) >= 10 else round(v, 1)


def chart(lines: dict[str, list[tuple[str, float]]], hours: float, w: int = 300, h: int = 70) -> dict[str, Any]:
    """Polylines for an inline SVG sharing one time axis (now at the right edge) and one y scale from 0."""
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(hours=hours)
    top = max([v for pts in lines.values() for _, v in pts] + [1.0])
    out = {}
    for name, pts in lines.items():
        xy = []
        for ts, v in pts:
            x = (datetime.fromisoformat(ts) - t0).total_seconds() / (hours * 3600) * w
            xy.append(f"{max(0.0, x):.1f},{h - 1 - v * (h - 2) / top:.1f}")
        out[name] = {"points": " ".join(xy), "last": _r(pts[-1][1]) if pts else None,
                     "first": _r(pts[0][1]) if pts else None}
    return {"w": w, "h": h, "top": _r(top), "lines": out, "empty": not any(lines.values())}
