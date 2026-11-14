"""The job runner: jobs and their steps, waits, retries, trips and cancellation.

A part of AutomationEngine (automations.py), split out by area; the methods are unchanged."""
from __future__ import annotations

from .engine_base import *  # noqa: F401,F403


class JobsMixin:
    # --- jobs --------------------------------------------------------------------------------------
    async def cooperation(self) -> dict:
        """The account's replicant cooperation: {"shared": bool, "public": replicants whose cohort_permission is public}."""
        acct = await self.db.kv_get("account", {}) or {}
        reps = await self.db.kv_get("replicants", {}) or {}
        return {"shared": acct.get("replicant_cooperation") == "shared",
                "public": {c for c, r in reps.items() if isinstance(r, dict) and r.get("cohort_permission") == "public"}}

    async def create_job(self, rule: str, title: str, device: str | None, steps: list[dict], meta: dict | None = None,
                         force: bool = False) -> dict | None:
        """force=True: a job the player asked for directly (e.g. a command chain) — dry run doesn't apply."""
        s = await self.settings()
        if not steps:
            return None
        if s["dry_run"] and not force:
            plan = "; ".join(st["desc"] for st in steps)
            await self.log(rule, f"[dry run] {title}: {plan}")
            return None
        if not force and not (meta or {}).get("through_hold") and await self.held():
            await self.log(rule, f"[paused for the season change] not started: {title}")
            return None
        if not force and not (meta or {}).get("sim"):
            far = await self._far_steps(steps)
            if far:   # never sent beyond the sector range (owner 2026-11-14): logged once a day per job title
                seen = self.__dict__.setdefault("_far_logged", {})
                if (_now() - (seen.get(title) or datetime.min.replace(tzinfo=timezone.utc))).total_seconds() > 86400:
                    seen[title] = _now()
                    await self.log(rule, f"not started: {title} — {far}")
                return None
        from .modular import prepare_moves
        from .shapes import normalize_blueprints
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        # drones tracking a site deactivate before moving; large (modular) devices compact (modular.py)
        steps = prepare_moves(steps, await self.devices(), bps, await self.cooperation())
        jobs = await self.jobs()
        job = {"id": f"{rule}-{int(_now().timestamp() * 1000)}-{len(jobs)}", "rule": rule, "title": title,
               "device": device, "steps": steps, "idx": 0, "status": "running", "created_at": now_iso(),
               "meta": meta or {}, "forced": force}
        from .version import RUN_ID, VERSION
        job["v"], job["run"] = VERSION, RUN_ID
        jobs.append(job)
        await self.save_jobs(jobs)
        await self.log(rule, f"started: {title} ({len(steps)} steps)")
        await self._advance(job["id"])
        return job

    async def _update(self, job: dict) -> None:
        jobs = await self.jobs()
        for i, j in enumerate(jobs):
            if j["id"] == job["id"]:
                jobs[i] = job
        await self.save_jobs(jobs)
        self.hub.publish("state", "automation")

    async def _get(self, job_id: str) -> dict | None:
        return next((j for j in await self.jobs() if j["id"] == job_id), None)

    async def _max_seq(self) -> int:
        row = await self.db.fetchone("SELECT MAX(seq) AS s FROM events")
        return int(row["s"] or 0) if row else 0

    async def _already_happened(self, st: dict, device: str | None) -> bool:
        """Did the awaited event arrive while the command was being sent (before we started waiting)?"""
        if not st["wait"]:
            return False
        marks = ",".join("?" * len(st["wait"]))
        rows = await self.db.fetchall(f"SELECT * FROM events WHERE event IN ({marks}) AND seq > ? ORDER BY seq",
                                      [*st["wait"], st.get("seq0", 0)])
        for r in rows:
            ev = dict(r)
            try:
                ev["payload"] = json.loads(r["payload"] or "{}")
            except ValueError:
                ev["payload"] = {}
            if event_matches(st, ev, device):
                return True
        return False

    async def held(self) -> bool:
        """Paused for a season change (season.py) until you resume."""
        from .season import HOLD_KV
        return bool(await self.db.kv_get(HOLD_KV, None))

    async def outage_pass(self) -> list[str]:
        """Scheduled game outages (season.py): pause before, resume after. Runs every tick, held or not."""
        from . import season
        wins = await self.db.kv_get(season.OUTAGE_KV, []) or []
        if not wins:
            return []
        hold = await self.db.kv_get(season.HOLD_KV, None)
        new, act, wid = season.outage_step(wins, hold, _now())
        if new != wins:
            await self.db.kv_set(season.OUTAGE_KV, new)
        w = next((x for x in new if x["id"] == wid), {})
        if act == "pause":
            _, start, end, back = season.window_times(w)
            await self.db.kv_set(season.SNAP_KV, await season.snapshot(self.db))
            await self.db.kv_set(season.HOLD_KV, {"since": now_iso(), "by": "outage schedule", "window": wid,
                                                  "until": back.isoformat(timespec="minutes")})
            await self.log("season", f"Automations paused for the game outage {start:%Y-%m-%d %H:%M}–{end:%H:%M} UTC; they "
                                     f"resume by themselves at {back:%H:%M} UTC (evacuations keep running).", "warning", notify=True)
            return ["paused for the outage"]
        if act == "resume":
            await self.db.kv_set(season.HOLD_KV, None)
            await self.log("season", "Automations resumed after the scheduled game outage. Check Dashboard › Story for news.",
                           "warning", notify=True)
            return ["resumed after the outage"]
        return []

    async def station_jobs_pass(self) -> list[str]:
        """Station jobs (stations.py): read the boards and your jobs when due, then work the job handed to the runner."""
        from . import stations
        return await stations.run_pass(self)

    async def season_snapshot_pass(self) -> list[str]:
        """The pre-change copy (season.py), every few hours; never while held, so it keeps the galaxy as it was."""
        from . import season
        # the copy is megabytes: its row's write time says how old it is without reading it
        if (_ts(await self.db.kv_updated(season.SNAP_KV)) or _now() - timedelta(days=1)) > _now() - timedelta(hours=season.SNAP_HOURS):
            return []
        await self.db.kv_set(season.SNAP_KV, await season.snapshot(self.db))
        return []

    async def _let_off_riders(self, st: dict, job: dict) -> bool:
        """Before a carrier's trip: members of another fleet riding it that this trip would carry away from their fleet
        (fleets.foreign_riders) are let off where it is first; the loadout pass then brings them home. True when steps
        were added ahead of this one (it runs after them)."""
        from . import fleets as fl
        body, path = st.get("body") or {}, str(st.get("path") or "")
        if st.get("riders_checked") or not body.get("destination") or not (
                (body.get("command") == "travel" and path.startswith("/devices/")) or path.endswith("/travel")):
            return False
        st["riders_checked"] = True
        carrier = path.split("/")[2] if path.startswith("/devices/") else st.get("wait_device") or job.get("device")
        if not carrier:
            return False
        riders = fl.foreign_riders(carrier, body["destination"], await self.devices(), await self.fleets(), job)
        if not riders:
            return False
        add = fl.let_off_steps(carrier, riders)
        i = job["idx"]
        for s2 in job["steps"][i:]:
            if isinstance(s2.get("seq0_from"), int) and s2["seq0_from"] >= i:
                s2["seq0_from"] += len(add)
        job["steps"][i:i] = add
        st["tries"] -= 1
        await self.log(job["rule"], f"{job['title']}: {carrier} lets off {', '.join(x['code'] for x in riders)} before "
                                    f"leaving for {body['destination']} (another fleet's members)", "warning")
        await self._update(job)
        return True

    async def _advance(self, job_id: str) -> None:
        job = await self._get(job_id)
        if job and not job.get("forced") and not (job.get("meta") or {}).get("through_hold") and await self.held():
            return   # paused for a season change: picks up again when you resume
        # only jobs you started print on vessels (and a simulation run's, in its virtual galaxy)
        tok = HAND_JOB.set(bool(job and (job.get("forced") or (job.get("meta") or {}).get("sim"))))
        try:
            await self._advance_job(job)
        finally:
            HAND_JOB.reset(tok)

    async def _advance_job(self, job: dict | None) -> None:
        while job and job["status"] in ("running", "waiting"):
            if job["idx"] >= len(job["steps"]):
                job["status"] = "done"
                job["finished_at"] = now_iso()
                skipped = sum(1 for s in job["steps"] if s["status"] == "skipped")
                await self._update(job)
                await self.log(job["rule"], f"finished: {job['title']}" + (f" ({skipped} step(s) skipped)" if skipped else ""),
                               notify=True)
                if (job.get("meta") or {}).get("sim"):   # a run's clock runs fast: its next step now, not next tick
                    self.sim_kick()
                return
            st = job["steps"][job["idx"]]
            if st["status"] in ("waiting", "confirm"):
                return  # on_event / tick will move it on (a held trip: your Confirm)
            if st["status"] in ("done", "skipped"):
                job["idx"] += 1
                continue
            st["tries"] += 1
            st["started_at"] = now_iso()
            st["seq0"] = await self._max_seq()
            if st["method"] == "WAIT":
                # nothing to send: just wait for an event, counting from an earlier step if asked
                ref = st.get("seq0_from")
                if isinstance(ref, int) and 0 <= ref < len(job["steps"]):
                    st["seq0"] = job["steps"][ref].get("seq0", st["seq0"])
                # a trip this job sent: wait at least until the game's ETA (+30 min) — a ~500 ly surge outlasts the
                # default hour (seen live 2026-10-10: Surveyors' wait timed out mid-surge, the deploys then failed)
                eta = _ts((job.get("etas") or {}).get(st.get("wait_device") or job.get("device") or ""))
                if eta:
                    st["timeout"] = max(st.get("timeout", STEP_TIMEOUT), (eta - _now()).total_seconds() + 1800)
                ok, resp, err = True, None, None
            else:
                if await self._relay_drop_spot(st, job):
                    continue   # the carrier flies to an L4/L5 point first
                if st.get("meet") and await self._meet(st, job):
                    continue   # already beside it (or it can't be found): no trip
                no_spot = None
                if st.get("to_entry") or st.get("entry_of"):
                    no_spot = await self._to_entry(st, job)
                    if no_spot is True:
                        continue   # already there: no trip
                if await self._let_off_riders(st, job):
                    continue   # other fleets' members get off first; this trip goes out after them
                why = await self._beyond_sector(st, job)
                if why:   # beyond the sector range: never sent (owner 2026-11-14), the job stops here
                    await self._block_far(job, st, why)
                    return
                far = no_spot or await self._too_far(st)
                if far and not no_spot:
                    far += await self._lift(st)
                far = far or await self._dark_destination(st, job)
                if far:
                    ok, resp, err = False, None, far
                else:
                    exit_via = await self.plan_via(st, job) or st.get("exit_via")
                    if await self._ramp_legs(st, job):
                        continue   # to the entry point where it lands, then on via the off-ramp
                    if await self._hop_unsettled(st, job):
                        return   # the surge hop before it hasn't landed in the game's eyes yet: next tick
                    ok, resp, err = await self.send(st["method"], st["path"], st["body"], f"auto: {st['desc']}")
                    if not ok and "loop detected" in (err or "").lower() and await self._hop_legs(st, job):
                        await self.log(job["rule"], f"{job['title']}: via {', '.join(exit_via or st['body'].get('via') or [])} refused ({err}); "
                                                    "hopping there first", "warning")
                        continue   # the surge hops inside the system go first, each a trip of its own
                    busy = SERVER_RE.search(err or "") or BUSY_RE.search(err or "")   # struggling server, device mid-trip: the same
                    # trip later (live 2026-11-11: F7E30727's via got a 503, the fallback dropped it, the retry cruised 499 AU)
                    if not ok and exit_via and not busy:   # refused: the route without the surge spots
                        await self.log(job["rule"], f"{job['title']}: via {', '.join(exit_via)} refused ({err}); "
                                                    "the game's own route", "warning")
                        from .hubroute import waypoints
                        rest = waypoints([st["hub_via"]], str(st["body"].get("destination") or "")) if st.get("hub_via") else []
                        st["body"].pop("via", None)
                        if rest:
                            st["body"]["via"] = rest
                        st["exit_via"] = None
                        ok, resp, err = await self.send(st["method"], st["path"], st["body"], f"auto: {st['desc']}")
                    if not ok and st.get("hub_via") and not busy:   # the waypoint refused: the direct route, as planned
                        await self.log(job["rule"], f"{job['title']}: via {st['hub_via']} refused ({err}); going direct",
                                       "warning")
                        st["body"].pop("via", None)
                        st["hub_via"] = None
                        ok, resp, err = await self.send(st["method"], st["path"], st["body"], f"auto: {st['desc']}")
            st["response"] = resp if isinstance(resp, (dict, list)) else None
            if ok and st.get("scan_star") and isinstance(resp, dict) and resp.get("planets") is not None:
                # a system scan a job made (a season rescan): kept like the arrival rule's, entry point to the catalog
                await self.db.execute("INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                                      (st["scan_star"], json.dumps(resp), now_iso()))
                await self.note_entry_point(st["scan_star"], resp.get("entry_point"))
            if ok and job["rule"] == "chain" and self.worker:   # a chain you started: its devices refreshed right away
                self.worker.after_order(st["path"], st["body"], resp)
            if ok and isinstance(resp, dict):
                # long trips: wait at least until the game's own ETA (+30 min) before giving up — this step, and the
                # job's later waits for the same device (travel answers: arrives_at, or a route's time_seconds)
                eta = _trip_eta(resp)
                if eta:
                    st["timeout"] = max(st.get("timeout", STEP_TIMEOUT), (eta - _now()).total_seconds() + 1800)
                    if (st.get("body") or {}).get("command") == "travel" and str(st.get("path") or "").startswith("/devices/"):
                        job.setdefault("etas", {})[st["path"].split("/")[2]] = eta.isoformat(timespec="seconds")
                    if (st.get("body") or {}).get("command") == "travel" or "/travel" in str(st.get("path") or ""):
                        st["eta"] = eta.isoformat(timespec="seconds")
                        await self.log(job["rule"], f"{job['title']}: {st['desc']} — expected {eta_text(eta)}")
            if ok and (st.get("body") or {}).get("command") == "travel" and str(st.get("path") or "").startswith("/devices/"):
                job.setdefault("trips", {})[st["path"].split("/")[2]] = st["body"].get("destination")
            if (not ok and (st.get("body") or {}).get("command") == "deploy"
                    and "already deployed" in (err or "").lower()):
                # seen live (2026-10-04, Surveyors fleet): drones already out — that's what the step wanted
                ok, st["note"] = True, err
                st["wait"] = []
            if (not ok and (st.get("body") or {}).get("command") in ("deactivate", "withdraw", "clear_directive", "release")
                    and re.search(r"nothing to deactivate|not deployed|not launched|no directive|not adopted", (err or "").lower())):
                # stopping something that isn't running (stowed, already idle, never launched): what the step wanted
                ok, st["note"] = True, err
                st["wait"] = []
            if (not ok and (st.get("body") or {}).get("command") == "travel" and "not deployed" in (err or "").lower()
                    and str(st.get("path") or "").startswith("/devices/")):
                # seen live (2026-10-15, Surveyor 3 recall): a controller already stowed in its vessel was told to fly
                # to it and board. Aboard something already: nothing to fly — its waits for this trip are dropped
                code = st["path"].split("/")[2]
                devs_now = await self.devices()
                cur = next((d for d in devs_now if d.get("device_code") == code), {})
                host = next((d for d in devs_now if d.get("device_code") == (cur.get("stowed_in_device_code")
                                                                             or cur.get("attached_to_device_code"))), {})
                fleet_tags = {x for x in cur.get("tags") or [] if x.startswith("fleet:")}
                # only aboard a carrier of its own fleet counts (aboard another fleet's vessel, it still has to move)
                if host and fleet_tags & set(host.get("tags") or []):
                    ok, st["note"] = True, f"{err} — already aboard {cur.get('stowed_in_device_code') or cur.get('attached_to_device_code')}"
                    st["wait"] = []
                    for s2 in job["steps"][job["idx"] + 1:]:
                        if s2.get("method") == "WAIT" and s2.get("wait_device") == code and s2.get("status") == "pending":
                            s2["status"], s2["note"] = "skipped", "already aboard"
            if (not ok and (st.get("body") or {}).get("command") in ("travel", "stow", "attach")
                    and re.search(r"while (scanning|searching)", (err or "").lower()) and not st.get("scan_retry")
                    and str(st.get("path") or "").startswith("/devices/")):
                # live 2026-10-16 (Miner_3, Miner_4): a drone started a scan after the job was planned. Wait for it
                # to finish (scan.completed / search.completed), then try once more
                code = st["body"].get("device") if st["body"].get("command") == "attach" else st["path"].split("/")[2]
                w = step(f"wait for {code} to finish its scan", "", None, method="WAIT",
                         wait=["scan.completed", "search.completed"], timeout=3600)
                w["wait_device"] = code
                again = step(st["desc"], st["path"], dict(st["body"]), wait=list(st.get("wait") or []),
                             timeout=st.get("timeout", STEP_TIMEOUT), critical=st.get("critical", False),
                             match=dict(st.get("match") or {}))
                again["wait_device"], again["scan_retry"] = st.get("wait_device"), True
                job["steps"][job["idx"] + 1:job["idx"] + 1] = [w, again]
                for s2 in job["steps"][job["idx"] + 3:]:
                    if isinstance(s2.get("seq0_from"), int) and s2["seq0_from"] > job["idx"]:
                        s2["seq0_from"] += 2
                ok, st["note"] = True, f"{err} — waiting for {code}'s scan, then again"
                st["wait"] = []
            if (not ok and (st.get("body") or {}).get("command") == "attach" and "already attached" in (err or "").lower()
                    and str(st.get("path") or "").startswith("/devices/")):
                # seen live (2026-10-15, Prospector 4, Miner_3): "Target device is already attached to a carrier".
                # Attached to this carrier or another of its fleet: that's what the step wanted. Attached to another
                # carrier: that carrier detaches it (a deploy only frees a stowed device; the game answers "already
                # deployed", live 2026-10-20 at FALQUORYX). The device list names no carrier: deploy it, then attach again
                carrier, code = st["path"].split("/")[2], st["body"].get("device")
                devs_now = await self.devices()
                by_now = {d.get("device_code"): d for d in devs_now}
                other = (by_now.get(code) or {}).get("attached_to_device_code")
                fleet_tags = {x for x in (by_now.get(carrier) or {}).get("tags") or [] if x.startswith("fleet:")}
                if other and (other == carrier or fleet_tags & set((by_now.get(other) or {}).get("tags") or [])):
                    ok, st["note"] = True, f"{err} — already attached to {other}, a carrier of this fleet"
                    st["wait"] = []
                else:
                    if other and other in by_now:
                        dep = step(f"{other}: detach {code} (for {carrier} to attach it)", f"/devices/{other}",
                                   {"command": "detach", "device": code}, timeout=SHORT_TIMEOUT, critical=True)
                    else:
                        dep = step(f"deploy {code} out of {other or 'the carrier it is on'} (for {carrier} to attach it)",
                                   f"/devices/{code}", {"command": "deploy"}, wait=["device.deployed"], timeout=SHORT_TIMEOUT,
                                   critical=True)
                        dep["wait_device"] = code
                    again = step(st["desc"], st["path"], dict(st["body"]), wait=list(st.get("wait") or []),
                                 timeout=st.get("timeout", STEP_TIMEOUT), critical=st.get("critical", False))
                    again["wait_device"], again["no_retry_attach"] = st.get("wait_device"), True
                    if not st.get("no_retry_attach"):
                        job["steps"][job["idx"] + 1:job["idx"] + 1] = [dep, again]
                        for s2 in job["steps"][job["idx"] + 3:]:
                            if isinstance(s2.get("seq0_from"), int) and s2["seq0_from"] > job["idx"]:
                                s2["seq0_from"] += 2
                        ok, st["note"] = True, (f"{err} — on {other}: detaching it first" if other and other in by_now
                                                else f"{err} — on {other or 'a carrier outside the fleet'}: deploying it first")
                        st["wait"] = []
            m_short = re.search(r"insufficient (\w+) at location: need [\d.]+, have ([\d.]+)", (err or "").lower())
            if (not ok and m_short and (st.get("body") or {}).get("command") == "collect_resources"
                    and str(((job.get("meta") or {}).get("logistics") or {}).get("req") or "").startswith(
                        ("return:", "consolidate:", "evacuate:"))):
                # moving a pile home: someone took part of it since the plan (an AMI ferry, live 2026-10-20 at
                # ALPHARDAN-BELT-1). Load what's there instead; stop only when nothing is left to load
                res = {k: v for k, v in dict(st["body"].get("resources") or {}).items()}
                key = next((k for k in res if k.lower() == m_short.group(1)), None)
                if key is not None and (st.get("short_retries") or 0) < 5:
                    have = math.floor(float(m_short.group(2)))
                    if have >= 1:
                        res[key] = have
                    else:
                        res.pop(key)
                    if res:
                        again = step(st["desc"], st["path"], {**st["body"], "resources": res}, wait=list(st.get("wait") or []),
                                     timeout=st.get("timeout", STEP_TIMEOUT), critical=st.get("critical", False))
                        again["wait_device"], again["short_retries"] = st.get("wait_device"), (st.get("short_retries") or 0) + 1
                        job["steps"][job["idx"] + 1:job["idx"] + 1] = [again]
                        for s2 in job["steps"][job["idx"] + 2:]:
                            if isinstance(s2.get("seq0_from"), int) and s2["seq0_from"] > job["idx"]:
                                s2["seq0_from"] += 1
                        ok, st["note"] = True, f"{err} — loading what is there"
                        st["wait"] = []
            if not ok and "already at destination" in (err or "").lower():
                ok, st["note"] = True, err  # nothing to do: count it as done and move on
                st["wait"] = []
            if (not ok and (st.get("body") or {}).get("command") == "detach"
                    and "not attached to this carrier" in (err or "").lower()):
                # seen live: carriers let go of their cargo on arrival, so the device is already off — that's the goal
                ok, st["note"] = True, err
                st["wait"] = []
            if (not ok and (st.get("body") or {}).get("command") == "stow"
                    and "already stowed" in (err or "").lower()):
                # seen live (2026-10-06, Surveyors recall): the drone was already aboard — that's what the step wanted
                ok, st["note"] = True, err
                st["wait"] = []
            if not ok and (st.get("body") or {}).get("command") == "compact" and _already_compact(err):
                # the device is already compacted (or folding): that's what the step wanted. Still folding: keep
                # waiting for device.compacted; done: move on
                ok, st["note"] = True, err
                if "compacting" not in (err or "").lower():
                    st["wait"] = []
                    from .modular import remember   # so the next pass sends the carrier instead of compacting again
                    await remember(self.db, st["path"].split("/")[2], True, now_iso())
            if not ok and (st.get("body") or {}).get("command") == "unfurl" and _already_unfurled(err):
                ok, st["note"] = True, err   # already unfolded (or unfolding: then keep waiting for device.unfurled)
                if "unfurling" not in (err or "").lower():
                    st["wait"] = []
                from .modular import remember
                await remember(self.db, st["path"].split("/")[2], False, now_iso())
            if not ok and re.match(r"^/locations/[^/]+/events/[^/]+$", st.get("path") or "") \
                    and "already completed" in (err or "").lower():
                # seen live (2026-10-08): "Event already completed by this account" — the contract is done (by an
                # earlier try, or by hand): that's the goal, so the run collects its rewards and goes home
                ok, st["note"] = True, err
                st["wait"] = []
                from .gameevents import DONE_KV
                done = await self.db.kv_get(DONE_KV, {}) or {}
                done[st["path"].rsplit("/", 1)[1]] = now_iso()
                await self.db.kv_set(DONE_KV, done)
            if (not ok and (st.get("body") or {}).get("command") == "change_owner"
                    and "already belongs to that replicant" in (err or "").lower()):
                # seen live (2026-10-06): a second owner pass for a device the first had already moved
                ok, st["note"] = True, err
                st["wait"] = []
            if not ok and (st.get("body") or {}).get("command") == "attach" and "cannot attach a large device" in (err or "").lower():
                await self.learn_no_attach(str((st.get("body") or {}).get("device") or ""))
            if not ok:
                hint = next((h for k, h in (st.get("hint") or {}).items() if k in (err or "").lower()), None)
                st["error"] = err = f"{err} — {hint}" if hint else err
                held = re.search(r"rate limited(?: for|: this kind of request is refused for another) (\d+) s", err or "")
                if held and int(held.group(1)) > 60 and st["tries"] < SERVER_TRIES:
                    # this endpoint's own limit (api.LONG_RETRY): the step waits it out, nothing else is held up
                    st["status"] = "retrying"
                    st["retry_at"] = (_now() + timedelta(seconds=int(held.group(1)) + 5)).isoformat(timespec="seconds")
                    job["status"] = "waiting"
                    await self._update(job)
                    if st["tries"] == 1:
                        await self.log(job["rule"], f"{job['title']}: '{st['desc']}' — the game limits this kind of request; "
                                                    f"trying again in {int(held.group(1)) // 60 + 1} min", "warning")
                    return
                if st["tries"] < 2 and re.search(r"\brate.?limit|too many requests", (err or "").lower()):
                    await self._update(job)
                    return  # retry on next tick
                if st["tries"] < SERVER_TRIES and SERVER_RE.search(err or ""):
                    st["status"] = "retrying"
                    st["retry_at"] = (_now() + timedelta(seconds=SERVER_RETRY_S)).isoformat(timespec="seconds")
                    job["status"] = "waiting"
                    await self._update(job)
                    if st["tries"] == 1:
                        await self.log(job["rule"], f"{job['title']}: '{st['desc']}' — {err}; the game server is "
                                                    f"struggling, trying again every {SERVER_RETRY_S // 60} min")
                    return
                if BUSY_RE.search(err or "") and (st["tries"] < BUSY_TRIES or await self._still_moving(st)) \
                        and not await self._off_course(st, job):
                    # the game refuses while something is still on its way ("Cannot deploy while replicant is
                    # travelling"): try again in a few minutes instead of skipping it
                    eta = await self._moving_eta(st)
                    when = max(eta + timedelta(seconds=60), _now() + timedelta(seconds=60)) if eta \
                        else _now() + timedelta(seconds=BUSY_RETRY_S)
                    st["status"], st["retry_at"] = "retrying", when.isoformat(timespec="seconds")
                    job["status"] = "waiting"
                    await self._update(job)
                    if eta and st.get("logged_eta") != eta.isoformat(timespec="minutes"):
                        st["logged_eta"] = eta.isoformat(timespec="minutes")
                        await self.log(job["rule"], f"{job['title']}: '{st['desc']}' — {err}; still on its way, expected "
                                                    f"{eta_text(eta)} — trying again then")
                    elif not eta and st["tries"] == 1:
                        await self.log(job["rule"], f"{job['title']}: '{st['desc']}' — {err}; trying again every "
                                                    f"{BUSY_RETRY_S // 60} min")
                    return
                if st["critical"]:
                    st["status"] = "failed"
                    job["status"] = "failed"
                    await self._update(job)
                    await self.log(job["rule"], f"stopped: {job['title']} — {st['desc']} failed: {err}{ownership_hint(err)}", stop_level(job), notify=True, job=job)
                    return
                st["status"] = "skipped"
                if st.get("extra_unit"):
                    # a later unit of a deal bought several times: its price isn't all there yet (live 2026-10-28: 5 of
                    # 50 propulsors at ALKALAROPS-OORT, three refusals stopped the job). Skip the other units; they stay
                    # approved for the next delivery (logistics.settle_trades)
                    rest = [x for x in job["steps"][job["idx"] + 1:] if x.get("extra_unit") and x.get("path") == st.get("path")
                            and x.get("status") == "pending"]
                    for x in rest:
                        x["status"], x["error"] = "skipped", err
                    bought = sum(1 for x in job["steps"] if x.get("path") == st.get("path") and x.get("status") == "done")
                    await self.log(job["rule"], f"{job['title']}: {st['desc'].split(' at ')[0]}: bought {bought} of "
                                                f"{bought + 1 + len(rest)} ({err}); the rest stays approved")
                    job["idx"] += 1
                    while job["idx"] < len(job["steps"]) and job["steps"][job["idx"]] in rest:
                        job["idx"] += 1
                    await self._update(job)
                    continue
                quiet = bool(QUIET_SKIP_RE.search(err or ""))
                sg = SALVAGE_GONE_RE.search(err or "")
                if sg and job["rule"] == "salvage_when_depleted" and job.get("meta", {}).get("salvage"):
                    from .salvage import body_of
                    body = body_of(job["meta"]["salvage"])
                    sgone = await self.db.kv_get("salvage_gone", {}) or {}
                    sgone[body] = sorted(set(sgone.get(body) or []) | {sg.group(1).lower()})
                    await self.db.kv_set("salvage_gone", sgone)
                gone = str((st.get("body") or {}).get("destination") or "")
                if GONE_OBJECT_RE.search(err or "") and str(st.get("desc") or "").endswith("(its at: pin)") and gone:
                    # the object it was pinned to is gone (live 2026-11-10, after the Season 4 time skip: 3 propulsors
                    # pinned to SOLARES-OBJ-6, refused every pass): the pin goes, so it's never sent there again
                    code = str(st.get("path") or "").split("/")[2]
                    await self.send("PATCH", f"/devices/{code}", {"configuration": {"remove_tags": [f"at:{gone.lower()}"]}},
                                    f"auto: unpin {code} from {gone} (gone)")
                    err = f"{err}; {gone} is gone, so its at: pin is removed"
                await self.log(job["rule"], f"{job['title']}: skipped '{st['desc']}' ({err}){ownership_hint(err)}",
                               "info" if quiet else "alert")
                job["idx"] += 1
                done_steps = [x for x in job["steps"][:job["idx"]] if x["method"] != "WAIT" and not x.get("extra_unit")
                              and not QUIET_SKIP_RE.search(str(x.get("error") or ""))]
                last3, last6 = done_steps[-3:], done_steps[-6:]
                if ((len(last3) == 3 and all(x["status"] == "skipped" and x.get("error") == err for x in last3))
                        or (len(last6) == 6 and all(x["status"] == "skipped" for x in last6))):
                    job["status"] = "failed"
                    await self._update(job)
                    await self.log(job["rule"], f"stopped: {job['title']} — 3 steps in a row failed with: {err}{ownership_hint(err)}", stop_level(job), notify=True, job=job)
                    return
                await self._update(job)
                continue
            if st["wait"]:
                wait_dev = st.get("wait_device") or job.get("device")
                if await self._already_happened(st, wait_dev) or (st["method"] == "WAIT" and await self._arrived_now(st, job)):
                    st["status"] = "done"
                    job["idx"] += 1
                else:
                    st["status"] = "waiting"
                    job["status"] = "waiting"
                    await self._update(job)
                    return
            else:
                st["status"] = "done"
                job["idx"] += 1
            job["status"] = "running"
            await self._update(job)
            job = await self._get(job["id"])

    async def _moving_eta(self, st: dict) -> datetime | None:
        """When the device a refused step names (or the carrier it rides in, or the device a detach / stow names) is due
        to arrive, from the device list's travel info."""
        path = str(st.get("path") or "")
        if not path.startswith("/devices/"):
            return None
        by = {x.get("device_code"): x for x in await self.devices()}
        cands = [by.get(path.split("/")[2]), by.get(str((st.get("body") or {}).get("device") or "")),
                 by.get(str((st.get("body") or {}).get("target") or ""))]
        etas = []
        for d in [c for c in cands if c]:
            host = by.get(d.get("attached_to_device_code") or d.get("stowed_in_device_code") or "")
            for x in (d, host):
                tr = (x or {}).get("travel") or {}
                t = _ts(tr.get("final_arrives_at") or tr.get("arrives_at"))
                if t and t > _now() - timedelta(seconds=TRIP_GRACE_S):   # due, or only just overdue
                    etas.append(t)
        return max(etas) if etas else None

    async def _still_moving(self, st: dict) -> bool:
        """A refused step whose device (or the carrier it rides in) is still on a long trip: keep retrying past the usual
        3 h until that trip's ETA (+30 min) (seen live 2026-10-11: a detach after a ~500 ly carry gave up mid-surge)."""
        from .loadouts import in_transit
        eta = await self._moving_eta(st)
        if not eta or _now() > eta + timedelta(seconds=TRIP_GRACE_S):
            return False
        path = str(st.get("path") or "")
        if not path.startswith("/devices/"):
            return False
        by = {x.get("device_code"): x for x in await self.devices()}
        d = by.get(path.split("/")[2])
        target = (st.get("body") or {}).get("device")   # detach / attach name the carried device
        return any(x and in_transit(x, by) for x in (d, by.get(target or "")))

    async def _trip_device(self, st: dict) -> str | None:
        """The device a travel step moves: /devices/X {command: travel} moves X; /replicants/R/travel moves R's host
        vessel (live 2026-10-30: a repair patrol sent 5DF3161B by its replicant from ISOLITHOR-1-L4 to ISOLITHOR-BELT-1,
        and with no surge hop planned for replicant trips it cruised 499 AU, 56 min). None for any other step."""
        body, path = st.get("body") or {}, str(st.get("path") or "")
        if path.startswith("/devices/") and body.get("command") == "travel":
            return path.split("/")[2]
        if not (path.startswith("/replicants/") and path.endswith("/travel")):
            return None
        if st.get("wait_device"):
            return str(st["wait_device"])
        r = path.split("/")[2]
        return next((x.get("device_code") for x in await self.devices()
                     if (x.get("hosting_replicant") or {}).get("replicant_code") == r), None)

    @staticmethod
    def _travel_body(path: str, dest: str) -> dict:
        """A travel step's body for `path`: a replicant's /travel takes no command."""
        return {"destination": dest} if path.startswith("/replicants/") else {"command": "travel", "destination": dest}

    async def plan_via(self, st: dict, job: dict) -> list[str]:
        """A device trip's waypoints, set once in the step's body `via`: the surge spots (_exit_via) first, then any hub
        waypoint (_hub_via), then the off-ramp. A spot in another system than the stop before it gets
        its star first (live 2026-10-24: "Cannot travel directly from CAELZAROR to ISOLITHOR-KUIPER (different systems)"),
        and a destination in another system than the last waypoint gets its star last (live 2026-10-26: "Cannot travel
        directly from ISOLITHOR-KUIPER to KHAZENEN-BELT-1"). Returns the surge spots."""
        from . import hubroute
        exit_via = await self._exit_via(st, job)
        await self._hub_via(st, job)
        if exit_via:
            ramp = [x for x in exit_via if x == st.get("arrive_via")]
            hubs_ = [x for x in st["body"].get("via") or [] if x not in exit_via]
            code = await self._trip_device(st)
            d = next((x for x in await self.devices() if x.get("device_code") == code), None) or {}
            st["body"]["via"] = hubroute.waypoints(
                hubroute.stars_between(d.get("location") or "", [x for x in exit_via if x not in ramp] + hubs_ + ramp),
                str(st["body"].get("destination") or "").upper())
        return exit_via

    async def trip_via(self, device: str, dest: str) -> list[str]:
        """The waypoints the job runner would give `device`'s trip to `dest` (surge hops, a hub, the off-ramp), for a
        trip sent by hand (a replicant's travel form). Nothing is logged."""
        return (await self.trip_plan(device, dest))[0]

    async def trip_plan(self, device: str, dest: str) -> tuple[list[str], str | None]:
        """trip_via's waypoints, and the off-ramp one trip can't take: in a system whose surges land at its entry point
        the job runner splits the trip there (_ramp_legs: to the entry point, then on via the off-ramp). A hand-sent
        trip with such an off-ramp goes as a job too (live 2026-11-03: 5DF3161B and 2D6BFDF1, sent from DELTA to
        ISOLITHOR-BELT-1 by their travel forms, landed at ISOLITHOR-1-L4 and cruised 499 AU, 57 min)."""
        st = {"path": f"/devices/{device}", "body": {"command": "travel", "destination": dest}, "desc": ""}
        await self.plan_via(st, {"rule": None, "title": ""})
        via = list(st["body"].get("via") or [])
        ramp = st.get("arrive_via")
        split = None
        if ramp in via and await self._hub_entry(star_of(ramp)):
            split = ramp
            # one trip can't take the off-ramp where surges land at the entry point (it cruises from there): leave it out
            via = via[:via.index(ramp)]
            while via and star_of(via[-1]) == star_of(ramp):
                via.pop()
            from . import hubroute
            via = hubroute.waypoints(via, dest.upper()) if via else []
        return via, split

    async def _hop_legs(self, st: dict, job: dict) -> bool:
        """A trip inside one system whose surge spots the game refused as a `via` (live 2026-10-27: F7E30727
        ISOLITHOR-1-L4 → ISOLITHOR-BELT-1 via ISOLITHOR-KUIPER: "Loop detected: already at ISOLITHOR", then cruised 499 AU
        for 57 min; from the belt to ISOLITHOR-1-L4 the same via was taken). Each spot becomes a trip of its own ahead of
        this one (a 30 s surge hop), and this one then cruises the rest.
        A replicant's trip (/replicants/{code}/travel, a hand-sent chain with the planned `via`) hops the same way.
        True when steps were added ahead of this one (it runs after them)."""
        body, path = st.get("body") or {}, str(st.get("path") or "")
        via = [x for x in body.get("via") or [] if "-" in x]
        rep = path.startswith("/replicants/")
        code = await self._trip_device(st)
        if not via or not code:
            return False
        dest = str(body.get("destination") or "").upper()
        d = next((x for x in await self.devices() if x.get("device_code") == code), None) or {}
        if star_of(d.get("location")) != star_of(dest) or any(star_of(x) != star_of(dest) for x in via):
            return False
        add = []
        for x in via:
            h = step(f"{code} → {x} (surge hop)", path, {"destination": x} if rep else {"command": "travel", "destination": x},
                     wait=["travel.arrived"],
                     match={"destination": x})
            h.update(wait_device=code, exit_checked=True, hub_checked=True, riders_checked=True)
            if add:
                h["from_spot"] = add[-1]["body"]["destination"]
            add.append(h)
        body.pop("via", None)
        st["exit_via"] = None
        st["from_spot"] = via[-1]
        i = job["idx"]
        for s2 in job["steps"][i:]:
            if isinstance(s2.get("seq0_from"), int) and s2["seq0_from"] >= i:
                s2["seq0_from"] += len(add)
        job["steps"][i:i] = add
        st["tries"] -= 1
        await self._update(job)
        return True

    async def _hop_unsettled(self, st: dict, job: dict) -> bool:
        """A trip that starts where the trip before it ended (`from_spot`: a surge hop's spot, or the entry point before
        the hop) goes only once the game shows the device standing there (live 2026-11-03: F7E30727 and 5DF3161B hopped
        ISOLITHOR-1-L4 → ISOLITHOR-KUIPER, and the trip on to ISOLITHOR-BELT-1, sent the moment the hop's arrival came
        in, was routed from ISOLITHOR-1-L4 and cruised 499 AU, 57 min, instead of 29 AU from the Kuiper belt). The
        device is read fresh; while it isn't there yet the step waits a tick (up to HOP_SETTLE_TRIES), then goes anyway.
        True when the step waits."""
        from . import fleets as fl
        spot = str(st.get("from_spot") or "").upper()
        code = await self._trip_device(st) if spot else None
        if not code:
            return False
        try:
            d = await self.api.request("GET", f"/devices/{code}", background=True) or {}
        except ApiError:
            d = {}
        moving = bool(d) and (bool(fl.in_flight(d)) or str(d.get("status") or "").lower().startswith(fl.MOVING))
        here = str(d.get("location") or "").upper()
        n = int(st.get("settle_waits") or 0)
        if not d or (here == spot and not moving) or n >= HOP_SETTLE_TRIES:
            if d and here != spot and n >= HOP_SETTLE_TRIES:
                await self.log(job["rule"], f"{job['title']}: {code} still not shown at {spot} ({here or 'no location'}); "
                                            f"{st['desc']} goes anyway", "warning")
            st.pop("from_spot", None)
            return False
        st["settle_waits"] = n + 1
        st["tries"] -= 1
        st["status"], job["status"] = "retrying", "waiting"
        st["retry_at"] = (_now() + timedelta(seconds=HOP_SETTLE_S)).isoformat(timespec="seconds")
        await self._update(job)
        return True

    async def _hub_entry(self, star: str) -> str | None:
        """Where surges into `star` land when that's its entry point: once a scan has found it, or with an active hub
        there (anyone's; game docs, owner 2026-10-25). None otherwise (they land at the Oort cloud or Kuiper belt)."""
        from . import wardhub as wh
        _, entry = await self._surge_spots(star, await self.cruise_radii())
        if not entry:
            return None
        hubs = {star_of(x.get("location")) for x in await self.devices() if wh.is_hub(x) and wh.active(x)} \
            | await self.star_lookup("hubs")
        return entry if star in hubs or await self.system_scan(star) else None

    async def _ramp_legs(self, st: dict, job: dict) -> bool:
        """An off-ramp (a surge spot in the destination system, _exit_via) into a system whose surges land at its entry
        point (_hub_entry), where the game then cruises to the off-ramp as a via (live 2026-10-28: F7E30727
        ANLOREN → ISOLITHOR-BELT-1 via ISOLITHOR-KUIPER cruised 527 AU from ISOLITHOR-1-L4, 49 min, though the game's
        route priced that leg at 13 s; three Delta trips waited out their hour). The trip is split: to the entry point,
        then this trip from there via the off-ramp (a 30 s hop, then the rest).
        True when steps were added ahead of this one (it runs after them)."""
        from . import hubroute
        body, path = st.get("body") or {}, str(st.get("path") or "")
        ramp = st.get("arrive_via")
        code = await self._trip_device(st)
        if not ramp or ramp not in (body.get("via") or []) or not code:
            return False
        ds = star_of(ramp)
        entry = await self._hub_entry(ds)
        if not entry or entry == ramp:
            return False
        via = list(body.get("via") or [])
        before = via[:via.index(ramp)]
        while before and star_of(before[-1]) == ds:   # the star stars_between put ahead of the off-ramp
            before.pop()
        flags = {"wait_device": code, "exit_checked": True, "hub_checked": True, "riders_checked": True, "sector_ok": True}
        first = step(f"{code} → {entry} (entry point, then on via {ramp})", path,
                     self._travel_body(path, entry) | ({"via": hubroute.waypoints(before, entry)} if before else {}),
                     wait=["travel.arrived"], match={"destination": entry})
        first.update(flags)
        add = [first]
        # from the entry point one trip with the off-ramp as its via: the game hops there, then cruises the rest (live
        # 2026-11-03: 35DD7376 ISOLITHOR-1-L4 via ISOLITHOR-KUIPER, 402 s). A hop of its own, then the rest, cruised the
        # whole way (live 2026-11-11: DCB956D8 and 74ECB101 shown at ISOLITHOR-KUIPER after the hop, yet the game routed
        # the trip on from ISOLITHOR-1-L4, 499 AU, 57 min). "Loop detected" still splits it (_hop_legs).
        body["via"] = hubroute.waypoints([ramp], str(body.get("destination") or "").upper())
        st["from_spot"] = entry
        st.update(exit_checked=True, hub_checked=True)
        st["exit_via"], st["arrive_via"] = [ramp], None
        i = job["idx"]
        for s2 in job["steps"][i:]:
            if isinstance(s2.get("seq0_from"), int) and s2["seq0_from"] >= i:
                s2["seq0_from"] += len(add)
        job["steps"][i:i] = add
        st["tries"] -= 1
        await self._update(job)
        if job.get("rule"):
            await self.log(job["rule"], f"{job['title']}: {code} flies to {entry} first, then on to "
                                        f"{body.get('destination')} (surges into {ds} land at its entry point)")
        return True

    async def _to_entry(self, st: dict, job: dict) -> bool | str | None:
        """Since game v4.0.0 an FTL relay switches on only at an L4/L5 point (live 2026-11-09: 18 relays dropped at the
        Oort cloud or Kuiper belt of new systems by two relay runs, "FTL relay can only be deployed at L4 or L5 Lagrange
        points", left dark out of everyone's reach). A carrier's trip marked `to_entry: STAR` goes to the system's entry
        point when it's an L4/L5 point, else another one known there; the catalog knows no entry point before a system is
        explored, so the carrier's replicant scans it first. The spot is kept in the job (meta entries) for steps marked
        `entry_of: STAR` (a relay's trip there). True: already there (the step is skipped); a text: no L4/L5 known."""
        from .placement import geography, is_lagrange
        star = str(st.get("to_entry") or st.get("entry_of") or "").upper()
        entries = job.setdefault("meta", {}).setdefault("entries", {})
        target = entries.get(star) if st.get("entry_of") else None
        code = await self._trip_device(st)
        devices = await self.devices()
        if not target:
            entry = str(((await self.star_lookup("by")).get(star) or {}).get("entry_point") or "").upper()
            scan = None
            if not is_lagrange(entry):
                path = str(st.get("path") or "")
                rep = path.split("/")[2] if path.startswith("/replicants/") else next(
                    ((x.get("hosting_replicant") or {}).get("replicant_code") for x in devices
                     if x.get("device_code") == code and x.get("hosting_replicant")), None)
                if rep:
                    ok, resp, _err = await self.send("POST", f"/replicants/{rep}/scan", {}, f"auto: scan {star} for an L4/L5 point")
                    scan = resp if ok and isinstance(resp, dict) else None
            lp = geography(star, devices, scan, entry if is_lagrange(entry) else None)["lagrange"]
            target = lp[0] if lp else ""
        if not target:
            return f"no L4/L5 point known in {star} (an FTL relay switches on only there)"
        entries[star] = target
        try:   # where it is now (the cached list can still show where it was before this trip)
            here = str((await self.api.request("GET", f"/devices/{code}", background=True) or {}).get("location") or "").upper()
        except ApiError:
            here = next((str(x.get("location") or "").upper() for x in devices if x.get("device_code") == code), "")
        if here == target:
            st["status"] = "skipped"
            await self._update(job)
            return True
        st["body"]["destination"] = target
        st["match"] = {**(st.get("match") or {}), "destination": target}
        st["desc"] = f"{code} → {target} (an L4/L5 point: relays switch on only there)"
        return None

    async def _relay_drop_spot(self, st: dict, job: dict) -> bool:
        """A relay deployed out of a carrier's hold and switched on later in the job is dropped at an L4/L5 point only
        (game v4.0.0): a carrier anywhere else flies to one first (a trip marked to_entry, inserted before this step).
        This also covers jobs planned before that rule. True when the trip was inserted."""
        from .placement import is_lagrange
        from .relaynet import RELAY_TYPES
        body, path = st.get("body") or {}, str(st.get("path") or "")
        if body.get("command") != "deploy" or not path.startswith("/devices/") or st.get("spot_checked"):
            return False
        st["spot_checked"] = True
        code = path.split("/")[2]
        later = job["steps"][job["idx"] + 1:]
        if not any(x.get("path") == path and (x.get("body") or {}).get("command") == "activate" for x in later):
            return False
        devices = await self.devices()
        d = next((x for x in devices if x.get("device_code") == code), None) or {}
        carrier = d.get("stowed_in_device_code")
        if (d.get("device_type") or "") not in RELAY_TYPES or not carrier:
            return False
        c = next((x for x in devices if x.get("device_code") == carrier), None) or {}
        trips = [x for x in job["steps"][:job["idx"]] if x.get("status") == "done" and (x.get("body") or {}).get("destination")
                 and x.get("wait_device") == carrier]
        loc = str((trips[-1]["body"]["destination"] if trips else c.get("location")) or "").upper()
        if not loc or is_lagrange(loc):
            return False
        rep = (c.get("hosting_replicant") or {}).get("replicant_code")
        tpath = f"/replicants/{rep}/travel" if rep else f"/devices/{carrier}"
        star = star_of(loc)
        go = step(f"{carrier} → {star} (an L4/L5 point: relays switch on only there)", tpath, self._travel_body(tpath, star),
                  wait=["travel.arrived"], match={"destination": star}, critical=True)
        go.update(wait_device=carrier, to_entry=star)
        i = job["idx"]
        for s2 in job["steps"][i:]:
            if isinstance(s2.get("seq0_from"), int) and s2["seq0_from"] >= i:
                s2["seq0_from"] += 1
        job["steps"][i:i] = [go]
        st["tries"] -= 1
        await self._update(job)
        return True

    async def _meet(self, st: dict, job: dict) -> bool:
        """A carrier's trip to a device known only by its star (the game names only the star of a device out of command
        range: live 2026-10-28, a rescue's carrier sent to ALKALAROPS for a device at ALKALAROPS-OORT): with the replicant
        in the system now, the device's own spot is read and the trip (and its wait) goes there. True when there's no trip
        to make (already beside it, or still no spot): the step and its wait are skipped."""
        code = st["meet"]
        carrier = await self._trip_device(st) or str(st.get("path") or "").split("/")[2]
        try:
            d = await self.api.request("GET", f"/devices/{code}", background=True) or {}
        except ApiError:
            d = {}
        loc = str(d.get("location") or "")
        here = next((x.get("location") for x in await self.devices() if x.get("device_code") == carrier), None)
        wait = next((w for w in job["steps"][job["idx"] + 1:] if w.get("meet") == code and w.get("method") == "WAIT"), None)
        if not loc or loc == star_of(loc) or loc == here:
            for x in (st, wait):
                if x:
                    x["status"] = "skipped"
            await self._update(job)
            return True
        st["body"]["destination"] = loc
        st["desc"] = f"{carrier} → {loc} (to {code})"
        if wait:
            wait["match"] = {"destination": loc}
            wait["desc"] = f"wait for {carrier} at {loc}"
        return False

    async def _hub_via(self, st: dict, job: dict) -> None:
        """A device's trip to another system goes through a hub (ours or another player's: any star the catalog flags
        has_hub) when the game's previews say that's enough faster (hubroute.py): sets the step's `via` once, before it's first sent. Previews that fail leave it direct."""
        from . import hubroute
        from . import wardhub as wh
        body = st.get("body") or {}
        path = str(st.get("path") or "")
        code = await self._trip_device(st)
        if (not code or "via" in body or body.get("dry_run") or st.get("hub_checked") or (job.get("meta") or {}).get("sim")):
            return
        st["hub_checked"] = True
        dest = str(body.get("destination") or "")
        devs = await self.devices()
        d = next((x for x in devs if x.get("device_code") == code), None) or {}
        # a hub slows every ship arriving in its system, whoever owns it (owner 2026-10-22)
        hubs = {star_of(x.get("location")) for x in devs if wh.is_hub(x) and wh.active(x)} | await self.star_lookup("hubs")
        if not d.get("location") or not hubs:
            return
        pos = await self.star_lookup("pos")
        cands = hubroute.candidates(star_of(d["location"]), star_of(dest), hubs, pos)
        if not cands:
            return

        async def preview(extra: dict) -> float | None:
            try:
                return hubroute.total_seconds(await self.api.request("POST", path, json_body={**body, **extra, "dry_run": True},
                                                                     background=True))
            except ApiError:
                return None
        direct = await preview({})
        best = None
        for h in cands:
            t = await preview({"via": hubroute.waypoints([h], dest)})
            if hubroute.worth_it(direct, t) and (best is None or t < best[1]):
                best = (h, t)
        if best:
            body["via"], st["hub_via"] = hubroute.waypoints([best[0]], dest), best[0]
            st["desc"] += f" (via hub {best[0]})"
            if job.get("rule"):
                await self.log(job["rule"], f"{job['title']}: {code} → {dest} via the hub at {best[0]}: "
                                            f"{hubroute.hm(best[1])} instead of {hubroute.hm(direct)}")

    async def _dark_destination(self, st: dict, job: dict) -> str | None:
        """A trip planned unescorted into comms coverage that has since gone dark: don't leave (live 2026-10-20: two
        trade freighters planned for SOL-3-1 at 13:22 left at 16:03 and arrived out of comms range, cargo aboard)."""
        star = st.get("need_comms")
        if not star or (st.get("body") or {}).get("command") != "travel":
            return None
        devices = await self.devices()
        if star in await self.commandable(devices):
            return None
        from .logistics import riding_hosts
        reps = await self.db.kv_get("replicants", {}) or {}
        hosts = riding_hosts(devices, {r.get("hosted_device_code"): c for c, r in reps.items() if r.get("hosted_device_code")})
        if set(hosts) & set([job.get("device")] + list((job.get("meta") or {}).get("devices") or [])):
            return None   # a replicant flies with this job: it brings comms along
        return f"{star} is out of comms range now (no relay network or replicant there): not leaving without an escort"

    async def _surge_spots(self, star: str, radii: dict[str, float]) -> tuple[set[str], str]:
        """A system's surge-capable places and its entry point. The game docs (owner 2026-10-25) name three: the Oort
        cloud, the Kuiper belt and the entry point (usually a planet's L5 next to the belt). Live dry runs agree: from
        FALQUORYX-1-L4 (its entry point) the Kuiper belt and the Oort cloud were 30 s `surge_hop`s, another planet's L4
        and its own L5 a cruise."""
        entry = str(((await self.star_lookup("by")).get(star) or {}).get("entry_point") or "").upper()
        return ({x.upper() for x in radii if star_of(x) == star and x.upper().endswith(("-OORT", "-KUIPER"))}
                | ({entry} - {""})), entry

    async def _exit_via(self, st: dict, job: dict) -> list[str]:
        """Surge hops (owner 2026-10-22): a surge-capable device hops in seconds between a system's surge-capable places
        (_surge_spots), then cruises the rest, and the game routes through them when they're in the trip's `via`
        (ISOLITHOR-BELT-1 → ISOBAREA via ISOLITHOR-KUIPER: 515 s, instead of 3544 s by a 499 AU cruise to the entry
        point). The waypoints this trip gets, once, before it's first sent:
          • from a belt or a body: the nearest such place, when the game would cruise much further (leaving the system,
            it cruises to the entry point; inside it, to the destination);
          • inside the system: then the one nearest the destination (Delta Force: 499 AU, 57 min from ISOLITHOR-1-L4 to
            a belt next to the Kuiper belt).
        Each needs to save at least 30 AU and a quarter of the cruise."""
        from .fleets import cruise_au
        body, path = st.get("body") or {}, str(st.get("path") or "")
        code = await self._trip_device(st)
        if (not code or st.get("exit_checked") or body.get("via") or body.get("dry_run")
                or (job.get("meta") or {}).get("sim")):
            return []
        st["exit_checked"] = True
        dest = str(body.get("destination") or "").upper()
        d = next((x for x in await self.devices() if x.get("device_code") == code), None) or {}
        here = str(d.get("location") or "").upper()
        if "surge" not in (d.get("features") or []) or not here or here == dest:
            return []
        radii = await self.cruise_radii()
        hs = star_of(here)
        spots, entry = await self._surge_spots(hs, radii)
        def gap(x: str, y: str) -> float:
            return cruise_au(x, y, radii)
        pos, via, saved_from = here, [], 0.0
        at_spot = here in spots
        inside = star_of(dest) == hs
        if not at_spot and (inside or entry):
            game = gap(here, entry) if not inside else gap(here, dest)
            near = min(spots, key=lambda x: (gap(here, x), x), default=None)
            tail = min((gap(x, dest) for x in spots), default=game) if inside else 0.0
            if near and game - (gap(here, near) + tail) >= max(30.0, game / 4):
                via, pos, saved_from = [near], near, game
        if inside and (via or at_spot):
            now_au = gap(pos, dest)
            hop = min(spots - {pos}, key=lambda x: (gap(x, dest), x), default=None)
            if hop and now_au - gap(hop, dest) >= max(30.0, now_au / 4):
                via.append(hop)
                saved_from = saved_from or now_au
        ds = star_of(dest)
        if not inside and not re.fullmatch(rf"{re.escape(ds)}-\d+-L[45]", dest):
            # the off-ramp (owner 2026-10-22: 2D6BFDF1 surged to ISOLITHOR-1-L4, then cruised 499 AU to ISOLITHOR-BELT-1):
            # the destination system's surge spot nearest the destination, when the game would arrive much further off
            # (at the entry point; not known: an inner L-point, as seen live)
            dspots, dentry = await self._surge_spots(ds, radii)
            if dest not in dspots:
                game = gap(dentry or f"{ds}-1-L4", dest)
                ramp = min(dspots, key=lambda x: (gap(x, dest), x), default=None)
                if ramp and game - gap(ramp, dest) >= max(30.0, game / 4):
                    via.append(ramp)
                    st["arrive_via"] = ramp
                    saved_from += game
        if via:
            st["exit_via"] = via
            st["desc"] += f" (surge hop via {', '.join(via)})"
            if job.get("rule"):
                await self.log(job["rule"], f"{job['title']}: {code} → {dest} via {', '.join(via)} (surge hop), instead of "
                                            f"≈{saved_from:.0f} AU of cruise")
        return via

    async def _beyond_sector(self, st: dict, job: dict) -> str | None:
        """The sector range (owner 2026-10-22: logistics sent freighter 2D6BFDF1 from SOLARES on a 241 h trip to
        ISOLITHOR to fetch a stockpile): an automated trip longer than logistics.SECTOR_RANGE_H at the game's surge pace,
        or of unknown length, is never sent (owner 2026-11-14; it waited for a Confirm before). Measured from the catalog; a trip over the range or off
        the catalog by the game's preview of its planned route (a hub can bring it under).
        Trips you asked for (a forced job, an allow_far step), simulations and previews go as they are."""
        from . import hubroute
        from .logistics import SECTOR_RANGE_H, SURGE_S_PER_LY
        body, path = st.get("body") or {}, str(st.get("path") or "")
        rep = path.startswith("/replicants/") and path.endswith("/travel")
        if (not body.get("destination") or not (rep or (body.get("command") == "travel" and path.startswith("/devices/")))
                or body.get("dry_run") or st.get("allow_far") or st.get("sector_ok") or job.get("forced")
                or (job.get("meta") or {}).get("sim")):
            return None
        if st.get("sector_h") is None:
            devs = {x.get("device_code"): x for x in await self.devices()}
            code = path.split("/")[2]
            if rep:
                code = ((await self.db.kv_get("replicants", {}) or {}).get(code) or {}).get("hosted_device_code") or ""
            d = devs.get(code) or {}
            car = devs.get(d.get("stowed_in_device_code") or d.get("attached_to_device_code") or "") or {}
            here, there = star_of(d.get("location") or car.get("location")), star_of(str(body["destination"]).upper())
            pos = await self.star_lookup("pos")
            hours = None
            if here and here == there:
                hours = 0.0
            elif here in pos and there in pos:
                pa, pb = pos[here], pos[there]
                hours = math.dist([float(pa.get(k) or 0) for k in "xyz"], [float(pb.get(k) or 0) for k in "xyz"]) * SURGE_S_PER_LY / 3600
            if code and (hours is None or hours > SECTOR_RANGE_H):
                # over the range by the catalog, or not in it: the game's time for the route the trip will take
                # (surge hops, a hub, the off-ramp), else its own route
                via = body.get("via") or await self.trip_via(code, str(body["destination"]).upper())
                for v in ([via] if via else []) + [None]:
                    try:
                        secs = hubroute.total_seconds(await self.api.request(
                            "POST", path, json_body={**{k: x for k, x in body.items() if k != "via"},
                                                     **({"via": v} if v else {}), "dry_run": True}, background=True))
                    except ApiError:
                        continue
                    if secs is not None:
                        hours = secs / 3600
                        break
            st["sector_h"] = round(hours, 1) if hours is not None else -1.0
            st["sector_leg"] = f"{here}>{there}" if here and there else None
        h = st["sector_h"]
        if 0 <= h <= SECTOR_RANGE_H:
            return None
        return (f"about {h:.0f} h of travel, beyond the {SECTOR_RANGE_H:.0f} h sector range" if h >= 0
                else "a trip of unknown length (beyond the sector range?)")

    async def _block_far(self, job: dict, st: dict, why: str) -> None:
        """A trip beyond the sector range: the job stops, nothing is sent, and the leg is remembered (KV_SECTOR_FAR) so
        no planner starts the same trip again for a week, 6 h when its length is unknown (create_job checks it)."""
        st["status"], st["error"], job["status"] = "failed", f"not sent: {why}", "failed"
        job["finished_at"] = now_iso()
        st.pop("hold", None)
        await self._update(job)
        leg = st.get("sector_leg")
        if leg:   # an unknown length (the game's preview failed too) is remembered for 6 h only: it may be a hiccup
            h = st.get("sector_h")
            far = await self.db.kv_get(KV_SECTOR_FAR, {}) or {}
            far[leg] = {"at": now_iso(), "h": h if isinstance(h, (int, float)) and h >= 0 else None}
            await self.db.kv_set(KV_SECTOR_FAR, far)
        await self.log(job["rule"], f"stopped: {job['title']}: {st['desc']} not sent — {why}", "warning", job=job)

    async def _far_steps(self, steps: list[dict]) -> str | None:
        """Before a job starts: a trip in it on a leg the job runner found beyond the sector range in the last week (its
        planned route measured by the game, a hub can bring a long straight line under). Returns why, or None."""
        from .logistics import SECTOR_RANGE_H
        trips = [st for st in steps if isinstance(st.get("body"), dict) and st["body"].get("destination")
                 and not st["body"].get("dry_run") and not st.get("allow_far") and not st.get("sector_ok")
                 and ((st["body"].get("command") == "travel" and str(st.get("path") or "").startswith("/devices/"))
                      or (str(st.get("path") or "").startswith("/replicants/") and str(st.get("path")).endswith("/travel")))]
        if not trips:
            return None
        devs = {x.get("device_code"): x for x in await self.devices()}
        far = await self.db.kv_get(KV_SECTOR_FAR, {}) or {}
        if not far:
            return None
        reps = await self.db.kv_get("replicants", {}) or {}
        week = _now() - timedelta(days=7)
        seen: dict[str, str] = {}
        for st in trips:
            path = str(st["path"])
            code = path.split("/")[2]
            if path.startswith("/replicants/"):
                code = (reps.get(code) or {}).get("hosted_device_code") or ""
            d = devs.get(code) or {}
            car = devs.get(d.get("stowed_in_device_code") or d.get("attached_to_device_code") or "") or {}
            here = seen.get(code) or star_of(d.get("location") or car.get("location"))
            there = star_of(str(st["body"]["destination"]).upper())
            seen[code] = there
            if not here or not there or here == there:
                continue
            e = far.get(f"{here}>{there}")
            e = e if isinstance(e, dict) else {"at": e, "h": 0}
            at = _ts(e.get("at"))
            if at and at > (week if e.get("h") is not None else _now() - timedelta(hours=6)):
                return (f"{here} → {there} was found beyond the {SECTOR_RANGE_H:.0f} h sector range"
                        + (f" (about {e['h']:.0f} h)" if e.get("h") else " (length unknown)"))
        return None

    async def confirm_trip(self, job_id: str) -> bool:
        """Your Confirm for a trip held at the sector range: it goes out now."""
        async with self.lock:
            job = await self._get(job_id)
            if not job or job["status"] != "waiting" or job["idx"] >= len(job["steps"]):
                return False
            st = job["steps"][job["idx"]]
            if st.get("status") != "confirm":
                return False
            st["sector_ok"], st["status"], job["status"] = True, "pending", "running"
            st.pop("hold", None)
            await self._update(job)
            await self.log(job["rule"], f"{job['title']}: {st['desc']} confirmed beyond the sector range")
            await self._advance(job_id)
            return True

    async def _too_far(self, st: dict) -> str | None:
        """The one check every automated trip goes through: a drone or AMI controller isn't sent on a long cruise inside
        a system (seen live 2026-10-11: a survey drone at an Oort cloud sent ~320 min to the inner system). Over the
        loadout settings' max cruise (AU) the step isn't sent; fleets fetch such devices with a carrier instead. A step
        marked allow_far (a follow-up you asked for yourself) is sent as is. Returns why not, or None."""
        body = st.get("body") or {}
        path = str(st.get("path") or "")
        if body.get("command") != "travel" or st.get("allow_far") or not path.startswith("/devices/"):
            return None
        code = path.split("/")[2]
        d = next((x for x in await self.devices() if x.get("device_code") == code), None) or {}
        t = str(d.get("device_type") or "")
        dest, here = str(body.get("destination") or ""), d.get("location")
        from .fleets import cruise_au
        if not ("drone" in t or "controller" in t) or not here or star_of(here) != star_of(dest) or "surge" in (d.get("features") or []):
            return None
        au, limit = cruise_au(here, dest, await self.cruise_radii()), await self.max_cruise_au()
        if au <= limit:
            return None
        return (f"not sent: {code} would cruise ≈{au:.0f} AU from {here} to {dest} (over the {limit:g} AU limit in "
                "Fleets › settings) — a carrier should fetch it")

    async def _lift(self, st: dict) -> str:
        """A trip refused by _too_far: the device is tagged `at:<destination>` + `lift`, so the loadouts pass sends a
        carrier to take it there (loadouts.LIFT; owner 2026-10-30: before, the same refusal came back every pass and
        nothing fetched it). A device already pinned somewhere else keeps its pin. Returns a note for the error."""
        from . import loadouts as lo
        code, dest = st["path"].split("/")[2], str((st.get("body") or {}).get("destination") or "")
        d = next((x for x in await self.devices() if x.get("device_code") == code), None) or {}
        pin, tags = lo.pinned_at(d), d.get("tags") or []
        if pin and pin != dest.upper():
            return f" (pinned to {pin}: not tagged for a carrier)"
        if lo.LIFT in tags and pin:
            return " (already tagged for a carrier)"
        add = [t for t in (lo.at_tag(dest), lo.LIFT) if t not in tags]
        ok, _, err = await self.send("PATCH", f"/devices/{code}", {"configuration": {"add_tags": add}},
                                     f"auto: tag {code} for a carrier to {dest}")
        if not ok:
            return f" (tagging it for a carrier failed: {err})"
        return f"; tagged {' '.join(add)}: a carrier takes it there"

    async def _arrived_now(self, st: dict, job: dict) -> bool:
        """A wait for an arrival whose device is already standing at the destination (seen live 2026-10-11: Surveyors'
        assemble waited for its survey controller to land at SOLARES-BELT-1 when it already had, before the job began —
        the event was older than the wait, and nothing moved for half an hour)."""
        if "travel.arrived" not in (st.get("wait") or []):
            return False
        path = str(st.get("path") or "")
        code = st.get("wait_device") or (path.split("/")[2] if path.startswith("/devices/") else None) or job.get("device")
        dest = str((st.get("match") or {}).get("destination") or (st.get("body") or {}).get("destination") or "").upper()
        by = {x.get("device_code"): x for x in await self.devices()}
        d, seen = by.get(code), set()
        # already aboard a carrier standing there counts too (live 2026-10-18: a survey controller with recall on stowed
        # its drones in the fleet's vessel itself, and the recall waited out an hour per drone)
        while d and not d.get("location") and (d.get("stowed_in_device_code") or d.get("attached_to_device_code")) \
                and d.get("device_code") not in seen:
            seen.add(d.get("device_code"))
            d = by.get(d.get("stowed_in_device_code") or d.get("attached_to_device_code"))
        if not d or not dest or not d.get("location") or d.get("location_stale"):
            return False
        if str(d.get("status") or "").lower().startswith(("travel", "cruis", "surg", "moving", "recalling", "returning")):
            return False
        loc = str(d["location"]).upper()
        return loc == dest or ("-" not in dest and star_of(loc) == dest)

    async def _not_arrived(self, st: dict, job: dict) -> str | None:
        """A wait for an arrival that's over: is the device in the system it was sent to? None = yes (or unknown)."""
        code = st.get("wait_device") or job.get("device")
        dest = (st.get("match") or {}).get("destination") or (job.get("trips") or {}).get(code or "")
        d = next((x for x in await self.devices() if x.get("device_code") == code), None)
        if not d or not dest or not d.get("location") or d.get("location_stale"):
            return None
        if star_of(d["location"]) != star_of(dest):
            return f"{code} didn't arrive at {dest}: it's at {d['location']}"
        return None

    async def _off_course(self, st: dict, job: dict) -> str | None:
        """For a step after a trip in this job: the carrier is now heading somewhere else, or has stopped somewhere
        else, than the trip's destination. None = on course (or no trip to compare with)."""
        trips = job.get("trips") or {}
        if not trips:
            return None
        by = {x.get("device_code"): x for x in await self.devices()}
        code = str(st.get("path") or "").split("/")[2] if str(st.get("path") or "").startswith("/devices/") else None
        d = by.get(code or "") or {}
        cands = [c for c in (d.get("stowed_in_device_code"), d.get("attached_to_device_code"), code) if c in trips]
        if not cands:
            cands = [c for c in trips if c in by]
        for c in cands:
            dest, car = star_of(trips[c]), by.get(c) or {}
            tr = car.get("travel") or {}
            heading = star_of(tr.get("final_destination") or tr.get("destination"))
            moving = str(car.get("status") or "").startswith(("travel", "cruis", "surg"))
            if moving and heading and heading != dest:
                return f"{c} is heading to {heading}, not {dest} (trip canceled or redirected)"
            if not moving and car.get("location") and not car.get("location_stale") and star_of(car["location"]) != dest:
                return f"{c} is at {car['location']}, not {dest}"
        return None

    async def _still_travelling(self, st: dict, job: dict, now) -> bool:
        """A wait for an arrival that timed out while the device is evidently still on its way (moving, or left out of
        the device list mid-surge): extend it until the trip's ETA (+30 min); with no ETA known, one more hour."""
        code = st.get("wait_device") or job.get("device")
        d = next((x for x in await self.devices() if x.get("device_code") == code), None)
        first = _ts(st.setdefault("first_started_at", st.get("started_at") or now.isoformat(timespec="seconds"))) or now
        tr = (d or {}).get("travel") or {}
        # the later of the trip's ETA and the current leg's: the game's route can underprice a leg (live 2026-10-28:
        # F7E30727's whole trip was due 22:52, its 527 AU leg to ISOLITHOR-KUIPER 23:45, and the wait gave up at 22:41)
        etas = [t for t in (_ts(tr.get("final_arrives_at")), _ts(tr.get("arrives_at"))) if t]
        eta = max(etas) if etas else _ts((job.get("etas") or {}).get(code or ""))
        limit = (eta + timedelta(seconds=TRIP_GRACE_S)) if eta else (first + timedelta(seconds=st.get("timeout", STEP_TIMEOUT)
                                                                                    + NO_ETA_EXTRA_S))
        if not d or now > limit:
            return False
        st_ = str(d.get("status") or "")
        moving = st_.startswith(("travel", "cruis", "surg")) or (d.get("unlisted") and not d.get("location"))
        if not moving:
            return False
        st["started_at"] = now.isoformat(timespec="seconds")
        if not st.get("extended"):
            await self.log(job["rule"], f"{job['title']}: {code} is still on its way — waiting longer for '{st['desc']}'")
        st["extended"] = int(st.get("extended") or 0) + 1
        await self._update(job)
        return True

    async def cancel(self, job_id: str) -> None:
        async with self.lock:
            job = await self._get(job_id)
            if job and job["status"] in ("running", "waiting"):
                job["status"] = "cancelled"
                job["finished_at"] = now_iso()
                await self._update(job)
                await self.log(job["rule"], f"canceled: {job['title']}")
