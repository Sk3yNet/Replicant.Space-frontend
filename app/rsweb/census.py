"""Stellar census: the stars around a vessel, and what's still unexplored.

The game's star catalogue (`GET /stars`) only covers about 70 ly around Sol. Seen live (2026-10-06): a starter region
~500 ly out (OTHILETH at −459, −224) had none of its stars in it, so the map and routes knew nothing nearby. A vessel
with the `census` feature (heaven and cargo vessels) answers `stellar_census` straight away with the stars around it:

    {"page": 1, "per_page": 20, "total_pages": 1, "replicant_position": {x, y, z},
     "stars": [{designation, color, spectral_type, position, entry_point (null until explored), estimated_planets,
                estimated_travel_time (s), distance_from_replicant (ly), explored, has_hub, has_life, has_ward, region}]}

Kept in the db:
  kv "census"        {origin star: {"at", "device", "count"}}       one entry per system a census was run from
  kv "census_stars"  {designation: star record + census_at, census_from}   the latest record of every star seen
The census stars are merged into the stored catalogue (kv "stars") whenever it's read from the game, so the map,
distances, routes and travel all know them.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def can_census(d: dict) -> bool:
    return "stellar_census" in (d.get("available_commands") or []) or "census" in (d.get("features") or [])


def merge(cat: dict | None, census_stars: dict[str, dict]) -> dict:
    """The catalogue with every census star in it: new stars are added (marked `from_census`), stars it already has get
    the census's explored / life / ward flags (and an entry point if it lacked one). Positions stay the catalogue's."""
    cat = dict(cat or {})
    stars = [dict(s) for s in cat.get("stars") or [] if isinstance(s, dict)]
    by = {s.get("designation"): s for s in stars}
    for code, c in sorted(census_stars.items()):
        s = by.get(code)
        if s is None:
            s = {k: v for k, v in c.items() if k not in ("distance_from_replicant", "estimated_travel_time")}
            s["from_census"] = True
            stars.append(s)
            by[code] = s
            continue
        for k in ("explored", "has_life", "has_ward", "has_hub", "census_at"):
            if c.get(k) is not None:
                s[k] = c[k]
        if not s.get("entry_point") and c.get("entry_point"):
            s["entry_point"] = c["entry_point"]
    cat["stars"] = stars
    cat["census_merged"] = len(census_stars)
    return cat


async def record(db, origin: str, device: str, resp: Any) -> list[dict]:
    """Store a census answer (one page or a list of pages) and merge it into the catalogue. Returns its stars."""
    pages = resp if isinstance(resp, list) else [resp]
    found = [s for p in pages if isinstance(p, dict) for s in p.get("stars") or [] if isinstance(s, dict) and s.get("designation")]
    at = _now()
    known = await db.kv_get("census_stars", {}) or {}
    for s in found:
        known[s["designation"]] = {**known.get(s["designation"], {}), **s, "census_at": at, "census_from": origin}
    await db.kv_set("census_stars", known)
    done = await db.kv_get("census", {}) or {}
    done[origin] = {"at": at, "device": device, "count": len(found)}
    await db.kv_set("census", done)
    await db.kv_set("stars", merge(await db.kv_get("stars", {}) or {}, known))
    return found


def _dist(a: dict | None, b: dict | None) -> float | None:
    if not a or not b:
        return None
    return math.dist([a.get(k, 0) for k in "xyz"], [b.get(k, 0) for k in "xyz"])


def seconds_per_ly(census_stars: dict[str, dict]) -> float | None:
    """Travel time per light-year, from the census estimates (median), to estimate ETAs from anywhere."""
    r = sorted(s["estimated_travel_time"] / s["distance_from_replicant"] for s in census_stars.values()
               if (s.get("distance_from_replicant") or 0) > 0 and s.get("estimated_travel_time"))
    return r[len(r) // 2] if r else None


def unexplored(cat: dict, explored: set[str], ref: dict | None, census_stars: dict[str, dict], limit: int = 100) -> list[dict]:
    """Stars nobody has explored yet, nearest to `ref` (a position) first: the census says explored false, or the
    catalogue has the star and we've neither scanned it nor been there."""
    rate = seconds_per_ly(census_stars)
    out = []
    for s in cat.get("stars") or []:
        code = s.get("designation")
        if not code or code in explored or s.get("explored") is True:
            continue
        d = _dist(ref, s.get("position"))
        out.append({**s, "distance": None if d is None else round(d, 2),
                    "eta": None if d is None or not rate else int(d * rate)})
    out.sort(key=lambda s: (s["distance"] is None, s["distance"] or 0, s.get("designation")))
    return out[:limit]


def destination_systems(cat: dict, explored: set[str], yours: set[str], here: str | None, limit: int = 400) -> list[dict]:
    """Systems to offer as travel destinations, nearest to `here` first, grouped: your systems (devices there),
    explored, unexplored. Census stars beyond the catalogue are included (they're merged into it)."""
    pos = {s.get("designation"): s.get("position") for s in cat.get("stars") or [] if isinstance(s, dict)}
    ref = pos.get((here or "").split("-")[0])
    out = []
    for s in cat.get("stars") or []:
        code = s.get("designation")
        if not code:
            continue
        group = ("Your systems" if code in yours else "Explored" if code in explored or s.get("explored") is True
                 else "Unexplored")
        d = _dist(ref, s.get("position"))
        out.append({"value": code, "group": group, "distance": None if d is None else round(d, 2),
                    "label": (f"{d:.1f} ly" if d is not None else "") + (" · census" if s.get("from_census") else "")})
    for code in sorted(yours - {o["value"] for o in out}):   # not in the catalogue at all (no census yet)
        out.append({"value": code, "group": "Your systems", "distance": None, "label": ""})
    order = {"Your systems": 0, "Explored": 1, "Unexplored": 2}
    out.sort(key=lambda o: (order[o["group"]], o["distance"] is None, o["distance"] or 0, o["value"]))
    return out[:limit]
