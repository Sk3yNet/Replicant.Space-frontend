"""A stationed mining fleet's path forward: a heading it keeps, its observatory prospecting along it, and choosing its
own next home when the current one runs dry.

Per fleet (Fleets page, stationed mining fleets):
    heading        {"vector": [x, y, z] (unit), "label": "..."} — fixed when set ("outward from Sol through FALQUORYX",
                   "toward KELMORNEA", or typed), so the fleet keeps going the same way after every move
    cone           degrees either side of the heading that still count as "ahead" (default 60)
    auto_prospect  its galactic observatory prospects along the heading when it's idle and few systems lie ahead
    auto_relocate  when its home runs dry, it picks the best-ranked system ahead (Systems › Mining prospects) and,
                   after a short grace period (cancel or go now on the card), makes that its home — the loadout pass
                   then moves the fleet there, as when the home is changed by hand
    next_home, next_home_at, depleted_since, path_note — state

A system is never chosen when it is: unscanned (no score), outside our relay coverage ("unserved": no relay of ours
within 7.5 ly, no hub within 15 ly), another fleet's home, another mining fleet's planned next home or mission target,
warded by another player, or the fleet's own home.
"""
from __future__ import annotations

import math
from typing import Any

RELAY_LY, HUB_LY = 7.5, 15.0
DEFAULT_CONE = 60
DRY_FOR_HOURS = 2.0           # dry this long before choosing (sites come and go)
GRACE_MINUTES = 30            # planned → moving: time to cancel
PROSPECT_EVERY_HOURS = 4.0
AHEAD_WANTED = 3              # prospect while fewer candidate systems than this lie ahead


def unit(v: Any) -> list[float] | None:
    try:
        x = [float(a) for a in v]
    except (TypeError, ValueError):
        return None
    n = math.sqrt(sum(a * a for a in x))
    return [round(a / n, 4) for a in x] if len(x) == 3 and n else None


def xyz(p: Any) -> tuple[float, float, float] | None:
    if not isinstance(p, dict):
        return None
    try:
        return (float(p.get("x") or 0), float(p.get("y") or 0), float(p.get("z") or 0))
    except (TypeError, ValueError):
        return None


def angle(origin: tuple | None, star: tuple | None, heading: list[float] | None) -> float | None:
    """Degrees between the heading and the direction origin → star (None when unknown)."""
    if not origin or not star or not heading:
        return None
    d = [b - a for a, b in zip(origin, star)]
    n = math.sqrt(sum(a * a for a in d))
    if not n:
        return None
    c = sum(a * b for a, b in zip(d, heading)) / n
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def _off(origin, star, heading) -> float:
    a = angle(origin, star, heading)
    return 180.0 if a is None else a   # unknown position: not counted as ahead


def heading_from(kind: str, home: tuple | None, target: tuple | None = None, custom: Any = None) -> list[float]:
    """The heading vector for a preset. Raises ValueError when it can't be worked out."""
    if kind == "outward":
        if not home or not any(home):
            raise ValueError("outward from Sol needs the home system's position, away from Sol")
        return unit(home)
    if kind == "star":
        if not home or not target:
            raise ValueError("both systems need positions in the star catalog")
        v = unit([b - a for a, b in zip(home, target)])
        if not v:
            raise ValueError("that's the fleet's own system")
        return v
    if kind == "custom":
        v = unit(custom)
        if not v:
            raise ValueError("type three numbers, e.g. 0.6, -0.2, 0.77")
        return v
    raise ValueError(f"unknown heading {kind!r}")


def served(star: str, xyz_of: dict[str, tuple], devices: list[dict]) -> bool:
    """Inside our relay coverage: a deployed relay of ours within 7.5 ly, or a hub within 15 ly."""
    p = xyz_of.get(star)
    for d in devices:
        t = d.get("device_type") or ""
        rng = HUB_LY if t == "system_hub" else RELAY_LY if "relay" in t else None
        if rng is None or not d.get("location") or d.get("stowed_in_device_code") \
                or str(d.get("status") or "").startswith(("stowed", "compact", "travel", "cruis", "surg")):
            continue
        s = d["location"].split("-")[0]
        if s == star:
            return True
        q = xyz_of.get(s)
        if p and q and math.dist(p, q) <= rng + 1e-9:
            return True
    return False


def taken(fleet: dict, fleets: list[dict]) -> dict[str, str]:
    """Systems other fleets hold: their homes, mining fleets' planned next homes and mining mission targets."""
    out: dict[str, str] = {}
    for f in fleets:
        if f.get("id") == fleet.get("id"):
            continue
        name = f.get("name") or f.get("id")
        if f.get("home"):
            out.setdefault(f["home"], f"home of {name}")
        if f.get("role") == "mining":
            if f.get("next_home"):
                out.setdefault(f["next_home"], f"{name}'s next home")
            m = f.get("mission") or {}
            if isinstance(m, dict) and m.get("status") in ("running", "stalled", "stopped"):
                for t in m.get("targets") or []:
                    out.setdefault(str(t).split("-")[0], f"{name}'s mission target")
    return out


def dry(home: str, devices: list[dict], viability: list[dict], nothing: dict, warded: set[str],
        belts: list[str]) -> str | None:
    """Why the home has run dry, or None: another player's ward came up; no belt and no salvage left; or every belt
    there is 'consider moving' (searches take far longer than a site lasts)."""
    if home in warded:
        return "another player's ward or hub is in it now"
    if home in (nothing or {}):
        return "no asteroid belt and no salvage left"
    verdicts = [v.get("verdict") for v in viability if v.get("star") == home]
    if belts and verdicts and len(verdicts) >= len(belts) and all(v == "consider moving" for v in verdicts):
        return "every belt is 'consider moving' (searches take far longer than a site lasts)"
    return None


def choose(fleet: dict, fleets: list[dict], rows: list[dict], xyz_of: dict[str, tuple], devices: list[dict],
           heading: list[float] | None, cone: float) -> tuple[str | None, list[str]]:
    """The best next home for the fleet: (star, why) or (None, reasons nothing qualified). `rows` are prospects.rank()
    rows from the fleet's home (best first)."""
    home = fleet.get("home") or ""
    hold = taken(fleet, fleets)
    skipped: dict[str, int] = {}
    best = None
    for r in rows:
        star = r.get("star")
        why = ("its own home" if star == home else
               "unscanned" if r.get("status") == "unscanned" or r.get("score") is None else
               "warded" if r.get("status") == "warded" else
               "held by another fleet" if star in hold else
               "behind the heading" if heading and _off(xyz_of.get(home), xyz_of.get(star), heading) > cone else
               "outside relay coverage" if not served(star, xyz_of, devices) else None)
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        best = r if best is None or (r["score"] or 0) > (best["score"] or 0) else best
    if best:
        a = angle(xyz_of.get(home), xyz_of.get(best["star"]), heading)
        note = [f"score {best['score']}"] + (best.get("reasons") or [])[:2] + \
            ([f"{a:.0f}° off the heading"] if a is not None else []) + \
            ([f"{best['distance']} ly"] if best.get("distance") is not None else [])
        return best["star"], note
    return None, [f"{n} {k}" for k, n in sorted(skipped.items())] or ["no scored systems"]


def ahead(home: str, xyz_of: dict[str, tuple], heading: list[float] | None, cone: float, stars: set[str],
          max_ly: float = 30.0) -> list[str]:
    """Stars among `stars` ahead of the home along the heading (inside the cone, within max_ly), nearest first."""
    o = xyz_of.get(home)
    if not o or not heading:
        return []
    out = []
    for s in stars:
        p = xyz_of.get(s)
        if not p or s == home:
            continue
        a = angle(o, p, heading)
        d = math.dist(o, p)
        if a is not None and a <= cone and d <= max_ly:
            out.append((d, s))
    return [s for _, s in sorted(out)]


def advance_target(home: str, xyz_of: dict[str, tuple], heading: list[float], cone: float, hop_ly: float,
                   skip: set[str], ok=lambda s: True) -> str | None:
    """An advancing explore fleet's next home: the farthest star within hop_ly inside the cone around the heading,
    not in `skip` (other fleets' homes, systems already advanced from) and passing `ok` (e.g. relay coverage)."""
    o = xyz_of.get(home)
    if not o or not heading:
        return None
    best = None
    for s, p in xyz_of.items():
        if s == home or s in skip or not p:
            continue
        d = math.dist(o, p)
        a = angle(o, p, heading)
        if a is None or a > cone or d > hop_ly or not ok(s):
            continue
        if best is None or d > best[0]:
            best = (d, s)
    return best[1] if best else None
