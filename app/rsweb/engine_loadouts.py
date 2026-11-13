"""Loadouts: fleet templates, the plan and its print, fetch and delivery jobs.

A part of AutomationEngine (automations.py), split out by area; the methods are unchanged."""
from __future__ import annotations

from .engine_base import *  # noqa: F401,F403


class LoadoutMixin:
    # --- loadouts --------------------------------------------------------------------------------------
    async def loadout_cfg(self) -> dict:
        """Templates, settings and ignore tags (kv "loadouts"), plus `fleets`: every fleet with its template resolved.
        The first call after an upgrade turns the old home fleets and materials roles into fleets (fleets.migrate)."""
        from . import fleets as fl
        from .ami_schedule import set_stationed
        from .loadouts import normalize
        raw = await self.db.kv_get("loadouts", {}) or {}
        items = await self.db.kv_get("fleets", []) or []
        if not raw.get("fleets_migrated"):
            cat = await self.db.kv_get("stars", {}) or {}
            pos = {x.get("designation"): x.get("position") or {} for x in (cat.get("stars") or []) if isinstance(x, dict)}
            had = {f.get("id") for f in items}
            raw, items, changed = fl.migrate(raw, items, pos)
            if changed:
                await self.db.kv_set("fleets", items)
                await self.db.kv_set("loadouts", raw)
                for f in items:
                    if f["id"] not in had:
                        await self.log("fleets", f"home fleet of {f['home']} is now the stationed fleet {f['name']} "
                                                 f"(fleet:{f['id']}); its devices get the fleet tag on the next loadout pass")
        cfg = normalize(raw)
        cfg["fleets"] = [fl.resolve_template(dict(f), cfg) for f in items]
        set_stationed({fl.fleet_tag(f["id"]): f["home"] for f in cfg["fleets"] if fl.stationed(f)})
        return cfg

    async def geography(self, devices: list[dict], stars: dict[str, dict]) -> dict[str, dict]:
        """Per system: belts, Lagrange points and inner planets (from its scan, the catalog and device positions)."""
        from . import placement as pl
        scans = {r["star"]: r["data"] for r in await self.db.fetchall("SELECT star, data FROM systems")}
        belts_seen = list((await self.db.kv_get("belt_reads", {}) or {}).keys())
        out = {}
        for star in {star_of(d.get("location")) for d in devices if d.get("location")}:
            try:
                scan = json.loads(scans[star]) if star in scans else None
            except ValueError:
                scan = None
            out[star] = pl.geography(star, devices, scan, (stars.get(star) or {}).get("entry_point"), belts_seen)
        return out

    async def cruise_radii(self) -> dict[str, float]:
        """Distance from its star (AU) of every place the stored system scans name — to tell long cruises from short."""
        from .fleets import system_radii
        gen = self.db.gen["systems"]
        memo = getattr(self, "_radii_memo", None)
        if memo and memo[0] is self.db and memo[1] == gen:   # every stored scan decoded once per change, not per call
            return dict(memo[2])
        out: dict[str, float] = {}
        for r in await self.db.fetchall("SELECT data FROM systems"):
            try:
                out.update(system_radii(json.loads(r["data"])))
            except ValueError:
                pass
        if self.db.gen["systems"] == gen:
            self._radii_memo = (self.db, gen, out)
        return dict(out)

    async def component_pass(self) -> list[str]:
        """Factories waiting for component devices (components.py): spares are pinned to the factory's location, the
        rest printed at another autofactory, with the waiting item's tags. Every 5 minutes."""
        from . import components as comp
        from .loadouts import pinned_at as pinned_at_
        last = _ts(await self.db.kv_get("components_last", None))
        if last and (_now() - last).total_seconds() < 300:
            return []
        await self.db.kv_set("components_last", now_iso())
        devices = await self.devices()
        out = []
        far = comp.far_pins(devices, await self.star_lookup("pos"))
        if far:   # pinned beyond the sector range: their held trips are dropped and they're spares again where they are
            codes = {d["device_code"] for d in far}
            for job in await self.jobs():
                held = any(st.get("status") == "confirm" for st in job.get("steps") or [])
                cargo = set((job.get("meta") or {}).get("devices") or []) | {job.get("device")}
                if job.get("status") in ("running", "waiting") and held and cargo & codes:
                    job["status"] = "cancelled"
                    job["finished_at"] = now_iso()
                    await self._update(job)
                    await self.log(job["rule"], f"canceled: {job['title']} (its components' factory is beyond the sector range)")
            busy = self.busy_devices(await self.jobs())
            n = 0
            for d in far:
                if d["device_code"] in busy:
                    continue
                cfg = comp.release(d)
                ok, _, _ = await self.send("PATCH", f"/devices/{d['device_code']}", {"configuration": cfg},
                                           f"components: {d['device_code']} released (factory beyond the sector range)")
                if ok:
                    n += 1
                    d["tags"] = [t for t in d.get("tags") or [] if t not in cfg["remove_tags"]] + ["spare"]
            if n:
                out.append(f"{n} component(s) pinned to a factory beyond the sector range are spares again")
                await self.log("components", out[-1])
        far_codes = {d["device_code"] for d in far}
        lost = [(d, cfg) for d, cfg in comp.unbound(devices) if d["device_code"] not in far_codes]
        for d, cfg in lost:   # pinned to a factory in another system but no longer bound there: sent again
            ok, _, _ = await self.send("PATCH", f"/devices/{d['device_code']}", {"configuration": cfg},
                                       f"components: {d['device_code']} sent on to {pinned_at_(d)}")
            if ok:
                d["tags"] = [t for t in d.get("tags") or [] if t not in cfg["remove_tags"]] + cfg["add_tags"]
        if lost:
            out.append(f"{len(lost)} component(s) pinned to a factory in another system were sent on again")
            await self.log("components", out[-1])
        claim = comp.claimable(devices)
        for d, tags in claim:   # already there but not tagged as its components (spare, stock, untagged): claimed
            cfg = comp.retag(d, tags)
            ok, _, _ = await self.send("PATCH", f"/devices/{d['device_code']}", {"configuration": cfg},
                                       f"components: {d['device_code']} kept at {d['location']}")
            if ok:
                d["tags"] = [t for t in d.get("tags") or [] if t not in cfg["remove_tags"] and t not in tags] + tags
        if claim:
            out.append(f"{len(claim)} device(s) at factories waiting for them tagged as components")
            await self.log("components", out[-1])
        if getattr(self.api, "vq", None) is not None:   # every queued print's components, not just the first one's
            from .shapes import normalize_blueprints
            bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
            lk = await self.db.kv_get("locked_prints", {}) or {}
            locked = {t for t, at in lk.items() if _ts(at) and (_now() - _ts(at)).total_seconds() < 6 * 3600}
            for g in comp.backfill(devices, {t: b for t, b in bps.items() if t not in locked}):
                ok, _, err = await self.send("POST", f"/devices/{g['factory']}",
                                             {"command": "enqueue_print", "device_type": g["type"], "quantity": g["short"],
                                              "tags": g["tags"]}, f"components: {g['short']} {g['type']} for {g['location']}")
                if ok:
                    out.append(f"{g['location']}: {g['short']} {g['type'].replace('_', ' ')} queued for the prints waiting there")
                    await self.log("components", out[-1])
            devices = await self.devices()
        gaps = comp.shortfalls(devices)
        for g in comp.shortfalls(devices, every=True):   # components queued there but behind the waiting item
            if g["short"] <= 0:
                ahead = await self.components_ahead(g, 0)
                if ahead:
                    out.append(f"{g['factory']} waits for {g['type'].replace('_', ' ')}: {ahead}")
                    await self.log("components", out[-1])
        if not gaps:
            return out
        busy = self.busy_devices(await self.jobs())
        blocked = {g["factory"] for g in gaps}
        for g in gaps:
            pinned, printed, note = await self.pin_or_print(g["type"], g["short"], g["location"], devices, busy,
                                                            f"components for {g['factory']}", tags=g["tags"], not_at=blocked)
            ahead = await self.components_ahead(g, g["short"] - len(pinned) - printed)
            what = ", ".join(x for x in [f"{len(pinned)} spare(s) sent" if pinned else "",
                                         f"{printed} queued to print" if printed else "", ahead] if x)
            if what:
                out.append(f"{g['factory']} needs {g['short']} {g['type'].replace('_', ' ')}: {what}")
                await self.log("components", out[-1])
            elif note:
                await self.log("components", f"{g['factory']} needs {g['short']} {g['type'].replace('_', ' ')}: {note}")
        return out

    async def vqueue_pass(self) -> list[str]:
        """The virtual print queue's feeder (vqueue.py): each factory's game queue holds the KEEP best waiting prints;
        a better one kept in the app takes a worse one's place, which is kept in the app again."""
        from . import printqueue as pq
        from . import vqueue as vqm
        vq = getattr(self.api, "vq", None)
        if vq is None:
            return []
        fed = getattr(self, "_vq_fed", None)
        if fed is None:
            fed = self._vq_fed = {}
        jumped = getattr(self, "_vq_jumped", None)
        if jumped is None:
            jumped = self._vq_jumped = {}
        out = []
        now = time.monotonic()
        devs = await self.devices()
        from . import loadouts as lom
        lom.CLASS_PRINTERS.update(await self.db.kv_get("class_printers", {}) or {})
        lom.EXOTIC_CLASS_ONLY[0] = bool(await self.db.kv_get("exotic_class_only", False))
        bpd = {b.get("device_type"): b for b in await self.db.kv_get("blueprints", []) or [] if isinstance(b, dict)}
        # the app's own prints kept for a printer that can't make them (an epitaxy printer: exotic blueprints only) are
        # dropped: the ward / relay / beacon orders then find them lost and print elsewhere; prints queued by hand stay
        printers = {d["device_code"] for d in devs if d.get("device_code") and lom.is_printer(d)}
        by_code = {d["device_code"]: d for d in devs if d.get("device_code") in printers}
        async with vq.lock:
            st = await vq.state()
            drop = [o for o in st["orders"] if o.get("factory") in by_code and o.get("prio") != 0
                    and not lom.can_make(by_code[o["factory"]], bpd.get((o.get("body") or {}).get("device_type")))]
            if drop:
                st["orders"] = [o for o in st["orders"] if not any(o is x for x in drop)]
                await self.db.kv_set(vqm.KV, st)
        for code in sorted({o["factory"] for o in drop}):
            n = sum(o["factory"] == code for o in drop)
            line = f"{code}: dropped {n} kept print(s) it can't make" + \
                (" (exotic blueprints only)" if lom.print_class(by_code[code]) else " (exotic: an epitaxy printer's)")
            out.append(line)
            await self.log("print queue", line, "warning")
        # factories of one type at one spot share their kept prints (owner 2026-11-03): each one's game queue is fed
        # from all of theirs, best first, so an idle factory takes over what waits behind a busy neighbor
        group: dict[tuple, list[str]] = {}
        for d in devs:
            if d.get("device_code") in printers and d.get("location"):
                group.setdefault((d["location"], d.get("device_type")), []).append(d["device_code"])
        for d in devs:
            code = d.get("device_code")
            if not code or code not in printers or not d.get("location") or now - fed.get(code, -1e9) < 150:
                continue
            kept = await vq.orders(codes=group.get((d["location"], d.get("device_type"))) or [code])
            real = [x for x in pq.items(d) if not x["extra"].get("virtual")]
            k, back, send = vqm.plan(real, kept, prios=vqm.real_prios(await vq.state(), code, real))
            waiting = str(d.get("status") or "").startswith("waiting_for_resources")
            if waiting and now - jumped.get(code, -1e9) < 600:
                continue   # a print just went ahead of the waiting one: the order by priority would undo it
            stuck = waiting
            if not send and not back and k == len(real) and not stuck:
                continue
            try:   # what the device list shows may be minutes old: the change is worked out on a fresh read
                dev = await self.api.request("GET", f"/devices/{code}", background=True, raw=True) or {}
            except ApiError:
                continue
            fed[code] = now
            real = pq.items(dev)
            if stuck and str(dev.get("status") or "").startswith("waiting_for_resources"):
                line = await self.vq_jump(vq, code, dev, real, kept)
                if line:
                    jumped[code] = now
                    out.append(line)
                    await self.log("print queue", line, "info")
                    continue
            async with vq.lock:
                st = await vq.state()
                prios = vqm.real_prios(st, code, real)
                had = (st.get("real") or {}).get(code)
                vqm.note_real(st, code, real, prios)   # forget hand moves that left the queue
                if (st.get("real") or {}).get(code) != had:
                    await self.db.kv_set(vqm.KV, st)
            k, back, send = vqm.plan(real, kept, prios=prios)
            if not send and not back and k == len(real):
                continue
            pr = {id(it): p for it, p in zip(real, prios)}
            async with vq.lock:
                st = await vq.state()
                final = [(it, pr[id(it)]) for it in real[:k]]
                low = min([o["seq"] for o in st["orders"]] + [st.get("low", 0), 0])   # back ones go ahead of the rest
                st["low"] = low - len(back) - 2
                removed = 0
                for _ in range(len(real) - k):
                    ok, _, err = await self.send("POST", f"/devices/{code}", {"command": "dequeue_print", "index": k},
                                                 f"print queue: {code} #{k + 1} back to the app's queue", raw=True)
                    if not ok:
                        break
                    removed += 1
                gone = {id(x) for x in real[k:k + removed]}
                for i, it in enumerate(x for x in back if id(x) in gone):
                    await vq.add(code, vqm.body_of(it), pr.get(id(it), vqm.priority(it)), s=st, seq=low - len(back) + i - 1)
                sent = 0
                for kind, x in send:
                    if kind == "real" and id(x) not in gone:
                        continue   # still in the game's queue (its dequeue failed)
                    body = vqm.body_of(x) if kind == "real" else vqm.send_body(x)
                    ok, _, err = await self.send("POST", f"/devices/{code}", body,
                                                 f"print queue: {code} gets {body.get('device_type')}", raw=True)
                    if not ok and not lom.print_class(d) and lom.is_class_bp(bpd.get(body.get("device_type"))) \
                            and re.search(r"epitaxy|exotic", str(err), re.I):
                        # a general printer refused an exotic print: from now on those go to epitaxy printers only
                        await self.db.kv_set("exotic_class_only", True)
                        lom.EXOTIC_CLASS_ONLY[0] = True
                    if not ok and lom.CLASS_RE.search(str(err)) and d.get("device_type"):
                        cp = await self.db.kv_get("class_printers", {}) or {}
                        cp.setdefault(str(d["device_type"]), str(err)[:200])
                        await self.db.kv_set("class_printers", cp)
                        lom.CLASS_PRINTERS.add(str(d["device_type"]))   # next pass drops what it still keeps
                        if kind == "kept":   # a type this printer can't make: dropped, never sent again
                            st["orders"] = vqm.take(st["orders"], x["factory"], x, 10 ** 9)
                            await self.log("print queue", f"{code}: dropped the {body.get('device_type')} print: {err}", "warning")
                            continue
                    if ok:
                        final.append((body, pr.get(id(x), vqm.priority(x)) if kind == "real" else x["prio"]))
                    if ok or (kind == "kept" and "not yet unlocked" in str(err).lower()):
                        sent += ok
                        if kind == "kept":
                            st["orders"] = vqm.take(st["orders"], x["factory"], x, int(body.get("quantity") or 1) if ok else 10 ** 9)
                        if not ok:   # the game won't print it: dropped, not offered again every pass
                            lk = await self.db.kv_get("locked_prints", {}) or {}
                            lk[str(body.get("device_type"))] = now_iso()
                            await self.db.kv_set("locked_prints", lk)
                            await self.log("print queue", f"{code}: dropped the {body.get('device_type')} print: {err}", "warning")
                    elif kind == "real":
                        await vq.add(code, body, pr.get(id(x), vqm.priority(x)), s=st, seq=low - len(back) - 1)
                vqm.note_real(st, code, [a for a, _ in final], [b for _, b in final])
                await self.db.kv_set(vqm.KV, st)
            line = f"{code}: {sent} print(s) into the game's queue" + (f", {len(back)} back to the app's queue" if back else "")
            out.append(line)
            await self.log("print queue", line, "info")
        return out

    async def vq_jump(self, vq: Any, code: str, dev: dict, real: list[dict], kept: list[dict]) -> str | None:
        """The factory waits for resources or components for the first print in its queue (owner 2026-11-01): a print
        that only uses what that one doesn't need (vqueue.jump) goes ahead of it. The waiting print stays first in the
        game's queue (live 2026-11-02: deactivate answered "Nothing to deactivate" on a waiting factory), so the game's
        queue goes back to the app's queue and is sent again: the one that fits first, the waiting one right behind."""
        from . import vqueue as vqm
        from .shapes import normalize_blueprints, normalize_inventory
        if not real:
            return None
        head = real[0]
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        from . import components as comp
        devs = await self.devices()
        kinds = {t for it in real[1:] + [o["body"] for o in kept] for t in comp.components(bps.get(it.get("device_type") or ""))}
        # deployed there now (queued prints of that type don't count: they aren't made yet), less the queue's own use
        free = {t: comp.free_here(devs, {"location": dev.get("location")}, t, {})
                - sum(comp.components(bps.get(it.get("device_type") or "")).get(t, 0) * max(1, int(it.get("quantity") or 1))
                      for it in real) for t in kinds}
        pick = vqm.jump(head, real[1:], kept, inv.get(dev.get("location")) or {}, bps, free=free)
        if not pick:
            return None
        kind, x = pick
        body = vqm.body_of(x) if kind == "real" else vqm.send_body(x)
        uses = comp.components(bps.get(body.get("device_type") or ""))
        if kind == "kept" and uses:   # no more of it than the free components cover
            body["quantity"] = max(1, min(int(body.get("quantity") or 1), min(free.get(c, 0) // n for c, n in uses.items())))
        again = vqm.body_of(head)
        async with vq.lock:
            st = await vq.state()
            low = min([o["seq"] for o in st["orders"]] + [st.get("low", 0), 0])
            st["low"] = low - len(real) - 3
            gone = []
            for _ in range(len(real)):
                ok, _, _ = await self.send("POST", f"/devices/{code}", {"command": "dequeue_print", "index": 0},
                                           f"print queue: {code} #1 back to the app's queue", raw=True)
                if not ok:
                    break
                gone.append(real[len(gone)])
            for i, it in enumerate(gone):
                if it is not x and it is not head:
                    await vq.add(code, vqm.body_of(it), vqm.priority(it), s=st, seq=low - len(real) + i - 1)
            if not any(g is head for g in gone):   # couldn't take the waiting one off: leave the rest to the feeder
                await self.db.kv_set(vqm.KV, st)
                return None
            ok, _, err = await self.send("POST", f"/devices/{code}", body, f"print queue: {code} gets {body['device_type']} "
                                         f"ahead of the waiting {head['device_type']}", raw=True)
            if ok and kind == "kept":
                st["orders"] = vqm.take(st["orders"], x["factory"], x, int(body.get("quantity") or 1))
            elif not ok and kind == "real" and any(g is x for g in gone):
                await vq.add(code, body, vqm.priority(x), s=st, seq=low - len(real) - 2)
            okb, _, _ = await self.send("POST", f"/devices/{code}", again,
                                        f"print queue: {code} gets the waiting {head['device_type']} back", raw=True)
            if not okb:
                await vq.add(code, again, vqm.priority(head), s=st, seq=low - len(real) - 3)
            await self.db.kv_set(vqm.KV, st)
        if not ok:
            return f"{code}: couldn't queue {body['device_type']} ahead of the waiting {head['device_type']} ({err})"
        return f"{code}: {body['device_type']} goes ahead of the waiting {head['device_type']}, which is queued right behind it"

    async def components_ahead(self, g: dict, left: int) -> str:
        """A factory stuck on components that nothing else prints (every factory waiting, snapshot 2026-11-01 20:41:
        44 rigs queued at the head of all 5 queues, no drone printed) prints them itself: the `left` ones are queued
        on it, and its component prints of that type go to the front of its queue, ahead of the item that waits."""
        from . import printqueue as pq
        from .loadouts import at_tag
        from .stock import enqueue_body
        code, pin = g["factory"], at_tag(g["location"])
        if left > 0:
            ok, _, _ = await self.send("POST", f"/devices/{code}", {"command": "enqueue_print", "device_type": g["type"],
                                       "quantity": left, "tags": g["tags"]}, f"components for {code}: print {left} {g['type']} itself")
            if not ok:
                return ""
        if getattr(self.api, "vq", None) is not None:   # the virtual queue's feeder puts components first
            return f"{left} queued on it" if left > 0 else ""
        try:
            dev = await self.api.get(f"/devices/{code}") or {}
        except ApiError:
            return ""
        q = pq.items(dev)
        mine = [x for x in q if x.get("device_type") == g["type"] and pin in (x.get("tags") or [])]
        if not mine or q[:len(mine)] == mine:
            return f"{left} queued on it" if left > 0 else ""
        order = mine + [x for x in q if x not in mine]
        k = next(i for i, (a, b) in enumerate(zip(q, order)) if a is not b)
        for _ in range(len(q) - k):
            ok, _, err = await self.send("POST", f"/devices/{code}", {"command": "dequeue_print", "index": k},
                                         f"{code}: take #{k + 1} off the queue (components first)")
            if not ok:
                return f"couldn't move its {g['type'].replace('_', ' ')} prints up: {err}"
        for it in order[k:]:
            await self.send("POST", f"/devices/{code}", enqueue_body(it), f"{code}: re-queue {it['device_type']} (components first)",
                            components_done=True)
        return f"{len(mine)} {g['type'].replace('_', ' ')} print(s) moved to the front of its queue"

    async def commandable(self, devices: list[dict], stars: dict | None = None, without: set[str] | None = None,
                          networked: bool = False) -> set[str]:
        """Systems you can give commands in: an active relay network's coverage (relaynet.coverage), or where one of your
        replicants is (other than the replicants in `without`). `networked`: the network's coverage only, without the
        systems a replicant covers just by being there (it may leave)."""
        from . import relaynet
        if stars is None:
            pos = await self.star_lookup("pos")   # shared, positions only
        else:
            pos = {k: (v or {}).get("position") for k, v in stars.items() if (v or {}).get("position")}
        reps = await self.db.kv_get("replicants", {}) or {}
        rstars = relaynet.replicant_stars({c: r for c, r in reps.items() if c not in (without or ())}, devices)
        from .shapes import normalize_blueprints
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        net = relaynet.networks(devices, pos, bps)
        cov = relaynet.coverage(net, pos, rstars)
        cover = set().union(*(cov["covered"][i] for i in cov["active"])) if networked else cov["commandable"]
        return cover - relaynet.dark_stars(devices)

    async def evacuation_inputs(self) -> tuple[dict, set[str], dict]:
        """(tracked objects, objects the game warned us about in the last two hours, system scans by star)."""
        import json as _json
        objs = await self.defence_objects()
        # warned in the last two hours (defence_objects files each warning under its asteroid's designation)
        warned = {des for des, o in objs.items() if _ts(o.get("warned_at")) and (_now() - _ts(o["warned_at"])).total_seconds() < 7200}
        scans = {}
        for r in await self.db.fetchall("SELECT star, data FROM systems"):
            try:
                scans[r["star"]] = _json.loads(r["data"] or "{}")
            except ValueError:
                pass
        return objs, warned, scans

    async def evacuation_targets(self, devices: list[dict], stars: dict) -> dict[str, str]:
        """{object location: where its stockpile goes} for every object due within the evacuation window."""
        from . import evacuate as ev
        objs, warned, scans = await self.evacuation_inputs()
        hot = ev.due(objs, warned=warned, pct=await self.diverted_pct())
        obj_at = ev.at_location(objs)
        from .rockmining import open_rocks   # v3.6.0: a rock being mined has its pile hauled out while the rigs work
        worked = {s.get("rock") for s in (await self.db.kv_get("rock_mining", {}) or {}).values()} & set(open_rocks(objs))
        hot = {**{r: 0.0 for r in worked}, **hot}
        for r in await self.overdue_rocks():   # it may strike any time: its pile is left there, no carrier goes in
            hot.pop(r, None)
        return {loc: ev.safe_spot(star_of(loc), (obj_at.get(loc) or {}).get("impact_target"), stars, scans.get(star_of(loc)),
                                  here=loc, avoid=set(hot))
                for loc in hot}

    async def evacuate_pass(self, force: bool = False) -> list[str]:
        """Evacuation before impact (evacuate.py): devices at an object due within the hour leave for a safe spot in
        the system (propulsors still pushing at the rule's leave_minutes, 45), grounded ones board a carrier there; piles go via logistics."""
        from . import evacuate as ev
        from . import loadouts as lo
        objs, warned, scans = await self.evacuation_inputs()
        pct = await self.diverted_pct()
        if not ev.due(objs, warned=warned, pct=pct):
            return []
        devices = await self.devices()
        stars = await self.star_lookup("by")   # shared with every reader: never change a record
        reps = await self.db.kv_get("replicants", {}) or {}
        hosts = {r.get("hosted_device_code") for r in reps.values() if r.get("hosted_device_code")}
        busy = self.busy_devices(await self.jobs())
        try:
            lv = float(((await self.settings())["rules"].get("asteroid_defence") or {}).get("leave_minutes") or ev.DIVERTER_MINUTES)
        except (TypeError, ValueError):
            lv = ev.DIVERTER_MINUTES
        p = ev.plan(objs, devices, stars, busy, hosts, scans, warned=warned, pct=pct,
                    diverter_minutes=min(max(lv, 30.0), ev.EVAC_MINUTES))
        from . import fleets as fl
        out = []
        boards: dict[str, list[dict]] = {}
        for b in p["boards"]:
            boards.setdefault(b["carrier"], []).append(b)
        for m in p["moves"]:
            code = m["code"]
            steps = [fl.board_step(code, b["code"], b["mode"]) for b in boards.get(code, [])]
            if m.get("deactivate"):
                steps.append(step(f"{code}: deactivate", f"/devices/{code}", {"command": "deactivate"}))
            steps.append(step(f"{code} → {m['to']} (evacuate {m['from']}: {m['why']})", f"/devices/{code}",
                              {"command": "travel", "destination": m["to"]}, critical=True))
            codes = [code] + [b["code"] for b in boards.get(code, [])]
            by = {d["device_code"]: d for d in devices}
            for c in codes:   # an at: pin to the object would send it straight back (loadouts pins)
                pin = lo.at_tag(m["from"])
                if pin in ((by.get(c) or {}).get("tags") or []):
                    steps.append(lo.tag_step(c, remove=[pin]))
            if await self.create_job("evacuate", f"evacuate {code} from {m['from']} → {m['to']}", code, steps,
                                     {"devices": codes, "through_hold": True}):   # an asteroid doesn't wait for a resume
                out.append(f"{code}: leaving {m['from']} for {m['to']} ({m['why']})")
        for s_ in p["stuck"]:
            key = f"evac-stuck:{s_['code']}"
            if self.stage_errors.get(key) != s_["why"]:
                self.stage_errors[key] = s_["why"]
                await self.log("evacuate", f"{s_['code']} at {s_['location']} can't be evacuated: {s_['why']}", "error", notify=True)
        for line in out:
            await self.log("evacuate", line, "alert", notify=True)
        return out

    async def dark_networks(self, devices: list[dict], pos: dict, bps: dict, reps: dict) -> list[set[str]]:
        """What each relay network without a replicant in reach covers: a replicant anywhere in one makes everything it
        covers commandable (logistics rescues)."""
        from . import relaynet
        net = relaynet.networks(devices, pos, bps)
        cov = relaynet.coverage(net, pos, relaynet.replicant_stars(reps, devices))
        return [stars for i, stars in cov["covered"].items() if i not in cov["active"]]

    async def supply_reach(self) -> float:
        """The loadout supply range (ly, Fleets › settings): nothing is fetched from farther."""
        from .loadouts import DEFAULT_SETTINGS
        try:
            v = ((await self.loadout_cfg()).get("settings") or {}).get("max_supply_ly")
            return float(v if v is not None else DEFAULT_SETTINGS["max_supply_ly"])
        except (TypeError, ValueError):
            return float(DEFAULT_SETTINGS["max_supply_ly"])

    async def max_cruise_au(self) -> float:
        from .fleets import MAX_CRUISE_AU
        try:
            return float((await self.loadout_cfg())["settings"].get("max_cruise_au") or MAX_CRUISE_AU)
        except (TypeError, ValueError):
            return MAX_CRUISE_AU

    async def loadout_orders(self) -> list[dict]:
        """Prints ordered for a system and not yet seen as a device (they count as incoming)."""
        orders = await self.db.kv_get("loadout_orders", []) or []
        codes = {d.get("device_code") for d in await self.devices()}
        jobs = {j["id"]: j for j in await self.jobs()}
        now = _now()
        keep = []
        for o in orders:
            if o.get("device_code") and o["device_code"] in codes:
                continue  # the printed device is in the device list now, tagged for its system
            j = jobs.get(o.get("job"))
            if j and (j["status"] == "failed" or (j["steps"] and j["steps"][0]["status"] == "skipped")):
                continue  # the enqueue didn't happen
            at = _ts(o.get("at"))
            if at and (now - at).total_seconds() > 48 * 3600:
                continue
            keep.append(o)
        from .loadouts import reconcile_orders
        keep = reconcile_orders(keep, await self.devices(), now.timestamp())
        if len(keep) != len(orders):
            await self.db.kv_set("loadout_orders", keep)
        return keep

    async def loadout_plan(self, only: set[str] | None = None) -> dict:
        from .loadouts import plan
        from .shapes import normalize_blueprints, normalize_inventory
        cfg = await self.loadout_cfg()
        devices = await self.devices()
        bps = normalize_blueprints(await self.db.kv_get("blueprints", []))
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        inv = await self.safe_piles(inv)   # piles at overdue rocks are left (2026-11-08)
        stars = await self.star_lookup("by")   # shared with every reader: never change a record
        reps = await self.db.kv_get("replicants", {}) or {}
        hosts = {r.get("hosted_device_code"): code for code, r in reps.items() if r.get("hosted_device_code")}
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        # a carrier whose delivery just failed (out of comms, mid-surge …) sits out for 30 minutes; one that failed twice
        # with the same cargo in 6 hours sits out the rest of those 6 hours (live 2026-10-20: five plates failed the
        # same attach every half hour)
        fails: dict[tuple, int] = defaultdict(int)
        for j in jobs:
            fin = _ts(j.get("finished_at") or j.get("created_at"))
            if j["rule"] == "loadouts" and j["status"] == "failed" and fin and (_now() - fin).total_seconds() < 6 * 3600:
                fails[(j.get("device"), tuple(sorted(j.get("meta", {}).get("devices", []))))] += 1
        for j in jobs:
            fin = _ts(j.get("finished_at") or j.get("created_at"))
            if j["rule"] != "loadouts" or j["status"] != "failed" or not fin:
                continue
            age = (_now() - fin).total_seconds()
            if age < 1800 or (age < 6 * 3600 and fails[(j.get("device"), tuple(sorted(j.get("meta", {}).get("devices", []))))] >= 2):
                busy.add(j.get("device"))
                busy.update(j.get("meta", {}).get("devices", []))
        # an approved shop trade waiting on logistics keeps one logistics vessel with a replicant aboard free for it, the
        # nearest that can surge: only such a vessel can fulfill it, and a device run can tie it up for hours (owner
        # 2026-10-20). Holding them all kept every fleet carrier towing a replicant off relay runs (owner 2026-10-20).
        # Contracts don't: a pre-approved civilization's are open nearly all the time.
        from . import logistics as lg
        deals = [r for r in ((await self.db.kv_get(lg.KV_STATE, {}) or {}).get("requests") or [])
                 if r.get("kind") == "trade" and not r.get("skipped") and not r.get("devices_short")]
        # (a trade still waiting for the devices it pays with holds nothing: those devices come by this pass, which may
        # need that very vessel as escort; live 2026-11-02: Delta's only one, 5DF3161B, sat at ISOLITHOR held for the
        # Crystal Synthesis Lab while its 10 compute cores and 5 relays waited there for an escort)
        if deals:
            from . import fleets as fl
            from .loadouts import can_surge
            ltags = {fl.fleet_tag(f["id"]) for f in await self.fleets() if f.get("role") == "logistics" and not f.get("recall_hold")}
            by = {d.get("device_code"): d for d in devices}
            pos = {k: v.get("position") for k, v in stars.items() if v.get("position")}
            vessels = [c for c in lg.riding_hosts(devices, hosts) if c not in busy and can_surge(by.get(c) or {})
                       and ltags & set((by.get(c) or {}).get("tags") or [])]
            for r in deals:
                to = star_of(r.get("to"))
                pick = min(vessels, key=lambda c: (lg._dist(star_of((by.get(c) or {}).get("location")), to, pos), c), default=None)
                if pick:
                    busy.add(pick)
                    vessels.remove(pick)
        from .loadouts import material_routes
        from .traffic import beacons as _beacons
        civ_locs = {r["location"] for r in await self.civ_coverage() if r.get("completed") or r.get("open")}
        protect = {b["device_code"] for b in _beacons(devices) if b.get("location") in civ_locs}
        p = plan(cfg, devices, bps, inv, stars, hosts, busy, await self.loadout_orders(),
                 await self.db.kv_get("stowed_map", {}) or {}, only, await self.known_open_sites(), protect,
                 await self.geography(devices, stars), await self.commandable(devices, stars))
        current = {}
        live: set[str] = set()
        for d in devices:
            if "transport" not in (d.get("device_type") or ""):
                continue
            if "ami_directive" in d:  # the game says what it's doing: trust that over events and memory
                dirv = d.get("ami_directive") or {}
                state = str(dirv.get("_eval_state") or "")
                current[d["device_code"]] = {"directive": dirv.get("name"), "configuration": dirv.get("config") or {},
                                             "finished": not dirv or state.startswith(("done", "complete"))
                                             or str(d.get("ami_directive_status") or "active") != "active"}
                live.add(d["device_code"])
                continue
            row = await self.db.fetchone("SELECT event, payload, created_at FROM events WHERE device_code=? AND event LIKE 'directive.%' "
                                         "ORDER BY seq DESC LIMIT 1", (d["device_code"],))
            if row:
                pl = json.loads(row["payload"] or "{}")
                current[d["device_code"]] = {"directive": pl.get("directive"), "configuration": pl.get("configuration"),
                                             "finished": row["event"] in ("directive.completed", "directive.cleared", "directive.paused"),
                                             "at": row["created_at"]}
        sent = await self.db.kv_get("loadout_ferries", {}) or {}
        for ctrl, f in sent.items():
            if ctrl in live:
                continue
            cur = current.get(ctrl)
            later_finish = cur and cur.get("finished") and (cur.get("at") or "") > (f.get("at") or "")
            if not later_finish:  # what we sent is still what it's doing, whatever the event payloads say
                current[ctrl] = {"directive": "ferry", "configuration": f.get("configuration"), "finished": False}
        cfg_only = {**cfg, "fleets": [f for f in cfg["fleets"] if not only or f.get("id") in only or f.get("home") in only
                                      or not f.get("materials") or f.get("materials") == "self"]}
        from .ami_schedule import managed_by
        from .fleets import fleet_tag
        logistics_tags = {fleet_tag(f["id"]) for f in cfg["fleets"] if f.get("role") == "logistics" and f.get("id")}   # the ferry never adopts a logistics
        busy_f = busy | {d["device_code"] for d in devices if logistics_tags & set(d.get("tags") or [])}
        routes, unmet = material_routes(cfg_only, devices, inv, stars, busy_f, current, await managed_by(self.db))
        p["routes"], p["unmet"] = routes, p["unmet"] + unmet
        return p

    async def apply_loadouts(self, only: set[str] | None = None, manual: bool = False) -> list[str]:
        from . import loadouts as lo
        cfg = await self.loadout_cfg()
        p = await self.loadout_plan(only)
        stars = await self.star_lookup("by")   # shared with every reader: never change a record
        stowed_in = {c: k for k, kids in (await self.db.kv_get("stowed_map", {}) or {}).items() for c in kids}
        lines = lo.describe(p)
        started = 0
        # what this pass moves to another system (the observatory pass leaves those alone instead of unfurling them —
        # seen live 2026-10-09: Miner 2's observatory unfurled while loadouts was compacting it for the trip to ITHVALAI)
        await self.db.kv_set("loadout_moves", {"at": now_iso(), "moves": dict(p.get("moves") or {})})
        radii, far_au = await self.cruise_radii(), await self.max_cruise_au()
        for ctrl, codes in sorted((p.get("releases") or {}).items()):
            why = "no longer in the loadout" if set(codes) <= set(p.get("made_spare") or []) else "in another system / spare"
            started += bool(await self.create_job("loadouts", f"loadouts: {ctrl} releases {len(codes)} device(s) ({why})", ctrl,
                                                  [lo.step(f"{ctrl}: release {', '.join(codes)}", f"/devices/{ctrl}",
                                                           {"command": "release", "devices": codes})],
                                                  {"devices": codes}, force=manual))
        tags = lo.tag_steps(p)
        if tags:
            started += bool(await self.create_job("loadouts", f"loadouts: spare tags ({len(tags)})", None, tags,
                                                  {"devices": []}, force=manual))
        started += await self.start_compactions(p, force=manual)
        orders = await self.db.kv_get("loadout_orders", []) or []
        if manual or not (await self.settings())["dry_run"]:
            for c in p.get("cancel_orders") or []:
                orders = await self.cancel_loadout_print(c, orders)
        for pr in p["prints"]:
            job = await self.create_job("loadouts", f"loadouts: print {pr['n']}× {pr['device_type']} for {pr['star']}",
                                        pr["factory"], lo.print_steps(pr), {"devices": [], "star": pr["star"]}, force=manual)
            if job:
                started += 1
                orders += [{"star": pr["star"], "fleet": pr.get("fleet"), "device_type": pr["device_type"],
                            "factory": pr["factory"], "at": now_iso(), "job": job["id"]} for _ in range(pr["n"])]
        await self.db.kv_set("loadout_orders", orders)
        for code, dest in p["self_moves"]:
            started += bool(await self.create_job("loadouts", f"loadouts: {code} → {dest}", code,
                                                  lo.self_move_steps(code, dest, stars, p["by_code"][code], p.get("managed"),
                                                                    gathering=code in set(p.get("gathering") or []),
                                                                    join=(p.get("assign") or {}).get(code)),
                                                  {"devices": [code], "star": dest}, force=manual))
        for dl in p["deliveries"]:
            started += bool(await self.create_job(
                "loadouts", f"loadouts: {dl['carrier']} carries {len(dl['devices'])} {dl['from']} → {' → '.join([dl['to']] + list(dl.get('stops') or []))}", dl["carrier"],
                lo.delivery_steps(dl, p["by_code"], stars, cfg["settings"]["carriers_return"], p.get("managed"),
                                                  gathering=set(p.get("gathering") or []), assign=p.get("assign"),
                                                  radii=radii, max_cruise_au=far_au),
                {"devices": dl["devices"], "star": dl["to"]}, force=manual))
        if p["deliveries"]:   # a carrier was wanted at the pick-up: the hubs' weights (balance.py)
            await self.note_demand({f"lo:{dl['carrier']}:{dl['from']}:{now_iso()}": dl["from"] for dl in p["deliveries"]})
        ferries = await self.db.kv_get("loadout_ferries", {}) or {}
        for r in p.get("routes") or []:
            job = await self.create_job("loadouts", f"loadouts: ferry {r['source']} → {r['dest']}"
                                        + (f" (+{len(r['adopt'])} freighter)" if r.get("adopt") else ""), r["controller"],
                                        lo.ferry_steps(r), {"devices": [a["code"] for a in r.get("adopt") or []], "star": r["dest"]},
                                        force=manual)
            if job:
                started += 1
                if r.get("resend", True):
                    ferries[r["controller"]] = {"configuration": {"collect": r["collect"], "deliver": r["deliver"]}, "at": now_iso()}
        await self.db.kv_set("loadout_ferries", ferries)
        on = lambda code, loc: [lo.activate_step(code)] if lo.relay_ready_at(p["by_code"].get(code) or {}, loc) else []  # noqa: E731
        for code, loc in p.get("pins") or []:
            started += bool(await self.create_job("loadouts", f"loadouts: {code} → {loc} (pinned)", code,
                                                  ([lo.deactivate_step(code)] if code in (p.get("pins_off") or []) else [])
                                                  + [lo.pin_step(code, loc)] + on(code, loc), {"devices": [code]}, force=manual))
        for code, loc in p.get("places") or []:
            started += bool(await self.create_job("loadouts", f"loadouts: {code} → {loc} (placement)", code,
                                                  [lo.pin_step(code, loc, "where its type works")] + on(code, loc),
                                                  {"devices": [code]}, force=manual))
        for code in p["arrived"]:
            steps = lo.arrived_steps(code, p["by_code"][code], stowed_in, join=(p.get("assign") or {}).get(code))
            if steps:
                started += bool(await self.create_job("loadouts", f"loadouts: {code} arrived", code, steps,
                                                      {"devices": [code]}, force=manual))
        started += await self._arrival_followups(cfg, p, manual, lines)
        await self.db.kv_set("loadouts_last", {"at": now_iso(), "lines": lines, "jobs": started,
                                               "dry_run": (await self.settings())["dry_run"] and not manual,
                                               "escort_wanted": lo.escort_wanted(p)})
        return lines

    async def cancel_loadout_print(self, c: dict, orders: list[dict]) -> list[dict]:
        """A spare at home took the place of a loadout order (loadouts.plan cancel_orders): its print comes off the
        queue it sits in, the game's (newest matching first) or the app's, on the ordering factory or a neighbor fed
        from it (same spot and type), and the order is forgotten. One already printing stays: its device turns out one
        over and is made spare by a later pass."""
        from . import fleets as fl, printqueue as pq
        from .loadouts import forget_queued
        devs = {d.get("device_code"): d for d in await self.devices()}
        f = devs.get(c["factory"]) or {}
        tag = fl.fleet_tag(c["fleet"])
        group = [c["factory"]] + sorted(k for k, d in devs.items() if k != c["factory"] and f.get("location")
                                        and d.get("location") == f.get("location") and d.get("device_type") == f.get("device_type"))
        mine = lambda t, tags: t == c["device_type"] and tag in (tags or [])   # noqa: E731
        label = (f"loadouts: spare {c['spare']} replaces the {c['device_type']} queued for {c['star']}" if c.get("spare")
                 else f"loadouts: the {c['device_type']} queued for {c['star']} isn't needed any more (the fleet has its count)")
        done = False
        for code in group:
            try:
                dev = await self.api.request("GET", f"/devices/{code}", background=True, raw=True) or {}
            except ApiError:
                continue
            hit = [it for it in pq.items(dev) if not it["extra"].get("virtual") and mine(it["device_type"], it["tags"])]
            if hit:
                ok, _, _ = await self.send("POST", f"/devices/{code}", {"command": "dequeue_print", "index": hit[-1]["api_index"]},
                                           f"{label}: off {code}'s queue", raw=True)
                done = ok
                break
        vq = getattr(self.api, "vq", None)
        if not done and vq is not None:
            kept = [o for o in await vq.orders(codes=group) if mine((o.get("body") or {}).get("device_type"),
                                                                    (o.get("body") or {}).get("tags"))]
            if kept:
                o = kept[-1]
                ok, _, _ = await self.send("POST", f"/devices/{o['factory']}", {"command": "dequeue_print", "order": o["seq"]},
                                           f"{label}: off the app's queue for {o['factory']}")
                done = ok
        if done:
            await self.log("loadouts", f"{label}: print taken off the queue", "info")
            return forget_queued(orders, c["factory"], c["device_type"], 1)
        return orders

    async def _arrival_followups(self, cfg: dict, p: dict, manual: bool, lines: list[str]) -> int:
        """After the moves: drones that reached their system join its controller; inactive maintenance drones and
        AMI controllers that reached home are switched on (once per arrival)."""
        from .ami_schedule import handoff_steps, handoffs, managed_by, wakeup_steps, wakeups
        from . import loadouts as lo
        rc = {"adopt_arrivals": True, "activate_arrivals": True, **((await self.settings())["rules"].get("loadouts") or {})}
        devices = await self.devices()
        busy = self.busy_devices(await self.jobs())
        skip = set(p.get("moves") or {}) | set(p.get("arrived") or []) | {c for dl in p.get("deliveries") or [] for c in dl["devices"]} \
            | {dl["carrier"] for dl in p.get("deliveries") or []}
        started = 0
        if rc.get("adopt_arrivals", True):
            for h in handoffs(devices, await managed_by(self.db), busy, skip, set(cfg.get("ignore_tags") or [])):
                lines.append(f"{h['drone']} joins controller {h['controller']}"
                             + (f" (flies {h['from']} → {h['to']})" if h["from"] != h["to"] else ""))
                started += bool(await self.create_job("loadouts", f"loadouts: {h['controller']} adopts {h['drone']}", h["controller"],
                                                      handoff_steps(h), {"devices": [h["drone"]]}, force=manual))
        if rc.get("activate_arrivals", True):
            done = await self.db.kv_get("loadout_woken", {}) or {}
            # a survey crew's relays wait switched off, and one working relay per system is enough
            skip = skip | lo.survey_cargo(devices, cfg.get("fleets") or []) | {
                d["device_code"] for d in devices if (d.get("device_type") or "") == "ftl_relay"
                and star_of(d.get("location")) in lo.relaying_stars(devices, {d["device_code"]})}
            by = {d.get("device_code"): d for d in devices}
            for w in wakeups(devices, busy, done, skip):
                code = w["code"]
                what = " + ".join(x for x, on in (("activate", w["activate"]), ("patrol", w["patrol"])) if on)
                lines.append(f"{what} {code} ({by[code].get('device_type')}) — it's home")
                job = await self.create_job("loadouts", f"loadouts: {what} {code}", code, wakeup_steps(w),
                                            {"devices": [code]}, force=manual)
                if job:
                    started += 1
                    done[code] = star_of(by[code].get("location"))
            await self.db.kv_set("loadout_woken", done)
        return started

    async def start_compactions(self, p: dict, force: bool = False) -> int:
        """Large devices the loadout plan moves to another system are compacted first, on their own (hours), so no
        carrier waits for it; the plan assigns the carrier once they report compacted."""
        from .modular import compact_seconds, compact_step
        from .shapes import normalize_blueprints
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        n = 0
        for code, dest in p.get("compact") or []:
            d = p["by_code"].get(code) or {}
            hours = compact_seconds(d, bps) / 3600
            job = await self.create_job("loadouts", f"loadouts: compact {code} ({d.get('device_type')}) for the trip to {dest} "
                                                    f"(≈{hours:.1f} h)", code,
                                        [compact_step(code, bps, d, f"for the trip to {dest}")], {"devices": [code]},
                                        force=force)
            n += bool(job)
        return n

    async def dispatch_new_prints(self, max_wait_minutes: int = 15) -> list[str]:
        """Prints that just came out bound for another system: refresh the device list until they show up, then start
        only the deliveries (carrier or own surge) that involve them — the rest waits for the regular pass."""
        from . import loadouts as lo
        pend = await self.db.kv_get("dispatch_pending", {}) or {}
        if not pend or not await self.rule_cfg("loadouts"):
            return []
        now = _now()
        pend = {c: at for c, at in pend.items() if _ts(at) and (now - _ts(at)).total_seconds() < max_wait_minutes * 60}
        codes = {d.get("device_code") for d in await self.devices()}
        if not set(pend) <= codes and self.worker:
            last = _ts(await self.db.kv_get("dispatch_sync_at", None))
            if not last or (now - last).total_seconds() >= 20:   # the new device isn't listed yet: re-read (≤ every 20 s)
                await self.db.kv_set("dispatch_sync_at", now.isoformat(timespec="seconds"))
                try:
                    await self.worker.sync_devices()
                except Exception as e:  # noqa: BLE001 — try again next tick
                    log.info("dispatch: device sync failed: %s", e)
                codes = {d.get("device_code") for d in await self.devices()}
        ready = {c for c in pend if c in codes}
        if not ready:
            await self.db.kv_set("dispatch_pending", pend)
            return []
        tried = _ts(await self.db.kv_get("dispatch_try_at", None))
        if tried and (now - tried).total_seconds() < 30:
            return []
        await self.db.kv_set("dispatch_try_at", now.isoformat(timespec="seconds"))
        cfg = await self.loadout_cfg()
        stars = await self.star_lookup("by")   # shared with every reader: never change a record
        p = await self.loadout_plan()
        radii, far_au = await self.cruise_radii(), await self.max_cruise_au()
        started: list[str] = []
        if await self.start_compactions({**p, "compact": [x for x in p.get("compact") or [] if x[0] in ready]}):
            started.append("compacting " + ", ".join(c for c, _ in p.get("compact") or [] if c in ready))
        for code, dest in p["self_moves"]:
            if code in ready:
                if await self.create_job("loadouts", f"loadouts: {code} → {dest} (just printed)", code,
                                         lo.self_move_steps(code, dest, stars, p["by_code"][code], p.get("managed"),
                                                                    gathering=code in set(p.get("gathering") or []),
                                                                    join=(p.get("assign") or {}).get(code)),
                                         {"devices": [code], "star": dest}):
                    started.append(f"{code} flies to {dest}")
                ready.discard(code), pend.pop(code, None)
        for dl in p["deliveries"]:
            if ready & set(dl["devices"]):
                if await self.create_job("loadouts", f"loadouts: {dl['carrier']} carries {len(dl['devices'])} {dl['from']} → {' → '.join([dl['to']] + list(dl.get('stops') or []))} "
                                         "(just printed)", dl["carrier"],
                                         lo.delivery_steps(dl, p["by_code"], stars, cfg["settings"]["carriers_return"], p.get("managed"),
                                                  gathering=set(p.get("gathering") or []), assign=p.get("assign"),
                                                  radii=radii, max_cruise_au=far_au),
                                         {"devices": dl["devices"], "star": dl["to"]}):
                    started.append(f"{dl['carrier']} carries {', '.join(dl['devices'])} to {dl['to']}")
                for c in dl["devices"]:
                    ready.discard(c), pend.pop(c, None)
        for u in p["unmet"]:   # nothing free to carry it now: say so, and leave it to the regular pass
            if "waiting for a surge-capable carrier" in u.get("why", "") and ready:
                await self.log("loadouts", f"just printed {', '.join(sorted(ready))}: {u['why']} — the next pass retries")
                for c in list(ready):
                    pend.pop(c, None)
                break
        await self.db.kv_set("dispatch_pending", pend)
        for line in started:
            await self.log("loadouts", f"dispatched right after printing: {line}")
        return started

    async def run_due_loadouts(self) -> None:
        cfg = await self.rule_cfg("loadouts")
        if not cfg:
            return
        last = _ts(((await self.db.kv_get("loadouts_last", {})) or {}).get("at"))
        if last and (_now() - last).total_seconds() < 60 * max(1, int(cfg.get("every_minutes") or 15)):
            return
        await self.apply_loadouts()
