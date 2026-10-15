"""Evacuation before an asteroid's impact (game v3.5.0, 2026-10-13).

Devices and resources left at an incoming object's location (its designation, e.g. STAR-OBJ-1) are destroyed on impact
(`device.destroyed`) or flung into space on diversion (`device.lost`); vessels hosting a replicant evacuate to the
planet's orbit by themselves. The game warns an hour ahead (`system.impact_warning`, to accounts with a device there).

Each pass (engine stage evacuate_pass, every minute, and at once on the warning), for every tracked object that isn't
closed and is due within EVAC_MINUTES:
  • devices at its location leave for a safe spot in the system (its entry point, else a body that isn't the impact
    target); propulsors still pushing it stay until DIVERTER_MINUTES before the ETA, then go too
  • a device that can't fly is stowed or attached by a carrier there with a free seat, which then leaves
  • a stockpile there is an evacuation request for logistics (top priority, see logistics.py)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

EVAC_MINUTES = 60       # the game's warning comes an hour ahead
DIVERTER_MINUTES = 30   # propulsors keep pushing until then
MOVING = ("travel", "cruis", "surg", "moving", "recall")


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _ts(v: Any) -> float | None:
    from .shapes import game_time
    dt = game_time(v)   # naive = the account's time zone (docs), not the server's
    return dt.timestamp() if dt else None


def diverted_enough(o: dict) -> bool:
    """Pushed far enough: the game puts the impact likelihood at 0, or the diversion progress at 100 %. More thrust is
    wasted, and whatever is still there when it swings past is flung off and lost — so everything leaves now."""
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    lk, pg = num(o.get("impact_likelihood")), num(o.get("progress_pct"))
    return (lk is not None and lk <= 0) or (pg is not None and pg >= 100)


def due(objects: dict[str, dict], now: float | None = None, minutes: float = EVAC_MINUTES,
        warned: set[str] | frozenset = frozenset()) -> dict[str, float]:
    """{object location: minutes to impact} for open objects due within `minutes` (or warned about by the game), and
    those diverted enough already (owner, 2026-10-15: the propulsors are recalled then, before the ETA)."""
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    out = {}
    for des, o in (objects or {}).items():
        if o.get("closed") or str(o.get("status") or "active") not in ("active", ""):
            continue
        eta = _ts(o.get("impact_eta"))
        left = (eta - now) / 60 if eta else None
        if des in warned or (left is not None and left <= minutes) or diverted_enough(o):
            out[des] = left if left is not None else 0.0
    return out


def safe_spot(star: str, target: str | None, stars: dict[str, dict], scan: dict | None = None) -> str | None:
    """Somewhere in the system away from the object and its impact target: the entry point, else the first body that
    isn't the target."""
    entry = (stars.get(star) or {}).get("entry_point")
    if entry and entry != target:
        return entry
    for p in (scan or {}).get("planets") or []:
        loc = p.get("designation") if isinstance(p, dict) else None
        if loc and loc != target:
            return loc
    return f"{star}-OORT" if target != f"{star}-OORT" else None


def plan(objects: dict[str, dict], devices: list[dict], stars: dict[str, dict], busy: set[str],
         hosts: set[str] | frozenset = frozenset(), scans: dict[str, dict] | None = None, now: float | None = None,
         warned: set[str] | frozenset = frozenset()) -> dict:
    """{"moves": [{code, from, to, why}], "boards": [{carrier, code, mode, to}], "stuck": [{code, location, why}],
    "piles": {location: minutes left}}."""
    from . import fleets as fl
    from .defence import DIVERTING
    hot = due(objects, now, EVAC_MINUTES, warned)
    out: dict = {"moves": [], "boards": [], "stuck": [], "piles": hot}
    by_loc: dict[str, list[dict]] = {}
    for d in devices:
        if d.get("location") in hot and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code"):
            by_loc.setdefault(d["location"], []).append(d)
    for loc, here in by_loc.items():
        left = hot[loc]
        star, target = star_of(loc), (objects.get(loc) or {}).get("impact_target")
        to = safe_spot(star, target, stars, (scans or {}).get(star))
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
            enough = diverted_enough(objects.get(loc) or {})
            if "propulsor" in (d.get("device_type") or "") and any(k in st for k in DIVERTING) \
                    and left is not None and left > DIVERTER_MINUTES and not enough:
                continue   # still pushing it off course
            if "travel" in (d.get("available_commands") or ["travel"]) and "cruise" in (d.get("features") or ["cruise"]):
                why = ("diverted enough — recalled before the swing-past" if enough
                       else f"impact in {left:.0f} min" if left is not None else "impact warning")
                out["moves"].append({"code": code, "from": loc, "to": to, "why": why})
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
