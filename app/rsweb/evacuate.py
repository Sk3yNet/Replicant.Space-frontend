"""Evacuation before an asteroid's impact (game v3.5.0, 2026-10-13).

Devices and resources left at an incoming object's location (its designation, e.g. STAR-OBJ-1) are destroyed on impact
(`device.destroyed`) or flung into space on diversion (`device.lost`); vessels hosting a replicant evacuate to the
planet's orbit by themselves. The game warns an hour ahead (`system.impact_warning`, to accounts with a device there).

Each pass (engine stage evacuate_pass, every minute, and at once on the warning), for every tracked object that isn't
closed and is due within EVAC_MINUTES:
  • devices at its location leave for a safe spot in the system (its entry point, else a body that isn't the impact
    target); propulsors still pushing it stay until DIVERTER_MINUTES (a rule setting, 45) before the ETA, then go too. Once it's diverted
    enough (or off_target, v3.6.0) the propulsors leave at once, the rest (mining rigs) still at the hour
  • a device that can't fly is stowed or attached by a carrier there with a free seat, which then leaves
  • a stockpile there is an evacuation request for logistics (top priority, see logistics.py)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

EVAC_MINUTES = 60       # the game's warning comes an hour ahead
DIVERTER_MINUTES = 45   # propulsors keep pushing until then (asteroid defense rule setting; owner 2026-10-22: 30 lost one)
MOVING = ("travel", "cruis", "surg", "moving", "recall")


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _ts(v: Any) -> float | None:
    from .shapes import game_time
    dt = game_time(v)   # naive = the account's time zone (docs), not the server's
    return dt.timestamp() if dt else None


DIVERTED_PCT = 1.0   # owner, 2026-10-15: the likelihood never reaches 0 % (e.g. 0.70 % at 99.30 % progress)


def diverted_enough(o: dict, pct: float = DIVERTED_PCT) -> bool:
    """Pushed far enough: the impact likelihood is at or below `pct` % (a setting of the asteroid defense rule), or the
    diversion progress at 100 %. More thrust is wasted, and whatever is still there when it swings past is flung off
    and lost — so everything leaves now."""
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    lk, pg = num(o.get("impact_likelihood")), num(o.get("progress_pct"))
    if o.get("status") == "off_target":   # v3.6.0: the game says it will miss; flung out of the system at the ETA
        return True
    return (lk is not None and lk <= pct) or (pg is not None and pg >= 100)


def due(objects: dict[str, dict], now: float | None = None, minutes: float = EVAC_MINUTES,
        warned: set[str] | frozenset = frozenset(), pct: float = DIVERTED_PCT) -> dict[str, float]:
    """{object location: minutes to impact} for open objects due within `minutes` (or warned about by the game), and
    those diverted enough already (owner, 2026-10-15: the propulsors are recalled then, before the ETA)."""
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    out = {}
    for des, o in (objects or {}).items():
        if o.get("closed") or str(o.get("status") or "active") not in ("active", "", "off_target"):
            continue
        eta = _ts(o.get("impact_eta"))
        left = (eta - now) / 60 if eta else None
        if left is not None and left <= 0:
            continue   # the impact time has passed: the event is over (owner, 2026-10-15: no alerts with negative times)
        if des in warned or (left is not None and left <= minutes) or diverted_enough(o, pct):
            for loc in [des] + list(o.get("also_at") or []):   # also_at: where the game warned devices of yours are
                out[loc] = left if left is not None else 0.0
    return out


def at_location(objects: dict[str, dict]) -> dict[str, dict]:
    """{location: the object threatening it}: the object's own designation and the places it warned devices at."""
    out: dict[str, dict] = {}
    for des, o in (objects or {}).items():
        for loc in [des] + list(o.get("also_at") or []):
            out.setdefault(loc, o)
    return out


def safe_spot(star: str, target: str | None, stars: dict[str, dict], scan: dict | None = None,
              here: str | None = None, avoid: set[str] | frozenset = frozenset()) -> str | None:
    """Somewhere in the system away from the object (`here`, where the devices are) and its impact target: the entry
    point, else the first body that is neither, else the Oort cloud. Never `here` itself (live 2026-10-15: devices at
    KELMORNEA-4-L4, the system's entry point, were sent "from KELMORNEA-4-L4 to KELMORNEA-4-L4"), nor another object
    that is due (`avoid`)."""
    bad = {x for x in (target, here) if x} | set(avoid)
    entry = (stars.get(star) or {}).get("entry_point")
    if entry and entry not in bad:
        return entry
    for p in (scan or {}).get("planets") or []:
        loc = p.get("designation") if isinstance(p, dict) else None
        if loc and loc not in bad:
            return loc
    for loc in (f"{star}-OORT", f"{star}-KUIPER"):
        if loc not in bad:
            return loc
    return None


def plan(objects: dict[str, dict], devices: list[dict], stars: dict[str, dict], busy: set[str],
         hosts: set[str] | frozenset = frozenset(), scans: dict[str, dict] | None = None, now: float | None = None,
         warned: set[str] | frozenset = frozenset(), pct: float = DIVERTED_PCT,
         diverter_minutes: float = DIVERTER_MINUTES) -> dict:
    """{"moves": [{code, from, to, why}], "boards": [{carrier, code, mode, to}], "stuck": [{code, location, why}],
    "piles": {location: minutes left}}."""
    from . import fleets as fl
    from .defence import DIVERTING
    hot = due(objects, now, EVAC_MINUTES, warned, pct)
    obj_at = at_location(objects)
    out: dict = {"moves": [], "boards": [], "stuck": [], "piles": hot}
    by_loc: dict[str, list[dict]] = {}
    for d in devices:
        if d.get("location") in hot and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code"):
            by_loc.setdefault(d["location"], []).append(d)
    for loc, here in by_loc.items():
        left = hot[loc]
        star, target = star_of(loc), (obj_at.get(loc) or {}).get("impact_target")
        to = safe_spot(star, target, stars, (scans or {}).get(star), here=loc, avoid=set(hot))
        if not to:
            out["stuck"].extend({"code": d["device_code"], "location": loc, "why": "no safe spot known in the system"} for d in here)
            continue
        seats = {c["device_code"]: dict(fl.seats(c, devices)) for c in here if fl.is_carrier(c)}
        leaving = []
        for d in sorted(here, key=lambda d: fl.is_carrier(d)):   # carriers last: they take the grounded ones along
            code = d["device_code"]
            st = str(d.get("status") or "").lower()
            if code in busy or st.startswith(MOVING) or code in hosts:
                continue   # on its way already, or a replicant's vessel (the game evacuates those to orbit)
            enough = diverted_enough(obj_at.get(loc) or {}, pct)
            if enough and left is not None and left > EVAC_MINUTES and "propulsor" not in (d.get("device_type") or ""):
                continue   # owner, 2026-10-22: only the propulsors leave early; rigs keep mining a diverted rock until the hour
            if "propulsor" in (d.get("device_type") or "") and any(k in st for k in DIVERTING) \
                    and left is not None and left > diverter_minutes and not enough:
                continue   # still pushing it off course
            if "travel" in (d.get("available_commands") or ["travel"]) and "cruise" in (d.get("features") or ["cruise"]):
                why = ("diverted enough — recalled before the swing-past" if enough
                       else f"impact in {left:.0f} min" if left is not None else "impact warning")
                # a pushing propulsor is switched off first: it won't travel while active (owner, 2026-10-18)
                out["moves"].append({"code": code, "from": loc, "to": to, "why": why,
                                     "deactivate": "propulsor" in (d.get("device_type") or "") and any(k in st for k in DIVERTING)})
                leaving.append(code)
                continue
            ride = next(((c, m) for c, s in seats.items() if c != code and (m := fl.take_seat(s, d))), None)
            if ride:
                out["boards"].append({"carrier": ride[0], "code": code, "mode": ride[1], "to": to})
            else:
                out["stuck"].append({"code": code, "location": loc, "why": "it can't fly and no carrier there has a free seat"})
        for b in out["boards"]:   # a carrier taking passengers leaves with them
            if b["carrier"] not in leaving and b["carrier"] not in hosts and b["carrier"] not in busy:
                out["moves"].append({"code": b["carrier"], "from": loc, "to": to, "why": "carrying evacuees"})
                leaving.append(b["carrier"])
    return out
