"""Virtual print queue (owner 2026-11-01): the app holds every print order and gives each factory only a short queue.

Every enqueue_print the app sends to a factory (automations and prints queued by hand in the app alike) is kept here
instead of going to the game; the feeder (Engine.vqueue_pass, every pass) keeps the first KEEP waiting prints of each
factory in the game's queue, best first:

  0 hand   prints queued by hand (in the app, or untagged ones found in the game's queue)
  1 component   component devices for another print (`component` tag), so a rig's drones always print before it
  2 network   relays, relay stations, beacons and wards
  3 loadout   everything else (fleet loadouts, contracts, lockers …)
  4 stock   keep-in-stock prints (`stock` tag)

then in the order they were queued. Factories of one type at one spot share what's kept here (owner 2026-11-03): each
is fed the best of all their kept orders, so their game queues stay even and none sits idle while a neighbor's backs up. A better print waiting here takes the place of a worse one in the game's queue
(that one comes back here). Why: on 2026-11-01 44 mining rigs sat at the head of all 5 factory queues waiting for drones
that nothing could print, because every factory's queue was blocked by a rig.

Reads of the factories (GET /devices, /devices/:code) list the prints waiting here after the game's, each marked
`virtual`, so every pass that counts what's queued (loadouts, components, stock, logistics) sees them as before.
dequeue_print with an index past the game's queue and clear_queue take them off here.
"""
from __future__ import annotations

import asyncio
import contextvars
import re
from typing import Any

KV = "vqueue"
KEEP = 2   # waiting prints each factory holds in the game's queue
CHUNK = 3  # the most of one order sent at a time (live 2026-11-02: "Not enough queue space (10 available, 28
           # requested)": a print's quantity takes that many of the queue's slots)
HAND = contextvars.ContextVar("vqueue_hand", default=False)   # set around prints the owner queues by hand
PATH = re.compile(r"^/(?:v1/)?devices/([0-9A-Fa-f]{8})/?$")
LIST = re.compile(r"^/(?:v1/)?devices/?$")
NETWORK = ("ftl_relay", "deep_space_relay_station", "ftl_beacon", "ward")
LABELS = {0: "by hand", 1: "component", 2: "network", 3: "loadout", 4: "stock"}


def priority(body: dict, hand: bool = False) -> int:
    tags = body.get("tags") or []
    t = str(body.get("device_type") or "")
    if hand or not tags:
        return 0
    if "component" in tags:
        return 1
    if t in NETWORK or "beacon" in t or "relay" in t:
        return 2
    if "stock" in tags:
        return 4
    return 3


def as_item(o: dict) -> dict:
    """A kept order as the game lists a queue item, marked virtual."""
    b = o["body"]
    it = {"device_type": b.get("device_type"), "quantity": max(1, int(b.get("quantity") or 1)),
          "tags": list(b.get("tags") or []), "virtual": True, "priority": LABELS.get(o["prio"], "")}
    if b.get("oncomplete"):
        it["oncomplete"] = b["oncomplete"]
    if b.get("controller"):
        it["notify"] = {"device": b["controller"]}
    if b.get("flatpack"):
        it["flatpack"] = True
    return it


def body_of(it: dict) -> dict:
    """The enqueue_print that puts a game queue item back (printqueue.items shape or raw)."""
    from .stock import enqueue_body
    from . import printqueue as pq
    if "api_index" not in it:
        it = pq.items({"print_queue": [it]})[0]
    return enqueue_body(it)


class VirtualQueue:
    def __init__(self, db: Any) -> None:
        self.db = db
        self.lock = asyncio.Lock()

    async def state(self) -> dict:
        s = await self.db.kv_get(KV, None) or {}
        s.setdefault("seq", 0)
        s.setdefault("orders", [])
        return s

    async def orders(self, code: str | None = None, codes: list[str] | None = None) -> list[dict]:
        """Kept orders of one factory (`code`), of several (`codes`: a spot's factories pooled), or all; best first."""
        s = await self.state()
        want = set(codes) if codes else ({code} if code else None)
        out = [o for o in s["orders"] if want is None or o["factory"] in want]
        return sorted(out, key=lambda o: (o["prio"], o["seq"]))

    async def add(self, code: str, body: dict, prio: int, s: dict | None = None, seq: int | None = None) -> dict:
        from .automations import now_iso
        own = s is None
        if own:
            s = await self.state()
        if seq is None:
            s["seq"] += 1
            seq = s["seq"]
        keep = {k: v for k, v in body.items() if k in ("device_type", "quantity", "tags", "oncomplete", "controller", "flatpack")}
        o = {"factory": code, "body": keep, "prio": prio, "seq": seq, "at": now_iso()}
        s["orders"].append(o)
        if own:
            await self.db.kv_set(KV, s)
        return o

    # --- the API client's hooks ------------------------------------------------------------------
    async def intercept(self, path: str, body: Any) -> Any | None:
        """A queue command the app sends: the response, or None to send it to the game as it is."""
        m = PATH.match(path or "")
        if not m or not isinstance(body, dict):
            return None
        code, cmd = m.group(1).upper(), body.get("command")
        if cmd == "enqueue_print":
            dev = await self._device(code)
            from .loadouts import is_factory
            if dev is not None and not is_factory(dev):
                return None   # a vessel prints through its replicant: no queue
            async with self.lock:
                o = await self.add(code, body, priority(body, HAND.get()))
                q = await self.orders(code)
            return {"status": "enqueued", "virtual": True, "queue_length": len(q), "order": o["seq"]}
        if cmd == "dequeue_print":
            dev = await self._device(code) or {}
            real = len(dev.get("print_queue") and [x for x in dev["print_queue"] if not (isinstance(x, dict) and x.get("virtual"))] or [])
            i = int(body.get("index") or 0)
            if i < real:
                return None
            async with self.lock:
                s = await self.state()
                mine = sorted((o for o in s["orders"] if o["factory"] == code), key=lambda o: (o["prio"], o["seq"]))
                if not 0 <= i - real < len(mine):
                    return None   # the game answers "Invalid queue index"
                gone = mine[i - real]
                s["orders"] = [o for o in s["orders"] if o is not gone]
                await self.db.kv_set(KV, s)
            return {"status": "dequeued", "virtual": True, "removed": as_item(gone)}
        if cmd == "clear_queue":
            async with self.lock:
                s = await self.state()
                s["orders"] = [o for o in s["orders"] if o["factory"] != code]
                await self.db.kv_set(KV, s)
        return None

    async def _device(self, code: str) -> dict | None:
        return next((d for d in await self.db.kv_get("devices", []) or [] if d.get("device_code") == code), None)

    async def merge(self, path: str, body: Any) -> Any:
        """A GET of the factories: the prints kept here listed after the game's."""
        if not isinstance(body, dict):
            return body
        if LIST.match(path or "") and isinstance(body.get("devices"), list):
            s = await self.state()
            if s["orders"]:
                by: dict[str, list[dict]] = {}
                for o in sorted(s["orders"], key=lambda o: (o["prio"], o["seq"])):
                    by.setdefault(o["factory"], []).append(o)
                for d in body["devices"]:
                    if isinstance(d, dict) and d.get("device_code") in by:
                        d["print_queue"] = list(d.get("print_queue") or []) + [as_item(o) for o in by[d["device_code"]]]
            return body
        m = PATH.match(path or "")
        if m and body.get("device_code"):
            mine = await self.orders(body["device_code"])
            if mine:
                body["print_queue"] = list(body.get("print_queue") or []) + [as_item(o) for o in mine]
        return body


def plan(real: list[dict], kept: list[dict], keep: int = KEEP) -> tuple[int, list[dict], list[tuple[str, dict]]]:
    """What the feeder changes in one factory's game queue: (k, back, send). Dequeue the game's waiting prints from
    position k down; `back` are the ones of them kept here again; then send `send` in order (("real", queue item) put
    back, or ("kept", order) taken from here). `real` is printqueue.items of the game's queue (no virtual ones),
    `kept` the orders kept here, best first. The game's queue ends up as the `keep` best prints of both."""
    pool = [(priority(it), i, "real", it) for i, it in enumerate(real)] + \
           [(o["prio"], len(real) + n, "kept", o) for n, o in enumerate(kept)]
    want = sorted(pool, key=lambda x: (x[0], x[1]))[:max(keep, 0)]
    k = 0
    while k < len(real) and k < len(want) and want[k][2] == "real" and want[k][3] is real[k]:
        k += 1
    send = [(w[2], w[3]) for w in want[k:]]
    resent = {id(it) for kind, it in send if kind == "real"}
    back = [it for it in real[k:] if id(it) not in resent]
    return k, back, send


def cost(t: str | None, bps: dict[str, dict]) -> dict[str, float]:
    from .shapes import as_amounts
    return dict(as_amounts((bps.get(t or "") or {}).get("resources") or {}))


def jump(cur: dict, real: list[dict], kept: list[dict], have: dict[str, float], bps: dict[str, dict],
         free: dict[str, int] | None = None) -> tuple[str, dict] | None:
    """A print that can start while the factory's current print waits (owner 2026-11-01): the best waiting one (real
    queue item or kept order) whose cost the stockpile covers with the waiting print's own cost still set aside. A
    component the waiting print waits for may use that reserve (it can't start without it). A print that uses
    components goes only when `free` ({type: count} free at the factory, less what its queue uses) covers them and
    the waiting print needs none of that type (snapshot 2026-11-03 23:43: mining rigs sat behind relay stations waiting
    for a relay while 18 drones lay idle at FALQUORYX). None when nothing fits."""
    from .components import components
    from .shapes import as_amounts
    have = dict(as_amounts(have or {}))
    reserve = cost(cur.get("device_type"), bps)
    wants = set(components(bps.get(cur.get("device_type") or "")))
    pool = [(priority(it), i, "real", it) for i, it in enumerate(real)] + \
           [(o["prio"], len(real) + n, "kept", o) for n, o in enumerate(kept)]
    for _, _, kind, x in sorted(pool, key=lambda p: (p[0], p[1])):
        t = x.get("device_type") if kind == "real" else x["body"].get("device_type")
        need = cost(t, bps)
        q = max(1, int((x.get("quantity") if kind == "real" else x["body"].get("quantity")) or 1))
        if kind == "kept":
            q = min(q, CHUNK)
        uses = components(bps.get(t or ""))
        if uses:
            if free is None or uses.keys() & wants:
                continue
            # a real item's own use is already taken off what's free; a kept order's isn't
            q = min(q, min((free.get(c, 0) + (n * q if kind == "real" else 0)) // n for c, n in uses.items()))
            if q < 1:
                continue
            if kind == "real" and q < max(1, int(x.get("quantity") or 1)):
                continue   # a real item goes whole or not at all
        if not need:
            continue   # no blueprint cost known: can't tell
        own = t in wants
        if all(have.get(r, 0.0) - (0.0 if own else reserve.get(r, 0.0)) >= v * q for r, v in need.items()):
            return kind, x
    return None


def send_body(o: dict) -> dict:
    """The enqueue_print for a kept order: at most CHUNK of it."""
    return {"command": "enqueue_print", **o["body"], "quantity": min(CHUNK, max(1, int(o["body"].get("quantity") or 1)))}


def take(orders: list[dict], code: str, o: dict, n: int) -> list[dict]:
    """The orders with `n` of order `o` sent to the game: what's left of it stays."""
    out = []
    for x in orders:
        if x["factory"] == code and x["seq"] == o["seq"]:
            left = max(1, int(x["body"].get("quantity") or 1)) - n
            if left > 0:
                out.append({**x, "body": {**x["body"], "quantity": left}})
            continue
        out.append(x)
    return out
