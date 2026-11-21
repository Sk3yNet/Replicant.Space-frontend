"""Rock chaser fleets in the engine (rockchase.py has the rules): stage rock_chase_pass."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .db import now_iso

log = logging.getLogger("rsweb.chase")
SKIP_HOURS = 6   # a rock whose trip failed or was too long isn't tried again for this long


def _ts(v: Any) -> float | None:
    from .shapes import game_time
    dt = game_time(v)
    return dt.timestamp() if dt else None


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


class ChaseMixin:
    async def rock_chase_pass(self) -> list[str]:
        """Every rock chaser fleet: choose / follow its target, mine it, ship the pile, leave in time, search the Kuiper belt
        and launch marked objects. Returns what it did."""
        from . import rockchase as rc
        from .shapes import normalize_inventory
        fleet_list = await self.fleets()
        queued = await self.db.kv_get(rc.PENDING_KV, []) or []   # card changes made while a tick held the lock
        if queued:
            by_id = {f.get("id"): f for f in fleet_list}
            for q in queued:
                if q.get("fid") in by_id:
                    rc.apply_form(by_id[q["fid"]], q.get("form") or {}, q.get("user") or "", q.get("at") or now_iso())
            await self.save_fleets(fleet_list)
            left = [q for q in await self.db.kv_get(rc.PENDING_KV, []) or [] if q not in queued]   # queued meanwhile: next pass
            await self.db.kv_set(rc.PENDING_KV, left)
        mine = [f for f in fleet_list if f.get("role") == rc.ROLE and f.get("home") and not f.get("recall_hold")]   # recalled: held
        if not mine:
            return []
        devices = await self.devices()
        jobs = {j["id"]: j for j in await self.jobs()}
        objs, _warned, scans = await self.evacuation_inputs()
        reps = await self.db.kv_get("replicants", {}) or {}
        state = await self.db.kv_get("rock_mining", {}) or {}
        ctx = {"devices": devices, "jobs": jobs, "busy": self.busy_devices(list(jobs.values())), "objs": objs,
               "scans": scans, "stars": await self.star_lookup("by"), "pos": await self.star_lookup("pos"),
               "cover": await self.commandable(devices, networked=True), "reach": await self.commandable(devices),
               "state": state,
               "full": {r: t for r, t in (await self.db.kv_get("rock_full", {}) or {}).items()
                        if _ts(t) and _now() - _ts(t) < 3600},
               "hosts": {r.get("hosted_device_code") for r in reps.values() if r.get("hosted_device_code")},
               "inv": {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))},
               "scanned": {r: s["at"] for r, s in (await self.rock_scans()).items()}, "fleets": mine}
        known = await self.db.kv_get(rc.YIELDS_KV, {}) or {}
        if rc.learn_yields(objs, known):
            await self.db.kv_set(rc.YIELDS_KV, known)
        ctx["yields"] = known
        # printing hubs (as megas.nearest_hubs counts them): the stockpile a chaser's haul ends up in
        ctx["hubs"] = sorted({x["home"] for x in fleet_list if x.get("home") and x.get("station")
                              and x.get("materials") == "self" and x.get("role") != "logistics"})
        out: list[str] = []
        changed = False
        for f in mine:
            if f.get("station"):
                f["station"] = False   # it roams: the loadout pass doesn't keep it at home
                changed = True
            run = f.setdefault("chase_run", {})
            before = repr(run)
            try:
                out += [f"{f['name']}: {x}" for x in await self._chase_fleet(f, rc.settings(f), run, ctx)]
            except Exception as e:  # noqa: BLE001  (one fleet's odd data mustn't stop the others)
                log.exception("rock chaser %s failed", f.get("id"))
                run["note"] = f"error: {type(e).__name__}: {e}"
            changed = changed or repr(run) != before
        await self.db.kv_set("rock_mining", state)
        await self.db.kv_set("rock_full", ctx["full"])
        if changed:
            await self.save_fleets(fleet_list)
        for line in out:
            await self.log("rock_chaser", line)
        return out

    async def _chase_fleet(self, f: dict, cfg: dict, run: dict, ctx: dict) -> list[str]:
        from . import fleets as fl
        from . import rockchase as rc
        from . import rockmining as rm
        devices, objs = ctx["devices"], ctx["objs"]
        ms = fl.members(f, devices)
        k = rc.kinds(ms)
        out: list[str] = []
        need = self._chase_need(f, run, ctx, k)
        if cfg.get("follow_need"):   # Joe 2026-11-19: mine what the nearest stockpile is low on first, then anything
            cfg = {**cfg, "priority": ", ".join(need)}
        if not k["rigs"]:
            run["note"] = "needs mining rigs (devices with the fracture feature)"
            return out
        open_ = rm.open_rocks(objs)
        j = ctx["jobs"].get(run.get("job"))
        if j and j["status"] in ("running", "waiting", "pending"):
            return out
        if j and j["status"] == "done" and run.get("phase") == "moving" and run.get("target"):
            run["arrived"] = run["target"]   # the trip's job ran to the end: the rigs are there, whatever the cached list says
        errs = [str(s.get("error") or "") for s in (j or {}).get("steps") or [] if s.get("status") in ("failed", "skipped")]
        if j and j["status"] == "failed" and errs and all("already mining" in e.lower() for e in errs):
            if run.get("target"):   # the rigs were mining already (a repeat job): the rock is fine
                run["arrived"] = run["target"]
        elif j and j["status"] == "failed":
            err = next((s.get("error") for s in j["steps"] if s.get("status") == "failed"), "") or "?"
            run["note"] = f"last {run.get('phase') or 'job'} failed: {err}"
            if run.get("phase") == "moving" and run.get("target"):
                run.setdefault("skip", {})[run["target"]] = now_iso()
                run.pop("target", None)
        run.pop("job", None)
        run["skip"] = {r: t for r, t in (run.get("skip") or {}).items() if _ts(t) and _now() - _ts(t) < SKIP_HOURS * 3600}
        if not run.get("target"):   # live 2026-11-16: rigs mining a rock the run had dropped: take it back
            working = sorted({d.get("location") for d in k["rigs"] if d.get("location") in open_
                              and str(d.get("status") or "").lower().startswith("mining")
                              and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code")})
            if working:
                (run.get("skip") or {}).pop(working[0], None)
                run.update({"target": working[0], "phase": "mining", "arrived": working[0], "since": now_iso(),
                            "note": f"rigs found mining {working[0]}: back on it"})
        target = run.get("target")
        if target and target not in open_:
            out.append(f"{target} is gone (impacted, diverted or past its ETA)")
            run.pop("target", None)
            target = None
        o = objs.get(target) if target else None
        rigs_at = [d for d in k["rigs"] if target and d.get("location") == target
                   and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code")]
        if target and not rigs_at and run.get("arrived") == target:
            # live 2026-11-16 (Chaser_1, RENGRELOR-OBJ-1): minutes after the arrival job the cached device list still showed
            # the rigs aboard and idle, so the pass sent them again; trust the job: the rigs it took there
            rigs_at = [d for d in k["rigs"] if (ctx["state"].get(d["device_code"]) or {}).get("rock") == target]
        if target and rigs_at and (rc.due_to_leave(o, cfg) or not rm.pick(o, cfg) or self._chase_better(f, cfg, run, ctx, o, target, need)):
            out += await self._chase_leave(f, cfg, run, ctx, k, target)   # due, mined down, or a rock with what's wanted waits
        elif target and rigs_at:
            run["phase"] = "mining"
            out += await self._chase_mine(f, cfg, run, ctx, k, target)
        else:   # no target, or the trip ended without the rigs there: pick (again)
            run.pop("target", None)
            out += await self._chase_move(f, cfg, run, ctx, k)
            if run.get("phase") == "idle" and not run.get("job"):
                out += await self._chase_gather(f, cfg, run, ctx, k, ms)
        mining = run.get("target") if run.get("phase") == "mining" and run.get("target") == target and rigs_at else None
        # Joe 2026-11-21: the fleet's freighters keep clearing a worked rock's pile after the rigs left, until its
        # evacuation hour, and finish an earlier rock's before the new one's (logistics never goes to a rock)
        mining = self._chase_leftover(run, ctx, mining) or mining
        out += await self._chase_ami(f, cfg, run, ctx, k, ms, mining)
        if run.get("target"):
            out += await self._chase_ship(f, cfg, run, ctx, k, run["target"])
        out += await self._chase_search(f, cfg, run, ctx, k)
        out += await self._chase_launch(f, cfg, run, ctx, k)
        out += await self._chase_refill(f, run, ctx, k)
        out += await self._chase_off_rocks(f, run, ctx, ms)
        return out

    async def _chase_off_rocks(self, f: dict, run: dict, ctx: dict, ms: list[dict]) -> list[str]:
        """Members idle at a rock the fleet isn't working (Joe 2026-11-19: freighter 2CBD7A06 sat at RENGRELOR-OBJ-1
        hours before its impact) fly to the rock's drop spot (else the fleet carrier's spot) at once; nothing waits for
        the evacuation hour."""
        from . import fleets as fl
        from . import rockchase as rc
        from .automations import step
        working = run.get("target") if run.get("phase") in ("mining", "moving") else None
        car = next((c for c in ms if fl.is_carrier(c) and not rc.is_rock(c.get("location"))), None)
        out: list[str] = []
        for d in ms:
            code, loc = d["device_code"], d.get("location") or ""
            pushing = d.get("device_type") == "propulsor" and str(d.get("status") or "").lower() not in ("idle", "inactive", "")
            if not rc.is_rock(loc) or loc == working or code in ctx["busy"] or d.get("stowed_in_device_code") \
                    or d.get("attached_to_device_code") or not rm_can_fly(d) \
                    or (str(d.get("status") or "").lower() != "idle" and not pushing):
                continue   # live 2026-11-20: Chaser_1's propulsors kept pushing RENGRELOR-OBJ-2 after it was mined out
            o = ctx["objs"].get(loc) or {}
            if pushing and self._priority_left(run, ctx, loc, o) and (rm_hours(o) or 0) > rc.SLOW_STOP_H:
                continue   # Joe 2026-11-20: keep buying time while priority materials are still on the rock or its pile
            to = (run.get("drops") or {}).get(loc) or ((car or {}).get("location") if fl.star_of((car or {}).get("location")) == fl.star_of(loc) else None)
            if not to or rc.is_rock(to):
                continue
            # Joe 2026-11-19: a hauler loads what the rock still holds before it goes
            take = rc.ship_take((ctx.get("inv") or {}).get(loc) or {}, d, rm_parse(run))
            steps = [step(f"{code}: load {', '.join(f'{q} {r}' for r, q in take.items())}", f"/devices/{code}",
                          {"command": "collect_resources", "resources": take})] if take else []
            if pushing:   # an active propulsor won't travel
                steps.append(step(f"{code}: switch off", f"/devices/{code}", {"command": "deactivate"}))
            steps += [step(f"{code} → {to}", f"/devices/{code}", {"command": "travel", "destination": to}, critical=True),
                      fl._wait_arrive(code, to)]
            if take:
                steps.append(step(f"{code}: deposit at {to}", f"/devices/{code}", {"command": "deposit_resources"}))
            job = await self.create_job("rock_chaser", f"{f['name']}: {code} off {loc} → {to}", code, steps, {"devices": [code]})
            if job:
                ctx["busy"].add(code)
                out.append(f"{code} leaves {loc} for {to}: nothing waits at a rock")
        return out

    async def _chase_slow(self, f: dict, cfg: dict, run: dict, ctx: dict, k: dict, rock: str, o: dict) -> list[str]:
        """Joe 2026-11-20: the fleet's propulsors buy more time on a rock it launched. Game docs: a propulsor set to
        `increase` or `decrease` changes the approach speed and the impact ETA (the default, `diversion`, would push the rock
        off course). Which one lengthens the flight isn't documented: they start on SLOW_DIR, and if the ETA moves earlier
        they switch to the other; if that moves it earlier too, they stop. Trajectory deflectors only change the approach
        angle (the terraform effect), not the time, so they are left alone. The evacuation takes the propulsors off at 45 min."""
        from . import fleets as fl
        from . import rockchase as rc
        from . import rockmining as rm
        from .automations import step
        mine = f"fleet {f['name']}"
        launched = (await self.db.kv_get(rc.LAUNCHED_KV, {}) or {}).get(rock) or {}
        if not cfg.get("slow_rocks") or not k["propulsors"] or launched.get("by") != mine:
            return []
        slow = run.get("slow") or {}
        eta = _ts(o.get("impact_eta"))
        if slow.get("rock") == rock and not self._priority_left(run, ctx, rock, o):
            # Joe 2026-11-20: once every priority material is mined and picked up, the propulsors stop and go; the rigs
            # use whatever time is left for the rest
            out = []
            drop = (run.get("drops") or {}).get(rock)
            for p in k["propulsors"]:
                code = p["device_code"]
                if p.get("location") != rock or code in ctx["busy"] or not drop:
                    continue
                steps = [step(f"{code}: switch off", f"/devices/{code}", {"command": "deactivate"}),
                         step(f"{code} → {drop}", f"/devices/{code}", {"command": "travel", "destination": drop}, critical=True),
                         fl._wait_arrive(code, drop)]
                if await self.create_job("rock_chaser", f"{f['name']}: {code} off {rock} → {drop}", code, steps, {"devices": [code]}):
                    ctx["busy"].add(code)
                    out.append(f"{code} stops slowing {rock}: priority materials all picked up")
            slow["note"] = "priority materials all picked up: propulsors stopped"
            slow["off"] = slow.get("off") or "done"
            return out
        if slow.get("rock") != rock:
            slow = {"rock": rock, "dir": rc.SLOW_DIR, "set": {}, "eta0": o.get("impact_eta")}
        run["slow"] = slow
        if slow.get("off") or not eta or (eta - _now()) / 3600 < rc.SLOW_STOP_H:   # the evacuation takes them off
            return []
        out: list[str] = []
        on = slow.get("eta_on")
        if on and _ts(on) and eta < _ts(on) - 120 and (_now() - (_ts(slow.get("on_at")) or _now())) > 600:
            if slow.get("flipped"):
                slow["off"] = f"both directions brought the ETA earlier ({o.get('impact_eta')}): propulsors switched off"
                for p in k["propulsors"]:
                    if p.get("location") == rock and p["device_code"] not in ctx["busy"]:
                        await self.send("POST", f"/devices/{p['device_code']}", {"command": "deactivate"},
                                        f"auto: rock chaser stops slowing {rock}")
                await self.log("rock_chaser", f"{f['name']}: {slow['off']}", "warning")
                return [slow["off"]]
            old = slow["dir"]
            slow.update({"dir": "increase" if old == "decrease" else "decrease", "flipped": True, "set": {}, "eta_on": None})
            await self.log("rock_chaser", f"{f['name']}: propulsors on {old} brought {rock}'s ETA earlier: switching to {slow['dir']}")
            out.append(f"propulsors on {old} brought {rock}'s ETA earlier: switching to {slow['dir']}")
        sent = slow.setdefault("set", {})
        for p in k["propulsors"]:
            code, loc = p["device_code"], p.get("location") or ""
            if code in ctx["busy"] or p.get("stowed_in_device_code") or p.get("attached_to_device_code"):
                continue
            active = str(p.get("status") or "").lower() not in ("idle", "inactive", "")
            if loc != rock:
                if fl.star_of(loc) != fl.star_of(rock) or not rm_can_fly(p):
                    continue
                steps = ([step(f"{code}: switch off", f"/devices/{code}", {"command": "deactivate"})] if active else []) + \
                    [step(f"{code} → {rock}", f"/devices/{code}", {"command": "travel", "destination": rock}, critical=True),
                     fl._wait_arrive(code, rock)]
                sent.pop(code, None)
                title, line = f"{f['name']}: {code} → {rock} to slow it", f"{code} → {rock} to slow it"
            elif sent.get(code) != slow["dir"]:
                steps = [step(f"{code}: direction {slow['dir']}", f"/devices/{code}",
                              {"configuration": {"settings": {"direction": slow["dir"]}}}, method="PATCH", critical=True),
                         step(f"{code}: switch on", f"/devices/{code}", {"command": "activate"})]
                sent[code] = slow["dir"]
                if not slow.get("eta_on"):
                    slow["eta_on"] = o.get("impact_eta")
                slow["on_at"] = now_iso()
                title, line = f"{f['name']}: {code} slows {rock} ({slow['dir']})", f"{code} pushes {rock} ({slow['dir']})"
            else:
                continue
            if await self.create_job("rock_chaser", title, code, steps, {"devices": [code]}):
                ctx["busy"].add(code)
                out.append(line)
        if slow.get("eta0") and _ts(slow["eta0"]):
            gained = (eta - _ts(slow["eta0"])) / 60
            slow["note"] = f"propulsors on {slow['dir']}: ETA {'+' if gained >= 0 else ''}{gained:.0f} min since launch"
        return out

    def _priority_left(self, run: dict, ctx: dict, rock: str, o: dict) -> bool:
        """A priority material (the hub's `LOW_COUNT` lowest) is still on the rock or in its pile waiting for pickup."""
        from . import rockchase as rc
        from . import rockmining as rm
        low = ((run.get("need") or {}).get("order") or rm.RESOURCES)[:rc.LOW_COUNT]
        left = rm.resources_left(o)
        pile = (ctx.get("inv") or {}).get(rock) or {}
        return any(float(left.get(r) or 0) > 0 or float(pile.get(r) or 0) > 0 for r in low)

    def _chase_better(self, f: dict, cfg: dict, run: dict, ctx: dict, o: dict, rock: str, need: list[str]) -> bool:
        """Joe 2026-11-19: a less wanted resource is mined only while nothing better is to be had. With follow_need on, rigs
        whose rock has none of the stockpile's `LOW_COUNT` lowest left move on when another rock in the area holds one."""
        from . import rockchase as rc
        from . import rockmining as rm
        if not cfg.get("follow_need"):
            return False
        low = need[:rc.LOW_COUNT]
        if rm.pick(o, cfg) in low:
            return False
        area = rc.area(cfg, f["home"], ctx["cover"], ctx["pos"])
        taken = {(x.get("chase_run") or {}).get("target") for x in ctx["fleets"] if x is not f} - {None}
        cands = rc.candidates(ctx["objs"], cfg, area, rock, ctx["pos"], taken | set(run.get("skip") or {}) | {rock},
                              set(ctx["full"]))
        better = next((c for c in cands if c["resource"] in low), None)
        if better:
            run["note"] = f"{better['rock']} holds {better['resource']}: leaving {rock}'s {rm.pick(o, cfg)}"
        return bool(better)

    def _chase_need(self, f: dict, run: dict, ctx: dict, k: dict) -> list[str]:
        """Resources by how little the nearest printing hub's stockpile holds (the fleet's haul ends up there; no hub
        known: the fleet's own system), least first. Kept in run["need"] for the card."""
        from . import balance
        from . import rockmining as rm
        star = rm.star_of(self._chase_where(k, ctx["devices"])) or f["home"]
        hub = balance.nearest(star, ctx.get("hubs") or [], ctx["pos"]) or star
        stock: dict[str, float] = {}
        for loc, items in (ctx.get("inv") or {}).items():
            if rm.star_of(loc) == hub:
                for r, q in (items or {}).items():
                    try:
                        stock[r] = stock.get(r, 0.0) + float(q or 0)
                    except (TypeError, ValueError):
                        pass
        from . import rockchase as rc
        order = rc.need_order(stock)
        run["need"] = {"hub": hub, "order": order, "stock": {r: int(stock.get(r, 0)) for r in order}}
        return order

    async def _chase_refill(self, f: dict, run: dict, ctx: dict, k: dict) -> list[str]:
        """Types the fleet is short of (a vector charge used up by a launch, a lost freighter) come from the nearest
        spares by logistics, as for a terraformer at work (tf_engine._tf_fetch): delivered to where the fleet's carrier
        waits in its home system, and they join on arrival. The loadout pass never fills a chaser (it isn't stationed)."""
        from . import fleets as fl
        from . import rockchase as rc
        fetch = run.setdefault("fetch", {})
        spot = run.get("fetch_at") if fetch else None
        if not spot:   # the home drop: logistics doesn't unload at the Kuiper belt; the gather takes them on from there
            spot = self._chase_drop(rc.settings(f), f["home"], ctx)
            if not spot or rc.is_rock(spot):
                if not fetch:
                    run.pop("fetch", None)
                return []
        if not fetch and not fl.short_list(f, ctx["devices"]):
            run.pop("fetch", None)
            run.pop("fetch_at", None)
            return []
        steps: list[dict] = []
        out = await self._tf_fetch(f, spot, run, ctx, [], fetch, steps)
        if fetch:
            run["fetch_at"] = spot
        else:
            run.pop("fetch", None)
            run.pop("fetch_at", None)
        if steps:
            codes = sorted({s["path"].rsplit("/", 1)[-1] for s in steps} - {""})
            job = await self.create_job("rock_chaser", f"{f['name']}: members for {spot}", codes[0], steps,
                                        {"devices": codes, "fleet": f["id"]})
            if job:
                ctx["busy"] |= set(codes)
        return out

    # --- choosing and reaching a target -------------------------------------------------------------------------
    def _chase_where(self, k: dict, devices: list[dict]) -> str | None:
        from . import fleets as fl
        lead = (k["carriers"] or k["rigs"])[0]
        return fl.tug(lead, devices).get("location")

    async def _chase_pick(self, f: dict, cfg: dict, run: dict, ctx: dict, k: dict, here: str | None,
                          exclude: set[str] = frozenset()) -> dict | None:
        """The best target the fleet can reach in time (the game's preview of the trip), or None (run["note"] says why)."""
        from . import rockchase as rc
        area = rc.area(cfg, f["home"], ctx["cover"], ctx["pos"])
        taken = {(x.get("chase_run") or {}).get("target") for x in ctx["fleets"] if x is not f} - {None}
        cands = rc.candidates(ctx["objs"], cfg, area, here, ctx["pos"], taken | set(run.get("skip") or {}) | set(exclude),
                              set(ctx["full"]))
        car = k["carriers"][0] if k["carriers"] else None
        mover = car or k["rigs"][0]
        for c in cands[:3]:
            if c["star"] != (here or "").split("-")[0] and not car:
                run["note"] = f"{c['rock']} is in another system: the fleet needs a carrier to go there"
                continue
            secs = await self.trip_seconds(mover["device_code"], c["rock"])
            if not rc.trip_ok(ctx["objs"][c["rock"]], secs, cfg):
                run.setdefault("skip", {})[c["rock"]] = now_iso()
                run["note"] = f"{c['rock']}: the {secs / 3600:.1f} h trip leaves under {cfg['min_hours']} h of mining"
                continue
            return {**c, "trip_s": secs}
        if not cands:
            where = " (home only)" if cfg["mode"] == "hub" else f" within {cfg['radius']:g} ly of {f['home']}"
            from . import rockmining as rm
            if not any(rm.star_of(r) in area for r in rm.open_rocks(ctx["objs"])):
                # the app hears of a rock only from a detection (a hub in its system) or the one-hour impact warning
                run["note"] = (f"no asteroid in flight is known in {len(area)} system(s){where}: rocks are seen only when a "
                               "hub in their system detects them")
            else:
                run["note"] = f"no rock meets the criteria in {len(area)} system(s){where}"
        return None

    def _passengers(self, k: dict, car: dict | None, devices: list[dict], busy: set[str],
                    keep: set[str] | frozenset = frozenset()) -> tuple[dict, list, list[str]]:
        """Who rides the carrier: {location: [(code, mode)]} to pick up, [(code, mode)] for everyone aboard after, and the
        codes left out. Self-flying shippers (cargo freighters) fly on their own."""
        from . import fleets as fl
        riders = k["rigs"] + k["surveyors"] + k["sensors"] + k["charges"] + [s for s in k["shippers"] if not fl.flies_itself(s)]
        if not car:
            return {}, [], []
        cc = car["device_code"]
        free = fl.seats(car, devices)
        stops: dict[str, list[tuple[str, str]]] = {}
        aboard, left = [], []
        for d in riders:
            code = d["device_code"]
            if code in keep and not (d.get("attached_to_device_code") == cc or d.get("stowed_in_device_code") == cc):
                continue   # stays at the Kuiper belt (searching, ready to launch)
            if d.get("attached_to_device_code") == cc:
                aboard.append((code, "attach"))
                continue
            if d.get("stowed_in_device_code") == cc:
                aboard.append((code, "stow"))
                continue
            if code in busy or d.get("stowed_in_device_code") or d.get("attached_to_device_code") or not d.get("location"):
                left.append(code)
                continue
            mode = fl.take_seat(free, d)
            if not mode:
                left.append(code)
                continue
            stops.setdefault(d["location"], []).append((code, mode))
            aboard.append((code, mode))
        return stops, aboard, left

    def _unload(self, car: str, aboard: list[tuple[str, str]]) -> list[dict]:
        from .automations import SHORT_TIMEOUT, step
        out = []
        for code, mode in aboard:
            if mode == "attach":
                out.append(step(f"{car}: detach {code}", f"/devices/{car}", {"command": "detach", "device": code}))
            else:
                st = step(f"deploy {code} from {car}", f"/devices/{code}", {"command": "deploy"}, wait=["device.deployed"],
                          timeout=SHORT_TIMEOUT)
                st["wait_device"] = code
                out.append(st)
        return out

    def _arrive_steps(self, k: dict, aboard: list[tuple[str, str]], car: str | None, rock: str, res: str) -> list[dict]:
        """At the rock: everyone off the carrier, the rigs mine `res`."""
        from .automations import step
        steps = self._unload(car, aboard) if car else []
        on = {c for c, _ in aboard} if car else {d["device_code"] for d in k["rigs"]}
        for d in k["rigs"]:
            if d["device_code"] in on:
                steps.append(step(f"{d['device_code']}: mine {res} at {rock}", f"/devices/{d['device_code']}",
                                  {"command": "start_mining", "resource_type": res}))
        return steps

    def _remember(self, run: dict, ctx: dict, k: dict, c: dict, codes: set[str]) -> None:
        for r in (run.pop("arrived", None), run.get("target")):   # its pile is cleared before the new rock's (Joe 2026-11-21)
            if r and r != c["rock"] and r not in (run.get("left") or []):
                run.setdefault("left", []).append(r)
        run.update({"target": c["rock"], "resource": c["resource"], "star": c["star"], "since": now_iso(), "phase": "moving",
                    "note": f"→ {c['rock']} to mine {c['resource']} ({c['mine_h']:.1f} h of mining)"})
        for d in k["rigs"]:
            if d["device_code"] in codes:   # logistics hauls a worked rock's pile, evacuation knows the rigs are there
                ctx["state"][d["device_code"]] = {"rock": c["rock"], "resource": c["resource"], "at": now_iso(), "sent": True}

    async def _chase_move(self, f: dict, cfg: dict, run: dict, ctx: dict, k: dict) -> list[str]:
        from . import fleets as fl
        from .automations import step
        devices, busy = ctx["devices"], ctx["busy"]
        here = self._chase_where(k, devices)
        c = await self._chase_pick(f, cfg, run, ctx, k, here)
        if not c:
            run["phase"] = "idle"
            return []
        rock = c["rock"]
        car = k["carriers"][0] if k["carriers"] else None
        steps: list[dict] = []
        for d in k["rigs"]:   # rigs still mining somewhere else stop first
            if str(d.get("status") or "").lower().startswith("mining") and d["device_code"] not in busy:
                steps.append(step(f"{d['device_code']}: stop mining", f"/devices/{d['device_code']}", {"command": "deactivate"}))
        if car:
            if car["device_code"] in busy:
                run["note"] = f"waiting for the carrier {car['device_code']} (busy)"
                return []
            cc = car["device_code"]
            keep: set[str] = set()
            if cfg.get("stage_kuiper") and c["star"] == f["home"]:
                keep = {d["device_code"] for d in k["sensors"] + k["charges"]}
            stops, aboard, left = self._passengers(k, car, devices, busy, keep)
            tour, _at = fl.pickup_tour(cc, car.get("location"), stops)
            steps += tour
            steps += [step(f"{cc} → {rock}", f"/devices/{cc}", {"command": "travel", "destination": rock}, critical=True),
                      fl._wait_arrive(cc, rock)]
            steps += self._arrive_steps(k, aboard, cc, rock, c["resource"])
            codes = {cc} | {x for x, _ in aboard}
        else:   # in its own system the rigs fly themselves
            fly = [d for d in k["rigs"] + k["surveyors"] if d["device_code"] not in busy and fl.tug(d, devices) is d]
            steps += [step(f"{d['device_code']} → {rock}", f"/devices/{d['device_code']}",
                           {"command": "travel", "destination": rock}, critical=d in k["rigs"]) for d in fly]
            steps += [fl._wait_arrive(d["device_code"], rock) for d in fly]
            steps += self._arrive_steps(k, [], None, rock, c["resource"])
            codes, left = {d["device_code"] for d in fly}, []
        job = await self.create_job("rock_chaser", f"{f['name']}: chase {rock} ({c['resource']})", (car or k["rigs"][0])["device_code"],
                                    steps, {"devices": sorted(codes)})
        if not job:
            return []
        self._remember(run, ctx, k, c, codes)
        run["job"] = job["id"]
        if left:
            run["note"] += f"; left behind: {', '.join(left)} (busy, aboard another device or no seat)"
        out = [f"→ {rock} to mine {c['resource']}"]
        out += await self._chase_freighters(f, cfg, run, ctx, k, c["star"])
        return out

    async def _chase_gather(self, f: dict, cfg: dict, run: dict, ctx: dict, k: dict, ms: list[dict]) -> list[str]:
        """No target: members away from home come home, so the fleet sets out together next time (Joe 2026-11-14,
        Chaser_1 split over SOLARES and FALQUORYX). The fleet's carriers fetch them from wherever they are within the
        sector range of home, nearest carrier first; self-flying members fly home on their own."""
        from . import fleets as fl
        from .automations import step
        from .bootstrap import dist
        from .logistics import SECTOR_RANGE_LY
        if (f.get("mission") or {}).get("status") in ("running", "stalled", "stopped"):
            return []   # a fleet mission (recall, end mission) has the members
        from . import rockchase as rc
        home, devices, busy, pos = f["home"], ctx["devices"], ctx["busy"], ctx["pos"]
        drop = self._chase_drop(cfg, home, ctx)
        if not drop:
            return []
        # Joe 2026-11-20: everything waits at the Kuiper belt (sensor sweeping it, close to a rock when it launches), but
        # the haulers and the AMI controllers, which run the supply ferry from the drop
        stage = rc.stage_spot(ctx["scans"].get(home)) if cfg.get("stage_kuiper") else None

        def spot_for(d: dict) -> str:
            return stage if stage and rc.stages(d) else drop

        def star(loc: str | None) -> str:
            return (loc or "").split("-")[0]

        def near(st: str) -> bool:
            return st == home or (st in ctx["reach"] and dist(st, home, pos) <= SECTOR_RANGE_LY)
        carriers = {c["device_code"]: c for c in k["carriers"]}
        free = {cc: fl.seats(c, devices) for cc, c in carriers.items()}
        aboard: dict[str, list[tuple[str, str]]] = {cc: [] for cc in carriers}
        stops: dict[str, dict[str, list[tuple[str, str]]]] = {cc: {} for cc in carriers}
        fly, left = [], []
        for d in ms:
            code = d["device_code"]
            if code in carriers:
                continue
            t = fl.tug(d, devices)
            if t is not d:
                if t.get("device_code") in carriers:
                    aboard[t["device_code"]].append((code, "attach" if d.get("attached_to_device_code") else "stow"))
                continue
            st = star(d.get("location"))
            if not d.get("location") or code in busy or (st == home and (d["location"] == spot_for(d) or spot_for(d) == drop)):
                continue   # in place (a member that waits at the drop may wait anywhere at home, as before)
            if st == home and (rc.is_rock(d["location"]) or str(d.get("status") or "").lower() not in ("idle", "inactive")):
                continue   # at a rock: _chase_off_rocks moves it; working: left alone
            if not near(st):
                left.append(code)
            elif fl.flies_itself(d):
                fly.append(d)
            else:
                cand = sorted((cc for cc in carriers if cc not in busy and near(star(carriers[cc].get("location")))),
                              key=lambda cc: (star(carriers[cc].get("location")) != st,
                                              dist(star(carriers[cc].get("location")), st, pos), cc))
                cc = mode = None
                for c in cand:
                    mode = fl.take_seat(free[c], d)
                    if mode:
                        cc = c
                        break
                if cc:
                    stops[cc].setdefault(d["location"], []).append((code, mode))
                    aboard[cc].append((code, mode))
                else:
                    left.append(code)
        out = []
        for cc, c in carriers.items():
            if cc in busy or not near(star(c.get("location"))):
                continue
            dest = stage or drop
            if not stops[cc] and (c.get("location") == dest or (not stage and star(c.get("location")) == home)):
                continue   # in place; what it carries goes along on the next chase
            if stage and not stops[cc] and not aboard[cc] and star(c.get("location")) == home and c.get("location") != dest \
                    and str(c.get("status") or "").lower() not in ("idle", "inactive"):
                continue
            tour, _at = fl.pickup_tour(cc, c.get("location"), stops[cc])
            steps = tour + [step(f"{cc} → {dest}", f"/devices/{cc}", {"command": "travel", "destination": dest}, critical=True),
                            fl._wait_arrive(cc, dest)] + self._unload(cc, aboard[cc])
            codes = {cc} | {x for x, _ in aboard[cc]}
            if await self.create_job("rock_chaser", f"{f['name']}: {cc} gathers the fleet at {home}", cc, steps,
                                     {"devices": sorted(codes)}):
                busy.update(codes)
                n = len(aboard[cc])
                out.append(f"{cc} brings {n} member(s) home to {dest}" if n else f"{cc} flies home to {dest}")
        for d in fly:
            code, to = d["device_code"], spot_for(d)
            steps = []
            if d.get("device_type") == "propulsor" and str(d.get("status") or "").lower() not in ("idle", "inactive"):
                steps.append(step(f"{code}: switch off", f"/devices/{code}", {"command": "deactivate"}))   # it won't travel on
            steps += [step(f"{code} → {to}", f"/devices/{code}", {"command": "travel", "destination": to}, critical=True),
                      fl._wait_arrive(code, to)]
            if await self.create_job("rock_chaser", f"{f['name']}: {code} → {to} (home)", code, steps, {"devices": [code]}):
                busy.add(code)
                out.append(f"{code} flies home to {to}")
        if out or left:
            run["note"] = (run.get("note") or "") + (f"; gathering at {home}" if out else "") + \
                (f"; can't fetch: {', '.join(left)} (out of reach, beyond {SECTOR_RANGE_LY:g} ly or no free seat)" if left else "")
        return out

    async def _chase_freighters(self, f: dict, cfg: dict, run: dict, ctx: dict, k: dict, star: str) -> list[str]:
        """Self-flying shippers in another system follow the fleet to the target system's drop."""
        from . import fleets as fl
        from .automations import step
        out = []
        drop = self._chase_drop(cfg, run.get("target") or star, ctx, run)
        for s in k["shippers"]:
            code = s["device_code"]
            if not fl.flies_itself(s) or code in ctx["busy"] or (s.get("location") or "").split("-")[0] == star or not drop:
                continue
            if await self.create_job("rock_chaser", f"{f['name']}: {code} → {drop} (ships the pile)", code,
                                     [step(f"{code} → {drop}", f"/devices/{code}", {"command": "travel", "destination": drop},
                                           critical=True), fl._wait_arrive(code, drop)], {"devices": [code]}):
                out.append(f"{code} → {drop} to ship for the fleet")
        return out

    # --- at the rock ------------------------------------------------------------------------------------------------
    async def _chase_mine(self, f: dict, cfg: dict, run: dict, ctx: dict, k: dict, rock: str) -> list[str]:
        from . import rockchase as rc
        from . import rockmining as rm
        o = ctx["objs"][rock]
        codes = {d["device_code"] for d in k["rigs"] + k["surveyors"]}
        mine_state = {c: s for c, s in ctx["state"].items() if c in codes}
        hours = rm.hours_left(o, _now()) or 0
        rcfg = {**cfg, "own_fleet": True, "send_idle": False, "scan_rocks": hours - rc.leave_h(cfg) > 1.0, "scan_minutes": 30}
        objs = {rock: o}   # only its own target
        p = rm.plan(objs, k["rigs"] + k["surveyors"], rcfg, ctx["busy"], mine_state, ctx["hosts"], set(ctx["full"]),
                    ctx["scanned"])
        out = await self.rock_commands(p, ctx["state"], ctx["full"], rule="rock_chaser")
        out += await self.rock_scan_jobs(p["scan"], rule="rock_chaser")
        out += await self._chase_slow(f, cfg, run, ctx, k, rock, o)
        if rock in ctx["full"]:
            run["note"] = f"{rock}: the game says its slots are taken"
        else:
            run["note"] = f"mining {run.get('resource') or rm.pick(o, cfg) or '?'} at {rock}, leaving in " \
                          f"{max(0.0, hours - rc.leave_h(cfg)):.1f} h"
        return out

    def _chase_drop(self, cfg: dict, rock: str, ctx: dict, run: dict | None = None) -> str | None:
        """Where shipped materials go: the card's drop (unless it is the impact target), else the spot with the shortest
        round trip found for this rock (_chase_pick_drop), else the system's safe spot. Never the impact target."""
        from . import evacuate as ev
        from . import rockchase as rc
        star = rock.split("-")[0]
        o = ctx["objs"].get(rock) or {}
        drop = str(cfg.get("drop") or "").strip().upper()
        if drop and drop.split("-")[0] == star and not rc.near_target(drop, o.get("impact_target")) and not rc.is_rock(drop):
            return drop
        best = ((run or {}).get("drops") or {}).get(rock)
        if best and not rc.near_target(best, o.get("impact_target")) and not rc.is_rock(best):
            return best
        from .rockmining import open_rocks
        hot = set(open_rocks(ctx["objs"]))
        return ev.safe_spot(star, o.get("impact_target"), ctx["stars"], ctx["scans"].get(star), here=rock, avoid=hot)

    def _chase_leftover(self, run: dict, ctx: dict, mining: str | None = None) -> str | None:
        """The oldest rock the rigs worked and left (run.left, then run.arrived) while a pile is left on it and its
        evacuation hour hasn't come (rm.CUTOFF_H). Rocks gone, past that hour or emptied drop off run.left."""
        from . import rockmining as rm
        from .shapes import as_amounts
        open_ = rm.open_rocks(ctx["objs"])

        def usable(rock: str | None) -> bool:
            o = open_.get(rock) if rock else None
            return bool(o) and (rm.hours_left(o, _now()) or 0) >= rm.CUTOFF_H + 0.1 \
                and sum(q for q in as_amounts(ctx["inv"].get(rock) or {}).values() if q > 0) >= 1
        left = [r for r in run.get("left") or [] if r != mining and usable(r)]
        if left:
            run["left"] = left
        else:
            run.pop("left", None)
        if left:
            return left[0]
        rock = run.get("arrived")
        return rock if rock != mining and usable(rock) else None

    async def _chase_ami(self, f: dict, cfg: dict, run: dict, ctx: dict, k: dict, ms: list[dict], rock: str | None) -> list[str]:
        """Joe 2026-11-16: the fleet's AMI transport controller runs every fleet freighter. While the rigs mine a rock it
        shuttles rock → best drop (never the impact target); otherwise its shuttle is cleared and the supply ferry
        (loadouts) takes it back: the drop's pile to the nearest hub. Unverified live: a shuttle collecting at an
        asteroid object (STAR-OBJ-n)."""
        from . import rockchase as rc
        from .automations import step
        ctrl = next((d for d in ms if "transport_controller" in (d.get("device_type") or "")), None)
        cur = run.get("ami") or {}
        if not ctrl or ctrl["device_code"] in ctx["busy"]:
            return []
        code = ctrl["device_code"]
        steps: list[dict] = []
        if rock:
            star = rock.split("-")[0]
            if (ctrl.get("location") or "").split("-")[0] != star or ctrl.get("stowed_in_device_code") \
                    or ctrl.get("attached_to_device_code"):
                run["ami_note"] = f"{code} is not deployed in {star}: the freighters ship by the app's own trips"
                return []
            await self._chase_pick_drop(cfg, run, ctx, k, rock)
            drop = self._chase_drop(cfg, rock, ctx, run)
            if not drop:
                return []
            free = [d for d in k["shippers"] + k["ferried"] if (d.get("location") or "").split("-")[0] == star
                    and d.get("controller_device_code") != code and d["device_code"] not in ctx["busy"]
                    and d["device_code"] not in (cur.get("adopted") or [])   # the cached list lags the adopt
                    and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code")]
            if cur.get("ctrl") == code and cur.get("collect") == rock and cur.get("deliver") == drop and not free:
                return []
            others: dict[str, list[str]] = {}
            for d in free:
                if d.get("controller_device_code"):
                    others.setdefault(d["controller_device_code"], []).append(d["device_code"])
            for other, codes in sorted(others.items()):
                steps.append(step(f"{other}: release {', '.join(codes)} to {code}", f"/devices/{other}",
                                  {"command": "release", "devices": codes}))
            if free:
                steps.append(step(f"{code}: adopt {len(free)} freighter(s)", f"/devices/{code}",
                                  {"command": "adopt", "devices": [d["device_code"] for d in free]}))
            if not (cur.get("ctrl") == code and cur.get("collect") == rock and cur.get("deliver") == drop):
                steps += [step(f"{code}: shuttle {rock} → {drop}", f"/devices/{code}",
                               {"command": "set_directive", "directive": "shuttle",
                                "configuration": {"collect": rock, "deliver": drop}}, critical=True),
                          step(f"{code}: launch", f"/devices/{code}", {"command": "launch"})]
            title, line = f"{f['name']}: {code} shuttles {rock} → {drop}", f"{code} shuttles {rock} → {drop}"
            new = {"ctrl": code, "collect": rock, "deliver": drop, "at": now_iso(),
                   "adopted": sorted(set(cur.get("adopted") or []) | {d["device_code"] for d in free})}
        else:
            if not cur.get("ctrl"):
                return []
            steps = [step(f"{code}: clear the rock shuttle", f"/devices/{code}", {"command": "clear_directive"})]
            rock_at, drop_at = cur.get("collect"), cur.get("deliver")
            if drop_at and not rc.is_rock(drop_at):   # nobody waits at the rock for cargo once the shuttle stops (Joe 2026-11-16)
                for d in k["shippers"] + k["ferried"]:
                    if d.get("location") == rock_at and d["device_code"] not in ctx["busy"] \
                            and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code"):
                        steps.append(step(f"{d['device_code']} → {drop_at} (leaves {rock_at})", f"/devices/{d['device_code']}",
                                          {"command": "travel", "destination": drop_at}))
            title, line = f"{f['name']}: {code} back to the supply ferry", f"{code}: shuttle cleared, the supply ferry takes over"
            new = None
        job = await self.create_job("rock_chaser", title, code, steps, {"devices": [code]})
        if not job:
            return []
        ctx["busy"].add(code)
        if new:
            sent = await self.db.kv_get("loadout_ferries", {}) or {}
            if sent.pop(code, None) is not None:   # else the loadout planner keeps reading it as its ferry and sends that again
                await self.db.kv_set("loadout_ferries", sent)
            run["ami"] = new
        else:
            run.pop("ami", None)
        run.pop("ami_note", None)
        return [line]

    async def _ferry_collect(self, k: dict, star: str) -> str | None:
        """Where a fleet controller's ferry directive collects in `star`, from its latest transport digest."""
        import json as _json
        codes = sorted({d.get("controller_device_code") for d in k.get("ferried") or [] if d.get("controller_device_code")})
        for code in codes:
            row = await self.db.fetchone("SELECT payload FROM events WHERE event='ami.transport.digest' AND device_code=? "
                                         "ORDER BY seq DESC LIMIT 1", (code,))
            try:
                p = _json.loads(row["payload"] or "{}") if row else {}
            except ValueError:
                continue
            collect = str((p.get("report") or {}).get("collect") or "")
            if p.get("directive") == "ferry" and collect.split("-")[0] == star:
                return collect
        return None

    async def _chase_pick_drop(self, cfg: dict, run: dict, ctx: dict, k: dict, rock: str) -> None:
        """Joe 2026-11-16: drop at a permanent spot with the shortest round trip, never at the impact target. Once per rock:
        the game's trip preview from a fleet member at the rock to each candidate (rockchase.drop_candidates)."""
        from . import rockchase as rc
        drops = run.setdefault("drops", {})
        if str(cfg.get("drop") or "").strip():
            return
        star = rock.split("-")[0]
        o = ctx["objs"].get(rock) or {}
        collect = await self._ferry_collect(k, star)
        if rc.is_rock(drops.get(rock)):
            drops.pop(rock)   # 1.440.8 took a ferry collecting at the rock itself for the drop (Chaser_1 lost 3 freighters)
        if collect and not rc.near_target(collect, o.get("impact_target")) and not rc.is_rock(collect):
            drops[rock] = collect   # a fleet ferry picks up there: drop where it collects (Joe 2026-11-16, 661BF2A6)
            return
        if rock in drops:
            return
        from .rockmining import open_rocks
        cands = rc.drop_candidates(star, o.get("impact_target"), (ctx["stars"].get(star) or {}).get("entry_point"),
                                   ctx["scans"].get(star), avoid=set(open_rocks(ctx["objs"])) | {rock})
        here = [d for d in k["shippers"] + k["rigs"] if d.get("location") == rock and not d.get("stowed_in_device_code")
                and not d.get("attached_to_device_code") and "travel" in (d.get("available_commands") or [])]
        if not cands or not here:
            return
        code = here[0]["device_code"]
        best: tuple[float, str] | None = None
        for spot in cands:
            secs = await self.trip_seconds(code, spot)
            if secs is not None and (best is None or secs < best[0]):
                best = (secs, spot)
        if best:
            drops[rock] = best[1]
            for r in [r for r in drops if r != rock and r not in ctx["objs"]]:
                drops.pop(r)

    async def _chase_ship(self, f: dict, cfg: dict, run: dict, ctx: dict, k: dict, rock: str, last: bool = False) -> list[str]:
        """A shipper in the system takes the rock's pile to the drop once it reaches `ship_min` (`last`: whatever is there)."""
        from . import fleets as fl
        from . import rockchase as rc
        from . import rockmining as rm
        from .automations import step
        from .shapes import as_amounts
        pile = {r: q for r, q in as_amounts(ctx["inv"].get(rock) or {}).items() if q > 0}
        total = sum(pile.values())
        o = ctx["objs"].get(rock) or {}
        if not total or (not last and total < float(cfg.get("ship_min") or 0)):
            return []
        if (rm.hours_left(o, _now()) or 0) < rm.CUTOFF_H + 0.1:
            return []   # too late: the defense evacuation hands the pile to logistics
        star = rock.split("-")[0]
        await self._chase_pick_drop(cfg, run, ctx, k, rock)
        drop = self._chase_drop(cfg, rock, ctx, run)
        if not drop:
            return []
        prio = rm.parse_list(cfg.get("priority"))
        for s in k["shippers"]:
            code = s["device_code"]
            at = fl.tug(s, ctx["devices"]).get("location") or ""
            if code in ctx["busy"] or s.get("stowed_in_device_code") or s.get("attached_to_device_code") or at.split("-")[0] != star:
                continue
            take = rc.ship_take(pile, s, prio)
            if not take:
                continue
            steps = []
            if at != rock:
                steps += [step(f"{code} → {rock}", f"/devices/{code}", {"command": "travel", "destination": rock}, critical=True),
                          fl._wait_arrive(code, rock)]
            steps += [step(f"{code}: load {', '.join(f'{q} {r}' for r, q in take.items())}", f"/devices/{code}",
                           {"command": "collect_resources", "resources": take}, critical=True),
                      step(f"{code} → {drop}", f"/devices/{code}", {"command": "travel", "destination": drop}, critical=True),
                      fl._wait_arrive(code, drop),
                      step(f"{code}: deposit at {drop}", f"/devices/{code}", {"command": "deposit_resources"})]
            if await self.create_job("rock_chaser", f"{f['name']}: {code} ships {sum(take.values())} from {rock} to {drop}",
                                     code, steps, {"devices": [code]}):
                ctx["busy"].add(code)
                return [f"{code} ships {sum(take.values())} from {rock} to {drop}"]
        if not run.get("ship_note"):
            run["ship_note"] = "no free hauler in the system: logistics hauls the pile out"
        return []

    async def _chase_leave(self, f: dict, cfg: dict, run: dict, ctx: dict, k: dict, rock: str) -> list[str]:
        """`leave_minutes` before the ETA: the rigs stop, everyone boards the carrier at once and it flies to the next target
        (deployed and mining there) or the system's safe spot; the last load ships out beside it."""
        from . import fleets as fl
        from .automations import step
        devices, busy = ctx["devices"], ctx["busy"]
        out = await self._chase_ship(f, cfg, run, ctx, k, rock, last=True)
        car = k["carriers"][0] if k["carriers"] else None
        c = await self._chase_pick(f, cfg, run, ctx, k, rock, exclude={rock})
        dest = c["rock"] if c else self._chase_drop(cfg, rock, ctx, run)
        if not dest:
            run["note"] = f"nowhere safe to go from {rock}: the defense evacuation takes over"
            return out
        steps = [step(f"{d['device_code']}: stop mining", f"/devices/{d['device_code']}", {"command": "deactivate"})
                 for d in k["rigs"] if d.get("location") == rock and d["device_code"] not in busy
                 and str(d.get("status") or "").lower().startswith("mining")]
        here = [d for d in k["rigs"] + k["surveyors"] + k["sensors"] + k["charges"]
                if d.get("location") == rock and d["device_code"] not in busy
                and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code")]
        codes = {d["device_code"] for d in here}
        if car and car["device_code"] not in busy:
            cc = car["device_code"]
            if car.get("location") != rock:
                steps += [step(f"{cc} → {rock} (pick up the rigs)", f"/devices/{cc}", {"command": "travel", "destination": rock},
                               critical=True), fl._wait_arrive(cc, rock)]
            free = fl.seats(car, devices)
            aboard = [(d["device_code"], m) for d, m in ((d, fl.take_seat(free, d)) for d in here) if m]
            seated = {x for x, _ in aboard}
            steps += [fl.board_step(cc, x, m) for x, m in aboard]
            safe = self._chase_drop(cfg, rock, ctx, run)
            self_fly = [d for d in here if d["device_code"] not in seated and rm_can_fly(d)] if safe else []
            steps += [step(f"{d['device_code']} → {safe} (no seat)", f"/devices/{d['device_code']}",
                           {"command": "travel", "destination": safe}) for d in self_fly]
            steps += [step(f"{cc} → {dest}", f"/devices/{cc}", {"command": "travel", "destination": dest}, critical=True),
                      fl._wait_arrive(cc, dest)]
            if c:
                steps += self._arrive_steps(k, aboard, cc, dest, c["resource"])
            codes |= {cc}
            stuck = [d["device_code"] for d in here if d["device_code"] not in seated and not rm_can_fly(d)]
        else:
            fly = [d for d in here if rm_can_fly(d) and (not c or c["star"] == rock.split("-")[0])]
            steps += [step(f"{d['device_code']} → {dest}", f"/devices/{d['device_code']}",
                           {"command": "travel", "destination": dest}, critical=d in k["rigs"]) for d in fly]
            steps += [fl._wait_arrive(d["device_code"], dest) for d in fly]
            if c:
                steps += self._arrive_steps(k, [], None, dest, c["resource"])
            stuck = [d["device_code"] for d in here if d not in fly]
        lead = car or (here[0] if here else k["rigs"][0])
        job = await self.create_job("rock_chaser", f"{f['name']}: leave {rock} → {dest}", lead["device_code"], steps,
                                    {"devices": sorted(codes)})
        for x in codes:
            ctx["state"].pop(x, None)
        if not job:
            return out
        run["job"] = job["id"]
        if c:
            self._remember(run, ctx, k, c, codes)
            run["note"] = f"left {rock} for {dest} to mine {c['resource']}"
        else:
            run.update({"phase": "leaving", "note": f"left {rock} for {dest}: no next target yet"})
            run.pop("target", None)
        if stuck:
            run["note"] += f"; can't move {', '.join(stuck)} (no seat, can't fly): the defense evacuation tries at the hour"
        return out + [f"leaving {rock} → {dest}"]

    # --- searching the Kuiper belt, launching marked objects -------------------------------------------------------
    async def _chase_search(self, f: dict, cfg: dict, run: dict, ctx: dict, k: dict) -> list[str]:
        from . import rockchase as rc
        out: list[str] = []
        marks = run.setdefault("marked", {})
        for des in [d for d, m in marks.items() if not _ts(m.get("found_at")) or _now() - _ts(m["found_at"]) > rc.KUIPER_HOURS * 3600]:
            marks.pop(des)
        searched, read = run.setdefault("searched", {}), run.setdefault("read", {})
        every = float(cfg.get("search_hours") or rc.DEFAULTS["search_hours"]) * 3600
        for s in k["sensors"]:
            code, loc = s["device_code"], s.get("location") or ""
            star = loc.split("-")[0]
            if not star or code in ctx["busy"] or s.get("stowed_in_device_code") or s.get("attached_to_device_code") \
                    or star not in ctx["reach"]:
                continue
            last = _ts(searched.get(star))
            if (not last or _now() - last > every) and loc != f"{star}-KUIPER":
                # Joe 2026-11-16: a sensor array detects only from the Kuiper belt: take it there first
                mv = run.setdefault("sensor_moves", {}).setdefault(code, {})
                out += await self._chase_ferry(f, run, ctx, k, s, f"{star}-KUIPER", mv,
                                               f"{f['name']}: {code} → {star}-KUIPER to search for Kuiper objects",
                                               f"{code} → {star}-KUIPER to search", f"{code} on its way to {star}-KUIPER")
                continue
            if not last or _now() - last > every:
                run.get("sensor_moves", {}).pop(code, None)
                ok, _, err = await self.send("POST", f"/devices/{code}", {"command": "detect_object"},
                                             f"auto: rock chaser {code} searches {star}")
                searched[star] = now_iso()
                out.append(f"{code} searches the {star} Kuiper belt" if ok else f"{code} search at {star} failed: {err}")
            elif _now() - last > rc.SEARCH_MINUTES * 60 and (_ts(read.get(star)) or 0) < last:
                try:
                    body = await self.api.request("GET", f"/locations/{star}-KUIPER", background=True) or {}
                except Exception as e:  # noqa: BLE001
                    read[star] = now_iso()   # once per search, not every tick (live 2026-11-06: SOLARES has no Kuiper belt, 404)
                    out.append(f"reading {star}-KUIPER failed: {e}")
                    continue
                read[star] = now_iso()
                new = {d: m for d, m in rc.marked(body.get("kuiper_objects") or [], cfg, star, now_iso()).items() if d not in marks}
                marks.update(new)
                if new:
                    out.append(f"marked for launch: {', '.join(sorted(new))}")
                    await self.log("rock_chaser", f"{f['name']}: Kuiper object(s) marked for launch at {star}: "
                                                  f"{', '.join(sorted(new))}")
        return out

    def _chase_cleared(self, run: dict, ctx: dict, rock: str) -> bool:
        """Joe 2026-11-21: the next rock is launched once the last one is cleared (the rigs worked it and left, and its
        pile is all shipped) or evacuated (its evacuation hour, rm.CUTOFF_H, has come). A rock not mined yet isn't."""
        from . import rockmining as rm
        from .shapes import as_amounts
        if rock == run.get("target") and run.get("phase") in ("mining", "moving"):
            return False
        o = rm.open_rocks(ctx["objs"]).get(rock)
        if not o or (rm.hours_left(o, _now()) or 0) < rm.CUTOFF_H + 0.1:
            return True
        if rock not in (run.get("left") or []) and rock != run.get("arrived"):
            return False
        return sum(q for q in as_amounts(ctx["inv"].get(rock) or {}).values() if q > 0) < 1

    async def _chase_launch(self, f: dict, cfg: dict, run: dict, ctx: dict, k: dict) -> list[str]:
        """run["launch"] {rock, at} (Fleet details, or auto_launch at launch_at): a fleet vector charge goes to the Kuiper
        object's belt (flying itself, or on the carrier while the fleet has no target) and detonates it at the body."""
        from . import rockchase as rc
        from . import rockmining as rm
        req = run.get("launch")
        at = str(cfg.get("launch_at") or "").strip().upper()
        mine_by = f"fleet {f['name']}"
        open_ = rm.open_rocks(ctx["objs"])
        inbound = [{"rock": d, "eta": (open_[d] or {}).get("impact_eta"), "at": (open_[d] or {}).get("impact_target")}
                   for d, v in sorted((await self.db.kv_get(rc.LAUNCHED_KV, {}) or {}).items())
                   if (v or {}).get("by") == mine_by and d in open_]
        if inbound:
            run["inbound"] = inbound
        else:
            run.pop("inbound", None)
        if not req and cfg.get("auto_launch") and at:
            mine = f"fleet {f['name']}"
            flying = [d for d, v in (await self.db.kv_get(rc.LAUNCHED_KV, {}) or {}).items()
                      if (v or {}).get("by") == mine and d in rm.open_rocks(ctx["objs"])]
            flying = [d for d in flying if not self._chase_cleared(run, ctx, d)]
            if flying:   # one launched rock at a time: the rigs work one rock
                run["launch_note"] = f"next launch once {', '.join(sorted(flying))} is cleared or evacuated"
            else:
                run.pop("inbound", None)
                need = (run.get("need") or {}).get("order") or rm.RESOURCES
                des, why = rc.launch_choice(run.get("marked") or {}, at.split("-")[0], need, ctx.get("yields"))
                run["launch_note"] = why
                if des:
                    req = run["launch"] = {"rock": des, "at": at, "by": "auto", "why": why}
        if not req:
            return []
        des, body = req["rock"], req["at"]
        spot = rc.launch_spot(des)   # the charge waits at the object's belt; the object itself is no travel destination
        charges = sorted(k["charges"], key=lambda d: (d.get("location") not in (des, spot), (d.get("location") or "").split("-")[0] != des.split("-")[0]))
        ch = next((d for d in charges if d["device_code"] not in ctx["busy"]), None)
        if not ch:
            req["note"] = "no free vector charge in the fleet"
            return []
        code = ch["device_code"]
        if rc.can_launch(ch, des):
            ok, resp, err = await self.send("POST", f"/devices/{code}", {"command": "detonate", "target": des, "destination": body},
                                            f"auto: rock chaser launches {des}")
            if not ok:
                req["note"] = f"launch failed: {err}"
                run.pop("launch", None)
                run.setdefault("launch_log", []).append({"rock": des, "at": body, "error": err, "when": now_iso()})
                return [f"launching {des} at {body} failed: {err}"]
            await rc.remember_launch(self.db, resp, f"fleet {f['name']}")
            (run.get("marked") or {}).pop(des, None)
            run.pop("launch", None)
            run.setdefault("launch_log", []).append({"rock": des, "at": body, "object": (resp or {}).get("object_designation")
                                                     if isinstance(resp, dict) else None, "when": now_iso()})
            run["launch_log"] = run["launch_log"][-10:]
            await self._notify("info", f"{f['name']}: launched {des} at {body}", "/defence")
            return [f"launched {des} at {body}"]
        return await self._chase_ferry(f, run, ctx, k, ch, spot, req, f"{f['name']}: {code} → {des} to launch it at {body}",
                                       f"{code} → {des} to launch it at {body}", f"{code} on its way to {des}")

    async def _chase_ferry(self, f: dict, run: dict, ctx: dict, k: dict, dev: dict, spot: str, req: dict, title: str,
                           line: str, note: str) -> list[str]:
        """Bring fleet device `dev` to `spot` in its own system: it flies itself if it can, else the fleet carrier takes it
        (only while the fleet has no rock target). `req` keeps the job id and a note."""
        from . import fleets as fl
        from .automations import step
        code = dev["device_code"]
        if req.get("job") and (ctx["jobs"].get(req["job"]) or {}).get("status") in ("running", "waiting", "pending"):
            return []
        car = k["carriers"][0] if k["carriers"] else None
        if rm_can_fly(dev) and (fl.tug(dev, ctx["devices"]).get("location") or "").split("-")[0] == spot.split("-")[0] \
                and not dev.get("stowed_in_device_code") and not dev.get("attached_to_device_code"):
            steps = [step(f"{code} → {spot}", f"/devices/{code}", {"command": "travel", "destination": spot}, critical=True),
                     fl._wait_arrive(code, spot)]
            devs = [code]
        elif car and not run.get("target") and car["device_code"] not in ctx["busy"]:
            cc = car["device_code"]
            if dev.get("attached_to_device_code") == cc:
                mode = "attach"
            elif dev.get("stowed_in_device_code") == cc:
                mode = "stow"
            else:
                mode = fl.take_seat(fl.seats(car, ctx["devices"]), dev)
            if not mode or (dev.get("stowed_in_device_code") or dev.get("attached_to_device_code")) not in (None, cc):
                req["note"] = f"no seat for {code} on {cc}"
                return []
            steps = []
            if (dev.get("stowed_in_device_code") or dev.get("attached_to_device_code")) != cc:
                tour, _ = fl.pickup_tour(cc, car.get("location"), {dev["location"]: [(code, mode)]})
                steps += tour
            steps += [step(f"{cc} → {spot}", f"/devices/{cc}", {"command": "travel", "destination": spot}, critical=True),
                      fl._wait_arrive(cc, spot)] + self._unload(cc, [(code, mode)])
            devs = [cc, code]
        else:
            req["note"] = f"waiting for the carrier to take {code} to {spot} (busy with a target)"
            return []
        job = await self.create_job("rock_chaser", title, code, steps, {"devices": devs})
        if job:
            req.update({"job": job["id"], "note": note})
            return [line]
        return []


def rm_hours(o: dict) -> float | None:
    from .rockmining import hours_left
    return hours_left(o, _now())


def rm_parse(run: dict) -> list[str]:
    return list(((run.get("need") or {}).get("order")) or [])


def rm_can_fly(d: dict) -> bool:
    from .rockmining import can_fly
    return can_fly(d)
