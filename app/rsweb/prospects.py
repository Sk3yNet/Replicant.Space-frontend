"""Mining prospects: how good a system would be to send a mining fleet to, from what the app already knows.

score (0–100) = richness (≤40)  belt resource levels × density; a resource you're short of or a waiting print needs
                                 counts a little more (a light tilt, never more than ~15 % of the richness)
              + proven (≤25)    known site quantities, open sites, salvage
              + staying (≤15)   belt viability: can survey drones re-open sites as fast as they close
              + access (≤20)    distance from where the fleet starts; −5 without a relay of yours there (a replicant
                                 has to ride with the fleet)
Not scored (with a reason instead): another player's ward or hub there (nothing of ours can mine it), a stationed fleet's home
(only that fleet works it), and systems with no scan yet ("survey first").
"""
from __future__ import annotations

import json
import math
from typing import Any

from .targets import LEVELS

DENSITY = {"dense": 1.0, "moderate": 0.85, "sparse": 0.65}
VERDICT = {"ok": 15, "watch": 8, "consider moving": 3}   # anything else (learning, untracked…) is unknown: 8
RESOURCES = ["structural", "conductive", "silicates", "carbon", "volatiles", "rares"]


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _dist(a: dict | None, b: dict | None) -> float | None:
    if not a or not b:
        return None
    return math.sqrt(sum((float(a.get(k) or 0) - float(b.get(k) or 0)) ** 2 for k in ("x", "y", "z")))


def score(star: str, scan: dict | None, res: dict | None, viability: list[dict], distance: float | None, relay: bool,
          wanted: dict[str, float] | None = None, warded: bool = False, stationed: str | None = None) -> dict:
    """One system's prospect: {star, score (None when not scored), parts, reasons, status, best (resources)}."""
    out: dict[str, Any] = {"star": star, "score": None, "parts": {}, "reasons": [], "status": "ok", "best": [],
                           "distance": None if distance is None else round(distance, 1)}
    if warded:
        out.update(status="warded", reasons=["another player's ward or hub — nothing of ours can mine here"])
        return out
    if stationed:
        out.update(status="stationed", reasons=[f"home of {stationed} — only that fleet works it"])
        return out
    belts = ((scan or {}).get("asteroid_belt") or {}).get("belts") or []
    if not scan:
        out.update(status="unscanned", reasons=["no scan yet — survey first"])
        return out
    wanted = wanted or {}
    # richness: the best belt, each resource's level (0–4) weighted 1 + 0.35 × how much you want it (0–1)
    rich, best_belt = 0.0, None
    for b in belts:
        lv = {r: LEVELS.index(l) for r, l in (b.get("resources") or {}).items() if l in LEVELS}
        if not lv:
            continue
        w = {r: 1 + 0.35 * min(1.0, max(0.0, wanted.get(r, 0.0))) for r in lv}
        val = sum(lv[r] * w[r] for r in lv) / (4 * sum(w.values())) * DENSITY.get(str(b.get("density") or ""), 0.85)
        if val > rich:
            rich, best_belt = val, b
    parts = {"richness": round(40 * rich, 1)}
    if best_belt:
        top = sorted(((r, l) for r, l in (best_belt.get("resources") or {}).items() if l in ("rich", "high")),
                     key=lambda x: -LEVELS.index(x[1]))
        out["best"] = [r for r, _ in top]
        if top:
            out["reasons"].append(", ".join(f"{l} {r}" for r, l in top[:3]))
        if best_belt.get("density"):
            out["reasons"].append(f"{best_belt['density']} belt")
        hit = [r for r, _ in top if wanted.get(r, 0) > 0.3]
        if hit:
            out["reasons"].append(f"has what you're short of ({', '.join(hit)})")
    elif belts:
        out["reasons"].append("belt resources not read yet")
    else:
        out["reasons"].append("no asteroid belt" + (" — salvage only" if (res or {}).get("salvageable") else ""))
    # proven: what's been measured
    res = res or {}
    mineable, salvageable = float(res.get("mineable") or 0), float(res.get("salvageable") or 0)
    open_sites = sum(1 for s in res.get("sites") or [] if not s.get("depleted"))
    parts["proven"] = round(15 * min(1.0, mineable / 5000) + 5 * min(1.0, open_sites / 5) + 5 * min(1.0, salvageable / 2000), 1)
    if mineable or open_sites:
        out["reasons"].append(f"{open_sites} open site(s)" + (f", {int(mineable):,} known" if mineable else ""))
    if salvageable:
        out["reasons"].append(f"{int(salvageable):,} salvage")
    # staying power: belt viability verdicts (unknown counts as middling)
    verdicts = [v.get("verdict") for v in viability if star_of(v.get("belt")) == star]
    parts["staying"] = float(max((VERDICT.get(v, 8) for v in verdicts), default=8))
    if verdicts and all(v == "consider moving" for v in verdicts):
        out["reasons"].append("sites close faster than they re-open")
    # access
    acc = 20 * max(0.0, 1 - distance / 30) if distance is not None else 8.0
    if not relay:
        acc -= 5
        out["reasons"].append("no relay of yours — a replicant must ride along")
    parts["access"] = round(max(0.0, acc), 1)
    if distance is not None:
        out["reasons"].append(f"{distance:.1f} ly")
    out["parts"] = parts
    out["score"] = round(sum(parts.values()))
    return out


def wanted_from(totals: dict[str, float], series: dict[str, list[float]], waiting_costs: list[dict[str, float]]) -> dict[str, float]:
    """How much you want each resource, 0–1: what waiting prints need beyond your stock, then a falling or low stockpile."""
    want = {r: 0.0 for r in RESOURCES}
    need: dict[str, float] = {}
    for cost in waiting_costs:
        for r, q in cost.items():
            need[r] = need.get(r, 0.0) + float(q or 0)
    for r, q in need.items():
        have = float(totals.get(r) or 0)
        if q > have:
            want[r] = max(want.get(r, 0.0), min(1.0, (q - have) / max(q, 1)) * 0.6 + 0.4)
    vals = [float(totals.get(r) or 0) for r in RESOURCES]
    mid = sorted(vals)[len(vals) // 2] if vals else 0
    for r in RESOURCES:
        s = series.get(r) or []
        if len(s) > 1 and s[-1] < s[0]:
            want[r] = max(want[r], min(0.6, (s[0] - s[-1]) / max(s[0], 1) * 3))   # falling over 48 h
        if mid and float(totals.get(r) or 0) < mid / 3:
            want[r] = max(want[r], 0.4)                                            # well below your other stocks
    return want


async def rank(db, eng, devices: list[dict], origin: str | None, limit_unscanned: int = 8) -> dict:
    """Every scanned system scored (best first), plus the nearest unscanned catalogue stars as "survey first"."""
    from . import outposts, printqueue, wards
    from .shapes import normalize_blueprints
    from .targets import system_resources
    cat = await db.kv_get("stars", {}) or {}
    pos = {s.get("designation"): s.get("position") for s in cat.get("stars") or [] if isinstance(s, dict)}
    warded = wards.foreign(cat, devices)
    homes = {f["home"]: f.get("name") or f["id"] for f in await eng.fleets() if f.get("station") and f.get("home")}
    viability = await eng.viability_report()
    # what you want: waiting prints' costs, falling / low stockpiles
    bps = {b["device_type"]: b for b in normalize_blueprints(await db.kv_get("blueprints", []))}
    waiting = []
    for d in devices:
        if str(d.get("status") or "").startswith("waiting_for_resources"):
            cur = await printqueue.current(db, d)
            cost = (bps.get((cur or {}).get("device_type") or "") or {}).get("resources") or {}
            if cost:
                waiting.append(cost)
    totals = await db.kv_get("inventory_totals", {}) or {}
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(timespec="seconds")
    series: dict[str, list[float]] = {}
    for r in await db.fetchall("SELECT resource, qty FROM inventory_history WHERE ts >= ? ORDER BY ts", (since,)):
        series.setdefault(r["resource"], []).append(r["qty"])
    wanted = wanted_from(totals, series, waiting)
    origin = star_of(origin)
    rows = []
    scanned = {r["star"]: json.loads(r["data"]) for r in await db.fetchall("SELECT star, data FROM systems")}
    for star, scan in scanned.items():
        rows.append(score(star, scan, await system_resources(db, star), viability, _dist(pos.get(origin), pos.get(star)),
                          bool(outposts.deployed_in(devices, star, "relay")), wanted, star in warded, homes.get(star)))
    unscanned = sorted((s for s in pos if s not in scanned and pos.get(s)),
                       key=lambda s: _dist(pos.get(origin), pos.get(s)) if pos.get(origin) else 0)[:limit_unscanned]
    for star in unscanned:
        rows.append(score(star, None, None, [], _dist(pos.get(origin), pos.get(star)), False, wanted, star in warded,
                          homes.get(star)))
    order = {"ok": 0, "unscanned": 1, "stationed": 2, "warded": 3}
    rows.sort(key=lambda r: (order[r["status"]], -(r["score"] or 0), r["distance"] if r["distance"] is not None else 1e9))
    return {"rows": rows, "wanted": {r: round(v, 2) for r, v in wanted.items() if v > 0}, "origin": origin}
