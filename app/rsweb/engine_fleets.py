"""Fleets: missions and their phases, filling, matrix pick-up, recall and the fleet runner.

A part of AutomationEngine (automations.py), split out by area; the methods are unchanged."""
from __future__ import annotations

from .engine_base import *  # noqa: F401,F403


class FleetsMixin:
    # --- fleets ---------------------------------------------------------------------------------------
    async def fleets(self) -> list[dict]:
        return (await self.loadout_cfg())["fleets"]

    async def save_fleets(self, items: list[dict]) -> None:
        from .ami_schedule import set_stationed
        from .fleets import fleet_tag, stationed
        derived = ("roster", "points", "job", "zero", "report", "route", "sends_to", "takes_from", "target_options",
                   "owner_move", "owner_hosts", "owners", "orders", "outposts", "matrix_note", "replicant_aboard")   # page-only fields, never stored
        keep = [{k: v for k, v in f.items() if k not in derived} for f in items]
        if any(f.get("materials") == "supply" or f.get("supply_to") for f in keep):   # nearest receiver, by home
            from .fleets import resolve_supply
            resolve_supply(keep, await self.star_lookup("pos_all"))
        # a stationed fleet that changed home: its replicants' vessels follow it there (rider_pass)
        before = {f.get("id"): f.get("home") for f in await self.db.kv_get("fleets", []) or [] if f.get("station")}
        moved = {f["id"]: f["home"] for f in keep if stationed(f) and f.get("home") and before.get(f["id"])
                 and before[f["id"]] != f["home"]}
        if moved:
            pending = await self.db.kv_get("rider_moves", {}) or {}
            for fid, home in moved.items():
                pending[fid] = {"home": home, "at": now_iso(), "sent": []}
            await self.db.kv_set("rider_moves", pending)
            hm = await self.db.kv_get("home_moves", {}) or {}   # open until every member is there (stray_pass)
            for fid, home in moved.items():
                hm[fid] = {"from": before[fid], "to": home, "at": now_iso()}
            await self.db.kv_set("home_moves", hm)
        await self.db.kv_set("fleets", keep)
        set_stationed({fleet_tag(f["id"]): f["home"] for f in items if stationed(f)})

    def _mlog(self, m: dict, text: str) -> None:
        m.setdefault("log", []).append({"at": now_iso(), "text": text})
        m["log"] = m["log"][-60:]

    async def auto_deals_pass(self) -> list[str]:
        """Trade fleets are retired (2026-10-12): the logistics service (logistics_pass) loads, delivers and fulfills approved
        contracts and trades. A trade fleet becomes a logistics fleet — same members and loadout — once no mission of its own
        is under way (a run already going finishes first)."""
        items = await self.fleets()
        out = []
        for f in items:
            if f.get("role") == "logistics" and (f.get("station") or f.get("materials")):
                # a logistics fleet works everywhere: never kept at home by the loadout pass, never a ferry's source (seen
                # in a snapshot 2026-10-13: a converted fleet still stationed at FALQUORYX, sending materials to printing-hub)
                f["station"], f["materials"] = False, ""
                out.append(f"{f.get('name')}: a logistics fleet isn't stationed and sends no materials — cleared")
            if f.get("role") != "trade" or (f.get("mission") or {}).get("status") in ("running", "stalled", "stopped"):
                continue
            f["role"] = "logistics"
            for k in ("auto_contracts", "auto_trades", "deals_done"):
                f.pop(k, None)
            if (f.get("mission") or {}).get("status") not in ("running", "stalled", "stopped"):
                f.pop("mission", None)
            out.append(f"{f.get('name')}: trade fleet is now a logistics fleet (trade fleets are retired)")
        if out:
            await self.save_fleets(items)
            for line in out:
                await self.log("fleets", line)
        return out

    async def listing_requests(self, reps: dict) -> tuple[list[str], list[dict]]:
        """Shop listings you asked for (Shop › Get listing): read now where a replicant of yours is in the shop's system
        (the game answers only then), else hand them to the logistics plan. Bill's too: it waits until his shop is seen.
        Returns (log lines, listings for lg.plan)."""
        from . import shop as sh
        wanted = await self.db.kv_get(sh.LISTING_KV, {}) or {}
        if not wanted:
            return [], []
        known = await sh.known_traders(self.db)
        here = {star_of((r or {}).get("location") or (r or {}).get("current_location")) for r in reps.values()} - {""}
        live = {j["id"] for j in await self.jobs() if j["status"] in ("running", "waiting")}
        lines, todo = [], []
        for ctrl, rq in list(wanted.items()):
            t = known.get(ctrl) or {}
            name = t.get("shop_name") or ctrl
            star = t.get("star") or star_of(t.get("location"))
            if star and star in here:
                try:
                    trades = await sh.read_listing(self.db, self.api, ctrl)
                except ApiError as e:
                    rq["tries"] = int(rq.get("tries") or 0) + 1
                    if rq["tries"] < 3:
                        continue
                    lines.append(f"listing of {name} at {t.get('location')}: reading it failed 3 times ({e.message}) — dropped")
                else:
                    lines.append(f"listing of {name} at {t.get('location')}: {len(trades)} trades read")
                wanted.pop(ctrl)
                continue
            if not t.get("location") or rq.get("job") in live:   # Bill's too: a logistics member goes (2026-10-22)
                continue
            todo.append({"controller": ctrl, "location": t["location"],
                         "label": f"get the listing of {name} at {t['location']}"})
        await self.db.kv_set(sh.LISTING_KV, wanted)
        return lines, todo

    async def logistics_pass(self, force: bool = False) -> list[str]:
        """The logistics service (logistics.py): every couple of minutes, track what prints, approved contracts and approved trades
        need, and send free logistics-fleet devices with it — priority: queued prints, local, long-range, stockpile returns."""
        from . import gameevents as gev
        from . import loadouts as lo
        from . import logistics as lg
        from . import wards
        from .shapes import normalize_blueprints, normalize_inventory
        last = _ts(await self.db.kv_get("logistics_last", None))
        if not force and last and (_now() - last).total_seconds() < 110:
            return []
        await self.db.kv_set("logistics_last", now_iso())
        for j, why in lg.stale_rescues(await self.jobs(), await self.devices()):
            j["status"], j["finished_at"] = "cancelled", now_iso()
            await self._update(j)
            await self.log("logistics", f"canceled: {j['title']} — {why}")
        items = await self.fleets()
        if not lg.logistics_fleets(items, held=True):
            await self.db.kv_set(lg.KV_STATE, {"at": now_iso(), "requests": [], "runs": [], "unmet": [], "fleet_devices": [],
                                               "note": "no logistics fleet yet (Fleets: a fleet with role logistics)"})
            return []
        devices = await self.devices()
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        inv = await self.safe_piles(inv)   # piles at overdue rocks are left (2026-11-08)
        inv_raw = {k: dict(v) for k, v in inv.items()}   # before what jobs will pick up is taken out (backlog sample)
        stars = await self.star_lookup("by")   # shared with every reader: never change a record
        pos = {k: v.get("position") for k, v in stars.items() if v.get("position")}
        reps = await self.db.kv_get("replicants", {}) or {}
        hosts = {r.get("hosted_device_code"): c for c, r in reps.items() if r.get("hosted_device_code")}
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        by_code0 = {d["device_code"]: d for d in devices}
        hosts = lg.riding_hosts(devices, hosts)
        incoming: dict[str, dict] = {}
        inv_at = _ts(await self.db.kv_updated("inventory"))
        leaving: set[str] = set()   # replicants a job is about to move: the system they're in now isn't covered for long
        fulfilling: set[str] = set()   # deals a logistics job is on its way to fulfill: one replicant each
        for j in jobs:
            if j["status"] not in ("running", "waiting"):
                continue
            t = (j.get("meta") or {}).get("logistics")
            if t and any(str(st.get("desc") or "").startswith(("fulfill ", "execute ")) for st in j["steps"][j.get("idx", 0):]):
                # live 2026-10-25: three replicant carriers left for Geothermal Surplus at ELUSORYR-3 within two hours
                fulfilling |= {str(x).split(":", 1)[1] for x in (t.get("fulfils") or [t.get("req")]) if x and ":" in str(x)}
            if t and t.get("to"):
                incoming[t["to"]] = {r: incoming.get(t["to"], {}).get(r, 0) + q for r, q in (t.get("carry") or {}).items()}
            # what any job (logistics run, fleet mission, relocation) is still going to pick up is spoken for, and so is
            # what it loaded since the last stockpile poll (seen live 2026-10-13: four haulers sent to KELMORNEA-3 for what
            # an earlier trip was already fetching; 2026-10-17: three loads at an ALPHARDAN-BELT-1 already emptied)
            at: dict[str, str] = {}
            for i, st in enumerate(j["steps"]):
                b, path = st.get("body") or {}, str(st.get("path") or "")
                code = path.split("/")[2] if path.startswith("/devices/") else None
                pending = i >= j.get("idx", 0)
                if b.get("command") == "travel" and code:
                    at[code] = b.get("destination")
                    if pending and code in hosts:
                        leaving.add(hosts[code])
                elif path.startswith("/replicants/") and path.endswith("/travel") and pending:
                    leaving.add(path.split("/")[2])
                elif b.get("command") == "collect_resources" and code and "(the payout)" not in st["desc"] \
                        and "(received)" not in st["desc"] and st.get("status") not in ("skipped", "failed"):
                    sent = _ts(st.get("started_at"))
                    if st.get("status") == "done" and sent and inv_at and sent < inv_at:
                        continue   # the stockpile list already shows it gone
                    named = st["desc"].rsplit(" at ", 1)[1] if " at " in st["desc"] else None   # "…: load 8 silicates at X"
                    pile = named or at.get(code) or (by_code0.get(code) or {}).get("location")
                    if pile:
                        lg.take_from(inv, pile, as_amounts(b.get("resources") or {}))
        taken, mission_stars, away_fleets = set(), set(), set()
        from .fleets import fleet_tag
        for f in items:
            m = f.get("mission") or {}
            if m.get("status") in ("running", "stalled", "stopped"):
                away_fleets.add(fleet_tag(f["id"]))   # out there on purpose: not stranded
                mission_stars |= {star_of(x) for x in m.get("targets") or []}
                if m.get("contract") or m.get("trade"):
                    from .fleets import deal as _deal
                    d = _deal(m)
                    taken.add(d.get("designation") or f"{d.get('controller')}:{d.get('trade_code')}")
        locked = wards.foreign(await self.star_lookup("flagged"), devices)
        contracts = []
        from .fleets import device_counts as fl_counts
        life = await gev.life_map(self.db)
        approvals = await gev.approvals_of(self.db, life)
        for des, e in (await gev.load(self.db)).items():
            if e.get("status") != "open" or not e.get("location") or des in taken or star_of(e["location"]) in locked:
                continue
            if not await gev.approved(self.db, e, life, approvals, devices):
                continue
            best = gev.progress(e, inv, devices, reps).get("best") or {}
            price = {x["resource"]: x["need"] for x in best.get("resources") or []}
            dshort = any(x["short_here"] for x in best.get("devices") or [])
            if price or best.get("devices"):
                rw = e.get("rewards") if isinstance(e.get("rewards"), dict) else {}
                contracts.append({"designation": des, "location": e["location"], "title": e.get("title"), "price": price,
                                  "devices_short": dshort, "fulfilling": des in fulfilling, "rewards": rw.get("resources") or {},
                                  "reward_devices": fl_counts(rw.get("devices"))})
        from collections import Counter
        from .fleets import device_counts as fl_counts
        ok = await self.db.kv_get(lg.KV_TRADES, {}) or {}
        if lg.settle_trades(ok, jobs):
            await self.db.kv_set(lg.KV_TRADES, ok)
        trades = []
        from .shop import known_traders
        from .contractsupply import status_note as cs_note
        supply_now = await self.db.kv_get("contract_supply", {}) or {}
        for ctrl, t in (await known_traders(self.db)).items():
            for tr in t.get("trades") or []:
                key = f"{ctrl}:{tr.get('trade_code')}"
                once = key in (ok.get("trades") or {})   # only a trade you approved: shop listings are never fulfilled
                if key in fulfilling or key in (ok.get("running") or {}):   # a job is on it: settled when it ends
                    continue
                # a ward's species lock stops other players completing location events (contracts) in its system, not a
                # shop's trades: Bill's shop is often at SOL, which is warded (owner 2026-10-20: an approved SOL trade was
                # left out of the requests)
                if not once or key in taken or not t.get("location") or tr.get("current_stock") == 0:
                    continue
                n_ = lg.trade_times(ok, key, tr.get("current_stock"))   # units approved (the game sells one per call)
                price = lg.times(as_amounts((tr.get("criteria") or {}).get("resources") or {}), n_)
                dneed = lg.times(fl_counts((tr.get("criteria") or {}).get("devices")), n_)
                there = Counter(d.get("device_type") for d in devices if gev.at_place(d, t["location"]))
                if price or dneed:
                    trades.append({"key": key, "controller": ctrl, "trade_code": tr.get("trade_code"), "name": tr.get("name"),
                                   "location": t["location"], "price": price, "once": once, "times": n_,
                                   "devices_short": any(there[k] < n for k, n in dneed.items()),
                                   "supply_note": cs_note((supply_now.get(f"trade:{key}") if dneed else None)),
                                   "rewards": lg.times(as_amounts((tr.get("rewards") or {}).get("resources") or {}), n_),
                                   "reward_devices": lg.times(fl_counts((tr.get("rewards") or {}).get("devices")), n_)})
        # a replicant about to leave doesn't cover its system for a delivery planned now (seen live 2026-10-17: two
        # deliveries to RHYBRENEUS-3 sent unescorted found the system out of comms range on arrival)
        commandable = await self.commandable(devices, stars, without=leaving)
        evac = await self.evacuation_targets(devices, stars)   # piles where an asteroid is due: {location: safe spot}
        drop_raw = lambda s_: lo.drop_point(s_, devices, inv, stars)  # noqa: E731
        homes_now = {f.get("home") for f in items if f.get("home") and f.get("station")}
        moves = await self.db.kv_get(lg.KV_MOVES, {}) or {}
        left = {star_of(l) for l, i in inv.items() if sum(as_amounts(i).values()) >= 1}
        moved = {o: e["to"] for o, e in moves.items() if o in left and o not in homes_now and e.get("to")}
        if moved != {o: e.get("to") for o, e in moves.items()}:
            await self.db.kv_set(lg.KV_MOVES, {o: moves[o] for o in moved})   # emptied, or a home again: done
        from . import consolidate as co
        sent = await self.db.kv_get("logistics_ami", {}) or {}   # AMI deliveries the logistics handed off: on their way until done
        for c, e in list(sent.items()):
            d = by_code0.get(c) or {}
            age = (_now() - (_ts(e.get("at")) or _now())).total_seconds()
            if age > 600 and (co.free_controller(d, devices, set()) or age > 3 * 3600):
                sent.pop(c)
            elif e.get("to"):
                incoming[e["to"]] = {r: incoming.get(e["to"], {}).get(r, 0) + q for r, q in (e.get("carry") or {}).items()}
                for g in e.get("legs") or []:   # the controller hasn't finished: its pick-up is spoken for too
                    lg.take_from(inv, g["pile"], g["take"])
        ami = [c for c in devices if co.free_controller(c, devices, busy) and c["device_code"] not in sent]
        # systems the Consolidate rule looks after (it's on, and they have an AMI transport controller): logistics leaves
        # their piles to it
        no_consolidate = ({star_of(c.get("location")) for c in devices if "transport" in (c.get("device_type") or "")
                           and "controller" in (c.get("device_type") or "") and c.get("location")}
                          if await self.rule_cfg("consolidate") else set())
        from .fleets import members as fl_members
        for f in lg.logistics_fleets(items):   # a member an AMI controller adopted (a ferry, say) is let go when idle
            for d in fl_members(f, devices):
                c = d.get("controller_device_code")
                if c and d["device_code"] not in busy and c not in busy and str(d.get("status") or "").startswith("idle"):
                    if await self.create_job("logistics", f"logistics: {c} releases {d['device_code']}", c,
                                             [step(f"{c}: release {d['device_code']} (it belongs to {f.get('name')})",
                                                   f"/devices/{c}", {"command": "release", "devices": [d["device_code"]]})],
                                             {"devices": [d["device_code"], c]}):
                        busy |= {d["device_code"], c}
        repair_to: dict[str, int] = defaultdict(int)   # repair devices on their way (lg.repair_runs)
        for j in jobs:
            t = (j.get("meta") or {}).get("logistics") or {}
            if j["status"] in ("running", "waiting") and str(t.get("req") or "").startswith("repair:"):
                repair_to[star_of(t.get("to"))] += 1
        out, listings = await self.listing_requests(reps)
        # relays go first: a vessel hosting a replicant near relays or beacons waiting for an escort is left to the loadouts
        ll = await self.db.kv_get("loadouts_last", {}) or {}
        escorts = {}
        if ll.get("escort_wanted") and _ts(ll.get("at")) and (_now() - _ts(ll["at"])).total_seconds() < 3600:
            escorts = lg.keep_escorts(ll["escort_wanted"], items, devices, hosts, busy, bps, pos, await self.supply_reach())
            busy |= set(escorts)
        wanted = None
        from . import wardhub as wh
        mcfg = await self.rule_cfg("maintenance") or {}   # hub upkeep is stocked at the hub's own spot (owner 2026-10-25)
        upkeep = (wh.upkeep_rows(devices, {l: as_amounts(i) for l, i in inv.items()})
                  if mcfg.get("hub_upkeep", True) and mcfg else [])
        from . import stations as stj   # station jobs the runner works: their resources, stocked like hub upkeep
        upkeep = upkeep + stj.logistics_rows(await self.db.kv_get(stj.KV_MINE, {}), await stj.runner_cfg(self.db),
                                             ((await self.db.kv_get(stj.KV_STATIONS, {})) or {}).get("stations") or [])
        p = lg.plan(fleets=items, devices=devices, bps=bps, inventory=inv, pos=pos, commandable=commandable, hosts=hosts,
                    busy=busy, reach=lg.SECTOR_RANGE_LY, contracts=contracts, trades=trades, incoming=incoming,
                    replicants=reps, drop=lambda s_: lo.destination(s_, stars) if drop_raw(s_) in evac else drop_raw(s_),
                    busy_stars=mission_stars,
                    prefs=await self.db.kv_get(lg.KV_PREFS, {}) or {}, ami=ami, moved=moved,
                    dark_nets=await self.dark_networks(devices, pos, bps, reps), away_fleets=away_fleets,
                    no_consolidate=no_consolidate, evacuate=evac, listings=listings,
                    repair_threshold=float(((await self.settings())["rules"].get("maintenance") or {}).get("threshold") or 80),
                    repair_to=repair_to,
                    # repair patrols stay in the network or at a fleet's home (live 2026-10-22: one sent to ERAJAELAE, lit
                    # only by a survey fleet's replicant, arrived after the fleet left and is stranded out of range)
                    repair_stars=(await self.commandable(devices, stars, networked=True)) | (homes_now & commandable),
                    lanes=(await self.db.kv_get(lg.KV_STATE, {}) or {}).get("lanes"), upkeep=upkeep,
                    rescue_pause=lg.rescue_pauses(await self.db.kv_get(lg.KV_RESCUE_PAUSE, {})))
        for r in p["requests"]:   # a rescue beyond the sector range waits for your OK (owner 2026-10-22): one alert each
            key = f"rescue-ok:{r['id']}"
            if r.get("confirm") and key not in self.stage_errors:
                self.stage_errors[key] = "asked"
                await self.log("logistics", f"{r['label']}: " + (f"about {r['trip_h']:.0f} h of travel" if r.get("trip_h") is not None
                                                                     else "trip of unknown length") + " — waits for your Confirm "
                                            "on Fleets › Logistics", "error", notify=True)
        by_code = by_code0
        pickups = await self.db.kv_get(lg.KV_PICKUPS, {}) or {}
        # devices a deal paid out: once they're at the site (fleetless, going nowhere), they're sent to the gather point,
        # carried like any delivery (logistics carriers first, a replicant along beyond the network)
        for loc, pk in list(pickups.items()):
            left = dict(pk.get("types") or {})
            for d in devices:
                t = d.get("device_type")
                tags = d.get("tags") or []
                if d.get("location") != loc or not left.get(t) or any(x.startswith(("fleet:", "to:")) for x in tags) \
                        or d["device_code"] in busy:
                    continue
                if await self.create_job("logistics", f"logistics: {d['device_code']} (payout at {loc}) → {pk['to']}",
                                         d["device_code"], [lo.tag_step(d["device_code"], [lo.to_tag(pk["to"])])],
                                         {"devices": [d["device_code"]]}):
                    left[t] -= 1
            left = {t: n for t, n in left.items() if n > 0}
            if not left or (_now() - (_ts(pk.get("at")) or _now())).total_seconds() > 86400:
                pickups.pop(loc)
            else:
                pk["types"] = left
        template = ((await self.db.kv_get("event_settings", {}) or {}).get("fulfil") or "").strip() or None
        for run in p["runs"]:
            steps = lg.run_steps(run, by_code, template)
            if not steps:
                continue
            codes = lg.run_devices(run)
            legs = [g for c in run.get("cargos") or [] for g in c.get("legs") or []] + (run.get("legs") or [])
            job = await self.create_job("logistics", f"logistics: {lg.describe(run)}", codes[0] if codes else run.get("ami"), steps,
                                        {"devices": codes, "logistics": {"req": run.get("req"), "to": run["to"], "carry": run["carry"],
                                                                         "legs": legs, **({"fulfils": [run.get("req")] + [
                                                                             x["req"] for x in run.get("also") or []]}
                                                                             if run.get("fulfil") else {})}})
            if job:
                run["job"] = job["id"]
                if run["kind"] == "listing":
                    from .shop import LISTING_KV
                    wanted = wanted if wanted is not None else await self.db.kv_get(LISTING_KV, {}) or {}
                    if run["controller"] in wanted:
                        wanted[run["controller"]]["job"] = job["id"]
                if run.get("ami"):
                    sent[run["ami"]] = {"to": run["to"], "carry": run["carry"], "at": now_iso(), "req": run.get("req"),
                                        "legs": legs}
                if run.get("fulfil") and run.get("reward_devices") and run.get("home"):
                    pickups[run["to"]] = {"types": run["reward_devices"], "to": star_of(run["home"]), "at": now_iso()}
                out.append(lg.describe(run))
                if run.get("fulfil"):   # the approval stays until the job ends; then the units it bought are counted
                    keys = [x["req"].split(":", 1)[1] for x in run.get("also") or [] if x["kind"] == "trade"]
                    if run["kind"] == "trade":
                        keys.append(run["req"].split(":", 1)[1])
                    for key in keys:
                        ok.setdefault("running", {})[key] = job["id"]
                    if keys:
                        await self.db.kv_set(lg.KV_TRADES, ok)
        if wanted is not None:
            from .shop import LISTING_KV
            await self.db.kv_set(LISTING_KV, wanted)
        await self.db.kv_set("logistics_ami", sent)
        await self.db.kv_set(lg.KV_PICKUPS, pickups)
        await self.db.kv_set(lg.KV_STATE, {"at": now_iso(), "requests": p["requests"], "runs": p["runs"], "unmet": p["unmet"],
                                           "far": p.get("far") or [], "fleet_devices": p["fleet_devices"], "reserve": p["reserve"], "escorts": escorts,
                                           "lanes": p["lanes"]})
        await self.sample_backlog(items, devices, inv_raw, drop_raw, busy, p, bps, reps)
        await self.note_demand({f"req:{r['id']}": r.get("to") for r in p["requests"] if not r.get("skipped")})
        out += await self.balance_pass(items, devices, hosts, pos, commandable, stars, inv_raw, set(escorts))
        for line in out:
            await self.log("logistics", line)
        return out

    async def note_demand(self, wants: dict[str, str | None]) -> None:
        """What needed a carrier where ({key: location}), first seen, kept balance.DEMAND_DAYS: the hubs' weights."""
        from . import balance as bal
        dem = await self.db.kv_get(bal.KV_DEMAND, {}) or {}
        now = _now()
        dem = {k: e for k, e in dem.items() if (now - (_ts(e.get("at")) or now)).total_seconds() < bal.DEMAND_DAYS * 86400}
        for k, loc in wants.items():
            if loc and k not in dem:
                dem[k] = {"star": star_of(loc), "at": now_iso()}
        await self.db.kv_set(bal.KV_DEMAND, dem)

    async def balance_pass(self, items: list[dict], devices: list[dict], hosts: dict, pos: dict, commandable: set[str],
                           stars: dict, inv: dict, keep: set[str]) -> list[str]:
        """Idle logistics carriers spread over the hubs by demand (balance.py, owner 2026-10-23)."""
        from . import balance as bal
        from . import loadouts as lo
        from . import logistics as lg
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        idle = {c: (_ts(t) or _now()).timestamp() for c, t in (await self.db.kv_get(bal.KV_IDLE, {}) or {}).items()}
        p = bal.plan(items, devices, jobs, hosts, busy, pos, commandable, lg.SECTOR_RANGE_LY,
                     await self.db.kv_get(bal.KV_DEMAND, {}) or {}, idle, _now().timestamp(), keep,
                     leeway=await self.db.kv_get(bal.KV_LEEWAY, bal.LEEWAY_PCT))
        await self.db.kv_set(bal.KV_IDLE, {c: datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")
                                           for c, t in idle.items()})
        await self.db.kv_set("hub_balance", {"at": now_iso(), **{k: p[k] for k in ("hubs", "weights", "classes")}})
        out = []
        for m in p["moves"]:
            code, rep = m["code"], m.get("replicant")
            dest = lo.drop_point(m["to"], devices, inv, stars)
            st = step(f"{code} → {dest} ({bal.CLASSES[m['cls']]}: {m['from']} has more than its share, {m['to']} fewer)",
                      f"/replicants/{rep}/travel" if rep else f"/devices/{code}",
                      {"destination": dest} if rep else {"command": "travel", "destination": dest},
                      wait=["travel.arrived"], match={"destination": m["to"]}, critical=True)
            st["wait_device"] = code
            if await self.create_job("logistics", f"logistics: {code} rebalances {m['from']} → {m['to']}", code, [st],
                                     {"devices": [code], "star": m["to"]}):
                out.append(f"{code} rebalances {m['from']} → {m['to']}")
        return out

    async def sample_backlog(self, items: list[dict], devices: list[dict], inv: dict, drop: Any, busy: set[str],
                             p: dict, bps: dict, reps: dict) -> None:
        """Dashboard › Logistics backlog and Trends (metrics.py): request ages every pass, a sample every ten minutes."""
        from . import logistics as lg, metrics, relaynet
        from .fleets import strays
        try:
            now = _now()
            jobs_req = {str((j.get("meta") or {}).get("logistics", {}).get("req")) for j in await self.jobs()
                        if j.get("rule") == "logistics" and j.get("status") in ("running", "waiting")
                        and (j.get("meta") or {}).get("logistics", {}).get("req")}
            track = metrics.track(await self.db.kv_get(metrics.KV_TRACK, {}),
                                  {r["id"] for r in p["requests"] if not r.get("skipped")} | jobs_req, now)
            await self.db.kv_set(metrics.KV_TRACK, track)
            if not await metrics.due(self.db):
                return
            receiving = {drop(f["home"]) for f in items if f.get("home") and (f.get("station") or f.get("materials") == "self")}
            carriers = lg.fleet_devices(items, devices)
            pos = await self.star_lookup("pos")
            cov = relaynet.coverage(relaynet.networks(devices, pos, bps), pos, relaynet.replicant_stars(reps, devices))
            relay = set().union(*[cov["covered"].get(i, set()) for i in cov["active"]]) if cov["active"] else set()
            await metrics.record(self.db, {
                **metrics.backlog(inventory=inv, receiving=receiving, carriers=carriers, devices=devices,
                                  strays=strays(items, devices, busy),
                                  fleet_homes={f["id"]: f.get("home") or "" for f in items if f.get("id")},
                                  requests=p["requests"], unmet=p["unmet"],
                                  commandable=cov["commandable"] - relaynet.dark_stars(devices)),
                **metrics.ages(track, now), **metrics.carrier_use(carriers, busy), **metrics.missions(items),
                "expired_24h": await metrics.noted(self.db, "expired", now), **metrics.roles(items, devices),
                **await metrics.mining_rates(self.db, now),
                **metrics.print_queues(devices),
                "cmd_systems": len(cov["commandable"] - relaynet.dark_stars(devices)), "relay_systems": len(relay)})
        except Exception as e:   # a chart sample never stops logistics
            log.warning("backlog sample failed: %s", e)

    async def contract_supply_pass(self, force: bool = False) -> list[str]:
        """Contracts asking for devices at their location (contractsupply.py): every few minutes, for each contract
        you set delivering (Contracts page) or a trade fleet is running, send fleetless spare / idle devices there and
        print only what you authorized; when a contract closes, its leftover devices become spare again."""
        from . import contractsupply as cs
        from . import fleets as fl
        from . import gameevents as gev
        from . import loadouts as lo
        from .shapes import normalize_blueprints, normalize_inventory
        last = _ts(await self.db.kv_get("contract_supply_last", None))
        if not force and last and (_now() - last).total_seconds() < 240:
            return []
        await self.db.kv_set("contract_supply_last", now_iso())
        supply = await self.db.kv_get(cs.KV, {}) or {}
        evs = await gev.load(self.db)
        devices = await self.devices()
        fleets_now = await self.fleets()
        running = {fl.deal(f["mission"]).get("designation") for f in fleets_now
                   if (f.get("mission") or {}).get("status") in ("running", "stalled") and (f["mission"].get("contract"))}
        # trade runs paid in devices: supplied like a contract, keyed trade:<controller>:<trade code>
        trade_runs: dict[str, dict] = {}
        for f in fleets_now:
            m_ = f.get("mission") or {}
            if m_.get("status") in ("running", "stalled") and m_.get("trade"):
                dl = fl.deal(m_)
                if dl["devices"] and dl["location"]:
                    trade_runs[dl["supply_key"]] = {
                        "designation": dl["supply_key"], "location": dl["location"], "star": dl["star"], "status": "open",
                        "title": dl["label"], "_need": dl["devices"]}
        # trades you approved for the logistics service (Shop: approve) that are paid in devices: supplied the same way
        from . import logistics as lg
        from .shop import known_traders
        ok = await self.db.kv_get(lg.KV_TRADES, {}) or {}
        if ok.get("trades"):
            for ctrl, t in (await known_traders(self.db)).items():
                for tr in t.get("trades") or []:
                    key = f"{ctrl}:{tr.get('trade_code')}"
                    need = lg.times(fl.device_counts((tr.get("criteria") or {}).get("devices")),
                                    lg.trade_times(ok, key, tr.get("current_stock")))
                    if need and t.get("location") and key in (ok.get("trades") or {}):
                        trade_runs.setdefault(f"trade:{key}", {
                            "designation": f"trade:{key}", "location": t["location"], "star": star_of(t["location"]),
                            "status": "open", "title": f"trade {tr.get('name') or tr.get('trade_code')}", "_need": need})
        trade_runs.update(await self.trade_post_supply(devices))   # trade posts' stock (tradepost.py)
        out: list[str] = []
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        # closed (or no longer wanted) contracts: their leftover devices go spare, the entry goes
        for des in list(supply):
            e = evs.get(des) or trade_runs.get(des)
            if des in trade_runs or (e and e.get("status") == "open" and (supply[des].get("active") or des in running)):
                continue
            tag = cs.contract_tag(des)
            left = [d for d in devices if tag in (d.get("tags") or []) and d.get("device_code") not in busy]
            steps = [st for d in left for st in cs.release_steps(d, des)]
            if steps:
                job = await self.create_job("contracts", f"contract {des}: {len(left)} leftover device(s) back to spare",
                                            None, steps, {"devices": [d["device_code"] for d in left]})
                if not job:
                    continue   # dry run: keep the entry so they're released once it's off
                out.append(f"{des}: released {len(left)}")
            if not e or e.get("status") != "open":
                supply.pop(des, None)
        cfg = await self.loadout_cfg()
        ls = cfg.get("settings") or {}
        reach = float(ls.get("max_supply_ly") if ls.get("max_supply_ly") is not None else lo.DEFAULT_SETTINGS["max_supply_ly"])
        stationed = {f["home"] for f in lo.stationed_fleets(cfg)}
        ignore = set(cfg.get("ignore_tags") or [])
        reps = await self.db.kv_get("replicants", {}) or {}
        hosts = {r.get("hosted_device_code") for r in reps.values() if r.get("hosted_device_code")}
        from .tradepost import carries_replicant   # a matrix (or anything with one inside) is never handed over
        hosts |= {d.get("device_code") for d in devices if carries_replicant(d, devices)}
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        inv = await self.safe_piles(inv)   # piles at overdue rocks are left (2026-11-08)
        bps = normalize_blueprints(await self.db.kv_get("blueprints", []))
        pos = await self.star_lookup("pos_all")
        from .traffic import beacons as _beacons
        civ_locs = {r["location"] for r in await self.civ_coverage() if r.get("completed") or r.get("open")}
        protect = {b["device_code"] for b in _beacons(devices) if b.get("location") in civ_locs}   # as the loadouts pass
        for des, e in [*evs.items(), *trade_runs.items()]:
            if e.get("status") != "open" or not e.get("location"):
                continue
            prog = ({"best": {"devices": [{"device_type": t, "need": n} for t, n in e["_need"].items()]}} if "_need" in e
                    else gev.progress(e, inv, devices, reps))
            if not cs.need_of(prog):
                continue
            entry = supply.get(des) or {}
            active = bool(entry.get("active")) or des in running or des in trade_runs
            if "_need" in e:   # a trade you approved (or a trade fleet runs): that is the OK to print what no spare covers
                ok_ = entry.setdefault("print_ok", {})   # (live 2026-11-02: 10 compute cores for the Crystal Synthesis
                for t_, n_ in e["_need"].items():        # Lab at DELTA-OBJ-2 waited forever, spares only 29,000 ly away)
                    ok_[t_] = max(int(ok_.get(t_) or 0), int(n_))
                if e.get("_tagged_only"):   # a trade post restocks for good: its recent prints don't use up the OK
                    for t_, n_ in e["_need"].items():
                        ok_[t_] = max(int(ok_.get(t_) or 0), cs.recent_orders(entry).get(t_, 0) + int(n_))
            if not active and des not in supply:
                continue
            p = cs.plan(e, prog, entry, devices, hosts=hosts, busy=busy, protect=protect, ignore=ignore,
                        stationed=stationed, blueprints=bps, inventory=inv, pos=pos, reach=reach, active=active)
            by = {d.get("device_code"): d for d in devices}
            entry.update({"location": e["location"], "star": e["star"], "assigned": p["assigned"], "printed": p["printed"],
                          "orders": p["orders"], "checked_at": now_iso(), "rows": p["rows"], "ready": p["ready"]})
            types = entry.setdefault("printed_types", {})
            for c in p["printed"]:
                if c in by:
                    types[c] = by[c].get("device_type")
            if p["send"]:
                steps = [st for d in p["send"] for st in cs.send_steps(d, e)]
                codes = [d["device_code"] for d in p["send"]]
                if await self.create_job("contracts", f"contract {des}: send {', '.join(codes)} to {e['location']}",
                                         None, steps, {"devices": codes}):
                    for d in p["send"]:
                        entry["assigned"][d["device_code"]] = {"type": d.get("device_type"), "at": now_iso()}
                    busy |= set(codes)
                    out.append(f"{des}: sending {len(codes)}")
            for pr in p["prints"]:
                if await self.create_job("contracts", f"contract {des}: print {pr['n']}× {pr['device_type']} on "
                                         f"{pr['factory']} for {e['location']}", pr["factory"], lo.print_steps(pr),
                                         {"devices": [], "star": e["star"]}):
                    entry["orders"].append({"type": pr["device_type"], "factory": pr["factory"], "n": pr["n"], "at": now_iso()})
                    busy.add(pr["factory"])
                    out.append(f"{des}: printing {pr['n']}× {pr['device_type']}")
            supply[des] = entry
        await self.db.kv_set(cs.KV, supply)
        for line in out:
            await self.log("contracts", line)
        return out

    async def _contract_option_ready(self, m: dict, inv: dict, devices: list[dict]) -> bool:
        from . import fleets as fl
        from . import gameevents as gev
        e = (await gev.load(self.db)).get(fl.deal(m).get("designation") or "")
        if not e or e.get("status") != "open":
            return False
        prog = gev.progress(e, inv, devices, {})
        if any(c["chosen"] for c in prog["criteria"]):   # you picked the option: only that one counts
            return bool(prog["best"]["ready_here"])
        return any(c["ready_here"] for c in prog["criteria"])

    async def _deal_devices_short(self, m: dict, devices: list[dict]) -> str:
        """A contract run whose contract also asks for devices at its location: what's still missing there."""
        from . import fleets as fl
        from . import gameevents as gev
        dl = fl.deal(m)
        if dl["kind"] == "trade":   # a trade paid in devices: they must be at the shop's location
            have = {t: sum(1 for d in devices if d.get("device_type") == t and d.get("location") == dl["location"])
                    for t in dl["devices"]}
            short = {t: n - have[t] for t, n in dl["devices"].items() if n > have[t]}
            return ", ".join(f"{n}× {t.replace('_', ' ')}" for t, n in sorted(short.items()))
        des = dl.get("designation")
        if not m.get("contract") or not des:
            return ""
        e = (await gev.load(self.db)).get(des)
        if not e or e.get("status") != "open":
            return ""
        prog = gev.progress(e, {}, devices, {})
        short = {x["device_type"]: x["short_here"] for x in (prog.get("best") or {}).get("devices") or [] if x["short_here"]}
        return ", ".join(f"{n}× {t.replace('_', ' ')}" for t, n in sorted(short.items()))

    async def _deal_gate(self, fleet: dict, m: dict, devices: list[dict], arriving: bool = False) -> tuple[str, str]:
        """A trade run at its wait phase: ("go", "") when the whole price is at the site and the fleet's replicant is
        there; ("wait", why) while it isn't (or materials are still on their way); ("rewind", why) when the site is
        short and nothing covers it — the mission steps back to deliver (cargo still aboard at the site) or to gather
        (load) the missing materials again."""
        from . import fleets as fl
        from .shapes import normalize_inventory
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        dl = fl.deal(m)
        site = dl["location"]
        if dl["kind"] == "trade" and dl.get("controller"):
            from .shop import known_traders
            t = (await known_traders(self.db)).get(dl["controller"]) or {}
            if t.get("location") and t["location"] != site:
                # a newer scan or directory read has the shop elsewhere (Bill's moves about daily)
                why = (f"the shop has moved: last seen at {t['location']} ({t.get('seen_at')}), not {site} — stop this run "
                       "and start the trade again there")
                if m.get("watch_note") != why:
                    self._mlog(m, why)
                    await self.log("fleets", f"{fleet['name']}: {why}", "error", notify=True)
                return "wait", why
        state, short = fl.deal_check(m, inv, devices)
        if state != "ok" and m.get("contract") and await self._contract_option_ready(m, inv, devices):
            # contracts offer alternative options (seen live 2026-10-08: Famine Assistance at AEMEROTH-2 — 3 orbital farms,
            # or a nutrient synthesizer and resources …): another option is complete at the site, so the price this run
            # set out to bring isn't needed
            state, short = "ok", {}
        what = ", ".join(f"{q} {r}" for r, q in short.items())
        if state == "incoming":
            return "wait", f"{site} is short of {what}: it's on its way there"
        if state in ("redeliver", "regather"):
            m["phase"] = "travel" if state == "redeliver" else None   # next: deliver / load
            if state == "regather":
                m["loaded"] = []
            why = (f"{site} is short of {what}: " + ("depositing the cargo still aboard" if state == "redeliver"
                                                       else "back to gathering it"))
            self._mlog(m, why)
            return "rewind", why
        dev_short = await self._deal_devices_short(m, devices)
        if dev_short:   # devices come by the contract's own delivery (contract_supply_pass), not in the fleet's holds
            if arriving:
                self._mlog(m, f"resources at {site}: waiting for devices there ({dev_short}) — Contracts page")
            return "wait", (f"{site} still needs {dev_short}: they're delivered separately ("
                            + ("spare ones are sent there; " if m.get("trade") else "Contracts page — ")
                            + "printing them needs your authorization)")
        reps = await self.db.kv_get("replicants", {}) or {}
        own = fl.fleet_replicant(fleet, devices, reps)
        by = {d.get("device_code"): d for d in devices}

        def at_site(code: str, r: dict) -> bool:
            host = by.get(r.get("hosted_device_code") or "") or {}
            return site in (r.get("location"), r.get("current_location"), host.get("location"))
        if own:   # the fleet's own replicant fulfills its trades
            code, r = own
            m["fulfiller"] = code
            if not at_site(code, r):
                if arriving:
                    self._mlog(m, f"everything delivered to {site}: waiting for {r.get('name') or code} (rides with the fleet)")
                return "wait", f"materials at {site}: waiting for {r.get('name') or code} (the fleet's replicant) to get there"
            self._mlog(m, f"{r.get('name') or code} at {site}: fulfilling")
            return "go", ""
        here = [c for c, r in reps.items() if at_site(c, r)]
        if not here:
            if arriving:
                self._mlog(m, f"everything delivered to {site}: waiting for a replicant there (none rides with the fleet)")
                await self.log("fleets", f"{fleet['name']}: materials at {site} — no replicant rides with the fleet; send "
                                         f"one there to fulfill {dl['label']}", notify=True)
            return "wait", f"materials at {site}: waiting for a replicant there to fulfill"
        m["fulfiller"] = here[0]
        self._mlog(m, f"{reps[here[0]].get('name') or here[0]} at {site}: fulfilling")
        return "go", ""

    async def next_salvage(self, m: dict) -> str | None:
        """Another salvage in a mining mission's system that it hasn't used up yet (the current one counts as used)."""
        from .salvage import available_salvage
        from .targets import system_resources
        target = (m.get("targets") or [None])[m.get("idx", 0)] if m.get("targets") else None
        if not target:
            return None
        used = set(m.get("salvaged") or []) | {m.get("salvage")}
        left = [x for x in available_salvage(await system_resources(self.db, star_of(target))) if x["code"] not in used]
        return left[0]["code"] if left else None

    async def fleet_phase_steps(self, fleet: dict, m: dict, phase: str, devices: list[dict]) -> tuple[list[dict], list[str]]:
        from . import fleets as fl
        from . import loadouts as lo
        from .shapes import normalize_inventory
        stars = await self.star_lookup("by")   # shared with every reader: never change a record
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        target = (m.get("targets") or [None])[m.get("idx", 0)] if m.get("targets") else None
        opts = m.get("opts") or {}
        if fleet["role"] == "mining" and phase in ("travel", "deploy", "work") and target \
                and star_of(target) in await self.warded(devices):
            m["stall"] = True   # warded since the mission started: nothing of ours can mine there, so don't go / unload
            return [], [f"another player's system ward or hub is in {star_of(target)} — nothing of ours can mine there; "
                        "recall the fleet or pick another system"]
        if m.get("relocate") and m.get("solo"):
            # surge-capable members not needed as carriers fly to the new home on their own (solo_pass): the
            # relocation's packing, gathering and travel leave them out
            ftag, solo = fl.fleet_tag(fleet["id"]), set(m["solo"])
            devices = [{**d, "tags": [x for x in d.get("tags") or [] if x != ftag]} if d.get("device_code") in solo else d
                       for d in devices]
        radii, far_au = await self.cruise_radii(), await self.max_cruise_au()
        if phase in ("assemble", "board"):   # board: boarding stragglers before a later leg (next_phase)
            return fl.assemble_steps(fleet, devices, radii, far_au)
        if phase == "gather":
            plan = fl.gather_plan(fleet, devices, stars, self.busy_devices(await self.jobs()), await self.supply_reach())
            if plan["recruit"]:
                self._mlog(m, "recruiting spares: " + ", ".join(f"{d['device_code']} ({d.get('device_type')})" for d in plan["recruit"]))
            if plan["tour"]:
                self._mlog(m, f"{plan['carrier']} collects from " + ", ".join(f"{s_} ({len(ds)})" for s_, ds in plan["tour"]))
            return fl.gather_steps(fleet, plan, stars, radii, far_au), plan["problems"]
        if phase == "travel":
            dark = await self.dark_home(fleet, target, devices) if m.get("relocate") else None
            if dark:
                m["stall"] = True
                return [], [dark]
            return fl.travel_steps(fleet, devices, target, stars), []
        if phase in ("deploy", "work") and target:
            behind = fl.left_behind(fleet, devices, target)
            if behind:
                # the travel leg didn't take the crew there: fly again rather than unload or survey in the wrong system
                where = ", ".join(f"{c['device_code']} in {star_of(c['location'])}" for c in behind)
                tries = m.setdefault("retravel", {})
                key = str(m.get("idx", 0))
                if tries.get(key, 0) >= 2:
                    m["stall"] = True
                    return [], [f"the fleet's carrier is still not in {star_of(target)} ({where}) after two more trips — "
                                "check its route, then retry"]
                tries[key] = tries.get(key, 0) + 1
                self._mlog(m, f"{where} with the crew aboard, not in {star_of(target)}: travelling again")
                m["phase"] = "gather"   # the loop's next phase is travel
                return [], []
        if phase == "deploy":
            if fleet["role"] == "explore" and target and await self.system_surveyed(target):
                # nothing left to survey there: don't unload, move on (the survey phase is skipped too)
                m["skip_survey"] = star_of(target)
                self._mlog(m, f"{star_of(target)} is already surveyed — not deploying, moving on")
                return [], []
            from . import placement as pl
            geo = pl.geography(target or "", devices, await self.system_scan(target) if target else None,
                               (stars.get(target) or {}).get("entry_point"))
            spot = fl.deploy_spot(geo)
            if not spot and fleet["role"] == "explore" and fl.loaded_outer(fleet, devices):
                # never drop a survey crew at the Kuiper belt or Oort cloud (owner 2026-10-21): the work phase takes
                # the carrier inside once the system's bodies are known
                self._mlog(m, f"nothing known inside {target} yet (no scan): the crew stays aboard for now")
                return [], []
            if not spot:
                self._mlog(m, f"nothing known inside {target} yet (no scan): unloading where the carrier is")
            # survey crews no longer drop relays or beacons (2026-10-11): ask for one on the Systems page instead
            return fl.unload_steps(fleet, devices, spot), []
        if phase == "work":
            if fleet["role"] == "explore" and m.get("skip_survey") and m["skip_survey"] == star_of(target or ""):
                return [], []
            if fleet["role"] == "explore":
                from . import placement as pl
                geo = pl.geography(target or "", devices, await self.system_scan(target) if target else None,
                                   (stars.get(target) or {}).get("entry_point"))
                ctrl = next((d for d in fl.members(fleet, devices) if d.get("device_type") == "ami_survey_controller"), None)
                if ctrl:   # it must be in the target system (in a carrier there, or deployed there)
                    by = {d.get("device_code"): d for d in devices}
                    host = by.get(ctrl.get("stowed_in_device_code") or ctrl.get("attached_to_device_code") or "") or {}
                    where = star_of(ctrl.get("location") or host.get("location"))
                    if where and where != star_of(target or ""):
                        # seen live 2026-10-08: it rode in another fleet's vessel, the survey ran in that system instead
                        m["stall"] = True
                        return [], [f"the survey controller {ctrl['device_code']} is in {where}, not in {star_of(target or '')} "
                                    "with the fleet — bring it aboard (Fleets: end mission & board, or move it), then retry"]
                pre, at = [], ctrl.get("location") if ctrl else None
                if fl.loaded_outer(fleet, devices):   # still aboard at the Kuiper belt / Oort cloud (the deploy phase)
                    drop = fl.deploy_spot(geo) or pl.target("ami_survey_controller", geo)
                    pre = fl.unload_steps(fleet, devices, drop)
                    at = at or drop
                spot = pl.target("ami_survey_controller", geo) if not ctrl or not pl.ok("ami_survey_controller", at, geo) \
                    else None   # no controller: where drones off a body or belt go to scan
                steps, problems = fl.explore_work_steps(fleet, devices, spot, radii, far_au)
                for st in steps:
                    if isinstance(st.get("seq0_from"), int):
                        st["seq0_from"] += len(pre)
                return pre + steps, problems
            belt = fl.richest_belt(target, await self.system_scan(target))
            sal = None
            if not belt:   # no belt here: salvage is the only work
                from .salvage import available_salvage
                from .targets import system_resources
                found = [x for x in available_salvage(await system_resources(self.db, target))
                         if x["code"] not in (m.get("salvaged") or [])]   # not one this mission already used up
                if not found:
                    m["stall"] = True
                    return [], [f"{target} has no asteroid belt and no salvage we know of — nothing to mine "
                                "(a survey drone finds salvage on the bodies; then retry)"]
                sal = found[0]
                m["salvage"] = sal["code"]
                self._mlog(m, f"no belt in {target}: salvaging {sal['code']} at {fl.salvage_body(sal['code'])}")
            m["belt"] = belt or fl.salvage_body(sal["code"])
            deliver_to = None
            if opts.get("deliver"):
                fleets = await self.fleets()
                pos = {k: (v or {}).get("position") or {} for k, v in stars.items()}
                # the fleet its materials go to, else (none set, or "supply") the nearest that takes them in from here
                to = fl.materials_target(fleet, fleets) if fleet.get("materials") != "supply" else None
                dests = [to["home"]] if to and to.get("home") and to["home"] != target else \
                    [f["home"] for f in fl.destinations(fleets) if f["home"] != target]
                if dests:
                    near = min(dests, key=lambda d: (lo._dist(target, d, pos), d))
                    deliver_to = lo.drop_point(near, devices, inv, stars)
                    m["deliver_to"] = deliver_to
                else:
                    self._mlog(m, "deliver mode, but no fleet takes materials in (Materials on the Fleets page) — hauling instead")
            return fl.mining_work_steps(fleet, devices, belt, deliver_to, sal)
        if phase == "recall":
            haul = m.get("belt") if fleet["role"] == "mining" and not m.get("deliver_to") and not m.get("end_here") else None
            for c in fl.outside_controllers(fleet, devices):
                self._mlog(m, f"{c['device_code']} ({c.get('device_type')}) runs this fleet's drones but isn't in the fleet "
                              "(no fleet tag): it won't be recalled or taken home — add it under Add / remove devices")
            return fl.recall_steps(fleet, devices, inv, haul, radii, far_au), []
        if phase == "return":
            rec = m.get("recall")
            if rec and rec.get("mode") == "after":   # picked again from where the task ended
                fleets = await self.fleets()
                pos = await self.star_lookup("pos")
                dest, why = fl.recall_target(fleet, fleets, devices, lambda a, b: lo._dist(a, b, pos))
                if dest and dest != rec.get("dest"):
                    rec.update(dest=dest, why=why)
                    self._mlog(m, f"recall: heading for {dest} ({why})")
                m["drop_star"] = rec.get("dest") or m.get("drop_star")
            return fl.travel_steps(fleet, devices, m.get("drop_star") or fleet["home"], stars), []
        if phase == "unload":
            # A stationed fleet's devices work its home system, so they come off the carriers. Any other fleet stays aboard,
            # ready for its next mission: only cargo is deposited (a cargo carrier riding a carrier hops off for it and
            # boards again).
            pile = lo.drop_point(m.get("drop_star") or fleet["home"], devices, inv, stars)
            if fleet.get("station") and fleet.get("role") != "explore":   # a survey crew stays aboard, ready to go again
                steps = fl.unload_steps(fleet, devices)
                self._mlog(m, "home: devices come off the carriers to work the home system (stationed fleet)")
            else:
                steps = []
                self._mlog(m, "home: everyone stays aboard; only cargo is deposited")
            r = fl.roster(fleet, devices)
            carriers = {c["device_code"]: c for c in r["carriers"]}
            for d in fl.members(fleet, devices):
                if int(d.get("cargo_used") or 0) <= 0 or d.get("device_type") not in ("cargo_freighter", "transport_hauler", "transport_drone"):
                    continue
                code = d["device_code"]
                ride = fl.aboard(d, set(carriers)) if not fleet.get("station") else None
                if ride:   # get off, deposit, get back on
                    if d.get("attached_to_device_code") == ride:
                        steps.append(step(f"{ride}: detach {code} (to deposit)", f"/devices/{ride}", {"command": "detach", "device": code}))
                    else:
                        st = step(f"deploy {code} from {ride} (to deposit)", f"/devices/{code}", {"command": "deploy"},
                                  wait=["device.deployed"], timeout=SHORT_TIMEOUT)
                        st["wait_device"] = code
                        steps.append(st)
                st = step(f"{code} → {pile}", f"/devices/{code}", {"command": "travel", "destination": pile},
                          wait=["travel.arrived"], match={"destination": pile})
                st["wait_device"] = code
                steps += [st, step(f"{code}: unload", f"/devices/{code}", {"command": "deposit_resources"})]
                if ride:
                    back = carriers[ride].get("location")
                    if back and back != pile:
                        st = step(f"{code} → {back} (board {ride})", f"/devices/{code}", {"command": "travel", "destination": back},
                                  wait=["travel.arrived"], match={"destination": back})
                        st["wait_device"] = code
                        steps.append(st)
                    steps.append(fl.board_step(ride, code, "attach" if d.get("attached_to_device_code") == ride else "stow"))
            return steps, []
        if fleet["role"] == "trade" and phase in ("load", "deliver", "trade", "collect", "home"):
            return await self.deal_phase_steps(fleet, m, phase, devices, inv, stars)
        return [], []

    async def deal_phase_steps(self, fleet: dict, m: dict, phase: str, devices: list[dict], inv: dict[str, dict],
                               stars: dict[str, dict]) -> tuple[list[dict], list[str]]:
        """A contract / trade mission (fleets.deal): gather the price, deliver it, fulfill, collect the rewards, go home."""
        from . import fleets as fl
        from . import loadouts as lo
        dl = fl.deal(m)
        site = dl["location"]
        pos = {k: (v or {}).get("position") or {} for k, v in stars.items()}

        def dist(a: str, b: str) -> float:
            return lo._dist(a, b, pos)
        if phase == "load":
            need = fl.site_short(dl["price"], inv.get(site) or {})
            m["need"] = need
            if not need:
                self._mlog(m, f"{site} already holds the price: nothing to pick up")
                return [], []
            legs, left, problems = fl.pickup_plan(fleet, devices, need, inv, site, dist)
            m["loaded"] = sorted({x["freighter"] for x in legs})
            for x in legs:
                self._mlog(m, f"{x['freighter']} picks up {', '.join(f'{q} {r}' for r, q in x['take'].items())} at {x['pile']}")
            if left and not legs:
                m["stall"] = True
            return fl.pickup_steps(legs, devices), problems
        if phase == "deliver":
            reps = await self.db.kv_get("replicants", {}) or {}
            hosts = {r.get("hosted_device_code") for r in reps.values() if r.get("hosted_device_code")}
            return fl.site_deliver_steps(fleet, devices, site, hosts, set(m.get("loaded") or [])), []
        if phase == "trade":   # the fleet's replicant (or the one found at the site) fulfills
            settings = await self.db.kv_get("event_settings", {}) or {}
            return [fl.fulfil_step(dl, m.get("fulfiller"), (settings.get("fulfil") or "").strip() or None)], []
        if phase == "collect":
            goods = dl["rewards"] or as_amounts(inv.get(site) or {})
            steps, problems = fl.collect_steps(fleet, devices, site, goods)
            m["drop_star"] = fl.nearest_drop_star(await self.fleets(), dl["star"], fleet["home"], dist)
            return steps, problems
        if phase == "home":
            if m.get("drop_star") and m["drop_star"] != fleet["home"]:
                return fl.travel_steps(fleet, devices, fleet["home"], stars), []
            return [], []
        return [], []

    async def fill_fleets(self, force: bool = False, only: str | None = None) -> list[str]:
        """Rule fleet_fill: idle fleets take spares for the gaps in their loadout (see fleets.fill_plan)."""
        cfg = await self.rule_cfg("fleet_fill")
        if not cfg and not force:
            return []
        if not force:
            last = _ts(await self.db.kv_get("fleet_fill_at", None))
            if last and (_now() - last).total_seconds() < 60 * max(1, int((cfg or {}).get("every_minutes") or 30)):
                return []
            await self.db.kv_set("fleet_fill_at", now_iso())
        from . import fleets as fl
        stars = await self.star_lookup("by")   # shared with every reader: never change a record
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        working = {(j.get("meta") or {}).get("fleet") for j in jobs if j["status"] in ("running", "waiting")}
        devices = await self.devices()
        radii, far_au = await self.cruise_radii(), await self.max_cruise_au()
        hubs = self.hub_stars(devices)
        ignore = set((await self.loadout_cfg()).get("ignore_tags") or [])
        out = []
        for f in await self.fleets():
            # a stalled or stopped mission still holds its devices where they are: don't send the carrier recruiting
            if (only and f["id"] != only) or (f.get("mission") or {}).get("status") in ("running", "stalled", "stopped"):
                continue
            if fl.stationed(f) or f.get("role") == "bootstrap":
                continue   # the loadout pass keeps a stationed fleet filled, at its home
            # a logistics fleet always has jobs going: its extras only need to be out of them (busy)
            if f["id"] in working and f.get("role") != "logistics":
                continue
            drop = self.droppable_extras(f, devices, busy, hubs, ignore)
            if drop:
                codes = [d["device_code"] for d in drop]
                job = await self.create_job("fleet_fill", f"{f['name']}: drops {len(codes)} extra(s) at a hub ({', '.join(codes)})",
                                            None, fl.drop_steps(f, drop, devices), {"devices": codes, "fleet": f["id"]}, force=force)
                busy |= set(codes)
                out.append(f"{f['name']}: drops " + ", ".join(f"{d['device_code']} ({d.get('device_type')})" for d in drop)
                           + " as spares at a hub" + ("" if job else " (dry run)"))
                if job:
                    continue   # one job per fleet at a time: the fill comes next pass
            if f["id"] in working or not fl.short_list(f, devices):
                continue
            steps, recruits, notes = fl.fill_plan(f, devices, stars, busy, radii, far_au, await self.supply_reach())
            for n in notes:
                out.append(f"{f['name']}: {n}")
            if not recruits:
                out.append(f"{f['name']}: short {fl.summarize(fl.short_list(f, devices))}, no idle spares to take")
                continue
            codes = [d["device_code"] for d in recruits]
            job = await self.create_job("fleet_fill", f"{f['name']}: takes {len(codes)} spare(s) ({', '.join(codes)})", None, steps,
                                        {"devices": codes + [d["device_code"] for d in fl.roster(f, devices)["carriers"]],
                                         "fleet": f["id"]}, force=force)
            busy |= set(codes)
            who = ", ".join(f"{d['device_code']} ({d.get('device_type')})" for d in recruits)
            out.append(f"{f['name']}: takes {who}"
                       + ("" if job else " (dry run)"))
        return out

    async def matrix_pickup_pass(self, force: bool = False, only: str | None = None) -> list[str]:
        """Loadout option "pick up a spare replicant matrix" (Joe 2026-11-10): a fleet with no replicant aboard, at
        home between missions, sends its vessel for the nearest spare filled matrix (fleets.spare_matrices /
        matrix_pickup). Runs with the loadouts rule, every 15 minutes. kv matrix_pickups {fleet: {at, note}}."""
        if not force:
            if not await self.rule_cfg("loadouts"):
                return []
            last = _ts(await self.db.kv_get("matrix_pickup_at", None))
            if last and (_now() - last).total_seconds() < 15 * 60:
                return []
            await self.db.kv_set("matrix_pickup_at", now_iso())
        from . import fleets as fl
        on = [f for f in await self.fleets() if f.get("pick_matrix")]
        items = [f for f in on if not only or f["id"] == only]
        notes = {k: v for k, v in (await self.db.kv_get("matrix_pickups", {}) or {}).items() if any(f["id"] == k for f in on)}
        if not items:
            await self.db.kv_set("matrix_pickups", notes)
            return []
        stars = await self.star_lookup("by")   # shared with every reader: never change a record
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        working = {(j.get("meta") or {}).get("fleet") for j in jobs if j["status"] in ("running", "waiting")}
        devices = await self.devices()
        ignore = set((await self.loadout_cfg()).get("ignore_tags") or [])
        spares = fl.spare_matrices(devices, busy, ignore)
        reach = await self.supply_reach()
        out = []
        for f in items:
            if (f.get("mission") or {}).get("status") in ("running", "stalled", "stopped") or f.get("role") == "bootstrap" \
                    or fl.held(f) or f["id"] in working:
                continue
            if fl.has_replicant(f, devices):
                notes[f["id"]] = {"at": now_iso(), "note": "has a replicant aboard"}
                continue
            steps, m, v, note = fl.matrix_pickup(f, devices, spares, stars, busy, reach)
            notes[f["id"]] = {"at": now_iso(), "note": note}
            if not steps:
                continue
            job = await self.create_job("loadouts", f"{f['name']}: picks up spare replicant matrix {m['device_code']}",
                                        v["device_code"], steps, {"devices": [m["device_code"], v["device_code"]],
                                                                  "fleet": f["id"]}, force=force)
            spares = [x for x in spares if x["device_code"] != m["device_code"]]
            busy |= {m["device_code"], v["device_code"]}
            out.append(f"{f['name']}: {note}" + ("" if job else " (dry run)"))
            if job:
                await self.log("loadouts", f"{f['name']} has no replicant: {note}")
        await self.db.kv_set("matrix_pickups", notes)
        return out

    @staticmethod
    def hub_stars(devices: list[dict]) -> set[str]:
        """Systems with one of our system hubs deployed: where fleets drop their extras."""
        return {star_of(d["location"]) for d in devices if d.get("device_type") == "system_hub" and d.get("location")
                and not str(d.get("status") or "").startswith(("destroyed", "decommission"))}

    @staticmethod
    def droppable_extras(f: dict, devices: list[dict], busy: set[str], hubs: set[str], ignore: set[str]) -> list[dict]:
        """An unstationed fleet's members beyond its loadout (fleets.extras) that can be let go now (owner 2026-10-27:
        when the fleet is next in at a hub, e.g. home for repairs): idle in a hub system, out of every job, in comms
        range, not working, and not riding a carrier that's on its way somewhere."""
        from . import fleets as fl
        by = {d.get("device_code"): d for d in devices}

        def moving(d: dict) -> bool:
            return bool(fl.in_flight(d)) or str(d.get("status") or "").lower().startswith(fl.MOVING)

        out = []
        for d in fl.extras(f, devices, ignore, hubs):
            host = by.get(d.get("stowed_in_device_code") or d.get("attached_to_device_code") or "")
            if (d["device_code"] in busy or fl.where_star(d, by) not in hubs or d.get("in_control_range") is False
                    or str(d.get("status") or "").lower().startswith(fl.BUSY_STATES) or moving(d)
                    or (host and (moving(host) or host.get("device_code") in busy))):
                continue
            out.append(d)
        return out

    async def fleet_owners(self, force: bool = False, only: str | None = None) -> list[str]:
        """Fleets with an owner (and 'keep' on, or `force`): members another replicant owns are handed to the owner
        (change_owner), one job per fleet. A transfer just sent isn't repeated for 15 minutes."""
        from . import fleets as fl
        if not force:
            last = _ts(await self.db.kv_get("fleet_owners_at", None))
            if last and (_now() - last).total_seconds() < 300:
                return []
            await self.db.kv_set("fleet_owners_at", now_iso())
        sent = await self.db.kv_get("owner_sent", {}) or {}
        now = _now()
        sent = {c: v for c, v in sent.items() if _ts(v.get("at")) and (now - _ts(v["at"])).total_seconds() < 900}
        devices = await self.devices()
        busy = self.busy_devices(await self.jobs())
        out = []
        for f in await self.fleets():
            if (only and f["id"] != only) or not f.get("owner") or not (f.get("keep_owner") or force):
                continue
            skip = busy | ({c for c, v in sent.items() if v.get("owner") == f["owner"]} if not force else set())
            steps = fl.owner_steps(f, devices, skip)
            if not steps:
                continue
            codes = [st["path"].split("/")[-1] for st in steps]
            job = await self.create_job("fleets", f"{f['name']}: {len(codes)} device(s) to owner {f['owner']}", None, steps,
                                        {"devices": codes, "fleet": f["id"]}, force=force)
            if job:
                for c in codes:
                    sent[c] = {"owner": f["owner"], "at": now_iso()}
            out.append(f"{f['name']}: {', '.join(codes)} → owner {f['owner']}" + ("" if job else " (dry run)"))
        await self.db.kv_set("owner_sent", sent)
        return out

    async def solo_pass(self, fleet: dict, m: dict, devices: list[dict], jobs: dict) -> bool:
        """A relocation's solo flyers (fleets.solo_flyers): each goes straight to the new home once it has finished
        what it's doing (not working, not in a job, not in flight) — from wherever it is."""
        from . import fleets as fl
        from .loadouts import WORKING
        target = (m.get("targets") or [None])[0]
        if not target:
            return False
        by = {d.get("device_code"): d for d in devices}
        busy = self.busy_devices(list(jobs.values()))
        stars = await self.star_lookup("by")   # shared with every reader: never change a record
        sent = m.setdefault("solo_sent", [])
        changed = False
        if await self.dark_home(fleet, target, devices):
            return False   # nobody flies there alone into the dark: the travel phase stalls the relocation and says why
        for code in sorted(set(m.get("solo") or []) - set(sent)):
            d = by.get(code)
            if not d or star_of(d.get("location")) == star_of(target):
                sent.append(code)
                changed = True
                continue
            if code in busy or str(d.get("status") or "").startswith(WORKING) or fl.in_flight(d):
                continue   # still on its current task
            steps = fl.travel_steps(fleet, [d], star_of(target), stars)
            if steps and await self.create_job("fleets", f"{code} flies to {fleet['name']}'s new home {star_of(target)} on its own",
                                               code, steps, {"devices": [code]}):
                sent.append(code)
                self._mlog(m, f"{code} ({d.get('device_type')}) flies to {star_of(target)} on its own")
                changed = True
        return changed

    @staticmethod
    def _retry_due(m: dict) -> bool:
        at = _ts((m.get("auto_retry") or {}).get("at"))
        return m.get("status") == "stalled" and not m.get("comms_wait") and bool(at) and _now() >= at

    def _plan_retry(self, m: dict) -> tuple[str, bool]:
        """A mission stalled: schedule its own retry. Returns the note for the logs and whether to skip the
        notification (a retry that stalled again on the same thing: Joe heard about it the first time)."""
        from . import fleets as fl
        again = int((m.get("auto_retry") or {}).get("n") or 0) > 0 \
            and (m.get("auto_retry") or {}).get("spot") == [m.get("idx", 0), m.get("phase")]
        at = fl.plan_retry(m, _now())
        if at:
            return f" — retries by itself at {at[11:16]} UTC (try {m['auto_retry']['n'] + 1} of {fl.RETRY_TRIES})", again
        return f" — no more automatic retries ({fl.RETRY_TRIES} tried): press Retry once it is sorted out", False

    # --- recall (Recall now / Recall after task, every fleet role; owner 2026-11-01) ------------------------------
    def fleet_jobs(self, fleet: dict, devices: list[dict], jobs: list[dict], skip: str | None = None) -> list[dict]:
        """Running jobs that work this fleet or any of its members (`skip`: a job id left out)."""
        from . import fleets as fl
        codes = {d["device_code"] for d in fl.members(fleet, devices)}
        out = []
        for j in jobs:
            meta = j.get("meta") or {}
            if j.get("status") not in ("running", "waiting", "pending") or j.get("id") == skip:
                continue
            if meta.get("fleet") == fleet["id"] or meta.get("bootstrap") == fleet["id"] or j.get("device") in codes \
                    or codes & set(meta.get("devices") or []):
                out.append(j)
        return out

    async def recall_fleet(self, fleet: dict, items: list[dict], mode: str, user: str = "") -> str:
        """`mode` "now": cancel what the fleet is doing and come back at once; "after": finish the current task (a
        mission's work at its target, or the job a member is on), then come back. Back means its station, else the
        nearest receiving point (fleets.recall_target). Roles without missions (fleets.HOLD_ROLES) are held: their own
        pass gives them no new work until Resume. Changes `fleet` in place (the caller saves); returns what it did."""
        from . import fleets as fl
        from . import loadouts as lo
        devices = await self.devices()
        pos = await self.star_lookup("pos")
        dest, why = fl.recall_target(fleet, items, devices, lambda a, b: lo._dist(a, b, pos))
        if not dest:
            return "nowhere to go: no station, receiving point or home"
        role = fleet.get("role")
        hold = role in fl.HOLD_ROLES
        m = fleet.get("mission") or {}
        live = m.get("status") in ("running", "stalled")
        jobs = await self.jobs()
        rec = {"mode": mode, "dest": dest, "why": why, "at": now_iso(), "by": user}
        if hold:
            prev = fleet.get("recall_hold") or {}
            fleet["recall_hold"] = {**rec, "state": "waiting", "paused_boot": prev.get("paused_boot", False)}
            b = fleet.get("boot")
            if role == "bootstrap" and b and not b.get("paused"):
                b["paused"] = True   # Resume un-pauses it
                fleet["recall_hold"]["paused_boot"] = True
        if mode == "after" and live and not m.get("homing") and not hold:
            m["recall"] = rec
            if role == "explore":
                m["targets"] = (m.get("targets") or [])[:m.get("idx", 0) + 1]   # this target is the last
                m["scout_ended"] = True
            self._mlog(m, f"recall after task ordered{' by ' + user if user else ''}: the work at this target "
                          f"finishes, then {dest} ({why})")
            return f"finishes its current task, then returns to {dest} ({why})"
        if mode == "now":
            for j in self.fleet_jobs(fleet, devices, jobs):
                await self.cancel(j["id"])
        elif self.fleet_jobs(fleet, devices, jobs, skip=m.get("job") if m.get("homing") else None) and not live:
            fleet["recall_hold"] = {**(fleet.get("recall_hold") or {}), **rec, "state": "waiting"}
            return f"finishes its current task, then returns to {dest} ({why})"
        if m.get("homing") and live:
            m["recall"], m["drop_star"] = rec, dest
            if mode == "now" and m.get("phase"):
                fl.rewind(fleet, m)   # its job was canceled above: run that phase again
            return f"already recalled: returning to {dest} ({why})"
        ros = fl.roster(fleet, devices)
        if not live and ros["stars"] in ([dest], []) and not ros["loose"]:
            if hold:
                fleet["recall_hold"]["state"] = "home"
                return f"already at {dest}: held there until Resume"
            fleet.pop("recall_hold", None)
            return f"already at {dest} ({why}): nothing to recall"
        self._start_homing(fleet, m, rec, live)
        return f"returning to {dest} ({why})" + (" — held there until Resume" if hold else "")

    def _start_homing(self, fleet: dict, m: dict, rec: dict, live: bool) -> None:
        """Turn the fleet's mission (or none) into a recall: recall → return → unload (fleets.HOMING_PHASES)."""
        new = {"status": "running", "homing": True, "phase": None, "targets": [], "idx": 0, "departed": True,
               "started_at": now_iso(), "log": list((m.get("log") or [])[-40:]) if live else [], "drop_star": rec["dest"],
               "recall": rec, "opts": dict(m.get("opts") or {}), "job": None}
        if live and fleet.get("role") == "mining" and m.get("phase") in ("work", "watch"):
            for k in ("belt", "deliver_to"):   # haul mode: freighters fill up at the belt on the way out
                if m.get(k):
                    new[k] = m[k]
        if fleet.get("recall_hold"):
            fleet["recall_hold"]["state"] = "returning"
        self._mlog(new, f"recall {'now' if rec['mode'] == 'now' else 'after task'}"
                        f"{' by ' + rec['by'] if rec.get('by') else ''}: everyone aboard, then {rec['dest']} ({rec['why']})")
        fleet["mission"] = new

    async def recall_pass(self, items: list[dict]) -> bool:
        """A Recall after task that waited for a member's job: start the trip home once nothing works the fleet."""
        waiting = [f for f in items if (f.get("recall_hold") or {}).get("state") == "waiting"
                   and (f.get("mission") or {}).get("status") not in ("running", "stalled")]
        if not waiting:
            return False
        devices, jobs = await self.devices(), await self.jobs()
        changed = False
        for f in waiting:
            if self.fleet_jobs(f, devices, jobs):
                continue
            rec = {k: v for k, v in f["recall_hold"].items() if k in ("mode", "dest", "why", "at", "by")}
            self._start_homing(f, f.get("mission") or {}, rec, False)
            await self.log("fleets", f"{f['name']}: task done — returning to {rec['dest']}")
            changed = True
        return changed

    async def run_fleets(self) -> None:
        from . import fleets as fl
        items = await self.fleets()
        if await self.recall_pass(items):
            await self.save_fleets(items)
        if not any((f.get("mission") or {}).get("status") == "running"
                   or (f.get("mission") or {}).get("comms_wait") or self._retry_due(f.get("mission") or {})
                   or ((f.get("mission") or {}).get("homing") and (f.get("mission") or {}).get("status") == "stalled")
                   for f in items):
            return
        jobs = {j["id"]: j for j in await self.jobs()}
        devices = await self.devices()
        changed = False
        for fleet in items:
            if fleet.get("role") == "bootstrap" and not (fleet.get("mission") or {}).get("homing"):
                continue   # no missions: bootstrap_pass drives it (a recall's trip home excepted)
            m = fleet.get("mission") or {}
            if (m.get("homing") and m.get("status") in ("running", "stalled") and m.get("phase") and fleet.get("station")
                    and fleet.get("role") != "bootstrap" and m.get("drop_star") == fleet.get("home")
                    and fl.roster(fleet, devices)["stars"] == [fleet.get("home")]
                    and not any(j_["status"] in ("running", "waiting") for j_ in self.fleet_jobs(fleet, devices, list(jobs.values())))):
                # recalled to its own station while every member is already there: nothing to board or fly (live
                # 2026-11-12: Miner_7 recalled at DOMBTAIRE, stalled for want of a carrier to put everyone aboard)
                m["status"], m["job"] = "done", None
                self._mlog(m, f"recall: everyone is already at {fleet['home']}, its station — nothing to fly")
                await self.log("fleets", f"{fleet['name']}: recall done, the fleet is already at {fleet['home']}")
                changed = True
                continue
            if m.get("status") == "stalled" and m.get("comms_wait"):
                # stalled because members were out of comms range (live 2026-10-22: Surveyor 2's heaven vessel left
                # ALFOLEN, its controller and drones went dark): resumes by itself once every member is back in range
                if any(d.get("in_control_range") is False for d in fl.members(fleet, devices)):
                    continue
                m.pop("comms_wait")
                fl.rewind(fleet, m)
                self._mlog(m, "back in comms range — resumed")
                changed = True
            elif self._retry_due(m):
                # stalled on something that may clear by itself (a member still on its way, a scan not in yet, a busy
                # device): try the stalled phase again, backing off (fleets.plan_retry)
                m["auto_retry"]["n"] = int(m["auto_retry"].get("n") or 0) + 1
                m["auto_retry"]["at"] = None
                fl.rewind(fleet, m)
                self._mlog(m, f"retrying by itself (try {m['auto_retry']['n']} of {fl.RETRY_TRIES})")
                changed = True
            if m.get("status") != "running":
                continue
            if m.get("relocate") and "solo" not in m and not m.get("departed"):
                # a relocation started before solo flyers existed (1.93): pick them now, so it stops waiting for
                # freighters nobody told to come back
                m["solo"] = sorted(fl.solo_flyers(fleet, devices))
                if m["solo"]:
                    self._mlog(m, f"{', '.join(m['solo'])} fly to the new home on their own after their current task")
                    changed = True
            if m.get("relocate") and set(m.get("solo") or []) - set(m.get("solo_sent") or []):
                changed |= await self.solo_pass(fleet, m, devices, jobs)
            j = jobs.get(m.get("job"))
            if j and j["status"] in ("running", "waiting"):
                continue
            err = next((s.get('error') for s in j['steps'] if s['status'] == 'failed'), '') if j else ''
            if (j and j["status"] == "failed" and m.get("contract") and m.get("phase") == "trade"
                    and "criteria not met" in str(err).lower() and int(m.get("criteria_misses") or 0) < 3):
                # the game says the contract isn't satisfied yet (seen live 2026-10-08: Trader_1 stalled at AEMEROTH-2
                # and stayed stalled after the contract's devices arrived): back to waiting at the site — the gate
                # holds until an option is complete there, then fulfills again
                m["criteria_misses"] = int(m.get("criteria_misses") or 0) + 1
                m["phase"], m["job"] = "wait", None
                self._mlog(m, f"the game says the contract's criteria aren't met yet ({err}) — waiting at the site "
                              f"until they are (try {m['criteria_misses']} of 3)")
                changed = True
                continue
            if j and j["status"] == "failed" and m.get("trade") and m.get("phase") == "trade":
                # a failed trade doesn't prove the shop is gone (owner, 2026-10-20): it stays listed until a scan of
                # its system doesn't show it
                dl = fl.deal(m)
                m["status"] = "stalled"
                again, quiet = self._plan_retry(m)
                self._mlog(m, f"stalled in trade: {err or 'the trade failed'} — the shop may have moved from "
                              f"{dl.get('location')}: scan that system to check, or Fleets › Refresh traders, then Retry"
                              + again)
                await self.log("fleets", f"{fleet['name']}: trade at {dl.get('location')} failed ({err}) — the shop may "
                                         "have moved" + again, "alert", notify=not quiet)
                changed = True
                continue
            if j and j["status"] == "failed":
                m["status"] = "stalled"
                why = next((s.get('error') for s in j['steps'] if s['status'] == 'failed'), 'a step failed')
                dark = "comms range" in str(err or why).lower() \
                    and any(d.get("in_control_range") is False for d in fl.members(fleet, devices))
                again, quiet = "", False
                if dark:
                    m["comms_wait"] = True
                    m.pop("auto_retry", None)
                else:
                    m.pop("comms_wait", None)
                    again, quiet = self._plan_retry(m)
                self._mlog(m, f"stalled in {m.get('phase')}: {why}"
                              + (" — resumes by itself once its members are back in comms range" if dark else again))
                await self.log("fleets", f"{fleet['name']}: stalled in {m.get('phase')}"
                                         + (" (out of comms range; resumes once back in range)" if dark else again),
                               "alert", notify=not quiet)
                changed = True
                continue
            if m.get("end_here"):
                # "End mission & board": one recall job (directives cleared, everyone back aboard a carrier), then stop
                # where the fleet is — no return trip
                if m.get("end_started"):
                    at = ", ".join(fl.roster(fleet, devices)["stars"]) or "?"
                    m["status"], m["job"] = "ended", None
                    m.pop("end_here", None), m.pop("end_started", None)
                    self._mlog(m, f"mission ended early — fleet aboard its carriers in {at}")
                    await self.log("fleets", f"{fleet['name']}: mission ended early, fleet aboard in {at}")
                    changed = True
                    continue
                m["phase"], m["phase_at"], m["end_started"] = "recall", now_iso(), True
                steps, problems = await self.fleet_phase_steps(fleet, m, "recall", devices)
                for pr in problems:
                    self._mlog(m, f"recall: {pr}")
                if steps:
                    codes = [d["device_code"] for d in fl.members(fleet, devices)]
                    job = await self.create_job("fleets", f"{fleet['name']}: end mission & board", None, steps,
                                                {"devices": codes, "fleet": fleet["id"]}, force=True)
                    m["job"] = job["id"] if job else None
                    self._mlog(m, f"ending: recall & board, {len(steps)} step(s)")
                else:
                    m["job"] = None
                    self._mlog(m, "ending: everyone is already aboard")
                changed = True
                continue
            if (fleet["role"] == "explore" and m.get("scouting") and m.get("phase") == "recall"
                    and m.get("idx", 0) + 1 >= len(m.get("targets") or []) and not m.get("scout_ended")):
                weak = self.scout_unhealthy(fleet, devices)
                here = star_of((m.get("targets") or [None])[m.get("idx", 0)])
                nxt, why = (None, "") if weak else await self.scout_next(fleet, items, devices, here, set(m.get("targets") or []))
                if nxt:
                    m["targets"].append(nxt)
                    self._mlog(m, f"{here} surveyed — on to {nxt}")
                else:
                    m["scout_ended"] = True
                    self._mlog(m, f"{here} surveyed — returning home " + (f"for repairs ({', '.join(weak)} at or below "
                                                                           f"{self.SCOUT_MIN_CAPACITY:g} %)" if weak else f"({why})"))
                changed = True
            if m.get("phase") == "wait":
                gate, note = await self._deal_gate(fleet, m, devices)
                if gate == "wait":
                    if m.get("watch_note") != note:
                        m["watch_note"] = note
                        changed = True
                    continue
                changed = True   # "go" (fulfill next) or "rewind" (the next phase is deliver / load again)
            if m.get("phase") == "watch":
                others = [j for j in await self.jobs() if (j.get("meta") or {}).get("fleet") != fleet["id"]]
                # the fleet's drones run by a controller outside it (a spare, another fleet's): that one releases them
                # and the fleet's own adopts them (live 2026-11-12: Surveyor 6's controller coordinated alone at
                # AELTHORION for two days, its 8 drones idle under the spare E085567D). Twice per spot at most.
                fix_key = f"{m.get('idx', 0)}"
                fixes = m.setdefault("reclaims", {})
                if fleet["role"] in ("explore", "mining") and int(fixes.get(fix_key) or 0) < 2:
                    fix = fl.held_elsewhere(fleet, devices, self.busy_devices(others))
                    if fix:
                        fixes[fix_key] = int(fixes.get(fix_key) or 0) + 1
                        codes = [d["device_code"] for d in fl.members(fleet, devices)]
                        job = await self.create_job("fleets", f"{fleet['name']}: own controller takes back its drones",
                                                    None, fix, {"devices": codes, "fleet": fleet["id"]}, force=True)
                        if job:
                            m["job"], m["phase_at"] = job["id"], now_iso()   # phase_at: wait for a fresh list after
                            self._mlog(m, f"drones held by another controller — {len(fix)} step(s) to take them back")
                            changed = True
                            continue
                done, why, upd = fl.watch_done(fleet, m, devices, now_iso(), self.busy_devices(others))
                m.update(upd)
                if m.get("watch_note") != why:
                    m["watch_note"] = why
                    changed = True
                if not done and not m.pop("recall_now", False):
                    continue
                nxt_sal = await self.next_salvage(m) if done and fleet["role"] == "mining" and m.get("salvage") \
                    and not m.get("recall") else None   # recalled after this task: no next salvage
                if nxt_sal:   # salvage used up, more in the system: back to work on the next one before ending
                    m.setdefault("salvaged", []).append(m["salvage"])
                    self._mlog(m, f"salvage {m['salvage']} used up — moving on to {nxt_sal}")
                    m["phase"], m["exhausted_since"] = "deploy", None   # the next phase is work, on the next salvage
                else:
                    self._mlog(m, f"work done ({why})")
            # next phase (skipping any with nothing to do)
            for _ in range(12):
                idx_was = m.get("idx", 0)   # next_phase moves an explore fleet on to its next target
                nxt = fl.next_phase(fleet["role"], m)
                if not nxt:
                    m["status"], m["job"] = "done", None
                    m.pop("auto_retry", None)
                    if m.get("homing"):
                        rh = fleet.get("recall_hold")
                        dest = (m.get("recall") or {}).get("dest") or m.get("drop_star")
                        if rh and fleet.get("role") in fl.HOLD_ROLES:
                            rh["state"] = "home"
                        else:
                            fleet.pop("recall_hold", None)
                        self._mlog(m, f"recalled: back at {dest}" + (" — held until Resume" if rh and fleet.get("role") in fl.HOLD_ROLES else ""))
                        await self.log("fleets", f"{fleet['name']}: recalled, back at {dest}", notify=True)
                        break
                    self._mlog(m, "mission complete")
                    await self.log("fleets", f"{fleet['name']}: mission complete")
                    break
                if nxt == "travel" and not m.get("departed"):   # first departure: the loadout (or members) all in one place
                    # all in one place: a relocation waits for every member, not for a complete loadout
                    r = (fl.together if m.get("relocate") else fl.readiness_anywhere)(
                        fleet, devices, prefer=m.get("from") or fleet.get("home"),
                        free=set(m.get("solo") or []) if m.get("relocate") else None)
                    if not r["ready"] and not (m.get("opts") or {}).get("partial"):
                        note = (f"waiting to leave: members still to collect into {r['at']}: "
                                + ", ".join(c for cs in r["elsewhere"].values() for c in cs) if m.get("relocate") else
                                f"waiting to leave: {fl.readiness_text(r)}") + " — Launch anyway sends it short"
                        if m.get("watch_note") != note:
                            m["watch_note"] = note
                            self._mlog(m, note)
                            changed = True
                        # spares can still fill the gaps: gather again (it only takes what's short)
                        steps, _ = await self.fleet_phase_steps(fleet, m, "gather", devices)
                        if steps:
                            m["phase"] = "gather"
                            job = await self.create_job("fleets", f"{fleet['name']}: gather (filling the loadout)", None, steps,
                                                        {"devices": [d["device_code"] for d in fl.members(fleet, devices)],
                                                         "fleet": fleet["id"]}, force=True)
                            m["job"] = job["id"] if job else None
                        break
                if nxt == "travel" or (nxt == "return" and m.get("departed")):
                    # every leg, not only the first (live 2026-10-21: a survey fleet left AINELRAMU for PHEVENIA while
                    # its recall withdrew survey controller 769B37CC, which had not boarded yet: it stayed behind)
                    ros = fl.roster(fleet, devices)
                    loose = [d["device_code"] for d in ros["loose"]]
                    partial = (m.get("opts") or {}).get("partial")
                    board_steps, board_problems = (await self.fleet_phase_steps(fleet, m, "assemble", devices)
                                                   if loose else ([], []))
                    if loose and not board_steps and board_problems and ros["carriers"] and not partial:
                        # nobody can board (live 2026-10-23..25: Surveyor 2 waited 41 h at FALQUORYX with its vessel's
                        # hold full, logging the same problem every pass): stall, so it's heard about and retried
                        note = f"waiting to leave: {'; '.join(board_problems)} — Launch anyway sends it short"
                        m["status"], m["job"], m["watch_note"] = "stalled", None, note
                        again, quiet = self._plan_retry(m)
                        self._mlog(m, note + again)
                        await self.log("fleets", f"{fleet['name']}: stalled before leaving: {'; '.join(board_problems)}"
                                       + again, "alert", notify=not quiet)
                        break
                    if loose and (board_steps or not partial):
                        # Launch anyway sends it short, but still takes along members standing right there
                        # together in one system isn't aboard (seen live 2026-10-11: Surveyor 3's gather failed while its
                        # vessel was still cruising in, a retry then flew the empty vessel to SARKARUS): board them first
                        note = f"waiting to leave: {len(loose)} not aboard yet ({', '.join(loose[:6])}) — boarding them"
                        if m.get("watch_note") != note:
                            m["watch_note"] = note
                            self._mlog(m, note)
                            changed = True
                        steps, problems = board_steps, board_problems
                        for p_ in problems:
                            self._mlog(m, p_)
                        title = "board before leaving"
                        if not steps and m.get("departed"):
                            # members left in a system with no fleet carrier (assemble leaves them to the gather phase):
                            # a carrier fetches them, then this same leg. Live 2026-11-10: Surveyor 10's crew sat at
                            # SYRAXOS with its vessel back at SOLARES; it waited in recall for good
                            steps, problems = await self.fleet_phase_steps(fleet, m, "gather", devices)
                            for p_ in problems:
                                self._mlog(m, p_)
                            title = "fetch members left behind"
                        if steps:
                            if m.get("departed"):   # a later leg: board, then this same leg (next_phase)
                                m["phase"], m["board_next"] = "board", nxt
                            else:
                                m["phase"] = "assemble"
                            job = await self.create_job("fleets", f"{fleet['name']}: {title}", None, steps,
                                                        {"devices": [d["device_code"] for d in fl.members(fleet, devices)],
                                                         "fleet": fleet["id"]}, force=True)
                            m["job"] = job["id"] if job else None
                        elif m.get("departed"):
                            # nothing to send: stay on this target. Before, next_phase had already moved on and the
                            # next pass appended another star, so a scouting fleet ran through 124 targets in recall
                            # without leaving (each one then counted as taken for every other survey fleet)
                            m["idx"] = idx_was
                            note = f"waiting to leave: {len(loose)} not aboard and no carrier can fetch them ({', '.join(loose[:6])})"
                            if problems:
                                note += f": {'; '.join(problems)}"
                            m["status"], m["job"], m["watch_note"] = "stalled", None, note
                            again, quiet = self._plan_retry(m)
                            self._mlog(m, note + again)
                            await self.log("fleets", f"{fleet['name']}: stalled: {note}" + again, "alert", notify=not quiet)
                        else:
                            m["idx"] = idx_was
                        break
                    m["departed"] = True
                    m.pop("watch_note", None)
                m["phase"], m["phase_at"] = nxt, now_iso()
                if nxt == "watch":
                    m.pop("auto_retry", None)   # got past whatever stalled it
                    self._mlog(m, "on station — watching until the work is done")
                    break
                if nxt == "wait":
                    changed = True
                    gate, note = await self._deal_gate(fleet, m, devices, arriving=True)
                    if gate == "rewind":
                        continue   # short at the site: deliver / gather again first
                    if gate == "wait":
                        m["watch_note"] = note
                        break
                    continue   # everything there and the replicant too: fulfill now
                steps, problems = await self.fleet_phase_steps(fleet, m, nxt, devices)
                for pr in problems:
                    self._mlog(m, f"{nxt}: {pr}")
                if m.pop("stall", False):
                    m["status"], m["job"] = "stalled", None
                    again, quiet = self._plan_retry(m)
                    if again:
                        self._mlog(m, again.lstrip(" —"))
                    await self.log("fleets", f"{fleet['name']}: stalled in {nxt}: {'; '.join(problems)}" + again, "alert",
                                   notify=not quiet)
                    break
                if not steps:
                    continue
                codes = [d["device_code"] for d in fl.members(fleet, devices)]
                job = await self.create_job("fleets", f"{fleet['name']}: {nxt}", None, steps, {"devices": codes, "fleet": fleet["id"]}, force=True)
                m["job"] = job["id"] if job else None
                self._mlog(m, f"{nxt}: {len(steps)} step(s)")
                break
            changed = True
        if changed:
            await self.save_fleets(items)
