"""Other players' system wards (and hubs).

A system ward stops other players mining in its system, and so does a system hub (the player confirmed, 2026-10-07).
The star catalog (`GET /stars`) and a stellar census flag them with `has_ward` / `has_hub` (present only when true),
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


REFUSED_KV = "ward_refused"   # {star: first refusal}: systems where the game wouldn't switch a ward on


def _records(stars: Any) -> list:
    if isinstance(stars, dict) and "stars" in stars:
        return stars.get("stars") or []
    if isinstance(stars, dict):
        return [dict(v or {}, designation=v.get("designation") or k) for k, v in stars.items() if isinstance(v, dict)]
    return list(stars or [])


def no_ward(stars: Any, devices: Iterable[dict], stations: Iterable[str] = (), refused: Iterable[str] = ()) -> set[str]:
    """Systems where the game refuses to switch a ward on ("Cannot activate a ward in a system with a hub"): a hub of
    ours deployed there, one the catalog / census flags `has_hub`, a space station (they count as hubs: live 2026-11-19,
    ward 26959E31 beside New Havana Station in ALKALAROPS), or a system the game refused before (kv ward_refused)."""
    out = {star_of(d.get("location")) for d in devices if d.get("device_type") == "system_hub" and d.get("location")
           and not d.get("stowed_in_device_code") and not str(d.get("status") or "").startswith(("stowed", "compact"))}
    out |= {s.get("designation") for s in _records(stars) if isinstance(s, dict) and s.get("has_hub") and s.get("designation")}
    return (out | set(stations) | set(refused)) - {""}


def foreign(stars: Any, devices: Iterable[dict]) -> set[str]:
    """Systems another player has warded: flagged has_ward or has_hub in the catalog / census, and no ward or hub
    of ours there.
    `stars`: the catalog ({"stars": [...]}), a list of star records, or {designation: record}.
    Wards and hubs also carry a species interaction lock: other players can't complete location events (contracts)
    there (game docs / the player, 2026-10-07)."""
    recs = _records(stars)
    flagged = {s.get("designation") for s in recs
               if isinstance(s, dict) and (s.get("has_ward") or s.get("has_hub")) and s.get("designation")}
    devices = list(devices)
    # the flag doesn't say whose ward it is (seen live 2026-10-07: every starter_155 system has_ward). A system where
    # one of our drones is mining can't be warded against us, so it doesn't count.
    mining_here = {star_of(d.get("location")) for d in devices if str(d.get("status") or "").startswith("mining")}
    return flagged - ours(devices) - mining_here
