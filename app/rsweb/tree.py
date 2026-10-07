"""Systems → devices → stowed devices, for the Tree tab."""
from __future__ import annotations

from collections import defaultdict


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _stowed(d: dict) -> bool:
    return str(d.get("status", "")).startswith("stowed")


def _counts(nodes: list[dict]) -> dict:
    c: dict[str, int] = defaultdict(int)
    for n in nodes:
        st = str(n["d"].get("status") or "")
        c["stowed" if _stowed(n["d"]) else "idle" if st.startswith(("idle", "inactive", "waiting")) else "active"] += 1
        cap = n["d"].get("operational_capacity")
        try:
            cap = float(cap) * (100 if float(cap) <= 1 else 1)
        except (TypeError, ValueError):
            cap = None
        if cap is not None and cap < 50:
            c["low"] += 1
    return dict(c)


def group_by_type(nodes: list[dict]) -> list[dict]:
    """[{type, nodes, counts, carrying, replicants}] — replicant hosts first, then by type name."""
    by: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        by[n["d"].get("device_type") or "device"].append(n)
    groups = []
    for t, ns in by.items():
        ns.sort(key=lambda n: (not n["replicant"], n["d"].get("location") or "", n["d"].get("device_code") or ""))
        groups.append({"type": t, "nodes": ns, "counts": _counts(ns),
                       "carrying": sum(len(n["children"]) for n in ns),
                       "replicants": [n["replicant"]["name"] for n in ns if n["replicant"]]})
    groups.sort(key=lambda g: (not g["replicants"], g["type"]))
    return groups


def build_tree(devices: list[dict], replicants: dict, stowed_map: dict[str, list[str]],
               carrier_codes: set[str]) -> list[dict]:
    """[{star, nodes, counts, replicants}] with nodes = [{d, children, replicant, guessed}].

    Where a stowed device sits comes from (in order) the carrier's own stowed list, the host
    replicant's stowed list, or — if exactly one carrier shares its location — that carrier (marked
    as a guess). Anything else stowed is listed at the system level as "carrier unknown".
    """
    by_code = {d.get("device_code"): d for d in devices if d.get("device_code")}
    parent: dict[str, str] = {}
    for d in devices:   # the device's own word first: stowed in / attached to
        ride = d.get("stowed_in_device_code") or d.get("attached_to_device_code")
        if ride and d.get("device_code"):
            parent[d["device_code"]] = ride
    for carrier, kids in (stowed_map or {}).items():
        for k in kids:
            parent.setdefault(k, carrier)
    host_of: dict[str, tuple[str, dict]] = {}
    known_type: dict[str, str] = {}
    for rcode, r in (replicants or {}).items():
        host = r.get("hosted_device_code")
        if host:
            host_of[host] = (rcode, r)
            for s in r.get("stowed_devices") or []:
                if isinstance(s, dict) and s.get("device_code"):
                    parent.setdefault(s["device_code"], host)
                    if s.get("device_type"):
                        known_type[s["device_code"]] = s["device_type"]
    guessed: set[str] = set()
    carriers_at: dict[str, list[str]] = defaultdict(list)
    for c in carrier_codes:
        if c in by_code:
            carriers_at[by_code[c].get("location")].append(c)
    for code, d in by_code.items():
        if _stowed(d) and code not in parent:
            here = [c for c in carriers_at.get(d.get("location"), []) if c != code]
            if len(here) == 1:
                parent[code] = here[0]
                guessed.add(code)

    # stowed entries we only know from a carrier's list (not in the device list) still get a row
    for child, carrier in parent.items():
        if child not in by_code and carrier in by_code:
            by_code[child] = {"device_code": child, "device_type": known_type.get(child, "device"), "status": "stowed",
                              "location": by_code[carrier].get("location"), "replicant_code": by_code[carrier].get("replicant_code")}

    children: dict[str, list[str]] = defaultdict(list)
    for child, carrier in parent.items():
        if child != carrier and carrier in by_code:
            children[carrier].append(child)

    def nowhere(d: dict) -> str | None:
        """Why a device has no location (the tree files it under "?")."""
        if d.get("location"):
            return None
        tr = d.get("travel") or {}
        if tr.get("destination") or tr.get("final_destination"):
            return f"in transit to {tr.get('final_destination') or tr.get('destination')}"
        ride = d.get("stowed_in_device_code") or d.get("attached_to_device_code") or parent.get(d.get("device_code"))
        if ride and ride not in by_code:
            return f"aboard {ride}, which isn't in your device list (another player's, or gone)"
        if d.get("unlisted"):
            return f"left out of the game's device list since {str(d.get('unlisted_since') or '?')[:16]}"
        if str(d.get("status") or "").startswith(("stowed", "attached")):
            return "stowed, but no carrier says it holds it"
        return "the game reports no location (seen when a device is deployed mid-surge — it can be lost)"

    def node(code: str, seen: frozenset) -> dict:
        kids = [node(k, seen | {code}) for k in sorted(children.get(code, []), key=lambda k: (by_code[k].get("device_type") or "", k))
                if k not in seen]
        rep = host_of.get(code)
        return {"d": by_code[code], "children": kids, "nowhere": nowhere(by_code[code]),
                # a carrier holding several kinds of thing gets the same type sub-groups as a system
                "groups": group_by_type(kids) if len(kids) >= 4 and len({k["d"].get("device_type") for k in kids}) > 1 else None, "guessed": code in guessed,
                "replicant": {"code": rep[0], "name": rep[1].get("name") or rep[0]} if rep else None,
                "total": 1 + sum(k["total"] for k in kids)}

    def where(code: str) -> str:
        """The system a device is in: its own location, else its carrier's (up the chain), else "?"."""
        seen = set()
        while code in by_code and code not in seen:
            seen.add(code)
            loc = by_code[code].get("location")
            if loc:
                return star_of(loc)
            code = parent.get(code)
        return "?"

    systems: dict[str, dict] = {}
    for code, d in by_code.items():
        top = code not in parent or parent[code] not in by_code
        if not top:
            continue   # it's drawn under its carrier, in the carrier's system
        star = where(code)
        sys_ = systems.setdefault(star, {"star": star, "nodes": [], "unknown": [], "replicants": [],
                                         "counts": defaultdict(int)})
        if top:
            if _stowed(d):
                sys_["unknown"].append(node(code, frozenset()))
            else:
                sys_["nodes"].append(node(code, frozenset()))
    for star, s in systems.items():
        for code, d in by_code.items():
            if where(code) == star:
                s["counts"]["devices"] += 1
                st = str(d.get("status") or "")
                s["counts"]["stowed" if _stowed(d) else "idle" if st.startswith(("idle", "inactive", "waiting")) else "active"] += 1
        s["counts"] = dict(s["counts"])
        s["groups"] = group_by_type(s["nodes"])
        # carriers and replicant hosts first, then by location and type
        s["nodes"].sort(key=lambda n: (not n["replicant"], not n["children"], n["d"].get("location") or "",
                                       n["d"].get("device_type") or "", n["d"].get("device_code")))
    for rcode, r in (replicants or {}).items():
        star = star_of(r.get("location") or r.get("current_location"))
        if star in systems:
            systems[star]["replicants"].append(r.get("name") or rcode)
    return sorted(systems.values(), key=lambda s: (-len(s["replicants"]), -s["counts"].get("devices", 0), s["star"]))
