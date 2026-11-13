"""Server-side automations: rules the player switches on, and multi-step jobs they launch.

A *rule* watches events (and a periodic tick) and, when its condition holds, creates a *job*:
an ordered list of game commands. Each step may wait for an event (e.g. travel.arrived) before
the next one runs; a step that errors or times out is logged and skipped so one bad target
doesn't strand the drone. Jobs are persisted, so they survive restarts.

Dry run: every rule plans its job as usual but no command is sent; the plan is logged instead.
"""
from __future__ import annotations

from .engine_base import *  # noqa: F401,F403
from .ops_rules import OpsRules
from .boot_engine import BootstrapMixin
from .sim_engine import SimMixin
from .stock import StockMixin
from .chase_engine import ChaseMixin
from .chatter import ChatterMixin
from .tradepost import TradePostMixin
from .engine_jobs import JobsMixin
from .engine_loadouts import LoadoutMixin
from .engine_fleets import FleetsMixin
from .engine_rules import MiningRulesMixin


class AutomationEngine(JobsMixin, LoadoutMixin, FleetsMixin, MiningRulesMixin, OpsRules, BootstrapMixin, SimMixin, StockMixin, ChaseMixin, ChatterMixin, TradePostMixin):
    def __init__(self, db, api, hub, worker):
        self.db, self.api, self.hub, self.worker = db, api, hub, worker
        self.lock = EngineLock()
        self.task: asyncio.Task | None = None
        self.stage: str | None = None          # what the current tick is doing (for the watchdog / snapshot)
        self._lock_alerted = False
        self._no_attach_loaded = False   # fleets.NO_ATTACH_TYPES read from kv (devices())
        self.stage_errors: dict[str, str] = {}  # stage -> its last error (announced once)
        self.tick_n = 0   # ticks run since start (SPREAD)

    # --- settings & persistence ---------------------------------------------------------
    async def settings(self) -> dict:
        s = await self.db.kv_get("automation_settings", None) or {}
        s.setdefault("dry_run", False)
        rules = s.setdefault("rules", {})
        for r in RULES:
            cfg = rules.setdefault(r.id, {})
            cfg.setdefault("enabled", r.default_on)
            for o in r.options:
                cfg.setdefault(o.name, o.default)
        return s

    async def save_settings(self, s: dict) -> None:
        await self.db.kv_set("automation_settings", s)

    async def rule_cfg(self, rule_id: str) -> dict | None:
        s = await self.settings()
        cfg = s["rules"].get(rule_id) or {}
        return cfg if cfg.get("enabled") else None

    async def jobs(self) -> list[dict]:
        return await self.db.kv_get("automation_jobs", []) or []

    async def save_jobs(self, jobs: list[dict]) -> None:
        # keep active jobs + the 50 most recent finished ones
        active = [j for j in jobs if j["status"] in ("running", "waiting")]
        done = [j for j in jobs if j["status"] not in ("running", "waiting")][-50:]
        await self.db.kv_set("automation_jobs", done + active)

    async def notify_link(self, rule: str, text: str, job: dict | None = None) -> str:
        """The page where you act on a notification (the outliner's Needs you rows and the Notifications page link there)."""
        if job:
            return "/fleets/logistics" if job.get("rule") == "logistics" else f"/automations#job-{job.get('id')}"
        if rule == "fleets":
            if text.startswith("away from their fleet"):
                return "/fleets"
            for f in await self.fleets():
                if f.get("name") and text.startswith(f"{f['name']}: "):
                    return f"/fleets/details?fleet={f.get('id')}"
            return "/fleets"
        if rule == "wards":
            return "/map/relays" if "relay rollout" in text else "/wards"
        if rule == "season":
            return "/fleets" if "fleet homes" in text else "/"
        return {"evacuate": "/defence", "station_jobs": "/jobs", "simulation": "/simulations", "trail": "/trail",
                "repeats": "/console", "logistics": "/fleets/logistics", "belt_viability": "/systems"}.get(rule, "/automations")

    async def log(self, rule: str, text: str, level: str = "info", notify: bool = False, job: dict | None = None) -> None:
        entries = await self.db.kv_get("automation_log", []) or []
        from .version import RUN_ID, VERSION
        entries.append({"at": now_iso(), "rule": rule, "level": level, "text": text, "v": VERSION, "run": RUN_ID})
        await self.db.kv_set("automation_log", entries[-MAX_LOG:])
        log.info("[%s] %s", rule, text)
        if notify:
            link = await self.notify_link(rule, text, job)
            title = f"Automation: {text}"
            nlevel = severity({"level": level, "text": text})   # error / warning / info, like the log
            cur = await self.db.execute(
                "INSERT INTO notifications(event_id, level, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                (None, nlevel, title, None, link, now_iso()))
            self.hub.publish("notify", {"id": cur.lastrowid, "level": nlevel, "title": title, "link": link})

    # --- lifecycle ------------------------------------------------------------------------
    def start(self) -> None:
        self.task = asyncio.create_task(self._tick_loop(), name="automations")
        self.lag_task = asyncio.create_task(perf.watch_loop(lambda: self.stage), name="loop-lag")
        perf.start_sampler(lambda: self.stage)

    async def stop(self) -> None:
        for t in (self.task, getattr(self, "lag_task", None)):
            if t:
                t.cancel()
                await asyncio.gather(t, return_exceptions=True)

    focus_until = 0.0   # time.monotonic(): until then nothing heavy runs (a swoop run is being flown, web_cockpit.py)

    def focus(self, seconds: float) -> None:
        """Keep the event loop free for a live run: no new tick, and a running tick stops at its next stage. Live
        2026-10-29: ticks of 18-47 s and loop stalls up to 18 s made round 4 of two swoop runs arrive too late."""
        self.focus_until = time.monotonic() + seconds if seconds > 0 else 0.0

    def focused(self) -> bool:
        return time.monotonic() < self.focus_until

    tick_t0: float | None = None   # time.monotonic() of the running tick's start; None between ticks
    tick_t1 = 0.0                  # when the last tick ended
    tick_last_s = 0.0              # how long it took

    def engine_state(self) -> dict:
        """For the cockpit: busy (stage, running for, about how long left), held (for how long), or idle (next pass in)."""
        now = time.monotonic()
        held = max(0.0, self.focus_until - now)
        out = {"held_s": round(held), "stage": self.stage}
        if self.tick_t0 is not None:
            ran = now - self.tick_t0
            out |= {"state": "busy", "ran_s": round(ran),
                    "left_s": round(max(1.0, self.tick_last_s - ran)) if self.tick_last_s else None}
        elif held:
            out["state"] = "held"
        else:
            out |= {"state": "idle", "next_s": round(max(0.0, self.tick_t1 + 60 - now)) if self.tick_t1 else None}
        return out

    async def _tick_loop(self) -> None:
        await asyncio.sleep(15)
        while True:
            try:
                if self.focused():
                    await asyncio.sleep(2)
                    continue
                await self.watchdog()
                if self.lock.locked() and self.lock.held_for() > LOCK_ALERT_SECONDS:
                    await asyncio.sleep(60)   # still stuck: don't queue up another tick behind it
                    continue
                t_tick, c_tick = time.perf_counter(), time.thread_time()
                self.tick_t0 = time.monotonic()
                try:
                    await self.tick()
                finally:
                    perf.note_stage("(whole tick)", (time.perf_counter() - t_tick) * 1000, (time.thread_time() - c_tick) * 1000)
                    if not self.focused():   # a tick cut short by a hold says nothing about how long a whole one takes
                        self.tick_last_s = time.monotonic() - self.tick_t0
                    self.tick_t0, self.tick_t1 = None, time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("automation tick failed")
                await self.note_tick(error=f"{type(e).__name__}: {e} (during {self.stage or '?'})")
            await asyncio.sleep(60)

    async def watchdog(self) -> None:
        """Alert (once per stall) when the engine lock has been held for over LOCK_ALERT_SECONDS."""
        held = self.lock.held_for()
        if held > LOCK_ALERT_SECONDS:
            if not self._lock_alerted:
                self._lock_alerted = True
                await self.log("engine", f"automations stalled: the engine has been busy for {held / 60:.0f} min "
                                         f"(held by {self.lock.holder or '?'}, tick stage: {self.stage or '-'}) — "
                                         "nothing runs until it's free; restart the app if this persists", "error", notify=True)
        elif self._lock_alerted and not self.lock.locked():
            self._lock_alerted = False
            await self.log("engine", "automations running again", notify=True)

    async def note_tick(self, started: str | None = None, error: str | None = None) -> None:
        """Heartbeat for the snapshot: when the last tick started / finished, and the last tick error."""
        hb = await self.db.kv_get("engine_tick", {}) or {}
        if started:
            hb["started_at"] = started
        elif error:
            hb["error"], hb["error_at"] = error, now_iso()
        else:
            hb["finished_at"] = now_iso()
        await self.db.kv_set("engine_tick", hb)

    def engine_status(self) -> dict:
        return {"lock_held": self.lock.locked(), "held_by": self.lock.holder, "held_seconds": round(self.lock.held_for()),
                "stage": self.stage}

    # --- sending commands -------------------------------------------------------------------
    async def vessel_print(self, method: str, path: str, body: Any) -> bool:
        """A print order aimed at a vessel: a replicant's print, or enqueue_print on a device whose type is a vessel."""
        if method != "POST":
            return False
        p = (path or "").rstrip("/")
        if VESSEL_PRINT_RE.match(p):
            return True
        if not isinstance(body, dict) or body.get("command") != "enqueue_print" or not p.startswith("/devices/"):
            return False
        code = p.split("/")[-1].upper()
        d = next((x for x in await self.devices() if (x.get("device_code") or "").upper() == code), None)
        return d is not None and "vessel" in (d.get("device_type") or "")

    async def send(self, method: str, path: str, body: Any, label: str,
                   components_done: bool = False, raw: bool = False, hand: bool = False) -> tuple[bool, Any, str | None]:
        """`components_done`: an enqueue_print that mustn't queue its components first (a re-queue: they're queued).
        `raw`: straight to the game's queue, past the virtual print queue (vqueue.py). `hand`: a job you started
        yourself; only those may print on a vessel (owner 2026-11-07: automations never print on vessels)."""
        from .fleets import RENAMES_KV, with_renamed_tags
        if not (hand or HAND_JOB.get()) and await self.vessel_print(method, path, body):
            err = "automations don't print on vessels (print on it by hand when you want to)"
            await self.log("engine", f"refused: {label} — {err}", "warning")
            return False, None, err
        body = with_renamed_tags(method, path, body, await self.db.kv_get(RENAMES_KV, {}) or {})
        status, resp, err = 200, None, None
        try:
            kw = {**({"components_done": True} if components_done else {}), **({"raw": True} if raw else {})}
            resp = await self.api.request(method, path, json_body=body, background=True, **kw)
            if self.worker:
                await self.worker.timers_from_response(path, resp, label)
        except ApiError as e:
            status, err, resp = e.status, e.message, e.body
        await self.db.execute(
            "INSERT INTO actions(at, user, method, path, body, status, response) VALUES(?,?,?,?,?,?,?)",
            (now_iso(), "automation", method, path, json.dumps(body) if body is not None else None, status,
             json.dumps(resp, default=str)[:20000] if resp is not None else None))
        if err is None and method == "PATCH" and isinstance(resp, dict) and isinstance(resp.get("tags"), list):
            await self.remember_tags(path.rstrip("/").split("/")[-1], resp["tags"])
        cmd = (body or {}).get("command") if isinstance(body, dict) else None
        if err is None and cmd in ("launch", "withdraw") and isinstance(resp, dict):
            # docs (AMI): launch answers assigned_devices {already_deployed, deployed, failed, skipped}; withdraw failed
            ad = resp.get("assigned_devices") if isinstance(resp.get("assigned_devices"), dict) else resp
            failed, skipped = ad.get("failed") or [], ad.get("skipped") or []
            if failed or skipped:
                await self.log("ami", f"{label}: {cmd} left out {len(failed)} failed / {len(skipped)} skipped device(s): "
                                      f"{json.dumps(failed or skipped, default=str)[:300]}", "warning")
        return err is None, resp, err

    async def remember_tags(self, code: str, tags: list[str]) -> None:
        """Update the cached device list right away, so the next pass doesn't redo tag changes before the next sync."""
        devices = await self.db.kv_get("devices", []) or []
        for d in devices:
            if d.get("device_code") == code:
                d["tags"] = list(tags)
                await self.db.kv_set("devices", devices)
                return

    # --- event & tick handling ------------------------------------------------------------------
    async def on_event(self, ev: dict, late: bool = False) -> None:
        """`late`: a replayed event (see ingest.LATE_EVENT). It still updates what we know and wakes waiting jobs,
        but doesn't fire reactive rules: they'd act on a world that has moved on since."""
        async with self.lock:
            name = ev.get("event") or ""
            p = ev.get("payload") or {}
            # remember what has been surveyed
            if name in ("scan.completed", "search.completed") and not ev.get("virtual"):
                target = (p.get("scan_target") or p.get("search_target") or p.get("target") or p.get("body")
                          or p.get("designation") or ev.get("location"))
                if target:
                    surveyed = await self.db.kv_get("surveyed", {}) or {}
                    surveyed[target] = now_iso()
                    await self.db.kv_set("surveyed", surveyed)
            # an AMI survey controller finished survey_system: the whole system counts as surveyed
            # (not any directive: a belt_search finishing on the same controller says nothing about the planets)
            if (name == "directive.completed" and "survey" in (ev.get("device_type") or "") and not ev.get("virtual")
                    and (p.get("directive") or "survey_system") == "survey_system"):
                await self.mark_system_surveyed(star_of(ev.get("location")) or ev.get("star") or "", ev.get("device_code"))
            # tags changed while it was printing (Print queue panel): put them on the new device now
            if name == "print.completed" and p.get("new_device_code"):
                from . import printqueue as pqm
                edits = await self.db.kv_get(pqm.EDITS_KV, {}) or {}
                lst = edits.get(ev.get("device_code") or "") or []
                was = [str(t) for t in p.get("tags") or []]
                e = pqm.edit_for(lst, p.get("device_type"), was)
                if e:
                    lst.remove(e)
                    edits[ev["device_code"]] = lst
                    if not lst:
                        edits.pop(ev["device_code"], None)
                    await self.db.kv_set(pqm.EDITS_KV, edits)
                    add = [t for t in e["to"] if t not in was]
                    rem = [t for t in was if t not in e["to"]]
                    if add or rem:
                        await self.send("PATCH", f"/devices/{p['new_device_code']}",
                                        {"configuration": {**({"add_tags": add} if add else {}),
                                                           **({"remove_tags": rem} if rem else {})}},
                                        f"auto: {p['new_device_code']} gets the tags set while it printed")
                    p = {**p, "tags": list(e["to"])}   # what follows (orders, dispatch) sees the new tags
            # a print ordered for a system came out: remember its code until the device list shows it
            if name == "print.completed":
                from .loadouts import at_tag, to_tag
                orders = await self.db.kv_get("loadout_orders", []) or []
                tags = [str(t) for t in p.get("tags") or []]
                hit = next((o for o in orders if not o.get("device_code") and o["device_type"] == p.get("device_type")
                            and (to_tag(o["star"]) in tags or o.get("factory") == ev.get("device_code"))), None)
                if hit:
                    new = p.get("new_device_code")
                    hit["device_code"], hit["printed_at"] = new or "?", now_iso()
                    await self.db.kv_set("loadout_orders", orders)
                    if new and hit.get("location") and at_tag(hit["location"]) not in tags:
                        await self.send("PATCH", f"/devices/{new}", {"configuration": {"add_tags": [at_tag(hit["location"])]}},
                                        f"auto: pin {new} at {hit['location']}")
                    if new and to_tag(hit["star"]) not in tags:
                        # the game didn't carry the print's tags over: tag it ourselves so it is routed, not re-printed
                        await self.send("PATCH", f"/devices/{new}", {"configuration": {"add_tags": [to_tag(hit["star"])]}},
                                        f"auto: tag new {p.get('device_type')} {new} for {hit['star']}")
            # a print that's bound for another system: dispatch it as soon as it's out, not at the next loadout pass
            if name == "print.completed":
                new = p.get("new_device_code")
                tags = [str(t) for t in p.get("tags") or []]
                hit_order = any(o.get("device_code") == new for o in await self.db.kv_get("loadout_orders", []) or [])
                if new and (hit_order or any(t.startswith("to:") for t in tags)):
                    pend = await self.db.kv_get("dispatch_pending", {}) or {}
                    pend[new] = now_iso()
                    await self.db.kv_set("dispatch_pending", pend)
            # wake waiting jobs
            for job in await self.jobs():
                if job["status"] != "waiting":
                    continue
                st = job["steps"][job["idx"]]
                wait_dev = st.get("wait_device") or job.get("device")
                if st["status"] == "waiting" and event_matches(st, ev, wait_dev):
                    st["status"] = "done"
                    job["idx"] += 1
                    job["status"] = "running"
                    await self._update(job)
                    await self._advance(job["id"])
                elif (st["status"] == "waiting" and name == "travel.cancelled" and "travel.arrived" in st.get("wait", [])
                      and wait_dev and ev.get("device_code") == wait_dev):
                    # a canceled trip never arrives: the device turns back to where it started (the event's
                    # return_time_seconds), so stop waiting for it instead of sitting out the timeout
                    back = int(float(p.get("return_time_seconds") or 0))
                    st["error"] = (f"travel canceled — {wait_dev} returns to {p.get('origin') or 'where it started'}"
                                   + (f" (≈{back // 60} min)" if back else ""))
                    # whatever came next (deploy, unload …) was meant for the destination: stop here, so nothing is
                    # done back where it started (a fleet mission stalls; Retry flies again)
                    st["status"], job["status"] = "failed", "failed"
                    await self._update(job)
                    await self.log(job["rule"], f"stopped: {job['title']} — {st['error']}", stop_level(job), notify=True, job=job)
            if name in ("site.depleted", "salvage.depleted") and not late:
                try:
                    await self.rule_salvage()
                except Exception as e:
                    log.exception("salvage rule failed")
                    await self.log("engine", f"salvage rule failed: {e}", "alert")
            if name in ("event.discovered", "event.completed", "print.completed") and not ev.get("virtual"):
                await self.civ_on_event(ev)
            if name.startswith("simulation."):
                try:
                    await self.sim_on_event(ev)
                except Exception:
                    log.exception("simulation end handling failed")
            if name == "system.object_detected":
                asyncio.create_task(self._locked_sync_objects())
            if name == "system.impact_warning" and not late:
                try:
                    await self.evacuate_pass(True)
                except Exception as e:
                    log.exception("evacuation failed")
                    await self.log("evacuate", f"evacuation failed: {e}", "error", notify=True)
            # rules triggered by arrivals
            if name == "bobnet.new" and not late:
                try:
                    await self.bill_moved(p)
                except Exception as e:
                    log.exception("bill move check failed")
                    await self.log("trail", f"checking Bill's post failed: {e}", "alert")
                try:
                    await self.shop_post(p)
                except Exception as e:
                    log.exception("shop post check failed")
                    await self.log("shop", f"checking a shop's BobNet post failed: {e}", "alert")
            if not late and name in ("bobnet.new", "directive.completed", "scan.completed", "site.depleted",
                                     "travel.arrived", "salvage.discovered", "event.completed"):
                try:   # NPC chatter: a canned line or reply (chatter.py)
                    await self.chatter_on_event(ev)
                except Exception:
                    log.exception("chatter failed")
            if name == "travel.arrived" and not late:
                try:
                    await self.on_arrival(ev)
                except Exception as e:
                    log.exception("arrival rules failed")
                    await self.log("engine", f"arrival handling failed: {e}", "alert")

    async def tick(self) -> None:
        async with self.lock:
            now = _now()
            self.stage = "jobs"
            await self.note_tick(started=now_iso())
            try:
                await self.outage_pass()
            except Exception:
                log.exception("outage schedule failed")
            held = await self.held()
            for job in await self.jobs():
                if held and not job.get("forced") and not (job.get("meta") or {}).get("through_hold"):
                    continue
                if job["status"] == "running":
                    await self._advance(job["id"])  # e.g. a rate-limited retry
                    continue
                if job["status"] != "waiting":
                    continue
                st = job["steps"][job["idx"]]
                if st.get("status") == "confirm":
                    continue   # beyond the sector range: waits for your Confirm
                if st.get("status") == "retrying":
                    # a trip waiting for its surge hop to land reads the device fresh itself (the list may be minutes old)
                    off = None if st.get("from_spot") else await self._off_course(st, job)
                    if off:   # turned back or went elsewhere: the retried step was meant for the destination
                        st["status"], st["error"], job["status"] = "failed", off, "failed"
                        await self._update(job)
                        await self.log(job["rule"], f"stopped: {job['title']} — {off}", stop_level(job), notify=True, job=job)
                        continue
                    if now >= (_ts(st.get("retry_at")) or now):
                        st["status"], job["status"] = "pending", "running"
                        await self._update(job)
                        await self._advance(job["id"])
                    continue
                started = _ts(st.get("started_at")) or now
                if (st.get("method") == "WAIT" or "travel.arrived" in (st.get("wait") or [])) \
                        and (now - started).total_seconds() > 60 and await self._arrived_now(st, job):
                    st["status"], st["note"] = "done", "already there"
                    job["idx"] += 1
                    job["status"] = "running"
                    await self._update(job)
                    await self._advance(job["id"])
                    continue
                if (now - started).total_seconds() > st.get("timeout", STEP_TIMEOUT) \
                        and "travel.arrived" in (st.get("wait") or []) and await self._still_travelling(st, job, now):
                    continue   # still on its way (or out of sight mid-surge): keep waiting
                if (now - started).total_seconds() > st.get("timeout", STEP_TIMEOUT) and "travel.arrived" in (st.get("wait") or []):
                    miss = await self._not_arrived(st, job)
                    if miss:   # not where the job sent it: don't go on to deploy / unload somewhere else
                        st["status"], st["error"], job["status"] = "failed", miss, "failed"
                        await self._update(job)
                        await self.log(job["rule"], f"stopped: {job['title']} — {miss}", stop_level(job), notify=True, job=job)
                        continue
                if (now - started).total_seconds() > st.get("timeout", STEP_TIMEOUT):
                    st["status"] = "skipped"
                    st["error"] = "timed out waiting for " + "/".join(st["wait"])
                    job["idx"] += 1
                    job["status"] = "running"
                    await self._update(job)
                    await self.log(job["rule"], f"{job['title']}: '{st['desc']}' timed out, moving on", "alert")
                    await self._advance(job["id"])
            if held:   # paused for a season change: nothing else runs until you resume
                self.stage = None
                await self.note_tick()
                return
            for stage in ("season_snapshot_pass", "evacuate_pass", "rock_mining_pass", "rock_chase_pass", "run_fleets", "auto_deals_pass", "trade_post_pass", "contract_supply_pass", "dispatch_pass", "component_pass", "fill_fleets", "matrix_pickup_pass", "fleet_owners", "dispatch_new_prints", "rule_contracts", "refresh_known_belts", "track_viability",
                          "rule_consolidate", "rule_reopen_sites", "rule_salvage", "rest_dry_controllers", "rule_restart_idle_miners",
                          "run_due_schedules", "run_due_repeats", "civ_beacon_pass", "maintenance_pass",
                          "decommission_queue_pass", "trail_follow_pass", "trail_pass", "fleet_rename_pass", "place_tag_pass", "rider_pass", "stray_pass", "pathing_pass", "observatory_pass", "auto_scout_pass", "repair_return_pass", "hub_watch_pass", "bootstrap_pass", "pending_home_pass", "ward_orders_pass", "relay_rollout_pass", "dark_relay_pass", "season_rescan_pass", "station_swap_pass", "news_pass", "station_jobs_pass", "sim_pass", "prospector_pass", "stock_pass", "vqueue_pass", "chatter_pass"):
                every, at = SPREAD.get(stage, (1, 0))
                if self.tick_n and self.tick_n % every != at:
                    continue   # a heavy pass that runs every few ticks, on a tick of its own (all run on the first)
                if self.focused():
                    break   # a swoop run started: the rest of this tick waits for the next one
                self.stage = stage
                await asyncio.sleep(0)   # let waiting page requests run between stages
                t_stage, c_stage = time.perf_counter(), time.thread_time()
                try:   # one stage failing on odd data mustn't stop every stage after it, every tick
                    out = await getattr(self, stage)()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception("engine stage %s failed", stage)
                    key = f"{stage}: {type(e).__name__}: {e}"[:300]
                    if self.stage_errors.get(stage) != key:   # say so once per distinct error, not every minute
                        self.stage_errors[stage] = key
                        await self.log("engine", f"stage {stage} failed: {type(e).__name__}: {e}", "alert", notify=True)
                    continue
                finally:
                    perf.note_stage(stage, (time.perf_counter() - t_stage) * 1000, (time.thread_time() - c_stage) * 1000)
                self.stage_errors.pop(stage, None)
                if stage == "civ_beacon_pass":
                    for line in out or []:
                        await self.log("civ_beacons", line)
            self.stage = None
            self.tick_n += 1
            await self.note_tick()

    async def trail_follow_pass(self) -> list[str]:
        """Map › Trail › Follow to the end: the replicant that scanned flies to the system the latest departure points
        at, looks for the target's beacons there and reads them; an arrival with no departure after it ends the
        follow (the target is there), a departure sends it on to the next system (another candidate if this one has
        no record of the target). State in kv trail["follow"]."""
        from . import trail as tl
        s = tl.normalize(await self.db.kv_get(tl.KV, {}) or {})
        fo = s.get("follow") or {}
        if not fo.get("active"):
            return []
        reps = await self.db.kv_get("replicants", {}) or {}
        rep = fo.get("replicant")
        host = (reps.get(rep) or {}).get("hosted_device_code")
        name = (reps.get(rep) or {}).get("name") or rep
        out: list[str] = []

        def note(text: str) -> None:
            fo.setdefault("log", []).append({"at": now_iso(), "text": text})
            fo["log"] = fo["log"][-40:]
            out.append(text)

        async def finish(text: str, level: str = "mention") -> None:
            fo["active"], fo["result"] = False, text
            note(text)
            await self.log("trail", f"follow {s['target_name']}: {text}")
            await self._notify(level, f"Trail: {text}", "/trail")

        if not rep or rep not in reps:
            await finish("stopped: the replicant following the trail is gone", "warning")
        elif fo.get("state") == "travel":
            here = star_of((reps[rep] or {}).get("location") or (reps[rep] or {}).get("current_location"))
            job = next((j for j in await self.jobs() if j["id"] == fo.get("job")), None) if fo.get("job") else None
            if not fo.get("job") and here == fo.get("star"):
                fo["state"] = "arrived"
            elif not fo.get("job"):
                st = step(f"{name} → {fo['star']} (following {s['target_name']})", f"/replicants/{rep}/travel",
                          {"destination": fo["star"]}, wait=["travel.arrived"], timeout=12 * 3600, critical=True)
                st["wait_device"] = host
                j = await self.create_job("trail", f"follow {s['target_name']}: {name} → {fo['star']}", host, [st],
                                          {"devices": [host] if host else []}, force=True)
                if j:
                    fo["job"] = j["id"]
                    note(f"{name} sets off for {fo['star']}")
            elif job is None or job["status"] == "done":
                fo["state"], fo["job"] = "arrived", None
                note(f"{name} reached {fo['star']}")
            elif job["status"] in ("failed", "cancelled"):
                err = next((x.get("error") for x in job["steps"] if x.get("error")), job["status"])
                await finish(f"stopped: {name} couldn't travel to {fo['star']} ({err})", "warning")
        if fo.get("active") and fo.get("state") == "arrived":
            star = fo["star"]
            try:
                # everything of his in the system: any device is a sighting, his trade controller ends the trail
                from . import others as oth
                snap = await oth.scan_into(self.db, self.api, rep, set(reps))
                s = {**s, **{k: v for k, v in tl.normalize(await self.db.kv_get(tl.KV, {}) or {}).items() if k == "sightings"}}
                tl.record_sightings(s, snap.get("raw") or [], star=snap.get("star") or star)
                for b in tl.beacons_in({"devices": snap.get("raw") or []}, s.get("target_code"), s.get("target_name")):
                    s["beacons"].setdefault(b["code"], {**b, "found_at": now_iso()})
                for code, b in list(s["beacons"].items()):
                    if b.get("star") == star or not b.get("star"):
                        await tl.read_beacons(self.api, s, code)
            except ApiError as e:
                fo["tries"] = int(fo.get("tries") or 0) + 1
                note(f"reading {star} failed: {e.message}")
                if fo["tries"] >= 3:
                    await finish(f"stopped: couldn't read the beacons at {star} ({e.message})", "warning")
            else:
                fo["tries"] = 0
                fo.setdefault("visited", []).append(star)
                cat = await self.db.kv_get("stars", {}) or {}
                pos = {x.get("designation"): x.get("position") for x in cat.get("stars") or [] if isinstance(x, dict)}
                h = tl.next_hop(s, star, pos, fo["visited"])
                shop = tl.shop_at(s, star)
                seen = tl.latest_sighting(s)
                newest_here = (tl.at_star(s, star) or [{}])[0].get("logged_at") or ""
                if shop:   # his trade controller is here: that's the shop, the end of the trail
                    await self.shop_found(s, shop)
                    await finish(f"found {s['target_name']}'s shop: trade controller {shop['code']} at {shop['location']} — "
                                 f"{name} is there too")
                    h = {}
                elif seen and seen.get("star") and seen["star"] != star and seen["star"] not in fo["visited"] \
                        and seen["at"] > str(newest_here) and "end" not in h \
                        and not (seen.get("travel_type") == "departure" and tl.resets_from(s, seen["star"])):   # → SOL
                    # a newer sighting elsewhere beats where the beacons here point
                    h = {"go": seen["star"], "alts": [], "why": f"{seen['via']} was seen there {seen['at']}"}
                if not fo.get("active"):
                    pass
                elif "end" in h:
                    await finish(f"found {s['target_name']}: {h['end']} — {name} is there too")
                elif "go" in h and len(fo["visited"]) < tl.MAX_HOPS:
                    fo.update(state="travel", star=h["go"], alts=h["alts"], job=None)
                    note(f"at {star}: {h['why']} — on to {h['go']}")
                elif "go" in h:
                    await finish(f"stopped after {tl.MAX_HOPS} systems at {star}: the trail goes on to {h['go']}", "warning")
                elif fo.get("alts"):
                    nxt, fo["alts"] = fo["alts"][0], fo["alts"][1:]
                    fo.update(state="travel", star=nxt, job=None)
                    note(f"at {star}: {h['cold']} — trying the next candidate, {nxt}")
                elif tl.is_bill(s) and not fo.get("sol_tried") and star != tl.BILL_HOME:
                    # Bill always passes through SOL: a cold trail (his location may have reset) is picked up there
                    fo.update(state="travel", star=tl.BILL_HOME, job=None, sol_tried=True)
                    note(f"at {star}: {h['cold']} — {s['target_name']} always passes through {tl.BILL_HOME}: picking "
                         "the trail up there")
                else:
                    await finish(f"the trail goes cold at {star}: {h['cold']}", "warning")
        s["follow"] = fo
        await self.db.kv_set(tl.KV, s)
        self.hub.publish("state", "trail")
        return out

    async def shop_found(self, s: dict, shop: dict) -> None:
        """The followed replicant's trade controller was seen: its shop is listed there (and no longer set aside)."""
        cache = await self.db.kv_get("traders_cache", {}) or {}
        e = dict(cache.get(shop["code"]) or {})
        e.update({"controller_code": shop["code"], "location": shop["location"], "star": shop["star"],
                  "owner_name": e.get("owner_name") or s.get("target_name"),
                  "owner_replicant_code": e.get("owner_replicant_code") or s.get("target_code"), "read_at": now_iso()})
        e.setdefault("trades", [])
        cache[shop["code"]] = e
        if shop.get("star"):   # remembered: leaving it, he resets to SOL (trail.resets_from)
            s["shop_stars"] = sorted(set(s.get("shop_stars") or []) | {shop["star"]})
        await self.db.kv_set("traders_cache", cache)
        from . import shop as sh
        wanted = await self.db.kv_get(sh.LISTING_KV, {}) or {}
        if shop["code"] in wanted:   # you asked for his listing: the follower is in the system now
            try:
                trades = await sh.read_listing(self.db, self.api, shop["code"])
            except ApiError as e:
                log.info("reading %s's trades failed: %s", shop["code"], e.message)
            else:
                wanted.pop(shop["code"])
                await self.db.kv_set(sh.LISTING_KV, wanted)
                await self.log("shop", f"listing of {shop['code']} at {shop['location']}: {len(trades)} trades read")

    async def shop_post(self, payload: dict) -> str | None:
        """A shop owner's BobNet post that the shop is open ("Rock Droppers is open for shoppers! … Find me in the
        Oort."): move that shop to the system and spot it names (owner, 2026-10-20: Dash announces each move)."""
        from . import shop as sh
        post = sh.open_post(payload, await self.star_lookup("names"))
        if not post:
            return None
        await sh.shop_opened(self.db, post, now_iso())
        text = f"{post['shop_name']} ({post['owner_name']}) is open at {post['location']} (BobNet)"
        await self.log("shop", text)
        return text

    async def bill_moved(self, payload: dict) -> str | None:
        """Bill posted that his Skunkworks moved: set his shop aside where it was, clear the beacon logs, and when you
        follow him, pick the trail up at SOL (leaving his shop he can't be followed: he resets to SOL)."""
        from . import trail as tl
        from .shop import is_bills, known_traders, mark_gone
        stars = await self.star_lookup("names")
        mv = tl.bill_move_post(payload, stars)
        if not mv:
            return None
        s = tl.normalize(await self.db.kv_get(tl.KV, {}) or {})
        last = tl.latest_sighting(s) if tl.is_bill(s) else None
        frm = mv["from"] or (last or {}).get("star")
        now = now_iso()
        try:   # where the shop is now, if the directory already lists it there
            from .shop import refresh_directory
            await refresh_directory(self.db, self.api, list(await self.db.kv_get("replicants", {}) or {}))
        except Exception:
            log.exception("trader directory refresh after Bill's post failed")
        closed = now_iso()   # after the refresh: his shop is closed until it's seen again (owner, 2026-10-20)
        for code, t in (await known_traders(self.db)).items():   # his shop isn't where we last saw it any more
            if is_bills(t) and (tl.star_of(t.get("location")) == frm if frm else t["seen_at"] < now):
                await mark_gone(self.db, code, t.get("location"), closed)
        from .shop import LISTING_KV
        wanted = await self.db.kv_get(LISTING_KV, {}) or {}
        known = await known_traders(self.db)
        for code, rq in wanted.items():   # a listing of his old shop: logistics goes once his shop is seen again
            if code not in known and rq.get("job"):
                await self.cancel(rq.pop("job"))
        if wanted:
            await self.db.kv_set(LISTING_KV, wanted)
        text = "Bill is on the move" + (f" — left {frm}" if frm else "") + f": “{mv['text'][:160]}”"
        if tl.is_bill(s):
            fo = s.get("follow") or {}
            if fo.get("job"):
                await self.cancel(fo["job"])
            s["moved"] = {"at": now, "from": frm, "text": mv["text"]}
            # the old route is history, and leaving his shop he can't be followed anyway — he resets to SOL (owner,
            # 2026-10-13): drop the beacon logs; the beacons stay known and are read again for the new route
            s["audit"] = {b: [] for b in s["audit"]}
            s["trail_reset_at"] = now
            if fo.get("active"):   # following him is started by hand on the Trail page only (owner, 2026-10-22)
                fo.update(active=False, job=None, result="stopped: Bill moved his shop")
                fo.setdefault("log", []).append({"at": now, "text": f"{text} — stopped (follow him again from Trail)"})
                s["follow"] = fo
                text += " — stopped following him (start it again on the Trail page)"
            s.pop("polled_at", None)   # re-read his beacons on the next pass
            await self.db.kv_set(tl.KV, s)
            self.hub.publish("state", "trail")
        await self.log("trail", text, notify=True)
        await self._notify("mention", f"Trail: {text}", "/trail")
        return text

    async def trail_pass(self) -> list[str]:
        """Map › Trail: every 15 minutes re-read the followed replicant's beacons (trail.py); each new departure or
        arrival of theirs raises a notification (level 'mention': the bell's badge counts it)."""
        from . import trail as tl
        s = tl.normalize(await self.db.kv_get(tl.KV, {}) or {})
        last = _ts(s.get("polled_at"))
        if not s["beacons"] or (last and (_now() - last).total_seconds() < 900):
            return []
        s["polled_at"] = now_iso()
        new, _errs = await tl.read_beacons(self.api, s)   # a beacon's first read returns nothing new
        await self.db.kv_set(tl.KV, tl.merge_read(await self.db.kv_get(tl.KV, {}) or {}, s))
        self.hub.publish("state", "trail")
        out = []
        for r in new:
            if s.get("target_code") and r.get("replicant_code") != s["target_code"]:
                continue
            where = r.get("location") or "?"
            text = (f"{s['target_name']}: {r.get('travel_type')} at {where}"
                    + (f" (heading {r.get('vector')})" if r.get("travel_type") == "departure" and r.get("vector") else ""))
            cur = await self.db.execute(
                "INSERT INTO notifications(event_id, level, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                (None, "mention", text, None, "/trail", now_iso()))
            self.hub.publish("notify", {"id": cur.lastrowid, "level": "mention", "title": text, "link": "/trail"})
            out.append(text)
        return out

    async def stray_pass(self) -> list[str]:
        """Members of stationed fleets away from home (fleets.strays), owner 2026-10-21. One riding another fleet's idle
        carrier (not a logistics one: the logistics pass brings those home) is let off where it is, so the loadout pass
        can bring it home. One stuck away for an hour with nothing bringing it is reported once. A fleet's move to a new
        home stays open (home_moves) until every member is there: reported when it's complete, and once if members are
        still missing after two hours."""
        from . import fleets as fl
        items = await self.fleets()
        devices = await self.devices()
        busy = self.busy_devices(await self.jobs())
        prev = await self.db.kv_get("fleet_strays", {}) or {}
        cur = fl.strays(items, devices, busy)
        # the loadout pass plans to send these home and waits for a carrier: not "nothing is bringing it home"
        lines = (await self.db.kv_get("loadouts_last", {}) or {}).get("lines") or []
        queued = {ln.split(" ", 1)[0] for ln in lines if "is away from its fleet's home" in ln}
        for s in cur:
            s["queued"] = s["code"] in queued
        now = _now()
        logistics = {fl.fleet_tag(f["id"])[6:] for f in items if f.get("role") == "logistics"}
        commandable = await self.commandable(devices)
        out, alerts, state = [], [], {}
        for s in cur:
            p = prev.get(s["code"]) or {}
            e = {**s, "since": p.get("since") or now_iso(), "alerted": p.get("alerted"), "let_off": p.get("let_off", 0)}
            state[s["code"]] = e
            car = s["riding"]
            if car and not s["busy"] and car not in busy and s["riding_fleet"] != fl.fleet_tag(s["fleet"])[6:] \
                    and s["riding_fleet"] not in logistics and s["star"] in commandable and e["let_off"] < 2:
                d = next((x for x in devices if x.get("device_code") == s["code"]), {})
                mode = "attach" if d.get("attached_to_device_code") == car else "stow"
                job = await self.create_job("fleets", f"{s['name']}: {car} lets off {s['code']} (its fleet is in {s['home']})",
                                            car, fl.let_off_steps(car, [{"code": s["code"], "mode": mode}]),
                                            {"devices": [car, s["code"]], "fleet": s["fleet"]})
                if job:
                    e["let_off"] += 1
                    busy.add(car)
                    out.append(f"{car} lets off {s['code']}")
            age = (now - (_ts(e["since"]) or now)).total_seconds() / 60
            if age >= fl.STRAY_ALERT_MINUTES and not e["alerted"] and not s["moving"] and not s["busy"]:
                e["alerted"] = now_iso()
                alerts.append(f"{s['code']} ({(s['type'] or 'device').replace('_', ' ')}, {s['name']}) in {s['star']}, "
                              f"home {s['home']}: {fl.stray_reason(s)}")
        if alerts:
            await self.log("fleets", "away from their fleet for an hour or more: " + "; ".join(alerts), "alert", notify=True)
        back = [c for c, p in prev.items() if c not in state and p.get("alerted")]
        if back:
            await self.log("fleets", f"back with their fleet: {', '.join(sorted(back))}")
        await self.db.kv_set("fleet_strays", state)
        moves = await self.db.kv_get("home_moves", {}) or {}
        if moves:
            names = {f["id"]: f for f in items}
            for fid, m in list(moves.items()):
                f = names.get(fid)
                if not f or f.get("home") != m["to"]:
                    moves.pop(fid)   # the fleet is gone or moved on (that move made its own entry)
                    continue
                left = [s for s in cur if s["fleet"] == fid]
                hours = (now - (_ts(m["at"]) or now)).total_seconds() / 3600
                if not left:
                    await self.log("fleets", f"{f.get('name')}: move to {m['to']} complete, every member is there")
                    moves.pop(fid)
                elif hours >= 2 and not m.get("alerted"):
                    m["alerted"] = now_iso()
                    await self.log("fleets", f"{f.get('name')}: move to {m['to']} still missing "
                                   + "; ".join(f"{s['code']} in {s['star']} ({fl.stray_reason(s)})" for s in left),
                                   "alert", notify=True)
                elif hours >= 48:
                    moves.pop(fid)   # two days: the stray list and its alerts carry on without it
            await self.db.kv_set("home_moves", moves)
        return out

    async def rider_pass(self) -> list[str]:
        """A stationed fleet moved home: the vessels in it that host a replicant fly there too (the loadout pass never
        moves a replicant's vessel, so without this it stayed behind). Only after a home change — a replicant you send
        elsewhere afterwards isn't dragged back. Devices stowed in the vessel travel with it."""
        from . import fleets as fl
        pending = await self.db.kv_get("rider_moves", {}) or {}
        if not pending:
            return []
        items = {f["id"]: f for f in await self.fleets()}
        devices = await self.devices()
        reps = await self.db.kv_get("replicants", {}) or {}
        host_rep = {r.get("hosted_device_code"): c for c, r in reps.items() if r.get("hosted_device_code")}
        busy = self.busy_devices(await self.jobs())
        out = []
        for fid, p in list(pending.items()):
            f = items.get(fid)
            if not f or f.get("home") != p["home"]:
                pending.pop(fid)   # the fleet is gone, or has moved on again (that move made its own entry)
                continue
            riders = [d for d in fl.members(f, devices) if d.get("device_code") in host_rep]
            left = []
            for d in riders:
                code = d["device_code"]
                if star_of(d.get("location")) == p["home"]:
                    continue
                if code in busy or str(d.get("status") or "").startswith(("travel", "cruis", "surg")) \
                        or p["sent"].count(code) >= 3:   # on its way (or tried three times: leave it be)
                    left.append(code) if p["sent"].count(code) < 3 else None
                    continue
                rep = host_rep[code]
                st = step(f"{reps[rep].get('name') or rep} follows {f['name']} to {p['home']}", f"/replicants/{rep}/travel",
                          {"destination": p["home"]}, wait=["travel.arrived"], timeout=12 * 3600, critical=True)
                st["wait_device"] = code
                job = await self.create_job("fleets", f"{f['name']}: {reps[rep].get('name') or rep}'s vessel {code} follows "
                                            f"the fleet to {p['home']}", code, [st], {"devices": [code], "fleet": fid})
                if job:
                    p["sent"].append(code)
                    out.append(f"{code} → {p['home']}")
                left.append(code)
            if not left or (_now() - (_ts(p.get("at")) or _now())).total_seconds() > 48 * 3600:
                pending.pop(fid)   # everyone's there (or it's been two days: give up quietly)
        await self.db.kv_set("rider_moves", pending)
        return out

    async def place_tag_pass(self) -> list[str]:
        """`to:<location>` tags typed by hand (e.g. to:sol-3) become to:<system> + at:<location> (loadouts.place_tag_fix),
        so the loadout pass delivers them to the system and then to the spot."""
        from . import loadouts as lo
        devices = await self.devices()
        if not any(t.startswith("to:") for d in devices for t in d.get("tags") or []):
            return []
        slugs = await self.star_lookup("slugs")
        extra = lo.star_slugs({star_of(d.get("location")) for d in devices if d.get("location")})
        if set(extra) - set(slugs):
            slugs = {**slugs, **extra}
        fleet_ids = {f.get("id") for f in await self.fleets() if f.get("id")}
        busy = self.busy_devices(await self.jobs())
        out = []
        for d in devices:
            fix = lo.place_tag_fix(d, slugs, fleet_ids)
            if not fix or d["device_code"] in busy:
                continue
            add, rem = fix
            job = await self.create_job("tags", f"{d['device_code']}: {rem[0]} → {' + '.join(add) or 'its pin'}", d["device_code"],
                                        [lo.tag_step(d["device_code"], add or None, rem)], {"devices": [d["device_code"]]})
            if job:
                out.append(f"{d['device_code']}: {rem[0]} read as {' + '.join(add)}")
        return out

    async def fleet_rename_pass(self) -> list[str]:
        """A renamed fleet's devices still carrying the old fleet: tag in the game (marked `_retag` by
        fleets.apply_renames): one PATCH each, swapping the old tag for the new; tried again every 10 minutes."""
        devices = await self.devices()
        todo = [d for d in devices if d.get("_retag")]
        if not todo:
            return []
        last = _ts(await self.db.kv_get("retag_at", None))
        if last and (_now() - last).total_seconds() < 600:
            return []
        busy = self.busy_devices(await self.jobs())
        steps = []
        for d in todo:
            if d["device_code"] in busy:
                continue
            new = next((t for t in d.get("tags") or [] if t.startswith("fleet:")), None)
            if not new or new == d["_retag"]:
                continue
            steps.append(step(f"{d['device_code']}: {d['_retag']} → {new}", f"/devices/{d['device_code']}",
                              {"configuration": {"add_tags": [new], "remove_tags": [d["_retag"]]}}, method="PATCH"))
        if not steps:
            return []
        await self.db.kv_set("retag_at", now_iso())
        await self.create_job("fleets", f"renamed fleet: retag {len(steps)} device(s)", None, steps, {"devices": []}, force=True)
        return [f"retag {len(steps)}"]

    async def news_pass(self, force: bool = False) -> list[str]:
        """The game's changelog and blog, every 6 hours (news.py): new versions and posts go to the bell."""
        from . import news
        s = await self.db.kv_get(news.KV, {}) or {}
        last = _ts(s.get("tried_at"))
        if not force and last and (_now() - last).total_seconds() < news.EVERY:
            return []
        if not force and "replicant.space" not in str(getattr(self.api.http, "base_url", "")):
            return []   # a dev / test setup against the mock server: don't reach out to the real site
        s["tried_at"] = now_iso()
        try:
            changelog = news.parse_changelog(await news.fetch("/changelog/"))
            blog = news.parse_blog(await news.fetch("/blog/"))
        except Exception as e:
            s["error"] = f"{type(e).__name__}: {e}"[:300]
            await self.db.kv_set(news.KV, s)
            return []
        if not changelog and not blog:
            s["error"] = "the changelog and blog pages had no entries the parser recognizes (layout changed?)"
            await self.db.kv_set(news.KV, s)
            return []
        new_c, new_b = news.new_items(s, changelog, blog)
        s.update({"checked_at": now_iso(), "error": None, "changelog": changelog[:news.KEEP] or s.get("changelog") or [],
                  "blog": blog[:news.KEEP] or s.get("blog") or [],
                  "known_versions": sorted(set(s.get("known_versions") or []) | {c["version"] for c in changelog}),
                  "known_posts": sorted(set(s.get("known_posts") or []) | {p["id"] for p in blog})})
        s["new"] = {"changelog": [c["version"] for c in new_c] + [v for v in (s.get("new") or {}).get("changelog") or []
                                                                  if v not in {c["version"] for c in new_c}][:10],
                    "blog": [p["id"] for p in new_b]}
        await self.db.kv_set(news.KV, s)
        out = []
        for c in reversed(new_c):
            await self._notify("mention", news.headline(c), c["url"])
            await self.log("news", news.headline(c))
            out.append(c["version"])
        for p in reversed(new_b):
            text = f"New blog post by {p['author']}: {p['title']}"
            await self._notify("mention", text, p["url"])
            await self.log("news", text)
            out.append(p["id"])
        if out:
            self.hub.publish("state", "news")
        return out

    async def _notify(self, level: str, text: str, link: str = "/fleets") -> None:
        cur = await self.db.execute("INSERT INTO notifications(event_id, level, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                                    (None, level, text, None, link, now_iso()))
        self.hub.publish("notify", {"id": cur.lastrowid, "level": level, "title": text, "link": link})

    async def pathing_pass(self, force: bool = False) -> list[str]:
        """Stationed mining fleets with a heading / auto-prospect / auto-relocate (pathing.py): the observatory
        prospects along the heading when few systems lie ahead; when the home has been dry for a while the fleet picks
        its next home (best-ranked system ahead that nothing excludes), announces it, and after the grace period makes
        it its home — the loadout pass then moves the fleet. Every 5 minutes."""
        from . import pathing as pa
        from . import prospects as pr
        from .fleets import fleet_tag
        last = _ts(await self.db.kv_get("pathing_at", None))
        if not force and last and (_now() - last).total_seconds() < 300:
            return []
        await self.db.kv_set("pathing_at", now_iso())
        items = await self.fleets()
        mine = [f for f in items if f.get("role") == "mining" and f.get("station") and f.get("home")
                and (f.get("auto_relocate") or f.get("auto_prospect"))]
        if not mine:
            return []
        devices = await self.devices()
        inp = await pr.rank_inputs(self.db, self, devices)
        xyz_of = inp["xyz"]
        out, changed, now = [], False, _now()
        nothing = ((await self.db.kv_get("salvage_state", {}) or {}).get("nothing") or {})
        # the salvage rule re-marks "nothing to mine" every 24 h while it's still true; an older mark is out of date
        nothing = {k: v for k, v in nothing.items() if _ts(v) and (now - _ts(v)).total_seconds() < 26 * 3600}
        for f in mine:
            home, heading = f["home"], (f.get("heading") or {}).get("vector")
            cone = float(f.get("cone") or pa.DEFAULT_CONE)
            # (prospecting: observatory_pass)
            if not f.get("auto_relocate"):
                continue
            # --- a planned move: carry it out once the grace period is over (if the target is still free)
            if f.get("next_home"):
                if now < (_ts(f.get("next_home_at")) or now):
                    continue
                nxt, block = f["next_home"], pa.taken(f, items).get(f["next_home"])
                if block or nxt in inp["warded"]:
                    f["path_note"] = f"{nxt} isn't free any more ({block or 'warded'}) — choosing again"
                    f.pop("next_home", None), f.pop("next_home_at", None)
                    changed = True
                    continue
                f["prev_home"], f["home"] = home, nxt
                f.pop("next_home", None), f.pop("next_home_at", None), f.pop("depleted_since", None), f.pop("idle_since", None)
                f["path_note"] = f"moved home {home} → {nxt} at {now_iso()} — " + await self.start_relocation(f, home, nxt)
                changed = True
                out.append(f"{f['name']}: home {home} → {nxt}")
                await self.log("fleets", f"{f['name']}: new home {nxt} (was {home}) — the loadout pass moves the fleet there")
                continue
            # --- is the home dry, and for long enough?
            why = pa.dry(home, devices, inp["viability"], nothing, inp["warded"], (await self.system_belts({home}))[home])
            idle = pa.idle_miners(fleet_tag(f["id"]), home, devices)
            if idle is None:
                f.pop("idle_since", None)
            elif not f.get("idle_since"):
                f["idle_since"] = now_iso()
                changed = True
            elif not why and (now - _ts(f["idle_since"])).total_seconds() >= pa.IDLE_HOURS * 3600:
                why = f"none of its miners has mined for {pa.IDLE_HOURS:g} h ({idle})"
                if not f.get("depleted_since"):   # it has been idle long enough already: no second wait
                    f["depleted_since"] = (now - timedelta(hours=float(f.get("dry_hours") or pa.DRY_FOR_HOURS))).isoformat(timespec="seconds")
                    f["path_note"] = f"{home} looks dry: {why}"
                    changed = True
                    await self.log("fleets", f"{f['name']}: {home} looks dry: {why}", "alert")
            if not why:
                if f.pop("depleted_since", None):
                    changed = True
                continue
            if not f.get("depleted_since"):
                f["depleted_since"], f["path_note"] = now_iso(), f"{home} looks dry: {why}"
                changed = True
                continue
            if (now - _ts(f["depleted_since"])).total_seconds() < float(f.get("dry_hours") or pa.DRY_FOR_HOURS) * 3600:
                continue
            rows = (await pr.rank(self.db, self, devices, home, limit_unscanned=0, weigh=True))["rows"]
            star, note = pa.choose(f, items, rows, xyz_of, devices, heading, cone, await self.commandable(devices))
            if star:
                f["next_home"] = star
                f["next_home_at"] = (now + timedelta(minutes=float(f.get("grace_minutes") or pa.GRACE_MINUTES))).isoformat(timespec="seconds")
                f["path_note"] = f"{home} is dry ({why}); next home {star}: " + ", ".join(note)
                changed = True
                out.append(f"{f['name']}: next home {star}")
                await self.log("fleets", f"{f['name']}: {f['path_note']}")
                await self._notify("warning", f"{f['name']}: {home} is dry — moving to {star} at "
                                              f"{f['next_home_at'][11:16]} UTC unless you cancel ({', '.join(note[:2])})")
            else:
                msg = f"{home} is dry ({why}) but no system qualifies as the next home: " + "; ".join(note)
                if f.get("path_note") != msg:
                    f["path_note"] = msg
                    changed = True
                    await self.log("fleets", f"{f['name']}: {msg}", "alert")
                    await self._notify("warning", f"{f['name']}: {msg}")
        if changed:
            await self.save_fleets(items)
        return out

    async def hub_watch_pass(self) -> list[str]:
        """Map › Wards & hubs: a warning in the bell when a hub of ours needs attention — its 7-day shield ends within a
        day, its capacity (as last reported, less 10 %/day) is under 50 %, or its system holds less than one
        maintenance's worth. Once per hub and state, and again only after it recovered."""
        from . import wardhub as wh
        from .shapes import normalize_inventory
        devices = await self.devices()
        if not any(wh.is_hub(d) for d in devices):
            return []
        rows = wh.hubs(devices, await wh.hub_events(self.db),
                       wh.stock_by_star(normalize_inventory(await self.db.kv_get("inventory", []))))
        seen = await self.db.kv_get("hub_alerts", {}) or {}
        out = []
        for h in rows:
            why = []
            if h["shielded"] and h["shield_left_h"] is not None and h["shield_left_h"] < 24:
                why.append(f"its shield ends in {h['shield_left_h']:.0f} h")
            if not h["shielded"] and h["projected"] is not None and h["projected"] < 50:
                why.append(f"capacity about {h['projected']:.0f} %")
            if h["short"]:
                why.append("short of one maintenance in " + h["star"] + ": "
                           + ", ".join(f"{v:g} {k}" for k, v in h["short"].items()))
            key = "|".join(sorted(w.split(" ")[0] for w in why))
            if not why:
                seen.pop(h["code"], None)
                continue
            if seen.get(h["code"]) == key:
                continue
            seen[h["code"]] = key
            text = f"Hub {h['code']} at {h['location']}: " + "; ".join(why)
            cur = await self.db.execute(
                "INSERT INTO notifications(event_id, level, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                (None, "warning", text, None, "/wards", now_iso()))
            self.hub.publish("notify", {"id": cur.lastrowid, "level": "warning", "title": text, "link": "/wards"})
            out.append(text)
        await self.db.kv_set("hub_alerts", seen)
        return out

    async def observatory_pass(self) -> list[str]:
        """prospecting.py: every fleet's deployed observatory unfurls and prospects, direction by direction, and compacts
        when every direction is used up or its fleet is set to leave."""
        from . import fleets as fl
        from . import pathing as pa
        from . import prospecting as pr
        from .loadouts import bound_for
        from .modular import compact_step, compacted, folded, unfurl_step
        from .shapes import normalize_blueprints
        devices = await self.devices()
        from .observatory import is_observatory
        obs = [d for d in devices if is_observatory(d) and fl.fleet_of(d)]
        if not obs:
            return []
        fleet_list = await self.fleets()
        items = {fl.fleet_tag(f["id"])[6:]: f for f in fleet_list}
        fleets_changed = False
        jobs = await self.jobs()
        by_id = {j["id"]: j for j in jobs}
        busy = self.busy_devices(jobs)
        runs = await self.db.kv_get("observatory_runs", {}) or {}
        lmoves = await self.db.kv_get("loadout_moves", {}) or {}
        lfresh = bool(_ts(lmoves.get("at")) and (_now() - _ts(lmoves["at"])).total_seconds() < 3600)
        pos = await self.star_lookup("pos_all")   # shared: read only
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        out: list[str] = []
        for o in obs:
            code, st = o["device_code"], str(o.get("status") or "")
            f = items.get(fl.fleet_of(o) or "")
            here = star_of(o.get("location"))
            if not f or not here or o.get("stowed_in_device_code") or o.get("attached_to_device_code") \
                    or o.get("location_stale") or st.startswith(("travel", "cruis", "surg", "stowed")):
                continue
            run = runs.get(code) or {}
            if run.get("star") != here:
                run = {"star": here, "used": [], "idx": 0, "found": 0}
            if st.startswith("prospecting") and not (run.get("live") and _ts(run["live"].get("completes_at"))
                                                    and _ts(run["live"]["completes_at"]) > _now()):
                # the prospect's direction / start / end (for the galaxy map): from the device list, else its details once
                from .observatory import live_prospect
                live = live_prospect(o)
                if not live:
                    try:
                        live = live_prospect(await self.api.request("GET", f"/devices/{code}", background=True) or {})
                    except ApiError:
                        live = None
                if live:
                    run["live"] = {k: live.get(k) for k in ("direction", "started_at", "completes_at", "origin")}
            # the prospect in flight: wait for it, then judge it by the new stars it found
            if run.get("job"):
                j = by_id.get(run["job"])
                if j and j["status"] in ("running", "waiting"):
                    runs[code] = run
                    continue
                err = next((x.get("error") for x in (j or {}).get("steps", []) if x.get("status") == "failed"), None)
                dense = bool(j and j["status"] == "failed" and "no new stars visible" in str(err or "").lower())
                if dense:
                    # too dense that way (docs: try another direction) — the star isn't used up by a refusal; the next
                    # direction goes out in this same pass
                    run.setdefault("used", []).append(run.get("idx") or 0)
                    run.pop("job", None)
                    run["note"] = f"refused that way: {err}"
                elif j and j["status"] == "failed" and not pr.already_surveyed(err):
                    run.pop("job", None)
                    run["retry_at"] = (_now() + timedelta(minutes=30)).isoformat(timespec="seconds")
                    run["note"] = f"prospect refused: {err}"
                    runs[code] = run
                    continue
                if dense:
                    pass
                elif not pr.already_surveyed(err):
                    if st.startswith("prospecting"):
                        runs[code] = run
                        continue
                    rows = await self.db.fetchall("SELECT payload FROM events WHERE event='prospect.completed' "
                                                  "AND (device_code=? OR payload LIKE ?) AND seq > ?",
                                                  (code, f"%{code}%", run.get("seq0") or 0))
                    # finished but the event isn't in yet: wait a little past the prospect's end (else 1 h from its
                    # start). It used to be 6 h from the start, silently — Prospectors 1, 2 and 5 sat unfurled and idle
                    # for hours at their stars (snapshot 2026-11-12 20:11)
                    end = _ts((run.get("live") or {}).get("completes_at"))
                    until = (end + timedelta(minutes=15)) if end else (_ts(run.get("at")) + timedelta(hours=1) if _ts(run.get("at")) else None)
                    if not rows and until and _now() < until:
                        run["note"] = f"prospect finished; waiting for its result until {until.strftime('%H:%M')} UTC"
                        runs[code] = run
                        continue
                    if not rows:
                        run["note"] = "no prospect result came in: counted as 0 new stars"
                    # the stars known when this prospect started (kept apart: see the end of this pass)
                    before = set(run.get("known") or (await self.db.kv_get(f"observatory_known:{code}", []) if run.get("known_kv") else []) or [])
                    found = set()
                    for r in rows:
                        found |= {(x.get("designation") if isinstance(x, dict) else x)
                                  for x in json.loads(r["payload"] or "{}").get("stars") or []}
                    new = len(found - before - {None})
                    run["found"] = int(run.get("found") or 0) + new
                else:
                    new = 0
                if not dense:
                    # docs + live (2026-10-08): a star can be prospected from only once — after one prospect (or
                    # "already been surveyed"), every direction is used up there
                    run["used"] = list(range(pr.N_DIRECTIONS))
                    pf = await self.db.kv_get("prospected_from", {}) or {}
                    pf[here] = {"at": now_iso(), "by": code, "found": new,
                                "outcome": "already surveyed" if pr.already_surveyed(err) else f"{new} new star(s)"}
                    await self.db.kv_set("prospected_from", pf)
                    run.pop("job", None)
            moving = (lmoves.get("moves") or {}).get(code)
            leaving = (bool(f.get("next_home")) or (bound_for(o, set(pos), {x["id"]: x.get("home") for x in fleet_list}) not in (None, here))
                       or bool(moving and moving != here and lfresh))
            nxt = pr.next_index(run)
            run["done"] = nxt is None
            runs[code] = run
            if code in busy or (_ts(run.get("retry_at")) and _now() < _ts(run["retry_at"])):
                continue
            if moving and moving != here and lfresh:
                run["note"] = f"loadouts is moving it to {moving}"
                continue   # loadouts compacts and carries it; nothing for this pass to do
            if (f.get("mission") or {}).get("status") in ("running", "stalled", "stopped"):
                run["note"] = "its fleet is on a mission"
                continue   # the mission packs up / unloads the observatory; nothing for this pass to do
            if run["done"] and f.get("role") == "explore" and f.get("advance") and here == f.get("home") and not leaving:
                # advance along the heading: the farthest star ahead becomes the fleet's home; the loadout pass packs up
                # and carries everyone there (the observatory compacts for the trip), leaving a relay and a beacon behind
                if await self._advance_explorer(f, fleet_list, devices, pos, out):
                    fleets_changed = True
                    continue
            if leaving or run["done"]:   # pack up: the fleet moves on, or nothing is left to find here
                if not compacted(o) and "compact" in (o.get("available_commands") or ["compact"]) \
                        and not st.startswith(("prospecting", "unfurling")):
                    why = "the fleet is leaving" if leaving else f"every direction tried in {here}"
                    if await self.create_job("observatory", f"{f['name']}: compact observatory {code} ({why})", code,
                                             [compact_step(code, bps, o, f"({why})")], {"devices": [code]}):
                        out.append(f"{code}: compacting ({why})")
                continue
            if st.startswith(("compacting", "unfurling", "prospecting")):
                continue   # busy (a prospect started by hand counts too)
            if folded(o) or st.startswith("compact"):
                if await self.create_job("observatory", f"{f['name']}: unfurl observatory {code} to prospect", code,
                                         [unfurl_step(code)], {"devices": [code]}):
                    out.append(f"{code}: unfurling")
                continue
            if "prospect" not in (o.get("available_commands") or []):
                continue
            dirs = pr.directions(pr.base_heading((f.get("heading") or {}).get("vector"), pa.xyz(pos.get(here))))
            if not (f.get("heading") or {}).get("vector"):
                # one prospect per star: emptiest hemisphere first (a fleet's set heading still goes first)
                dirs = pr.rank_directions(dirs, pa.xyz(pos.get(here)), await self.star_lookup("xyz"))
            v = dirs[nxt]
            seq0 = (await self.db.fetchone("SELECT max(seq) AS s FROM events") or {"s": 0})["s"] or 0
            job = await self.create_job("observatory", f"{f['name']}: prospect from {here} (direction {nxt + 1} of {len(dirs)})",
                                        code, [step(f"{code}: prospect {v}", f"/devices/{code}", {"command": "prospect", "direction": v})],
                                        {"devices": [code]})
            if job:
                from .census import observatory_stars
                found_so_far, unplaced = await observatory_stars(self.db)
                run.update({"idx": nxt, "job": job["id"], "seq0": seq0, "at": now_iso(),
                            "known": sorted(set(pos) | set(found_so_far) | set(unplaced))})
                run.pop("note", None)
                out.append(f"{code}: prospecting from {here}, direction {nxt + 1}")
        for c, r in runs.items():
            # every star known when a prospect began is about a megabyte per run: it is kept in a value of its own
            # (written once, read once) so the runs stay small for the passes and the map that read them all the time,
            # and emptied when the result is in
            if not isinstance(r, dict):
                continue
            if r.get("job") and "known" in r:
                await self.db.kv_set(f"observatory_known:{c}", r.pop("known"))
                r["known_kv"] = True
            elif not r.get("job"):
                r.pop("known", None)
                if r.pop("known_kv", None):
                    await self.db.kv_set(f"observatory_known:{c}", [])
        await self.db.kv_set("observatory_runs", runs)
        if fleets_changed:
            await self.save_fleets(fleet_list)
        return out

    async def prospector_pass(self) -> list[str]:
        """Fleets with role "prospector" (an observatory and a carrier): tour the stars within `radius` ly (30) of the fleet's
        home, one prospect each (observatory_pass prospects and compacts), then the carrier takes the observatory to the next
        star — the emptiest neighborhood not prospected from yet, nearer first, commandable. A tour that hasn't moved on or
        found anything for STALL_HOURS raises a warning (once per stall)."""
        from . import fleets as fl
        from . import pathing as pa
        from . import prospecting as pr
        from .modular import folded
        STALL_HOURS = 12
        fleet_list = await self.fleets()
        mine = [f for f in fleet_list if f.get("role") == "prospector" and f.get("home") and not f.get("recall_hold")]   # recalled: held
        if not mine:
            return []
        devices = await self.devices()
        jobs = {j["id"]: j for j in await self.jobs()}
        busy = self.busy_devices(list(jobs.values()))
        stars = await self.star_lookup("by")      # shared: read only
        xyz_all = await self.star_lookup("xyz")
        runs = await self.db.kv_get("observatory_runs", {}) or {}
        pfrom = await self.db.kv_get("prospected_from", {}) or {}
        allowed = await self.commandable(devices)
        out, changed = [], False
        for f in mine:
            if f.get("station"):
                f["station"] = False   # it tours: the loadout pass doesn't keep it at home
                changed = True
            tour = f.setdefault("tour", {"since": now_iso()})
            ms = fl.members(f, devices)
            from .observatory import is_observatory
            obs = next((d for d in ms if is_observatory(d)), None)
            car = next((d for d in ms if fl.is_carrier(d) and fl.capacity(d) > 0), None)
            if not obs or not car:
                tour["note"] = "needs a galactic observatory (or parallax array) and a carrier with an attach point"
                continue
            j = jobs.get(tour.get("job"))
            if j and j["status"] in ("running", "waiting"):
                continue
            if j and j["status"] == "failed":
                err = next((s.get("error") for s in j["steps"] if s.get("status") == "failed"), "")
                tour["note"] = f"last move failed: {err}"
            tour.pop("job", None)
            here = star_of(obs.get("location") or "")
            if here and here != tour.get("star"):
                tour.update({"star": here, "since": now_iso(), "alerted": False})
                tour.pop("note", None)   # "moving to <here>" is over: a later stall alert names what's wrong here
                changed = True
            if here and (pfrom.get(here) or {}).get("at", "") > (tour.get("since") or ""):
                tour["since"] = pfrom[here]["at"]   # a prospect here counts as progress
            # moving on: the observatory is folded and done with this star (one prospect each)
            run = runs.get(obs["device_code"]) or {}
            ready = here and obs.get("location") and folded(obs) and (here in pfrom or (run.get("star") == here and run.get("done")))
            if here and not ready and obs.get("attached_to_device_code") == car["device_code"] and star_of(car.get("location") or "") == here \
                    and not fl.in_flight(car) and not str(car.get("status") or "").lower().startswith(("travel", "cruis", "surg")) \
                    and obs["device_code"] not in busy and car["device_code"] not in busy:
                # still towed at a star it hasn't prospected from (the tour's detach never happened: a failed or
                # canceled move, or a trip that ended here): let go of it, so observatory_pass unfurls and prospects.
                # Snapshot 2026-11-12 17:03: Prospectors 3 and 4 sat folded on their platforms with nothing running.
                cc = car["device_code"]
                job = await self.create_job("fleets", f"{f['name']}: put observatory {obs['device_code']} down at {here}",
                                            cc, [step(f"{cc}: detach {obs['device_code']}", f"/devices/{cc}",
                                                      {"command": "detach", "device": obs["device_code"]}, critical=True)],
                                            {"devices": [cc, obs["device_code"]]})
                if job:
                    tour.update({"job": job["id"], "note": f"putting the observatory down at {here} to prospect"})
                    out.append(f"{f['name']}: detaching at {here}")
                    changed = True
                continue
            if ready and obs["device_code"] not in busy and car["device_code"] not in busy:
                taken = {(x.get("tour") or {}).get("target") for x in mine if x is not f} | \
                        {(x.get("tour") or {}).get("star") for x in mine if x is not f}
                nxt, left = pr.next_star(here, f["home"], xyz_all, set(pfrom), taken - {None}, allowed,
                                         float(f.get("radius") or 30))
                tour["left"] = left
                if not nxt:
                    tour["note"] = f"no star left to prospect within {f.get('radius') or 30} ly of {f['home']}"
                    if not tour.get("alerted"):
                        tour["alerted"] = True
                        await self._notify("warning", f"{f['name']}: {tour['note']}", "/fleets")
                    continue
                dest = fl.destination(nxt, stars)
                steps = []
                # every other member comes along (live 2026-10-16: the matrix containers stayed at FALQUORYX-BELT-1
                # while the surge platforms towed the observatories away): the carrier collects them first
                cc = car["device_code"]
                free = fl.seats(car, devices)
                if obs.get("attached_to_device_code") != cc:
                    free["attach"] -= 1   # kept for the observatory
                strays: dict[str, list[tuple[str, str]]] = {}
                no_room = []
                for d in ms:
                    if d is obs or d is car or cc in (d.get("stowed_in_device_code"), d.get("attached_to_device_code")):
                        continue
                    host = next((x for x in devices if x.get("device_code") in
                                 (d.get("stowed_in_device_code"), d.get("attached_to_device_code"))), None)
                    loc = d.get("location") or (host or {}).get("location")
                    mode = fl.take_seat(free, d) if loc and not host else None
                    if mode:
                        strays.setdefault(loc, []).append((d["device_code"], mode))
                    else:
                        no_room.append(d["device_code"])
                at = car.get("location")
                if strays:
                    tour_steps, at = fl.pickup_tour(cc, at, strays)
                    steps += tour_steps
                if at != obs.get("location"):   # the carrier comes to the observatory
                    steps += [step(f"{cc} → {obs['location']}", f"/devices/{cc}",
                                   {"command": "travel", "destination": obs["location"]}, critical=True),
                              fl._wait_arrive(cc, obs["location"])]
                if obs.get("attached_to_device_code") != cc:   # a retried move after a failed trip: still towed
                    steps.append(fl.board_step(cc, obs["device_code"], "attach"))
                steps += [step(f"{car['device_code']} → {dest}", f"/devices/{car['device_code']}",
                               {"command": "travel", "destination": dest}, critical=True),
                          fl._wait_arrive(car["device_code"], dest, nxt),
                          step(f"{car['device_code']}: detach {obs['device_code']}", f"/devices/{car['device_code']}",
                               {"command": "detach", "device": obs["device_code"]}, critical=True)]
                job = await self.create_job("fleets", f"{f['name']}: observatory {here} → {nxt} (prospecting tour)",
                                            cc, steps, {"devices": [cc, obs["device_code"]] + [c for g in strays.values()
                                                                                                for c, _ in g]})
                if job:
                    tour.update({"job": job["id"], "target": nxt, "note": f"moving to {nxt} ({left} star(s) left in range)"
                                 + (f"; picking up {', '.join(c for g in strays.values() for c, _ in g)} first" if strays else "")
                                 + (f"; can't take {', '.join(no_room)} (no room or aboard another device)" if no_room else "")})
                    out.append(f"{f['name']}: {here} → {nxt}")
                    changed = True
                continue
            # stalled: nothing found or moved for too long
            since = _ts(tour.get("since"))
            if since and (_now() - since).total_seconds() > STALL_HOURS * 3600 and not tour.get("alerted"):
                tour["alerted"] = True
                await self._notify("warning", f"{f['name']}: the prospecting tour has stalled at {here or '?'} for "
                                              f"{STALL_HOURS} h+ — {tour.get('note') or (runs.get(obs['device_code']) or {}).get('note') or 'check the observatory'}",
                                   "/fleets")
                changed = True
        if changed:
            await self.save_fleets(fleet_list)
        return out

    async def dark_home(self, f: dict, star: str, devices: list[dict]) -> str | None:
        """Why a fleet can't move its home to `star`, or None: nothing there could be commanded on arrival (no active
        relay network covers it and no replicant rides with the fleet). Live 2026-10-19: Miner_4 relocated to OXETH,
        which neither, and all 30 devices aboard 15DFB014 went out_of_range before they could be unloaded."""
        from . import fleets as fl
        star = star_of(star)
        if not star or star in await self.commandable(devices):
            return None
        if fl.fleet_replicant(f, devices, await self.db.kv_get("replicants", {}) or {}):
            return None
        return (f"{star} is outside an active relay network and no replicant rides with the fleet: nothing could be "
                f"commanded there on arrival — get a relay to {star} (or put a replicant aboard) first")

    async def start_relocation(self, f: dict, old: str, new: str, devices: list[dict] | None = None) -> str:
        """A stationed explore fleet whose home just changed moves there together, in one relocation mission (pack up —
        large devices compacted — everyone aboard, gather strays, travel, unload), when its carriers' hold slots and
        attach points cover every rider. Otherwise, and always for a mining fleet (owner 2026-10-21: no assembly stage),
        the loadout pass carries it piecemeal: its own and the logistics fleets' carriers each take as many as they hold
        and leave as they fill. Returns what happens, for the log."""
        from . import fleets as fl
        from .shapes import normalize_blueprints
        devices = devices if devices is not None else await self.devices()
        # a new home never waits for a complete loadout (owner 2026-10-21): the loadout pass fills the fleet there
        dark = await self.dark_home(f, new, devices) if old and new and old != new \
            and (f.get("mission") or {}).get("status") not in ("running", "stalled", "stopped") else None
        if dark:
            f["home"], f["pending_home"] = old, new   # it stays; pending_home_pass moves it once it can be commanded there
            return f"{new} is pending: {dark}"
        f.pop("pending_home", None)
        if old and new and old != new and f.get("materials") == "self":
            # a destination fleet (takes materials in): what's stockpiled at its old home follows it (logistics.py); any
            # other fleet's old piles are just stray ones, returned to the nearest gather point
            from .logistics import KV_MOVES
            moves = await self.db.kv_get(KV_MOVES, {}) or {}
            moves[old] = {"to": new, "fleet": f.get("id"), "at": now_iso()}
            await self.db.kv_set(KV_MOVES, moves)
        solo = fl.solo_flyers(f, devices)
        here = fl.together(f, devices, prefer=old, free=solo)
        start = here["at"] if here["ready"] else old   # where it's gathered: it leaves from there
        if f.get("role") != "explore" or not f.get("station") or old == new \
                or (f.get("mission") or {}).get("status") in ("running", "stalled", "stopped"):
            f.pop("move_partial", None)
            return "the loadout pass moves it"
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        if not [d for d in fl.members(f, devices) if star_of(d.get("location")) != new]:
            return "nothing to move"
        short = fl.attach_points(f, devices, bps)["now"]["short"]
        if short > 0:
            return f"its carriers are {short} seat(s) short of carrying everyone, so the loadout pass moves it piecemeal"
        m = {"status": "running", "phase": None, "idx": 0, "targets": [new], "started_at": now_iso(), "log": [],
             "opts": {"partial": bool(f.pop("move_partial", False))}, "auto": True, "relocate": True, "from": start,
             "solo": sorted(solo)}
        self._mlog(m, f"relocating {start} → {new}: pack up, everyone aboard, travel together, unload"
                      + (f"; {', '.join(sorted(solo))} fly there on their own after their current task" if solo else ""))
        f["mission"] = m
        return "moving together in one relocation mission (everyone aboard before anything leaves)"

    async def pending_home_pass(self) -> list[str]:
        """Fleets with a new home nothing could be commanded at yet (start_relocation): move once it can be."""
        from . import fleets as fl
        items = await self.fleets()
        waiting = [f for f in items if f.get("pending_home")]
        if not waiting:
            return []
        devices = await self.devices()
        out = []
        for f in waiting:
            new = f["pending_home"]
            dark = await self.dark_home(f, new, devices)
            if dark:
                note = f"{new} pending: {dark}"
                if f.get("path_note") != note:
                    f["path_note"] = note
                continue
            old = f["home"]
            f["home"] = new
            f.pop("pending_home", None)
            how = await self.start_relocation(f, old, new, devices)
            f["path_note"] = f"moving home {old} → {new} — {how}"
            out.append(f"{f['name']}: {old} → {new}")
            await self.log("fleets", f"{f['name']}: {new} reachable — moving home {old} → {new}: {how}")
        await self.save_fleets(items)
        return out

    async def _advance_explorer(self, f: dict, fleets: list[dict], devices: list[dict], pos: dict, out: list[str]) -> bool:
        """Move an advancing explore fleet's home to the farthest star ahead (pathing.advance_target). Without a replicant
        aboard only stars inside relay coverage qualify (arrivals outside it couldn't be commanded). A relay and a beacon
        of the fleet's in the old system stay there (fleet tag off, pinned where they are) when there's none of ours
        besides; a fleet without them just moves. True when it moved."""
        from . import fleets as fl
        from . import pathing as pa
        from . import prospecting as pr
        from .loadouts import at_tag
        home = f["home"]
        xyz_of = {k: v for k, v in ((k, pa.xyz(p)) for k, p in pos.items()) if v}
        heading = (f.get("heading") or {}).get("vector") or pr.base_heading(None, xyz_of.get(home))
        riding = bool(fl.fleet_replicant(f, devices, await self.db.kv_get("replicants", {}) or {}))
        skip = set(pa.taken(f, fleets)) | set(f.get("advanced_from") or [])
        reach = await self.commandable(devices, {k: {"position": v} for k, v in pos.items()}) if not riding else set()
        nxt = pa.advance_target(home, xyz_of, heading, float(f.get("cone") or pa.DEFAULT_CONE),
                                float(f.get("hop_ly") or 30), skip,
                                (lambda s: True) if riding else (lambda s: s in reach))
        if not nxt:
            note = (f"nothing ahead within {f.get('hop_ly') or 30:g} ly" + ("" if riding else " inside relay coverage")
                    + " — staying in " + home)
            if f.get("path_note") != note:
                f["path_note"] = note
                await self.log("fleets", f"{f['name']}: {note}", notify=True)
                return True
            return False
        tag = fl.fleet_tag(f["id"])
        steps = []
        for t in ("ftl_relay", "ftl_beacon"):
            mine_here = [d for d in devices if d.get("device_type") == t and star_of(d.get("location")) == home
                         and not d.get("stowed_in_device_code")]
            ours = [d for d in mine_here if tag in (d.get("tags") or [])]
            if ours and len(mine_here) == len(ours):   # no other one of ours there: leave this one behind
                d = ours[0]
                steps.append(step(f"{d['device_code']}: stays in {home}", f"/devices/{d['device_code']}",
                                  {"configuration": {"add_tags": [at_tag(d["location"])], "remove_tags": [tag]}}, method="PATCH"))
        if steps:
            await self.create_job("fleets", f"{f['name']}: leave a relay / beacon in {home}", None, steps, {"devices": []})
        f.setdefault("advanced_from", []).append(home)
        f["prev_home"], f["home"], f["station"] = home, nxt, True
        f["path_note"] = f"advancing from {home} to {nxt} (farthest star ahead within {f.get('hop_ly') or 30:g} ly)"
        # one relocation mission: compact the observatory and board everyone (nothing leaves until all are aboard —
        # far out there's no relay, so anything left behind couldn't be commanded once the replicant goes), fly together,
        # unload at the new home. The loadout pass leaves a fleet on a mission alone.
        how = await self.start_relocation(f, home, nxt, devices)
        f["path_note"] += f" — {how}"
        out.append(f"{f['name']}: advancing {home} → {nxt}")
        await self.log("fleets", f"{f['name']}: every direction prospected from {home} — advancing to {nxt}")
        return True

    SCOUT_MIN_CAPACITY = 50.0   # a member at or below this (%) ends the scouting: finish the system, go home for repairs
    SCOUT_START_CAPACITY = 85.0  # a run starts only with every member at or above this (%)
    SCOUT_RADIUS_LY = 100.0     # no further than this from home; nothing left inside it ends the scouting too

    async def scout_next(self, f: dict, items: list[dict], devices: list[dict], here: str | None = None,
                         exclude: set[str] | frozenset = frozenset()) -> tuple[str | None, str]:
        """The next star an auto-scouting fleet surveys: one not surveyed yet (scanned, every body but the belts
        surveyed), nearest its home first and then nearest where it is — so it works outward ring by ring through
        adjacent systems. Without a replicant riding along: only systems in our relay coverage. Not another player's
        warded system, another fleet's home, or a star another fleet is heading to. (star, why-none)."""
        from . import fleets as fl
        from . import pathing as pa
        from . import wards
        from .census import observatory_stars
        shared = await self.star_lookup("pos")   # shared and read-only: copying the catalog per fleet was most of the pass
        found, _ = await observatory_stars(self.db)
        extra = {k: v.get("position") for k, v in found.items() if k not in shared and v.get("position")}
        pos = {**shared, **extra} if extra else shared
        xyz_all = await self.star_lookup("xyz")
        xyz_of = {**xyz_all, **{k: v for k, v in ((k, pa.xyz(p)) for k, p in extra.items()) if v}} if extra else xyz_all
        home = f.get("home") or ""
        if not xyz_of.get(home):
            return None, f"{home or 'its home'} has no position in the star catalog"
        scans = {r["star"]: r["data"] for r in await self.db.fetchall("SELECT star, data FROM systems")}
        surveyed = await self.db.kv_get("surveyed", {}) or {}
        # only where another fleet is or is still heading: the targets it already left behind are done with (Surveyor 10
        # had 124 targets on its list, so every other survey fleet found nothing left to scout, 2026-11-11)
        busy = {star_of(t) for g in items if g.get("id") != f.get("id") and (g.get("mission") or {}).get("status") in ("running", "stalled")
                for t in (g["mission"].get("targets") or [])[(g["mission"].get("idx") or 0):]}
        skip = busy | (fl.worked_systems(items) - {home}) | wards.foreign(await self.star_lookup("flagged"), devices) | set(exclude)
        # a fleet with a replicant aboard may go beyond relay coverage: the replicant commands the system it is in (Joe
        # 2026-11-12, overriding the 11-10 rule after Surveyor 4 went out_of_range at RASALGEDO); without one, coverage only
        riding = fl.has_replicant(f, devices)
        reach = await self.commandable(devices, {k: {"position": v} for k, v in pos.items()} if extra else None)
        # per home hub (kv scout_radii {home: ly}, set on a survey fleet's auto-scout row), else SCOUT_RADIUS_LY
        radius = float((await self.db.kv_get("scout_radii", {}) or {}).get(home) or self.SCOUT_RADIUS_LY)

        def pick() -> str | None:   # pure: every catalog star against local copies, so it runs in a thread
            def done(star: str) -> bool:
                if star not in scans:
                    return False
                try:
                    scan = json.loads(scans[star] or "{}")
                except ValueError:
                    return False
                return not survey_targets(scan, surveyed, False, False, 1)   # every body but the belts surveyed
            home_xyz = xyz_of[home]
            here_xyz = xyz_of.get(here or home) or home_xyz
            cands = []
            for s, p in xyz_of.items():
                if p is None or s in skip or (not riding and s not in reach):
                    continue
                dh = math.dist(p, home_xyz)
                if dh > radius or done(s):
                    continue
                cands.append((round(dh / 2.0), math.dist(p, here_xyz), s))   # 2-ly rings outward from home; within a ring, the nearest hop first
            return min(cands)[2] if cands else None
        best = await asyncio.to_thread(pick)
        if best is None:
            return None, (f"every known system within {radius:g} ly of {home} "
                          + ("" if riding else "inside relay coverage ") + "is surveyed or taken")
        return best, ""

    async def system_surveyed(self, star: str) -> bool:
        """Every body but the belts surveyed (scout_next's test), or an AMI survey_system reported nothing left."""
        star = star_of(star)
        if star in (await self.db.kv_get("ami_surveyed", {}) or {}):
            return True
        row = await self.db.fetchone("SELECT data FROM systems WHERE star=?", (star,))
        if not row:
            return False
        try:
            scan = json.loads(row["data"] or "{}")
        except ValueError:
            return False
        return not survey_targets(scan, await self.db.kv_get("surveyed", {}) or {}, False, False, 1)

    def scout_unhealthy(self, f: dict, devices: list[dict], start_at: float | None = None) -> list[str]:
        """Members at or below the in-run limit (50 %), or — to start a run — below `start_at` (85 %)."""
        from . import fleets as fl
        from .loadouts import _cap
        return [d["device_code"] for d in fl.members(f, devices)
                if (_cap(d) < start_at if start_at is not None else _cap(d) <= self.SCOUT_MIN_CAPACITY)]

    async def deploy_for_repairs(self, f: dict, weak: list[str], devices: list[dict]) -> list[str]:
        """Only deployed devices get repaired: deploy the fleet's worn members still stowed in (or attached to) a vessel
        parked in its home system. Readiness counts deployed members at home, so the next run takes them aboard again."""
        from . import fleets as fl
        by = {d.get("device_code"): d for d in devices}
        home, out = f.get("home") or "", []
        for code in weak:
            d = by.get(code) or {}
            host = by.get(d.get("stowed_in_device_code") or d.get("attached_to_device_code") or "")
            if (not host or not home or star_of(host.get("location")) != home or fl.in_flight(host)
                    or str(host.get("status") or "").lower().startswith(("travel", "cruis", "surg"))):
                continue
            ok, _resp, err = await self.send("POST", f"/devices/{code}", {"command": "deploy"},
                                             f"auto: deploy {code} for repairs ({f.get('name') or f.get('id')})")
            out.append(f"{f.get('name') or f.get('id')}: deployed {code} for repairs" if ok
                       else f"{f.get('name') or f.get('id')}: deploying {code} for repairs failed: {err}")
        return out

    async def repair_return_pass(self) -> list[str]:
        """Mining and survey fleets (auto-scouting ones have their own rule): a member at or below 50 % sends the fleet
        home — a mission in its watch phase recalls now (then returns and unloads as usual). Back home, members below
        85 % still aboard a vessel there are deployed, once each, so the maintenance drone can repair them."""
        items = await self.fleets()
        devices = await self.devices()
        out, changed = [], False
        for f in items:
            if f.get("role") not in ("mining", "explore"):
                continue
            m = f.get("mission") or {}
            if m.get("status") in ("running", "stalled"):
                if m.get("scouting") or m.get("repair_return") or m.get("status") != "running" or m.get("end_here"):
                    continue
                weak = self.scout_unhealthy(f, devices)
                if weak and m.get("phase") == "watch":
                    m["recall_now"] = m["repair_return"] = True
                    if f["role"] == "explore":
                        m["targets"] = (m.get("targets") or [])[:m.get("idx", 0) + 1]   # no further targets
                    why = f"{', '.join(weak)} at or below {self.SCOUT_MIN_CAPACITY:g} %"
                    self._mlog(m, f"returning home for repairs ({why})")
                    await self.log("fleets", f"{f.get('name')}: returning home for repairs ({why})")
                    out.append(f"{f.get('name')}: home for repairs")
                    changed = True
                continue
            if f.get("auto_scout"):
                continue   # auto_scout_pass deploys those
            weak = self.scout_unhealthy(f, devices, self.SCOUT_START_CAPACITY)
            tried = set(f.get("repair_deploys") or [])
            sent = await self.deploy_for_repairs(f, [c for c in weak if c not in tried], devices) if weak else []
            if sent:
                out += sent
                f["repair_deploys"] = sorted(tried | {c for c in weak if any(c in x for x in sent)})
                changed = True
            elif not weak and f.get("repair_deploys"):
                f["repair_deploys"] = []
                changed = True
        if changed:
            await self.save_fleets(items)
        return out

    async def auto_scout_pass(self) -> list[str]:
        """Explore fleets with 'auto-scout' ticked: when one is free and every member is above 50 % capacity, send it
        to the next unsurveyed system (scout_next); each time it finishes a system it takes the next one, until a member
        is down to 50 % — then it finishes that system and returns home for repairs. A run starts only with every member at
        85 % or more, and only with its loadout complete at home."""
        from . import fleets as fl
        items = await self.fleets()
        free = [f for f in items if f.get("role") == "explore" and f.get("auto_scout")
                and (f.get("mission") or {}).get("status") not in ("running", "stalled", "stopped")]
        if not free:
            return []
        devices = await self.devices()
        out, changed = [], False
        for f in free:
            weak = self.scout_unhealthy(f, devices, self.SCOUT_START_CAPACITY)
            if weak:
                tried = set(f.get("repair_deploys") or [])
                sent = await self.deploy_for_repairs(f, [c for c in weak if c not in tried], devices)
                if sent:   # once per worn member: the device list may lag the deploy
                    out += sent
                    f["repair_deploys"], changed = sorted(tried | {c for c in weak if any(c in x for x in sent)}), True
                note = f"waiting for repairs: {', '.join(weak)} below {self.SCOUT_START_CAPACITY:g} % capacity"
                if f.get("scout_note") != note:
                    f["scout_note"], changed = note, True
                continue
            if f.get("repair_deploys"):
                f["repair_deploys"], changed = [], True
            r = fl.readiness(f, devices)
            if not r["ready"]:   # nothing leaves short: the loadout pass fills it at home first
                note = f"waiting: {fl.readiness_text(r)}"
                if f.get("scout_note") != note:
                    f["scout_note"], changed = note, True
                continue
            star, why = await self.scout_next(f, items, devices)
            if not star:
                if f.get("scout_note") != why:
                    f["scout_note"], changed = why, True
                continue
            m = {"status": "running", "phase": None, "idx": 0, "targets": [star], "started_at": now_iso(), "log": [],
                 "opts": {}, "auto": True, "scouting": True}
            self._mlog(m, f"started automatically: scouting {star} (nearest unsurveyed system)")
            f["mission"], f["scout_note"], changed = m, f"scouting from {f.get('home')}", True
            out.append(f"{f['name']}: scouting {star}")
            await self.log("fleets", f"{f['name']}: scouting {star} (auto)")
        if changed:
            await self.save_fleets(items)
        return out

    async def decommission_queue_pass(self) -> list[str]:
        """Devices asked to be decommissioned at an autofactory (decommission.py): once one is idle at its
        autofactory, decommission it there (the autofactory learns its blueprint)."""
        from . import decommission as dc
        q = await self.db.kv_get(dc.KV, {}) or {}
        if not q:
            return []
        devices = await self.devices()
        by = {d.get("device_code"): d for d in devices}
        busy = self.busy_devices(await self.jobs())
        out = []
        for code, e in list(q.items()):
            d = by.get(code)
            if not d:
                q.pop(code)   # gone from the list: decommissioned (or given away)
                continue
            if code in busy:
                continue
            sent = _ts(e.get("sent_at"))
            if sent and (_now() - sent).total_seconds() > 600 and not await self.db.fetchone(
                    "SELECT 1 FROM events WHERE event='device.decommissioned' AND device_code=?", (code,)):
                # its job ended (failed, timed out, canceled) and the device is still here: try again
                e.pop("sent_at", None)
                await self.log("decommission", f"{code}: still here after its decommission job ended — trying again")
            if not dc.ready(d, e):
                continue
            st = step(f"{code}: decommission at autofactory {e['factory']}", f"/devices/{code}", {"command": "decommission"},
                      wait=["device.decommissioned"], timeout=3600, critical=True)
            st["wait_device"] = code
            job = await self.create_job("decommission", f"{code} ({d.get('device_type')}): decommission at autofactory "
                                        f"{e['factory']} ({e['at']})", code, [st], {"devices": [code]})
            if job:
                e["sent_at"] = now_iso()
                out.append(f"{code} → decommission at {e['at']}")
        await self.db.kv_set(dc.KV, q)
        return out

    # --- AMI schedules ---------------------------------------------------------------------------------
    async def schedules(self) -> list[dict]:
        return await self.db.kv_get("ami_schedules", []) or []

    async def save_schedules(self, items: list[dict]) -> None:
        await self.db.kv_set("ami_schedules", items)

    async def run_due_schedules(self) -> None:
        from .ami_schedule import due
        if not await self.rule_cfg("ami_schedules"):
            return
        items = await self.schedules()
        changed = False
        for sched in items:
            if due(sched):
                await self.run_schedule(sched)
                changed = True
        if changed:
            await self.save_schedules(items)

    async def run_schedule(self, sched: dict, manual: bool = False) -> list[str]:
        """Apply one schedule now. Mutates sched['last_run'/'last_result']; returns what happened per controller."""
        from .ami_schedule import adoptable, controller_idle, managed_by, schedule_steps, targets_of
        devices = await self.devices()
        managed = await managed_by(self.db)
        busy = self.busy_devices(await self.jobs())
        results = []
        warded = await self.warded(devices)
        for ctrl in targets_of(sched, devices):
            code = ctrl["device_code"]
            if code in busy:
                results.append(f"{code}: already running a job")
                continue
            if ctrl.get("in_control_range") is False:   # the game refuses it ("Device is out of comms range"): skip, quietly
                results.append(f"{code}: out of comms range — skipped")
                continue
            if "mining" in (ctrl.get("device_type") or "") and star_of(ctrl.get("location")) in warded:
                results.append(f"{code}: another player's ward or hub in {star_of(ctrl.get('location'))} — no mining there")
                continue
            rest = (await self.db.kv_get("rested", {}) or {}).get(code)
            if rest and not manual and await self.open_sites_now(rest["belt"]) <= 0:
                results.append(f"{code}: resting at {rest['belt']} until it has open sites (an exhausted controller overheats)")
                continue
            idle, why = await controller_idle(self.db, ctrl)
            if sched.get("only_idle", True) and not idle:
                results.append(f"{code}: busy ({why})")
                continue
            dv = ctrl.get("ami_directive") or {}
            state = str(dv.get("_eval_state") or "")
            if dv.get("name") == "gather_salvage" and not state.startswith(("done", "complete", "no_targets")):
                results.append(f"{code}: on salvage — left alone")
                continue
            if not manual and dv.get("name") == sched["directive"] and state.startswith("exhausted"):
                from .salvage import belt_of, exhausted_place
                place = exhausted_place(state)
                pb = belt_of(place)
                if pb and pb == place and await self.open_sites_now(pb) > 0:
                    pass  # stale: the belt it ran dry at has open sites again — re-sending restarts it
                elif place and not pb:
                    results.append(f"{code}: exhausted at {place} (its drones are off the belt) — 'Salvage when depleted' "
                                   "brings them back to a belt with open sites")
                    continue
                else:
                    results.append(f"{code}: exhausted on {sched['directive']} at {place or '?'} — re-sending it won't help until sites re-open")
                    continue
            adopt = adoptable(devices, ctrl, managed) if sched.get("adopt", True) else []
            use = await self.print_tilt(sched, ctrl, devices)
            job = await self.create_job("ami_schedules", f"{use.get('name') or use['directive']} → {code}", code,
                                        schedule_steps(ctrl, use, adopt), {"schedule": sched.get("id"), "devices": adopt},
                                        force=manual)
            results.append(f"{code}: {'started' if job else 'planned (dry run)'}"
                           + (f", adopting {len(adopt)}" if adopt else "") + f" ({why})")
        if not results:
            results.append("no matching controllers")
        sched["last_run"] = _now().isoformat(timespec="seconds")
        sched["last_result"] = "; ".join(results)
        return results

    # --- repeating console commands --------------------------------------------------------------------
    async def run_due_repeats(self, only: str | None = None) -> None:
        """Note finds in each repeat's system, then send the ones that are due (only=id: that one, now)."""
        from . import repeats as rp
        items = await self.db.kv_get(rp.KV, []) or []
        if not any(r.get("enabled", True) for r in items) and not only:
            return
        devices = await self.devices()
        reps = await self.db.kv_get("replicants", {}) or {}
        marks = ",".join("?" * len(rp.FOUND_EVENTS))
        changed = False
        for r in items:
            if only and r.get("id") != only or not only and not r.get("enabled", True):
                continue
            star = rp.star_for(r["path"], devices, reps) or r.get("star") or ""
            for row in await self.db.fetchall(f"SELECT * FROM events WHERE seq > ? AND event IN ({marks}) ORDER BY seq",
                                              (int(r.get("seen_seq") or 0), *rp.FOUND_EVENTS)):
                r["seen_seq"] = row["seq"]
                changed = True
                ev = row_event(row)
                where = ev.get("star") or str(ev.get("location") or ev["payload"].get("location") or "").split("-")[0]
                if star and where and where != star:
                    continue
                r["finds"] = int(r.get("finds") or 0) + 1
                r["last_find"] = f"{rp.find_text(ev)} at {ev.get('location') or where or '?'}"
                if r.get("stop_on_find") and r.get("enabled", True):
                    r["enabled"] = False
                    await self.log("repeats", f"{r['name']}: stopped, found {r['last_find']}", notify=True)
            if not (only or rp.due(r)):
                continue
            r["star"] = star
            sub, cmd = rp.subject(r["path"]), (r.get("body") or {}).get("command") if isinstance(r.get("body"), dict) else None
            d = next((x for x in devices if x.get("device_code") == sub[1]), None) if sub and sub[0] == "device" and cmd else None
            if d and d.get("available_commands") is not None and cmd not in d["available_commands"]:
                r["last_result"] = f"waiting: {cmd} not available now ({d.get('status') or '?'})"   # sends once it is
                changed = True
                continue
            ok, _resp, err = await self.send(r["method"], r["path"], r.get("body"), f"repeat {r['name']}")
            r["runs"] = int(r.get("runs") or 0) + 1
            r["last_run"] = _now().isoformat(timespec="seconds")
            r["last_result"] = "ok" if ok else f"error: {err}"
            r["errors"] = 0 if ok else int(r.get("errors") or 0) + 1
            changed = True
            if r["errors"] >= rp.MAX_ERRORS and r.get("enabled", True):
                r["enabled"] = False
                await self.log("repeats", f"{r['name']}: stopped after {r['errors']} failed sends in a row ({err})",
                               "error", notify=True)
        if changed:
            await self.db.kv_set(rp.KV, items)

    async def print_needs(self, devices: list[dict]) -> dict[str, float]:
        """What waiting prints lack, per resource (prospects.print_short), and the system scans (kept a minute)."""
        from . import prospects as pr
        inp = await pr.rank_inputs(self.db, self, devices)
        return inp.get("short") or {}

    async def print_tilt(self, sched: dict, ctrl: dict, devices: list[dict]) -> dict:
        """A gather_evenly schedule on a mining controller turns into maintain_ratios leaning toward what waiting prints
        lack, among what its belt offers (owner 2026-10-22). Bootstrap controllers keep their own ratios."""
        from . import prospects as pr
        from .salvage import belt_of
        if sched.get("directive") != "gather_evenly" or "mining" not in (ctrl.get("device_type") or "") \
                or any(t.startswith("boot:") for t in ctrl.get("tags") or []):
            return sched
        row = await self.db.fetchone("SELECT data FROM systems WHERE star=?", (star_of(ctrl.get("location")),))
        offered = pr.belt_resources(json.loads(row["data"] or "{}") if row else {}, belt_of(ctrl.get("location")))
        ratios = pr.print_ratios(offered, await self.print_needs(devices))
        if not ratios:
            return sched
        # the game wants them under "ratios", not flat as its docs show (live 2026-10-25)
        return {**sched, "directive": "maintain_ratios", "configuration": {"ratios": ratios},
                "name": f"{sched.get('name') or 'gather_evenly'}, leaning to what prints wait on"}

    # --- helpers for rules ------------------------------------------------------------------------
    def busy_devices(self, jobs: list[dict]) -> set[str]:
        out = set()
        for j in jobs:
            if j["status"] in ("running", "waiting"):
                out.add(j.get("device"))
                out.update(j.get("meta", {}).get("devices", []))
        return out

    async def learn_no_attach(self, code: str) -> None:
        """The game refused to attach `code` as a large device: no carrier is offered attach points for its type again
        (fleets.NO_ATTACH_TYPES; kv no_attach_types {type: first seen})."""
        from . import fleets as fl
        t = next((d.get("device_type") for d in await self.db.kv_get("devices", []) or [] if d.get("device_code") == code), None)
        if not t or t in fl.NO_ATTACH_TYPES:
            return
        known = await self.db.kv_get("no_attach_types", {}) or {}
        known.setdefault(t, now_iso())
        await self.db.kv_set("no_attach_types", known)
        fl.NO_ATTACH_TYPES.add(t)
        await self.log("logistics", f"the game won't attach a {t} (large device): from now on it only rides in a hold")

    async def devices(self) -> list[dict]:
        if not getattr(self, "_no_attach_loaded", False):
            from . import fleets as fl, shapes
            fl.NO_ATTACH_TYPES.update(await self.db.kv_get("no_attach_types", {}) or {})
            fl.learn_stowable(shapes.normalize_blueprints(await self.db.kv_get("blueprints", [])))
            self._no_attach_loaded = True
        devs = await self.db.kv_get("devices", []) or []
        s = await self.db.kv_get("sim", {}) or {}
        if s.get("active"):   # a simulation's devices are its own (sim_engine): no other automation touches them
            from . import sim
            devs = [d for d in devs if not sim.is_sim_device(d, s)]
        return devs

    async def warded(self, devices: list[dict] | None = None) -> set[str]:
        """Systems another player has warded (wards.foreign): the mining rules leave them alone."""
        from . import wards
        return wards.foreign({"stars": await self.star_lookup("flagged")}, devices if devices is not None else await self.devices())

    async def star_lookup(self, name: str) -> Any:
        """Lookups over the star catalog, built once per catalog update and shared (don't change them): "pos"
        {star: position} for stars with one, "pos_all" (every star, {} without one), "pos_any" (every star, None without one), "by" {star: record}, "xyz" {star: (x, y, z)}, "names" (set), "slugs"
        (loadouts.star_slugs), "flagged" (records flagged has_ward / has_hub), "hubs" (set of stars flagged has_hub)."""
        from . import loadouts as lo
        from . import pathing as pa

        def recs(cat):
            return [x for x in (cat or {}).get("stars") or [] if isinstance(x, dict)]
        fns = {"pos": lambda c: {x.get("designation"): x["position"] for x in recs(c) if x.get("position")},
               "pos_any": lambda c: {x.get("designation"): x.get("position") for x in recs(c)},
               "pos_all": lambda c: {x.get("designation"): x.get("position") or {} for x in recs(c)},
               "by": lambda c: {x.get("designation"): x for x in recs(c)},
               "xyz": lambda c: {x.get("designation"): pa.xyz(x["position"]) for x in recs(c) if x.get("position")},
               "names": lambda c: {x["designation"] for x in recs(c) if x.get("designation")},
               "slugs": lambda c: lo.star_slugs({x["designation"] for x in recs(c) if x.get("designation")}),
               "flagged": lambda c: [x for x in recs(c) if x.get("has_ward") or x.get("has_hub")],
               "hubs": lambda c: {x["designation"] for x in recs(c) if x.get("has_hub") and x.get("designation")}}
        return await self.db.kv_derived("stars", name, fns[name], {})

    async def replicant_for_host(self, device_code: str) -> tuple[str, dict] | None:
        reps = await self.db.kv_get("replicants", {}) or {}
        for code, r in reps.items():
            if r.get("hosted_device_code") == device_code:
                return code, r
        return None

    async def stowed_in(self, vessel: str) -> list[dict]:
        """Devices stowed in a vessel: from the vessel's detail, else the host replicant's detail."""
        items: list = []
        try:
            detail = await self.api.request("GET", f"/devices/{vessel}", background=True)
            items = (detail or {}).get("stowed_devices") or []
        except ApiError:
            pass
        if not items:
            rep = await self.replicant_for_host(vessel)
            if rep:
                try:
                    detail = await self.api.request("GET", f"/replicants/{rep[0]}", background=True)
                    items = (detail or {}).get("stowed_devices") or []
                except ApiError:
                    items = rep[1].get("stowed_devices") or []
        return [i for i in items if isinstance(i, dict) and i.get("device_code")]

    async def system_scan(self, star: str) -> dict | None:
        row = await self.db.fetchone("SELECT data FROM systems WHERE star=?", (star,))
        if row:
            return json.loads(row["data"])
        try:
            data = await self.api.request("GET", f"/locations/{star}", background=True)
        except ApiError:
            return None
        if isinstance(data, dict) and data.get("planets") is not None:
            await self.db.execute("INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                                  (star, json.dumps(data), now_iso()))
            return data
        return None

    # --- rules -------------------------------------------------------------------------------------------
    async def mark_system_surveyed(self, star: str, by: str | None = None) -> None:
        if not star:
            return
        done = await self.db.kv_get("ami_surveyed", {}) or {}
        done[star] = now_iso()
        await self.db.kv_set("ami_surveyed", done)
        scan = await self.system_scan(star)
        if scan:
            surveyed = await self.db.kv_get("surveyed", {}) or {}
            for t in survey_targets(scan, {}, True, True, 10_000):
                surveyed.setdefault(t["target"], now_iso())
            await self.db.kv_set("surveyed", surveyed)
        await self.log("auto_survey", f"{star} fully surveyed" + (f" by {by}" if by else ""))

    async def arrived_from_elsewhere(self, ev: dict, star: str) -> bool:
        """True when this arrival brought the device into `star` from another system (not an in-system hop)."""
        p = ev.get("payload") or {}
        if str(p.get("travel_type") or "").startswith("surge"):
            return True
        origin = p.get("origin")
        if not origin:
            row = await self.db.fetchone("SELECT payload FROM events WHERE device_code=? AND event='travel.departed' "
                                         "ORDER BY seq DESC LIMIT 1", (ev.get("device_code"),))
            origin = (json.loads(row["payload"] or "{}") if row else {}).get("origin")
        return bool(origin) and star_of(origin) != star

    async def on_arrival(self, ev: dict) -> None:
        vessel = ev.get("device_code")
        p = ev.get("payload") or {}
        dest = p.get("destination") or ev.get("location")
        star = star_of(dest) or ev.get("star")
        if not vessel or not star or p.get("recalling"):
            return
        if not await self.arrived_from_elsewhere(ev, star):
            return  # moving around inside a system never re-triggers the arrival rules
        s = await self.settings()
        enabled = {rid for rid, cfg in s["rules"].items() if cfg.get("enabled")}
        if not enabled:
            return
        if "scan_on_arrival" in enabled:
            await self.rule_scan_on_arrival(vessel, star)
        if "devices_on_arrival" in enabled:
            await self.rule_devices_on_arrival(vessel, star)
        if "census_on_arrival" in enabled:
            await self.rule_census_on_arrival(vessel, star)
        if "auto_survey" in enabled:
            stowed = await self.stowed_in(vessel)
            await self.rule_auto_survey(vessel, dest, star, stowed, s["rules"]["auto_survey"])

    async def rule_scan_on_arrival(self, vessel: str, star: str) -> None:
        rep = await self.replicant_for_host(vessel)
        if not rep:
            return
        from . import season
        from .ops_rules import can_scan
        host = next((d for d in await self.devices() if d.get("device_code") == vessel), None)
        if host is not None and not can_scan(host):
            return   # a matrix container or hub can't scan ("Device does not have system scan capability")
        row = await self.db.fetchone("SELECT updated_at FROM systems WHERE star=?", (star,))
        if row and not season.stale(row["updated_at"], await season.reset_at(self.db)):
            return   # scanned since the last season reset (which made every system unexplored again)
        if (await self.settings())["dry_run"]:
            await self.log("scan_on_arrival", f"[dry run] would scan {star} with {rep[1].get('name') or rep[0]}")
            return
        ok, resp, err = await self.send("POST", f"/replicants/{rep[0]}/scan", {}, f"auto: scan {star}")
        if ok and isinstance(resp, dict) and resp.get("planets") is not None:
            await self.db.execute("INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                                  (star, json.dumps(resp), now_iso()))
            await self.log("scan_on_arrival", f"scanned {star}: {len(resp.get('planets') or [])} planets")
            await self.note_entry_point(star, resp.get("entry_point"))
        else:
            await self.log("scan_on_arrival", f"scan of {star} failed: {err}", "alert")

    async def note_entry_point(self, star: str, entry: str | None) -> None:
        """A scan names the system's entry point: the catalog takes it (a census keeps an old one it lacks, and after a
        season reset the game names none until a scan)."""
        if not entry:
            return
        cat = await self.db.kv_get("stars", {}) or {}
        hit = next((x for x in cat.get("stars") or [] if isinstance(x, dict) and x.get("designation") == star), None)
        if hit is not None and (hit.get("entry_point") != entry or not hit.get("explored")):
            hit["entry_point"], hit["explored"] = entry, True
            await self.db.kv_set("stars", cat)

    async def rule_devices_on_arrival(self, vessel: str, star: str) -> None:
        from . import others, traffic as tr
        from .api import ApiError
        rep = await self.replicant_for_host(vessel)
        if not rep:
            return
        if (await self.settings())["dry_run"]:
            await self.log("devices_on_arrival", f"[dry run] would scan the devices in {star} with {rep[1].get('name') or rep[0]}")
            return
        mine = tr.my_codes(await self.db.kv_get("account", {}) or {}, await self.db.kv_get("replicants", {}) or {},
                           await self.db.kv_get("devices", []) or [])
        try:
            snap = await others.scan_into(self.db, self.api, rep[0], mine)
        except ApiError as e:
            await self.log("devices_on_arrival", f"device scan of {star} failed: {e.message}", "alert")
            return
        shops = snap.get("shops") or []
        await self.log("devices_on_arrival", f"scanned the devices in {snap.get('star') or star}: "
                       f"{len(snap.get('devices') or [])} fixed, {len(snap.get('drones') or [])} drones of others"
                       + (f", shops: {', '.join(shops)}" if shops else ""))
        if self.hub:
            self.hub.publish("state", "traffic")

    async def rule_census_on_arrival(self, vessel: str, star: str) -> None:
        from . import season
        from .census import can_census
        done = (await self.db.kv_get("census", {}) or {}).get(star)
        if done is not None and not season.stale((done or {}).get("at") if isinstance(done, dict) else None,
                                                 await season.reset_at(self.db)):
            return   # run from here since the last season reset (which left every system unexplored)
        d = next((x for x in await self.devices() if x.get("device_code") == vessel), None)
        if not d or not can_census(d):
            return
        await self.run_census(vessel, star)

    async def run_census(self, device: str, star: str, manual: bool = False) -> tuple[list[dict], str | None]:
        """`stellar_census` from `device` (in `star`): the stars around it are stored and merged into the catalogue.
        Follows extra pages (at most 5). Returns (stars, error)."""
        from . import census
        if (await self.settings())["dry_run"] and not manual:
            await self.log("census_on_arrival", f"[dry run] would run a stellar census in {star} with {device}")
            return [], None
        pages, err = [], None
        for n in range(1, 6):
            body = {"command": "stellar_census", **({"page": n} if n > 1 else {})}
            ok, resp, err = await self.send("POST", f"/devices/{device}", body,
                                            f"{'' if manual else 'auto: '}stellar census in {star}" + (f" (page {n})" if n > 1 else ""))
            if not ok or not isinstance(resp, dict):
                break
            pages.append(resp)
            err = None
            if n >= int(resp.get("total_pages") or 1):
                break
        if not pages:
            await self.log("census_on_arrival", f"stellar census in {star} with {device} failed: {err}", "alert")
            return [], err
        found = await census.record(self.db, star, device, pages)
        new = [s["designation"] for s in found if s.get("explored") is False]
        await self.log("census_on_arrival", f"stellar census in {star}: {len(found)} stars"
                       + (f", unexplored: {', '.join(new)}" if new else ", all explored"))   # no notification (Joe 2026-11-12)
        return found, err

    async def rule_auto_survey(self, vessel: str, vessel_loc: str, star: str, stowed: list[dict], cfg: dict) -> None:
        jobs = await self.jobs()
        if any(j["rule"] == "auto_survey" and j["status"] in ("running", "waiting") and j["meta"].get("star") == star
               for j in jobs):
            return
        if star in (await self.db.kv_get("ami_surveyed", {}) or {}):
            return  # an AMI survey_system already covered every body
        # a fleet's vessel and its passengers away from its station are the fleet's (live 2026-10-21: Surveyor 1's heaven
        # vessel flown into PHEVENIA had its survey controller and a drone deployed and sent off while the fleet boarded)
        by = {d.get("device_code"): d for d in await self.devices()}
        if _reserved(by.get(vessel) or {}):
            return
        stowed = [i for i in stowed if not _reserved({**(by.get(i.get("device_code")) or i), "location": vessel_loc})]
        for d in by.values():
            st_ = str(((d.get("ami_directive") or {}).get("_eval_state")) or "")
            if "survey" in (d.get("device_type") or "") and star_of(d.get("location")) == star and st_.startswith("no_targets"):
                await self.mark_system_surveyed(star, d["device_code"])  # the controller says there's nothing left to survey
                return
        started = (await self.db.kv_get("ami_survey_started", {}) or {}).get(star)
        if started and (_now() - (_ts(started) or _now())).total_seconds() < 12 * 3600:
            return  # an AMI survey of this system is (or was recently) under way; don't restart it on every arrival
        scan = await self.system_scan(star)
        if not scan:
            await self.log("auto_survey", f"no scan data for {star}; enable 'System scan on arrival' or scan manually")
            return
        surveyed = await self.db.kv_get("surveyed", {}) or {}
        targets = survey_targets(scan, surveyed, bool(cfg.get("include_moons")), bool(cfg.get("include_belts", True)),
                                 int(cfg.get("max_targets") or 20))
        if not targets:
            return
        busy = self.busy_devices(jobs)
        if cfg.get("use_ami", True) and await self.survey_with_ami(vessel, vessel_loc, star, stowed, cfg, busy):
            return
        drones = [(i["device_code"], True) for i in stowed
                  if "survey" in (i.get("device_type") or "") and i["device_code"] not in busy
                  and "controller" not in (i.get("device_type") or "")]
        if cfg.get("use_idle", True):
            have = {d for d, _ in drones}
            for d in await self.devices():
                if _reserved(d):
                    continue
                if ("survey" in (d.get("device_type") or "") and star_of(d.get("location")) == star
                        and str(d.get("status", "")).startswith("idle") and d["device_code"] not in busy | have):
                    drones.append((d["device_code"], False))
        if not drones:
            return
        # round-robin the targets over the drones
        per: dict[str, list[dict]] = {d: [] for d, _ in drones}
        for i, t in enumerate(targets):
            per[drones[i % len(drones)][0]].append(t)
        for drone, was_stowed in drones:
            mine = per[drone]
            if not mine:
                continue
            steps = []
            if was_stowed:
                steps.append(step(f"deploy {drone}", f"/devices/{drone}", {"command": "deploy"},
                                  wait=["device.deployed"], timeout=SHORT_TIMEOUT, critical=True))
            for t in mine:
                steps.append(step(f"{drone} → {t['target']}", f"/devices/{drone}",
                                  {"command": "travel", "destination": t["target"]}, wait=["travel.arrived"],
                                  match={"destination": t["target"]}))
                steps.append(step(f"{t['action']} {t['target']}", f"/devices/{drone}", {"command": t["action"]},
                                  wait=[f"{t['action']}.completed"], match={f"{t['action']}_target": t["target"]}))
            if cfg.get("return_and_stow", True) and was_stowed:
                steps.append(step(f"{drone} → back to {vessel_loc}", f"/devices/{drone}",
                                  {"command": "travel", "destination": vessel_loc}, wait=["travel.arrived"],
                                  match={"destination": vessel_loc}))
                steps.append(step(f"stow {drone} in {vessel}", f"/devices/{drone}",
                                  {"command": "stow", "target": vessel}, wait=["device.stowed"], timeout=SHORT_TIMEOUT))
            await self.create_job("auto_survey", f"survey {star} with {drone} ({len(mine)} bodies)", drone, steps,
                                  {"star": star, "targets": [t["target"] for t in mine]})

    async def survey_with_ami(self, vessel: str, vessel_loc: str, star: str, stowed: list[dict], cfg: dict,
                              busy: set) -> bool:
        """Survey via an AMI survey controller (carried or already in the system). True if a job was made."""
        devices = [d for d in await self.devices() if not _reserved(d)]
        carried_ctrl = next((i for i in stowed if "survey" in (i.get("device_type") or "") and "controller" in (i.get("device_type") or "")
                             and i["device_code"] not in busy), None)
        in_system = next((d for d in devices if "survey" in (d.get("device_type") or "") and "controller" in (d.get("device_type") or "")
                          and star_of(d.get("location")) == star and not str(d.get("status", "")).startswith("stowed")
                          and d["device_code"] not in busy), None)
        ctrl = carried_ctrl or in_system
        if not ctrl:
            return False
        code = ctrl["device_code"]
        steps = []
        if carried_ctrl:
            st = step(f"deploy survey controller {code}", f"/devices/{code}", {"command": "deploy"},
                      wait=["device.deployed"], timeout=SHORT_TIMEOUT, critical=True)
            st["wait_device"] = code
            steps.append(st)
        drones = [i["device_code"] for i in stowed if "survey_drone" in (i.get("device_type") or "") and i["device_code"] not in busy]
        for dcode in drones:
            st = step(f"deploy {dcode}", f"/devices/{dcode}", {"command": "deploy"}, wait=["device.deployed"], timeout=SHORT_TIMEOUT)
            st["wait_device"] = dcode
            steps.append(st)
        here = ctrl.get("location") if not carried_ctrl else vessel_loc
        if cfg.get("use_idle", True):   # idle belt surveyors here too: a survey controller adopts them (live 2026-10-24)
            drones += [d["device_code"] for d in devices if ("survey_drone" in (d.get("device_type") or "")
                                                            or d.get("device_type") == "belt_surveyor")
                       and d.get("location") == here
                       and str(d.get("status", "")).startswith("idle") and d["device_code"] not in busy | set(drones)]
        # a survey controller works from the belt (or the inner system when there's none): take it and its drones there
        from . import placement as pl
        entry = ((await self.star_lookup("by")).get(star) or {}).get("entry_point")
        geo = pl.geography(star, await self.devices(), await self.system_scan(star), entry)
        spot = pl.target("ami_survey_controller", geo) if not pl.ok("ami_survey_controller", here, geo) else None
        if spot and drones:
            movers = [code] + drones
            first = len(steps)
            for c in movers:
                steps.append(step(f"{c} → {spot}", f"/devices/{c}", {"command": "travel", "destination": spot},
                                  critical=c == code))
            for i, c in enumerate(movers):
                w = step(f"wait for {c} at {spot}", "", None, method="WAIT", wait=["travel.arrived"],
                         match={"destination": spot}, timeout=STEP_TIMEOUT)
                w["wait_device"], w["seq0_from"] = c, first + i
                steps.append(w)
        from .ami_schedule import managed_by
        already = [d for d, c in (await managed_by(self.db)).items() if c == code]
        if not drones and not already:
            return False  # a controller with no drones can't survey anything; let the drone path decide
        if drones:
            steps.append(step(f"{code}: adopt {len(drones)} survey drone(s)", f"/devices/{code}", {"command": "adopt", "devices": drones}))
        config = {"planets": "all", "moons": "all" if cfg.get("include_moons") else "none",
                  "recall": bool(cfg.get("return_and_stow", True))}
        steps.append(step(f"{code}: survey_system", f"/devices/{code}",
                          {"command": "set_directive", "directive": "survey_system", "configuration": config}, critical=True))
        steps.append(step(f"{code}: launch", f"/devices/{code}", {"command": "launch"}))
        job = await self.create_job("auto_survey", f"survey {star} with AMI {code} ({len(drones) + len(already)} drones)", code, steps,
                                    {"star": star, "devices": drones, "ami": True})
        if job:
            started = await self.db.kv_get("ami_survey_started", {}) or {}
            started[star] = now_iso()
            await self.db.kv_set("ami_survey_started", started)
        return True if job is not None or (await self.settings())["dry_run"] else False

    async def exhausted_places(self) -> list[str]:
        """Places the game told us are mined out ("Belt exhausted", or a controller's exhausted state), for 12 h."""
        ex = await self.db.kv_get("exhausted_places", {}) or {}
        now = _now()
        live = {k: v for k, v in ex.items() if (now - (_ts(v) or now)).total_seconds() < 12 * 3600}
        if len(live) != len(ex):
            await self.db.kv_set("exhausted_places", live)
        return list(live)
