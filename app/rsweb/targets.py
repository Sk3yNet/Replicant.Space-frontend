"""Everything we know is *in one star system*, gathered from the latest scan reports.

Sources, newest knowledge wins for notes:
  • the cached system scan (planets, belts, Kuiper/Oort, entry point)
  • cached location details (belt resource sites, planet/moon/object details)
  • any event that mentions a code in this system (scan/search reports, salvage, objects, arrivals …)
  • stockpiles (inventory) and where your devices are
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from .shapes import as_amounts

LEVELS = ["scarce", "low", "moderate", "high", "rich"]
CATEGORY_ORDER = ["belt", "site", "salvage", "stockpile", "planet", "moon", "lagrange", "object", "outer", "star", "other"]
CATEGORY_LABEL = {"belt": "Asteroid belts", "site": "Resource sites", "salvage": "Salvage", "stockpile": "Stockpiles",
                  "planet": "Planets", "moon": "Moons", "lagrange": "Lagrange points", "object": "Objects",
                  "outer": "Outer system", "star": "Star", "other": "Other"}


def category(code: str, star: str) -> str:
    rest = code[len(star):].lstrip("-")
    if not rest:
        return "star"
    if re.search(r"-SAL-\d+$", code):
        return "salvage"
    if re.fullmatch(r"BELT-\d+-SITE-\d+", rest):
        return "site"
    if re.fullmatch(r"BELT-\d+", rest):
        return "belt"
    if rest in ("KUIPER", "OORT"):
        return "outer"
    if re.fullmatch(r"OBJ-\d+", rest):
        return "object"
    if re.fullmatch(r"\d+-L[1-5]", rest):
        return "lagrange"
    if re.fullmatch(r"\d+", rest):
        return "planet"
    if re.fullmatch(r"\d+-\d+", rest):
        return "moon"
    return "other"


def _res_note(resources: Any) -> str:
    if not isinstance(resources, dict):
        return ""
    ranked = sorted(((r, lvl) for r, lvl in resources.items() if isinstance(lvl, str)),
                    key=lambda x: -(LEVELS.index(x[1]) if x[1] in LEVELS else -1))
    good = [f"{r} {lvl}" for r, lvl in ranked if lvl in ("high", "rich")]
    return ", ".join(good[:3]) or ", ".join(f"{r} {lvl}" for r, lvl in ranked[:2])


def in_star(code: str, star: str) -> bool:
    """`code` is the system `star` or somewhere in it (KEL-3 is in KEL; KELMORNEA-3 isn't)."""
    return code == star or code.startswith(star + "-")


def _codes_in(obj: Any, pattern: re.Pattern) -> set[str]:
    return set(pattern.findall(json.dumps(obj))) if obj else set()


async def system_targets(db, star: str) -> dict:
    """{"targets": [{code, category, note, stock}], "by_category": {...}, "resources": {res: {level, stock}}}."""
    star = (star or "").upper()
    if not star:
        return {"targets": [], "by_category": {}, "resources": {}, "scanned": None}
    pat = re.compile(r'"(' + re.escape(star) + r'(?:-[A-Z0-9]+)*)"')
    found: dict[str, dict] = {}

    def add(code: str | None, note: str = "", **extra) -> None:
        if not isinstance(code, str) or not (code == star or code.startswith(star + "-")):
            return
        t = found.setdefault(code, {"code": code, "category": category(code, star), "note": "", "stock": {}})
        if note and not t["note"]:
            t["note"] = note
        t.update({k: v for k, v in extra.items() if v})

    best_level: dict[str, int] = {}
    row = await db.fetchone("SELECT data, updated_at FROM systems WHERE star=?", (star,))
    scanned = row["updated_at"] if row else None
    scan = json.loads(row["data"]) if row else {}
    add(star, f"{(scan.get('star') or {}).get('spectral_type', '')} star".strip())
    for p in scan.get("planets") or []:
        des = p.get("designation")
        note = " · ".join(x for x in [p.get("type"), "habitable zone" if p.get("in_habitable_zone") else "",
                                        f"{p.get('moon_count')} moons" if p.get("moon_count") else ""] if x)
        add(des, note)
        for lp in ("L4", "L5"):
            add(f"{des}-{lp}", f"Lagrange point of {des}")
    for b in ((scan.get("asteroid_belt") or {}).get("belts")) or []:
        add(b.get("designation"), " · ".join(x for x in [b.get("density"), _res_note(b.get("resources"))] if x))
        for r, lvl in (b.get("resources") or {}).items():
            if lvl in LEVELS:
                best_level[r] = max(best_level.get(r, -1), LEVELS.index(lvl))
    outer = scan.get("outer_system") or {}
    for k in ("kuiper", "oort"):
        o = outer.get(k) or {}
        add(o.get("designation"), f"{k.title()} · {o.get('distance_au', '?')} AU")
    if scan.get("entry_point"):
        add(scan["entry_point"], "entry point")

    # cached location details (resource sites etc.)
    for r in await db.fetchall("SELECT key, value FROM kv WHERE key LIKE ?", (f"loc:{star}%",)):
        loc_code = r["key"][4:]
        if not (loc_code == star or loc_code.startswith(star + "-")):
            continue
        detail = json.loads(r["value"])
        for site in detail.get("resource_sites") or []:
            if isinstance(site, str):
                add(site, "resource site")
            elif isinstance(site, dict):
                code = site.get("designation") or site.get("site") or site.get("code") or site.get("location")
                res = site.get("resource_type") or site.get("resource")
                lvl = site.get("availability") or site.get("abundance")
                add(code, " · ".join(str(x) for x in [res, lvl] if x) or "resource site")
        obj = detail.get("object") or {}
        if obj:
            add(obj.get("designation"), " · ".join(str(x) for x in [obj.get("object_type"), obj.get("composition")] if x))
        for c in _codes_in(detail, pat):
            add(c)

    # events: scan/search reports, salvage, mining sites, arrivals …
    rows = await db.fetchall("SELECT event, location, payload FROM events WHERE star=? OR location LIKE ? "
                             "ORDER BY seq DESC LIMIT 3000", (star, f"{star}-%"))
    for r in rows:
        p = json.loads(r["payload"] or "{}")
        name = r["event"]
        if name == "salvage.discovered":
            add(p.get("designation") or p.get("location"), " · ".join(str(x) for x in [p.get("name"), p.get("salvage_type")] if x) or "salvage")
        elif name in ("mining.started", "mining.retargeted"):
            add(p.get("site"), " · ".join(str(x) for x in [p.get("resource_type") or p.get("new_resource"), p.get("availability")] if x))
        elif name == "system.object_detected":
            add(p.get("object_designation"), f"incoming · {p.get('size_class', '')}".strip(" ·"))
        elif name in ("site.depleted", "salvage.depleted"):
            add(p.get("site"), "depleted")
        add(r["location"])
        for c in _codes_in(p, pat):
            add(c)

    # stockpiles & your devices
    stock_total: dict[str, float] = defaultdict(float)
    for inv in await db.kv_get("inventory", []) or []:
        loc = inv.get("location") or ""
        if loc == star or loc.startswith(star + "-"):
            items = as_amounts(inv.get("items"))
            add(loc)
            found[loc]["stock"] = items
            for k, v in items.items():
                stock_total[k] += v
    here_devices: dict[str, int] = defaultdict(int)
    for d in await db.kv_get("devices", []) or []:
        loc = d.get("location") or ""
        if loc == star or loc.startswith(star + "-"):
            here_devices[loc] += 1
            add(loc)
    for code, t in found.items():
        t["devices"] = here_devices.get(code, 0)
        bits = [t["note"]] if t["note"] else []
        if t["stock"]:
            top = sorted(t["stock"].items(), key=lambda kv: -kv[1])[:2]
            bits.append("stock " + ", ".join(f"{int(q)} {r}" for r, q in top))
        if t["devices"]:
            bits.append(f"{t['devices']} of your devices")
        t["label"] = " · ".join(bits)

    res = await system_resources(db, star)
    hidden = res["hidden"]
    # a site or salvage is shown only while the resource data counts it as live — codes picked up from old event
    # payloads (digests, logs) never went through the depleted / closed checks
    live = {x["code"] for x in res["sites_shown"]} | {x["code"] for x in res["salvage_shown"]}
    targets = sorted((t for t in found.values() if t["code"] not in hidden
                      and (t["category"] not in ("site", "salvage") or t["code"] in live)),
                     key=lambda t: (CATEGORY_ORDER.index(t["category"]), t["code"]))
    by_cat: dict[str, list] = defaultdict(list)
    for t in targets:
        by_cat[t["category"]].append(t)
        if t["stock"]:
            by_cat["stockpile"].append(t)
    resources = {r: {"level": LEVELS[best_level[r]] if r in best_level else None, "stock": stock_total.get(r, 0)}
                 for r in set(best_level) | set(stock_total)}
    return {"star": star, "targets": targets, "by_category": dict(by_cat), "resources": resources, "scanned": scanned}


def options_for(sys_t: dict, categories: list[str] | None) -> list[tuple[str, list[dict]]]:
    """Grouped options for a location <select>: [(group label, [targets])], limited to `categories` if given."""
    cats = categories or [c for c in CATEGORY_ORDER if c != "stockpile"]
    groups = []
    seen: set[str] = set()
    for c in cats:
        items = [t for t in sys_t.get("by_category", {}).get(c, []) if t["code"] not in seen]
        seen.update(t["code"] for t in items)
        if items:
            groups.append((CATEGORY_LABEL.get(c, c), items))
    return groups


# --- quantities: what can be mined or salvaged in a system ------------------------------------------
QTY_KEYS = ("quantity", "remaining", "remaining_quantity", "amount", "available", "reserve", "reserves", "total", "size")


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def site_quantity(site: dict) -> tuple[dict[str, float], float | None]:
    """({resource: qty}, total) from a resource-site or salvage record, whatever the field names are."""
    res = site.get("resources")
    if isinstance(res, (dict, list)):
        amounts = {k: v for k, v in as_amounts(res).items() if v}
        if amounts:
            return amounts, sum(amounts.values())
    for k in QTY_KEYS:
        q = _num(site.get(k))
        if q is not None:
            r = site.get("resource_type") or site.get("resource")
            return ({r: q} if r else {}), q
    return {}, None


def _site_code(site: dict) -> str | None:
    return site.get("designation") or site.get("site") or site.get("code") or site.get("location")


async def system_resources(db, star: str) -> dict:
    """Mining sites and salvage in a system with the quantities we know, plus stockpiles.

    {"sites": [...], "salvage": [...], "totals": {res: {sites, salvage, stock, level}}, "known_at": iso,
     "unknown_sites": n}  — quantities come from cached location details (belt → resource_sites,
    salvage locations) and `salvage.discovered` events; depleted ones are marked.
    """
    star = (star or "").upper()
    depleted: set[str] = set()
    sites: dict[str, dict] = {}
    salvage: dict[str, dict] = {}
    rows = await db.fetchall("SELECT event, payload, created_at FROM events WHERE (star=? OR location LIKE ?) AND event IN "
                             "('site.depleted', 'salvage.depleted', 'salvage.discovered', 'mining.started', 'mining.retargeted') "
                             "ORDER BY seq", (star, f"{star}-%"))
    for r in rows:
        p = json.loads(r["payload"] or "{}")
        if r["event"] in ("site.depleted", "salvage.depleted"):
            depleted.add(p.get("site") or "")
        elif r["event"] == "salvage.discovered":
            code = p.get("designation") or p.get("location")
            if code and in_star(code, star):
                amounts, total = site_quantity(p)
                salvage[code] = {"code": code, "name": p.get("name"), "type": p.get("salvage_type"), "amounts": amounts,
                                 "base": dict(amounts), "total": total, "at": r["created_at"], "source": "discovered"}
        else:
            code = p.get("site")
            if code and in_star(code, star) and code not in sites:
                sites[code] = {"code": code, "belt": code.rsplit("-SITE-", 1)[0], "resource": p.get("resource_type") or p.get("new_resource"),
                               "level": p.get("availability"), "amounts": {}, "total": None, "at": None, "source": "mining"}
    known_at = None
    listed: dict[str, set[str]] = {}   # belt -> sites its latest detail lists (anything else there has closed)
    body_listed: dict[str, set[str]] = {}   # body -> salvage its latest detail lists (anything else there is used up)
    for r in await db.fetchall("SELECT key, value, updated_at FROM kv WHERE key LIKE ?", (f"loc:{star}-%",)):
        loc = r["key"][4:]
        detail = json.loads(r["value"])
        if "resource_sites" in detail and (detail.get("location_type") == "belt" or "-BELT-" in loc and "-SITE-" not in loc):
            listed[loc] = {(_site_code(x) if isinstance(x, dict) else x) for x in detail.get("resource_sites") or []}
        for site in detail.get("resource_sites") or []:
            if isinstance(site, dict) and (site.get("site_type") == "salvage" or "-SAL-" in (_site_code(site) or "")):
                # a body lists its salvage as resource sites: {designation, name, site_type: "salvage",
                # resources_remaining_pct: {res: pct}} — amounts = discovered amounts × remaining %
                code = _site_code(site)
                body_listed.setdefault(loc, set()).add(code)
                prev = salvage.get(code, {})
                pct = {k: float(v) for k, v in (site.get("resources_remaining_pct") or {}).items() if _num(v) is not None}
                base = prev.get("base") or {}
                amounts, total = site_quantity(site)
                if not amounts and pct:
                    amounts = {k: base[k] * v / 100 for k, v in pct.items() if k in base and v > 0}
                    total = sum(amounts.values()) if all(k in base for k, v in pct.items() if v > 0) else None
                salvage[code] = {"code": code, "name": site.get("name") or prev.get("name"),
                                 "type": site.get("salvage_type") or prev.get("type") or "salvage",
                                 "amounts": amounts, "base": base, "total": total, "remaining_pct": pct,
                                 "used_up": bool(pct) and all(v <= 0 for v in pct.values()),
                                 "at": r["updated_at"], "source": "location"}
                known_at = max(known_at or "", r["updated_at"] or "")
                continue
            if not isinstance(site, dict):
                if isinstance(site, str):
                    sites.setdefault(site, {"code": site, "belt": loc, "resource": None, "level": None, "amounts": {},
                                            "total": None, "at": r["updated_at"], "source": "location"})
                continue
            code = _site_code(site) or f"{loc}-SITE-?"
            amounts, total = site_quantity(site)
            pct = {k: float(v) for k, v in (site.get("resources_remaining_pct") or {}).items() if _num(v) is not None}
            sites[code] = {"code": code, "belt": loc, "resource": site.get("resource_type") or site.get("resource"),
                           "level": site.get("availability") or site.get("abundance") or site.get("richness"),
                           "amounts": amounts, "total": total, "at": r["updated_at"], "source": "location",
                           "remaining_pct": pct, "used_up": bool(pct) and all(v <= 0 for v in pct.values())}
            known_at = max(known_at or "", r["updated_at"] or "")
        sal = detail.get("salvage") or (detail if "-SAL-" in loc else None)
        for item in (sal if isinstance(sal, list) else []):   # a body's detail listing its salvage
            if isinstance(item, dict) and (item.get("designation") or item.get("code")):
                code = item.get("designation") or item.get("code")
                amounts, total = site_quantity(item)
                prev = salvage.get(code, {})
                salvage[code] = {"code": code, "name": item.get("name") or prev.get("name"),
                                 "type": item.get("salvage_type") or prev.get("type"),
                                 "amounts": amounts or prev.get("amounts", {}), "total": total if total is not None else prev.get("total"),
                                 "at": r["updated_at"], "source": "location"}
        if isinstance(sal, dict):
            code = sal.get("designation") or loc
            amounts, total = site_quantity(sal)
            prev = salvage.get(code, {})
            salvage[code] = {"code": code, "name": sal.get("name") or prev.get("name"), "type": sal.get("salvage_type") or prev.get("type"),
                             "amounts": amounts or prev.get("amounts", {}), "total": total if total is not None else prev.get("total"),
                             "at": r["updated_at"], "source": "location"}
    for d in list(sites.values()) + list(salvage.values()):
        d["depleted"] = d["code"] in depleted
    totals: dict[str, dict] = defaultdict(lambda: {"sites": 0.0, "salvage": 0.0, "stock": 0.0, "level": None, "site_count": 0})
    for s in sites.values():
        if s["depleted"]:
            continue
        for res, q in (s["amounts"] or ({s["resource"]: 0} if s["resource"] else {})).items():
            totals[res]["sites"] += q
            totals[res]["site_count"] += 1
    for s in salvage.values():
        if not s["depleted"]:
            for res, q in s["amounts"].items():
                totals[res]["salvage"] += q
    for inv in await db.kv_get("inventory", []) or []:
        loc = inv.get("location") or ""
        if loc == star or loc.startswith(star + "-"):
            for res, q in as_amounts(inv.get("items")).items():
                totals[res]["stock"] += q
    row = await db.fetchone("SELECT data FROM systems WHERE star=?", (star,))
    scan = json.loads(row["data"]) if row else {}
    for b in ((scan.get("asteroid_belt") or {}).get("belts")) or []:
        for res, lvl in (b.get("resources") or {}).items():
            if lvl in LEVELS:
                cur = totals[res]["level"]
                if cur is None or LEVELS.index(lvl) > LEVELS.index(cur):
                    totals[res]["level"] = lvl
    order = ["structural", "conductive", "silicates", "carbon", "volatiles", "rares"]
    totals_sorted = dict(sorted(totals.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else 99, kv[0])))
    # Hidden from display: salvage that's used up (it never comes back), and sites that are depleted or closed
    # (a belt's latest detail no longer lists them — e.g. the tracking survey drone left). Belts themselves stay:
    # they never run out; new sites are opened by searching.
    hidden: set[str] = set()
    for x in sites.values():
        b = x.get("belt")
        if x["depleted"] or x.get("used_up") or (b in listed and x["code"] not in listed[b]):
            hidden.add(x["code"])
            x["closed"] = not x["depleted"]
    from .salvage import body_of
    for x in salvage.values():
        x["body"] = body_of(x["code"])
        if x["body"] in body_listed and x["code"] not in body_listed[x["body"]]:
            x["used_up"] = True    # its body no longer lists it
    hidden |= {x["code"] for x in salvage.values() if x["depleted"] or x.get("used_up")
               or (x.get("total") is not None and x["total"] <= 0)}
    return {"star": star, "hidden": hidden,
            "sites_shown": sorted((x for x in sites.values() if x["code"] not in hidden), key=lambda s: s["code"]),
            "salvage_shown": sorted((x for x in salvage.values() if x["code"] not in hidden), key=lambda s: s["code"]),
            "sites": sorted(sites.values(), key=lambda s: (s["depleted"], s["code"])),
            "salvage": sorted(salvage.values(), key=lambda s: (s["depleted"], s["code"])), "totals": totals_sorted,
            "known_at": known_at, "unknown_sites": sum(1 for s in sites.values() if s["total"] is None and not s["depleted"]),
            "mineable": sum(t["sites"] for t in totals.values()), "salvageable": sum(t["salvage"] for t in totals.values())}
