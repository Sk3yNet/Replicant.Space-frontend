"""Logistics carriers spread over the hubs (owner 2026-10-23). One logistics fleet works anywhere, nearest carrier first,
and its carriers stay where their last job ended, so they drift to wherever the work was. Each hub (a stationed fleet
that takes materials in, within the sector range of the logistics fleet's home) gets a share of every carrier class,
weighted by the hub's demand over the last 3 days (logistics requests and loadout pick-ups near it), at least one each.
A carrier idle for IDLE_MINUTES at a hub over its share flies to the hub furthest under its share, one per class per pass.
A leeway (KV_LEEWAY, percent of the share, at least one carrier) lets imbalances even out by themselves: a carrier only
moves from a hub more than the leeway over its share to one more than the leeway under it (or from any hub over its
share to one with none)
(owner 2026-10-25: plates flew SOLARES to FALQUORYX and back within minutes)."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any

KV_DEMAND = "hub_demand"     # {key: {"star", "at"}}: what logistics and loadouts needed carriers for, kept DEMAND_DAYS
KV_LEEWAY = "hub_balance_leeway"   # percent of a hub's share it may be over or under before a carrier moves
LEEWAY_PCT = 25
KV_IDLE = "hub_balance_idle"  # {code: since}: when each logistics carrier was first seen idle
DEMAND_DAYS = 3
REBALANCE_RE = re.compile(r"(\w+) rebalances (\S+) → (\S+)")   # a balance_pass job's title
IDLE_MINUTES = 30
ATTACH = ("mobile_fleet", "surge_platform", "surge_carrier")
CLASSES = {"attach": "big attach carriers", "vessel": "replicant vessels", "plate": "surge plates",
           "hold": "cargo vessels", "freighter": "cargo freighters"}


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _dist(a: str, b: str, pos: dict[str, Any]) -> float:
    from .logistics import _dist as dist
    d = dist(a, b, pos)
    return math.inf if d >= 1e9 else d


def klass(d: dict, hosts: dict) -> str | None:
    """The carrier class a logistics device balances in, or None (not a carrier, or it can't surge)."""
    if "surge" not in (d.get("features") or []):
        return None
    t = d.get("device_type") or ""
    if d.get("device_code") in hosts or d.get("hosting_replicant"):
        return "vessel"
    if t in ATTACH:
        return "attach"
    return {"surge_plate": "plate", "cargo_vessel": "hold", "cargo_freighter": "freighter"}.get(t)


def hubs(fleets: list[dict], pos: dict, commandable: set[str], reach: float) -> list[str]:
    """Homes of stationed fleets that take materials in (the printing hubs), commandable, and within `reach` of a
    logistics fleet's home (ISOLITHOR, ~29,000 ly away, stays out until a carrier can get there)."""
    bases = [f["home"] for f in fleets if f.get("role") == "logistics" and f.get("home")]
    out = []
    for f in fleets:
        h = f.get("home")
        if not h or h in out or not f.get("station") or f.get("materials") != "self" or f.get("role") == "logistics":
            continue
        if h not in commandable or h not in pos:
            continue
        if bases and not any(b == h or _dist(b, h, pos) <= reach for b in bases):
            continue
        out.append(h)
    return sorted(out)


def nearest(star: str, hub_list: list[str], pos: dict) -> str | None:
    if star in hub_list:
        return star
    best = min(hub_list, key=lambda h: (_dist(star, h, pos), h), default=None)
    return best if best and _dist(star, best, pos) < math.inf else None


def weights(demand: dict, hub_list: list[str], pos: dict) -> dict[str, float]:
    w = Counter()
    for e in (demand or {}).values():
        h = nearest(star_of(e.get("star")), hub_list, pos)
        if h:
            w[h] += 1
    return {h: float(w.get(h, 0)) for h in hub_list}


def targets(n: int, w: dict[str, float]) -> dict[str, int]:
    """n carriers over the hubs: one each first (the busiest hubs when there are fewer carriers than hubs), the rest by
    weight, largest remainder (evenly when no hub has any demand)."""
    order = sorted(w, key=lambda h: (-w[h], h))
    if n <= len(order):
        return {h: int(i < n) for i, h in enumerate(order)}
    out = {h: 1 for h in order}
    rest = n - len(order)
    tot = sum(w.values())
    share = {h: rest * (w[h] / tot if tot else 1 / len(order)) for h in order}
    for h in order:
        out[h] += int(share[h])
    left = rest - sum(int(share[h]) for h in order)
    for h in sorted(order, key=lambda h: (-(share[h] - int(share[h])), -w[h], h))[:left]:
        out[h] += 1
    return out


def slack(share: int, pct: float) -> int:
    """How many carriers a hub may be over or under its share and still be left alone: pct of it, at least one
    (none at 0 %: the strict balance before 1.260.0)."""
    return max(1, round(share * pct / 100)) if pct > 0 else 0


def plan(fleets: list[dict], devices: list[dict], jobs: list[dict], hosts: dict, busy: set[str], pos: dict,
         commandable: set[str], reach: float, demand: dict, idle_since: dict[str, float], now: float,
         keep: set[str] = frozenset(), leeway: float = LEEWAY_PCT) -> dict:
    """{"hubs", "weights", "classes": {cls: {"targets", "counts"}}, "moves": [{code, cls, from, to}]}. A carrier on a job
    counts at the hub nearest where the job takes it; `idle_since` (epoch seconds) is updated in place."""
    from .fleets import fleet_tag
    from .logistics import logistics_fleets
    hub_list = hubs(fleets, pos, commandable, reach)
    out: dict = {"hubs": hub_list, "weights": {}, "classes": {}, "moves": []}
    tags = {fleet_tag(f["id"]) for f in logistics_fleets(fleets) if f.get("id")}
    going: dict[str, str] = {}
    by_code = {d.get("device_code"): d for d in devices}
    flying: set[tuple[str, str, str | None]] = set()   # (from, to, class) of rebalance trips underway
    for j in jobs:
        if j.get("status") in ("running", "waiting"):
            m = j.get("meta") or {}
            r = REBALANCE_RE.search(j.get("title") or "")
            if r and j.get("rule") == "logistics":
                flying.add((r.group(2), r.group(3), klass(by_code.get(r.group(1)) or {}, hosts)))
            to = star_of((m.get("logistics") or {}).get("to") or m.get("star"))
            for c in [j.get("device")] + list(m.get("devices") or []):
                if c and to:
                    going.setdefault(c, to)
    seen = set()
    if len(hub_list) < 2:
        idle_since.clear()
        return out
    w = weights(demand, hub_list, pos)
    out["weights"] = w
    by_cls: dict[str, list[tuple[dict, str]]] = defaultdict(list)
    for d in devices:
        code = d.get("device_code")
        if not code or not tags & set(d.get("tags") or []):
            continue
        cls = klass(d, hosts)
        if not cls or d.get("stowed_in_device_code") or d.get("attached_to_device_code"):
            continue
        here = star_of(d.get("location"))
        where = going.get(code) if code in busy else here
        h = nearest(where or here, hub_list, pos) if (where or here) else None
        if not h:
            continue
        by_cls[cls].append((d, h))
        st = str(d.get("status") or "").lower()
        if code not in busy and code not in keep and here and not st.startswith(("travel", "cruis", "surg")):
            seen.add(code)
            idle_since.setdefault(code, now)
    for c in list(idle_since):
        if c not in seen:
            idle_since.pop(c)
    for cls, items in sorted(by_cls.items()):
        t = targets(len(items), w)
        cnt = Counter(h for _, h in items)
        out["classes"][cls] = {"targets": t, "counts": {h: cnt.get(h, 0) for h in hub_list}}
        # short: more than the leeway under its share, or none at all
        short = sorted((h for h in hub_list if cnt.get(h, 0) < t[h]
                        and (not cnt.get(h, 0) or cnt.get(h, 0) < t[h] - slack(t[h], leeway))), key=lambda h: (cnt.get(h, 0) - t[h], -w[h], h))
        if not short:
            continue
        to = short[0]
        # never back the other way while a carrier of the class is still on its way here: across the 500 ly between the
        # home sector and Sol each trip takes hours (2026-10-25: plates flew SOLARES to FALQUORYX and back 2 min apart)
        need_empty = not cnt.get(to, 0)   # a hub with none gets one from any hub over its share
        free = [(d, h) for d, h in items if cnt.get(h, 0) > t[h] + (0 if need_empty else slack(t[h], leeway))
                and d["device_code"] in seen
                and (to, h, cls) not in flying
                and now - idle_since.get(d["device_code"], now) >= IDLE_MINUTES * 60
                and _dist(star_of(d.get("location")), to, pos) <= reach]
        if not free:
            continue
        d, h = min(free, key=lambda x: (_dist(star_of(x[0].get("location")), to, pos), x[0]["device_code"]))
        counts = out["classes"][cls]["counts"]
        counts[h] -= 1   # it counts where it's going from now on
        counts[to] += 1
        out["moves"].append({"code": d["device_code"], "cls": cls, "from": h, "to": to,
                             "replicant": (d.get("hosting_replicant") or {}).get("replicant_code")})
    return out
