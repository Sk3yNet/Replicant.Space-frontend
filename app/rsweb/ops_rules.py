"""Automation rules for the 'watch' side of the game: civilization beacons, asteroid defense and maintenance.

Mixed into AutomationEngine (automations.py), so `self` has db, api, hub, worker, create_job, log, settings …
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .db import now_iso

log = logging.getLogger("rsweb.auto")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def star_of_(loc) -> str:
    return (loc or "").split("-")[0]


def _ts(v):
    from .automations import _ts as ts
    return ts(v)


def _after(a, b) -> bool:
    """Time a is later than time b (both known)."""
    ta, tb = _ts(a), _ts(b)
    return bool(ta and tb and ta > tb)


def _born(o: dict):
    """When this rock was first seen: an object's designation is reused by a later rock."""
    return o.get("detected_at") or o.get("discovered_at")


class OpsRules:
    # --- ward, relay and beacon orders (Map › Prospects, Systems) --------------------------------------------------
    async def order_ward(self, star: str, user: str = "") -> str:
        return await self.order_outpost("ward", star, user)

    async def remove_ward(self, star: str, user: str = "") -> str:
        return await self.remove_outpost("ward", star, user)

    async def _outpost_ctx(self) -> tuple[dict, float]:
        cat = await self.db.kv_get("stars", {}) or {}
        pos = {s.get("designation"): s.get("position") for s in cat.get("stars") or [] if isinstance(s, dict)}
        from .loadouts import DEFAULT_SETTINGS
        reach = float(((await self.loadout_cfg()).get("settings") or {}).get("max_supply_ly") or DEFAULT_SETTINGS["max_supply_ly"])
        return pos, reach

    async def order_outpost(self, kind: str, star: str, user: str = "") -> str:
        """Add a ward / relay / beacon in `star`: activate one there, else send a spare, else print one (ward_orders.py).
        A beacon in a system with local civilizations goes to each civilization body still missing one."""
        from . import ward_orders as wo
        from . import wardhub as wh
        from . import wards
        if kind not in wo.KINDS:
            return f"unknown kind {kind}"
        dtype, label = wo.KINDS[kind], wo.LABELS[kind]
        star = star.upper()
        devices = await self.devices()
        orders = await self.db.kv_get(wo.kv_key(kind), {}) or {}
        if kind == "ward":
            cat = await self.db.kv_get("stars", {}) or {}
            if star in wards.foreign(cat, devices):
                return f"{star} is already warded by another player"
            if star in {star_of_(x.get("location")) for x in devices if wh.is_hub(x) and wh.deployed(x)}:
                return f"you have a system hub in {star}: wards and hubs don't go together"
            w = wh.wards(devices)
            if w["active"] + len([o for s_, o in orders.items() if s_ != star]) >= wh.WARD_CAP:
                return f"{w['active']} wards active and {len(orders)} on order — the game allows {wh.WARD_CAP}"
        here = wo.ward_here(devices, star, dtype)
        civ = wo.civ_places(await self.civ_coverage(), star) if kind == "beacon" else []
        if civ:   # one beacon per civilization body: a loose one in the system goes on to the next body missing one
            missing = wo.civ_missing(devices, civ, dtype)
            if not missing:
                return f"every civilization in {star} has a working {label} ({', '.join(civ)})"
            here = wo.loose_here(devices, star, civ, dtype)
            idle = next((d for d in devices if d.get("device_type") == dtype and d.get("location") in missing
                         and wh.deployed(d)), None)
            if idle and not here:
                here = idle
        elif here and wh.active(here):
            return f"{here['device_code']} already works in {star}"
        busy = self.busy_devices(await self.jobs())
        order = {"at": now_iso(), "by": user}
        if here:
            order.update(how="activate", device=here["device_code"])
            msg = f"activating {here['device_code']} in {star} ({label})"
        elif (bound := wo.bound_to(devices, star, dtype)):
            order.update(how="send", device=bound["device_code"])
            msg = f"{label} {bound['device_code']} is already on its way to {star} (the loadout pass carries it)"
        elif (fab := wo.printing_for(devices, star, dtype)):
            order.update(how="print", factory=fab["device_code"])
            msg = f"a {label} for {star} is already in the print queue of {fab['device_code']} (the loadout pass then carries it)"
        else:
            how, msg = await self._outpost_source(kind, star, devices, busy, orders)
            if not how:
                return msg
            order.update(how)
        if civ:
            order["civ"] = missing
            if len(civ) > 1:
                msg += f" · {len(missing)} of {len(civ)} civilizations still need one ({', '.join(missing)})"
        orders[star] = order
        await self.db.kv_set(wo.kv_key(kind), orders)
        await self.log("wards", f"{star}: {msg}" + (f" (by {user})" if user else ""))
        await self.ward_orders_pass()
        return msg

    async def _outpost_source(self, kind: str, star: str, devices: list[dict], busy: set[str],
                              orders: dict) -> tuple[dict | None, str]:
        """Get a ward / relay / beacon on its way to `star`: send a spare, else print one on the nearest autofactory.
        Returns the order fields (how, device / factory) and a line, or (None, why not)."""
        from . import loadouts as lo
        from . import ward_orders as wo
        dtype, label = wo.KINDS[kind], wo.LABELS[kind]
        pos, reach = await self._outpost_ctx()
        spare = wo.spare_ward(devices, star, busy, pos, reach, dtype)
        if spare:
            rem = [t for t in spare.get("tags") or [] if t in (lo.SPARE, lo.STOCK) or t.startswith(("home:", "at:"))]
            await self.create_job("wards", f"{label} {spare['device_code']} → {star}", spare["device_code"],
                                  [lo.tag_step(spare["device_code"], [lo.to_tag(star)], rem or None)],
                                  {"devices": [spare["device_code"]]}, force=True)
            busy.add(spare["device_code"])
            return ({"how": "send", "device": spare["device_code"]},
                    f"sending spare {label} {spare['device_code']} to {star} (the loadout pass carries it)")
        ordered: dict[str, int] = {}
        for k in wo.KINDS:
            for o in ((await self.db.kv_get(wo.kv_key(k), {}) or {}) if k != kind else orders).values():
                if o.get("how") == "print" and o.get("factory"):
                    ordered[o["factory"]] = ordered.get(o["factory"], 0) + 1
        f = wo.factory_near(devices, star, busy, pos, reach, ordered)
        if not f:
            return None, f"no spare {label} and no autofactory within {reach:g} ly of {star}"
        await self.create_job("wards", f"print a {label} on {f['device_code']} for {star}", f["device_code"],
                              lo.print_steps({"factory": f["device_code"], "factory_star": star_of_(f.get("location")),
                                              "device_type": dtype, "n": 1, "star": star,
                                              "tags": [lo.to_tag(star)]}), {"devices": []}, force=True)
        return ({"how": "print", "factory": f["device_code"]},
                f"printing a {label} on {f['device_code']} for {star} (the loadout pass then carries it there)")

    async def ward_orders_pass(self) -> list[str]:
        """Ward / relay / beacon orders: one of yours deployed and idle in the ordered system is switched on (a ward
        within the cap and never beside a hub of yours; a relay only at an L4/L5 point — placement takes it there)."""
        from . import ward_orders as wo
        from . import wardhub as wh
        from .automations import step
        from .placement import is_lagrange
        out: list[str] = []
        devices = busy = None
        for kind, dtype in wo.KINDS.items():
            label = wo.LABELS[kind]
            orders = await self.db.kv_get(wo.kv_key(kind), {}) or {}
            if not orders:
                continue
            if devices is None:
                devices = await self.devices()
                busy = self.busy_devices(await self.jobs())
            changed = False
            cov = await self.civ_coverage() if kind == "beacon" else []
            for star, o in list(orders.items()):
                here = wo.ward_here(devices, star, dtype)
                civ = wo.civ_places(cov, star)
                if civ and not wo.expired(o):
                    # local civilizations: each body gets its own beacon, and only one working there counts
                    missing = wo.civ_missing(devices, civ, dtype)
                    if not missing:
                        orders.pop(star)
                        changed = True
                        at = ", ".join(civ)
                        out.append(f"{star}: {label}s up at {at}" if len(civ) > 1 else f"{star}: {label} is up at {at}")
                        await self.log("wards", f"{star}: {label} up at every civilization ({at})", notify=True)
                        continue
                    if missing != o.get("civ"):
                        o["civ"] = missing
                        changed = True
                    idle = next((d for d in devices if d.get("device_type") == dtype and d.get("location") in missing
                                 and wh.deployed(d) and not wh.active(d)), None)
                    here = wo.loose_here(devices, star, civ, dtype) or idle
                    if here and here is not idle:
                        msg = await self._beacon_to_civ(star, missing, devices, busy, keep=civ)
                        if msg and msg != o.get("note"):
                            o["note"] = msg
                            changed = True
                            out.append(f"{star}: {msg}")
                        continue
                    if (not here and len(missing) < len(civ) and o.get("device") not in busy
                            and not wo.bound_to(devices, star, dtype) and not wo.printing_for(devices, star, dtype)
                            and not any(j.get("meta", {}).get("beacon_loc") in civ for j in await self.jobs()
                                        if j["status"] in ("running", "waiting"))):
                        # one body has its beacon, the next needs another: send a spare or print one
                        how, msg = await self._outpost_source(kind, star, devices, busy, orders)
                        if how:
                            o.update(how, at=now_iso())
                            o.pop("factory" if how["how"] == "send" else "device", None)
                        if msg != o.get("note"):
                            o["note"] = msg
                            changed = True
                            out.append(f"{star}: {msg}")
                        continue
                if here and wh.active(here) and not civ:
                    orders.pop(star)
                    changed = True
                    out.append(f"{star}: {label} {here['device_code']} is up")
                    await self.log("wards", f"{star}: {label} {here['device_code']} is up", notify=True)
                    continue
                if wo.expired(o):
                    orders.pop(star)
                    changed = True
                    from . import metrics
                    await metrics.note(self.db, "expired")
                    await self.log("wards", f"{star}: {label} order dropped — none got there in {wo.EXPIRE_DAYS} days",
                                   "alert", notify=True)
                    continue
                if not here and o.get("how") == "print" and o.get("factory"):
                    swap = await self._ward_print_to_spare(star, o, devices, busy, kind)
                    if swap:
                        changed = True
                        out.append(swap)
                        continue
                if (here and here["device_code"] not in busy
                        and "activate" in (here.get("available_commands") or ["activate"])
                        and (kind != "ward" or wh.wards(devices)["active"] < wh.WARD_CAP)
                        and (kind != "relay" or is_lagrange(here.get("location")))):
                    await self.create_job("wards", f"activate {label} {here['device_code']} in {star}", here["device_code"],
                                          [step(f"activate {label} {here['device_code']}", f"/devices/{here['device_code']}",
                                                {"command": "activate"})], {"devices": [here["device_code"]]}, force=True)
                    busy.add(here["device_code"])
                    out.append(f"{star}: activating {here['device_code']}")
            if changed:
                await self.db.kv_set(wo.kv_key(kind), orders)
        return out

    async def relay_rollout_pass(self) -> list[str]:
        """Relay rollout (relayroll.py): keep as many relay orders open as the logistics vessels with a replicant have
        free hold slots (relayroll.capacity), nearest the seed systems first; a finished order counts as done, a
        dropped one is skipped until retried."""
        from . import relaynet
        from . import relayroll as rr
        from . import ward_orders as wo
        from .shapes import normalize_blueprints
        st = rr.state(await self.db.kv_get(rr.KV, {}))
        if not st["on"] and not st["ordered"]:
            return []
        devices = await self.devices()
        cat = await self.db.kv_get("stars", {}) or {}
        pos = {x.get("designation"): x.get("position") for x in cat.get("stars") or [] if isinstance(x, dict) and x.get("position")}
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        have = set(relaynet.nodes(devices, pos, bps))
        orders = await self.db.kv_get(wo.kv_key("relay"), {}) or {}
        out: list[str] = []
        for star in list(st["ordered"]):   # orders the rollout made that are over: up, or dropped
            if star in orders:
                continue
            st["ordered"].pop(star)
            if star in have:
                st["done"] += 1
            else:
                st["skipped"][star] = "the order was dropped: nothing got there in time"
                out.append(f"{star}: skipped (the relay order was dropped)")
        from . import fleets as fl
        from .loadouts import _free
        ltags = {fl.fleet_tag(f["id"]) for f in await self.fleets() if f.get("role") == "logistics"}
        cap = rr.capacity(devices, ltags, lambda d: _free(d, bps, {}))
        st["capacity"] = cap
        while st["on"] and len(st["ordered"]) < max(1, cap):
            nxt = rr.queue(st["seeds"], pos, have, set(orders) | set(st["ordered"]), set(st["skipped"]),
                           float(st["radius"]), bool(st["chained"]), limit=1)
            if not nxt:
                if not st["ordered"]:
                    st["on"] = False
                    await self.log("wards", f"relay rollout finished: {st['done']} relays up, {len(st['skipped'])} systems "
                                   "skipped", notify=True)
                break
            star = nxt[0]["star"]
            msg = await self.order_outpost("relay", star, st["by"] or "relay rollout")
            orders = await self.db.kv_get(wo.kv_key("relay"), {}) or {}
            if star in orders:
                st["ordered"][star] = orders[star].get("at")
                st["streak"] = 0
                out.append(f"{star}: {msg}")
                continue
            st["skipped"][star] = msg
            out.append(f"{star}: skipped ({msg})")
            st["streak"] = st.get("streak", 0) + 1
            if st["streak"] >= rr.MAX_STREAK:   # nothing works anywhere (no spare, no factory): stop rather than skip all
                st["on"] = False
                await self.log("wards", f"relay rollout paused: the last {st['streak']} systems couldn't get a relay "
                               f"(last: {msg})", "alert", notify=True)
            if len(out) >= rr.MAX_STREAK:
                break
        await self.db.kv_set(rr.KV, st)
        return out

    async def _beacon_to_civ(self, star: str, civ: list[str], devices: list[dict], busy: set[str],
                             keep: list[str] | None = None) -> str | None:
        """A requested beacon has reached a system with a local civilization: a vessel there carries a beacon of yours
        to the first civilization body in `civ` and deploys it (traffic.placement: deploy / carry / fetch); beacons at
        `keep` (every civilization body) stay. Never prints another."""
        from . import traffic as tr
        jobs = await self.jobs()
        if any(j.get("meta", {}).get("beacon_loc") in civ for j in jobs if j["status"] in ("running", "waiting")):
            return None
        # a failed try waits 30 min before the next one (live 2026-10-23: the same failing stow every 90 s)
        fails = [j for j in jobs if j["rule"] == "wards" and j["status"] == "failed"
                 and j.get("meta", {}).get("beacon_loc") == civ[0] and _ts(j.get("created_at"))
                 and (_now() - _ts(j["created_at"])).total_seconds() < 1800]
        if fails:
            err = next((x.get("error") for x in max(fails, key=lambda j: j["created_at"])["steps"] if x.get("status") == "failed"), None)
            return f"beacon to the civilization at {civ[0]}: last try failed ({err or 'see Jobs'}), retrying in 30 min"
        p = tr.placement(civ[0], devices, await self.db.kv_get("replicants", {}) or {},
                         await self.db.kv_get("stowed_map", {}) or {}, busy, use_replicant_vessel=True,
                         keep=set(keep or civ))
        if p["kind"] not in ("deploy", "carry", "fetch"):
            return f"beacon there, waiting to move it to the civilization at {civ[0]}: {p['text']}"
        devs = [x for x in (p.get("vessel"), p.get("beacon")) if x]
        job = await self.create_job("wards", f"beacon to the civilization at {civ[0]}: {p['text']}",
                                    p.get("vessel") or p.get("beacon"), tr.placement_steps(p, civ[0]),
                                    {"devices": devs, "beacon_loc": civ[0]}, force=True)
        if job:
            busy.update(devs)
        return f"moving a beacon to the civilization at {civ[0]}: {p['text']}"

    async def _ward_print_to_spare(self, star: str, o: dict, devices: list[dict], busy: set[str],
                                   kind: str = "ward") -> str | None:
        """A print still waiting in its factory's queue gives way to a spare of the same kind in reach: dequeue the
        print, send the spare. Updates the order in place."""
        from . import loadouts as lo
        from . import ward_orders as wo
        from .automations import step
        dtype, label = wo.KINDS[kind], wo.LABELS[kind]
        f = next((d for d in devices if d.get("device_code") == o["factory"]), None)
        idx = wo.queued_at(f, star, dtype) if f and f["device_code"] not in busy else None
        if idx is None:
            return None
        pos, reach = await self._outpost_ctx()
        spare = wo.spare_ward(devices, star, busy, pos, reach, dtype)
        if not spare:
            return None
        rem = [t for t in spare.get("tags") or [] if t in (lo.SPARE, lo.STOCK) or t.startswith(("home:", "at:"))]
        await self.create_job("wards", f"{label} {spare['device_code']} → {star} (instead of the print on {f['device_code']})",
                              spare["device_code"],
                              [step(f"{f['device_code']}: drop the queued {label} for {star}", f"/devices/{f['device_code']}",
                                    {"command": "dequeue_print", "index": idx}),
                               lo.tag_step(spare["device_code"], [lo.to_tag(star)], rem or None)],
                              {"devices": [spare["device_code"], f["device_code"]]}, force=True)
        busy.update({spare["device_code"], f["device_code"]})
        o.update(how="send", device=spare["device_code"], factory=None)
        await self.log("wards", f"{star}: spare {label} {spare['device_code']} goes instead of the print queued on {f['device_code']}")
        return f"{star}: sending spare {spare['device_code']} instead of printing"

    async def remove_outpost(self, kind: str, star: str, user: str = "") -> str:
        """Remove your ward / relay / beacon from `star`: cancel its order (and a print not started yet) and deactivate
        yours there and mark it spare (owner 2026-10-22), so a later add elsewhere can send it."""
        from . import loadouts as lo
        from . import ward_orders as wo
        from . import wardhub as wh
        from .automations import step
        if kind not in wo.KINDS:
            return f"unknown kind {kind}"
        dtype, label = wo.KINDS[kind], wo.LABELS[kind]
        star = star.upper()
        orders = await self.db.kv_get(wo.kv_key(kind), {}) or {}
        order = orders.pop(star, None)
        if order is not None:
            await self.db.kv_set(wo.kv_key(kind), orders)
        devices = await self.devices()
        dropped = ""
        f = next((d for d in devices if d.get("device_code") == (order or {}).get("factory")), None)
        idx = wo.queued_at(f, star, dtype) if f and (order or {}).get("how") == "print" else None
        if idx is not None:   # a print not started yet goes too
            await self.create_job("wards", f"{f['device_code']}: drop the queued {label} for {star}", f["device_code"],
                                  [step(f"{f['device_code']}: drop the queued {label} for {star}", f"/devices/{f['device_code']}",
                                        {"command": "dequeue_print", "index": idx})], {"devices": [f["device_code"]]}, force=True)
            dropped = f", queued print on {f['device_code']} dropped"
        here = wo.ward_here(devices, star, dtype)
        if not here or not wh.active(here):
            return f"no active {label} of yours in {star}" + (f" (order canceled{dropped})" if order is not None else "")
        code = here["device_code"]
        tags = here.get("tags") or []
        rem = [t for t in tags if t.startswith(("home:", "at:", "to:"))]
        job = await self.create_job("wards", f"deactivate {label} {code} in {star} and mark it spare", code,
                                    [step(f"deactivate {label} {code}", f"/devices/{code}", {"command": "deactivate"}),
                                     lo.tag_step(code, [lo.SPARE], rem or None)], {"devices": [code]}, force=True)
        if job:
            await self.remember_tags(code, sorted((set(tags) - set(rem)) | {lo.SPARE}))
        await self.log("wards", f"{star}: {label} {code} deactivated and marked spare" + (f" (by {user})" if user else ""))
        return f"deactivating {code} in {star} and marking it spare (the loadout pass may send it where one is needed)"

    # --- civilization beacons -------------------------------------------------------------------------------------
    async def civ_coverage(self) -> list[dict]:
        from . import gameevents as ge
        from . import traffic as tr
        events = await ge.load(self.db)
        systems = tr.systems_from_rows(await self.db.fetchall("SELECT star, data FROM systems"))
        return tr.coverage(events, await self.devices(), systems)

    async def civ_beacon_pass(self, only_loc: str | None = None, manual: bool = False) -> list[str]:
        """Put a beacon at every event location (discovered or completed) that has none. One line per location."""
        from . import traffic as tr
        s = await self.settings()
        cfg = s["rules"].get("civ_beacons") or {}
        if not manual and not cfg.get("enabled"):
            return []
        if not manual:
            last = _ts(await self.db.kv_get("civ_beacons_at", None))
            if not only_loc and last and (_now() - last).total_seconds() < 600:
                return []
            if not only_loc:
                await self.db.kv_set("civ_beacons_at", now_iso())
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        pending = {j.get("meta", {}).get("beacon_loc") for j in jobs if j["status"] in ("running", "waiting")}
        wanted = await self.db.kv_get("beacon_wanted", {}) or {}
        wanted = {k: v for k, v in wanted.items() if _ts(v) and (_now() - _ts(v)).total_seconds() < 1800}
        orders = await self.db.kv_get("beacon_orders", {}) or {}
        orders = {k: v for k, v in orders.items() if _ts(v) and (_now() - _ts(v)).total_seconds() < 3 * 3600}
        devices = await self.devices()
        reps = await self.db.kv_get("replicants", {}) or {}
        stowed = await self.db.kv_get("stowed_map", {}) or {}
        from .shapes import normalize_blueprints
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        rows = await self.civ_coverage()
        keep = {r["location"] for r in rows}
        from . import fleets as fl
        logistics = {d["device_code"] for f in await self.fleets() if f.get("role") == "logistics" for d in fl.members(f, devices)}
        lines = []
        for row in rows:
            loc = row["location"]
            if only_loc and loc != only_loc:
                continue
            if not row["needs_beacon"] and not (manual and only_loc and not row["beacon"]):
                continue
            if loc in pending or loc in wanted:
                lines.append(f"{loc}: a beacon is already on its way")
                continue
            p = tr.placement(loc, devices, reps, stowed, busy, use_replicant_vessel=manual, logistics=logistics,
                             keep=keep, bps=bps, vessel_print=manual)
            if p["kind"] == "factory" and loc in orders:
                lines.append(f"{loc}: a beacon is being printed for it — a vessel picks it up once it's out"
                             + ("" if p.get("has_vessel") else f" (needs a vessel with a hold in {row['star']})"))
                continue
            if p["kind"] in ("none", "wait") or (p["kind"] == "factory" and not (manual or cfg.get("print_beacons", True))):
                lines.append(f"{loc}: {p['text']}")
                continue
            devs = [x for x in (p.get("vessel"), p.get("beacon")) if x]
            job = await self.create_job("civ_beacons", f"beacon at {loc}: {p['text']}", p.get("vessel") or p.get("beacon") or p.get("factory"),
                                        tr.placement_steps(p, loc), {"devices": devs, "beacon_loc": loc if p["kind"] != "factory" else None},
                                        force=manual)
            if job:
                busy.update(devs)
                if p["kind"] == "print":
                    wanted[loc] = now_iso()
                if p["kind"] == "factory":
                    orders[loc] = now_iso()
            lines.append(f"{loc}: {p['text']}" + ("" if job else " (dry run)"))
        await self.db.kv_set("beacon_wanted", wanted)
        await self.db.kv_set("beacon_orders", orders)
        if not only_loc and (manual or cfg.get("spare_redundant", True)):
            lines += await self.spare_redundant_beacons(rows, manual)
        return lines

    async def spare_redundant_beacons(self, rows: list[dict] | None = None, manual: bool = False) -> list[str]:
        """Tag beacons a system doesn't need as spare (the loadouts pass then gathers them at the spare depot)."""
        from . import traffic as tr
        from .loadouts import tag_step
        rows = rows if rows is not None else await self.civ_coverage()
        devices = await self.devices()
        by = {d.get("device_code"): d for d in devices}
        lines = []
        for r in tr.redundant_beacons(devices, rows, self.busy_devices(await self.jobs())):
            d = by.get(r["code"]) or {}
            homes = [t for t in d.get("tags") or [] if t.startswith(("home:", "at:"))]
            job = await self.create_job("civ_beacons", f"beacon {r['code']} ({r['location']}) is redundant: spare", r["code"],
                                        [tag_step(r["code"], ["spare"], homes or None)], {"devices": [r["code"]]}, force=manual)
            if job:
                await self.remember_tags(r["code"], sorted((set(d.get("tags") or []) - set(homes)) | {"spare"}))
            lines.append(f"{r['code']} at {r['location']} marked spare — {r['why']}" + ("" if job else " (dry run)"))
        return lines

    async def civ_on_event(self, ev: dict) -> None:
        name = ev.get("event") or ""
        p = ev.get("payload") or {}
        if name in ("event.discovered", "event.completed") and (p.get("location") or ev.get("location")):
            try:
                for line in await self.civ_beacon_pass(only_loc=p.get("location") or ev.get("location")):
                    await self.log("civ_beacons", line)
            except Exception as e:  # noqa: BLE001
                log.exception("civ beacons failed")
                await self.log("civ_beacons", f"failed: {e}", "alert")
        if name == "print.completed" and "beacon" in str(p.get("device_type") or "") and p.get("new_device_code"):
            wanted = await self.db.kv_get("beacon_wanted", {}) or {}
            loc = ev.get("location")
            if not loc:
                printer = next((d for d in await self.devices() if d.get("device_code") == ev.get("device_code")), None)
                reps = await self.db.kv_get("replicants", {}) or {}
                rep = next((r for r in reps.values() if r.get("hosted_device_code") == ev.get("device_code")), None)
                loc = (printer or {}).get("location") or (rep or {}).get("location")
            if loc in wanted:
                new = p["new_device_code"]
                wanted.pop(loc, None)
                await self.db.kv_set("beacon_wanted", wanted)
                from .automations import step
                await self.create_job("civ_beacons", f"deploy new beacon {new} at {loc}", new,
                                      [step(f"deploy beacon {new} at {loc}", f"/devices/{new}", {"command": "deploy"})],
                                      {"devices": [new], "beacon_loc": loc})

    async def sync_traffic(self) -> dict:
        from . import traffic as tr
        s = await self.settings()
        cfg = s["rules"].get("visitor_alerts") or {}
        out = await tr.sync(self.db, self.api, self.hub, cfg if cfg.get("enabled") else None)
        for a in out["alerts"]:
            await self.log("visitor_alerts", a)
        from . import others
        state = await self.db.kv_get("traffic", {}) or {}
        mine = tr.my_codes(await self.db.kv_get("account", {}) or {}, await self.db.kv_get("replicants", {}) or {},
                           await self.db.kv_get("devices", []) or [])
        out["others"] = await others.on_traffic_poll(self.db, self.api, state.get("entries") or [], mine)
        if self.hub and (out["new"] or out["others"]["scanned"] or out["others"]["removed"]):
            self.hub.publish("state", "traffic")    # the Galaxy map redraws its arrows / other players
        return out

    # --- asteroid defense -----------------------------------------------------------------------------------------
    async def diverted_pct(self) -> float:
        """The asteroid defense rule's "diverted enough" impact likelihood (%)."""
        from .evacuate import DIVERTED_PCT
        try:
            return float(((await self.settings())["rules"].get("asteroid_defence") or {}).get("diverted_pct", DIVERTED_PCT))
        except (TypeError, ValueError):
            return DIVERTED_PCT

    async def objects_interval(self, base: int = 900) -> int:
        """Seconds to the next read of the incoming objects: the rule's recheck interval while propulsors push one
        (its likelihood is watched for "diverted enough"), else POLL_OBJECTS."""
        objs = await self.db.kv_get("objects", {}) or {}
        pushing = any(not o.get("closed") and int(float(o.get("active_propulsors") or 0)) > 0 for o in objs.values())
        if not pushing:
            return base
        try:
            m = int(((await self.settings())["rules"].get("asteroid_defence") or {}).get("recheck_minutes") or 5)
        except (TypeError, ValueError):
            m = 5
        return max(60, min(base, m * 60))

    async def defence_objects(self) -> dict[str, dict]:
        """Tracked objects, after picking up detections and closures from the event log."""
        import json as _json
        from . import defence as dfn
        objs = await self.db.kv_get("objects", {}) or {}
        rows = await self.db.fetchall("SELECT event, payload, location, created_at FROM events WHERE event='system.object_detected' "
                                      "OR event LIKE 'diversion.%' OR event IN ('system.object_diverted', 'system.object_impacted', "
                                      "'system.impact_warning', 'system.object_off_target') ORDER BY seq")
        for r in rows:
            p = _json.loads(r["payload"] or "{}")
            # v3.5.0 events carry no designation: the envelope's location is the object (docs catalogue)
            des = p.get("object_designation") or p.get("designation") or r["location"]
            if not des:
                continue
            if r["event"] == "system.object_detected":
                old = objs.get(des)
                if old is None or (old.get("closed") and _after(r["created_at"], old.get("closed_at"))):
                    # designations are reused (live 2026-10-22: FALQUORYX-OBJ-2 impacted on 10-13, a new one came 10-21)
                    objs[des] = {**dfn.from_detected(p, des), "detected_at": r["created_at"]}
            elif r["event"] == "system.impact_warning":
                # {destination, impact_eta, device_codes}: one hour out, to accounts with devices at risk — it may be the
                # first we hear of it (no hub or beacon in the system saw it detected). Filed under the asteroid's own
                # location designation; where the warned devices are is kept as `also_at` (evacuated as well)
                obj = dfn.warning_object(p, r["location"], objs)
                at = r["location"] if r["location"] and r["location"] != obj else None
                if not obj:   # not known yet: under the warned location until a scan names it (sync_objects)
                    obj = r["location"]
                if at and at in objs and obj != at and objs[at].get("source") == "impact_warning":
                    objs.pop(at)   # filed under the warned location before it was resolved
                des = obj
                o = objs.setdefault(des, {"designation": des, "status": "active", "source": "impact_warning", "history": [],
                                          "detected_at": r["created_at"]})
                if at and at != des:
                    o["also_at"] = sorted(set(o.get("also_at") or []) | {at})
                if not dfn.is_object(des):
                    o["unresolved"] = True
                o["warned_devices"] = p.get("device_codes") or o.get("warned_devices") or []
                o["impact_eta"] = p.get("impact_eta") or o.get("impact_eta")
                o["impact_target"] = o.get("impact_target") or p.get("destination")
                o["destination"] = p.get("destination") or o.get("destination")
                o["warned_at"] = r["created_at"]
            elif r["event"] == "system.object_off_target":   # v3.6.0: it will miss; minable until flung out at the ETA
                if des in objs and not objs[des].get("closed"):
                    objs[des]["status"] = dfn.OFF_TARGET
            elif r["event"] in dfn.CLOSED_EVENTS:
                if des not in objs:   # matched by its target when the location isn't the object's
                    des = next((k for k, o in objs.items() if not o.get("closed") and p.get("impact_target")
                                and o.get("impact_target") == p.get("impact_target")), des)
                if des in objs and p.get("outcome") != "partial" \
                        and not _after(_born(objs[des]), r["created_at"]):   # partial: still a threat; older: a former rock
                    objs[des]["closed"] = dfn.closed_as(r["event"])
                    objs[des].setdefault("closed_at", r["created_at"])
        for o in objs.values():   # closed by a former rock's event before 1.209.0 (FALQUORYX-OBJ-2, 2026-10-22)
            if o.get("closed") in ("diverted", "impacted") and _after(_born(o), o.get("closed_at")):
                o.pop("closed")
                o.pop("closed_at", None)
        mine = await self.db.kv_get("launched_rocks", {}) or {}   # rocks you launched (Terraforming): not a threat
        for k, o in objs.items():
            if k in mine:
                o["launched_by_you"] = True
                o.setdefault("impact_target", mine[k].get("destination"))
                o.setdefault("impact_eta", mine[k].get("impact_eta"))
        for k, o in objs.items():   # saved before 1.99 without a designation (v3.5.0 detections): its location is its name
            if not o.get("designation"):
                o["designation"] = k
        # owner, 2026-10-15: an event is over once its impact time has passed — no more reads, alerts or evacuations
        for o in objs.values():
            eta = _ts(o.get("impact_eta"))
            if not o.get("closed") and eta and eta <= _now():
                o["closed"] = "concluded (impact time passed)"
                o["closed_at"] = o.get("impact_eta")
        return objs

    async def defence_report(self) -> list[dict]:
        from . import defence as dfn
        from .shapes import normalize_blueprints
        objs = await self.defence_objects()
        devices = await self.devices()
        bps = normalize_blueprints(await self.db.kv_get("blueprints", []))
        enroute = await self._defence_enroute()
        pct = await self.diverted_pct()
        out = [dfn.assess(o, devices, bps, enroute=enroute.get(d, 0), pct=pct) for d, o in objs.items()]
        return sorted(out, key=lambda a: (not a["threat"], a["hours_left"] if a["hours_left"] is not None else 1e9))

    async def _defence_enroute(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for j in await self.jobs():
            des = j.get("meta", {}).get("defence")
            fin = _ts(j.get("finished_at"))
            recent = j["status"] == "done" and fin and (_now() - fin).total_seconds() < 300   # until the device list catches up
            if des and (j["status"] in ("running", "waiting") or recent):
                out[des] = out.get(des, 0) + len(j.get("meta", {}).get("devices") or [])
        orders = await self.db.kv_get("defence_orders", {}) or {}
        for des, items in orders.items():
            out[des] = out.get(des, 0) + sum(int(i.get("n") or 0) for i in items
                                             if _ts(i.get("at")) and (_now() - _ts(i["at"])).total_seconds() < 3 * 3600)
        return out

    async def sync_objects(self, manual: bool = False, only: str | None = None) -> dict:
        """Read every active incoming object, alert on changes, and (rule on) act."""
        from . import defence as dfn
        from .api import ApiError
        from .shapes import normalize_blueprints
        objs = await self.defence_objects()
        read = 0
        for des, o in list(objs.items()):   # a warning not matched to an object yet: the system's scan names it
            if o.get("unresolved") and not o.get("closed") and not (only and des != only):
                star = des.split("-")[0]
                try:
                    found = dfn.objects_in_scan(await self.api.request("GET", f"/locations/{star}", background=True) or {}, star)
                except ApiError:
                    continue
                if found:
                    new = str(found[0].get("designation") or found[0].get("code") or found[0].get("location")).upper()
                    if new not in objs:
                        objs[new] = {**o, "designation": new, "also_at": sorted(set(o.get("also_at") or []) | {des})}
                        objs[new].pop("unresolved", None)
                    objs.pop(des)
        for des, o in objs.items():
            if o.get("closed") or (only and des != only):
                continue
            eta = _ts(o.get("impact_eta"))
            try:
                body = await self.api.request("GET", f"/locations/{des}", background=True) or {}
            except ApiError as e:
                if e.status == 404:
                    o["closed"] = "gone"
                else:
                    o["read_error"] = e.message
                continue
            reading = body.get("object") if isinstance(body.get("object"), dict) else None
            if reading:
                objs[des] = dfn.merge_reading(o, reading, now_iso())
                objs[des].pop("read_error", None)
                if isinstance(body.get("resource_sites"), list):   # v3.6.0: asteroids are minable, % left per resource
                    objs[des]["resource_sites"] = body["resource_sites"]
                read += 1
        await self.db.kv_set("objects", objs)
        s = await self.settings()
        cfg = s["rules"].get("asteroid_defence") or {}
        alerted = await self.db.kv_get("objects_alerted", {}) or {}
        lines: list[str] = []
        devices = await self.devices()
        bps = normalize_blueprints(await self.db.kv_get("blueprints", []))
        enroute = await self._defence_enroute()
        pct = await self.diverted_pct()
        for des, o in objs.items():
            a = dfn.assess(o, devices, bps, enroute=enroute.get(des, 0), pct=pct)
            key = a["verdict"].split(" —")[0]
            if (cfg.get("enabled") or manual) and alerted.get(des) != key:
                alerted[des] = key
                if a["threat"] or key in ("diverted", "diverted enough", "too late", "impacted"):
                    from .traffic import notify
                    eta = f", impact in {a['hours_left']:.1f} h" if (a["hours_left"] or 0) > 0 and "impact in" not in a["verdict"] else ""
                    need = f", needs ~{a['needed']} propulsor(s), have {a['active']}" if a["needed"] is not None and a["threat"] else ""
                    await notify(self.db, self.hub, f"Asteroid {des} → {a['target'] or '?'}: {a['verdict']}{eta}{need}",
                                 "alert" if a["threat"] else "done", "/defence")
            if not a["threat"] or not (cfg.get("enabled") or manual):
                continue
            jobs_now = await self.jobs()
            busy = self.busy_devices(jobs_now) | {c for j in jobs_now if j.get("meta", {}).get("defence") and j["status"] == "done"
                                                   and _ts(j.get("finished_at")) and (_now() - _ts(j["finished_at"])).total_seconds() < 300
                                                   for c in j.get("meta", {}).get("devices") or []}
            # `a` already counts propulsors on their way (jobs + recent prints), so its shortfall is what's left
            p = dfn.plan(a, devices, bps, busy, {**cfg, **({"print_missing": True} if manual else {})})
            for note in p["notes"]:
                lines.append(f"{des}: {note}")
            for title, steps, devs in dfn.plan_steps(a, p):
                job = await self.create_job("asteroid_defence", title, devs[0] if devs else None, steps,
                                            {"devices": devs, "defence": des}, force=manual)
                lines.append(title + ("" if job else " (dry run)"))
                if job and p.get("print") and "print" in title:
                    orders = await self.db.kv_get("defence_orders", {}) or {}
                    orders.setdefault(des, []).append({"n": p["print"]["n"], "at": now_iso()})
                    await self.db.kv_set("defence_orders", orders)
        await self.db.kv_set("objects_alerted", alerted)
        for line in lines:
            await self.log("asteroid_defence", line)
        from . import evacuate as ev
        if any(ev.diverted_enough(o, pct) for o in objs.values() if not o.get("closed")):
            lines += await self.evacuate_pass()   # recall the propulsors now, not at the next tick
        return {"read": read, "lines": lines}

    async def poll_objects(self) -> dict:
        async with self.lock:
            return await self.sync_objects()

    async def _locked_sync_objects(self) -> None:
        try:
            async with self.lock:
                await self.sync_objects()
        except Exception:  # noqa: BLE001
            log.exception("asteroid sync failed")

    # --- asteroid mining (game v3.6.0) --------------------------------------------------------------------------
    async def rock_scans(self) -> dict[str, dict]:
        """{rock: {at, amounts}} from the latest survey scan of each asteroid (scan.completed)."""
        import json as _json
        from . import rockmining as rm
        from .defence import is_object
        out: dict[str, dict] = {}
        for r in await self.db.fetchall("SELECT payload, location, created_at FROM events WHERE event='scan.completed' "
                                        "AND (location LIKE '%-OBJ-%' OR payload LIKE '%-OBJ-%') ORDER BY seq"):
            p = _json.loads(r["payload"] or "{}")
            rock = str(p.get("scan_target") or p.get("target") or r["location"] or "").upper()
            if is_object(rock):
                out[rock] = {"at": r["created_at"], "amounts": rm.amounts(p.get("report") or p)}
        return out

    async def rock_mining_pass(self) -> list[str]:
        """See rockmining.py. Returns what it did."""
        from . import rockmining as rm
        from .automations import step
        from .loadouts import at_tag, tag_step
        cfg = await self.rule_cfg("asteroid_mining")
        if not cfg:
            return []
        objs = await self.defence_objects()
        state = await self.db.kv_get("rock_mining", {}) or {}
        flying = rm.open_rocks(objs)
        state = {c: s for c, s in state.items() if s.get("rock") in flying}
        full = {r: t for r, t in (await self.db.kv_get("rock_full", {}) or {}).items()
                if _ts(t) and (_now() - _ts(t)).total_seconds() < 3600}
        devices = await self.devices()
        reps = await self.db.kv_get("replicants", {}) or {}
        hosts = {r.get("hosted_device_code") for r in reps.values() if r.get("hosted_device_code")}
        busy = self.busy_devices(await self.jobs())
        where = {d["device_code"]: d.get("location") for d in devices}
        state = {c: s for c, s in state.items() if where.get(c) == s.get("rock") or c in busy}   # evacuated, or the trip failed
        scans = await self.rock_scans()
        p = rm.plan(objs, devices, cfg, busy, state, hosts, set(full), {r: s["at"] for r, s in scans.items()})
        dry = (await self.settings())["dry_run"]
        out: list[str] = []
        for kind, body in (("start", "start_mining"), ("retarget", "retarget"), ("stop", "deactivate")):
            for x in p[kind]:
                code, rock, res = x["code"], x["rock"], x["resource"]
                what = f"{code} at {rock}: " + (f"{kind} {res}" if res else "stop (mined down to what is kept)")
                if dry:
                    out.append("[dry run] " + what)
                    continue
                cmd = {"command": body, **({"resource_type": res} if res else {})}
                ok, _, err = await self.send("POST", f"/devices/{code}", cmd, f"auto: asteroid mining {code}")
                if ok:
                    if res:
                        state[code] = {"rock": rock, "resource": res, "at": now_iso()}
                    else:
                        state.pop(code, None)
                    out.append(what)
                else:
                    if any(w in (err or "").lower() for w in ("slot", "full", "no room", "capacity")):
                        full[rock] = now_iso()
                    out.append(f"{what} failed: {err}")
        for x in p["send"]:
            code, rock, res = x["code"], x["rock"], x["resource"]
            from .hubroute import total_seconds
            try:   # owner 2026-10-22: the actual travel time counts, not just the hours left
                secs = total_seconds(await self.api.request("POST", f"/devices/{code}", json_body={
                    "command": "travel", "destination": rock, "dry_run": True}, background=True))
            except Exception:  # noqa: BLE001  (no preview: the hours-left filter alone)
                secs = None
            if not rm.trip_ok(objs[rock], secs, cfg):
                note = f"{code} not sent to {rock}: {secs / 3600:.1f} h trip leaves under {cfg.get('min_hours') or 1} h of mining"
                if self.stage_errors.get(f"rock-trip:{code}:{rock}") != note:
                    self.stage_errors[f"rock-trip:{code}:{rock}"] = note
                    await self.log("asteroid_mining", note)
                continue
            tr = step(f"{code}: travel → {rock}", f"/devices/{code}", {"command": "travel", "destination": rock},
                      wait=["travel.arrived"], critical=True, match={"destination": rock})
            tr["wait_device"] = code
            steps = [tag_step(code, add=[at_tag(rock)]), tr,
                     step(f"{code}: mine {res} at {rock}", f"/devices/{code}", {"command": "start_mining", "resource_type": res})]
            if await self.create_job("asteroid_mining", f"asteroid mining: {code} → {rock} ({res})", code, steps, {"devices": [code]}):
                state[code] = {"rock": rock, "resource": res, "at": now_iso(), "sent": True}
                out.append(f"{code} → {rock} to mine {res}")
        for x in p["scan"]:
            code, rock = x["code"], x["rock"]
            tr = step(f"{code}: travel → {rock}", f"/devices/{code}", {"command": "travel", "destination": rock},
                      wait=["travel.arrived"], critical=True, match={"destination": rock})
            tr["wait_device"] = code
            sc = step(f"{code}: scan {rock}", f"/devices/{code}", {"command": "scan"}, wait=["scan.completed"], timeout=3600)
            sc["wait_device"] = code
            steps = [sc] if x.get("here") else [tr, sc]
            if x.get("stop"):   # searching a belt for a new site: called off first
                steps.insert(0, step(f"{code}: stop searching", f"/devices/{code}", {"command": "deactivate"}))
            if await self.create_job("asteroid_mining", f"asteroid mining: {code} scans {rock}", code, steps, {"devices": [code]}):
                out.append(f"{code} scans {rock}" if x.get("here") else f"{code} → {rock} to stay and scan it")
        await self.db.kv_set("rock_mining", state)
        await self.db.kv_set("rock_full", full)
        for note in p["notes"]:
            key = f"rock-note:{note}"
            if self.stage_errors.get(key) != note:
                self.stage_errors[key] = note
                await self.log("asteroid_mining", note, "alert")
        for line in out:
            await self.log("asteroid_mining", line)
        return out

    # --- maintenance --------------------------------------------------------------------------------------------
    async def maintenance_pass(self, manual: bool = False, star: str | None = None) -> list[str]:
        from . import printqueue as pq, upkeep as up
        from .automations import step
        from .shapes import normalize_blueprints
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        s = await self.settings()
        cfg = s["rules"].get("maintenance") or {}
        if not manual:
            if not cfg.get("enabled"):
                return []
            last = _ts(await self.db.kv_get("maintenance_at", None))
            if last and (_now() - last).total_seconds() < 60 * max(1, int(cfg.get("every_minutes") or 15)):
                return []
            await self.db.kv_set("maintenance_at", now_iso())
        devices = await self.devices()
        busy = self.busy_devices(await self.jobs())
        lines = []
        for d in up.to_patrol(devices, busy):
            if star and up.star_of(d.get("location")) != star:
                continue
            code = d["device_code"]
            job = await self.create_job("maintenance", f"maintenance: {code} → patrol ({d.get('location')})", code,
                                        [step(f"{code}: set_directive patrol", f"/devices/{code}",
                                              {"command": "set_directive", "directive": "patrol"})], {"devices": [code]},
                                        force=manual)
            lines.append(f"{code} at {d.get('location')} set to patrol" + ("" if job else " (dry run)"))
        rep = up.report(devices, float(cfg.get("threshold") or 80))
        noted = await self.db.kv_get("maintenance_noted", {}) or {}
        orders = await self.db.kv_get("maintenance_orders", {}) or {}
        for r in rep:
            if r["verdict"] != "no maintenance drone" or (star and r["star"] != star):
                continue
            worst = r["damaged"][0]
            if worst["capacity"] >= float(cfg.get("critical") or 60) and not manual:
                continue
            o = _ts(orders.get(r["star"]))
            if o and (_now() - o).total_seconds() < 6 * 3600:
                continue
            fac = pq.least_loaded([f for f in devices if "enqueue_print" in (f.get("available_commands") or [])
                                   and "vessel" not in (f.get("device_type") or "")   # never on a (replicant's) vessel
                                   and up.star_of(f.get("location")) == r["star"]], bps)
            if fac and (cfg.get("print_missing") or manual):
                job = await self.create_job("maintenance", f"maintenance: print a maintenance drone for {r['star']}", fac["device_code"],
                                            [step(f"{fac['device_code']}: print maintenance_drone for {r['star']}",
                                                  f"/devices/{fac['device_code']}",
                                                  {"command": "enqueue_print", "device_type": "maintenance_drone", "quantity": 1,
                                                   "tags": []})], {"devices": []}, force=manual)
                if job:
                    orders[r["star"]] = now_iso()
                lines.append(f"{r['star']}: printing a maintenance drone on {fac['device_code']} "
                             f"(worst {worst['code']} at {worst['capacity']} %)")
            else:
                last = _ts(noted.get(r["star"]))
                if not last or (_now() - last).total_seconds() > 24 * 3600:
                    noted[r["star"]] = now_iso()
                    text = (f"{r['star']}: {len(r['damaged'])} device(s) wearing down (worst {worst['code']} {worst['capacity']} %) "
                            "and no maintenance drone there" + ("" if fac else " — and no autofactory to print one"))
                    from .traffic import notify
                    await notify(self.db, self.hub, text, "alert", "/maintenance")
                    lines.append(text)
        await self.db.kv_set("maintenance_noted", noted)
        await self.db.kv_set("maintenance_orders", orders)
        for line in lines:
            await self.log("maintenance", line)
        return lines
