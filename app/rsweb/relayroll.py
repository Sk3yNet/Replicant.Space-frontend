"""Relay rollout (Map › Relays): a relay in every system, nearest the designated seed systems first.

The engine stage relay_rollout_pass keeps up to `at_once` relay orders open (ward_orders.py does the work: activate an
idle one there, else send a spare, else print one in range). When one finishes or fails it orders the next system in
line. Chained (the default), only a system within 7.5 ly of a working relay or hub of yours, a relay on order or a seed
is next in line, so the network grows outward in one piece. A system whose order fails (no spare and no factory in
range, or nothing got there before the order expired) is skipped until you retry it; after MAX_STREAK failures in a row the rollout pauses (nothing can get a relay
anywhere).

State in kv "relay_rollout": {on, seeds: [star], radius, at_once, chained, ordered: {star: at}, skipped: {star: why},
done: int, streak: failures in a row, by}.
"""
from __future__ import annotations

import math
from typing import Any

from .relaynet import RELAY_LY, _grid, _near

KV = "relay_rollout"
MAX_STREAK = 5
DEFAULTS = {"on": False, "seeds": [], "radius": 50.0, "at_once": 3, "chained": True, "ordered": {}, "skipped": {},
            "done": 0, "streak": 0, "by": ""}


def state(raw: dict | None) -> dict:
    out = {**DEFAULTS, **(raw or {})}
    out["ordered"] = dict(out.get("ordered") or {})
    out["skipped"] = dict(out.get("skipped") or {})
    return out


def queue(seeds: list[str], pos: dict[str, Any], have: set[str], ordered: set[str], skipped: set[str],
          radius: float, chained: bool, limit: int | None = None) -> list[dict]:
    """Systems still without a relay, in rollout order: [{star, ly (to the nearest seed), seed}], nearest first.
    Chained: only those within a relay's hop of a system that has one, has one on order, or is a seed, growing outward
    as each is reached (the order the rollout would go in if every order succeeded)."""
    xyz_all, cells = _grid(pos)
    seed_xyz = [(s, xyz_all[s]) for s in seeds if s in xyz_all]
    if not seed_xyz:
        return []

    def nearest_seed(x: tuple) -> tuple[str, float]:
        return min(((s, math.dist(x, y)) for s, y in seed_xyz), key=lambda t: t[1])

    todo: dict[str, tuple[str, float]] = {}
    for s, x in xyz_all.items():
        if s in have or s in ordered or s in skipped:
            continue
        seed, d = nearest_seed(x)
        if d <= radius + 1e-9:
            todo[s] = (seed, d)
    rank = lambda s: (todo[s][1], s)  # noqa: E731
    if not chained:
        out = sorted(todo, key=rank)
    else:   # a frontier search: always the nearest-to-a-seed system that touches what's already there or planned
        import heapq
        reached = {s for s in have | ordered if s in xyz_all} | {s for s, _ in seed_xyz}
        heap: list[tuple[float, str]] = []
        queued: set[str] = set()

        def push_near(s: str) -> None:
            for t in _near(cells, xyz_all[s], RELAY_LY):
                if t in todo and t not in queued:
                    queued.add(t)
                    heapq.heappush(heap, rank(t))
        for s in [s for s, _ in seed_xyz if s in todo]:   # a seed without a relay goes first
            queued.add(s)
            heapq.heappush(heap, rank(s))
        for s in reached:
            push_near(s)
        out = []
        while heap and (limit is None or len(out) < limit):
            _, s = heapq.heappop(heap)
            out.append(s)
            push_near(s)
    if limit is not None:
        out = out[:limit]
    return [{"star": s, "ly": round(todo[s][1], 2), "seed": todo[s][0]} for s in out]
