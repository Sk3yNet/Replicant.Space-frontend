"""Contract and mining rules: contracts, site reopening, consolidation, viability, salvage and idle miners.

A part of AutomationEngine (automations.py), split out by area; the methods are unchanged."""
from __future__ import annotations

from .engine_base import *  # noqa: F401,F403


class MiningRulesMixin:
    async def rule_contracts(self, force: bool = False) -> list[str]:
        """See gameevents.py. Fulfill ready events; optionally deliver shortfalls / send a nearby replicant."""
        cfg = await self.rule_cfg("contracts")
        if not cfg:
            return []
        from . import gameevents as gev
        from .shapes import normalize_inventory
        state = await self.db.kv_get("contracts_state", {}) or {}
        last = _ts(state.get("_last"))
        if not force and last and (_now() - last).total_seconds() < 60 * max(1, int(cfg.get("every_minutes") or 5)):
            return []
        state["_last"] = now_iso()
        devices = await self.devices()
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        reps = await self.db.kv_get("replicants", {}) or {}
        settings = await self.db.kv_get("event_settings", {}) or {}
        tmpl = (settings.get("fulfil") or "").strip() or gev.DEFAULT_FULFIL
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        done = []
        from . import wards
        locked = wards.foreign(await self.star_lookup("flagged"), devices)   # wards and hubs lock contracts
        life = await gev.life_map(self.db)
        approvals = await gev.approvals_of(self.db, life)
        for des, e in (await gev.load(self.db)).items():
            if e["status"] != "open" or not e.get("location"):
                continue
            if star_of(e["location"]) in locked:
                continue   # another player's ward or hub: its species interaction lock stops us completing this event
            prog = gev.progress(e, inv, devices, reps)
            tried = state.get(des) or {}
            if prog["state"] == "ready" and cfg.get("auto_fulfil", True) \
                    and await gev.approved(self.db, e, life, approvals, devices):   # approved on the Contracts page
                t = _ts(tried.get("fulfil"))
                if t and (_now() - t).total_seconds() < 1800:
                    continue  # tried recently; let the event stream catch up (or the error be read)
                rep = prog["present"][0]["code"]
                filled = (tmpl.replace("{designation}", des).replace("{replicant}", rep).replace("{location}", e["location"])
                          .replace("{criteria}", (prog.get("best") or {}).get("name") or "default"))
                method, _, rest = filled.partition(" ")
                path, _, body = rest.partition(" ")
                job = await self.create_job("contracts", f"fulfill {e.get('title')} at {e['location']}", None,
                                            [step(f"fulfill {des}", path, json.loads(body) if body.strip() else None, method=method.upper())],
                                            {"event": des})
                tried["fulfil"] = now_iso()
                done.append(f"fulfill {des}" + ("" if job else " (dry run)"))
            elif prog["state"] == "deliver" and cfg.get("auto_deliver"):
                plan = gev.delivery_plan(e, prog, devices)
                ctrl = plan["controller"]
                t = _ts(tried.get("deliver"))
                if not ctrl or ctrl["device_code"] in busy or not plan["legs"] or (t and (_now() - t).total_seconds() < 3600):
                    continue
                leg = plan["legs"][0]  # one leg per check; the next check sends the next pile
                code = ctrl["device_code"]
                await self.create_job("contracts", f"deliver for {e.get('title')}: {leg['collect']} → {leg['deliver']}", code,
                                      [step(f"{code}: delivery", f"/devices/{code}",
                                            {"command": "set_directive", "directive": "delivery",
                                             "configuration": {"route": {"collect": leg["collect"], "deliver": leg["deliver"]},
                                                               "requirement": leg["requirement"]}}, critical=True),
                                       step(f"{code}: launch", f"/devices/{code}", {"command": "launch"})], {"event": des})
                tried["deliver"] = now_iso()
                done.append(f"deliver {des}")
            elif prog["state"] == "needs replicant" and cfg.get("send_replicant") and prog["nearby"]:
                t = _ts(tried.get("send"))
                if t and (_now() - t).total_seconds() < 3600:
                    continue
                rep = prog["nearby"][0]["code"]
                st = step(f"{rep} → {e['location']}", f"/replicants/{rep}/travel", {"destination": e["location"]},
                          wait=["travel.arrived"], match={"destination": e["location"]})
                await self.create_job("contracts", f"send {prog['nearby'][0]['name']} to {e['location']} for {e.get('title')}",
                                      None, [st], {"event": des})
                tried["send"] = now_iso()
                done.append(f"send {rep} → {e['location']}")
            state[des] = tried
        await self.db.kv_set("contracts_state", state)
        return done

    async def rule_reopen_sites(self) -> list[str]:
        """See sites.py."""
        cfg = await self.rule_cfg("reopen_sites")
        if not cfg:
            return []
        from . import sites
        from .ami_schedule import managed_by
        devices = [d for d in await self.devices() if not _reserved(d)]
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        managed = await managed_by(self.db)
        state = await self.db.kv_get("reopen_state", {}) or {}
        cooldown = timedelta(minutes=int(cfg.get("cooldown_minutes") or 30))
        per = max(1, int(cfg.get("drones_per_belt") or 2))
        now = _now()
        done: list[str] = []
        rested = [v["belt"] for v in (await self.db.kv_get("rested", {}) or {}).values() if v.get("belt")]
        for belt, why in sorted(sites.exhausted_belts(devices, await self.exhausted_places() + rested).items()):
            last = _ts(state.get(belt))
            if last and now - last < cooldown:
                continue
            if any(j["rule"] == "reopen_sites" and j["status"] in ("running", "waiting") and j.get("meta", {}).get("belt") == belt
                   for j in jobs):
                continue
            if any(j["rule"] == "reopen_sites" and j.get("meta", {}).get("belt") == belt
                   and (_ts(j.get("finished_at")) or now) > now - sites.FULL_WAIT
                   and any("Maximum active sites" in str(st.get("error") or "") for st in j.get("steps") or [])
                   for j in jobs):
                continue   # the belt holds the game's most sites (searches under way count): wait for them to come up
            star = star_of(belt)
            have = len(sites.survey_at(devices, belt))
            if have >= per:
                continue  # enough drones already opening/holding sites there
            from .loadouts import lifted
            ctrls = [d for d in devices if "survey" in (d.get("device_type") or "") and "controller" in (d.get("device_type") or "")
                     and star_of(d.get("location")) == star and d["device_code"] not in busy and d.get("in_control_range") is not False
                     and not lifted(d)]   # waiting for a carrier to its spot (its own trip was refused)
            # belt surveyors first (about 4x faster, owner 2026-10-24); a survey controller adopts them too
            idle = sites.surveyors_first([d for d in devices if sites.is_surveyor(d) and star_of(d.get("location")) == star
                                          and str(d.get("status") or "").startswith("idle") and d["device_code"] not in busy
                                          and d.get("in_control_range") is not False])
            ctrl = None
            if cfg.get("use_ami", True) and ctrls:
                def on_belt_search(c: dict) -> bool:
                    dv = c.get("ami_directive") or {}
                    return dv.get("name") == "belt_search" and c.get("location") == belt and not sites.paused(c) and \
                        not str(dv.get("_eval_state") or "").startswith(("no_targets", "idle", "done"))
                if any(on_belt_search(c) for c in ctrls):
                    continue  # already searching this belt
                ctrl = next((c for c in ctrls if c.get("location") == belt), ctrls[0])
            if ctrl:
                mine = [d for d, c in managed.items() if c == ctrl["device_code"]]
                adopt = [d["device_code"] for d in idle if d["device_code"] not in managed][:max(0, per - len(mine))]
                await self.create_job("reopen_sites", f"re-open sites at {belt} with {ctrl['device_code']} ({why})",
                                            ctrl["device_code"], sites.ami_steps(ctrl, belt, adopt), {"devices": adopt, "belt": belt})
                done.append(f"{ctrl['device_code']} → belt_search {belt}")
            else:
                free = [d for d in idle if d["device_code"] not in managed][:per - have]
                if not free:
                    await self.log("reopen_sites", f"{belt} is worked out but there's no idle survey drone in {star} to search it", "alert")
                    state[belt] = now.isoformat(timespec="seconds")
                    continue
                for d in free:
                    await self.create_job("reopen_sites", f"{d['device_code']}: search {belt} ({why})", d["device_code"],
                                          sites.drone_steps(d, belt), {"belt": belt})
                    done.append(f"{d['device_code']} → search {belt}")
            state[belt] = now.isoformat(timespec="seconds")
        # a survey controller the game paused (the Season 4 time skip paused AMI directives: live 2026-11-10, FENIRON,
        # SOLARES, ANLOREN and ISOLITHOR held 20 idle surveyors on a stale belt_capacity state) is launched again when
        # its belt has room for another site
        for c in devices:
            belt = c.get("location") or ""
            if not ("survey" in (c.get("device_type") or "") and "controller" in (c.get("device_type") or "")
                    and "BELT" in belt and sites.paused(c)) or c["device_code"] in busy or c.get("in_control_range") is False:
                continue
            key = f"resume:{belt}"
            last = _ts(state.get(key))
            if last and now - last < cooldown or any(j["rule"] == "reopen_sites" and j["status"] in ("running", "waiting")
                                                     and j.get("meta", {}).get("belt") == belt for j in jobs):
                continue
            searching = sum(1 for d in devices if d.get("location") == belt and str(d.get("status") or "").startswith("searching"))
            if sites.MAX_SITES - await self.open_sites_now(belt) - searching <= 0:
                continue
            await self.create_job("reopen_sites", f"re-open sites at {belt} with {c['device_code']} (its search was paused)",
                                  c["device_code"], sites.ami_steps(c, belt, []), {"devices": [], "belt": belt})
            done.append(f"{c['device_code']} → belt_search {belt} (resumed)")
            state[key] = now.isoformat(timespec="seconds")
        # idle survey drones at a belt with fewer open sites than the game allows search there too: the belt isn't
        # worked out, but its sites may lack what's needed (ISOLITHOR-BELT-1 2026-10-23: no conductive or rares at any)
        idle_at: dict[str, list[dict]] = defaultdict(list)
        for d in devices:
            if (sites.is_surveyor(d) and "BELT" in (d.get("location") or "")
                    and str(d.get("status") or "").startswith("idle") and d["device_code"] not in busy
                    and d["device_code"] not in managed and d.get("in_control_range") is not False):
                idle_at[d["location"]].append(d)
        theirs = await self.warded(devices) if idle_at else set()
        for belt, idle in sorted(idle_at.items()):
            key = f"cap:{belt}"
            last = _ts(state.get(key))
            if last and now - last < cooldown or star_of(belt) in theirs:
                continue
            if any(j["rule"] == "reopen_sites" and j.get("meta", {}).get("belt") == belt
                   and (j["status"] in ("running", "waiting") or (_ts(j.get("finished_at")) or now) > now - sites.FULL_WAIT
                        and any("Maximum active sites" in str(st.get("error") or "") for st in j.get("steps") or []))
                   for j in jobs):
                continue
            searching = sum(1 for d in devices if d.get("location") == belt and str(d.get("status") or "").startswith("searching"))
            room = sites.MAX_SITES - await self.open_sites_now(belt) - searching
            for d in sites.surveyors_first(idle)[:max(0, room)]:
                await self.create_job("reopen_sites", f"{d['device_code']}: search {belt} (fewer than {sites.MAX_SITES} open sites)",
                                      d["device_code"], sites.drone_steps(d, belt), {"belt": belt})
                done.append(f"{d['device_code']} → search {belt}")
            state[key] = now.isoformat(timespec="seconds")
        await self.db.kv_set("reopen_state", state)
        return done

    async def belt_open_sites(self, belts: list[str], max_age_minutes: int = 10) -> dict[str, int]:
        """Open-site counts for belts, re-reading each belt's detail if our copy is older than max_age_minutes."""
        from .salvage import open_site_count
        reads = await self.db.kv_get("belt_reads", {}) or {}
        now = _now()
        out: dict[str, int] = {}
        for b in dict.fromkeys(belts):
            t = _ts(reads.get(b))
            detail = await self.db.kv_get(f"loc:{b}", None)
            if detail is None or not t or now - t > timedelta(minutes=max_age_minutes):
                try:
                    detail = await self.api.request("GET", f"/locations/{b}", background=True)
                    await self.db.kv_set(f"loc:{b}", detail or {})
                    reads[b] = now.isoformat(timespec="seconds")
                except ApiError:
                    pass
            out[b] = open_site_count(detail, await self.depleted_since(b, reads.get(b)))
        await self.db.kv_set("belt_reads", reads)
        return out

    async def depleted_since(self, belt: str, since: str | None) -> set[str]:
        """Sites at `belt` a site.depleted event closed after `since` (when we last read the belt). Seen live
        (2026-10-08, LORSELAN): eight drones mined out five small sites in eight minutes; a ten-minute-old read still
        listed all five, and the controller was relaunched onto nothing — and overheated."""
        if not since:
            return set()
        rows = await self.db.fetchall("SELECT payload FROM events WHERE event='site.depleted' AND received_at >= ? "
                                      "AND payload LIKE ?", (since, f'%"{belt}-%'))
        out = set()
        for r in rows:
            try:
                site = (json.loads(r["payload"] or "{}") or {}).get("site")
            except ValueError:
                continue
            if site and str(site).startswith(belt + "-"):
                out.add(site)
        return out

    async def open_sites_now(self, belt: str) -> int:
        """Open sites at a belt from our last read, less what has been mined out since (no request)."""
        from .salvage import open_site_count
        reads = await self.db.kv_get("belt_reads", {}) or {}
        return open_site_count(await self.db.kv_get(f"loc:{belt}", None), await self.depleted_since(belt, reads.get(belt)))

    async def refresh_known_belts(self, every_minutes: int = 20, max_belts: int = 8) -> list[str]:
        """Keep the belts your devices are at current (open sites appear and close all the time), so closed or used-up
        sites drop off the System page and map without a manual refresh. One GET per belt, at most every 20 min."""
        from .salvage import belt_of
        last = _ts(await self.db.kv_get("belts_refreshed_at", None))
        if last and _now() - last < timedelta(minutes=every_minutes):
            return []
        belts = sorted({b for d in await self.devices() for b in [belt_of(d.get("location"))] if b})[:max_belts]
        await self.db.kv_set("belts_refreshed_at", _now().isoformat(timespec="seconds"))
        if belts:
            await self.belt_open_sites(belts, max_age_minutes=every_minutes)
        return belts

    async def consolidate_plan(self, cfg: dict | None = None) -> list[dict]:
        from . import consolidate as co
        from . import gameevents as gev
        from .shapes import normalize_blueprints, normalize_inventory
        cfg = cfg or (await self.settings())["rules"].get("consolidate") or {}
        devices = await self.devices()
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        inv = await self.safe_piles(inv)   # piles at overdue rocks are left (2026-11-08)
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        protected = {e.get("location") for e in (await gev.load(self.db)).values() if e.get("status") == "open" and e.get("location")}
        protected |= {d.get("location") for d in devices if d.get("device_type") == "system_hub" and d.get("location")}   # its upkeep
        handed = set(await self.db.kv_get("logistics_ami", {}) or {})   # given a delivery by the logistics service: busy
        return co.plan(devices, inv, bps, self.busy_devices(await self.jobs()) | handed, protected, float(cfg.get("min_amount") or 100))

    async def rule_consolidate(self, force: bool = False) -> list[str]:
        from . import consolidate as co
        cfg = await self.rule_cfg("consolidate")
        if not cfg and not force:
            return []
        cfg = cfg or (await self.settings())["rules"].get("consolidate") or {}
        last = _ts(await self.db.kv_get("consolidate_last", None))
        if not force and last and _now() - last < timedelta(minutes=int(cfg.get("every_minutes") or 20)):
            return []
        await self.db.kv_set("consolidate_last", _now().isoformat(timespec="seconds"))
        done = []
        for p in await self.consolidate_plan(cfg):
            if not p.get("controller"):
                continue
            job = await self.create_job("consolidate", f"consolidate: {p['collect']} → {p['deliver']} ({p['total']} units)",
                                        p["controller"], co.steps(p), {"devices": [], "star": p["star"]}, force=force)
            done.append(co.describe(p) + ("" if job else " (dry run)"))
        return done

    async def viability_report(self) -> list[dict]:
        from . import viability as via
        state = await self.db.kv_get("viability", {}) or {}
        stars = await self.star_lookup("by")   # shared: read only
        known: dict[str, int] = {}
        for r in await self.db.fetchall("SELECT key, value FROM kv WHERE key LIKE 'loc:%-BELT-%'"):
            b = via.belt_of(r["key"][4:])
            if b and b == r["key"][4:]:
                idx = [x.get("site_index") for x in (json.loads(r["value"]) or {}).get("resource_sites") or []
                       if isinstance(x, dict) and x.get("site_index") is not None]
                known[b] = max(idx) if idx else 0
        cfg = (await self.settings())["rules"].get("belt_viability") or {}
        rows = via.report(state, stars, (cfg.get("move_at_percent") or 250) / 100, known)
        from . import others as oth   # other players' mining drones on the belt at the last scan of the system
        snaps = oth.normalize(await self.db.kv_get(oth.KV, {}))["stars"]
        for r in rows:
            r["theirs"] = oth.drones_at(snaps.get(r.get("star")), r.get("belt") or "")
        return rows

    async def track_viability(self) -> list[str]:
        """Fold this pass's device list and freshly read belt details into the viability record (no API calls);
        alert once when a belt crosses into 'consider moving'."""
        from . import viability as via
        state = await self.db.kv_get("viability", {}) or {}
        reads = await self.db.kv_get("belt_reads", {}) or {}
        last = state.get("last_obs") or ""
        details = {}
        for b, at in reads.items():
            if at and at > last:
                details[b] = await self.db.kv_get(f"loc:{b}", None)
        now = _now().isoformat(timespec="seconds")
        state, notes = via.observe(state, await self.devices(), details, now)
        state["last_obs"] = now
        alerts: list[str] = []
        cfg = await self.rule_cfg("belt_viability")
        if cfg:
            sent = state.setdefault("alerted", {})
            for r in await self._viability_rows(state):
                if r["verdict"] == "consider moving" and sent.get(r["belt"]) != "consider moving":
                    text = via.alert_text(r)
                    await self.log("belt_viability", text, "alert", notify=True)
                    alerts.append(text)
                sent[r["belt"]] = r["verdict"]
        await self.db.kv_set("viability", state)
        return alerts

    async def _viability_rows(self, state: dict) -> list[dict]:
        await self.db.kv_set("viability", state)   # report() reads the stored state
        return await self.viability_report()

    async def known_open_sites(self, max_age_hours: float = 2) -> dict[str, int]:
        """star → open mining sites on its belts, from belt details read in the last couple of hours (stars whose belts
        weren't read recently are left out = unknown)."""
        from .salvage import open_site_count
        reads = await self.db.kv_get("belt_reads", {}) or {}
        out: dict[str, int] = {}
        for b, at in reads.items():
            t = _ts(at)
            if not t or (_now() - t).total_seconds() > max_age_hours * 3600:
                continue
            out[star_of(b)] = out.get(star_of(b), 0) + open_site_count(await self.db.kv_get(f"loc:{b}", None),
                                                                       await self.depleted_since(b, at))
        return out

    async def system_belts(self, stars: set[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for star in stars:
            row = await self.db.fetchone("SELECT data FROM systems WHERE star=?", (star,))
            scan = json.loads(row["data"]) if row else {}
            out[star] = [b["designation"] for b in ((scan.get("asteroid_belt") or {}).get("belts")) or [] if b.get("designation")]
        return out

    async def rule_back_to_belt(self, cfg: dict) -> list[str]:
        """Mining controllers stuck 'exhausted' at a body (drones left at used-up salvage), with a stale 'exhausted' at a
        belt that has re-opened, paused, or done with salvage — while a belt in their system has open sites: bring the
        drones back to the belt, re-adopt them, re-set the directive and launch."""
        from . import salvage as sv
        from .ami_schedule import is_controller, kind_of, managed_by, targets_of
        devices = await self.devices()
        ctrls = [d for d in devices if is_controller(d) and kind_of(d.get("device_type")) == "mining" and not _reserved(d)]
        def candidate(c: dict) -> bool:
            dv = c.get("ami_directive") if isinstance(c.get("ami_directive"), dict) else {}
            st = str(dv.get("_eval_state") or "")
            return (st.startswith("exhausted") or str(c.get("ami_directive_status") or "") == "paused"
                    or (dv.get("name") == "gather_salvage" and st.startswith(sv.FINISHED_STATES)) or sv.parked(c))
        mgd = await managed_by(self.db)
        warded = await self.warded(devices)
        rested = await self.db.kv_get("rested", {}) or {}
        cands = [c for c in ctrls if (candidate(c) or sv.scattered(c, devices, mgd) or c.get("device_code") in rested)
                 and star_of(c.get("location")) not in warded]
        if not cands:
            return []
        stars = {star_of(c.get("location")) for c in cands}
        sysb = await self.system_belts(stars)
        for c in cands:  # the controller's own belt counts even if the scan doesn't list it
            b = sv.belt_of(c.get("location"))
            if b and b not in sysb.setdefault(star_of(c.get("location")), []):
                sysb[star_of(c.get("location"))].append(b)
        open_sites = await self.belt_open_sites([b for bl in sysb.values() for b in bl])
        state = await self.db.kv_get("salvage_state", {}) or {}
        back = state.setdefault("back", {})
        cool = timedelta(minutes=int(cfg.get("back_cooldown_minutes") or 30))
        now = _now()
        skip = self.busy_devices(await self.jobs()) | {k for k, v in back.items() if _ts(v) and now - _ts(v) < cool}
        directive_for = {k: v.get("directive") for k, v in rested.items() if v.get("directive")}   # what it ran before
        for sched in await self.schedules():   # use the directive an AMI schedule gives that controller, if any
            if sched.get("enabled", True):
                for c in targets_of(sched, devices):
                    directive_for.setdefault(c["device_code"], sched["directive"])
        done = []
        busy = self.busy_devices(await self.jobs())
        free = {d["device_code"] for d in devices if not _reserved(d) and d["device_code"] not in busy}
        for p in sv.back_to_belt_plan(cands, devices, mgd, open_sites, sysb, skip, directive_for, free, set(rested)):
            r = rested.get(p["ctrl"]) or {}
            if r.get("directive") == p["directive"] and r.get("config"):
                p["config"] = r["config"]   # the configuration it had before resting (e.g. gather_resources amounts)
            n = len(p["away"]) + len(p["strays"])
            job = await self.create_job("salvage_when_depleted",
                                        f"{p['ctrl']}: back to {p['belt']} ({p['why']})"
                                        + (f", bringing {n} drone(s)" if n else ""),
                                        p["ctrl"], sv.back_to_belt_steps(p), {"devices": p["away"] + p["strays"] + p.get("gone", []), "belt": p["belt"]})
            back[p["ctrl"]] = now.isoformat(timespec="seconds")
            if job and rested.pop(p["ctrl"], None):
                await self.db.kv_set("rested", rested)
            done.append(f"{p['ctrl']} → {p['belt']}" + (" (planned, dry run)" if not job else ""))
        await self.db.kv_set("salvage_state", {**(await self.db.kv_get("salvage_state", {}) or {}), "back": back})
        return done

    async def rest_dry_controllers(self) -> list[str]:
        """A mining controller whose directive is exhausted at its belt while that belt has no open sites (the survey
        drones are still searching) and no salvage took it: clear its directive so it stops coordinating drones with
        nothing to mine — seen live (2026-10-08, LORSELAN) to log ami_overheat every 20 s, heat 2.0, and lose ~9 %
        capacity an hour. Its drones stay at the belt. kv "rested" remembers the directive; back to the belt relaunches
        it once the belt has open sites, and AMI schedules leave it alone until then."""
        cfg = await self.rule_cfg("salvage_when_depleted")
        if not cfg or not cfg.get("rest_when_dry", True):
            return []
        from . import salvage as sv
        from .ami_schedule import is_controller, kind_of
        devices = await self.devices()
        busy = self.busy_devices(await self.jobs())
        rested = await self.db.kv_get("rested", {}) or {}
        out = []
        for c in devices:
            code = c.get("device_code")
            if not (is_controller(c) and kind_of(c.get("device_type")) == "mining") or _reserved(c) or code in busy \
                    or code in rested or c.get("in_control_range") is False:
                continue
            dv = c.get("ami_directive") if isinstance(c.get("ami_directive"), dict) else {}
            state = str(dv.get("_eval_state") or "")
            if not dv.get("name") or dv.get("name") == "gather_salvage" or not state.startswith("exhausted"):
                continue
            place = sv.exhausted_place(state)
            belt = sv.belt_of(place)
            if not belt or belt != place or (await self.belt_open_sites([belt]))[belt] > 0:
                continue   # exhausted at a body (back to the belt handles that), or the belt has sites: not dry
            job = await self.create_job("salvage_when_depleted", f"{code}: rest at {belt} — no open sites, the survey "
                                        "drones are searching (an exhausted controller overheats)", code,
                                        [step(f"{code}: clear_directive", f"/devices/{code}", {"command": "clear_directive"},
                                              critical=True)], {"devices": [], "belt": belt})
            if job:
                rested[code] = {"belt": belt, "directive": dv.get("name"), "config": dv.get("config") or {}, "at": now_iso()}
                out.append(f"{code} rests at {belt}")
        # forget a rest the player ended (a directive set by hand) or a controller that has gone
        by = {d.get("device_code"): d for d in devices}
        for code in list(rested):
            d = by.get(code)
            if d is None or ((d.get("ami_directive") or {}).get("name") and code not in busy
                             and not str((d.get("ami_directive") or {}).get("_eval_state") or "").startswith("exhausted")):
                rested.pop(code)
        await self.db.kv_set("rested", rested)
        return out

    async def rule_salvage(self) -> list[str]:
        """See salvage.py. Returns what it did (for the log / tests)."""
        cfg = await self.rule_cfg("salvage_when_depleted")
        if not cfg:
            return []
        back_done: list[str] = []
        if cfg.get("back_to_belt", True):
            back_done = await self.rule_back_to_belt(cfg)
        from . import salvage as sv
        from .ami_schedule import adoptable, controller_idle, is_controller, kind_of, managed_by
        from .targets import system_resources
        devices = await self.devices()
        busy = self.busy_devices(await self.jobs())
        managed = await managed_by(self.db)
        state = await self.db.kv_get("salvage_state", {}) or {}
        cool, assigned = state.setdefault("cool", {}), state.setdefault("assigned", {})
        cooldown = timedelta(minutes=int(cfg.get("cooldown_minutes") or 10))
        now = _now()

        def cooling(code: str) -> bool:
            t = _ts(cool.get(code))
            return bool(t and now - t < cooldown)

        # print components and devices pinned (at:) somewhere stay where they are (live 2026-11-12: two component rigs
        # pinned to FALQUORYX-BELT-1 were sent to salvage at FALQUORYX-2-2)
        miners = [d for d in devices if (d.get("device_type") in ("mining_drone", "mining_rig") or
                  (is_controller(d) and kind_of(d.get("device_type")) == "mining"))
                  and not _reserved(d) and not any(t == "component" or t.startswith("at:") for t in d.get("tags") or [])]
        sgone = await self.db.kv_get("salvage_gone", {}) or {}
        done: list[str] = []
        nothing = state.setdefault("nothing", {})   # star -> when we last said there's nothing to mine there
        warded = await self.warded(devices)
        for star in sorted({star_of(d.get("location")) for d in miners} - warded):
            res = await system_resources(self.db, star)
            scanned = await self.db.fetchone("SELECT 1 FROM systems WHERE star=?", (star,))
            no_belt = bool(scanned) and not (await self.system_belts({star}))[star]   # scanned: no belt to mine
            dry = sv.worked_out(res) | {p for p in await self.exhausted_places() if star_of(p) == star}
            sal = [x for x in sv.available_salvage(res) if sv.main_resource(x, sgone.get(sv.body_of(x["code"]))) is not None]
            dead_sal = {x["code"] for x in res.get("salvage") or [] if x.get("depleted")
                        or sv.main_resource(x, sgone.get(sv.body_of(x["code"]))) is None}
            # a salvage a controller here reports finished ("depleted:recalling") is used up too, whatever our copy of the
            # system says (live 2026-11-10: 4 mining fleets sat paused at used-up salvage for a week, "on salvage")
            spent = {str(((d.get("ami_directive") or {}).get("config") or {}).get("location") or "")
                     for d in miners if is_controller(d) and star_of(d.get("location")) == star
                     and (d.get("ami_directive") or {}).get("name") == "gather_salvage"
                     and str((d.get("ami_directive") or {}).get("_eval_state") or "").startswith(sv.FINISHED_STATES)} - {""}
            if spent:
                dead_sal |= {x["code"] for x in res.get("salvage") or [] if x.get("code") in spent or sv.body_of(x.get("code")) in spent}
                sal = [x for x in sal if x["code"] not in dead_sal]
            for k in [k for k, v in assigned.items() if k in dead_sal]:
                assigned.pop(k)
            ctrls = [d for d in miners if is_controller(d) and star_of(d.get("location")) == star]
            # the controller's own report: "_eval_state": "exhausted:[...]:<place>" = nothing left to mine there
            exhausted = {c["device_code"] for c in ctrls
                         if str(((c.get("ami_directive") or {}).get("_eval_state")) or "").startswith("exhausted")}
            if no_belt and not sal:
                t = _ts(nothing.get(star))
                if not t or now - t > timedelta(hours=24):
                    nothing[star] = now.isoformat(timespec="seconds")
                    await self.log("salvage_when_depleted", f"nothing to mine in {star}: no asteroid belt and no salvage we know "
                                   "of — move its miners, or survey the bodies for salvage", "error", notify=True)
                continue
            if (not dry and not exhausted and not no_belt) or not sal:
                continue
            if ctrls and cfg.get("use_ami", True):
                reopening = await self.rule_cfg("reopen_sites")
                survey_here = any("survey" in (d.get("device_type") or "") and star_of(d.get("location")) == star for d in devices)
                for c in ctrls:
                    code = c["device_code"]
                    if code in busy or cooling(code):
                        continue
                    if reopening and survey_here and not no_belt:
                        continue  # sites are being re-opened by survey drones; keep mining rather than switch to salvage
                    row = await self.db.fetchone("SELECT event, payload FROM events WHERE device_code=? AND event LIKE 'directive.%' "
                                                 "ORDER BY seq DESC LIMIT 1", (code,))
                    cur = json.loads(row["payload"] or "{}") if row else {}
                    on_salvage = (row and row["event"] == "directive.set" and cur.get("directive") == "gather_salvage"
                                  and (cur.get("configuration") or {}).get("location") not in dead_sal | {sv.body_of(c) for c in dead_sal} | spent)
                    if on_salvage:
                        continue
                    idle, _ = await controller_idle(self.db, c)
                    if not (code in exhausted or sv.at_worked_out_place(c.get("location"), dry, dead_sal) or idle or no_belt):
                        continue
                    target = next((x for x in sal if assigned.get(x["code"]) in (None, code)), None)
                    if not target:
                        break
                    adopt = adoptable(devices, c, managed)
                    job = await self.create_job("salvage_when_depleted", f"{code}: salvage {target['code']} (sites in {star} depleted)",
                                                code, sv.ami_steps(code, target["code"], bool(cfg.get("recall")), adopt),
                                                {"devices": adopt, "salvage": target["code"]})
                    cool[code] = now.isoformat(timespec="seconds")
                    if job:
                        assigned[target["code"]] = code
                    done.append(f"{code} → gather_salvage {target['code']}")
                continue  # the AMI handles this system; drones are left to it
            per = int(cfg.get("drones_per_salvage") or 0)
            idle_drones = sorted((d for d in miners if d.get("device_type") == "mining_drone" and star_of(d.get("location")) == star
                                  and str(d.get("status")) == "idle" and d["device_code"] not in busy
                                  and d["device_code"] not in managed and not cooling(d["device_code"])
                                  and (no_belt or sv.at_worked_out_place(d.get("location"), dry, dead_sal))),
                                 key=lambda d: d["device_code"])
            counts: dict[str, int] = defaultdict(int)
            for d in miners:  # drones already at (or heading for) a salvage count toward its share
                for x in sal:
                    if d.get("location") in (x["code"], sv.body_of(x["code"])) and str(d.get("status")) != "idle":
                        counts[x["code"]] += 1
            for j in await self.jobs():
                if j["rule"] == "salvage_when_depleted" and j["status"] in ("running", "waiting") and j.get("meta", {}).get("salvage"):
                    counts[j["meta"]["salvage"]] += 1
            i = 0
            for d in idle_drones:
                while i < len(sal) and per and counts[sal[i]["code"]] >= per:
                    i += 1
                if i >= len(sal):
                    break
                target = sal[i]
                code = d["device_code"]
                await self.create_job("salvage_when_depleted", f"{code}: salvage {target['code']} (sites in {star} depleted)", code,
                                      sv.drone_steps(code, d.get("location"), target, sgone.get(sv.body_of(target["code"]))), {"salvage": target["code"]})
                cool[code] = now.isoformat(timespec="seconds")
                counts[target["code"]] += 1
                done.append(f"{code} → {target['code']}")
        prev = await self.db.kv_get("salvage_state", {}) or {}
        state["back"] = prev.get("back", state.get("back", {}))   # written by rule_back_to_belt this pass
        state["nothing"] = nothing
        await self.db.kv_set("salvage_state", state)
        return back_done + done

    @staticmethod
    def _fed_stars(d: dict, fleets: list[dict]) -> set[str]:
        """The systems a drone's mining feeds: its own, and the home of the fleet its fleet sends materials to."""
        from . import fleets as fl
        out = {star_of(d.get("location"))}
        tags = set(d.get("tags") or [])
        for f in fleets:
            if fl.fleet_tag(f["id"]) in tags:
                to = fl.materials_target(f, fleets)
                if to and to.get("home"):
                    out.add(to["home"])
        return out

    async def rule_restart_idle_miners(self) -> None:
        cfg = await self.rule_cfg("restart_idle_miners")
        if not cfg:
            return
        cooldown = timedelta(minutes=int(cfg.get("cooldown_minutes") or 10))
        attempts = await self.db.kv_get("miner_restarts", {}) or {}
        busy = self.busy_devices(await self.jobs())
        now = _now()
        dry = (await self.settings())["dry_run"]
        from .ami_schedule import controller_idle, is_controller, kind_of, managed_by
        managed = await managed_by(self.db)
        devices = await self.devices()
        handed: dict[str, list[str]] = {}
        dry_belts: set[str] = set(await self.exhausted_places())
        if await self.rule_cfg("salvage_when_depleted"):
            from .salvage import worked_out
            from .targets import system_resources
            for star in {star_of(d.get("location")) for d in devices if d.get("device_type") == "mining_drone"}:
                dry_belts |= worked_out(await system_resources(self.db, star))
        warded = await self.warded(devices)
        from .bootstrap import no_site, waiting_short
        from .salvage import site_resources
        nosite = await self.db.kv_get("miner_no_site", {}) or {}   # {belt: {resource: at}}, like bootstrap.no_site
        reads = await self.db.kv_get("belt_reads", {}) or {}
        site_res: dict[str, set[str] | None] = {}
        fleets = await self.fleets()
        for d in devices:
            code = d.get("device_code")
            if _reserved(d):
                continue  # a fleet's drones are run by the fleet
            if star_of(d.get("location")) in warded:
                continue  # another player's ward or hub: nothing of ours can mine here
            if (d.get("device_type") not in ("mining_drone", "mining_rig") or str(d.get("status")) != "idle"
                    or "BELT" not in (d.get("location") or "") or code in busy or code in managed
                    or "start_mining" not in (d.get("available_commands") or ["start_mining"])):
                continue
            from . import prospects as pr_
            from .salvage import belt_of
            if belt_of(d.get("location")) in dry_belts:
                continue  # nothing left to mine there: the salvage rule moves it
            last = _ts(attempts.get(code))
            if last and now - last < cooldown:
                continue
            # the drone's own fleet's controller (another fleet's would take it over: live 2026-10-27, Miner_8's
            # drones ran under Miner_9's controller at SOLARES-BELT-1), else a fleetless one
            from .ami_schedule import fleet_of as ctrl_fleet, same_side
            ctrl = min((c for c in devices if is_controller(c) and kind_of(c.get("device_type")) == "mining"
                        and c.get("location") == d.get("location") and same_side(c, d)),
                       key=lambda c: (ctrl_fleet(c) != ctrl_fleet(d), c["device_code"]), default=None) \
                if cfg.get("prefer_ami", True) else None
            if ctrl:
                handed.setdefault(ctrl["device_code"], []).append(code)
                attempts[code] = now.isoformat(timespec="seconds")
                continue
            resource = cfg.get("resource") or "same"
            belt = belt_of(d.get("location")) or d.get("location")
            skip = no_site({"no_site": nosite}, belt, now)
            if belt not in site_res:   # what the open sites hold (our last read, less sites mined out since)
                site_res[belt] = site_resources(await self.db.kv_get(f"loc:{belt}", None),
                                                await self.depleted_since(belt, reads.get(belt)))
            have = site_res[belt]
            if resource == "same":
                row = await self.db.fetchone(
                    "SELECT payload FROM events WHERE device_code=? AND event IN ('mining.started','mining.stopped','mining.retargeted') "
                    "ORDER BY seq DESC LIMIT 1", (code,))
                pl = json.loads(row["payload"]) if row else {}
                resource = pl.get("resource_type") or pl.get("new_resource") or "structural"
                # what waiting prints lack, if its belt has it (owner 2026-10-22) — first what the factories this belt
                # feeds wait for (its own system's, or the system its fleet sends materials to), else any waiting print
                srow = await self.db.fetchone("SELECT data FROM systems WHERE star=?", (star_of(d.get("location")),))
                offered = pr_.belt_resources(json.loads(srow["data"] or "{}") if srow else {}, belt_of(d.get("location")))
                offered = {r for r in (offered if have is None else have) if r not in skip}
                local = waiting_short(devices, self._fed_stars(d, fleets))
                short = {r: q for r, q in local.items() if r in offered} or \
                    {r: q for r, q in (await self.print_needs(devices)).items() if q > 0 and r in offered}
                if short:
                    resource = max(short, key=lambda r: (short[r], r == resource, r))
            if resource in skip or (have is not None and resource not in have):
                # no open site has it (the game said so lately, or our read of the sites): mine something else the
                # open sites have, or wait for the survey drones
                srow = await self.db.fetchone("SELECT data FROM systems WHERE star=?", (star_of(d.get("location")),))
                left = sorted(r for r in (have if have is not None else
                                          pr_.belt_resources(json.loads(srow["data"] or "{}") if srow else {}, belt))
                              if r not in skip)
                if not left:
                    continue
                resource = left[0]
            attempts[code] = now.isoformat(timespec="seconds")
            site = None
            if cfg.get("focus_sites"):   # one site at a time, so sites run out in turn (owner 2026-10-25)
                from .sites import focus_site
                site = focus_site(await self.db.kv_get(f"loc:{belt}", None), resource,
                                  await self.depleted_since(belt, reads.get(belt)))
            on = f"{resource}" + (f" at {site}" if site else "")
            if dry:
                await self.log("restart_idle_miners", f"[dry run] would restart {code} on {on}")
                continue
            body = {"command": "start_mining", "resource_type": resource} | ({"target": site} if site else {})
            ok, _, err = await self.send("POST", f"/devices/{code}", body, f"auto: restart {code}")
            if not ok and site and not NO_SITE_RE.search(err or "") and "exhausted" not in (err or "").lower():
                # the site was refused (closed since our read, or the target not taken): any site will do
                on = resource
                ok, _, err = await self.send("POST", f"/devices/{code}", {"command": "start_mining", "resource_type": resource},
                                             f"auto: restart {code}")
            if not ok and "exhausted" in (err or "").lower():
                from .salvage import belt_of
                ex = await self.db.kv_get("exhausted_places", {}) or {}
                ex[belt_of(d.get("location")) or d.get("location")] = now_iso()
                await self.db.kv_set("exhausted_places", ex)
            quiet = not ok and bool(NO_SITE_RE.search(err or ""))
            if quiet:   # skipped 3 h at this belt (live 2026-10-23: 3 alerts at ISOLITHOR-BELT-1 for conductive)
                nosite.setdefault(belt, {})[resource] = now.isoformat(timespec="seconds")
                await self.db.kv_set("miner_no_site", nosite)
            await self.log("restart_idle_miners", f"restarted {code} on {on}" if ok else f"could not restart {code}: {err}",
                           "info" if ok or quiet else "alert")
        for ccode, drones in handed.items():
            ctrl = next(c for c in devices if c.get("device_code") == ccode)
            idle, _ = await controller_idle(self.db, ctrl)
            steps = [step(f"{ccode}: adopt idle {', '.join(drones)}", f"/devices/{ccode}", {"command": "adopt", "devices": drones})]
            if idle:
                steps.append(step(f"{ccode}: launch", f"/devices/{ccode}", {"command": "launch"}))
            await self.create_job("restart_idle_miners", f"hand {len(drones)} idle miner(s) to {ccode}", ccode, steps,
                                  {"devices": drones})
        await self.db.kv_set("miner_restarts", attempts)
