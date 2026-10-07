"""Galactic observatory: which way to point a `prospect`.

The game's docs (quoted by the player, 2026-10-07):
    Default (outward)    omit          looks away from Sol, pushing the frontier further out
    Toward Sol           [−x, −y, −z]  looks back inward (negate each component of your current position)
    Sideways             [0, 1, 0]     looks along the y-axis, perpendicular to the Sol line
    Toward another star  [dx, dy, dz]  the target's position minus your star's position
Positions are the star catalogue's (light-years from Sol). A `prospect.completed` event lists the stars it found
(`stars_generated`, `stars`); they're merged into the catalogue like census stars (ingest).
"""
from __future__ import annotations

AIMS = {"outward": "Outward (away from Sol — default)", "sol": "Toward Sol", "sideways": "Sideways (along y)",
        "star": "Toward a star", "custom": "Custom vector"}


def _xyz(p: dict | None) -> list[float] | None:
    if not isinstance(p, dict) or not all(k in p for k in "xyz"):
        return None
    return [float(p.get(k) or 0) for k in "xyz"]


def aim_vector(aim: str, here: dict | None, target: dict | None = None) -> list[float] | None:
    """The `direction` for an aim, or None to omit it (outward). Raises ValueError when a position is missing."""
    if aim in ("", "outward"):
        return None
    if aim == "sideways":
        return [0.0, 1.0, 0.0]
    h = _xyz(here)
    if aim == "sol":
        if h is None:
            raise ValueError("the observatory's system isn't in the star catalogue, so 'toward Sol' can't be worked out")
        if not any(h):
            raise ValueError("the observatory is at Sol: 'toward Sol' has no direction — pick another aim")
        return [round(-v, 3) for v in h]
    if aim == "star":
        t = _xyz(target)
        if h is None or t is None:
            raise ValueError("both systems need positions in the star catalogue (Map › Stars) to aim at a star")
        d = [round(b - a, 3) for a, b in zip(h, t)]
        if not any(d):
            raise ValueError("that's the observatory's own system — pick another star")
        return d
    raise ValueError(f"unknown aim {aim!r}")
