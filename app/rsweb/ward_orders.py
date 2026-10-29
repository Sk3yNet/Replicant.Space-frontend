"""Ward, relay and beacon orders (Map › Prospects: Add ward; Systems: add / remove ward, relay, beacon): get one of your
devices of that kind working in a system — activate one that's already there, else send a spare one (tagged to:<star>,
the loadout pass carries it within the supply range; placement takes a relay to an L4/L5 point), else print one on an
autofactory in range (the shortest queue, then the nearest; tagged the same way). A print still waiting in the queue is
dropped for a spare that turns up meanwhile (e.g. one you removed elsewhere). The engine stage ward_orders_pass switches
it on once it's deployed there (a ward within the 25-ward cap and never alongside a hub of yours; a relay only at an
L4/L5 point). A beacon for a system with a local civilization goes on to that body (its event location, else a body
with intelligent / spacefaring life: civilizations only send requests to a beacon AT their planet or moon); the order is
done once one of yours works there. kv "ward_orders" / "relay_requests" / "beacon_requests" ("beacon_orders" is the civilization
beacons' own): {star: {how: activate|send|print, device, factory,
at, sourced, note}}; an order not done in 3 days is dropped. A print order whose print never showed up (no print
queued or printing for it, nothing tagged for it, no job on it: live 2026-10-21/22, 73 relay orders on two autofactories
with 10-slot queues, nothing ever printed and every one expired) is sourced again: a spare, else a factory with a free
queue slot, else it waits for one (and doesn't expire while it waits).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import loadouts as lo
from . import wardhub as wh

KV = "ward_orders"
EXPIRE_DAYS = 3
QUEUE_CAP = 10       # print queue slots when the device row doesn't say (autofactory queue_size)
LOST_MINUTES = 20    # a print not in its factory's queue this long after it was ordered never got there
KINDS = {"ward": "system_ward", "relay": "ftl_relay", "beacon": "ftl_beacon",
         "station": "deep_space_relay_station"}   # station: the 10 ly relay (relaynet.STATION)
LABELS = {"ward": "ward", "relay": "FTL relay", "beacon": "FTL beacon", "station": "deep space relay station"}
RELAY_KINDS = ("relay", "station")


def kv_key(kind: str) -> str:
    return KV if kind == "ward" else f"{kind}_requests"


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def ward_here(devices: list[dict], star: str, dtype: str = "system_ward") -> dict | None:
    """One of your devices of this type deployed in `star` (working first)."""
    ws = [d for d in devices if d.get("device_type") == dtype and wh.deployed(d) and star_of(d.get("location")) == star]
    return sorted(ws, key=lambda d: (not wh.active(d), d.get("device_code") or ""))[0] if ws else None


def bound_to(devices: list[dict], star: str, dtype: str = "system_ward") -> dict | None:
    """One of yours of this type already tagged to:<star> and not working (e.g. printed for an order that expired, live
    2026-10-21: MOLURNA re-ordered while its relay still waited at the factory): a new order sends that one."""
    tag = lo.to_tag(star)
    ds = [d for d in devices if d.get("device_type") == dtype and tag in (d.get("tags") or []) and not wh.active(d)]
    return sorted(ds, key=lambda d: d.get("device_code") or "")[0] if ds else None


def printing_for(devices: list[dict], star: str, dtype: str = "system_ward") -> dict | None:
    """A factory of yours already printing (or with one waiting in its queue) a device of this type tagged to:<star>:
    a new order waits for that print instead of queueing another (owner 2026-10-21)."""
    from . import printqueue as pq
    tag = lo.to_tag(star)
    for f in devices:
        pr = f.get("printing") if isinstance(f.get("printing"), dict) else {}
        its = (list(pq.items(f)) if pq.has_queue(f) else []) + ([pr] if pr else [])
        if any(it.get("device_type") == dtype and tag in (it.get("tags") or []) for it in its):
            return f
    return None


def spare_ward(devices: list[dict], star: str, busy: set[str], pos: dict, reach: float,
               dtype: str = "system_ward", kept: set[str] | frozenset = frozenset()) -> dict | None:
    """A device of this type that isn't working and belongs to no fleet or contract, nearest first, within reach.
    `kept` (a printing hub's own keep-in-stock spares) go last: taking one only gets it printed again there, while
    another hub's extras wait unused (owner 2026-10-28: use up the spare relays at FALQUORYX)."""
    out = []
    for d in devices:
        tags = d.get("tags") or []
        if (d.get("device_type") != dtype or wh.active(d) or d.get("device_code") in busy or d.get("location_stale")
                or any(t.startswith(("fleet:", "contract:", "to:", "boot:")) for t in tags)):
            continue
        here = star_of(d.get("location"))
        if not here:
            host = next((x for x in devices if x.get("device_code") == (d.get("stowed_in_device_code") or "")), {})
            here = star_of(host.get("location"))
        dist = lo._dist(here, star, pos) if here else 1e9
        if reach and reach < dist < 1e9:
            continue
        out.append((d.get("device_code") in kept, dist, d.get("device_code"), d))
    return min(out, key=lambda x: x[:3])[3] if out else None


def civ_places(rows: list[dict], star: str) -> list[str]:
    """Civilization bodies in a system (traffic.coverage rows): event locations first, then bodies with intelligent /
    spacefaring life."""
    from .traffic import CIV_STAGES
    ev = [r["location"] for r in rows if r["star"] == star and (r.get("open") or r.get("completed"))]
    life = [r["location"] for r in rows if r["star"] == star and r["location"] not in ev
            and (r.get("life") or {}).get("life_stage") in CIV_STAGES]
    return ev + life


def civ_missing(devices: list[dict], civ: list[str], dtype: str = "ftl_beacon") -> list[str]:
    """The civilization bodies (civ_places) without a working one of yours: each civilization needs its own beacon at
    its body (two in a system get two)."""
    up = {d.get("location") for d in devices if d.get("device_type") == dtype and wh.active(d)}
    return [c for c in civ if c not in up]


def loose_here(devices: list[dict], star: str, civ: list[str], dtype: str = "ftl_beacon") -> dict | None:
    """One of yours deployed in `star` but not at a civilization body: the next one to move on to a body still
    missing one (a beacon already at a body stays there)."""
    ws = [d for d in devices if d.get("device_type") == dtype and wh.deployed(d) and star_of(d.get("location")) == star
          and d.get("location") not in civ]
    return sorted(ws, key=lambda d: (not wh.active(d), d.get("device_code") or ""))[0] if ws else None


def load(f: dict) -> int:
    """Prints ahead of a new one on this factory: its queue plus the one printing."""
    return len(f.get("print_queue") or []) + bool(f.get("printing"))


def room(f: dict, pending: int = 0) -> int:
    """Free print queue slots on a factory, less `pending` prints ordered on it that it doesn't list yet."""
    return int(f.get("queue_size") or QUEUE_CAP) - len(f.get("print_queue") or []) - pending


def factory_near(devices: list[dict], star: str, busy: set[str], pos: dict, reach: float,
                 ordered: dict[str, int] | None = None, stocked: set[str] | None = None) -> dict | None:
    """The autofactory to print a ward on, within reach of `star`: one whose stockpile covers the print (`stocked`, when
    given: owner 2026-10-26, SOLARES relays sat waiting for conductive while FALQUORYX had plenty), then the shortest
    queue (counting wards already ordered on it, `ordered`), then the nearest; one with a full queue is skipped.
    Compacted or carried ones can't print (seen live 2026-10-11: four orders went to 1131B50B while 3E95BD59, idle
    beside it, lost every tie on its code)."""
    out = []
    for f in devices:
        st = str(f.get("status") or "")
        if not lo.is_factory(f) or f.get("device_code") in busy or f.get("location_stale") or not f.get("location") \
                or f.get("in_control_range") is False or any(t.startswith("boot:") for t in f.get("tags") or []) \
                or st.startswith(("compact", "stowed", "attach")) or f.get("stowed_in_device_code") \
                or f.get("attached_to_device_code"):
            continue
        dist = lo._dist(star_of(f["location"]), star, pos)
        if (reach and reach < dist < 1e9) or room(f, (ordered or {}).get(f["device_code"], 0)) <= 0:
            continue
        out.append((stocked is not None and f["device_code"] not in stocked,
                    load(f) + (ordered or {}).get(f["device_code"], 0), dist, f["device_code"], f))
    return min(out, key=lambda x: x[:4])[4] if out else None


def stocked(devices: list[dict], cost: dict[str, float], inventory: dict[str, dict]) -> set[str]:
    """Factories whose location's stockpile holds one print's worth of `cost`."""
    from .stock import covers
    return {f["device_code"] for f in devices if lo.is_factory(f) and f.get("location") and covers(f, cost, inventory)}


def queued_at(factory: dict, star: str, dtype: str = "system_ward") -> int | None:
    """Index in the factory's print queue of the device waiting for `star` (not yet printing), or None."""
    tag = lo.to_tag(star)
    for i, q in enumerate(factory.get("print_queue") or []):
        if q.get("device_type") == dtype and tag in (q.get("tags") or []):
            return i
    return None


def _age_min(order: dict, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(str(order.get("sourced") or order.get("at")))).total_seconds() / 60
    except ValueError:
        return 1e9


def pending_print(order: dict, now: datetime | None = None) -> bool:
    """A print ordered on a factory so lately that its queue may not show it yet."""
    return (order.get("how") == "print" and bool(order.get("factory"))
            and _age_min(order, now or datetime.now(timezone.utc)) < LOST_MINUTES)


def lost_print(devices: list[dict], star: str, order: dict, jobs: list[dict], dtype: str = "system_ward",
               now: datetime | None = None) -> bool:
    """A print order with nothing to show for it: no print queued or printing for `star`, none of yours tagged for it,
    no job still on it, and past LOST_MINUTES (or waiting for a free slot, with no factory)."""
    if order.get("how") != "print":
        return False
    if order.get("factory") and _age_min(order, now or datetime.now(timezone.utc)) < LOST_MINUTES:
        return False
    if printing_for(devices, star, dtype) or bound_to(devices, star, dtype):
        return False
    return not any(j["status"] in ("running", "waiting") and ((j.get("meta") or {}).get("outpost") == star
                                                              or str(j.get("title") or "").endswith(f" for {star}"))
                   for j in jobs)


def expired(order: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        at = datetime.fromisoformat(str(order.get("at")))
    except ValueError:
        return True
    return now - at > timedelta(days=EXPIRE_DAYS)
