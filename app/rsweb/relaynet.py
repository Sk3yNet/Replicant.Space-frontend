"""Your FTL relay network (Map › Relays, and the galaxy map's relay-network overlay).

Nodes are your working relays (status relaying) and active system hubs, one per system (the longest reach there). Two
nodes link when they're within reach of each other: 7.5 ly for a relay, 15 ly for a hub — or the range the device
itself reports (`range_ly` / `relay_range_ly`: upgraded relays may reach further). Other players' relays don't join
your network (confirmed by the owner 2026-10-12). Hubs link by distance like relays (15 ly): the docs' "mesh across the
galaxy" isn't what the game does — live 2026-10-15, the SOLARES hub's network listed only nodes within 15 ly, not the
FALQUORYX hub. Linked nodes form a network; each network gets its own color.

Commands: that range is only for relays talking to each other. A relay or hub delivers commands to its own system only
(owner, 2026-10-18), so a network commands the systems its nodes are in, and only while one of your replicants is in
one of them.

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
# one color per network, clear of the map's hazard red, resource orange, anomaly magenta and supply amber
PALETTE = ["#4fc3f7", "#fff176", "#69f0ae", "#b39ddb", "#d4e157", "#80cbc4", "#e0e0e0", "#90a4ff", "#c5a880", "#a5d6a7"]


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
    if "relay" in t and (st.startswith("relaying") or (st.startswith("out_of_range") and d.get("_on"))):
        # out of range and switched on (the status doesn't say which: d["_on"], from the activation history — see
        # web.relays_on): linked to nothing, a network of its own
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
                links.append({"a": a, "b": b, "ly": round(ly, 2),
                              "hub": "hub" in ns[a]["kinds"] or "hub" in ns[b]["kinds"]})
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


async def star_pos(db) -> dict[str, Any]:
    """{star: position} for every catalog star with one, built once per catalog update and shared (same as the
    engine's star_lookup("pos")): callers only read it, and star_grid / bridges memoize on it."""
    return await db.kv_derived("stars", "pos", lambda c: {x.get("designation"): x["position"] for x in (c or {}).get("stars") or []
                                                          if isinstance(x, dict) and x.get("position")}, {})


_grid_memo: list = [None, None]   # [pos it was built from, (xyz_all, cells)]


def star_grid(pos: dict[str, Any]) -> tuple[dict, dict]:
    """_grid(pos), kept while the same pos dict comes back (star_pos): building it walks the whole catalog (~30k stars),
    and the device sync and the map asked for it every time (sampled live 2026-10-27)."""
    if _grid_memo[0] is not pos:
        _grid_memo[0], _grid_memo[1] = pos, _grid(pos)
    return _grid_memo[1]


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


def dark_stars(devices: list[dict]) -> set[str]:
    """Systems the game says are out of comms range: a device of yours there reports in_control_range false and none
    there reports true. The game's word beats the distance model (live 2026-10-18: 18 logistics devices sent to
    RHYBRENEUS, which the model counted as covered, all out_of_range there)."""
    dark, lit = set(), set()
    for d in devices:
        s = star_of(d.get("location"))
        if not s or d.get("location_stale") or d.get("stowed_in_device_code") or d.get("attached_to_device_code"):
            continue
        if d.get("in_control_range") is False:
            dark.add(s)
        elif d.get("in_control_range") is True:
            lit.add(s)
    return dark - lit


def coverage(net: dict, pos: dict[str, Any], replicant_stars: set[str]) -> dict:
    """Where you can give commands: {"covered": {net id: the systems its nodes are in}, "active": {net ids with one of
    your replicants in one of those systems}, "commandable": systems an active network covers, or where a replicant is}.
    A relay's range only links it to other relays: it delivers commands to its own system alone."""
    covered: dict[int, set[str]] = defaultdict(set)
    for s in net["nodes"]:
        covered[net["of"][s]].add(s)
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
    key = (frozenset((n, of[n], ns[n]["range"]) for n in ns), net.get("hop"), max_new, id(pos), len(pos))
    if _bridge_memo.get("key") == key and _bridge_memo.get("pos") is pos:   # the map asks on every refresh
        return [dict(b, stars=list(b["stars"])) for b in _bridge_memo["out"]]
    out = _bridges(net, pos, max_new)
    _bridge_memo.update(key=key, pos=pos, out=out)
    return [dict(b, stars=list(b["stars"])) for b in out]


_bridge_memo: dict = {}


def _bridges(net: dict, pos: dict[str, Any], max_new: int) -> list[dict]:
    ns, of = net["nodes"], net["of"]
    xyz_all, cells = star_grid(pos)   # a 7.5 ly grid for neighbor lookups

    def near(x: tuple, r: float) -> list[str]:
        return _near(cells, x, r)

    hop = net.get("hop") or RELAY_LY

    def links_to(s: str, x: tuple) -> set[int]:
        """Networks a new relay at s would join: any node within its reach or within the node's own reach."""
        return {of[o] for o in near(x, reach) if o in ns and math.dist(x, ns[o]["xyz"]) <= max(hop, ns[o]["range"]) + 1e-9}
    reach = max([hop] + [n["range"] for n in ns.values()])
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
