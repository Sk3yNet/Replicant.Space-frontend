"""Devices in transit, for the maps: where along their route they are, and which way they're heading.

A moving device's `travel` (live shape, 2026-10-06):
    {"origin": "OTHDANAX-2", "destination": "OTHDANAX-OORT", "final_destination": …, "departed_at": …, "arrives_at": …,
     "final_arrives_at": …, "progress_percent": 83.2, "type": "cruise", "stage": "decelerating",
     "route": [{"leg": 1, "from": "OTHDANAX-2", "to": "OTHDANAX-OORT", "type": "cruise", "time_seconds": 8483.6, …}]}
Legs are cruise (inside a system) or surge / surge_hop (between stars). Each leg gets absolute start/end times (spread
over departed_at → final_arrives_at by its share of the route time), so the maps can place the device at any moment.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _ts(v: Any) -> float | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()   # no offset: UTC, like everywhere else


def trip(d: dict, now: float | None = None) -> dict | None:
    """The device's current trip with timed legs, or None when it isn't traveling (or the trip is long over)."""
    tr = d.get("travel") or {}
    t0 = _ts(tr.get("departed_at"))
    t1 = _ts(tr.get("final_arrives_at") or tr.get("arrives_at"))
    if not tr or t0 is None or t1 is None or t1 <= t0:
        return None
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    if t1 < now - 120:
        return None   # arrived a while ago; the device list just hasn't caught up
    legs = [x for x in tr.get("route") or [] if isinstance(x, dict) and x.get("from") and x.get("to")]
    if not legs:
        legs = [{"from": tr.get("origin"), "to": tr.get("final_destination") or tr.get("destination"),
                 "type": tr.get("type") or "cruise", "time_seconds": t1 - t0}]
    # each leg's share of the trip: its own time, or (without one) the average of the timed legs — all equal when
    # no leg has a time — scaled so the legs fill departure → arrival
    times = [float(x.get("time_seconds") or 0) for x in legs]
    timed = [s for s in times if s > 0]
    guess = sum(timed) / len(timed) if timed else 1.0
    weights = [s if s > 0 else guess for s in times]
    scale, at, out = (t1 - t0) / sum(weights), t0, []
    for x, w in zip(legs, weights):
        dt = w * scale
        out.append({"from": x["from"], "to": x["to"], "type": x.get("type") or "cruise", "t0": at, "t1": at + dt})
        at += dt
    return {"code": d.get("device_code"), "device_type": d.get("device_type"), "status": d.get("status"),
            "origin": tr.get("origin") or legs[0]["from"], "destination": tr.get("final_destination") or tr.get("destination"),
            "t0": t0, "t1": t1, "progress": max(0.0, min(1.0, (now - t0) / (t1 - t0))), "eta": max(0, int(t1 - now)),
            "legs": out}


def trips(devices: list[dict], now: float | None = None) -> list[dict]:
    """Every device's trip, devices traveling together (same route, arriving within a minute) as one."""
    groups: dict[tuple, dict] = {}
    for d in devices:
        t = trip(d, now)
        if not t:
            continue
        key = (t["origin"], t["destination"], tuple((x["from"], x["to"]) for x in t["legs"]), round(t["t1"] / 60))
        g = groups.get(key)
        if g:
            g["codes"].append(t["code"])
            g["types"].append(t["device_type"])
        else:
            groups[key] = {**t, "codes": [t["code"]], "types": [t["device_type"]]}
    out = list(groups.values())
    for g in out:
        g["label"] = describe(g)
    return sorted(out, key=lambda g: (g["t1"], g["codes"][0]))


def describe(g: dict) -> str:
    """'A42C5AB6 survey drone', or '3× survey drone' (or '3 devices' when they differ)."""
    n = len(g["codes"])
    kinds = sorted(set(t or "device" for t in g["types"]))
    if n == 1:
        return f"{g['codes'][0]} {kinds[0].replace('_', ' ')}"
    return f"{n}× {kinds[0].replace('_', ' ')}" if len(kinds) == 1 else f"{n} devices"


def galaxy_movers(groups: list[dict], positions: dict[str, dict]) -> list[dict]:
    """Trips between stars, for the galaxy map: their legs as segments between star positions with times (ms).
    A cruise leg keeps the device at its star; legs to or from a star we have no position for are left out."""
    out = []
    for g in groups:
        if star_of(g["origin"]) == star_of(g["destination"]):
            continue
        segs = []
        for x in g["legs"]:
            a, b = positions.get(star_of(x["from"])), positions.get(star_of(x["to"]))
            if not a or not b:
                continue
            segs.append({"a": a, "b": b, "t0": int(x["t0"] * 1000), "t1": int(x["t1"] * 1000)})
        if not segs:
            continue
        out.append({"label": g["label"], "codes": g["codes"], "origin": star_of(g["origin"]),
                    "destination": g["destination"], "t0": int(g["t0"] * 1000), "t1": int(g["t1"] * 1000), "segs": segs})
    return out
