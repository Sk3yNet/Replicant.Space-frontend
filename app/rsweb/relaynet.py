"""Your FTL relay network (Map › Relays, and the galaxy map's relay-network overlay).

Nodes are your working relays (status relaying) and active system hubs, one per system (the longest reach there). Two
nodes link when they're within reach of each other: 7.5 ly for a relay, 15 ly for a hub — or the range the device
itself reports (`range_ly` / `relay_range_ly`: upgraded relays may reach further). Other players' relays don't join
your network (confirmed by the owner 2026-10-12). Linked nodes form a network; each network gets its own color.

Bridges: for each network, the fewest new relays (each a 7.5 ly hop, any system — relays are allowed in warded systems)
that link it to another of your networks, at most MAX_BRIDGE new relays. One suggestion per pair of networks.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from typing import Any

RELAY_LY = 7.5
HUB_LY = 15.0
MAX_BRIDGE = 10
PALETTE = ["#4fc3f7", "#ffb74d", "#ba68c8", "#81c784", "#f06292", "#fff176", "#4db6ac", "#e57373", "#9575cd", "#a1887f"]


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _xyz(p: Any) -> tuple[float, float, float] | None:
    if isinstance(p, dict) and any(k in p for k in "xyz"):
        return (float(p.get("x") or 0), float(p.get("y") or 0), float(p.get("z") or 0))
    return None


RANGE_KEYS = ("relay_range_ly", "relay_range", "range_ly", "comm_range_ly")   # blueprints carry it since 2026-10-12


def own_range(x: dict | None) -> float | None:
    """A relay range a device or blueprint reports (the field name isn't confirmed yet: the likely ones are tried)."""
    for k in RANGE_KEYS:
        try:
            v = float((x or {}).get(k) or 0)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return None


def reach(d: dict, bps: dict[str, dict] | None = None) -> float | None:
    """How far a working relay / hub of yours links, else None: its own reported range, else its blueprint's, else the
    known default."""
    t = d.get("device_type") or ""
    st = str(d.get("status") or "")
    if not d.get("location") or d.get("location_stale") or d.get("stowed_in_device_code") or d.get("attached_to_device_code"):
        return None
    own = own_range(d) or own_range((bps or {}).get(t))
    if "relay" in t and st.startswith("relaying"):
        return float(own or RELAY_LY)
    if "hub" in t and st and not st.startswith(("inactive", "idle", "stowed", "compact", "travel", "cruis", "surg", "unfurl")):
        return float(own or HUB_LY)
    return None


def nodes(devices: list[dict], pos: dict[str, Any], bps: dict[str, dict] | None = None) -> dict[str, dict]:
    """{star: {star, xyz, range, devices: [codes], kinds}} — one node per system with a working relay / hub of yours."""
    out: dict[str, dict] = {}
    for d in devices:
        r = reach(d, bps)
        s = star_of(d.get("location"))
        xyz = _xyz(pos.get(s))
        if r is None or not xyz:
            continue
        n = out.setdefault(s, {"star": s, "xyz": xyz, "range": 0.0, "devices": [], "kinds": set()})
        n["range"] = max(n["range"], r)
        n["devices"].append(d.get("device_code"))
        n["kinds"].add("hub" if "hub" in (d.get("device_type") or "") else "relay")
    for n in out.values():
        n["kinds"] = sorted(n["kinds"])
    return out


def networks(devices: list[dict], pos: dict[str, Any], bps: dict[str, dict] | None = None) -> dict:
    """{"networks": [{id, color, stars, relays, hubs}], "links": [{a, b, ly, net}], "of": {star: net id}} — biggest first."""
    ns = nodes(devices, pos, bps)
    stars = sorted(ns)
    parent = {s: s for s in stars}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    links = []
    for i, a in enumerate(stars):
        for b in stars[i + 1:]:
            ly = math.dist(ns[a]["xyz"], ns[b]["xyz"])
            if ly <= max(ns[a]["range"], ns[b]["range"]) + 1e-9:
                links.append({"a": a, "b": b, "ly": round(ly, 2)})
                parent[find(a)] = find(b)
    groups: dict[str, list[str]] = defaultdict(list)
    for s in stars:
        groups[find(s)].append(s)
    ordered = sorted(groups.values(), key=lambda g: (-len(g), g[0]))
    out, of = [], {}
    for i, g in enumerate(ordered):
        for s in g:
            of[s] = i
        out.append({"id": i, "color": PALETTE[i % len(PALETTE)], "stars": sorted(g),
                    "relays": sum(1 for s in g if "relay" in ns[s]["kinds"]),
                    "hubs": sum(1 for s in g if "hub" in ns[s]["kinds"]),
                    "devices": sorted(c for s in g for c in ns[s]["devices"])})
    for l in links:
        l["net"] = of[l["a"]]
    hop = own_range((bps or {}).get("ftl_relay")) or RELAY_LY   # a new relay's reach (bridges)
    return {"networks": out, "links": links, "of": of, "nodes": ns, "hop": hop}


def _grid(pos: dict[str, Any]) -> tuple[dict, dict]:
    xyz_all = {s: x for s, x in ((s, _xyz(p)) for s, p in pos.items()) if x}
    cells: dict[tuple, list[tuple[str, tuple]]] = defaultdict(list)
    for s, x in xyz_all.items():
        cells[tuple(int(math.floor(c / RELAY_LY)) for c in x)].append((s, x))
    return xyz_all, cells


def _near(cells: dict, x: tuple, r: float) -> list[str]:
    k = int(math.ceil(r / RELAY_LY))
    cx = [int(math.floor(c / RELAY_LY)) for c in x]
    out = []
    for dx in range(-k, k + 1):
        for dy in range(-k, k + 1):
            for dz in range(-k, k + 1):
                for s, y in cells.get((cx[0] + dx, cx[1] + dy, cx[2] + dz), ()):
                    if math.dist(x, y) <= r + 1e-9:
                        out.append(s)
    return out


def replicant_stars(replicants: dict[str, dict], devices: list[dict]) -> set[str]:
    """Systems your replicants are in. A matrix stowed in a container that rides a fleet carrier reports no location of
    its own: it's where the outermost carrier is."""
    by = {d.get("device_code"): d for d in devices}
    out = set()
    for r in (replicants or {}).values():
        loc = r.get("location") or r.get("current_location")
        c, seen = r.get("hosted_device_code"), set()
        while not loc and c and c not in seen:
            seen.add(c)
            d = by.get(c) or {}
            loc = d.get("location")
            c = d.get("attached_to_device_code") or d.get("stowed_in_device_code")
        if star_of(loc):
            out.add(star_of(loc))
    return out


def coverage(net: dict, pos: dict[str, Any], replicant_stars: set[str]) -> dict:
    """Where you can give commands: {"covered": {net id: systems within its nodes' reach}, "active": {net ids with one of
    your replicants inside their coverage}, "commandable": systems an active network covers, or where a replicant is}."""
    _, cells = _grid(pos)
    covered: dict[int, set[str]] = defaultdict(set)
    for s, n in net["nodes"].items():
        covered[net["of"][s]] |= set(_near(cells, n["xyz"], n["range"])) | {s}
    active = {i for i, c in covered.items() if c & replicant_stars}
    commandable = set(replicant_stars)
    for i in active:
        commandable |= covered[i]
    for g in net["networks"]:
        g["active"] = g["id"] in active
    return {"covered": dict(covered), "active": active, "commandable": commandable}


def bridges(net: dict, pos: dict[str, Any], max_new: int = MAX_BRIDGE) -> list[dict]:
    """The fewest new relays linking each pair of your networks (≤ max_new). [{a, b, stars: [new relay systems in order],
    n}] — one per pair, cheapest first. A breadth-first search from each network over catalog stars, 7.5 ly a hop."""
    ns, of = net["nodes"], net["of"]
    if len(net["networks"]) < 2:
        return []
    xyz_all, cells = _grid(pos)   # a 7.5 ly grid for neighbor lookups

    def near(x: tuple, r: float) -> list[str]:
        return _near(cells, x, r)

    hop = net.get("hop") or RELAY_LY

    def links_to(s: str, x: tuple) -> set[int]:
        """Networks a new relay at s would join: any node within its reach or within the node's own reach."""
        return {of[o] for o in near(x, max([hop] + [n["range"] for n in ns.values()])) if o in ns
                and math.dist(x, ns[o]["xyz"]) <= max(hop, ns[o]["range"]) + 1e-9}
    best: dict[tuple[int, int], dict] = {}
    for g in net["networks"]:
        start = g["id"]
        prev: dict[str, str | None] = {}
        depth: dict[str, int] = {}
        q: deque[str] = deque()
        for s in g["stars"]:   # first hop: systems a new relay could link this network from
            for t in near(ns[s]["xyz"], max(hop, ns[s]["range"])):
                if t not in ns and t not in depth:
                    depth[t], prev[t] = 1, None
                    q.append(t)
        while q:
            s = q.popleft()
            joined = links_to(s, xyz_all[s]) - {start}
            for other in joined:
                key = tuple(sorted((start, other)))
                if key not in best or best[key]["n"] > depth[s]:
                    path, c = [], s
                    while c:
                        path.append(c)
                        c = prev[c]
                    best[key] = {"a": key[0], "b": key[1], "stars": list(reversed(path)), "n": depth[s]}
            if joined or depth[s] >= max_new:
                continue
            for t in near(xyz_all[s], hop):
                if t not in ns and t not in depth:
                    depth[t], prev[t] = depth[s] + 1, s
                    q.append(t)
    return sorted(best.values(), key=lambda b: (b["n"], b["a"], b["b"]))
