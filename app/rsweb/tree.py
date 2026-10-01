"""Systems → devices → stowed devices, for the Tree tab."""
from __future__ import annotations

from collections import defaultdict


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _stowed(d: dict) -> bool:
    return str(d.get("status", "")).startswith("stowed")


def build_tree(devices: list[dict], replicants: dict, stowed_map: dict[str, list[str]],
               carrier_codes: set[str]) -> list[dict]:
    """[{star, nodes, counts, replicants}] with nodes = [{d, children, replicant, guessed}].

    Where a stowed device sits comes from (in order) the carrier's own stowed list, the host
    replicant's stowed list, or — if exactly one carrier shares its location — that carrier (marked
    as a guess). Anything else stowed is listed at the system level as "carrier unknown".
    """
    by_code = {d.get("device_code"): d for d in devices if d.get("device_code")}
    parent: dict[str, str] = {}
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

    def node(code: str, seen: frozenset) -> dict:
        kids = [node(k, seen | {code}) for k in sorted(children.get(code, []), key=lambda k: (by_code[k].get("device_type") or "", k))
                if k not in seen]
        rep = host_of.get(code)
        return {"d": by_code[code], "children": kids, "guessed": code in guessed,
                "replicant": {"code": rep[0], "name": rep[1].get("name") or rep[0]} if rep else None,
                "total": 1 + sum(k["total"] for k in kids)}

    systems: dict[str, dict] = {}
    for code, d in by_code.items():
        star = star_of(d.get("location")) or "?"
        sys_ = systems.setdefault(star, {"star": star, "nodes": [], "unknown": [], "replicants": [],
                                         "counts": defaultdict(int)})
        top = code not in parent or parent[code] not in by_code
        if top:
            if _stowed(d):
                sys_["unknown"].append(node(code, frozenset()))
            else:
                sys_["nodes"].append(node(code, frozenset()))
    for star, s in systems.items():
        for code, d in by_code.items():
            if (star_of(d.get("location")) or "?") == star:
                s["counts"]["devices"] += 1
                st = str(d.get("status") or "")
                s["counts"]["stowed" if _stowed(d) else "idle" if st.startswith(("idle", "inactive", "waiting")) else "active"] += 1
        s["counts"] = dict(s["counts"])
        # carriers and replicant hosts first, then by location and type
        s["nodes"].sort(key=lambda n: (not n["replicant"], not n["children"], n["d"].get("location") or "",
                                       n["d"].get("device_type") or "", n["d"].get("device_code")))
    for rcode, r in (replicants or {}).items():
        star = star_of(r.get("location") or r.get("current_location"))
        if star in systems:
            systems[star]["replicants"].append(r.get("name") or rcode)
    return sorted(systems.values(), key=lambda s: (-len(s["replicants"]), -s["counts"].get("devices", 0), s["star"]))
