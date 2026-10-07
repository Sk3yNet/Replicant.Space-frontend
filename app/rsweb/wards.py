"""Other players' system wards.

A system ward stops other players mining in its system. The star catalogue (`GET /stars`) and a stellar census both flag
a warded system with `has_ward`, which is also true for a ward of ours; so a system is warded by someone else when it's
flagged and none of our wards is deployed there. Used to:
  • refuse a mining mission to such a system (and stall one that finds the target warded on the way),
  • leave a stationed fleet's mining controllers and drones out of its loadout while its home is warded,
  • keep the in-system mining rules out of it,
  • mark it in the mining prospects.
"""
from __future__ import annotations

from typing import Any, Iterable

MINING_TYPES = ("ami_mining_controller", "mining_drone")   # what a ward makes useless


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def ours(devices: Iterable[dict]) -> set[str]:
    """Systems with one of our wards deployed (not stowed)."""
    return {star_of(d.get("location")) for d in devices
            if d.get("device_type") == "system_ward" and d.get("location") and not str(d.get("status") or "").startswith("stowed")}


def foreign(stars: Any, devices: Iterable[dict]) -> set[str]:
    """Systems another player has warded: flagged has_ward in the catalogue / census, and no ward of ours there.
    `stars`: the catalogue ({"stars": [...]}), a list of star records, or {designation: record}."""
    if isinstance(stars, dict) and "stars" in stars:
        recs = stars.get("stars") or []
    elif isinstance(stars, dict):
        recs = [dict(v or {}, designation=v.get("designation") or k) for k, v in stars.items() if isinstance(v, dict)]
    else:
        recs = list(stars or [])
    flagged = {s.get("designation") for s in recs if isinstance(s, dict) and s.get("has_ward") and s.get("designation")}
    return flagged - ours(devices)
