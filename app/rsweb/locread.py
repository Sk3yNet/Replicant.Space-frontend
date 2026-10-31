"""One place for a location's detail (GET /locations/{place}): every reader saves what it got as kv loc:PLACE and
stamps when, so the others take that copy within their tolerance instead of asking the game again (1.317.0, Joe:
pages read belts and the engine read the same belts again minutes later, because only the engine stamped its reads).
Stamps: belts in belt_reads (the engine's open-site counts, Automations.belt_open_sites), planets and moons in
tf_body_reads (Terraforming › Candidates)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .db import now_iso
from . import tfcandidates as tc


def _belt(place: str) -> bool:
    from .salvage import belt_of   # salvage imports the engine: not at import time
    return belt_of(place) == place


async def save(db, place: str, data: Any) -> None:
    """Keep a fresh detail of `place` and stamp the read for everyone who keeps a copy of it."""
    if not isinstance(data, dict) or not data:
        return
    place = place.upper()
    await db.kv_set(f"loc:{place}", data)
    stamps = []
    if _belt(place):
        stamps.append("belt_reads")
    if tc.body_of(data):
        stamps.append(tc.READS_KV)
    for key in stamps:
        reads = await db.kv_get(key, {}) or {}
        reads[place] = now_iso()
        await db.kv_set(key, reads)


async def fresh(db, place: str, max_age_s: float) -> dict | None:
    """The kept detail of `place` when it was read less than max_age_s ago (by anyone), else None."""
    place = place.upper()
    key = "belt_reads" if _belt(place) else tc.READS_KV
    at = (await db.kv_get(key, {}) or {}).get(place)
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds()
    except (TypeError, ValueError):
        return None
    if age > max_age_s:
        return None
    return await db.kv_get(f"loc:{place}", None) or None


async def read(db, api, place: str, max_age_s: float = 0, background: bool = False) -> Any:
    """The detail of `place`: the kept copy when it's within max_age_s, else the game's (saved and stamped)."""
    if max_age_s > 0 and (kept := await fresh(db, place, max_age_s)) is not None:
        return kept
    data = await (api.request("GET", f"/locations/{place}", background=True) if background else api.get(f"/locations/{place}"))
    await save(db, place, data)
    return data
