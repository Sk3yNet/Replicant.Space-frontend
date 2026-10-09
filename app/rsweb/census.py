"""Stellar census: the stars around a vessel, and what's still unexplored.

The game's star catalog (`GET /stars`) only covers about 70 ly around Sol. Seen live (2026-10-06): a starter region
~500 ly out (OTHILETH at −459, −224) had none of its stars in it, so the map and routes knew nothing nearby. A vessel
with the `census` feature (heaven and cargo vessels) answers `stellar_census` straight away with the stars around it:

    {"page": 1, "per_page": 20, "total_pages": 1, "replicant_position": {x, y, z},
     "stars": [{designation, color, spectral_type, position, entry_point (null until explored), estimated_planets,
                estimated_travel_time (s), distance_from_replicant (ly), explored, has_hub, has_life, has_ward, region}]}

Kept in the db:
  kv "census"        {origin star: {"at", "device", "count"}}       one entry per system a census was run from
  kv "census_stars"  {designation: star record + census_at, census_from}   the latest record of every star seen
The census stars are merged into the stored catalog (kv "stars") whenever it's read from the game, so the map,
distances, routes and travel all know them.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def can_census(d: dict) -> bool:
    return "stellar_census" in (d.get("available_commands") or []) or "census" in (d.get("features") or [])


def merge(cat: dict | None, census_stars: dict[str, dict]) -> dict:
    """The catalog with every census star in it: new stars are added (marked `from_census`), stars it already has get
    the census's explored / life / ward flags (and an entry point if it lacked one). Positions stay the catalog's."""
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
    catalog has the star and we've neither scanned it nor been there."""
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
    explored, unexplored. Census stars beyond the catalog are included (they're merged into it)."""
    import heapq
    here = (here or "").split("-")[0]
    rp = next((s.get("position") for s in cat.get("stars") or [] if isinstance(s, dict) and s.get("designation") == here), None)
    ref = _xyz(rp)
    order = {"Your systems": 0, "Explored": 1, "Unexplored": 2}
    keyed, seen = [], set()
    for s in cat.get("stars") or []:   # sort keys only (thousands of stars); dicts just for the ones returned
        code = s.get("designation") if isinstance(s, dict) else None
        if not code:
            continue
        seen.add(code)
        group = ("Your systems" if code in yours else "Explored" if code in explored or s.get("explored") is True
                 else "Unexplored")
        p = _xyz(s.get("position"))
        d = math.dist(ref, p) if ref and p else None
        keyed.append(((order[group], d is None, d or 0, code), group, d, s))
    for code in sorted(yours - seen):   # not in the catalog at all (no census yet)
        keyed.append(((0, True, 0, code), "Your systems", None, {"designation": code}))
    return [{"value": s["designation"], "group": group, "distance": None if d is None else round(d, 2),
             "label": (f"{d:.1f} ly" if d is not None else "") + (" · census" if s.get("from_census") else "")}
            for _, group, d, s in heapq.nsmallest(limit, keyed, key=lambda x: x[0])]


def _xyz(p) -> tuple[float, float, float] | None:
    if not isinstance(p, dict):
        return None
    try:
        return (float(p.get("x") or 0), float(p.get("y") or 0), float(p.get("z") or 0))
    except (TypeError, ValueError):
        return None


async def fetch_catalogue(api) -> dict:
    """GET /stars. Documented as unpaginated; if the game ever pages it (next_cursor), the rest is followed too (at
    most 20 pages — the catalog allows 1 request/minute, so a second page may be refused; what was read is kept)."""
    body = await api.get("/stars", background=True) or {}
    stars = list(body.get("stars") or [])
    cursor = body.get("next_cursor")
    for _ in range(20):
        if not cursor:
            break
        try:
            more = await api.get("/stars", background=True, cursor=cursor) or {}
        except Exception:
            break
        stars += more.get("stars") or []
        cursor = more.get("next_cursor")
    return {**body, "stars": stars, "pages_read": True if not cursor else "partial"}


def _position(s: dict) -> dict | None:
    p = s.get("position")
    if isinstance(p, dict) and all(k in p for k in "xyz"):
        return p
    if all(k in s for k in "xyz"):
        return {k: s[k] for k in "xyz"}
    return None


async def observatory_stars(db) -> tuple[dict[str, dict], list[str]]:
    """Stars our observatories found (every stored prospect.completed event, so finds from before the client merged
    them count too): ({designation: record with a position}, [designations reported without a position])."""
    placed: dict[str, dict] = {}
    unplaced: list[str] = []
    rows = await db.fetchall("SELECT payload, device_code, created_at FROM events WHERE event='prospect.completed' ORDER BY seq")
    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}")
        except ValueError:
            continue
        for s in p.get("stars") or []:
            if isinstance(s, str):
                unplaced.append(s)
            elif isinstance(s, dict) and s.get("designation"):
                pos = _position(s)
                if pos:
                    placed[s["designation"]] = {**s, "position": pos, "found_by": r["device_code"], "found_at": r["created_at"],
                                                "from_observatory": True}
                else:
                    unplaced.append(s["designation"])
    return placed, sorted(set(unplaced) - set(placed))


async def full_catalogue(db, raw: dict) -> dict:
    """The game's catalog plus the stars our censuses and observatories found beyond it."""
    extra = dict(await db.kv_get("census_stars", {}) or {})
    obs, unplaced = await observatory_stars(db)
    for k, v in obs.items():
        extra.setdefault(k, v)
    cat = merge(raw or {}, extra)
    have = {s.get("designation") for s in cat.get("stars") or []}
    cat["sources"] = {"catalogue": len((raw or {}).get("stars") or []), "catalogue_total": (raw or {}).get("total"),
                      "census": sum(1 for s in cat["stars"] if s.get("from_census") and not s.get("from_observatory")),
                      "observatory": sum(1 for s in cat["stars"] if s.get("from_observatory")),
                      "observatory_unplaced": [u for u in unplaced if u not in have],
                      "pages_read": (raw or {}).get("pages_read")}
    return cat
