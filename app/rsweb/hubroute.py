"""Long trips through a hub of ours. Arriving in a system with our own hub is 25 % faster (live 2026-10-18: 37.5 s/ly
on surges into FALQUORYX and SOLARES, 50 s/ly elsewhere; leaving a hub gains nothing, and the game's routing never
picks one by itself). So before an automated device trip between systems, the hubs of ours that could shorten it are
tried as a waypoint (`via`): the game previews the direct route and the hub routes (dry_run), and the fastest hub
route is taken when it saves at least MIN_SAVING_S and MIN_SAVING_PART of the direct time.

Estimates (distance, a leg into a hub at 75 %) only choose which hubs to preview; the game's own times decide.
Other players' hubs don't count (unconfirmed whether they speed us up)."""
from __future__ import annotations

import math
from typing import Any

from .pathing import xyz

HUB_FACTOR = 0.75        # a leg that ends at a hub of ours
MIN_LY = 15.0            # shorter trips can't save MIN_SAVING_S (≈ 50 s/ly)
MIN_SAVING_S = 600.0     # 10 minutes
MIN_SAVING_PART = 0.05   # and 5 % of the direct trip
CANDIDATES = 2           # hub routes previewed per trip (each is one dry run)


def candidates(origin: str, dest: str, hubs: set[str], pos: dict[str, Any]) -> list[str]:
    """Hub systems worth previewing for a trip between two systems, best estimate first: none when the trip is short,
    stays in one system, or already ends at a hub."""
    o, t = xyz(pos.get(origin)), xyz(pos.get(dest))
    if not o or not t or origin == dest or dest in hubs:
        return []
    direct = math.dist(o, t)
    if direct < MIN_LY:
        return []
    est = []
    for h in hubs - {origin}:
        p = xyz(pos.get(h))
        if p:
            e = HUB_FACTOR * math.dist(o, p) + math.dist(p, t)
            if e <= direct * (1 - MIN_SAVING_PART):
                est.append((e, h))
    return [h for _, h in sorted(est)[:CANDIDATES]]


def total_seconds(resp: Any) -> float | None:
    """A travel preview's total time: total_time_seconds, else its legs summed."""
    if not isinstance(resp, dict):
        return None
    secs = resp.get("total_time_seconds")
    if secs is None and isinstance(resp.get("route"), list):
        secs = sum(float((x or {}).get("time_seconds") or 0) for x in resp["route"] if isinstance(x, dict)) or None
    try:
        return float(secs) if secs is not None else None
    except (TypeError, ValueError):
        return None


def worth_it(direct: float | None, via: float | None) -> bool:
    return bool(direct and via) and direct - via >= max(MIN_SAVING_S, MIN_SAVING_PART * direct)


def hm(secs: float) -> str:
    m = round(secs / 60)
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"
