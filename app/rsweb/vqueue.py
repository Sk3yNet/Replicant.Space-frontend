"""Virtual print queue (owner 2026-11-01): the app holds every print order and gives each factory only a short queue.

Every enqueue_print the app sends to a factory (automations and prints queued by hand in the app alike) is kept here
instead of going to the game; the feeder (Engine.vqueue_pass, every pass) keeps the first KEEP waiting prints of each
factory in the game's queue behind its current print, best first (KEEP 1 since 1.499.0, owner 2026-11-19: the game holds
only the current print and the next one, so the rest can go to any factory; a print waiting for resources is the queue's
first item and counts as the current one):

  0 hand   prints queued by hand (in the app, or untagged ones found in the game's queue)
  1 component   component devices for another print (`component` tag), so a rig's drones always print before it
  2 network   relays, relay stations, beacons and wards
  3 loadout   everything else (fleet loadouts, contracts, lockers …)
  4 stock   keep-in-stock prints (`stock` tag)
  5 rollout   relays printed for the relay rollout (`rollout` tag): every other print goes first (owner 2026-11-16)

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
# printer types that refused a print type (Joe 2026-11-20: the factories at a spot share one app queue, so note when
# one kind won't take a print): {printer_type: {print_type: {"err", "at", "code"}}}; that kind is never offered it again
REFUSE_KV = "printer_refusals"
# answers that say nothing about what the printer can make: no lesson learned from them
TRANSIENT_RE = re.compile(r"queue space|busy|not yet unlocked|resource|component|service unavailable|bad gateway|"
                          r"gateway time-?out|timed? ?out|network|rate|too many|out of range|offline|not found|"
                          r"compact|stowed|attached|travel|unfurl|deploying|folded|moving|cruis|surg|"
                          r"device is \w+ing\b", re.I)   # "Device is unfurling" (live 2026-11-22) taught every autofactory to refuse beacons
GONE_SECONDS = 3600   # prints kept for a printer missing from the device list this long are dropped
KEEP = 1   # prints each factory holds in the game's queue behind its current one (1.499.0; 2 before)
CHUNK = 3  # the most of one order sent at a time (live 2026-11-02: "Not enough queue space (10 available, 28
           # requested)": a print's quantity takes that many of the queue's slots)
HAND = contextvars.ContextVar("vqueue_hand", default=False)   # set around prints the owner queues by hand
PATH = re.compile(r"^/(?:v1/)?devices/([0-9A-Fa-f]{8})/?$")
LIST = re.compile(r"^/(?:v1/)?devices/?$")
NETWORK = ("ftl_relay", "deep_space_relay_station", "ftl_beacon", "ward")
ROLLOUT = "rollout"   # tag on relay prints the relay rollout orders: they wait behind every other print
TARGET = "relaytarget"   # tag on relay prints a relay target orders (Joe 2026-11-19): just ahead of the rollout's
JOB_TAG = "stationjob"   # on a print a station job needs (stations.run_pass): it must be done within the job's deadline
LABELS = {0: "by hand", 0.5: "station job", 1: "component", 2: "network", 3: "loadout", 3.5: "contract", 4: "stock", 4.5: "relay target", 5: "rollout"}
PRIO_TAG = "prio:"   # on a component print: its own priority, just ahead of the print it goes into (component_rank)
COMPONENT_STEP = 0.25


def component_rank(parent: float) -> float:
    """A component's priority: only slightly ahead of the print it goes into (Joe 2026-11-17), never a whole class."""
    return parent - COMPONENT_STEP


def label(p: float) -> str:
    return LABELS.get(p) or ("component" if p != int(p) else "")


def priority(body: dict, hand: bool = False) -> float:
    """By hand 0 > station job 0.5 > network 2 > loadout (fleets) 3 > contract 3.5 > stock 4 > relay target 4.5 > rollout 5; a component goes just ahead of
    its parent print (its prio: tag), or 1 when it carries none (queued before 1.469.0)."""
    tags = body.get("tags") or []
    t = str(body.get("device_type") or "")
    if hand or not tags:
        return 0
    if "component" in tags:
        for x in tags:
            if str(x).startswith(PRIO_TAG):
                try:
                    return float(str(x)[len(PRIO_TAG):])
                except ValueError:
                    pass
        return 1
    if JOB_TAG in tags:   # a station job's deadline (Joe 2026-11-17): ahead of every automatic print
        return 0.5
    if TARGET in tags:
        return 4.5
    if ROLLOUT in tags:
        return 5
    if any(str(x).startswith("contract:") for x in tags):   # contracts and trades wait behind fleets (Joe 2026-11-17)
        return 3.5
    if t in NETWORK or "beacon" in t or "relay" in t:
        return 2
    if "stock" in tags:
        return 4
    return 3


def as_item(o: dict) -> dict:
    """A kept order as the game lists a queue item, marked virtual."""
    b = o["body"]
    it = {"device_type": b.get("device_type"), "quantity": max(1, int(b.get("quantity") or 1)),
          "tags": list(b.get("tags") or []), "virtual": True, "priority": label(o["prio"]), "rank": o["prio"],
          "order": o["seq"]}
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
            from . import loadouts as lom
            if dev is not None and lom.print_class(dev):
                bp = next((b for b in await self.db.kv_get("blueprints", []) or []
                           if isinstance(b, dict) and b.get("device_type") == body.get("device_type")), None)
                if bp is not None and not lom.can_make(dev, bp):   # unknown blueprints go to the game to answer
                    from .api import ApiError
                    raise ApiError(400, f"This printer can only fabricate {lom.print_class(dev)}-class devices: "
                                        f"{body.get('device_type')} isn't an exotic blueprint.")
            elif dev is not None and not lom.is_factory(dev) and not lom.for_contract(dev):
                return None   # a vessel prints through its replicant: no queue (a contract's goods keep it here:
                # the feeder hands it to a working printer at the spot)
            async with self.lock:
                o = await self.add(code, body, priority(body, HAND.get()))
                q = await self.orders(code)
            return {"status": "enqueued", "virtual": True, "queue_length": len(q), "order": o["seq"]}
        if cmd == "dequeue_print" and body.get("order") is not None:
            # a print the panel showed as kept here, named by its order: the index may have moved since the device
            # list was saved (the feeder hands kept prints to the game), so it's never used for these
            async with self.lock:
                s = await self.state()
                gone = next((o for o in s["orders"] if o["factory"] == code and o["seq"] == body["order"]), None)
                if gone is None:
                    from .api import ApiError
                    raise ApiError(404, "That print isn't in the app's queue any more (it may have gone to the game's queue).")
                s["orders"] = [o for o in s["orders"] if o is not gone]
                await self.db.kv_set(KV, s)
            return {"status": "dequeued", "virtual": True, "removed": as_item(gone)}
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


def real_prios(st: dict, code: str, real: list[dict]) -> list[int]:
    """The priority of each print in a factory's game queue: the one it was given when moved there by hand
    (`note_real`), else the one its tags give."""
    left = list((st.get("real") or {}).get(code) or [])
    out = []
    for it in real:
        m = next((e for e in left if e.get("device_type") == it.get("device_type")
                  and list(e.get("tags") or []) == list(it.get("tags") or [])), None)
        if m is not None:
            left.remove(m)
            out.append(float(m.get("prio", 3)))
        else:
            out.append(priority(it))
    return out


def note_real(st: dict, code: str, real: list[dict], prios: list[int]) -> None:
    """Remember the game queue's prints whose priority isn't the one their tags give (a hand move), so the feeder
    doesn't undo the move."""
    own = [{"device_type": it.get("device_type"), "tags": list(it.get("tags") or []), "prio": p}
           for it, p in zip(real, prios) if p != priority(it)]
    st.setdefault("real", {})
    if own:
        st["real"][code] = own
    else:
        st["real"].pop(code, None)


def hand_prios(orig: list[int]) -> list[int]:
    """Priorities for a queue the owner put in this order: each print's own, raised to the best of those behind it,
    so sorting by (priority, place) keeps the order (a print moved up takes the priority of what it now stands before)."""
    p = list(orig)
    for i in range(len(p) - 2, -1, -1):
        p[i] = min(p[i], p[i + 1])
    return p


def head_waits(dev: dict) -> int:
    """1 when the game queue's first item is the factory's current print, waiting for resources or components (live
    2026-11-02), else 0: the printing one isn't listed in the queue."""
    return 1 if str((dev or {}).get("status") or "").startswith("waiting_for_resources") and (dev or {}).get("print_queue") else 0


def plan(real: list[dict], kept: list[dict], keep: int | None = None,
         prios: list[int] | None = None) -> tuple[int, list[dict], list[tuple[str, dict]]]:
    """What the feeder changes in one factory's game queue: (k, back, send). Dequeue the game's waiting prints from
    position k down; `back` are the ones of them kept here again; then send `send` in order (("real", queue item) put
    back, or ("kept", order) taken from here). `real` is printqueue.items of the game's queue (no virtual ones),
    `kept` the orders kept here, best first. The game's queue ends up as the `keep` best prints of both."""
    pool = [(prios[i] if prios else priority(it), i, "real", it) for i, it in enumerate(real)] + \
           [(o["prio"], len(real) + n, "kept", o) for n, o in enumerate(kept)]
    want = sorted(pool, key=lambda x: (x[0], x[1]))[:max(KEEP if keep is None else keep, 0)]
    k = 0
    while k < len(real) and k < len(want) and want[k][2] == "real" and want[k][3] is real[k]:
        k += 1
    send = [(w[2], w[3]) for w in want[k:]]
    resent = {id(it) for kind, it in send if kind == "real"}
    back = [it for it in real[k:] if id(it) not in resent]
    return k, back, send


def shared(kept: list[dict], code: str, waiting: dict[str, int], keep: int | None = None) -> list[dict]:
    """What factory `code` may take of a spot's pooled orders (best first): its own, and a neighbor's only when that
    neighbor's game queue is already full (`waiting` {factory: waiting prints in the game's queue}), so the order would
    wait behind it. Before (live 2026-11-17), the first factories fed took everyone's orders: 10 matrices split over 5
    autofactories by hand went 4/4/2/0/0."""
    keep = KEEP if keep is None else keep
    return [o for o in kept if o.get("factory") == code or waiting.get(o.get("factory"), keep) >= keep]


def refused(refusals: dict, printer_type: str | None, print_type: str | None) -> bool:
    return bool(print_type) and print_type in ((refusals or {}).get(printer_type or "") or {})


def learned(refusals: dict) -> dict:
    """The refusals that say what a kind of printer can't make: one noted from an answer about the printer's state
    (before 1.538.0, "Device is unfurling" from 11C05A70 at NIO-3-L4 kept every autofactory off beacons) is forgotten."""
    out = {}
    for ptype, by in (refusals or {}).items():
        keep = {t: r for t, r in (by or {}).items() if not TRANSIENT_RE.search(str((r or {}).get("err") or ""))}
        if keep:
            out[ptype] = keep
    return out


def takeable(orders: list[dict], refusals: dict, printer_type: str | None) -> list[dict]:
    """The pooled orders a printer of this type may be given: none of a print type its kind has refused."""
    return [o for o in orders if not refused(refusals, printer_type, (o.get("body") or {}).get("device_type"))]


def hand_to(pool: list[dict], refusals: dict, print_type: str | None, load: dict[str, int] | None = None) -> str | None:
    """Another printer of the shared queue whose kind hasn't refused this print type, the least loaded first."""
    ok = [d for d in pool if d.get("device_code") and not refused(refusals, d.get("device_type"), print_type)]
    if not ok:
        return None
    return min(ok, key=lambda d: ((load or {}).get(d["device_code"], 0), d["device_code"]))["device_code"]


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


def queueless(d: dict) -> bool:
    """A printer whose device row says it has no queue (queue_size 0, the crystal synthesis lab): it takes one print
    at a time, and only while it makes nothing. A row without queue_size is an ordinary 10-slot factory."""
    return "queue_size" in d and d.get("queue_size") is not None and int(d.get("queue_size") or 0) <= 0


def idle_printer(d: dict) -> bool:
    """Not printing or waiting on a print."""
    st = str(d.get("status") or "")
    return not st.startswith(("printing", "waiting"))


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
