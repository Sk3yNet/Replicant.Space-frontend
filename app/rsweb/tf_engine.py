"""Terraformer fleets in the engine (terraformer.py has the rules): stage terraformer_pass."""
from __future__ import annotations

import json
import logging
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
        if not tr.valid_body(planet):
            run.update({"phase": "idle", "note": "assign a planet or moon (e.g. KELMONENT-3) under Terraforming below"})
            return []
        if run.get("planet") != planet:   # a new assignment starts over
            for k in ("departed", "in_range_since", "push", "alerts", "recent", "go", "left"):
                run.pop(k, None)
            run["planet"] = planet
        ms = fl.members(f, devices)
        tfd = [d for d in ms if (d.get("device_type") or "") in tf.DEVICES]
        if not any(d.get("device_type") == "terraform_monitor" for d in tfd):
            run.update({"phase": "idle", "note": "needs a terraform monitor in the loadout"})
            return []
        out: list[str] = []
        for key in ("job", "steer_job"):
            j = ctx["jobs"].get(run.get(key))
            if j and j["status"] in ("running", "waiting", "pending"):
                continue
            if j and j["status"] == "failed":
                err = next((s.get("error") for s in j["steps"] if s.get("status") == "failed"), "") or "?"
                run["note"] = f"last {'trip' if key == 'job' else 'steering'} failed: {err}"
            run.pop(key, None)
        here = [d for d in tfd if d.get("location") == planet and not d.get("stowed_in_device_code")
                and not d.get("attached_to_device_code")]
        away = [d for d in tfd if d not in here]
        if away and not run.get("job"):
            out += await self._tf_move(f, cfg, run, ctx, ms, away)
        if run.get("job"):
            run["phase"] = "moving"
        if any(d.get("device_type") == "terraform_monitor" for d in here):
            if not run.get("job"):
                run["phase"] = "terraforming"
            out += await self._tf_steer(f, cfg, run, ctx, here)
        return out

    # --- getting there ---------------------------------------------------------------------------------------------
    async def _tf_move(self, f: dict, cfg: dict, run: dict, ctx: dict, ms: list[dict], away: list[dict]) -> list[str]:
        from . import fleets as fl
        from . import terraform as tf
        from .automations import SHORT_TIMEOUT, step
        from .logistics import holds_replicant
        from .rockmining import open_rocks
        devices, busy, planet = ctx["devices"], ctx["busy"], cfg["planet"]
        star = planet.split("-")[0]
        hot = [r for r, o in open_rocks(ctx["objs"]).items() if (o.get("impact_target") or "") == planet]
        if hot:
            run["note"] = f"an asteroid ({hot[0]}) is headed for {planet}: the fleet stays off it until that's over"
            return []
        departed = bool(run.get("departed"))
        if not departed:
            short = [r for r in fl.roster(f, devices)["rows"] if r["short"]]
            if short and not run.get("go"):   # Depart now: whatever it has
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
            riders = [d for d in ms if d is not car and not fl.flies_itself(d) and not fl.is_carrier(d)
                      and not holds_replicant(d, devices)]
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
                if code in busy or d.get("stowed_in_device_code") or d.get("attached_to_device_code") or not d.get("location"):
                    left.append(code)
                    continue
                mode = fl.take_seat(free, d)
                if not mode:
                    left.append(code)
                    continue
                stops.setdefault(d["location"], []).append((code, mode))
                aboard.append((code, mode))
            tour, at = fl.pickup_tour(cc, car.get("location"), stops)
            steps += tour
            if at != planet:
                steps += [step(f"{cc} → {planet}", f"/devices/{cc}", {"command": "travel", "destination": planet}, critical=True),
                          fl._wait_arrive(cc, planet)]
            for code, mode in aboard:   # the terraform devices come off at the planet; the rest stays aboard
                d = next((x for x in ms if x["device_code"] == code), {})
                if (d.get("device_type") or "") not in tf.DEVICES:
                    continue
                if mode == "attach":
                    steps.append(step(f"{cc}: detach {code}", f"/devices/{cc}", {"command": "detach", "device": code}))
                else:
                    st = step(f"deploy {code} from {cc}", f"/devices/{code}", {"command": "deploy"}, wait=["device.deployed"],
                              timeout=SHORT_TIMEOUT)
                    st["wait_device"] = code
                    steps.append(st)
            moved = [cc] + [c for c, _ in aboard]
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
        if not departed:
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
        if not cfg["steer"]:
            run["note"] = (run.get("note") or "") if not fresh else "steering paused: the devices stay as they are"
            return out
        if run.get("steer_job"):
            return out
        now = _now()
        recent = {c: r["key"] for c, r in (run.get("recent") or {}).items() if now - (_ts(r.get("at")) or 0) < RECENT_S}
        reach = [d for d in here if d.get("in_control_range") is not False and d["device_code"] not in ctx["busy"]]
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
        it has cleared. A warning while the fleet steers; an error when steering is paused (nothing counters it)."""
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
                                                           "; steering is paused, nothing counters it"))
                await self.log("terraformer", txt, level, notify=True)
                out.append(f"disaster: {c['rule']}")
        a = tr.anomaly_text(st)
        if a:
            k = "anomaly:" + tr.norm(a)
            now[k] = seen.get(k) or now_iso()
            if k not in seen:
                await self.log("terraformer", f"{f['name']}: anomaly at {planet}: {a}", level, notify=True)
                out.append("anomaly")
        run["alerts"] = now
        return out
