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
        mine = [f for f in fleet_list if f.get("role") == rc.ROLE and f.get("home")]
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
        if not k["rigs"]:
            run["note"] = "needs mining rigs (devices with the fracture feature)"
            return out
        open_ = rm.open_rocks(objs)
        j = ctx["jobs"].get(run.get("job"))
        if j and j["status"] in ("running", "waiting", "pending"):
            return out
        if j and j["status"] == "failed":
            err = next((s.get("error") for s in j["steps"] if s.get("status") == "failed"), "") or "?"
            run["note"] = f"last {run.get('phase') or 'job'} failed: {err}"
            if run.get("phase") == "moving" and run.get("target"):
                run.setdefault("skip", {})[run["target"]] = now_iso()
                run.pop("target", None)
        run.pop("job", None)
        run["skip"] = {r: t for r, t in (run.get("skip") or {}).items() if _ts(t) and _now() - _ts(t) < SKIP_HOURS * 3600}
        target = run.get("target")
        if target and target not in open_:
            out.append(f"{target} is gone (impacted, diverted or past its ETA)")
            run.pop("target", None)
            target = None
        o = objs.get(target) if target else None
        rigs_at = [d for d in k["rigs"] if target and d.get("location") == target
                   and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code")]
        if target and rigs_at and (rc.due_to_leave(o, cfg) or not rm.pick(o, cfg)):   # due, or mined down: move on
            out += await self._chase_leave(f, cfg, run, ctx, k, target)
        elif target and rigs_at:
            run["phase"] = "mining"
            out += await self._chase_mine(f, cfg, run, ctx, k, target)
        else:   # no target, or the trip ended without the rigs there: pick (again)
            run.pop("target", None)
            out += await self._chase_move(f, cfg, run, ctx, k)
        if run.get("target"):
            out += await self._chase_ship(f, cfg, run, ctx, k, run["target"])
        out += await self._chase_search(f, cfg, run, ctx, k)
        out += await self._chase_launch(f, cfg, run, ctx, k)
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
            run["note"] = (f"no rock meets the criteria in {len(area)} system(s)"
                           + (" (home only)" if cfg["mode"] == "hub" else f" within {cfg['radius']:g} ly of {f['home']}"))
        return None

    def _passengers(self, k: dict, car: dict | None, devices: list[dict], busy: set[str]) -> tuple[dict, list, list[str]]:
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
            stops, aboard, left = self._passengers(k, car, devices, busy)
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

    async def _chase_freighters(self, f: dict, cfg: dict, run: dict, ctx: dict, k: dict, star: str) -> list[str]:
        """Self-flying shippers in another system follow the fleet to the target system's drop."""
        from . import fleets as fl
        from .automations import step
        out = []
        drop = self._chase_drop(cfg, run.get("target") or star, ctx)
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
        rcfg = {**cfg, "send_idle": False, "scan_rocks": hours - rc.leave_h(cfg) > 1.0, "scan_minutes": 30}
        objs = {rock: o}   # only its own target
        p = rm.plan(objs, k["rigs"] + k["surveyors"], rcfg, ctx["busy"], mine_state, ctx["hosts"], set(ctx["full"]),
                    ctx["scanned"])
        out = await self.rock_commands(p, ctx["state"], ctx["full"], rule="rock_chaser")
        out += await self.rock_scan_jobs(p["scan"], rule="rock_chaser")
        if rock in ctx["full"]:
            run["note"] = f"{rock}: the game says its slots are taken"
        else:
            run["note"] = f"mining {run.get('resource') or rm.pick(o, cfg) or '?'} at {rock}, leaving in " \
                          f"{max(0.0, hours - rc.leave_h(cfg)):.1f} h"
        return out

    def _chase_drop(self, cfg: dict, rock: str, ctx: dict) -> str | None:
        from . import evacuate as ev
        star = rock.split("-")[0]
        drop = str(cfg.get("drop") or "").strip().upper()
        if drop and drop.split("-")[0] == star:
            return drop
        from .rockmining import open_rocks
        o = ctx["objs"].get(rock) or {}
        hot = set(open_rocks(ctx["objs"]))
        return ev.safe_spot(star, o.get("impact_target"), ctx["stars"], ctx["scans"].get(star), here=rock, avoid=hot)

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
        drop = self._chase_drop(cfg, rock, ctx)
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
        dest = c["rock"] if c else self._chase_drop(cfg, rock, ctx)
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
            safe = self._chase_drop(cfg, rock, ctx)
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
            if not last or _now() - last > every:
                # unverified live: whether the array must be at the Kuiper belt for detect_object (sent wherever it is)
                ok, _, err = await self.send("POST", f"/devices/{code}", {"command": "detect_object"},
                                             f"auto: rock chaser {code} searches {star}")
                searched[star] = now_iso()
                out.append(f"{code} searches the {star} Kuiper belt" if ok else f"{code} search at {star} failed: {err}")
            elif _now() - last > rc.SEARCH_MINUTES * 60 and (_ts(read.get(star)) or 0) < last:
                try:
                    body = await self.api.request("GET", f"/locations/{star}-KUIPER", background=True) or {}
                except Exception as e:  # noqa: BLE001
                    run["note"] = f"reading {star}-KUIPER failed: {e}"
                    continue
                read[star] = now_iso()
                new = {d: m for d, m in rc.marked(body.get("kuiper_objects") or [], cfg, star, now_iso()).items() if d not in marks}
                marks.update(new)
                if new:
                    out.append(f"marked for launch: {', '.join(sorted(new))}")
                    await self._notify("info", f"{f['name']}: Kuiper object(s) marked for launch at {star}: "
                                               f"{', '.join(sorted(new))}", "/fleets/details")
        return out

    async def _chase_launch(self, f: dict, cfg: dict, run: dict, ctx: dict, k: dict) -> list[str]:
        """run["launch"] {rock, at} (Fleet details, or auto_launch at launch_at): a fleet vector charge goes to the Kuiper
        object (flying itself, or on the carrier while the fleet has no target) and detonates it at the body."""
        from . import fleets as fl
        from .automations import step
        from .web_terraform import LAUNCHED_KV
        req = run.get("launch")
        at = str(cfg.get("launch_at") or "").strip().upper()
        if not req and cfg.get("auto_launch") and at:
            des = next((d for d, m in sorted((run.get("marked") or {}).items()) if m.get("star") == at.split("-")[0]), None)
            if des:
                req = run["launch"] = {"rock": des, "at": at, "by": "auto"}
        if not req:
            return []
        des, body = req["rock"], req["at"]
        charges = sorted(k["charges"], key=lambda d: (d.get("location") != des, (d.get("location") or "").split("-")[0] != des.split("-")[0]))
        ch = next((d for d in charges if d["device_code"] not in ctx["busy"]), None)
        if not ch:
            req["note"] = "no free vector charge in the fleet"
            return []
        code = ch["device_code"]
        if ch.get("location") == des and not ch.get("stowed_in_device_code") and not ch.get("attached_to_device_code"):
            ok, resp, err = await self.send("POST", f"/devices/{code}", {"command": "detonate", "target": des, "destination": body},
                                            f"auto: rock chaser launches {des}")
            if not ok:
                req["note"] = f"launch failed: {err}"
                run.pop("launch", None)
                run.setdefault("launch_log", []).append({"rock": des, "at": body, "error": err, "when": now_iso()})
                return [f"launching {des} at {body} failed: {err}"]
            if isinstance(resp, dict) and resp.get("object_designation"):
                rocks = await self.db.kv_get(LAUNCHED_KV, {}) or {}
                rocks[resp["object_designation"]] = {**resp, "at": now_iso(), "by": f"fleet {f['name']}"}
                await self.db.kv_set(LAUNCHED_KV, rocks)
            (run.get("marked") or {}).pop(des, None)
            run.pop("launch", None)
            run.setdefault("launch_log", []).append({"rock": des, "at": body, "object": (resp or {}).get("object_designation")
                                                     if isinstance(resp, dict) else None, "when": now_iso()})
            run["launch_log"] = run["launch_log"][-10:]
            await self._notify("info", f"{f['name']}: launched {des} at {body}", "/defence")
            return [f"launched {des} at {body}"]
        if req.get("job") and (ctx["jobs"].get(req["job"]) or {}).get("status") in ("running", "waiting", "pending"):
            return []
        car = k["carriers"][0] if k["carriers"] else None
        if rm_can_fly(ch) and (fl.tug(ch, ctx["devices"]).get("location") or "").split("-")[0] == des.split("-")[0] \
                and not ch.get("stowed_in_device_code") and not ch.get("attached_to_device_code"):
            steps = [step(f"{code} → {des}", f"/devices/{code}", {"command": "travel", "destination": des}, critical=True),
                     fl._wait_arrive(code, des)]
            devs = [code]
        elif car and not run.get("target") and car["device_code"] not in ctx["busy"]:
            cc = car["device_code"]
            if ch.get("attached_to_device_code") == cc:
                mode = "attach"
            elif ch.get("stowed_in_device_code") == cc:
                mode = "stow"
            else:
                mode = fl.take_seat(fl.seats(car, ctx["devices"]), ch)
            if not mode or (ch.get("stowed_in_device_code") or ch.get("attached_to_device_code")) not in (None, cc):
                req["note"] = f"no seat for {code} on {cc}"
                return []
            steps = []
            if (ch.get("stowed_in_device_code") or ch.get("attached_to_device_code")) != cc:
                tour, _ = fl.pickup_tour(cc, car.get("location"), {ch["location"]: [(code, mode)]})
                steps += tour
            steps += [step(f"{cc} → {des}", f"/devices/{cc}", {"command": "travel", "destination": des}, critical=True),
                      fl._wait_arrive(cc, des)] + self._unload(cc, [(code, mode)])
            devs = [cc, code]
        else:
            req["note"] = f"waiting for the carrier to take {code} to {des} (busy with a target)"
            return []
        job = await self.create_job("rock_chaser", f"{f['name']}: {code} → {des} to launch it at {body}", code, steps,
                                    {"devices": devs})
        if job:
            req.update({"job": job["id"], "note": f"{code} on its way to {des}"})
            return [f"{code} → {des} to launch it at {body}"]
        return []


def rm_can_fly(d: dict) -> bool:
    from .rockmining import can_fly
    return can_fly(d)
