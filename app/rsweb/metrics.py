"""Logistics backlog over time (Dashboard › Logistics backlog, owner 2026-10-22: is logistics keeping up with demand?).

The logistics pass samples, at most every SAMPLE_S, from what it already has in hand:
  res_away     resources in stockpiles that aren't a receiving stockpile (a fleet home's drop point, where piles
               gather: fleets that take materials in or are stationed)
  res_holds    resources aboard logistics-fleet devices (loaded, not yet unloaded)
  dev_waiting  devices waiting to be moved: fleet members away from their stationed fleet's home (Fleets › Strays),
               devices tagged to: / at: that aren't there yet, and devices logistics is asked to rescue
  req_open     open logistics requests (skipped ones left out)
  req_unmet    requests the last pass couldn't serve (Fleets › Logistics, unmet)
Rows older than KEEP_DAYS are dropped.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .shapes import as_amounts

SAMPLE_S = 600
KEEP_DAYS = 30
METRICS = ("res_away", "res_holds", "dev_waiting", "req_open", "req_unmet")


def backlog(*, inventory: dict[str, dict], receiving: set[str], carriers: list[dict], devices: list[dict],
            strays: list[dict], fleet_homes: dict[str, str], requests: list[dict],
            unmet: list[dict]) -> dict[str, float]:
    """One sample. `inventory`: {location: {resource: qty}}; `receiving`: receiving stockpile locations; `carriers`:
    logistics-fleet devices; `strays`: fleets.strays(...); `fleet_homes`: fleet id → home, for to:<fleet id> tags."""
    from .fleets import where_star
    from .loadouts import pinned_at, to_tag
    away = sum(q for loc, items in inventory.items() if loc not in receiving
               for q in (items or {}).values() if isinstance(q, (int, float)) and q > 0)
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
    return {"res_away": round(away, 1), "res_holds": round(holds, 1), "dev_waiting": len(waiting),
            "req_open": sum(1 for r in requests if not r.get("skipped")), "req_unmet": len(unmet)}


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
    out: dict[str, list[tuple[str, float]]] = {m: [] for m in METRICS}
    for r in await db.fetchall("SELECT ts, metric, value FROM metric_history WHERE ts >= ? ORDER BY ts", (since,)):
        out.setdefault(r["metric"], []).append((r["ts"], r["value"]))
    return out


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
        out[name] = {"points": " ".join(xy), "last": round(pts[-1][1]) if pts else None,
                     "first": round(pts[0][1]) if pts else None}
    return {"w": w, "h": h, "top": round(top), "lines": out, "empty": not any(lines.values())}
