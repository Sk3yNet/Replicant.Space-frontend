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


def _ts(v):
    from .automations import _ts as ts
    return ts(v)


class OpsRules:
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
            p = tr.placement(loc, devices, reps, stowed, busy, use_replicant_vessel=manual or bool(cfg.get("use_replicant_vessel")),
                             keep=keep, bps=bps)
            if p["kind"] == "factory" and loc in orders:
                lines.append(f"{loc}: a beacon is being printed for it — a vessel picks it up once it's out"
                             + ("" if p.get("has_vessel") else f" (needs a vessel with a hold in {row['star']})"))
                continue
            if p["kind"] in ("none", "wait") or (p["kind"] == "print" and not (manual or cfg.get("allow_print", True))) \
                    or (p["kind"] == "factory" and not (manual or cfg.get("print_beacons", True))):
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
        return out

    # --- asteroid defense -----------------------------------------------------------------------------------------
    async def defence_objects(self) -> dict[str, dict]:
        """Tracked objects, after picking up detections and closures from the event log."""
        import json as _json
        from . import defence as dfn
        objs = await self.db.kv_get("objects", {}) or {}
        rows = await self.db.fetchall("SELECT event, payload, location, created_at FROM events WHERE event='system.object_detected' "
                                      "OR event LIKE 'diversion.%' ORDER BY seq")
        for r in rows:
            p = _json.loads(r["payload"] or "{}")
            des = p.get("object_designation") or p.get("designation")
            if not des:
                continue
            if r["event"] == "system.object_detected":
                if des not in objs:
                    objs[des] = {**dfn.from_detected(p), "detected_at": r["created_at"]}
            elif r["event"] in dfn.CLOSED_EVENTS and des in objs:
                objs[des]["closed"] = r["event"].split(".")[1]
                objs[des].setdefault("closed_at", r["created_at"])
        return objs

    async def defence_report(self) -> list[dict]:
        from . import defence as dfn
        from .shapes import normalize_blueprints
        objs = await self.defence_objects()
        devices = await self.devices()
        bps = normalize_blueprints(await self.db.kv_get("blueprints", []))
        enroute = await self._defence_enroute()
        out = [dfn.assess(o, devices, bps, enroute=enroute.get(d, 0)) for d, o in objs.items()]
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
        now = _now()
        read = 0
        for des, o in objs.items():
            if o.get("closed") or (only and des != only):
                continue
            eta = _ts(o.get("impact_eta"))
            if eta and (now - eta).total_seconds() > 3600:
                o["closed"] = "past impact time"
                continue
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
                read += 1
        await self.db.kv_set("objects", objs)
        s = await self.settings()
        cfg = s["rules"].get("asteroid_defence") or {}
        alerted = await self.db.kv_get("objects_alerted", {}) or {}
        lines: list[str] = []
        devices = await self.devices()
        bps = normalize_blueprints(await self.db.kv_get("blueprints", []))
        enroute = await self._defence_enroute()
        for des, o in objs.items():
            a = dfn.assess(o, devices, bps, enroute=enroute.get(des, 0))
            key = a["verdict"].split(" —")[0]
            if (cfg.get("enabled") or manual) and alerted.get(des) != key:
                alerted[des] = key
                if a["threat"] or key in ("diverted", "too late", "impacted"):
                    from .traffic import notify
                    eta = f", impact in {a['hours_left']:.1f} h" if a["hours_left"] else ""
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
