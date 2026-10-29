"""Where your FTL relays, beacons and wards are, for the Systems list (and the relay check before a mission).

Survey crews no longer drop relays or beacons (removed 2026-10-11): ask for one on the Systems page instead
(ward_orders.py). System wards still travel with their fleet as members (fleets.unload_steps, modular.with_untracking).
"""
from __future__ import annotations

KINDS = {"relay": "ftl_relay", "station": "deep_space_relay_station", "beacon": "ftl_beacon",
         "ward": "system_ward"}   # what the Systems list shows
LABELS = {"relay": "FTL relay", "station": "deep space relay station", "beacon": "FTL beacon", "ward": "system ward"}


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def deployed_in(devices: list[dict], star: str, kind: str) -> list[dict]:
    """Your deployed relays / beacons in a system."""
    t = KINDS[kind]
    return [d for d in devices if d.get("device_type") == t and d.get("location") and star_of(d["location"]) == star
            and not str(d.get("status") or "").startswith("stowed")]


def presence(devices: list[dict]) -> dict[str, dict[str, list[dict]]]:
    """{star: {kind: [your deployed relays / beacons / wards there]}} for the Systems list."""
    out: dict[str, dict[str, list[dict]]] = {}
    for d in devices:
        k = next((k for k, t in KINDS.items() if d.get("device_type") == t), None)
        if k and d.get("location") and not str(d.get("status") or "").startswith("stowed"):
            out.setdefault(star_of(d["location"]), {x: [] for x in KINDS})[k].append(d)
    return out
