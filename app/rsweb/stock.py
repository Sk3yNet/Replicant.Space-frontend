"""Print queue order and keep-in-stock printing at the printing hubs.

Re-queue: the game has no command to reorder an autofactory's queue (only enqueue_print at the end and dequeue_print by
0-based position), so moving a waiting print removes every waiting print from its new place down and enqueues them again
in the new order, with their tags, oncomplete, controller and flatpack. The print running now can't be moved.

Keep in stock (owner's request, 2026-10-17): each loadout (a fleet's own, or a template's, which its fleets follow) can
keep a number of each device type in stock at the fleet's home, beyond the loadout: `stock: {type: n}` on the fleet
(set in the loadout editor and the Templates grid). Spares of that type in the home system
(tagged `spare`, in no fleet) count, and so do prints already queued for the stock (tagged `stock`). What's missing is
printed one at a time on a factory there with nothing printing or queued, tagged `spare` and `stock`, so any fleet can
take it. Stock prints are low priority: logistics supplies them last (logistics.print_costs), and a stock print still
waiting in a queue is moved behind every other waiting print (stock_pass)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .api import ApiError
from .db import now_iso

log = logging.getLogger("rsweb.stock")

STOCK_TAG = "stock"
SPARE_TAG = "spare"
ORDERS_KV = "stock_orders"   # {factory code: iso} — when a stock print was last queued there (the device list lags)
ORDER_GAP_S = 900


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def is_stock(it: dict) -> bool:
    return STOCK_TAG in (it.get("tags") or [])


def enqueue_body(it: dict) -> dict:
    """The enqueue_print that puts a queue item (printqueue.items) back as it was."""
    body: dict[str, Any] = {"command": "enqueue_print", "device_type": it["device_type"],
                            "quantity": max(1, int(it.get("quantity") or 1))}
    if it.get("tags"):
        body["tags"] = list(it["tags"])
    if it.get("oncomplete"):
        body["oncomplete"] = it["oncomplete"]
    if it.get("notify"):
        body["controller"] = it["notify"]
    if (it.get("extra") or {}).get("flatpack"):
        body["flatpack"] = True
    return body


def moved(queue: list[dict], frm: int, to: int) -> list[dict]:
    """The queue with the item at 0-based `frm` moved to 0-based `to`."""
    q = list(queue)
    it = q.pop(frm)
    q.insert(max(0, min(to, len(q))), it)
    return q


def stock_last(queue: list[dict]) -> list[dict]:
    """Every waiting stock print behind every other waiting print (each group keeps its order)."""
    return [x for x in queue if not is_stock(x)] + [x for x in queue if is_stock(x)]


def hub_factories(fleet: dict, devices: list[dict]) -> list[dict]:
    """The autofactories that print a fleet's stock: at its home, the fleet's own or in no fleet."""
    from .fleets import fleet_of
    from .loadouts import is_factory
    home = fleet.get("home")
    return sorted((d for d in devices if is_factory(d) and star_of(d.get("location")) == home
                   and fleet_of(d) in (None, fleet.get("id"))), key=lambda d: d.get("device_code") or "")


def _current(d: dict) -> tuple[str | None, list[str] | None]:
    """(type, tags) of the print running or waiting for resources now; tags None when not known."""
    pr = d.get("printing")
    if isinstance(pr, dict) and pr.get("device_type"):
        return pr["device_type"], pr.get("tags") if "tags" in pr else None
    st = str(d.get("status") or "")
    if st.startswith("printing") and "(" in st:
        return st[st.find("(") + 1:st.rfind(")")], None
    return None, None


def idle(d: dict) -> bool:
    from . import printqueue as pq
    st = str(d.get("status") or "").lower()
    return not pq.items(d) and _current(d)[0] is None and not st.startswith(("printing", "waiting_for_resources"))


def shortfall(fleet: dict, devices: list[dict], partners: list[dict] | None = None) -> dict[str, int]:
    """{type: n} still to print for the hub's stock: target minus spares at home (or on their way there, `to:` it) minus
    stock prints under way, here or for here on a `partners` hub's autofactory (partner_prints)."""
    from . import printqueue as pq
    from .fleets import fleet_of
    want = {t: int(n) for t, n in (fleet.get("stock") or {}).items() if int(n or 0) > 0}
    if not want or not fleet.get("home"):
        return {}
    to = home_tag(fleet["home"])
    have = {t: 0 for t in want}
    for d in devices:
        t, tags = d.get("device_type"), d.get("tags") or []
        if t in have and SPARE_TAG in tags and not fleet_of(d) \
                and (to in tags or (star_of(d.get("location")) == fleet["home"] and not elsewhere(tags, fleet["home"]))):
            have[t] += 1

    def mine(tags: list[str] | None, home: bool) -> bool:
        """A stock print counts for this hub: on its own factory unless bound elsewhere, on a partner's when bound here."""
        return STOCK_TAG in tags and (not elsewhere(tags, fleet["home"]) if home else to in tags) if tags is not None else home
    for home, facs in ((True, hub_factories(fleet, devices)), (False, [f for p in partners or [] for f in hub_factories(p, devices)])):
        for f in facs:
            for it in pq.items(f):
                if it["device_type"] in have and mine(it.get("tags") or [], home):
                    have[it["device_type"]] += max(1, int(it.get("quantity") or 1))
            t, tags = _current(f)
            if t in have and mine(tags, home):   # tags unknown: counted at home, so it's never printed twice
                have[t] += 1
    return {t: n - have[t] for t, n in want.items() if n > have[t]}


def home_tag(star: str) -> str:
    from .loadouts import to_tag
    return to_tag(star)


def elsewhere(tags: list[str], home: str) -> bool:
    """Tagged to go to another system than `home` (a partner hub's stock print, for its own home)."""
    return any(t.startswith("to:") and t != home_tag(home) for t in tags)


def partners(fleet: dict, fleets: list[dict], pos: dict, reach: float) -> list[dict]:
    """The other printing hubs (stationed fleets that take materials in) within `reach` of this one's home: they print
    each other's stock when their own stockpile can't (owner 2026-10-26: FALQUORYX and SOLARES, ~500 ly apart; a hub
    beyond the sector range, like Delta's at ISOLITHOR, is left out)."""
    from .loadouts import _dist
    home = fleet.get("home")
    return [f for f in fleets if f is not fleet and f.get("home") and f["home"] != home and f.get("station")
            and f.get("materials") == "self" and f.get("role") not in ("logistics", "bootstrap")
            and _dist(home, f["home"], pos) <= reach]


def covers(f: dict, cost: dict[str, float], inventory: dict[str, dict]) -> bool:
    """The stockpile at the factory's location holds one print's worth of `cost`."""
    from .shapes import as_amounts
    have = as_amounts(inventory.get(f.get("location")) or {})
    return bool(cost) and all(have.get(r, 0) >= v for r, v in cost.items())


class StockMixin:
    async def requeue(self, code: str, order: list[dict], why: str) -> tuple[bool, str]:
        """Re-read the factory, then rebuild its waiting queue in `order` (items from printqueue.items, matched by
        position): dequeue from the first position that changes down, enqueue the rest again in the new order."""
        from . import printqueue as pq
        try:
            dev = await self.api.get(f"/devices/{code}") or {}
        except ApiError as e:
            return False, e.message
        now = pq.items(dev)
        if [(x["device_type"], x.get("tags")) for x in now] != [(x["device_type"], x.get("tags")) for x in
                                                                   sorted(order, key=lambda x: x["api_index"])]:
            return False, "the queue changed meanwhile: look again and retry"
        k = next((i for i, (a, b) in enumerate(zip(now, order)) if a["api_index"] != b["api_index"]), None)
        if k is None:
            return True, "already in that order"
        for _ in range(len(now) - k):
            ok, _, err = await self.send("POST", f"/devices/{code}", {"command": "dequeue_print", "index": k},
                                         f"{code}: take #{k + 1} off the queue to re-queue ({why})")
            if not ok:
                return False, f"dequeue failed: {err}"
        for it in order[k:]:
            ok, _, err = await self.send("POST", f"/devices/{code}", enqueue_body(it),
                                         f"{code}: re-queue {it['device_type']} ({why})")
            if not ok:
                return False, f"re-queue of {it['device_type']} failed: {err} — it's off the queue now"
        return True, f"queue re-ordered from #{k + 1}"

    async def stock_pass(self) -> list[str]:
        """Keep-in-stock printing at the homes of fleets whose loadout has stock, and stock prints kept behind every
        other print there."""
        from . import printqueue as pq
        devices = await self.devices()
        out: list[str] = []
        # a stock print taken for a fleet or a system (no longer spare, or bound somewhere) isn't stock any more
        # (owner 2026-10-20: fleet relays reserved for a system kept the tag)
        busy = self.busy_devices(await self.jobs())
        for d in devices:
            tags = d.get("tags") or []
            if STOCK_TAG in tags and d.get("device_code") not in busy \
                    and (SPARE_TAG not in tags or any(t.startswith(("to:", "fleet:")) for t in tags)):
                ok, _, err = await self.send("PATCH", f"/devices/{d['device_code']}",
                                             {"configuration": {"remove_tags": [STOCK_TAG]}}, "auto: drop the stock tag")
                if ok:
                    d["tags"] = [t for t in tags if t != STOCK_TAG]
                    out.append(f"{d['device_code']}: no longer stock (reserved)")
        hubs = [f for f in await self.fleets() if f.get("stock") and f.get("home")]
        if not hubs:
            for line in out:
                await self.log("stock", line)
            return out
        orders = await self.db.kv_get(ORDERS_KV, {}) or {}
        t_now = datetime.now(timezone.utc)

        def recent(code: str) -> bool:
            try:
                return (t_now - datetime.fromisoformat(orders[code])).total_seconds() < ORDER_GAP_S
            except (KeyError, ValueError, TypeError):
                return False
        from .logistics import SECTOR_RANGE_LY
        from .loadouts import GATHER
        from .shapes import as_amounts, normalize_blueprints, normalize_inventory
        cat = await self.db.kv_get("stars", {}) or {}
        pos = {s.get("designation"): s.get("position") or {} for s in cat.get("stars") or [] if isinstance(s, dict)}
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        every = await self.fleets()
        for f in hubs:
            facs = hub_factories(f, devices)
            mates = partners(f, every, pos, SECTOR_RANGE_LY)
            for fac in facs:   # a stock print waiting ahead of another print goes behind it
                q = pq.items(fac)
                if q and stock_last(q) != q and not recent(fac["device_code"]):
                    ok, msg = await self.requeue(fac["device_code"], stock_last(q), "stock prints go last")
                    orders[fac["device_code"]] = now_iso()
                    out.append(f"{fac['device_code']}: stock prints moved to the back — {msg}")
            short = shortfall(f, devices, mates)
            for t, n in sorted(short.items(), key=lambda kv: (-kv[1], kv[0])):
                cost = as_amounts((bps.get(t) or {}).get("resources"))
                free = [x for x in facs if idle(x) and not recent(x["device_code"])]
                fac = next((x for x in free if covers(x, cost, inv)), None)
                tags = [SPARE_TAG, STOCK_TAG]
                if not fac:
                    # the home stockpile can't cover it: a partner hub whose stockpile can prints it and a carrier brings
                    # it over (owner 2026-10-26: shipping the device beats shipping its materials); else it waits here
                    fac = next((x for p in mates for x in hub_factories(p, devices)
                                if idle(x) and not recent(x["device_code"]) and covers(x, cost, inv)), None)
                    if fac:
                        tags += [GATHER, home_tag(f["home"])]
                    else:
                        fac = free[0] if free else None
                if not fac:
                    break
                away = star_of(fac.get("location")) != f["home"]
                job = await self.create_job("stock", f"stock: print {t} for {f.get('name') or f['id']} ({f['home']})"
                                            + (f" at {star_of(fac.get('location'))}" if away else ""),
                                            fac["device_code"],
                                            [self._stock_step(fac["device_code"], t, tags)], {"devices": []})
                orders[fac["device_code"]] = now_iso()
                if job:
                    out.append(f"{fac['device_code']}: printing a {t} to keep in stock at {f['home']} ({n} short)"
                               + (f", from the {star_of(fac.get('location'))} stockpile" if away else ""))
        await self.db.kv_set(ORDERS_KV, {c: v for c, v in orders.items() if recent(c)})
        for line in out:
            await self.log("stock", line)
        return out

    @staticmethod
    def _stock_step(code: str, dtype: str, tags: list[str] | None = None) -> dict:
        from .automations import step
        return step(f"{code}: print {dtype} for stock", f"/devices/{code}",
                    {"command": "enqueue_print", "device_type": dtype, "quantity": 1, "tags": tags or [SPARE_TAG, STOCK_TAG]})
