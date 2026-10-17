"""Engine side of simulations (sim.py): prepare a run (compute cores + a replicant at the interface), nag when it's
ready, start it on your say-so, play it with the bootstrap logic, and when it ends keep the report and discard the rest.

Stage sim_pass (every tick): prep while a run is queued, the ready nag, the run itself while one is active."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from . import sim
from .api import ApiError
from .db import now_iso

log = logging.getLogger("rsweb.sim")


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


class SimMixin:
    async def sim_state(self) -> dict:
        s = await self.db.kv_get(sim.KV, {}) or {}
        if not (s.get("interface") or {}).get("code"):
            s["interface"] = dict(sim.KNOWN_INTERFACE)
        return s

    async def sim_save(self, s: dict) -> None:
        await self.db.kv_set(sim.KV, s)
        self.hub.publish("state", "sim")

    # --- prep: cores and a replicant to the interface ----------------------------------------------------------------
    async def sim_pass(self) -> list[str]:
        s = await self.sim_state()
        out: list[str] = []
        if not s.get("active"):
            out += await self.sim_adopt(s)
        if s.get("active"):
            out += await self.sim_run_pass(s)
        elif s.get("prep"):
            out += await self.sim_prep_pass(s)
        return out

    def sim_kick(self) -> None:
        """Play the run's next step right away instead of on the next tick (a minute away). A simulation's clock runs
        faster than the real one: travel, prints and mining finish sooner and their events arrive at once, so waiting a
        tick between steps would cost the run many minutes of its own time. One kick at a time; it waits for the lock."""
        import asyncio
        if getattr(self, "_sim_kicking", False):
            return
        self._sim_kicking = True

        async def go() -> None:
            try:
                for _ in range(3):   # a scan, then the step it unlocks: at most a few passes
                    async with self.lock:
                        s = await self.sim_state()
                        if not s.get("active"):
                            return
                        out = await self.sim_run_pass(s)
                    if not any(x.startswith("simulation: scan") for x in out):
                        return
            except Exception:  # noqa: BLE001
                log.exception("simulation kick failed")
            finally:
                self._sim_kicking = False
        asyncio.get_running_loop().create_task(go())

    SIM_ADOPT_EVERY = 300   # seconds between checks of the game's active runs while the app knows of none

    async def sim_adopt(self, s: dict) -> list[str]:
        """A run of yours the game reports (GET /devices/{interface}/simulate/active) that the app didn't record (live
        2026-10-15: Mining Rush #6 started while a prep pass saved over it, or started by hand): take it over."""
        iface = (s.get("interface") or {}).get("code")
        t = datetime.now(timezone.utc).timestamp()
        if not iface or t - getattr(self, "_sim_adopt_t", 0) < self.SIM_ADOPT_EVERY:
            return []
        self._sim_adopt_t = t
        try:
            body = await self.api.get(f"/devices/{iface}/simulate/active", background=True) or {}
        except ApiError:
            return []
        done = {r.get("id") for r in s.get("runs") or []}
        g = next((x for x in body.get("simulations") or [] if x.get("is_mine") and x.get("simulation_id") not in done), None)
        if not g:
            return []
        reps = await self.db.kv_get("replicants", {}) or {}
        rep = g.get("replicant_code") or next((c for c, r in reps.items() if isinstance(r, dict)
                                               and r.get("name") == g.get("replicant_name")), None)
        if not rep:
            return [f"simulation #{g.get('simulation_id')} is running, but its replicant {g.get('replicant_name')} is unknown"]
        scen = g.get("scenario_code") or ""
        learn = (s.get("learn") or {}).get(scen) or {}
        vi = sim.pick_variant(learn, len([r for r in s.get("runs") or [] if r.get("scenario") == scen]))
        sc = sim.scenario(s, scen)
        s["active"] = {"id": g.get("simulation_id"), "scenario": scen, "name": g.get("scenario_name") or sc.get("name"),
                       "seed": None, "replicant": rep, "vessel": None, "devices": [], "star": None, "stars": [],
                       "start_location": None, "started_at": g.get("started_at") or now_iso(),
                       "timeout_hours": g.get("timeout_hours") or sc.get("timeout_hours"),
                       "target": sc.get("objective_target"), "variant": vi, "timeline": [], "log": [], "scans": {},
                       "adopted": True}
        self._sim_attach_order(s)
        sim.mark(s["active"], "entered", s["active"]["started_at"])
        s.pop("prep", None)
        await self.sim_save(s)
        await self.log("simulation", f"took over {s['active']['name']} (#{s['active']['id']}) with {rep}: the game reports "
                                     f"it running", notify=True)
        return [f"simulation: took over {s['active']['name']} #{s['active']['id']}"]

    @staticmethod
    def _sim_attach_order(s: dict) -> None:
        """The run follows the build order chosen on the Simulations page (the built-in one if none is)."""
        from . import sim_order
        name = s.get("order_choice") or "Default"
        text = (s.get("orders") or {}).get(name) or sim_order.DEFAULT
        steps, _ = sim_order.parse(text)
        s["active"].update(order=steps, order_name=name, order_idx=0, order_done=[])

    async def sim_prep_pass(self, s: dict) -> list[str]:
        from .loadouts import at_tag, is_factory, to_tag
        prep = s["prep"]
        loc = prep.get("location") or sim.DATACENTER
        sc = sim.scenario(s, prep["scenario"])
        ctype, need = sim.cost(sc)
        devices = await self.devices()
        reps = await self.db.kv_get("replicants", {}) or {}
        busy = self.busy_devices(await self.jobs())
        here = [d for d in devices if d.get("device_type") == ctype and d.get("location") == loc and not d.get("stowed_in_device_code")]
        pinned = [d for d in devices if d.get("device_type") == ctype and at_tag(loc) in (d.get("tags") or []) and d not in here]
        out = []
        prep["cores_here"], prep["cores_coming"], prep["cores_needed"] = len(here), len(pinned), need
        # cores that turned up pinned since the last pass (beyond spares this pass pinned itself) were printed ones
        arrived = len(here) + len(pinned) - int(prep.get("_seen") or 0) - int(prep.pop("_tagged", 0) or 0)
        prep["printing"] = max(0, int(prep.get("printing") or 0) - max(0, arrived))
        prep["_seen"] = len(here) + len(pinned)
        short = need - len(here) - len(pinned) - int(prep.get("printing") or 0)
        if short > 0:
            # spare cores anywhere first: pin them to the interface (the loadout pass delivers to: / at: tagged devices)
            spares = [d for d in devices if d.get("device_type") == ctype and d not in here and d not in pinned
                      and "spare" in (d.get("tags") or []) and d.get("device_code") not in busy]
            for d in spares[:short]:
                ok, _, err = await self.send("PATCH", f"/devices/{d['device_code']}",
                                             {"configuration": {"add_tags": [to_tag(star_of(loc)), at_tag(loc)]}},
                                             f"simulation: {d['device_code']} to {loc}")
                if ok:
                    short -= 1
                    prep["_tagged"] = int(prep.get("_tagged") or 0) + 1
                    out.append(f"{d['device_code']} pinned to {loc}")
            if short > 0:   # print the rest at an autofactory (logistics brings its materials; the tags bring the core)
                bps = {b.get("device_type") for b in await self.db.kv_get("blueprints", []) or [] if isinstance(b, dict)}
                facs = [d for d in devices if is_factory(d) and d.get("location") and d.get("device_code") not in busy]
                if ctype in bps and facs:
                    f = min(facs, key=lambda d: (len(d.get("print_queue") or []), d["device_code"]))
                    ok, _, err = await self.send("POST", f"/devices/{f['device_code']}",
                                                 {"command": "enqueue_print", "device_type": ctype, "quantity": short,
                                                  "tags": [to_tag(star_of(loc)), at_tag(loc)]},
                                                 f"simulation: print {short} {ctype} for {loc}")
                    if ok:
                        prep["printing"] = int(prep.get("printing") or 0) + short
                        out.append(f"{short} {ctype} queued at {f['device_code']} for {loc}")
                    else:
                        prep["note"] = f"couldn't queue the {ctype} print: {err}"
                else:
                    prep["note"] = f"no {ctype} blueprint or no free autofactory to print one"
        rep_here = next((c for c, r in reps.items() if (r.get("location") or r.get("current_location")) == loc), None)
        if len(here) >= need and not rep_here and not prep.get("rep_sent"):
            cand = self._sim_replicant(reps, devices, busy, loc)
            if cand:
                ok, _, err = await self.send("POST", f"/replicants/{cand}/travel", {"destination": loc},
                                             f"simulation: replicant {cand} to {loc}")
                if ok:
                    prep["rep_sent"] = {"code": cand, "at": now_iso()}
                    out.append(f"replicant {cand} sent to {loc}")
                else:
                    prep["note"] = f"sending {cand} failed: {err}"
            else:
                prep["note"] = "no free replicant to send"
        prep["replicant"] = rep_here
        ready = len(here) >= need and bool(rep_here)
        if rep_here and not s.get("interface", {}).get("found") and not prep.get("interface_scanned"):
            prep["interface_scanned"] = True   # once per run: confirm (or correct) the known code with a device scan
            await self.sim_find_interface(s, rep_here, loc)
        prep["ready"] = ready and bool(s.get("interface", {}).get("code"))
        if prep["ready"]:
            await self._sim_nag(s, sc)
        if (await self.db.kv_get(sim.KV, {}) or {}).get("active"):
            return out   # started meanwhile (the Start button): this pass's copy is stale, saving it would drop the run
        await self.sim_save(s)
        return out

    def _sim_replicant(self, reps: dict, devices: list[dict], busy: set[str], loc: str) -> str | None:
        """The replicant to send: one whose host isn't in a job, nearest by star name match first (same system)."""
        by = {d.get("device_code"): d for d in devices}
        cands = []
        for c, r in reps.items():
            host = r.get("hosted_device_code")
            if not host or host in busy or str((by.get(host) or {}).get("status") or "").startswith(("travel", "surg", "cruis")):
                continue
            cands.append((star_of(r.get("location") or r.get("current_location")) != star_of(loc), c))
        return min(cands)[1] if cands else None

    async def sim_find_interface(self, s: dict, rep: str, loc: str) -> None:
        """The interface's device code: a device scan from the replicant there (docs: scan/devices takes device_type)."""
        try:
            body = await self.api.get(f"/replicants/{rep}/scan/devices", background=True, device_type=sim.INTERFACE) or {}
        except ApiError as e:
            s.setdefault("prep", {})["note"] = f"looking for the replicant interface failed: {e.message}"
            return
        for d in body.get("devices") or []:
            if sim.is_interface(d) and (d.get("location") in (None, loc)):
                s["interface"] = {"code": d.get("device_code"), "location": d.get("location") or loc, "found": True}
                await self.sim_load_scenarios(s)
                return

    async def sim_load_scenarios(self, s: dict) -> None:
        code = (s.get("interface") or {}).get("code")
        if not code:
            return
        try:
            body = await self.api.get(f"/devices/{code}/simulate", background=True) or {}
            if body.get("scenarios"):
                s["scenarios"] = body["scenarios"]
        except ApiError as e:
            log.warning("scenario list failed: %s", e.message)

    async def _sim_nag(self, s: dict, sc: dict) -> None:
        """A persistent nag: a notification while the run is ready and not started, again every NAG_EVERY_H hours."""
        from datetime import datetime, timezone
        last = s["prep"].get("nagged_at")
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600 if last else None
        except ValueError:
            age = None
        if age is not None and age < sim.NAG_EVERY_H:
            return
        s["prep"]["nagged_at"] = now_iso()
        await self._notify("warning", f"Simulation ready: {sc.get('name') or sc.get('code')} — compute cores and "
                                      f"{s['prep'].get('replicant')} are at the interface. Start it on the Simulations page.",
                           "/simulations")

    # --- the run -------------------------------------------------------------------------------------------------------
    async def sim_start(self, scen: str, rep: str | None = None) -> tuple[bool, str]:
        s = await self.sim_state()
        if s.get("active"):
            return False, "a simulation is already running"
        iface = (s.get("interface") or {}).get("code")
        if not iface:
            return False, "the replicant interface isn't known yet (a replicant at the datacenter finds it)"
        rep = rep or (s.get("prep") or {}).get("replicant")
        if not rep:
            return False, "no replicant at the interface"
        ok, resp, err = await self.send("POST", f"/devices/{iface}/simulate", {"scenario": scen, "replicant_code": rep},
                                        f"simulation: start {scen} with {rep}")
        if not ok:
            return False, err or "the game refused"
        resp = resp if isinstance(resp, dict) else {}
        learn = (s.get("learn") or {}).get(scen) or {}
        vi = sim.pick_variant(learn, len([r for r in s.get("runs") or [] if r.get("scenario") == scen]))
        sc = sim.scenario(s, scen)
        devs = [d.get("device_code") for d in resp.get("devices") or [] if d.get("device_code")]
        vessel = next((d.get("device_code") for d in resp.get("devices") or [] if "vessel" in str(d.get("device_type"))), None)
        s["active"] = {"id": resp.get("simulation_id"), "scenario": scen, "name": resp.get("scenario_name") or sc.get("name"),
                       "seed": resp.get("seed"), "replicant": rep, "vessel": vessel, "devices": devs,
                       "star": resp.get("starting_star"), "stars": [resp.get("starting_star")] if resp.get("starting_star") else [],
                       "start_location": resp.get("starting_location"), "started_at": now_iso(),
                       "timeout_hours": resp.get("timeout_hours") or sc.get("timeout_hours"),
                       "target": sc.get("objective_target"), "variant": vi, "timeline": [], "log": [], "scans": {}}
        self._sim_attach_order(s)
        sim.mark(s["active"], "entered", now_iso())
        s.pop("prep", None)
        await self.sim_save(s)
        await self.log("simulation", f"started {s['active']['name']} (#{s['active']['id']}) with {rep}, build order: "
                                     f"{s['active'].get('order_name')}", notify=True)
        return True, f"started {s['active']['name']}"

    async def sim_run_pass(self, s: dict) -> list[str]:
        """One step of the run: read the sim's devices, its system and stock, and act like a bootstrap's home stage."""
        from . import bootstrap as bt
        from .shapes import as_amounts, normalize_blueprints
        a = s["active"]
        rep = a["replicant"]
        if any(j.get("status") in ("running", "waiting") and (j.get("meta") or {}).get("sim") == a.get("id")
               for j in await self.jobs()):
            return []   # one job at a time, like the bootstrap
        try:
            devs = (await self.api.get(f"/replicants/{rep}/devices", background=True) or {}).get("devices") or []
            # the game lists the replicant's real devices too: only the virtual galaxy's belong to the run
            devs = sim.split_devices(devs)[1] or sim.split_devices(await self.db.kv_get("sim:devices", []) or [])[1]
        except ApiError as e:
            if e.status in (403, 404, 409):
                return await self.sim_finish(s, "ended")
            return []
        now = now_iso()
        real = {d.get("device_code") for d in await self.db.kv_get("devices", []) or []}   # never a run's (fixes 1.114.0)
        a["devices"] = sorted((set(a.get("devices") or []) | {d.get("device_code") for d in devs if d.get("device_code")})
                              - real - {None})
        vessel = next((d for d in devs if d.get("device_code") == a.get("vessel")), None) \
            or next((d for d in devs if "vessel" in str(d.get("device_type"))), None)
        if not vessel:
            return await self.sim_finish(s, "ended")
        a["vessel"] = vessel["device_code"]
        star = star_of(vessel.get("location") or a.get("start_location"))
        if star and star not in a["stars"]:
            a["stars"].append(star)
        if star and star not in a["scans"]:
            ok, resp, err = await self.send("POST", f"/replicants/{rep}/scan", {}, f"simulation: scan {star}")
            if ok and isinstance(resp, dict):
                a["scans"][star] = resp
                sim.mark(a, "system scanned", now)
            await self.sim_save(s)
            return [f"simulation: scan {star}"]
        tag = bt.family_tag(f"sim{a.get('id')}")
        for d in devs:
            d["tags"] = list(d.get("tags") or []) + [tag]
        v = sim.VARIANTS[a.get("variant") or 0]
        f = {"id": f"sim{a.get('id')}", "name": f"Simulation {a.get('name')}",
             "boot": {**bt.new_state(a["vessel"], rep, star), "settings": {**bt.DEFAULTS, "hold": v["cap"],
                                                                         "survey_per_miner": v["survey_per_miner"]}}}
        belt = bt.best_belt(a["scans"].get(star))
        inv: dict[str, Any] = {}
        if belt:
            try:
                body = await self.api.get("/inventory", background=True, location=belt["designation"]) or {}
                for it in body.get("inventory") or body.get("stockpiles") or []:
                    if it.get("location") == belt["designation"]:
                        inv[belt["designation"]] = as_amounts(it.get("items"))
            except ApiError:
                pass
        mined = sum((inv.get((belt or {}).get("designation")) or {}).values())
        a["mined_seen"] = max(float(a.get("mined_seen") or 0), mined)
        target = float(a.get("target") or 0)
        for pct in (25, 50, 75):
            if target and a["mined_seen"] >= target * pct / 100:
                sim.mark(a, f"{pct}% of the objective", now)
        w = {"devices": devs, "scans": a["scans"], "inventory": inv,
             "bps": {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}}
        if a.get("order"):   # the owner's build order (sim_order), step by step
            from . import sim_order
            seen = len(a.get("order_done") or [])
            bd = (belt or {}).get("designation")
            o_acts, note = sim_order.advance(a, devs, vessel, bd, inv.get(bd) or {}, star in a["scans"], now)
            for x in (a.get("order_done") or [])[seen:]:
                sim.mark(a, f"step {x['step'] + 1}: {a['order'][x['step']]['raw']}", now)
            ev = {"actions": o_acts, "next": note, "done": int(a.get("order_idx") or 0) >= len(a["order"])}
        else:
            ev = bt.compound(f, w, star, v["cap"], autofactory=False)
        miners = [d for d in devs if d.get("device_type") == "mining_drone"]
        if any(not d.get("stowed_in_device_code") and d.get("location") for d in miners):
            sim.mark(a, "drones deployed", now)
        if any(str(d.get("status") or "").startswith("mining") for d in miners):
            sim.mark(a, "first drone mining", now)
        n_print = len([d for d in devs if d.get("device_type") in ("mining_drone", "survey_drone")])
        a["max_drones"] = max(int(a.get("max_drones") or 0), n_print)
        if a["max_drones"] > 3:
            sim.mark(a, f"{a['max_drones']} drones", now)
        if ev.get("done"):
            sim.mark(a, "build order complete", now)
        acts = [x for x in ev.get("actions") or [] if x["kind"] != "scan"]
        if not acts:
            await self.sim_save(s)
            return []
        title = await self._boot_act({**f, "boot": f["boot"]}, {**ev, "actions": acts}, [], devs)
        if title:   # the job belongs to the run: tagged so it's found (and dropped) with it
            jobs = await self.jobs()
            for j in reversed(jobs):
                if j.get("title") == title:
                    j.setdefault("meta", {})["sim"] = a.get("id")
                    break
            await self.save_jobs(jobs)
            a.setdefault("log", []).append({"at": now, "text": ev.get("next") or title})
            a["log"] = a["log"][-30:]
        await self.sim_save(s)
        return [title] if title else []

    async def sim_on_event(self, ev: dict) -> None:
        name = ev.get("event") or ""
        if name in ("simulation.completed", "simulation.expired", "simulation.abandoned", "simulation.timed_out"):
            s = await self.sim_state()
            if s.get("active"):
                await self.sim_finish(s, {"simulation.completed": "completed"}.get(name, name.split(".")[1]))

    async def sim_finish(self, s: dict, outcome: str) -> list[str]:
        """The run is over: keep its report (score from the game's history), learn from it, discard the rest."""
        a = s.pop("active")
        hist = None
        try:
            body = await self.api.get("/accounts/simulations", background=True) or {}
            hist = next((h for h in body.get("simulations") or [] if h.get("id") == a.get("id")), None)
        except ApiError:
            pass
        if hist:
            outcome = ("completed" if hist.get("completed_at") else "abandoned" if hist.get("abandoned_at")
                       else "timed out" if hist.get("timed_out_at") else outcome)
        if hist and hist.get("completed_at"):
            sim.mark(a, "objective complete", hist["completed_at"])
        rep = sim.report(a, hist, outcome)
        rep["order_name"] = a.get("order_name")
        rep["order_steps"] = f"{len(a.get('order_done') or [])} of {len(a.get('order') or [])}" if a.get("order") else None
        s.setdefault("runs", []).append(rep)
        s["runs"] = s["runs"][-50:]
        if rep.get("score_seconds"):
            s.setdefault("learn", {}).setdefault(a["scenario"], {}).setdefault(str(a.get("variant") or 0), []).append(rep["score_seconds"])
        rep["lessons"] = sim.lessons(s["runs"], (s.get("learn") or {}).get(a["scenario"]), a["scenario"])
        await self.sim_purge(a)
        await self.sim_save(s)
        text = (f"simulation {rep['name']} {outcome}" + (f" in {rep['score_seconds'] / 60:.0f} min" if rep.get("score_seconds") else "")
                + (f" — {rep['lessons'][1]}" if len(rep["lessons"]) > 1 else ""))
        await self.log("simulation", text, notify=True)
        return [text]

    async def sim_purge(self, a: dict) -> None:
        """Everything from inside the run goes: its events, scans, cached location details, timers and jobs."""
        codes = [c for c in a.get("devices") or [] if c]
        stars = [x for x in a.get("stars") or [] if x]
        if codes:
            marks = ",".join("?" * len(codes))
            await self.db.execute(f"DELETE FROM events WHERE device_code IN ({marks})", codes)
            await self.db.execute(f"DELETE FROM timers WHERE device_code IN ({marks})", codes)
        for st in stars:
            await self.db.execute("DELETE FROM events WHERE star=? OR (location >= ? AND location < ?)", (st, f"{st}-", f"{st}."))
            await self.db.execute("DELETE FROM systems WHERE star=?", (st,))
            await self.db.execute("DELETE FROM kv WHERE key >= ? AND key < ?", (f"loc:{st}-", f"loc:{st}."))
        jobs = [j for j in await self.jobs() if (j.get("meta") or {}).get("sim") != a.get("id")]
        await self.save_jobs(jobs)
        devices = await self.db.kv_get("devices", []) or []
        keep = [d for d in devices if d.get("device_code") not in set(codes) and star_of(d.get("location")) not in set(stars)]
        if len(keep) != len(devices):
            await self.db.kv_set("devices", keep)
        from . import lost
        ls = lost.normalize(await self.db.kv_get(lost.KV, {}))   # they vanish with the run: not lost
        n = len(ls["lost"])
        ls["lost"] = {c: v for c, v in ls["lost"].items() if c not in set(codes) and not sim.SIM_CODE.match(c)}
        if len(ls["lost"]) != n:
            await self.db.kv_set(lost.KV, ls)
