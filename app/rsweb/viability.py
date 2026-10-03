"""Belt viability: is it still worth searching this belt for new sites?

Each open site is held by one tracking survey drone; a drone can only search again once its site is used up. So per
miner kept busy you need about (1 + search time / site life) survey drones. Searches get slower every time on a belt
(diminishing returns), so that ratio only grows — past ~2.5 a fresh belt elsewhere gives far more per drone.

Observations (no extra API calls — the device list and belt details the app already reads):
  • searches: a survey drone with status `searching` carries `scan {target, started_at, completes_at}` → duration
  • sites: belt details list `resource_sites [{designation, site_index, resources_remaining_pct}]`; first seen → open,
    gone from the list (or 0 % everywhere) → closed. Life = closed − first seen.
  • miners: mining drones at the belt (and how many are mining) each pass.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from statistics import median
from typing import Any

BELT_RE = re.compile(r"^([A-Z0-9]+-BELT-\d+)")
WATCH, MOVE = 1.0, 2.5      # ratio thresholds: search time ÷ site life
MAX_SEARCHES, MAX_CLOSED = 20, 30


def belt_of(loc: str | None) -> str | None:
    m = BELT_RE.match((loc or "").upper())
    return m.group(1) if m else None


def star_of(code: str | None) -> str:
    return (code or "").split("-")[0]


def _ts(v: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def _minutes(a: Any, b: Any) -> float | None:
    ta, tb = _ts(a), _ts(b)
    return round((tb - ta).total_seconds() / 60, 1) if ta and tb and tb > ta else None


def _live_sites(detail: dict | None) -> dict[str, dict]:
    out = {}
    for s in (detail or {}).get("resource_sites") or []:
        if not isinstance(s, dict) or s.get("site_type") == "salvage":
            continue
        code = s.get("designation") or s.get("site")
        if not code or "-SAL-" in code:
            continue
        pct = s.get("resources_remaining_pct") or {}
        if isinstance(pct, dict) and pct and all((v or 0) <= 0 for v in pct.values()):
            continue  # 0 % everywhere: used up
        idx = s.get("site_index")
        if idx is None:
            m = re.search(r"-SITE-(\d+)$", code)
            idx = int(m.group(1)) if m else None
        out[code] = {"index": idx, "pct": pct}
    return out


def observe(state: dict, devices: list[dict], details: dict[str, dict], now: str) -> tuple[dict, list[str]]:
    """Fold one pass of observations into `state`. `details`: belt → its latest detail (only belts read recently).
    Returns (state, notes about what changed)."""
    belts = state.setdefault("belts", {})
    notes: list[str] = []
    # searches in progress (recorded once per drone+start; duration from the game's own completes_at)
    for d in devices:
        if not str(d.get("status") or "").startswith("searching"):
            continue
        sc = d.get("scan") or {}
        b = belt_of(sc.get("target")) or belt_of(d.get("location"))
        if not b or not sc.get("started_at"):
            continue
        rec = belts.setdefault(b, {})
        ss = rec.setdefault("searches", [])
        key = (d.get("device_code"), sc.get("started_at"))
        if any((x.get("drone"), x.get("start")) == key for x in ss):
            continue
        mins = _minutes(sc.get("started_at"), sc.get("completes_at"))
        ss.append({"drone": d.get("device_code"), "start": sc.get("started_at"), "end": sc.get("completes_at"), "minutes": mins,
                   "after_index": rec.get("max_index")})
        rec["searches"] = ss[-MAX_SEARCHES:]
        if mins:
            notes.append(f"{b}: search by {d.get('device_code')} takes {mins:.0f} min")
    # sites opening and closing
    for b, detail in details.items():
        b = belt_of(b)
        if not b or detail is None:
            continue
        rec = belts.setdefault(b, {})
        sites = rec.setdefault("sites", {})
        live = _live_sites(detail)
        for code, info in live.items():
            s = sites.setdefault(code, {"index": info["index"], "first": now})
            s["last"] = now
            s.pop("closed", None)
            if info["index"] is not None:
                rec["max_index"] = max(rec.get("max_index") or 0, info["index"])
        for code, s in sites.items():
            if code not in live and not s.get("closed"):
                s["closed"] = now
                life = _minutes(s.get("first"), now)
                notes.append(f"{b}: {code} closed after ~{life:.0f} min" if life else f"{b}: {code} closed")
        # keep open sites + the most recent closed ones
        closed = sorted((c for c, s in sites.items() if s.get("closed")), key=lambda c: sites[c]["closed"])
        for c in closed[:-MAX_CLOSED]:
            sites.pop(c, None)
        rec["open"] = len(live)
        rec["read_at"] = now
    # miners at each belt this pass
    for b, rec in belts.items():
        here = [d for d in devices if belt_of(d.get("location")) == b and d.get("device_type") == "mining_drone"]
        rec["miners"] = len(here)
        rec["mining"] = sum(1 for d in here if str(d.get("status") or "").startswith("mining"))
        rec["survey"] = sum(1 for d in devices if belt_of(d.get("location")) == b and d.get("device_type") == "survey_drone")
        rec["seen_at"] = now
    return state, notes


def assess(b: str, rec: dict, move_at: float = MOVE) -> dict:
    """Ratio, survey drones needed and a verdict for one belt."""
    searches = [x["minutes"] for x in rec.get("searches") or [] if x.get("minutes")]
    lives = sorted(((s["closed"], _minutes(s.get("first"), s.get("closed")))
                    for s in (rec.get("sites") or {}).values() if s.get("closed")), key=lambda x: x[0])
    lives = [m for _, m in lives if m]
    search = median(searches[-4:]) if searches else None
    life = median(lives[-6:]) if lives else None
    ratio = round(search / life, 2) if search and life else None
    miners = rec.get("miners") or 0
    need = math.ceil(miners * (1 + ratio)) if ratio is not None and miners else None
    trend = None
    if len(searches) >= 4:
        a, z = median(searches[:2]), median(searches[-2:])
        trend = round(z / a, 2) if a else None
    if ratio is None:
        verdict = "learning" if not searches else "no site lifetimes yet"
    elif ratio >= move_at:
        verdict = "consider moving"
    elif ratio >= WATCH:
        verdict = "watch"
    else:
        verdict = "ok"
    return {"belt": b, "star": star_of(b), "search_min": search, "site_life_min": life, "ratio": ratio, "miners": miners,
            "mining": rec.get("mining") or 0, "survey": rec.get("survey") or 0, "survey_needed": need,
            "survey_short": max(0, (need or 0) - (rec.get("survey") or 0)) if need else None,
            "max_index": rec.get("max_index"), "open": rec.get("open"), "searches": len(searches), "sites_closed": len(lives),
            "trend": trend, "verdict": verdict}


def _dist(a: str, b: str, pos: dict) -> float:
    pa, pb = pos.get(a) or {}, pos.get(b) or {}
    if not pa or not pb:
        return 1e9
    return math.dist([pa.get(k, 0) for k in "xyz"], [pb.get(k, 0) for k in "xyz"])


def report(state: dict, stars: dict[str, dict] | None = None, move_at: float = MOVE,
           known_belts: dict[str, int] | None = None) -> list[dict]:
    """One row per tracked belt, worst first, each 'consider moving' row with the best alternative: the nearest belt
    (tracked or merely known) with a lower ratio, or a low site index if nothing has been timed there yet.
    `known_belts`: belt → highest site index seen (e.g. from belt details of systems not tracked yet)."""
    pos = {k: (v or {}).get("position") or {} for k, v in (stars or {}).items()}
    rows = [assess(b, rec, move_at) for b, rec in (state.get("belts") or {}).items()
            if rec.get("searches") or rec.get("sites") or rec.get("miners")]
    cands = {r["belt"]: r for r in rows}
    for b, idx in (known_belts or {}).items():
        cands.setdefault(b, {"belt": b, "star": star_of(b), "ratio": None, "max_index": idx, "verdict": "untracked"})
    for r in rows:
        if r["verdict"] != "consider moving":
            continue
        def better(c: dict) -> bool:
            if c["belt"] == r["belt"]:
                return False
            if c.get("ratio") is not None:
                return c["ratio"] < WATCH
            return (c.get("max_index") or 0) < (r.get("max_index") or 0) / 4
        opts = sorted((c for c in cands.values() if better(c)),
                      key=lambda c: (_dist(r["star"], c["star"], pos), c.get("ratio") or 0, c["belt"]))
        if opts:
            o = opts[0]
            d = _dist(r["star"], o["star"], pos)
            r["move_to"] = {"belt": o["belt"], "ratio": o.get("ratio"), "max_index": o.get("max_index"),
                            "ly": None if d >= 1e9 else round(d, 2), "same_system": o["star"] == r["star"]}
    order = {"consider moving": 0, "watch": 1, "ok": 2, "no site lifetimes yet": 3, "learning": 4}
    return sorted(rows, key=lambda r: (order.get(r["verdict"], 5), -(r["ratio"] or 0), r["belt"]))


def alert_text(r: dict) -> str:
    t = (f"{r['belt']}: searches take {r['ratio']}× a site's life ({r['search_min']:.0f} vs {r['site_life_min']:.0f} min) — "
         f"keeping {r['miners']} miner(s) busy needs ~{r['survey_needed']} survey drones")
    mv = r.get("move_to")
    if mv:
        where = mv["belt"] + (f" ({mv['ly']} ly)" if mv.get("ly") is not None and not mv["same_system"] else "")
        why = f"ratio {mv['ratio']}" if mv.get("ratio") is not None else f"site index {mv.get('max_index') or 0}"
        t += f". Consider moving the miners to {where}, {why}."
    else:
        t += ". Consider moving the miners to a fresh belt."
    return t
