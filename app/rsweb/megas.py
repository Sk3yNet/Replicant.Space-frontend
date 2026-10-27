"""Where the megastructures are, for the galaxy map's megastructure symbol. A megastructure sits at a system object
location (STAR-OBJ-n, docs: locations / megastructures), and system objects show up in system scans; the scan's
field for them isn't documented, so any entry in a stored scan that says it's a megastructure counts. Also: the
megastructures you contributed to (megastructure.contributed) and the known datacenter (sim.DATACENTER).
{star: [designation, ...]}."""
from __future__ import annotations

import json
from typing import Any

_cache: dict[str, Any] = {"key": None, "val": {}}


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def in_scan(data: Any) -> set[str]:
    """Designations of the megastructures a stored system scan mentions, wherever in it they are."""
    out: set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            code = x.get("designation") or x.get("code") or x.get("location")
            kind = str(x.get("location_type") or x.get("object_type") or x.get("type") or "").lower()
            if isinstance(code, str) and (kind == "megastructure" or isinstance(x.get("megastructure"), dict)):
                out.add(code)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    return out


async def known(db) -> dict[str, list[str]]:
    from .sim import DATACENTER
    stamp = await db.fetchone("SELECT COUNT(*) AS n, MAX(updated_at) AS t FROM systems")
    ev = await db.fetchone("SELECT MAX(seq) AS s FROM events WHERE event = 'megastructure.contributed'")
    key = (stamp["n"], stamp["t"], ev["s"])
    if _cache["key"] == key:
        return _cache["val"]
    codes = {DATACENTER}
    for r in await db.fetchall("SELECT data FROM systems"):
        try:
            codes |= in_scan(json.loads(r["data"] or "{}"))
        except ValueError:
            pass
    for r in await db.fetchall("SELECT payload, location FROM events WHERE event = 'megastructure.contributed'"):
        p = json.loads(r["payload"] or "{}") or {}
        c = p.get("megastructure_designation") or r["location"]
        if c:
            codes.add(c)
    out: dict[str, list[str]] = {}
    for c in sorted(codes):
        out.setdefault(star_of(c), []).append(c)
    _cache.update(key=key, val=out)
    return out


STATION_RE = ("space station",)


def stations_in(overview: Any) -> set[str]:
    """Systems with a colony space station, from GET /overview: a storyline or event whose summary says space station
    and lists systems (live 2026-10-27: "Terraforming the Colony Worlds" names the twelve where the Exodus Ark's
    replicants built one). The game reports no station location or object, so this is the only source."""
    out: set[str] = set()
    ov = overview if isinstance(overview, dict) else {}
    for x in (ov.get("storylines") or []) + (ov.get("events") or []):
        if isinstance(x, dict) and any(w in str(x.get("summary") or "").lower() for w in STATION_RE):
            out |= {star_of(c) for c in x.get("locations") or [] if isinstance(c, str) and c}
    return out


async def remember_stations(db, overview: Any) -> None:
    """Keep every station system the overview has named (kv colony_stations), so they stay once the storyline ends."""
    new = stations_in(overview)
    old = set(await db.kv_get("colony_stations", []) or [])
    if new - old:
        await db.kv_set("colony_stations", sorted(old | new))


async def stations(db) -> set[str]:
    return set(await db.kv_get("colony_stations", []) or []) | stations_in(await db.kv_get("overview", {}))
