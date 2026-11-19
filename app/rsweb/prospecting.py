"""Every fleet's galactic observatory prospects wherever it is deployed (engine stage observatory_pass).

Per observatory and system (kv "observatory_runs", {code: run}):
    star        the system it is working in (a new system starts a fresh run)
    used        indexes of directions that are used up
    idx         the direction being tried, job / seq0 / at for the prospect in flight, found = new stars here
    done        every direction used up

Directions (14): the fleet's heading — else outward from Sol — then 6 around it at 45°, 6 at 90°, and straight back.
A star can be prospected from only once (docs; live 2026-10-08: every further direction "This location has already been
surveyed"): a finished prospect, or that answer, uses the star up. "No new stars visible" (too dense that way) uses up
only that direction: the next one is tried. The observatory unfurls and prospects as soon as it is deployed; it compacts when
every direction is used up, or as soon as its fleet is set to leave (a planned next home, or loadouts moving it).
"""
from __future__ import annotations

import math

N_RING = 6
N_DIRECTIONS = 1 + 2 * N_RING + 1


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


SPARSE_R = 25.0   # ly: how far around a star the catalog is counted to judge how empty it is


def _dist(a, b) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def rank_directions(dirs: list[list[float]], here, xyz_all: dict[str, list[float]], r: float = SPARSE_R) -> list[list[float]]:
    """The directions, emptiest hemisphere first (fewest catalog stars within `r` ly on that side): the game only finds
    something where the hemisphere it looks into is sparse, and a star gives one prospect only."""
    if not here:
        return dirs
    near = [[p[i] - here[i] for i in range(3)] for p in xyz_all.values() if p and 0 < _dist(p, here) <= r]

    def crowd(d):
        return sum(1 for v in near if sum(v[i] * d[i] for i in range(3)) > 0)
    return sorted(dirs, key=crowd)   # stable: ties keep the heading-first order


SHELL_LY = 10.0   # ly: tours finish one shell around home before the next (a dense sphere grows outward)
COVER_LY = 5.0    # ly: a star this close to one prospected from (or another prospector's) lies in covered ground


def next_star(here: str, home: str, xyz_all: dict[str, list[float]], done: set[str], taken: set[str],
              allowed: set[str] | None, radius: float = 30.0, r: float = SPARSE_R) -> tuple[str | None, int]:
    """Where a prospector goes next: a star within `radius` ly of `home` not prospected from yet (`done`), not another
    prospector's (`taken`), commandable (`allowed`, None = any). Returns (star, candidates left).

    Coverage grows from home outward as a dense sphere (Joe 2026-11-19: the emptiest-neighborhood rule sent tours to the
    frontier and left big unprospected holes inside the relay network): stars in open ground (over COVER_LY from every
    star prospected from or taken) come before covered ones; then the innermost shell (SHELL_LY wide) around home; in a
    shell the biggest gap (farthest from anything prospected) first; then the emptier neighborhood (catalog stars
    within `r`), then the shorter hop."""
    h, c = xyz_all.get(home), xyz_all.get(here) or xyz_all.get(home)
    if not h:
        return None, 0
    cands = [s for s, p in xyz_all.items() if p and s not in done and s not in taken and s != here
             and _dist(p, h) <= radius and (allowed is None or s in allowed)]
    if not cands:
        return None, 0
    seen = [xyz_all[s] for s in (done | taken | {here}) if xyz_all.get(s)]
    local = [q for q in xyz_all.values() if q and _dist(q, h) <= radius + r]   # all a candidate's neighbors can be

    def score(s):
        p = xyz_all[s]
        gap = min((_dist(p, q) for q in seen), default=radius)
        crowd = sum(1 for q in local if 0 < _dist(p, q) <= r)
        return (gap < COVER_LY, int(_dist(p, h) // SHELL_LY), -round(gap), crowd, _dist(p, c) if c else 0, s)
    return min(cands, key=score), len(cands)
