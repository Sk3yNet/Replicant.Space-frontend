"""Dashboard › Hub stockpiles (Joe 2026-11-20): each printing hub's stockpile at its printing location(s) over time.

A hub is a receiving fleet's home (fleets.receiving_points). Its printing locations are the spots in that system with a
working general factory (loadouts.is_factory, spares left out); a hub with none counts its drop point. The sum of
those stockpiles per resource is saved in hub_inventory_history on each inventory poll that changes it, so no extra
game reads."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .loadouts import star_of
from .shapes import as_amounts

KV = "hub_inventory_totals"


def factory_locs(devices: list[dict] | None) -> frozenset[str]:
    """Every location with a working general factory (spares left out)."""
    from .loadouts import is_factory
    return frozenset(d["location"] for d in devices or [] if isinstance(d, dict) and d.get("location") and is_factory(d)
                     and "spare" not in (d.get("tags") or []))


def spots(fleets: list[dict], factories: frozenset[str] | set[str], inventory: dict[str, dict],
          stars: dict | None = None) -> dict[str, list[str]]:
    """{hub star: [printing locations]}, sorted."""
    from . import fleets as fl
    from .loadouts import drop_point
    hubs = fl.receiving_points([f for f in fleets or [] if isinstance(f, dict)])
    out: dict[str, set[str]] = {h: set() for h in hubs}
    for loc in factories:
        if star_of(loc) in out:
            out[star_of(loc)].add(loc)
    for h, locs in out.items():
        if not locs:
            locs.add(drop_point(h, [], inventory, stars or {}))
    return {h: sorted(l) for h, l in sorted(out.items())}


def totals(where: dict[str, list[str]], inventory: dict[str, dict]) -> dict[str, dict[str, float]]:
    """{hub: {resource: qty}} summed over its printing locations."""
    out: dict[str, dict[str, float]] = {}
    for h, locs in where.items():
        t: dict[str, float] = defaultdict(float)
        for loc in locs:
            for k, v in as_amounts(inventory.get(loc) or {}).items():
                t[k] += float(v)
        out[h] = dict(t)
    return out


async def record(db, locs: list[dict], ts: str) -> None:
    """Called by the inventory sync: save each hub's totals when they changed (a row per hub and resource)."""
    inv = {i.get("location"): i.get("items") or {} for i in locs if isinstance(i, dict)}
    where = spots(await db.kv_get("fleets", []) or [], await db.kv_derived("devices", "hub_factory_locs", factory_locs, frozenset()), inv)
    now = totals(where, inv)
    prev = await db.kv_get(KV, {}) or {}
    for h, t in now.items():
        if prev.get(h) == t and h in prev:
            continue
        for k in set(t) | set(prev.get(h) or {}):   # a resource used up drops to 0 rather than vanishing
            await db.execute("INSERT OR REPLACE INTO hub_inventory_history(ts, hub, resource, qty) VALUES(?,?,?,?)",
                             (ts, h, k, t.get(k, 0.0)))
    await db.kv_set(KV, now)
    await db.kv_set("hub_inventory_spots", where)


async def history(db, since: str, now: str | None = None) -> dict[str, dict[str, list[tuple[str, float]]]]:
    """{hub: {resource: [(ts, qty)]}} for the window. Rows are saved only on change, so a hub whose pile sat still
    (Gamma at NIO, 1.569.0) had no rows in a 2-day window and its chart went blank: each line starts with its last value
    from before the window (at `since`) and, with `now`, runs on to now at its latest value."""
    raw: dict[str, dict[str, list[tuple[str, float]]]] = defaultdict(lambda: defaultdict(list))
    for r in await db.fetchall("SELECT hub, resource, qty, MAX(ts) AS ts FROM hub_inventory_history WHERE ts < ? "
                               "GROUP BY hub, resource", (since,)):
        raw[r["hub"]][r["resource"]].append((since, r["qty"]))
    for r in await db.fetchall("SELECT ts, hub, resource, qty FROM hub_inventory_history WHERE ts >= ? ORDER BY ts", (since,)):
        raw[r["hub"]][r["resource"]].append((r["ts"], r["qty"]))
    if now:
        for res in raw.values():
            for pts in res.values():
                if pts[-1][0] < now:
                    pts.append((now, pts[-1][1]))
    return raw
