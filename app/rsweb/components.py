"""Prints that use up component devices (blueprint `components`, e.g. a biosphere cultivator takes 2 hydroponic bays,
1 nutrient synthesizer and 1 orbital farm). The factory uses components deployed at its location, and its queue
waits at the head until they're there (`waiting_for.components: {type: {have, need}}`, live 2026-10-18).

- A new print (any enqueue_print the app sends) uses the components already at the factory (deployed there or queued
  there, less what earlier prints in its queue use up), then pins spare ones from elsewhere to the factory's location,
  and only queues the rest first, on the same factory, so they're printed there before the item. A component the game
  won't print (a locked blueprint) is logged and the item queued anyway: the factory waits for it. They carry the item's tags (its reservation: contract, fleet, spare …), with the item's
  to:/at: pins swapped for the factory's location so the loadout pass keeps them there.
- A factory already waiting for components gets them from spares (pinned to its location) or printed at another
  autofactory (component_pass in the engine). Components of the missing type already at its location are claimed
  (tagged `component`) first: printed before 1.292.0 they came out tagged spare/stock or untagged, the printing hub's
  loadout (impulse charge: 0) made them spare, and the two hubs' component passes sent them back and forth (live
  2026-10-28: both vector charge prints sat at 4 of 6 impulse charges while two charges crossed between the systems).
"""
from __future__ import annotations

import re
from typing import Any

PRINT_PATH = re.compile(r"^/?(?:v1/)?devices/([^/]+)$")
MAX_DEPTH = 3
COMPONENT = "component"   # loadouts.is_component: reserved for a print at its pinned location, never spare


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
    """The item's tags for its components: `component`, pinned to the factory's location; the item's location pins and
    the tags that would count them for a fleet or as stock left out."""
    from .loadouts import at_tag, to_tag
    keep = [t for t in tags or [] if t not in ("spare", "stock", COMPONENT) and not t.startswith(("to:", "at:", "fleet:", "home:"))]
    return keep + [COMPONENT, to_tag(location.split("-")[0]), at_tag(location)]


def retag(d: dict, tags: list[str]) -> dict:
    """The PATCH configuration that makes a device a component with `tags`: its spare/stock tags and other pins go."""
    return {"add_tags": tags, "remove_tags": [t for t in d.get("tags") or [] if t not in tags and (
        t in ("spare", "stock") or t.startswith(("to:", "at:", "fleet:", "home:")))]}


def claimable(devices: list[dict]) -> list[tuple[dict, list[str]]]:
    """Devices at a factory waiting for components of their type that aren't tagged as its components yet:
    [(device, tags)], at most what the factory still needs."""
    from . import printqueue as pq
    from .loadouts import for_contract, is_component, pinned_at
    out = []
    for f in devices:
        wf = (f.get("waiting_for") or {}).get("components") or {}
        if not isinstance(wf, dict) or not f.get("location"):
            continue
        tags = component_tags((pq.items(f) or [{}])[0].get("tags"), f["location"])
        for t, x in sorted(wf.items()):
            if not isinstance(x, dict):
                continue
            here = [d for d in devices if d.get("device_type") == t and d.get("location") == f["location"]
                    and not d.get("stowed_in_device_code") and not str(d.get("status") or "").startswith(("stowed", "compact"))
                    and not any(str(g).startswith("fleet:") for g in d.get("tags") or []) and not for_contract(d)
                    and pinned_at(d) in (None, f["location"])]
            mine = sum(1 for d in here if is_component(d))
            for d in sorted((d for d in here if not is_component(d)), key=lambda d: d.get("device_code") or "")[
                    :max(0, int(x.get("need") or 0) - mine)]:
                out.append((d, tags))
    return out


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
    devices = await db.kv_get("devices", []) or []
    tags = component_tags(body.get("tags"), fac["location"])
    for t, n in sorted(need.items()):
        left = n * qty - free_here(devices, fac, t, bps)
        for d in spares_elsewhere(devices, fac["location"], t)[:max(0, left)]:   # pinned: the loadout pass brings them
            try:
                await api.request("PATCH", f"/devices/{d['device_code']}", json_body={"configuration": retag(d, tags)})
                left -= 1
            except Exception:  # noqa: BLE001  (not pinned: printed instead)
                pass
        if left <= 0:
            continue
        sub = {"command": "enqueue_print", "device_type": t, "quantity": left, "tags": tags}
        try:
            await before_print(api, db, path, sub, depth + 1)
            await api.request("POST", path, json_body=sub, components_done=True)
        except Exception as e:  # noqa: BLE001
            # live 2026-10-19: climate processors need a terraform monitor, whose blueprint isn't unlocked ("Blueprint
            # not yet unlocked: terraform_monitor") — that refusal aborted the item's own print. The item is queued
            # anyway: the factory waits for the component, which can come from a spare or another source
            # since v3.6.1 decommissioning a device made from components unlocks the components' blueprints too
            hint = (f" (decommissioning a {str(body.get('device_type')).replace('_', ' ')} or a {t.replace('_', ' ')} "
                    "unlocks it)") if "not yet unlocked" in str(getattr(e, "message", e)).lower() else ""
            await _note(db, f"{m.group(1)}: couldn't queue {left} {t.replace('_', ' ')} for "
                            f"{str(body.get('device_type')).replace('_', ' ')} ({getattr(e, 'message', e)}); "
                            f"the {str(body.get('device_type')).replace('_', ' ')} waits for it" + hint)


async def _note(db: Any, text: str) -> None:
    """A warning in the automation log (rule "components")."""
    from .automations import MAX_LOG, now_iso
    entries = await db.kv_get("automation_log", []) or []
    entries.append({"at": now_iso(), "rule": "components", "level": "warning", "text": text})
    await db.kv_set("automation_log", entries[-MAX_LOG:])


def free_here(devices: list[dict], fac: dict, device_type: str, bps: dict[str, dict]) -> int:
    """Components of a type the factory can use for a new print: deployed at its location, plus the ones its queue is
    printing, minus what the prints already in its queue will use up."""
    from . import printqueue as pq
    loc = fac.get("location")
    n = sum(1 for d in devices if d.get("device_type") == device_type and d.get("location") == loc
            and not d.get("stowed_in_device_code") and not str(d.get("status") or "").startswith(("stowed", "compact")))
    queued = list(pq.items(fac)) if pq.has_queue(fac) else []
    if isinstance(fac.get("printing"), dict):
        queued.append(fac["printing"])
    for it in queued:
        q = int(it.get("quantity") or 1)
        if it.get("device_type") == device_type:
            n += q
        n -= components(bps.get(it.get("device_type"))).get(device_type, 0) * q
    return max(0, n)


def spares_elsewhere(devices: list[dict], location: str, device_type: str) -> list[dict]:
    """Spare components of a type away from the location and not pinned anywhere (another factory's components stay)."""
    from .loadouts import is_component, pinned_at
    return sorted((d for d in devices if d.get("device_type") == device_type and "spare" in (d.get("tags") or [])
                   and d.get("location") != location and not pinned_at(d) and not is_component(d)),
                  key=lambda d: d.get("device_code") or "")


def coming(devices: list[dict], location: str, device_type: str) -> int:
    """Devices of a type on their way to `location` or queued to print for it (pinned there by an at: tag)."""
    from .loadouts import at_tag, to_tag
    from . import printqueue as pq
    pin = at_tag(location)

    def bound(d: dict) -> bool:   # pinned to two places (before 1.292.0): the one its to: tag names
        tags = d.get("tags") or []
        return sum(1 for t in tags if t.startswith("at:")) < 2 or to_tag(location.split("-")[0]) in tags
    n = sum(1 for d in devices if d.get("device_type") == device_type and pin in (d.get("tags") or [])
            and (d.get("location") != location or d.get("stowed_in_device_code")) and bound(d))
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
