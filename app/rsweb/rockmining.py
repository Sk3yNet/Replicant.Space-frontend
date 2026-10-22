"""Asteroid mining (game v3.6.0, 2026-10-21): incoming asteroids carry a resource site at their location (STAR-OBJ-n).

From the game docs (Asteroid Mining, Asteroids location page):
  • only devices with the `fracture` feature (mining rigs) can mine them; mining drones can't
  • mining slots by mass class: small 1, medium 2, large 5 — shared by every player, first come first served
  • GET /locations/<object> lists `resource_sites [{designation, resources_remaining_pct: {res: pct}}]`; a survey
    drone's scan of the object gives exact amounts (scan.completed)
  • yields taper: the last 20 % takes much longer than the first 20 % — so the rigs work one resource at a time, the
    owner's priority first (owner 2026-10-22: the first resource mined is the one most likely to be finished)
  • a fully mined rock has no terraform effect on impact: rocks you launched (Terraforming) keep `keep_pct` of every
    resource, and only the resources chosen for them are mined
  • everything left at the location is destroyed on impact or lost when it's flung out (off_target): evacuate.py pulls
    the rigs out an hour before the ETA, and the stockpile mined there is hauled out by logistics while they work

Each pass (engine stage rock_mining_pass): rigs at a minable rock start mining (or retarget) the top resource left;
idle rigs that can fly elsewhere in the system go to a rock with a free slot and enough time left; an idle survey drone
in the system goes to a rock being mined and stays, scanning it every `scan_minutes` until the cutoff.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

RESOURCES = ["structural", "conductive", "silicates", "carbon", "volatiles", "rares"]
SLOTS = {"small": 1, "medium": 2, "large": 5}
OPEN = ("active", "off_target", "")
CUTOFF_H = 1.0   # evacuate.EVAC_MINUTES: everything but the propulsors leaves an hour before the ETA


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def is_rig(d: dict) -> bool:
    return "fracture" in (d.get("features") or [])


def parse_list(text: Any) -> list[str]:
    """'rares, conductive volatiles' → ['rares', 'conductive', 'volatiles'] (unknown words dropped, no repeats)."""
    out: list[str] = []
    for w in re.split(r"[\s,;/]+", str(text or "").lower()):
        if w in RESOURCES and w not in out:
            out.append(w)
    return out


def order_for(o: dict, cfg: dict) -> tuple[list[str], float]:
    """(resources in the order to mine them, % to leave of each) for one rock. A rock you launched: only its chosen
    resources (else the priority list), never below keep_pct. Any other rock: the priority list, then the rest, to dust."""
    prio = parse_list(cfg.get("priority"))
    if o.get("launched_by_you"):
        return (parse_list(cfg.get("launched_resources")) or prio or list(RESOURCES)), float(cfg.get("keep_pct") or 0)
    return prio + [r for r in RESOURCES if r not in prio], 0.0


def resources_left(o: dict) -> dict[str, float]:
    from .defence import resources_left as rl
    return rl(o)


def pick(o: dict, cfg: dict) -> str | None:
    """The resource the rigs on this rock should work: the first in order that still has more than the floor."""
    left = resources_left(o)
    order, floor = order_for(o, cfg)
    return next((r for r in order if r in left and left[r] > floor), None)


def _ts(v: Any) -> float | None:
    from .shapes import game_time
    dt = game_time(v)
    return dt.timestamp() if dt else None


def hours_left(o: dict, now: float) -> float | None:
    eta = _ts(o.get("impact_eta"))
    return (eta - now) / 3600 if eta else None


def open_rocks(objs: dict[str, dict], now: float | None = None) -> dict[str, dict]:
    """{rock: object} still in flight: not closed, before its ETA."""
    from .defence import is_object
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    out = {}
    for des, o in (objs or {}).items():
        h = hours_left(o, now)
        if not o.get("closed") and str(o.get("status") or "") in OPEN and is_object(des) and h is not None and h > 0:
            out[des] = o
    return out


def minable(objs: dict[str, dict], cfg: dict, now: float | None = None) -> dict[str, dict]:
    """{rock: object} worth sending rigs to: open, a resource above its floor, and `min_hours` of mining left before the
    rigs leave again (an hour before the ETA), not counting the trip (trip_ok checks that with the game's preview)."""
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    out = {}
    for des, o in open_rocks(objs, now).items():
        if (hours_left(o, now) or 0) <= CUTOFF_H + float(cfg.get("min_hours") or 1.0):
            continue
        if pick(o, cfg):
            out[des] = o
    return out


def trip_ok(o: dict, travel_s: float | None, cfg: dict, now: float | None = None) -> bool:
    """Owner 2026-10-22: a trip is worth it only if, after the travel time (the game's preview), `min_hours` of mining
    remain before the one-hour cutoff. No preview: the hours-left filter alone."""
    if travel_s is None:
        return True
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    return (hours_left(o, now) or 0) - travel_s / 3600 - CUTOFF_H >= float(cfg.get("min_hours") or 1.0)


def can_fly(d: dict) -> bool:
    return "travel" in (d.get("available_commands") or ["travel"]) and "cruise" in (d.get("features") or [])


def plan(objs: dict[str, dict], devices: list[dict], cfg: dict, busy: set[str], state: dict[str, dict],
         hosts: set[str] | frozenset = frozenset(), full: set[str] | frozenset = frozenset(),
         scanned: dict[str, str] | None = None, now: float | None = None) -> dict:
    """{"start": [{code, rock, resource}], "retarget": [...], "stop": [...], "send": [{code, rock, resource}], "scan": [{code, rock,
    back}], "notes": [str]}. `state`: {rig: {rock, resource}} as last commanded; `full`: rocks whose slots the game
    said were taken (other players' rigs count too); `scanned`: {rock: last scan time}."""
    from .ami_schedule import reserved
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    rocks, flying = minable(objs, cfg, now), open_rocks(objs, now)
    out: dict = {"start": [], "retarget": [], "stop": [], "send": [], "scan": [], "notes": []}
    free = lambda d: d["device_code"] not in busy and d["device_code"] not in hosts and not reserved(d)   # noqa: E731
    rigs = [d for d in devices if is_rig(d) and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code")]
    there: dict[str, int] = {}
    for d in rigs:
        if d.get("location") in rocks:
            there[d["location"]] = there.get(d["location"], 0) + 1
    for c, s in state.items():   # on their way
        if s.get("rock") in rocks and s.get("sent") and not any(d["device_code"] == c and d.get("location") == s["rock"] for d in rigs):
            there[s["rock"]] = there.get(s["rock"], 0) + 1
    for d in rigs:
        code, loc, st = d["device_code"], d.get("location"), str(d.get("status") or "").lower()
        if loc not in flying or not free(d):
            continue
        want = pick(flying[loc], cfg)
        if not want:   # mined down to the floor (a rock you launched keeps its terraform effect) or dry
            if st.startswith("mining"):
                out["stop"].append({"code": code, "rock": loc, "resource": None})
        elif st.startswith("idle"):
            out["start"].append({"code": code, "rock": loc, "resource": want})
        elif st.startswith("mining") and (state.get(code) or {}).get("resource") not in (None, want):
            out["retarget"].append({"code": code, "rock": loc, "resource": want})
    if cfg.get("send_idle", True):
        for d in sorted(rigs, key=lambda d: d["device_code"]):
            code, loc = d["device_code"], d.get("location")
            if loc in rocks or not free(d) or not str(d.get("status") or "").lower().startswith("idle"):
                continue
            to = next((r for r, o in sorted(rocks.items(), key=lambda x: hours_left(x[1], now) or 0, reverse=True)
                       if star_of(r) == star_of(loc) and r not in full
                       and there.get(r, 0) < SLOTS.get(str(o.get("mass_class") or o.get("size_class") or "").lower(), 1)), None)
            if not to:
                continue
            if not can_fly(d):
                out["notes"].append(f"{code} can't fly to {to} (no cruise): carry it there by hand")
                continue
            there[to] = there.get(to, 0) + 1
            out["send"].append({"code": code, "rock": to, "resource": pick(rocks[to], cfg)})
    if cfg.get("scan_rocks", True):
        worked = {s["rock"] for s in out["start"] + out["send"]} | {s.get("rock") for s in state.values()} | set(there)
        every = float(cfg.get("scan_minutes") or 30) * 60
        taken: set[str] = set()
        idle = lambda d: str(d.get("status") or "").lower().startswith("idle") and free(d)   # noqa: E731
        surveyors = [d for d in devices if d.get("device_type") == "survey_drone"
                     and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code")]
        for r in sorted(x for x in worked if x in flying):
            here = [d for d in surveyors if d.get("location") == r]
            if here:   # owner 2026-10-22: a drone stays at the rock and scans every scan_minutes (it leaves at the cutoff)
                last = _ts((scanned or {}).get(r))
                d = next((d for d in here if idle(d)), None)
                if d and not (last and now - last < every):
                    out["scan"].append({"code": d["device_code"], "rock": r, "here": True})
                continue
            if r not in rocks:
                continue
            # owner 2026-10-22: an idle drone, else one searching a belt for a new site; never one tracking a site
            spare = [d for d in surveyors if star_of(d.get("location")) == star_of(r) and can_fly(d) and free(d)
                     and d["device_code"] not in taken and not d.get("tracking_site_id")
                     and (idle(d) or str(d.get("status") or "").lower().startswith("searching"))]
            drone = min(spare, key=lambda d: (not idle(d), d["device_code"]), default=None)
            if drone:
                taken.add(drone["device_code"])
                out["scan"].append({"code": drone["device_code"], "rock": r, "here": False, "stop": not idle(drone)})
    return out


def amounts(report: Any) -> dict[str, float]:
    """Exact amounts from a survey scan of a rock (scan.completed `report`): every number under a resource's name."""
    out: dict[str, float] = {}

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if k in RESOURCES and isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[k] = out.get(k, 0.0) + float(v)
                elif k in RESOURCES and isinstance(v, dict):
                    q = v.get("quantity", v.get("amount", v.get("remaining")))
                    if isinstance(q, (int, float)):
                        out[k] = out.get(k, 0.0) + float(q)
                elif "pct" not in k and "percent" not in k:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                if isinstance(v, dict) and v.get("resource_type") in RESOURCES:
                    q = v.get("quantity", v.get("amount", v.get("remaining")))
                    if isinstance(q, (int, float)):
                        out[v["resource_type"]] = out.get(v["resource_type"], 0.0) + float(q)
                        continue
                walk(v)
    walk(report)
    return out
