"""Terraformer fleets in the engine (terraformer.py has the rules): stage terraformer_pass."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .db import now_iso

log = logging.getLogger("rsweb.terraformer")
RECENT_S = 300   # an order sent this recently isn't sent again while the device list catches up


def _ts(v: Any) -> float | None:
    from .shapes import game_time
    dt = game_time(v)
    return dt.timestamp() if dt else None


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


class TerraformerMixin:
    async def terraformer_pass(self) -> list[str]:
        """Every terraformer fleet: gather, fly to its planet, deploy, then steer the world's devices from the monitor's
        reports until every value is in range (and keep holding it). Returns what it did."""
        from . import fleets as fl
        from . import terraformer as tr
        fleet_list = await self.fleets()
        mine = [f for f in fleet_list if f.get("role") == tr.ROLE and f.get("home") and not fl.held(f)]
        if not mine:
            return []
        jobs = await self.jobs()
        objs, _w, _s = await self.evacuation_inputs()
        ctx = {"devices": await self.devices(), "jobs": {j["id"]: j for j in jobs}, "busy": self.busy_devices(jobs),
               "objs": objs, "species": ((await self.db.kv_get("species", {}) or {}).get("species") or [])}
        out: list[str] = []
        changed = False
        for f in mine:
            if f.get("station"):
                f["station"] = False   # it works away from home: the loadout pass doesn't keep it there
                changed = True
            if not f.get("tf_init"):   # once (a new fleet, or one switched to this role): it needs a replicant aboard
                f.update({"tf_init": True, "pick_matrix": True})
                changed = True
            run = f.setdefault("tf_run", {})
            before = json.dumps(run, sort_keys=True, default=str)
            try:
                out += [f"{f['name']}: {x}" for x in await self._tf_fleet(f, tr.settings(f), run, ctx)]
            except Exception as e:  # noqa: BLE001  (one fleet's odd data mustn't stop the others)
                log.exception("terraformer %s failed", f.get("id"))
                run["note"] = f"error: {type(e).__name__}: {e}"
            changed = changed or json.dumps(run, sort_keys=True, default=str) != before
        if changed:
            await self.save_fleets(fleet_list)
        for line in out:
            await self.log("terraformer", line)
        return out

    async def tf_report(self, body: str, devices: list[dict]) -> tuple[dict, float | None]:
        """The latest terraform.status report for `body` and its time (None: only the monitor's own copy in the device
        list, as fresh as that list)."""
        r = await self.db.fetchone("SELECT payload, COALESCE(created_at, received_at) AS created_at FROM events "
                                   "WHERE event = 'terraform.status' AND location = ? ORDER BY seq DESC LIMIT 1", (body,))
        if r:
            try:
                p = json.loads(r["payload"] or "{}") or {}
            except ValueError:
                p = {}
            if p.get("attributes"):
                return p, _ts(r["created_at"])
        for d in devices:
            ts = d.get("terraform_status")
            if isinstance(ts, dict) and ts.get("attributes") and (ts.get("body") or d.get("location")) == body:
                return ts, None
        return {}, None

    async def _tf_fleet(self, f: dict, cfg: dict, run: dict, ctx: dict) -> list[str]:
        from . import fleets as fl
        from . import terraform as tf
        from . import terraformer as tr
        devices = ctx["devices"]
        planet = cfg["planet"]
        if not tr.valid_body(planet) and run.get("home_trip"):   # Return home: everyone aboard, back to the home system
            return await self._tf_home(f, run, ctx)
        if not tr.valid_body(planet):
            run.update({"phase": "idle", "note": "assign a planet or moon (e.g. KELMONENT-3) under Terraforming below"})
            return []
        run.pop("home_trip", None)   # a planet assigned again overrides an earlier Return home
        if run.get("planet") != planet:   # a new assignment starts over
            for k in ("departed", "in_range_since", "push", "alerts", "recent", "go", "left"):
                run.pop(k, None)
            run["planet"] = planet
        ms = fl.members(f, devices)
        out_pre = await self._tf_surplus(f, planet, run, ctx, ms)
        tfd = [d for d in ms if (d.get("device_type") or "") in tf.DEVICES]
        if not any(d.get("device_type") == "terraform_monitor" for d in tfd):
            run.update({"phase": "idle", "note": "needs a terraform monitor in the loadout"})
            return []
        out: list[str] = list(out_pre)
        for key in ("job", "steer_job"):
            j = ctx["jobs"].get(run.get(key))
            if j and j["status"] in ("running", "waiting", "pending") and key == "job" and self._tf_bound(j) not in (None, planet):
                # a trip to an earlier planet (Joe 2026-11-15: Terraformer 1 flew on to MAZARAR-2 after he picked
                # ALAYEBASAN-4): it ends here, and the next trip picks everyone up for the new planet
                await self._tf_end_job(j, f"{f['name']}: reassigned to {planet}")
                run["note"] = f"trip to {self._tf_bound(j)} ended: reassigned to {planet}"
                run.pop(key, None)
                continue
            if j and j["status"] in ("running", "waiting", "pending"):
                continue
            if j and j["status"] == "failed":
                err = next((s.get("error") for s in j["steps"] if s.get("status") == "failed"), "") or "?"
                run["note"] = f"last {'trip' if key == 'job' else 'steering'} failed: {err}"
            if j and key == "steer_job":
                self._tf_learn_broken(j, run, devices)
            run.pop(key, None)
        mon = next(d for d in tfd if d.get("device_type") == "terraform_monitor")
        if run.get("departed") and not run.get("job") and fl.star_of(fl.tug(mon, devices).get("location")) != planet.split("-")[0]:
            # the monitor left the planet's system (live 2026-11-17: Terraformer 1 evacuated ADHARA to SOLARES before an
            # impact): it's no longer at work there, so the next trip takes everyone again without waiting for the loadout
            run.pop("departed", None)
            run["go"] = True
        here = [d for d in tfd if d.get("location") == planet and not d.get("stowed_in_device_code")
                and not d.get("attached_to_device_code")]
        away = [d for d in tfd if d not in here]
        if away and not run.get("job"):
            out += await self._tf_move(f, cfg, run, ctx, ms, away)
        if run.get("job"):
            run["phase"] = "moving"
        if run.get("departed") and not run.get("job"):   # at work: repairs, and members that come later
            out += await self._tf_upkeep(f, cfg, run, ctx, ms)
        else:
            for k in ("upkeep", "upkeep_warn", "fixing"):
                run.pop(k, None)
        if any(d.get("device_type") == "terraform_monitor" for d in here):
            if not run.get("job"):
                run["phase"] = "terraforming"
            out += await self._tf_steer(f, cfg, run, ctx, here)
        return out

    @staticmethod
    def _tf_learn_broken(job: dict, run: dict, devices: list[dict]) -> None:
        """An order answered "Device non-operational (capacity 17%). Requires repair." (live 2026-11-15, Terraformer 1's
        monitor E13168A9): the device isn't ordered again until its capacity rises above that (a repair)."""
        from . import upkeep as up
        by = {d["device_code"]: d for d in devices}
        for s in job.get("steps") or []:
            err = str(s.get("error") or "")
            if "non-operational" in err.lower() or "requires repair" in err.lower():
                code = str(s.get("path") or "").rsplit("/", 1)[-1]
                m = re.search(r"capacity\s+(\d+(?:\.\d+)?)", err)
                c = float(m.group(1)) if m else up.cap((by.get(code) or {}).get("operational_capacity"))
                if code:
                    run.setdefault("broken", {})[code] = c if c is not None else 0.0
                    (run.get("recent") or {}).pop(code, None)   # once repaired it's ordered at once

    @staticmethod
    def _tf_bound(job: dict) -> str | None:
        """Where a trip job ends: its last travel step's destination."""
        dests = [s["body"]["destination"] for s in job.get("steps") or []
                 if isinstance(s.get("body"), dict) and s["body"].get("command") == "travel" and s["body"].get("destination")]
        return dests[-1] if dests else None

    async def _tf_end_job(self, job: dict, why: str) -> None:
        """Cancel a job from inside the tick (it already holds the engine lock that cancel() takes)."""
        job["status"] = "cancelled"
        job["finished_at"] = now_iso()
        await self._update(job)
        await self.log(job["rule"], f"canceled: {job['title']} ({why})")

    async def _tf_home(self, f: dict, run: dict, ctx: dict) -> list[str]:
        """Return home (Joe 2026-11-15, for a reassignment): every member boards the carrier wherever it is (terraform
        devices switched off first) and the carrier flies to the home system's entry point; nothing is unloaded."""
        from . import fleets as fl
        devices, home = ctx["devices"], f["home"]
        j = ctx["jobs"].get(run.get("job"))
        if j and j["status"] in ("running", "waiting", "pending"):
            run["phase"] = "returning"
            return []
        if j and j["status"] == "failed":
            run["note"] = "last trip failed: " + (next((s.get("error") for s in j["steps"] if s.get("status") == "failed"), "") or "?")
        run.pop("job", None)
        ms = fl.members(f, devices)
        cars = [d for d in ms if fl.is_carrier(d)]
        aboard = {c["device_code"] for c in cars}
        out_of_place = [d for d in ms if not (d in cars or fl.flies_itself(d)) and not fl.aboard(d, aboard)]
        away_cars = [c for c in cars if fl.star_of(c.get("location")) != home]
        if not out_of_place and not away_cars:
            run.pop("home_trip", None)
            run.update({"phase": "idle", "note": f"back in {home} with everyone aboard: assign a planet"})
            return []
        dest = fl.destination(home, await self.star_lookup("by"))
        return await self._tf_move(f, {"planet": dest}, run, ctx, ms, out_of_place, home=True)

    # --- getting there ---------------------------------------------------------------------------------------------
    async def _tf_move(self, f: dict, cfg: dict, run: dict, ctx: dict, ms: list[dict], away: list[dict],
                       home: bool = False) -> list[str]:
        from . import fleets as fl
        from . import terraform as tf
        from .automations import SHORT_TIMEOUT, step
        from . import upkeep as up
        from .logistics import holds_replicant
        from .rockmining import open_rocks
        devices, busy, planet = ctx["devices"], ctx["busy"], cfg["planet"]
        star = planet.split("-")[0]
        hot = [r for r, o in open_rocks(ctx["objs"]).items() if (o.get("impact_target") or "") == planet]
        if hot:
            run["note"] = f"an asteroid ({hot[0]}) is headed for {planet}: the fleet stays off it until that's over"
            return []
        departed = bool(run.get("departed")) and not home
        if not departed and not home:
            short = [r for r in fl.roster(f, devices)["rows"] if r["short"]]
            # Depart now: whatever it has. Away from home (a new planet for a fleet at work) it never waits: everyone
            # boards where they are and goes, and what's missing follows by logistics (_tf_fetch)
            if short and not run.get("go") and fl.where(f, devices) in (None, f["home"]):
                run.update({"phase": "gathering", "note": "waiting for its loadout: short " + ", ".join(
                    f"{r['short']} {r['type'].replace('_', ' ')}" for r in short)})
                return []
            if not fl.has_replicant(f, devices):
                if not run.get("no_rep_said") and not f.get("pick_matrix"):   # with the option on, a spare is fetched
                    run["no_rep_said"] = now_iso()
                    await self.log("terraformer", f"{f['name']}: can't leave for {planet} without an active replicant "
                                   "aboard (it works outside relay coverage): attach a filled matrix, or tick Needs an "
                                   "active replicant aboard", "error", notify=True)
                run.update({"phase": "gathering", "note": "waiting for an active replicant aboard"})
                return []
        run.pop("no_rep_said", None)
        cars = [d for d in ms if fl.is_carrier(d)]
        # the carrier that holds the replicant goes first: commands at the planet need it there
        cars.sort(key=lambda c: (not any(holds_replicant(x, devices) for x in devices
                                         if x.get("stowed_in_device_code") == c["device_code"]
                                         or x.get("attached_to_device_code") == c["device_code"])
                                 and not c.get("hosting_replicant"), c["device_code"]))
        car = cars[0] if cars else None
        steps: list[dict] = []
        moved: list[str] = []
        if departed:   # stragglers: fetched only inside the planet's system (the replicant stays with the world)
            away = [d for d in away if fl.star_of(fl.tug(d, devices).get("location")) == star or fl.flies_itself(d)]
            if not away:
                run["note"] = "members away from the planet in other systems: bring them to " + planet
                return []
        if car and car["device_code"] in busy:
            run["note"] = f"waiting for the carrier {car['device_code']} (busy)"
            return []
        if car:
            cc = car["device_code"]
            free = fl.seats(car, devices)
            stops: dict[str, list[tuple[str, str]]] = {}
            aboard: list[tuple[str, str]] = []
            left: list[str] = []
            # every member rides, its own matrix container too (Joe 2026-11-15: Terraformer 1 left SOLARES without
            # 4B6AA959 and Sk3y-34): the replicant is what commands the devices at the planet. It stays aboard there.
            riders = [d for d in ms if d is not car and not fl.flies_itself(d) and not fl.is_carrier(d)]
            for d in riders:
                code = d["device_code"]
                if d.get("attached_to_device_code") == cc:
                    aboard.append((code, "attach"))
                    continue
                if d.get("stowed_in_device_code") == cc:
                    aboard.append((code, "stow"))
                    continue
                if departed and d not in away:
                    continue   # already at work on the planet
                if not departed and not home and ((d.get("location") or "") == planet or (d.get("location") or "").startswith(planet + "-L")):
                    continue   # waiting at the planet or its L-points (parked off an impact): fetched there after arrival
                if code in busy or d.get("stowed_in_device_code") or d.get("attached_to_device_code") or not d.get("location"):
                    left.append(code)
                    continue
                mode = fl.take_seat(free, d)
                if not mode:
                    left.append(code)
                    continue
                stops.setdefault(d["location"], []).append((code, mode))
                aboard.append((code, mode))
            stuck = [c for c in left if holds_replicant(next(x for x in ms if x["device_code"] == c), devices)]
            if stuck and not any(holds_replicant(x, devices) for x in ms if (x["device_code"], "attach") in aboard
                                 or (x["device_code"], "stow") in aboard) and not car.get("hosting_replicant"):
                run.update({"phase": run.get("phase") if departed else "gathering",
                            "note": f"waiting to take its replicant ({', '.join(stuck)}) aboard {cc}: busy, aboard "
                                    "something else or no seat"})
                return []
            for loc, group in stops.items():   # a working terraform device switches off before it boards
                for code, _m in group:
                    d = next(x for x in ms if x["device_code"] == code)
                    if (d.get("device_type") or "") in tf.DEVICES and tf.is_on(d):
                        steps.append(step(f"deactivate {code}", f"/devices/{code}", {"command": "deactivate"}))
            tour, at = fl.pickup_tour(cc, car.get("location"), stops)
            # the fleet's other carriers bring what they already hold (live 2026-11-17: Terraformer 1's second mobile
            # fleet 583EBC0D sat at SOLARES with 9 devices attached); they leave together with the first
            extra_go: list[dict] = []
            extra_end: list[dict] = []
            for oc in cars[1:]:
                oco = oc["device_code"]
                if oco in busy or oc.get("location") == planet or (departed and fl.star_of(oc.get("location")) != star):
                    continue
                # what didn't fit the first carrier boards this one where it stands (live 2026-11-18: Terraformer 1 left
                # ADHARA-2 for ALKALAROPS-3 on 20EF9B48 alone, 11 members and the empty 583EBC0D stayed behind, dark
                # once the replicant had gone)
                ofree = fl.seats(oc, devices)
                for code in list(left):
                    d = next((x for x in ms if x["device_code"] == code), None)
                    if not d or code in busy or d.get("stowed_in_device_code") or d.get("attached_to_device_code") \
                            or not d.get("location") or d.get("location") != oc.get("location"):
                        continue
                    mode = fl.take_seat(ofree, d)
                    if not mode:
                        continue
                    left = [c for c in left if c != code]
                    if (d.get("device_type") or "") in tf.DEVICES and tf.is_on(d):
                        extra_go.append(step(f"deactivate {code}", f"/devices/{code}", {"command": "deactivate"}))
                    extra_go.append(fl.board_step(oco, code, mode))
                    d = dict(d, **({"attached_to_device_code": oco} if mode == "attach" else {"stowed_in_device_code": oco}))
                    riders = [d if x["device_code"] == code else x for x in riders]
                held = [d for d in riders if oco in (d.get("attached_to_device_code"), d.get("stowed_in_device_code"))]
                if not held:
                    continue
                extra_go.append(step(f"{oco} → {planet}", f"/devices/{oco}", {"command": "travel", "destination": planet}))
                extra_end.append(fl._wait_arrive(oco, planet))
                for d in held:
                    code = d["device_code"]
                    left = [c for c in left if c != code]
                    moved.append(code)
                    if home or ((d.get("device_type") or "") not in tf.DEVICES and not up.is_maint(d)):
                        continue
                    if d.get("attached_to_device_code") == oco:
                        extra_end.append(step(f"{oco}: detach {code}", f"/devices/{oco}", {"command": "detach", "device": code}))
                    else:
                        st = step(f"deploy {code} from {oco}", f"/devices/{code}", {"command": "deploy"},
                                  wait=["device.deployed"], timeout=SHORT_TIMEOUT)
                        st["wait_device"] = code
                        extra_end.append(st)
                moved.append(oco)
            # the other carriers board and leave before the replicant's carrier starts its tour (Joe 2026-11-18): the
            # system stays commandable while they go, and nothing waits behind in a system gone dark
            steps += extra_go + tour
            if at != planet:
                steps += [step(f"{cc} → {planet}", f"/devices/{cc}", {"command": "travel", "destination": planet}, critical=True),
                          fl._wait_arrive(cc, planet)]
            for code, mode in aboard:   # the terraform devices come off at the planet; the rest stays aboard
                d = next((x for x in ms if x["device_code"] == code), {})
                if home or ((d.get("device_type") or "") not in tf.DEVICES and not up.is_maint(d)):
                    continue   # service bots come off too: they repair the devices at work (Joe 2026-11-15)
                if mode == "attach":
                    steps.append(step(f"{cc}: detach {code}", f"/devices/{cc}", {"command": "detach", "device": code}))
                else:
                    st = step(f"deploy {code} from {cc}", f"/devices/{code}", {"command": "deploy"}, wait=["device.deployed"],
                              timeout=SHORT_TIMEOUT)
                    st["wait_device"] = code
                    steps.append(st)
            steps += extra_end
            moved = [cc] + [c for c, _ in aboard] + moved
            if left:
                run["left"] = left
            else:
                run.pop("left", None)
        else:   # no carrier: only devices already in the planet's system can go, each on its own
            fly = [d for d in away if d["device_code"] not in busy and fl.tug(d, devices) is d
                   and (fl.flies_itself(d) or fl.star_of(d.get("location")) == star)]
            if not fly:
                run.update({"phase": "gathering" if not departed else run.get("phase"),
                            "note": f"needs a carrier to take its devices to {planet}"})
                return []
            for d in fly:
                steps += [step(f"{d['device_code']} → {planet}", f"/devices/{d['device_code']}",
                               {"command": "travel", "destination": planet}, critical=True), fl._wait_arrive(d["device_code"], planet)]
            moved = [d["device_code"] for d in fly]
        if not steps:
            return []
        job = await self.create_job("terraformer", f"{f['name']}: {'to' if not departed else 'devices to'} {planet}",
                                    moved[0], steps, {"devices": sorted(set(moved)), "fleet": f["id"]})
        if not job:
            return []
        run["job"] = job["id"]
        if home:
            run["phase"] = "returning"
        elif not departed:
            run.update({"departed": now_iso(), "phase": "moving", "since": now_iso(), "go": False})
        run["note"] = f"→ {planet} with {len(moved)} device(s)" + (f"; left behind: {', '.join(run['left'])}" if run.get("left") else "")
        return [f"→ {planet}" + (" (stragglers)" if departed else "")]

    # --- at the planet ---------------------------------------------------------------------------------------------
    async def _tf_steer(self, f: dict, cfg: dict, run: dict, ctx: dict, here: list[dict]) -> list[str]:
        from . import terraform as tf
        from . import terraformer as tr
        from .automations import step
        planet = cfg["planet"]
        devices = ctx["devices"]
        targets = tr.targets_for(cfg["species"], ctx["species"])
        st, at = await self.tf_report(planet, devices)
        fresh = bool(st) and (at is None or _now() - at < tr.STALE_MIN * 60)
        out: list[str] = []
        if st:
            out += await self._tf_alerts(f, cfg, run, st)
            run["report"] = {"at": now_iso() if at is None else datetime.fromtimestamp(at, timezone.utc).isoformat(),
                             "values": {a: tf.value(st, a) for a in tf.ATTRS},
                             "out": tf.out_of_range(st, targets) or [], "warnings": st.get("warnings") or []}
        mon = [d for d in here if d.get("device_type") == "terraform_monitor"]
        if not fresh:
            p = {"set": {d["device_code"]: {"on": True, "strength": None, "direction": None} for d in mon}, "why": {}, "push": {}}
            run["note"] = "waiting for the monitor's report" + (" (the last one is old)" if st else "")
        else:
            p = tr.plan(st, targets, here, cfg, run.get("push"))
            run["push"] = p["push"]
            run["why"] = p["why"]
            out += await self._tf_progress(f, cfg, run, st, targets)
        if not cfg["steer"]:   # off by default (Joe 2026-11-14: you play it): only the monitor is kept on, for the reports
            p = {**p, "set": {c: s for c, s in p["set"].items() if c in {d["device_code"] for d in mon}}}
            if fresh:
                run["note"] = (run.get("note") or "") + " · you steer it on the Terraforming console"
        if run.get("steer_job"):
            return out
        now = _now()
        recent = {c: r["key"] for c, r in (run.get("recent") or {}).items() if now - (_ts(r.get("at")) or 0) < RECENT_S}
        hold = set(run.get("fixing") or []) | set(self._tf_broken(run, devices))   # under repair, or waiting for one
        reach = [d for d in here if d.get("in_control_range") is not False and d["device_code"] not in ctx["busy"]
                 and d["device_code"] not in hold]
        cmds = tr.commands(p["set"], reach, recent)
        if not cmds:
            run["recent"] = {c: r for c, r in (run.get("recent") or {}).items() if c in recent}
            return out
        steps = []
        for code, kind, body in cmds:
            if kind == "settings":
                s = body["configuration"]["settings"]
                steps.append(step(f"{code}: " + " · ".join(f"{k} {v}" for k, v in s.items()), f"/devices/{code}", body,
                                  method="PATCH"))
            else:
                steps.append(step(f"{kind} {code}", f"/devices/{code}", body))
        job = await self.create_job("terraformer", f"{f['name']}: steer {planet} ({len(cmds)} order(s))", cmds[0][0], steps,
                                    {"devices": sorted({c for c, _, _ in cmds}), "fleet": f["id"]})
        if job:
            run["steer_job"] = job["id"]
            rec = {c: r for c, r in (run.get("recent") or {}).items() if c in recent}
            for code, _k, _b in cmds:
                rec[code] = {"key": tr.key(p["set"][code]), "at": now_iso()}
            run["recent"] = rec
            ons = sum(1 for _c, k, _b in cmds if k == "activate")
            offs = sum(1 for _c, k, _b in cmds if k == "deactivate")
            out.append(f"steering {planet}: {ons} on, {offs} off, {len(cmds) - ons - offs} setting(s)")
        return out

    async def _tf_progress(self, f: dict, cfg: dict, run: dict, st: dict, targets: dict) -> list[str]:
        """The fleet stays terraforming as long as it is assigned (Joe 2026-11-14): no "complete" state. The note says
        how far the world is; in range it keeps holding it (the regulator)."""
        from . import terraform as tf
        bad = tf.out_of_range(st, targets) or []
        run["phase"] = "terraforming"
        if bad:
            run.pop("in_range_since", None)
            push = run.get("push") or {}
            run["note"] = "out of range: " + ", ".join(
                f"{a} {'↑' if push.get(a, 0) > 0 else '↓' if push.get(a, 0) < 0 else ''}".strip() for a in bad)
            return []
        since = run.setdefault("in_range_since", now_iso())
        held_h = (_now() - (_ts(since) or _now())) / 3600
        run["note"] = (f"every value in range for {cfg['species']} for {held_h:.1f} h: holding it until the fleet is "
                       "assigned elsewhere")
        return []

    async def _tf_alerts(self, f: dict, cfg: dict, run: dict, st: dict) -> list[str]:
        """A disaster (coupling rule) turning active or an anomaly in the report: one notification each, again only after
        it has cleared. A disaster is a warning while the fleet steers and an error when you steer it yourself (steering off,
        the default); an anomaly is always a warning."""
        from . import terraformer as tr
        planet = cfg["planet"]
        level = "warning" if cfg["steer"] else "error"
        seen = run.get("alerts") or {}
        now: dict[str, str] = {}
        out: list[str] = []
        for c in tr.disasters(st):
            k = "disaster:" + c["rule"]
            now[k] = seen.get(k) or now_iso()
            if k not in seen:
                txt = (f"{f['name']}: disaster at {planet}: {c['rule']} ({c['attr']} {c['op']} {c['threshold']:g}, now "
                       f"{c['value']:g}: {c['effect']})" + ("; the fleet steers away from it" if cfg["steer"] else
                                                           "; steering is off: counter it on the Terraforming console"))
                await self.log("terraformer", txt, level, notify=True)
                out.append(f"disaster: {c['rule']}")
        a = tr.anomaly_text(st)
        if a:
            # an anomaly is the game's passing weather (solar flare, toxic bloom, eruption): nothing to do about it, so a
            # warning, once from its forecast to its end (2026-11-16: each one was two errors, ~40 in a night)
            k = "anomaly:" + tr.anomaly_key(st)
            now[k] = seen.get(k) or now_iso()
            if k not in seen:
                await self.log("terraformer", f"{f['name']}: anomaly at {planet}: {a}", "warning", notify=True)
                out.append("anomaly")
        run["alerts"] = now
        return out

    # --- upkeep: repairs, and members that come later ----------------------------------------------------------------
    @staticmethod
    def _tf_broken(run: dict, devices: list[dict]) -> list[str]:
        """Devices the game called non-operational whose capacity hasn't risen since (forgotten once it has)."""
        from . import upkeep as up
        by = {d["device_code"]: d for d in devices}
        broken = run.get("broken") or {}
        for code in list(broken):
            c = up.cap((by.get(code) or {}).get("operational_capacity"))
            if code not in by or (c is not None and c > float(broken[code] or 0) + 0.5):
                broken.pop(code)
        if not broken:
            run.pop("broken", None)
        return sorted(broken)

    async def _tf_upkeep(self, f: dict, cfg: dict, run: dict, ctx: dict, ms: list[dict]) -> list[str]:
        """Terraform devices wear fast while they work (Joe 2026-11-15: shades at 30 %, the monitor broken at 17 % after
        15 h). The fleet's service bots (repair devices) come off the carrier at the planet and patrol there: the game's
        patrol picks the most damaged device, switches it off and repairs it to 100 % (upkeep.py). A device that was
        on when its repair began is switched back on afterwards (with steering on, the plan does that). Members the fleet
        gets after it left (a service bot added to the loadout, a member somewhere else) come by logistics: a spare is
        tagged for delivery to the planet (to:/at:, as the Deliver command does) and joins the fleet when it arrives."""
        from . import fleets as fl
        from . import terraform as tf
        from . import upkeep as up
        from .automations import step
        planet, devices, busy = cfg["planet"], ctx["devices"], ctx["busy"]
        by = {d["device_code"]: d for d in devices}
        steps: list[dict] = []
        notes: list[str] = []
        out: list[str] = []
        warn = False
        # 1. service bots: off the carrier at the planet, then on patrol
        bots = [d for d in ms if up.is_maint(d)]
        for d in bots:
            code = d["device_code"]
            host = by.get(d.get("stowed_in_device_code") or d.get("attached_to_device_code") or "")
            if code in busy:
                continue
            if host and host.get("location") == planet:
                if d.get("stowed_in_device_code"):
                    st = step(f"deploy {code} from {host['device_code']}", f"/devices/{code}", {"command": "deploy"},
                              wait=["device.deployed"], timeout=600)
                    st["wait_device"] = code
                    steps.append(st)
                else:
                    steps.append(step(f"{host['device_code']}: detach {code}", f"/devices/{host['device_code']}",
                                      {"command": "detach", "device": code}))
                steps.append(step(f"{code}: set_directive patrol", f"/devices/{code}",
                                  {"command": "set_directive", "directive": "patrol"}))
            elif up.placed(d) and d.get("location") != planet and fl.star_of(d.get("location")) == fl.star_of(planet):
                st = step(f"{code} → {planet}", f"/devices/{code}", {"command": "travel", "destination": planet})
                steps += [st, fl._wait_arrive(code, planet)]   # dropped elsewhere in the system: to the planet
            elif d.get("location") == planet and up.placed(d) and not up.patrolling(d) \
                    and "set_directive" in (d.get("available_commands") or ["set_directive"]):
                steps.append(step(f"{code}: set_directive patrol", f"/devices/{code}",
                                  {"command": "set_directive", "directive": "patrol"}))
        working = [d for d in bots if d.get("location") == planet and up.placed(d)]
        # 2. repairs: what the bots work on now; devices that were on get switched back on once theirs is done
        fixing = {r["target"] for d in working if (r := up.repairing(d)) and r.get("target")}
        run["fixing"] = sorted(fixing)
        prev_on = set(run.get("on") or [])
        off = run.setdefault("repair_off", {})
        for c in fixing:
            if c in prev_on and c not in off:
                off[c] = now_iso()
        for c in [c for c in off if c not in fixing]:
            d = by.get(c)
            off.pop(c)
            if not d or cfg["steer"] or d.get("device_type") == "terraform_monitor" or tf.is_on(d) or c in busy \
                    or d.get("location") != planet or c in self._tf_broken(run, devices):
                continue   # steering (and the monitor's keep-on) switch those on by themselves
            steps.append(step(f"activate {c} (repaired)", f"/devices/{c}", {"command": "activate"}))
        if not off:
            run.pop("repair_off", None)
        run["on"] = sorted({d["device_code"] for d in ms if (d.get("device_type") or "") in tf.DEVICES and tf.is_on(d)} | set(off))
        if fixing:
            notes.append("repairing " + ", ".join(sorted(fixing)))
        broken = self._tf_broken(run, devices)
        if broken:
            notes.append("out of order until repaired: " + ", ".join(
                f"{c} ({(by.get(c) or {}).get('device_type', '?').replace('_', ' ')})" for c in broken))
        # 3. members that come later
        fetch = run.setdefault("fetch", {})
        from .loadouts import tag_step
        for d in ms:   # old pins on members at work (446569DB kept at:solares-belt-1): a later trip home would obey them
            stale = [t for t in d.get("tags") or [] if t.startswith(("to:", "at:"))]
            if stale and d.get("location") == planet and d["device_code"] not in busy and d["device_code"] not in fetch:
                steps.append(tag_step(d["device_code"], None, stale))
        sent = await self._tf_fetch(f, planet, run, ctx, ms, fetch, steps)
        out += sent
        if fetch:
            notes.append("on the way by logistics: " + ", ".join(f"{c} ({v['type'].replace('_', ' ')})"
                                                                 for c, v in sorted(fetch.items())))
        else:
            run.pop("fetch", None)
        short = {t: n - sum(1 for v in fetch.values() if v["type"] == t) for t, n in fl.short_list(f, devices).items()}
        short = {t: n for t, n in short.items() if n > 0}
        if short:
            out += await self._tf_print(f, planet, run, ctx, short)
            why = run.get("short_why") or {}
            notes.append("short " + "; ".join(f"{n}× {t.replace('_', ' ')} ({why.get(t) or 'none free'})"
                                               for t, n in sorted(short.items())))
            warn = True
        if broken and not working and not any(up.is_maint(by.get(c) or {}) for c in fetch):
            warn = True
            notes.append("no service bot at the planet: add one to the loadout")
            said = run.get("broken_said") or {}
            for c in broken:
                if c not in said:
                    said[c] = now_iso()
                    await self.log("terraformer", f"{f['name']}: {c} ({(by.get(c) or {}).get('device_type')}) at {planet} "
                                   "is out of order and needs a repair: no service bot with the fleet there", "error", notify=True)
            run["broken_said"] = said
        else:
            run.pop("broken_said", None)
        run["upkeep"] = " · ".join(notes)
        run["upkeep_warn"] = warn
        if not run["upkeep"]:
            run.pop("upkeep", None)
        if steps:
            codes = sorted({s["path"].rsplit("/", 1)[-1] for s in steps} - {""})
            job = await self.create_job("terraformer", f"{f['name']}: upkeep at {planet}", codes[0], steps,
                                        {"devices": codes, "fleet": f["id"]})
            if job:
                busy |= set(codes)
                out.append(f"upkeep at {planet}: {len(steps)} order(s)")
        return out

    async def _tf_fetch(self, f: dict, planet: str, run: dict, ctx: dict, ms: list[dict], fetch: dict,
                        steps: list[dict]) -> list[str]:
        """Members for a fleet already at work: spares of the types it's short of (nearest first, within the supply range)
        and its members in other systems are tagged for delivery to the planet (logistics carries them; the fleet's
        replicant stays with the world). On arrival they get the fleet's tag back. fetch: {code: {type, at}}."""
        from . import fleets as fl
        from .loadouts import delivery_tags, tag_step
        from .logistics import holds_replicant
        devices, busy = ctx["devices"], ctx["busy"]
        by = {d["device_code"]: d for d in devices}
        tag, star = fl.fleet_tag(f["id"]), fl.star_of(planet)
        out: list[str] = []
        for code, v in list(fetch.items()):
            d = by.get(code)
            if not d or (fl.fleet_of(d) and fl.fleet_of(d) != f["id"]):
                fetch.pop(code)   # gone, or taken by another fleet
                continue
            if code in busy:
                continue   # aboard a logistics trip
            tags = d.get("tags") or []
            if d.get("location") == planet and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code"):
                steps.append(tag_step(code, [tag], [t for t in tags if t.startswith(("to:", "at:")) or t == "spare"]))
                fetch.pop(code)
                out.append(f"{code} ({d.get('device_type')}) arrived at {planet} and joins")
                continue
            age = _now() - (_ts(v.get("at")) or _now())
            if age > 600 and tag not in tags and not any(t.startswith(("to:", "at:")) for t in tags):
                fetch.pop(code)   # its delivery was canceled by hand
        pos = {k: (s or {}).get("position") or {} for k, s in (await self.star_lookup("by")).items()}
        reach = fl.sector_reach(await self.supply_reach())

        def send(d: dict, why: str) -> bool:
            add, remove, no = delivery_tags(d, planet, {tag: f["home"]})
            if no:
                return False
            if tag in (d.get("tags") or []) and tag not in remove:
                remove = sorted(set(remove) | {tag})   # off the roster while logistics carries it
            steps.append(tag_step(d["device_code"], add, remove))
            fetch[d["device_code"]] = {"type": d.get("device_type") or "", "at": now_iso()}
            out.append(f"{d['device_code']} ({d.get('device_type')}) {why}: logistics brings it to {planet}")
            return True

        def movable(d: dict) -> bool:
            return (d["device_code"] not in busy and d["device_code"] not in fetch and not fl.is_carrier(d)
                    and not holds_replicant(d, devices) and (d.get("device_type") or "") not in ("matrix_container", "replicant_matrix")
                    and not d.get("controller_device_code"))

        # members in another system (not aboard the fleet's own carriers)
        cars = {c["device_code"] for c in ms if fl.is_carrier(c)}
        for d in ms:
            if not movable(d) or fl.aboard(d, cars) or fl.flies_itself(d):
                continue
            here = fl.star_of(fl.tug(d, devices).get("location"))
            if here and here != star and not fl.beyond_reach(here, star, pos, reach):
                send(d, f"is in {here}")
        # spares for what the loadout is short of
        short = {t: n - sum(1 for v in fetch.values() if v["type"] == t) for t, n in fl.short_list(f, devices).items()}
        short = {t: n for t, n in short.items() if n > 0}
        if short:
            # a spare, or a device nobody claims (no tags at all: Joe 2026-11-16, three lances idle at FALQUORYX-BELT-1)
            free = [d for d in devices if ("spare" in (d.get("tags") or []) or not d.get("tags")) and not fl.fleet_of(d)
                    and movable(d) and short.get(d.get("device_type") or "", 0) > 0 and d.get("location")
                    and str(d.get("status") or "").startswith(("idle", "stowed", "coordinating", "inactive", "compacted"))]
            cands = [d for d in free if not fl.beyond_reach(fl.star_of(d.get("location")), star, pos, reach)]
            far = {}
            for d in free:
                if d not in cands:
                    far[d.get("device_type") or ""] = far.get(d.get("device_type") or "", 0) + 1
            run["short_why"] = {t: (f"{far[t]} free beyond the {reach:g} ly supply range (Fleets › settings)" if far.get(t)
                                    else "no free spare anywhere: tag one spare, or print it")
                                for t in short}
            cands.sort(key=lambda d: (fl._dist(star, fl.star_of(d.get("location")), pos), d["device_code"]))
            for d in cands:
                t = d.get("device_type") or ""
                if short.get(t, 0) > 0 and send(d, "is free"):
                    short[t] -= 1
        else:
            run.pop("short_why", None)
        return out

    async def _tf_surplus(self, f: dict, planet: str, run: dict, ctx: dict, ms: list[dict]) -> list[str]:
        """More of a type coming than the loadout wants (Joe 2026-11-16: prints queued with Print missing while Terraformer 1
        was short, then Fill from spares took D1E96D13 and a spare regulator A8BC5318 was sent for as well): the extras go,
        members first kept, then devices on their way (spares, free), then prints. A print not started comes off its
        queue (newest first); a spare sent for gets its delivery tags off and is spare again. Deliveries still pinned to
        an earlier planet are sent on to this one."""
        from . import fleets as fl
        from . import printqueue as pq
        from .loadouts import delivery_tags, tag_step
        from .automations import step  # noqa: F401
        devices, busy = ctx["devices"], ctx["busy"]
        by = {d["device_code"]: d for d in devices}
        tag = fl.fleet_tag(f["id"])
        fetch = run.get("fetch") or {}
        wants = {t: int(n or 0) for t, n in (f.get("wants") or {}).items()}
        have: dict[str, int] = {}
        for d in ms:
            have[d.get("device_type") or ""] = have.get(d.get("device_type") or "", 0) + 1
        steps: list[dict] = []
        out: list[str] = []
        # spares sent for beyond the want (newest first), and the rest re-pinned to the current planet
        for t in sorted({v["type"] for v in fetch.values()}):
            mine = sorted((c for c, v in fetch.items() if v["type"] == t), key=lambda c: fetch[c].get("at") or "")
            extra = max(0, have.get(t, 0) + len(mine) - wants.get(t, 0))
            for c in mine[::-1][:extra]:
                d = by.get(c)
                fetch.pop(c)
                if d and c not in busy:
                    steps.append(tag_step(c, ["spare"], [x for x in d.get("tags") or [] if x.startswith(("to:", "at:"))]))
                    out.append(f"{c} ({t}) not needed any more: spare again")
            have[t] = have.get(t, 0) + len(mine) - extra
        if planet and fl.star_of(planet) and "-" in planet:
            for c in list(fetch):
                d = by.get(c)
                if not d or c in busy or f"at:{planet.lower()}" in (d.get("tags") or []):
                    continue
                add, remove, no = delivery_tags(d, planet, {tag: f["home"]})
                if not no:
                    steps.append(tag_step(c, add, remove))
                    out.append(f"{c} sent on to {planet}")
        if steps:
            await self.create_job("terraformer", f"{f['name']}: deliveries ({len(steps)})", None, steps,
                                  {"devices": sorted({s['path'].rsplit('/', 1)[-1] for s in steps}), "fleet": f["id"]})
            busy |= {s["path"].rsplit("/", 1)[-1] for s in steps}
        # prints beyond the want: off their queues (a few per pass)
        queued: dict[str, list[str]] = {}
        for d in devices:
            for it in pq.items(d):
                if tag in it["tags"]:
                    queued.setdefault(it["device_type"], []).extend([d["device_code"]] * max(1, int(it.get("quantity") or 1)))
        n = 0
        for t, facs in sorted(queued.items()):
            extra = max(0, have.get(t, 0) + len(facs) - wants.get(t, 0))
            for fac in facs[::-1][:extra]:
                if n >= 4:
                    break
                n += 1
                await self.cancel_loadout_print({"factory": fac, "device_type": t, "fleet": f["id"], "star": f.get("home") or "",
                                                 "rule": "terraformer",
                                                 "label": f"{f['name']}: the {t} queued for it isn't needed (it has {have.get(t, 0)} of "
                                                          f"{wants.get(t, 0)})"}, [])
                out.append(f"extra {t} print off {fac}'s queue")
        return out

    async def _tf_print(self, f: dict, planet: str, run: dict, ctx: dict, short: dict[str, int]) -> list[str]:
        """What no spare covers is printed for the fleet (Joe 2026-11-17: a terraformer works one world for a long time
        and gets new devices there, like a stationed fleet at home). Prints go on the autofactories nearest the planet
        (within the supply range) tagged with the fleet, so each one is a member in another system once printed and
        _tf_fetch has logistics bring it to the planet. Prints already queued for the fleet count; a type printed in the
        last 20 minutes waits for the device list to show its queue (run.printed). Off with Fleets › settings *Print
        what spares can't cover*."""
        from . import fleets as fl
        from . import printqueue as pq
        from .automations import step
        from .loadouts import is_factory
        from .shapes import normalize_blueprints
        cfg = await self.loadout_cfg()
        if not (cfg.get("settings") or {}).get("print_missing", True):
            return []
        devices, star = ctx["devices"], fl.star_of(planet)
        tag = fl.fleet_tag(f["id"])
        need = dict(short)
        for d in devices:
            for it in pq.items(d):
                if tag in it["tags"] and need.get(it["device_type"]):
                    need[it["device_type"]] = max(0, need[it["device_type"]] - max(1, int(it.get("quantity") or 1)))
        printed = {t: v for t, v in (run.get("printed") or {}).items()
                   if _now() - (_ts(v) or 0) < 1200}
        run["printed"] = printed
        if not printed:
            run.pop("printed", None)
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        need = {t: n for t, n in need.items() if n > 0 and t not in printed and t in bps}
        if not need:
            return []
        pos = {k: (v or {}).get("position") or {} for k, v in (await self.star_lookup("by")).items()}
        reach = fl.sector_reach(await self.supply_reach())
        facs = [d for d in devices if is_factory(d) and d.get("location") and d.get("in_control_range") is not False
                and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code")
                and not str(d.get("status") or "").startswith(("compact", "stowed", "attach"))
                and not fl.beyond_reach(fl.star_of(d.get("location")), star, pos, reach)]
        if not facs:
            why = run.setdefault("short_why", {})
            for t in need:
                why[t] = f"no autofactory within the {reach:g} ly supply range to print it"
            return []
        facs.sort(key=lambda d: (fl._dist(star, fl.star_of(d.get("location")), pos), d["device_code"]))
        peers = [d for d in facs if fl.star_of(d.get("location")) == fl.star_of(facs[0].get("location"))]
        load = {d["device_code"]: pq.load_seconds(d, bps) for d in peers}
        steps = []
        for t, n in sorted(need.items()):
            for fac, k in pq.split(peers, t, n, bps, load):
                steps.append(step(f"print {k}× {t} on {fac['device_code']} for {f['name']}", f"/devices/{fac['device_code']}",
                                  {"command": "enqueue_print", "device_type": t, "quantity": k, "tags": [tag]}))
        if not steps:
            return []
        codes = sorted({s["path"].rsplit("/", 1)[-1] for s in steps})
        await self.create_job("terraformer", f"{f['name']}: print {fl.summarize(need)} for {planet}", codes[0], steps,
                              {"devices": codes, "fleet": f["id"], "print": need})
        for t in need:
            printed[t] = now_iso()
        run["printed"] = printed
        return [f"no spare {t.replace('_', ' ')} free: printing {n} at {fl.star_of(peers[0].get('location'))} for {planet}"
                for t, n in sorted(need.items())]
