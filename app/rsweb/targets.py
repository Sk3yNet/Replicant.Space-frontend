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
                             "ORDER BY seq DESC LIMIT 3000", (star, f"{star}%"))
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

    targets = sorted(found.values(), key=lambda t: (CATEGORY_ORDER.index(t["category"]), t["code"]))
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
