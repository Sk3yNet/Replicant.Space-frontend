"""Engine stage bootstrap_pass: runs each bootstrap fleet (bootstrap.py) — one job of commands per pass at most, and
nothing while that job runs or a decision waits for you."""
from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any

from . import bootstrap as bt
from .db import now_iso
from .wardhub import WARD_CAP


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


class BootstrapMixin:
    async def bootstrap_world(self, devices: list[dict] | None = None, fleets: list[dict] | None = None) -> dict:
        from . import fleets as fl
        from . import wards
        from .shapes import normalize_blueprints, normalize_inventory
        devices = devices if devices is not None else await self.devices()
        fleets = fleets if fleets is not None else await self.fleets()
        cat = await self.db.kv_get("stars", {}) or {}
        scans = {}
        for r in await self.db.fetchall("SELECT star, data FROM systems"):
            try:
                scans[r["star"]] = json.loads(r["data"])
            except (TypeError, ValueError):
                continue
        reports = {}
        for f in fleets:
            if f.get("family") and fl.stationed(f):
                have = Counter(d.get("device_type") for d in fl.members(f, devices))
                reports[f["id"]] = {"short": sum(max(0, n - have.get(t, 0)) for t, n in fl.station_wants(f).items())}
        return {"devices": devices,
                "inventory": {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))},
                "bps": {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))},
                "pos": {s.get("designation"): s.get("position") for s in cat.get("stars") or []
                        if isinstance(s, dict) and s.get("position")},
                "scans": scans, "warded": wards.foreign(cat, devices),
                "homes": {f["home"] for f in fleets if f.get("station") and f.get("home") and not f.get("family")},
                "reports": reports}

    async def bootstrap_pass(self, only: str | None = None) -> list[str]:
        fleets = await self.fleets()
        boots = [f for f in fleets if f.get("role") == "bootstrap" and f.get("boot") and (not only or f["id"] == only)]
        if not boots:
            return []
        devices = await self.devices()
        w = await self.bootstrap_world(devices, fleets)
        jobs = await self.jobs()
        out, changed = [], False
        for f in boots:
            b = f["boot"]
            if any(j["status"] in ("running", "waiting") and (j.get("meta") or {}).get("bootstrap") == f["id"] for j in jobs):
                continue
            before = json.dumps(b, sort_keys=True, default=str)
            await self._boot_adopt_prints(f, devices)
            ev = bt.evaluate(f, w)
            b["status"] = {k: ev.get(k) for k in ("stage", "label", "gate", "next", "blocked")}
            b["status"]["at"] = now_iso()
            if ev["actions"] and not b.get("paused") and not b.get("decision"):
                line = await self._boot_act(f, ev, fleets, devices)
                if line:
                    out.append(line)
            await self._boot_wards(f, fleets, devices, w)
            await self._boot_controllers(f, fleets, devices, w)
            if json.dumps(b, sort_keys=True, default=str) != before:
                if b.get("decision") and b.get("decision_noted") != json.dumps(b["decision"], sort_keys=True, default=str):
                    b["decision_noted"] = json.dumps(b["decision"], sort_keys=True, default=str)
                    await self.log("fleets", f"{f['name']}: waiting for your OK — {b['decision']['kind']} (Fleets page)",
                                   "alert", notify=True)
                changed = True
        if changed:
            await self.save_fleets(fleets)
        return out

    async def _boot_wards(self, f: dict, fleets: list[dict], devices: list[dict], w: dict) -> None:
        """Each of the bootstrap's systems gets a ward when it's worth it: the ward costs less than `ward_hours` of what
        the system has been producing, or straight away when another player's drones mine there — while under the cap."""
        from . import others as oth
        from . import wards
        b = f["boot"]
        s = bt.settings(b)
        kids = [g for g in fleets if g.get("family") == f["id"] and g.get("station") and g.get("home")]
        if not kids:
            return
        cost = sum(bt.cost_of("system_ward", w["bps"]).values())
        n_wards = len([d for d in devices if d.get("device_type") == "system_ward"]) + \
            sum(1 for g in fleets if (g.get("wants") or {}).get("system_ward") and not any(
                d.get("device_type") == "system_ward" for d in bt.members(g, devices)))
        snaps = oth.normalize(await self.db.kv_get(oth.KV, {}))["stars"]
        now = now_iso()
        samples = b.setdefault("output", {})
        for g in kids:
            star = g["home"]
            total = sum(sum(bt.amounts(items).values()) for loc, items in w["inventory"].items() if star_of(loc) == star)
            hist = [x for x in samples.get(star) or [] if x[0] >= _hours_ago(24)] + [[now, total]]
            samples[star] = hist[-48:]
            if (g.get("wants") or {}).get("system_ward") or star in wards.ours(devices) or n_wards >= WARD_CAP:
                continue
            crowded = oth.drones_at(snaps.get(star), star) > 0
            rate = _rate(samples[star])
            if crowded or (cost and rate > 0 and cost / rate <= s["ward_hours"]):
                g.setdefault("wants", {})["system_ward"] = 1
                n_wards += 1
                why = "another player's drones mine there" if crowded else f"costs {cost / rate:.1f} h of its output"
                self._boot_log(b, f"ward for {star}: {why}")
                await self.log("fleets", f"{f['name']}: a system ward for {star} — {why}")

    async def _boot_controllers(self, f: dict, fleets: list[dict], devices: list[dict], w: dict) -> None:
        """The hub's and each outpost's AMI controllers, once active at the system's best belt: the survey controller runs
        belt_search (keeps sites open), the mining controller maintain_ratios — the mix the family's missing devices cost,
        refreshed when it shifts (at most every 6 h). The loadout pass already has new drones join them."""
        from . import fleets as fl
        from .automations import step
        b = f["boot"]
        kids = [g for g in fleets if g.get("family") == f["id"] and g.get("station") and g.get("home")]
        if not kids:
            return
        busy = self.busy_devices(await self.jobs())
        short: dict[str, int] = {}
        for g in kids:
            have = Counter(d.get("device_type") for d in fl.members(g, devices))
            for t, n in fl.station_wants(g).items():
                if n > have.get(t, 0):
                    short[t] = short.get(t, 0) + n - have.get(t, 0)
        want = bt.ratios({t: n for t, n in short.items() if t in w["bps"]}, w["bps"])
        sent = b.setdefault("directives", {})
        steps, codes = [], []
        for g in kids:
            belt = (bt.best_belt(w["scans"].get(g["home"])) or {}).get("designation")
            for d in fl.members(g, devices):
                t, code = d.get("device_type"), d.get("device_code")
                st = str(d.get("status") or "")
                at = d.get("location") or ""
                if t not in ("ami_mining_controller", "ami_survey_controller") or code in busy or not belt \
                        or not at.startswith(g["home"] + "-BELT") or st.startswith(("inactive", "stowed", "travel", "cruis", "surg")) \
                        or d.get("in_control_range") is False:
                    continue
                dv = d.get("ami_directive") or {}
                name = dv.get("name") if isinstance(dv, dict) else dv
                if t == "ami_survey_controller":
                    if name != "belt_search":
                        steps.append(step(f"{code}: belt_search at {at}", f"/devices/{code}",
                                          {"command": "set_directive", "directive": "belt_search", "configuration": {}}))
                        codes.append(code)
                    continue
                last = sent.get(code) or {}
                drift = max((abs(want.get(r, 0) - (last.get("ratios") or {}).get(r, 0)) for r in set(want) | set(last.get("ratios") or {})), default=1)
                stale = not last.get("at") or last["at"] < _hours_ago(6)
                if name != "maintain_ratios" or (drift > 0.1 and stale):
                    steps.append(step(f"{code}: maintain_ratios {want}", f"/devices/{code}",
                                      {"command": "set_directive", "directive": "maintain_ratios", "configuration": want}))
                    sent[code] = {"ratios": want, "at": now_iso()}
                    codes.append(code)
        if steps:
            await self.create_job("fleets", f"{f['name']}: controller directives", None, steps,
                                  {"devices": codes, "bootstrap": f["id"]})
            self._boot_log(b, f"controller directives: {len(steps)}")

    async def _boot_adopt_prints(self, f: dict, devices: list[dict]) -> None:
        """A print the vessel finished: the new device (of the printed type, new since the print, aboard or where the
        vessel is) is tagged as the bootstrap's own."""
        b = f["boot"]
        p = b.get("printing")
        if not p:
            return
        v = next((d for d in devices if d.get("device_code") == b["vessel"]), {})
        known = set(p.get("known") or [])
        new = [d for d in devices if d.get("device_type") == p["type"] and d.get("device_code") not in known
               and (d.get("stowed_in_device_code") == b["vessel"] or (v.get("location") and d.get("location") == v.get("location")))
               and bt.family_tag(f["id"]) not in (d.get("tags") or [])]
        if not new:
            return
        d = sorted(new, key=lambda x: x.get("device_code"))[0]
        await self.create_job("fleets", f"{f['name']}: {d['device_code']} joins the bootstrap", d["device_code"],
                              [self._tag_step(d["device_code"], [bt.family_tag(f["id"])])],
                              {"devices": [d["device_code"]], "bootstrap": f["id"]})
        b.pop("printing", None)

    @staticmethod
    def _tag_step(code: str, add: list[str], remove: list[str] | None = None) -> dict:
        from .automations import step
        cfg: dict[str, Any] = {"add_tags": add}
        if remove:
            cfg["remove_tags"] = remove
        return step(f"{code}: tags +{','.join(add)}" + (f" −{','.join(remove)}" if remove else ""), f"/devices/{code}",
                    {"configuration": cfg}, method="PATCH")

    async def _boot_act(self, f: dict, ev: dict, fleets: list[dict], devices: list[dict]) -> str | None:
        """Turn the evaluation's actions into one job (or a direct call for scans / child fleets)."""
        from . import fleets as fl
        from .automations import step
        b = f["boot"]
        rep, vessel = b["replicant"], b["vessel"]
        steps: list[dict] = []
        touched: list[str] = []
        for a in ev["actions"]:
            k = a["kind"]
            if k == "scan":
                ok, resp, err = await self.send("POST", f"/replicants/{rep}/scan", {}, f"bootstrap: scan {a['star']}")
                if ok and isinstance(resp, dict) and (resp.get("asteroid_belt") is not None or resp.get("planets") is not None):
                    await self.db.execute("INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                                          (a["star"], json.dumps(resp), now_iso()))
                elif ok:
                    await self.system_scan(a["star"])
                b.setdefault("visited", []).append(a["star"])
                self._boot_log(b, f"scanned {a['star']}" if ok else f"scan of {a['star']} failed: {err}")
                return f"{f['name']}: scan {a['star']}"
            if k == "make_hub":
                return await self._boot_make_hub(f, fleets, devices)
            if k == "travel":
                steps.append(step(f"{a['device']} → {a['to']}", f"/devices/{a['device']}",
                                  {"command": "travel", "destination": a["to"]}, critical=True))
                steps.append(fl._wait_arrive(a["device"], a["to"]))
                if a.get("visit"):
                    b.setdefault("visited", []).append(a["visit"])
                touched.append(a["device"])
                break   # nothing else until it's there
            if k == "deploy":
                st = step(f"deploy {a['device']}", f"/devices/{a['device']}", {"command": "deploy"},
                          wait=["device.deployed"], timeout=300)
                st["wait_device"] = a["device"]
                steps.append(st)
                touched.append(a["device"])
            elif k == "stow":
                steps.append(fl.board_step(vessel, a["device"], "stow"))
                touched.append(a["device"])
            elif k == "start_mining":
                steps.append(step(f"{a['device']}: mine {a['resource']}", f"/devices/{a['device']}",
                                  {"command": "start_mining", "resource_type": a["resource"]}))
                touched.append(a["device"])
            elif k == "search":
                steps.append(step(f"{a['device']}: search the belt", f"/devices/{a['device']}", {"command": "search"}))
                touched.append(a["device"])
            elif k == "vessel_mine":
                steps.append(step(f"{rep}: mine {a['resource']} onboard", f"/replicants/{rep}/mine",
                                  {"resource_type": a["resource"]}))
            elif k == "vessel_stop_mining":
                steps.append(step(f"{rep}: stop onboard mining", f"/replicants/{rep}/mine", None, method="DELETE"))
            elif k == "print":
                # no wait: while it prints the vessel says so (status printing) and nothing else is ordered
                steps.append(step(f"{rep}: print {a['device_type']} on the vessel", f"/replicants/{rep}/print",
                                  {"device_type": a["device_type"]}, critical=True))
                b["printing"] = {"type": a["device_type"], "at": now_iso(),
                                 "known": [d["device_code"] for d in devices if d.get("device_type") == a["device_type"]]}
        if not steps:
            return None
        title = f"{f['name']}: {ev.get('next') or 'bootstrap'}"
        await self.create_job("fleets", title, vessel, steps, {"devices": touched, "bootstrap": f["id"]})
        self._boot_log(b, ev.get("next") or title)
        return title

    @staticmethod
    def _boot_log(b: dict, text: str) -> None:
        b.setdefault("log", []).append({"at": now_iso(), "text": text})
        b["log"] = b["log"][-40:]

    async def _boot_make_hub(self, f: dict, fleets: list[dict], devices: list[dict]) -> str:
        """The autofactory is up: the hub becomes a stationed fleet that takes materials in, with the bootstrap's
        devices there, its loadout what's there plus the hub kit."""
        from . import fleets as fl
        b = f["boot"]
        hub = b["hub"]
        hid = fl.fleet_id_for(f"{f['name']} hub", fleets)
        mine = [d for d in bt.members(f, devices) if d["device_code"] != b["vessel"] and star_of(d.get("location")) == hub]
        have = Counter(d.get("device_type") for d in mine)
        s = bt.settings(b)
        wants = {**dict(have), **bt.HUB_KIT}
        wants["mining_drone"] = max(wants.get("mining_drone", 0), int(s["hub_miners"]))
        wants["survey_drone"] = max(wants.get("survey_drone", 0), math.ceil(wants["mining_drone"] * s["survey_per_miner"]))
        fleets.append({"id": hid, "name": f"{f['name']} · Hub", "role": "mining", "home": hub, "wants": wants,
                       "station": True, "materials": "self", "template": None, "family": f["id"], "parent": f["id"]})
        b.setdefault("children", {})["hub"] = hid
        b["stage"] = "hub"
        steps = [self._tag_step(d["device_code"], [fl.fleet_tag(hid)], [fl.fleet_tag(f["id"])] if fl.fleet_tag(f["id"]) in (d.get("tags") or []) else None)
                 for d in mine]
        await self.create_job("fleets", f"{f['name']}: {hub} becomes the hub", None, steps,
                              {"devices": [d["device_code"] for d in mine], "bootstrap": f["id"]})
        self._boot_log(b, f"autofactory up: {hub} is the hub ({f['name']} · Hub)")
        await self.log("fleets", f"{f['name']}: autofactory up — {hub} is the hub", notify=True)
        return f"{f['name']}: hub at {hub}"

    async def bootstrap_decide(self, fid: str, pick: str, kind: str) -> str:
        """Your OK on a waiting decision: the hub, an outpost, a relay waypoint, or a replacement for a warded system."""
        from . import fleets as fl
        fleets = await self.fleets()
        f = next((x for x in fleets if x.get("id") == fid and x.get("role") == "bootstrap"), None)
        if not f or not f.get("boot"):
            return "no such bootstrap fleet"
        b = f["boot"]
        dec = b.get("decision") or {}
        pick = pick.strip().upper()
        if not pick or dec.get("kind") != kind:
            return "nothing waiting for that"
        if kind == "hub":
            b["hub"], b["decision"], b["stage"] = pick, None, "move"
            msg = f"hub: {pick}"
        elif kind in ("outpost", "waypoint"):
            n = len(b.get("outposts") or []) + 1
            if kind == "outpost":
                oid = fl.fleet_id_for(f"{f['name']} outpost {n}", fleets)
                fleets.append({"id": oid, "name": f"{f['name']} · Outpost {n}", "role": "mining", "home": pick,
                               "wants": dict(bt.OUTPOST_KIT), "station": True, "materials": b["children"].get("hub", ""),
                               "template": None, "family": fid, "parent": fid})
                b.setdefault("outposts", []).append({"star": pick, "fleet": oid})
                msg = f"outpost {n}: {pick}"
            else:
                wid = fl.fleet_id_for(f"{f['name']} relay {pick}", fleets)
                fleets.append({"id": wid, "name": f"{f['name']} · Relay {pick}", "role": "mining", "home": pick,
                               "wants": {"ftl_relay": 1}, "station": True, "materials": "", "template": None,
                               "family": fid, "parent": fid})
                b.setdefault("waypoints", []).append(pick)
                msg = f"relay waypoint: {pick}"
            b["decision"] = None
        elif kind == "replace":
            lost = dec.get("for")
            o = next((x for x in b.get("outposts") or [] if x["star"] == lost), None)
            g = next((x for x in fleets if o and x.get("id") == o["fleet"]), None)
            if g:
                old = g["home"]
                g["home"], o["star"] = pick, pick
                how = await self.start_relocation(g, old, pick)
                msg = f"{g['name']} moves {old} → {pick} — {how}"
            else:
                msg = f"{lost} isn't an outpost; the hub can't move on its own — pick a new hub by ending this bootstrap"
            b["decision"] = None
        else:
            return "unknown decision"
        self._boot_log(b, f"OK: {msg}")
        await self.save_fleets(fleets)
        await self.log("fleets", f"{f['name']}: {msg}")
        return msg


def _hours_ago(h: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat(timespec="seconds")


def _rate(samples: list[list]) -> float:
    """Units per hour from [[iso, total stock], …]: the rise between the oldest and newest sample at least an hour
    apart (falls — prints, deliveries — aren't output, so only positive steps count)."""
    from datetime import datetime
    if len(samples) < 2:
        return 0.0
    try:
        t0, t1 = datetime.fromisoformat(samples[0][0]), datetime.fromisoformat(samples[-1][0])
    except (TypeError, ValueError):
        return 0.0
    hours = (t1 - t0).total_seconds() / 3600
    if hours < 1:
        return 0.0
    rise = sum(max(0.0, b[1] - a[1]) for a, b in zip(samples, samples[1:]))
    return rise / hours
