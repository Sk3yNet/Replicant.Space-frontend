"""Prints that use up component devices (blueprint `components`, e.g. a biosphere cultivator takes 2 hydroponic bays,
1 nutrient synthesizer and 1 orbital farm). The factory uses components deployed at its location, and its queue
waits at the head until they're there (`waiting_for.components: {type: {have, need}}`, live 2026-10-18).

- A new print (any enqueue_print the app sends) queues its components first, on the same factory, so they're printed
  there before the item. They carry the item's tags (its reservation: contract, fleet, spare …), with the item's
  to:/at: pins swapped for the factory's location so the loadout pass keeps them there.
- A factory already waiting for components gets them from spares (pinned to its location) or printed at another
  autofactory (component_pass in the engine).
"""
from __future__ import annotations

import re
from typing import Any

PRINT_PATH = re.compile(r"^/?(?:v1/)?devices/([^/]+)$")
MAX_DEPTH = 3


def components(bp: dict | None) -> dict[str, int]:
    """{device type: count} one print of this blueprint uses up."""
    raw = (bp or {}).get("components") or {}
    if isinstance(raw, list):   # [{device_type, quantity}] just in case
        raw = {x.get("device_type"): x.get("quantity") or x.get("count") or 1 for x in raw if isinstance(x, dict)}
    out = {}
    for t, n in raw.items() if isinstance(raw, dict) else []:
        try:
            if t and int(n) > 0:
                out[str(t)] = int(n)
        except (TypeError, ValueError):
            pass
    return out


def total_resources(device_type: str, bps: dict[str, dict], depth: int = 0) -> dict[str, float]:
    """The materials one print takes, its components' prints included."""
    from .shapes import as_amounts
    bp = bps.get(device_type) or {}
    out = dict(as_amounts(bp.get("resources") or {}))
    if depth < MAX_DEPTH:
        for t, n in components(bp).items():
            for r, q in total_resources(t, bps, depth + 1).items():
                out[r] = out.get(r, 0.0) + q * n
    return out


def component_tags(tags: list[str] | None, location: str) -> list[str]:
    """The item's tags for its components: its reservation kept, its location pins replaced by the factory's."""
    from .loadouts import at_tag, to_tag
    keep = [t for t in tags or [] if not t.startswith(("to:", "at:"))]
    return keep + [to_tag(location.split("-")[0]), at_tag(location)]


async def before_print(api: Any, db: Any, path: str, body: Any, depth: int = 0) -> None:
    """Called before every POST the app sends: an enqueue_print of a type with components queues those first on the
    same factory (and theirs before them)."""
    if not isinstance(body, dict) or body.get("command") != "enqueue_print" or depth >= MAX_DEPTH:
        return
    m = PRINT_PATH.match(path or "")
    if not m:
        return
    from .shapes import normalize_blueprints
    bps = {b["device_type"]: b for b in normalize_blueprints(await db.kv_get("blueprints", []) or [])}
    need = components(bps.get(body.get("device_type")))
    if not need:
        return
    fac = next((d for d in await db.kv_get("devices", []) or [] if d.get("device_code") == m.group(1)), None) or {}
    from .loadouts import is_factory
    if not fac.get("location") or not is_factory(fac):
        return   # a replicant's vessel prints one at a time through the replicant: no queue to put them in
    qty = int(body.get("quantity") or 1)
    tags = component_tags(body.get("tags"), fac["location"])
    for t, n in sorted(need.items()):
        sub = {"command": "enqueue_print", "device_type": t, "quantity": n * qty, "tags": tags}
        await before_print(api, db, path, sub, depth + 1)
        await api.request("POST", path, json_body=sub, components_done=True)


def coming(devices: list[dict], location: str, device_type: str) -> int:
    """Devices of a type on their way to `location` or queued to print for it (pinned there by an at: tag)."""
    from .loadouts import at_tag
    from . import printqueue as pq
    pin = at_tag(location)
    n = sum(1 for d in devices if d.get("device_type") == device_type and pin in (d.get("tags") or [])
            and (d.get("location") != location or d.get("stowed_in_device_code")))
    for f in devices:
        for it in pq.items(f) if pq.has_queue(f) else []:
            if it.get("device_type") == device_type and pin in (it.get("tags") or []):
                n += 1
        pr = f.get("printing") or {}
        if isinstance(pr, dict) and pr.get("device_type") == device_type and pin in (pr.get("tags") or []):
            n += 1
    return n


def shortfalls(devices: list[dict]) -> list[dict]:
    """Factories waiting for components: [{factory, location, type, short, tags}] — what's still missing beyond what's
    there or already on its way."""
    from . import printqueue as pq
    out = []
    for f in devices:
        wf = (f.get("waiting_for") or {}).get("components") or {}
        if not isinstance(wf, dict) or not f.get("location"):
            continue
        head = (pq.items(f) or [{}])[0]
        for t, x in sorted(wf.items()):
            if not isinstance(x, dict):
                continue
            short = int(x.get("need") or 0) - int(x.get("have") or 0) - coming(devices, f["location"], t)
            if short > 0:
                out.append({"factory": f["device_code"], "location": f["location"], "type": t, "short": short,
                            "tags": component_tags(head.get("tags"), f["location"])})
    return out
