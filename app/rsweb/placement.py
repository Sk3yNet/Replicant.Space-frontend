"""Where some device types have to sit inside their system to be any use (confirmed by Joe, 2026-10-05):

  ftl_relay              — an L4/L5 Lagrange point (`STAR-N-L4` / `-L5`): it needs a gravitationally stable spot and
                           can only be activated there (game docs, confirmed by Joe).
  ami_mining_controller  — the system's asteroid belt.
  ami_survey_controller  — the asteroid belt too; with no belt in the system, the inner system (a planet, its moons or
                           its Lagrange points — never the Kuiper belt, Oort cloud or an object).

`geography()` gathers what is known about a system (its scan, the catalog's entry point, where devices are);
`target()` picks the spot for a device type and `ok()` says whether a location already satisfies the rule.
"""
from __future__ import annotations

import re
from typing import Any

RULES = {"ftl_relay": "lagrange", "ami_mining_controller": "belt", "ami_survey_controller": "belt_or_inner"}
WHY = {"lagrange": "a relay only works at an L4/L5 Lagrange point", "belt": "a mining controller belongs at the asteroid belt",
       "belt_or_inner": "a survey controller belongs at the asteroid belt (or the inner system when there's none)"}

_LAGRANGE = re.compile(r"^[A-Z0-9]+-\d+-L[45]$")
_BELT = re.compile(r"^([A-Z0-9]+-BELT-\d+)")
_PLANETARY = re.compile(r"^[A-Z0-9]+-\d+(-|$)")       # STAR-3, STAR-3-2 (moon), STAR-3-L4 …


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def is_lagrange(loc: str | None) -> bool:
    return bool(_LAGRANGE.match((loc or "").upper()))


def belt_of(loc: str | None) -> str | None:
    m = _BELT.match((loc or "").upper())
    return m.group(1) if m else None


def geography(star: str, devices: list[dict], scan: dict | None = None, entry_point: str | None = None,
              belts_seen: list[str] | None = None) -> dict[str, Any]:
    """What's known about `star`: belts, Lagrange points (L4/L5 first) and inner-system bodies (innermost first)."""
    scan = scan or {}
    belts = {b.get("designation") for b in ((scan.get("asteroid_belt") or {}).get("belts") or []) if b.get("designation")}
    belts |= {b for b in belts_seen or [] if star_of(b) == star}
    locs = [d.get("location") for d in devices if star_of(d.get("location")) == star and d.get("location")]
    belts |= {b for b in (belt_of(x) for x in locs) if b}
    planets = sorted((p for p in scan.get("planets") or [] if p.get("designation")),
                     key=lambda p: (p.get("orbital_distance_au") if p.get("orbital_distance_au") is not None else 1e9,
                                    p["designation"]))
    lp: list[str] = []
    ep = entry_point or scan.get("entry_point")
    for x in [ep] + sorted(set(locs)) + [f"{p['designation']}-{s}" for p in planets for s in ("L4", "L5")]:
        if x and is_lagrange(x) and x not in lp:
            lp.append(x)
    lp.sort(key=lambda x: x != ep)   # the entry point first
    inner = [p["designation"] for p in planets]
    if not inner:   # no scan: planets we've seen devices at, lowest number first
        nums = sorted({int(m.group(1)) for x in locs for m in [re.match(r"^[A-Z0-9]+-(\d+)", x)] if m})
        inner = [f"{star}-{n}" for n in nums]
    return {"belts": sorted(belts), "lagrange": lp, "inner": inner, "scanned": bool(scan)}


def rule_for(device_type: str | None) -> str | None:
    return RULES.get(device_type or "")


def ok(device_type: str | None, loc: str | None, geo: dict) -> bool:
    rule, loc = rule_for(device_type), (loc or "").upper()
    if not rule or not loc:
        return True
    if rule == "lagrange":
        return is_lagrange(loc)
    if belt_of(loc):
        return True
    if rule == "belt_or_inner" and not geo.get("belts"):
        return bool(_PLANETARY.match(loc))
    return False


def target(device_type: str | None, geo: dict) -> str | None:
    rule = rule_for(device_type)
    if rule == "lagrange":
        return (geo.get("lagrange") or [None])[0]
    if geo.get("belts"):
        return geo["belts"][0]
    if rule == "belt_or_inner":
        return (geo.get("inner") or [None])[0]
    return None
