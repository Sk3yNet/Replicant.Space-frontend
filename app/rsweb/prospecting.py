"""Every fleet's galactic observatory prospects wherever it is deployed (engine stage observatory_pass).

Per observatory and system (kv "observatory_runs", {code: run}):
    star        the system it is working in (a new system starts a fresh run)
    used        indexes of directions that are used up
    idx         the direction being tried, job / seq0 / at for the prospect in flight, found = new stars here
    done        every direction used up

Directions (14): the fleet's heading — else outward from Sol — then 6 around it at 45°, 6 at 90°, and straight back.
A prospect that finds no new star (or the game's "This location has already been surveyed") uses its direction up; one
that finds stars is tried again. The observatory unfurls and prospects as soon as it is deployed; it compacts when
every direction is used up, or as soon as its fleet is set to leave (a planned next home, or loadouts moving it).
"""
from __future__ import annotations

import math

N_RING = 6


def _unit(v) -> list[float]:
    n = math.sqrt(sum(a * a for a in v)) or 1.0
    return [a / n for a in v]


def _cross(a, b) -> list[float]:
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def base_heading(heading, here_xyz) -> list[float]:
    """The fleet's heading, else outward from Sol through this system (from Sol itself: +x)."""
    if heading and any(heading):
        return _unit(heading)
    if here_xyz and any(here_xyz):
        return _unit(here_xyz)
    return [1.0, 0.0, 0.0]


def directions(h: list[float]) -> list[list[float]]:
    """14 unit vectors: h, 6 at 45° around it, 6 at 90°, and -h (rounded for the command)."""
    h = _unit(h)
    ref = [0.0, 0.0, 1.0] if abs(h[2]) < 0.9 else [1.0, 0.0, 0.0]
    u = _unit(_cross(h, ref))
    w = _cross(h, u)
    out = [h]
    for tilt in (45, 90):
        t = math.radians(tilt)
        for k in range(N_RING):
            a = 2 * math.pi * k / N_RING + (math.pi / N_RING if tilt == 90 else 0)   # the rings offset from each other
            side = [math.cos(a) * u[i] + math.sin(a) * w[i] for i in range(3)]
            out.append(_unit([math.cos(t) * h[i] + math.sin(t) * side[i] for i in range(3)]))
    out.append([-a for a in h])
    return [[round(a, 4) for a in v] for v in out]


def next_index(run: dict, n: int = 1 + 2 * N_RING + 1) -> int | None:
    """The direction to try now: the current one unless used up, else the first not used up; None when all are."""
    used = set(run.get("used") or [])
    if run.get("idx") is not None and run["idx"] not in used:
        return run["idx"]
    return next((i for i in range(n) if i not in used), None)


def already_surveyed(err: str | None) -> bool:
    return "already been surveyed" in str(err or "").lower()


def summary(run: dict | None) -> str:
    if not run:
        return ""
    used = len(run.get("used") or [])
    found = int(run.get("found") or 0)
    if run.get("done"):
        return f"done in {run.get('star')}: all 14 directions tried, {found} new star(s)"
    return f"prospecting in {run.get('star')}: direction {min(used + 1, 14)} of 14, {found} new star(s) so far"
