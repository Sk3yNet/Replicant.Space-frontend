"""Decommission at an autofactory: take a device to an autofactory and decommission it there, so the autofactory learns
its blueprint (game docs: "Decommission one to an autofactory to learn the blueprint, so you can print more").

Asking for it (device page) tags the device `to:<factory's system>` (unless it's there already) and `at:<factory's
location>`, and lets it go from its fleet (fleet / home / spare tags off) — the loadout pass then carries it there
(compacting a large device first) and places it at the autofactory. The request is kept in kv "decommission_queue"
{device: {factory, at, asked_at, sent_at?}}; once the device is idle at that location the engine's
decommission_queue_pass sends `decommission` and waits for `device.decommissioned`. A device that's gone from the list
(decommissioned) leaves the queue.
"""
from __future__ import annotations

import math

KV = "decommission_queue"
BUSY = ("stowed", "travel", "cruis", "surg", "compact", "unfurl", "decommission", "recall", "attached")


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def factories(devices: list[dict], stars: dict[str, dict], here: str | None) -> list[dict]:
    """Your autofactories that are set up somewhere, nearest to `here` first: [{code, location, star, distance}]."""
    pos = {k: (v or {}).get("position") for k, v in stars.items()}
    ref = pos.get(star_of(here))
    out = []
    for d in devices:
        if "autofactory" in (d.get("device_type") or "") and d.get("location") \
                and not str(d.get("status") or "").startswith(("stowed", "compact")):
            p = pos.get(star_of(d["location"]))
            dist = math.dist([ref.get(k, 0) for k in "xyz"], [p.get(k, 0) for k in "xyz"]) if ref and p else None
            out.append({"code": d["device_code"], "location": d["location"], "star": star_of(d["location"]),
                        "distance": None if dist is None else round(dist, 1)})
    out.sort(key=lambda f: (f["star"] != star_of(here), f["distance"] is None, f["distance"] or 0, f["code"]))
    return out


def retag(d: dict, factory: dict, to_tag, at_tag) -> dict:
    """The tag change that sends `d` to the factory: {add_tags, remove_tags} (no tag in both)."""
    add = [at_tag(factory["location"])]
    if star_of(d.get("location")) != factory["star"]:
        add.append(to_tag(factory["star"]))
    remove = [t for t in d.get("tags") or [] if t.startswith(("fleet:", "home:", "to:", "at:")) or t == "spare"]
    return {"add_tags": add, "remove_tags": [t for t in remove if t not in add]}


def ready(d: dict, entry: dict) -> bool:
    """At the autofactory and idle: decommission now."""
    return (d.get("location") == entry.get("at") and not entry.get("sent_at")
            and not str(d.get("status") or "").startswith(BUSY)
            and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code"))
