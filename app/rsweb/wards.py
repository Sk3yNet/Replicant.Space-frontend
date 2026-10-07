"""Other players' system wards (and hubs).

A system ward stops other players mining in its system, and so does a system hub (the player confirmed, 2026-10-07).
The star catalogue (`GET /stars`) and a stellar census flag them with `has_ward` / `has_hub` (present only when true),
which is also true for one of ours; so a system is warded by someone else when it's flagged and none of our wards or
hubs is deployed there. Used to:
  • refuse a mining mission to such a system (and stall one that finds the target warded on the way),
  • leave a stationed fleet's mining controllers and drones out of its loadout while its home is warded,
  • keep the in-system mining rules out of it,
  • mark it in the mining prospects.
"""
from __future__ import annotations

from typing import Any, Iterable

MINING_TYPES = ("ami_mining_controller", "mining_drone")   # what a ward makes useless
WARDING_TYPES = ("system_ward", "system_hub")               # devices that ward a system


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def ours(devices: Iterable[dict]) -> set[str]:
    """Systems with one of our wards or hubs deployed (not stowed or folded up)."""
    return {star_of(d.get("location")) for d in devices
            if d.get("device_type") in WARDING_TYPES and d.get("location")
            and not str(d.get("status") or "").startswith(("stowed", "compact"))}


def foreign(stars: Any, devices: Iterable[dict]) -> set[str]:
    """Systems another player has warded: flagged has_ward or has_hub in the catalogue / census, and no ward or hub
    of ours there.
    `stars`: the catalogue ({"stars": [...]}), a list of star records, or {designation: record}.
    Wards and hubs also carry a species interaction lock: other players can't complete location events (contracts)
    there (game docs / the player, 2026-10-07)."""
    if isinstance(stars, dict) and "stars" in stars:
        recs = stars.get("stars") or []
    elif isinstance(stars, dict):
        recs = [dict(v or {}, designation=v.get("designation") or k) for k, v in stars.items() if isinstance(v, dict)]
    else:
        recs = list(stars or [])
    flagged = {s.get("designation") for s in recs
               if isinstance(s, dict) and (s.get("has_ward") or s.get("has_hub")) and s.get("designation")}
    devices = list(devices)
    # the flag doesn't say whose ward it is (seen live 2026-10-07: every starter_155 system has_ward). A system where
    # one of our drones is mining can't be warded against us, so it doesn't count.
    mining_here = {star_of(d.get("location")) for d in devices if str(d.get("status") or "").startswith("mining")}
    return flagged - ours(devices) - mining_here
