"""Wormhole gates (game story, 2026-11-17 07:08 UTC, Kepler: "The corridor is open"). A vessel entering the DELTA gate
(the aperture at DELTA-OBJ-2, where the crystal seeds went) emerges at TWAFFY with no transit time. One way only: DELTA to
TWAFFY. The game takes the gate when the trip's `via` names the gate location (owner 2026-11-17).

A trip leaving the Delta sector (DELTA is ~28,900 ly from the home sector, TWAFFY ~270 ly from Sol) is offered the gate
when the catalog says the route through it is much shorter than the straight line; the game's previews then decide
(Engine._gate_via), like hub routes (hubroute.py)."""
from __future__ import annotations

import math
from typing import Any

from .pathing import xyz

# (gate star, gate location, the star it leads to): one way
GATES: tuple[tuple[str, str, str], ...] = (("DELTA", "DELTA-OBJ-2", "TWAFFY"),)
OPENED = "2026-11-17T07:08:42+00:00"   # sector-range refusals before this were measured without the gate
MIN_SAVING_LY = 100.0                  # a route through a gate must save this much
MIN_SAVING_PART = 0.25                 # and this part of the straight line


def _d(a: str, b: str, pos: dict[str, Any]) -> float | None:
    if a == b:
        return 0.0
    pa, pb = xyz(pos.get(a)), xyz(pos.get(b))
    return math.dist(pa, pb) if pa and pb else None


def gate_for(origin: str, dest: str, pos: dict[str, Any]) -> tuple[str, str, str] | None:
    """The gate a trip between two systems should take (the best estimate), or None: when no gate route is shorter by
    MIN_SAVING_LY and MIN_SAVING_PART, or positions are missing."""
    if not origin or not dest or origin == dest:
        return None
    direct = _d(origin, dest, pos)
    if direct is None:
        return None
    best = None
    for g in GATES:
        a, b = _d(origin, g[0], pos), _d(g[2], dest, pos)
        if a is None or b is None or dest == g[0]:
            continue
        ly = a + b
        if direct - ly >= max(MIN_SAVING_LY, MIN_SAVING_PART * direct) and (best is None or ly < best[0]):
            best = (ly, g)
    return best[1] if best else None


def ly(origin: str, dest: str, pos: dict[str, Any]) -> float | None:
    """The distance a trip origin → dest covers at the surge pace: the straight line, or through a gate when that's
    shorter (one way: dest → origin may be far longer)."""
    direct = _d(origin, dest, pos)
    g = gate_for(origin, dest, pos)
    if g:
        return (_d(origin, g[0], pos) or 0.0) + (_d(g[2], dest, pos) or 0.0)
    return direct


def stale_far(leg: str, entry: Any, pos: dict[str, Any]) -> bool:
    """A remembered sector-range refusal (kv sector_far, "FROM>TO") from before the gates opened on a leg a gate now
    shortens: it no longer counts (the job runner measures the trip again, through the gate)."""
    at = entry.get("at") if isinstance(entry, dict) else entry
    a, _, b = str(leg).partition(">")
    return bool(at) and str(at) < OPENED and gate_for(a, b, pos) is not None
