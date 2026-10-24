"""Relay rollout (Map › Relays): a relay in every system, nearest the designated seed systems first.

The engine stage relay_rollout_pass keeps as many relay orders open as the logistics vessels with a replicant aboard
have free hold slots (capacity; at least one), with no other cap (owner 2026-10-20). ward_orders.py does the work:
activate an idle one there, else send a spare, else print one in range; the loadout pass then fills a carrier's hold
with relays bound for several systems and drops them nearest first. When one finishes or fails it orders the next system in
line. Chained (the default), only a system within 7.5 ly of a working relay or hub of yours, a relay on order or a seed
is next in line, so the network grows outward in one piece. A system whose order fails (no spare and no factory in
range, or nothing got there before the order expired) is skipped until you retry it; after MAX_STREAK failures in a row the rollout pauses (nothing can get a relay
anywhere).

Each seed has its own radius (radii: {seed: ly}, owner 2026-10-24: SOLARES needs far less than FALQUORYX); a seed without
one uses radius, the default. A system is in line when it lies within some seed's radius, and counts for the nearest such seed.

State in kv "relay_rollout": {on, seeds: [star], radius, radii, chained, capacity (last seen), ordered: {star: at}, skipped: {star: why},
done: int, streak: failures in a row, by}.
"""
from __future__ import annotations

import math
from typing import Any

from .relaynet import RELAY_LY, _grid, _near

KV = "relay_rollout"
MAX_STREAK = 5
DEFAULTS = {"on": False, "seeds": [], "radius": 50.0, "radii": {}, "chained": True, "capacity": 0, "ordered": {}, "skipped": {},
            "done": 0, "streak": 0, "by": ""}


def state(raw: dict | None) -> dict:
    out = {**DEFAULTS, **(raw or {})}
    out["ordered"] = dict(out.get("ordered") or {})
    out["skipped"] = dict(out.get("skipped") or {})
    out["radii"] = {k: float(v) for k, v in (out.get("radii") or {}).items() if k in out["seeds"]}
    return out


def queue(seeds: list[str], pos: dict[str, Any], have: set[str], ordered: set[str], skipped: set[str],
          radius: float, chained: bool, limit: int | None = None, radii: dict[str, float] | None = None) -> list[dict]:
    """Systems still without a relay, in rollout order: [{star, ly (to the nearest seed), seed}], nearest first.
    A seed's reach is radii[seed], else radius; a system counts for the nearest seed whose reach it lies in.
    Chained: only those within a relay's hop of a system that has one, has one on order, or is a seed, growing outward
    as each is reached (the order the rollout would go in if every order succeeded)."""
    xyz_all, cells = _grid(pos)
    seed_xyz = [(s, xyz_all[s]) for s in seeds if s in xyz_all]
    if not seed_xyz:
        return []

    reach = {s: float((radii or {}).get(s, radius)) for s, _ in seed_xyz}

    def nearest_seed(x: tuple) -> tuple[str, float] | None:
        inside = [(s, d) for s, y in seed_xyz if (d := math.dist(x, y)) <= reach[s] + 1e-9]
        return min(inside, key=lambda t: t[1]) if inside else None

    todo: dict[str, tuple[str, float]] = {}
    for s, x in xyz_all.items():
        if s in have or s in ordered or s in skipped:
            continue
        if (hit := nearest_seed(x)):
            todo[s] = hit
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


def capacity(devices: list[dict], logistics_tags: set[str], free) -> int:
    """Free hold slots on logistics vessels with a replicant aboard (hosted, or its matrix riding in them): the relays
    that can be on their way at once. `free(device)` gives a carrier's free slots (loadouts._free)."""
    from .logistics import riding_hosts
    hosts = {d["device_code"]: (d.get("hosting_replicant") or {}).get("replicant_code") or "?" for d in devices
             if d.get("hosting_replicant")}
    escorts = riding_hosts(devices, hosts)
    return sum(max(0, int(free(d))) for d in devices
               if d.get("device_code") in escorts and logistics_tags & set(d.get("tags") or [])
               and "surge" in (d.get("features") or []))
