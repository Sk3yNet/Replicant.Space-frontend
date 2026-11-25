"""Station jobs (game v4.0.0): the Jobs page, the runner and its logistics rows."""
from rsweb import stations as sj
from test_logistics import BPS, FLEETS, POS, SURGE, dev
from test_ops import H, HX, client, eng  # noqa: F401  (fixture)

STATIONS = [{"slug": "new_havana", "name": "New Havana Station", "location": "A-OBJ-2",
             "factions": [{"type": "crew", "name": "The Mechanics", "influence": 1.5}]}]


def test_objective_and_kind_read_widely():
    j = {"id": 1, "station_slug": "new_havana", "objectives": {"type": "gather_resources", "station_location": "A-OBJ-2",
                                                             "resource": "carbon", "quantity": 120}}
    ob = sj.objective(j, STATIONS)
    assert ob["kind"] == "gather" and ob["at"] == "A-OBJ-2" and ob["qty"] == 120 and ob["complete_at"] == ["A-OBJ-2"]
    assert sj.progress(ob, {"A-OBJ-2": {"carbon": 130}}, [])[0] is True
    assert sj.kind_of({"objectives_summary": "Transport colony_shuttle to Tianjin Station"}) == "courier"
    assert sj.kind_of({"objectives_summary": "Scan ALKALAROPS-4 and return to station"}) == "scan"
    c = sj.objective({"station_slug": "new_havana", "provided": {"device_code": "D1"},
                      "objectives": {"type": "courier", "destination_station": "new_havana"}}, STATIONS)
    assert c["device_code"] == "D1" and c["dest"] == "A-OBJ-2"
    assert sj.factions(STATIONS[0])["crew"]["name"] == "The Mechanics"


def test_pick_and_blocked_rows():
    mine = {"jobs": [], "reputation": {}}
    boards = {"new_havana": {"jobs": [{"id": 1, "faction": "crew", "tier": 2, "objectives_summary": "Gather 5 carbon", "reputation_reward": 20},
                                      {"id": 2, "faction": "trader", "tier": 1, "objectives_summary": "Gather 5 carbon", "reputation_reward": 10},
                                      {"id": 3, "faction": "leader", "tier": 1, "objectives_summary": "Scan A-4", "reputation_reward": 10}]}}
    rows = sj.board_rows(STATIONS, boards, mine)
    assert "needs 50 reputation" in rows[0]["blocked"] and not rows[1]["blocked"]
    assert sj.pick(rows, {**sj.DEFAULT_RUNNER})["id"] == 2   # tier 2 blocked, scan not runnable
    busy = sj.board_rows(STATIONS, boards, {"jobs": [{"id": 9, "status": "accepted"}]})
    assert all(r["blocked"] == "you hold a job already" for r in busy)


def test_job_rows_go_to_logistics_as_urgent():
    """Joe 2026-11-17: a station job must be done within its 24 h, so its resources go ahead of other work."""
    mine = {"jobs": [{"id": 7, "status": "accepted", "title": "Supply", "station_slug": "new_havana",
                      "expires_at": "2026-11-18T10:00:00Z",
                      "objectives": {"type": "gather_resources", "station_location": "A-OBJ-2", "resource": "carbon", "quantity": 50}}]}
    assert not sj.logistics_rows(mine, {"run": []}, STATIONS)
    rows = sj.logistics_rows(mine, {"run": ["7"]}, STATIONS)
    assert rows[0]["location"] == "A-OBJ-2" and rows[0]["target"] == {"carbon": 50}
    from rsweb import logistics as lg
    devs = [dev("F1", "cargo_freighter", "A-1", cargo_capacity=1000, **SURGE), dev("HV", "heaven_vessel", "A-1", **SURGE)]
    p = lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory={"A-1": {"carbon": 500}}, pos=POS, commandable={"A", "B"},
                hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[], trades=[], incoming={},
                replicants={"R1": {"location": "A-1"}}, drop=lambda s: f"{s}-1", upkeep=rows)
    r = next(r for r in p["requests"] if r["id"] == "job:job7")
    assert r["need"] == {"carbon": 50} and r["category"] == "stations" and r["urgent"] == "station job"
    assert "station job" in r["label"] and "due 11-18 10:00" in r["label"]
    assert next(x for x in p["runs"] if x["req"] == "job:job7")["to"] == "A-OBJ-2"


def _at_station(client, loc="ALKALAROPS-OBJ-2"):
    db = client.app.state.db
    reps = client.portal.call(db.kv_get, "replicants") or {}
    code = next(iter(reps))
    reps[code] = {**reps[code], "location": loc}
    client.portal.call(db.kv_set, "replicants", reps)


def test_boards_read_only_where_a_replicant_is(client):  # noqa: F811
    db = client.app.state.db
    client.post("/jobs/refresh", headers=HX)   # nobody at a station: no board read
    boards = client.portal.call(db.kv_get, sj.KV_BOARDS)
    assert boards["new_havana"]["away"] and not boards["new_havana"].get("jobs")
    assert "a replicant at ALKALAROPS-OBJ-2 reads it" in client.get("/jobs", headers=H).text
    _at_station(client)
    client.post("/jobs/refresh", headers=HX)
    assert client.portal.call(db.kv_get, sj.KV_BOARDS)["new_havana"]["jobs"]
    _at_station(client, "SOL-3")                # gone again: the last board read is kept, marked as such
    client.post("/jobs/refresh", headers=HX)
    b = client.portal.call(db.kv_get, sj.KV_BOARDS)["new_havana"]
    assert b["away"] and b["jobs"]
    assert "read " in client.get("/jobs", headers=H).text


def test_jobs_page_accept_run_and_complete(client):  # noqa: F811
    e, db = eng(client), client.app.state.db
    _at_station(client)
    client.post("/jobs/refresh", headers=HX)
    page = client.get("/jobs", headers=H).text
    assert "Structural Supply Run" in page and "New Havana Station" in page and "The Mechanics" in page
    assert "No job accepted" in page
    r = client.post("/jobs/42/accept", data={"run": "1"}, headers=HX)
    assert r.status_code == 200
    cfg = client.portal.call(sj.runner_cfg, db)
    assert cfg["run"] == ["42"]
    page = client.get("/jobs", headers=H).text
    assert "Runner works it" in page and "0 of 120 structural at ALKALAROPS-OBJ-2" in page
    assert "you hold a job already" in page
    # stocked and a replicant there: the runner completes it
    client.portal.call(db.kv_set, "inventory", [{"location": "ALKALAROPS-OBJ-2", "items": {"structural": 150}}])
    out = client.portal.call(e.station_jobs_pass)
    assert out == ["completed job 42"]
    assert not sj.accepted(client.portal.call(db.kv_get, sj.KV_MINE))
    assert "Contributor" in client.get("/jobs/station/new_havana", headers=HX).text


def test_abandon_and_auto_accept(client):  # noqa: F811
    e, db = eng(client), client.app.state.db
    _at_station(client)
    client.post("/jobs/refresh", headers=HX)
    client.post("/jobs/43/accept", headers=HX)
    client.post("/jobs/43/abandon", headers=HX)
    assert not sj.accepted(client.portal.call(db.kv_get, sj.KV_MINE))
    client.post("/jobs/settings", data={"auto": "1", "factions": ["crew"], "kinds": ["gather"]}, headers=HX)
    out = client.portal.call(e.station_jobs_pass)
    assert out == ["accepted job 42"]
    assert client.portal.call(sj.runner_cfg, db)["run"] == ["42"]


def test_job_resources_follow_evacuations_and_lead_sol_trades():
    rows = [{"code": "job7", "location": "A-OBJ-2", "target": {"carbon": 50}, "kind": "job", "urgent": True,
             "due": "2026-11-18T10:00:00+00:00", "label": "station job"}]
    sol = {"key": "S:T1", "location": "SOL-3", "price": {"carbon": 30}, "controller": "S", "trade_code": "T1", "name": "T1"}
    from rsweb import logistics as lg
    from test_logistics import world
    reqs, _, _ = lg.requests(devices=world(), bps=BPS, inventory={"Z-1": {"carbon": 50}}, pos=POS, contracts=[],
                             trades=[sol], incoming={}, fleets=FLEETS, gather={"A": "A-1"}, busy_stars=set(),
                             evacuate={"Z-1": "A-1"}, upkeep=rows)
    assert [r["id"] for r in reqs[:3]] == ["evacuate:Z-1", "job:job7", "trade:S:T1"]


def test_devices_for_a_running_deploy_or_courier_job_are_urgent():
    mine = {"jobs": [{"id": 8, "status": "accepted", "station_slug": "new_havana",
                      "objectives": {"type": "deploy_devices", "station_location": "A-OBJ-2", "device_type": "ftl_relay"}}]}
    assert sj.urgent_pins(mine, {"run": []}, STATIONS) == set()
    pins = sj.urgent_pins(mine, {"run": ["8"]}, STATIONS)
    assert pins == {"A-OBJ-2"}
    from rsweb import loadouts as lo
    devs = [{"device_code": "R1", "device_type": "ftl_relay", "location": "B-4", "tags": ["to:a", "at:a-obj-2"]},
            {"device_code": "R2", "device_type": "ftl_relay", "location": "A-OBJ-2", "tags": ["at:a-obj-2"]},
            {"device_code": "R3", "device_type": "ftl_relay", "location": "B-4", "tags": ["at:a-2"]}]
    assert lo.priority_devices(devs, pins) == {"R1"} and lo.priority_devices(devs) == set()


def test_station_job_prints_go_first():
    """Joe 2026-11-17: prints a deploy job needs jump the print queue, and their materials are urgent."""
    from rsweb import logistics as lg
    from rsweb import vqueue as vqm
    body = {"device_type": "ftl_relay", "tags": ["to:a", "at:a-obj-2", vqm.JOB_TAG]}
    assert vqm.priority(body) == 0.5 and vqm.label(0.5) == "station job"
    assert vqm.priority(body) < vqm.priority({"device_type": "mining_drone", "tags": ["component", "prio:1.5"]})
    assert vqm.priority({"device_type": "x", "tags": ["fleet:1"]}, hand=True) < vqm.priority(body)
    bps = {"ftl_relay": {"device_type": "ftl_relay", "resources": {"carbon": 40}}}
    fac = {"device_code": "AF", "device_type": "autofactory", "location": "A-1", "status": "idle",
           "print_queue": [{"device_type": "ftl_relay", "quantity": 1, "tags": body["tags"]}]}
    assert lg.print_costs([fac], bps)["A-1"]["job"] is True
    reqs, _, _ = lg.requests(devices=[fac], bps=bps, inventory={}, pos=POS, contracts=[], trades=[], incoming={},
                             fleets=FLEETS, gather={}, busy_stars=set())
    r = next(r for r in reqs if r["id"] == "print:A-1")
    assert r["tier"] == 0 and r["urgent"] == "station job print"


def _station_plan(station_carriers=frozenset(), upkeep=None, inv=None, lanes=None):
    from rsweb import logistics as lg
    devs = [dev("F1", "cargo_freighter", "A-1", cargo_capacity=100, **SURGE),
            dev("F2", "cargo_freighter", "A-1", cargo_capacity=100, **SURGE),
            dev("HV", "heaven_vessel", "A-1", **SURGE)]
    rows = upkeep if upkeep is not None else [
        {"code": "job7", "location": "B-OBJ-1", "target": {"carbon": 50}, "kind": "job", "urgent": True, "station": True,
         "source": "A-OBJ-2", "label": "station job"}]
    return lg.plan(fleets=FLEETS, devices=devs, bps=BPS, inventory=inv or {"A-OBJ-2": {"carbon": 50}, "A-1": {"carbon": 500}},
                   pos=POS, commandable={"A", "B"}, hosts={"HV": "R1"}, busy=set(), reach=100, contracts=[], trades=[],
                   incoming={}, replicants={"R1": {"location": "A-1"}}, drop=lambda s: f"{s}-1", upkeep=rows,
                   station_carriers=set(station_carriers), lanes=lanes)


def test_station_jobs_are_a_category_their_carriers_take_first():
    """Joe 2026-11-24: one heaven vessel and one cargo ship work station jobs first, other categories after."""
    p = _station_plan({"HV", "F2"})
    r = next(x for x in p["requests"] if x["id"] == "job:job7")
    assert r["category"] == "stations"
    run = next(x for x in p["runs"] if x["req"] == "job:job7")
    assert "F2" in [c["code"] for c in run["cargos"]] and "F1" not in [c["code"] for c in run["cargos"]]
    # the handed-over pile at the station (a STAR-OBJ-n spot, not a rock) is the source, never a stockpile return
    assert [leg["pile"] for c in run["cargos"] for leg in c["legs"]] == ["A-OBJ-2"]
    assert not any(x["id"] == "return:A-OBJ-2" for x in p["requests"])
    assert p["lanes"]["F2"] == "stations" and p["lanes"].get("F1") != "stations"
    # no station work: the same carriers take other categories
    p = _station_plan({"HV", "F2"}, upkeep=[], lanes={"F2": "stations"})
    assert p["lanes"].get("F2") != "stations"


def test_auto_accept_skips_jobs_outside_the_systems_you_command():
    """Job 96 (2026-11-18): 199 conductive for Novgorod Station at ARUCALIS, out of reach, never went and the job failed."""
    sts = STATIONS + [{"slug": "novgorod", "name": "Novgorod Station", "location": "Z-OBJ-1"}]
    boards = {"new_havana": {"jobs": [
        {"id": 5, "faction": "trader", "tier": 1, "reputation_reward": 10, "station_slug": "new_havana",
         "objectives": {"type": "deliver_resources", "destination": "Z-OBJ-1", "resource": "carbon", "quantity": 9}},
        {"id": 6, "faction": "trader", "tier": 1, "reputation_reward": 10, "station_slug": "new_havana",
         "objectives": {"type": "gather_resources", "station_location": "A-OBJ-2", "resource": "carbon", "quantity": 9}}]}}
    rows = sj.board_rows(sts, boards, {"jobs": []})
    assert sj.pick(rows, {**sj.DEFAULT_RUNNER}, sts, {"A"})["id"] == 6
    assert sj.pick(rows, {**sj.DEFAULT_RUNNER}, sts, {"A", "Z"})["id"] == 5
    mine = {"jobs": [{"id": 5, "status": "accepted", "station_slug": "new_havana", **boards["new_havana"]["jobs"][0]}]}
    row = sj.logistics_rows(mine, {"run": ["5"]}, sts)[0]
    assert row["location"] == "Z-OBJ-1" and row["source"] == "A-OBJ-2" and row["station"]


def test_station_carriers_are_picked_from_the_logistics_fleet_and_kept():
    pool = [dev("HV", "heaven_vessel", "A-1"), dev("HV2", "heaven_vessel", "A-1"), dev("CV", "cargo_vessel", "A-1"),
            dev("F1", "cargo_freighter", "A-1", cargo_capacity=200), dev("F2", "cargo_freighter", "A-1", cargo_capacity=500)]
    hosts = {"HV": "R1", "HV2": "R2", "CV": "R3"}
    assert sj.pick_carriers({}, pool, hosts, {"R2"}) == {"vessel": "HV2", "cargo": "F2"}   # a freighter (Joe 2026-11-25)
    assert sj.pick_carriers({"carriers": {"vessel": "HV", "cargo": "F1"}}, pool, hosts) == {"vessel": "HV", "cargo": "F1"}
    assert sj.pick_carriers({"carriers": {"vessel": "GONE", "cargo": "CV"}}, pool[2:], {"CV": "R3"}) == {"cargo": "F2"}


class _TourEng:
    def __init__(self, boards):
        self.kv, self.made = {sj.KV_BOARDS: boards}, []
        eng = self

        class _Db:
            async def kv_get(self, k, default=None):
                return eng.kv.get(k, default)
        self.db = _Db()

    async def jobs(self):
        return []

    async def commandable(self, devices, stars=None):
        return {"A", "B", "Z"}

    async def star_lookup(self, what):
        return POS

    async def create_job(self, rule, title, device, steps, meta=None, force=False):
        self.made.append((title, steps[0]["body"]["destination"]))
        return {"id": "j1"}


def test_the_station_jobs_vessel_tours_the_stations_and_stays_while_a_job_is_open():
    """Joe 2026-11-25: fulfill the jobs at a station, then move on to the next station when none are available."""
    import asyncio
    sts = [{"slug": "a", "name": "A Station", "location": "A-OBJ-1"}, {"slug": "b", "name": "B Station", "location": "B-OBJ-2"},
           {"slug": "z", "name": "Z Station", "location": "Z-OBJ-1"}]
    vessel = dev("HV", "heaven_vessel", "A-OBJ-1", hosting_replicant={"replicant_code": "R1"})
    cfg = {**sj.DEFAULT_RUNNER, "auto": True, "carriers": {"vessel": "HV"}, "tour": {}}
    job = {"id": 1, "faction": "crew", "tier": 1, "reputation_reward": 10,
           "objectives": {"type": "gather_resources", "station_location": "A-OBJ-1", "resource": "carbon", "quantity": 5}}
    e = _TourEng({"a": {"at": "2000-01-01T00:00:00+00:00", "jobs": [job]}})
    run = lambda: asyncio.run(sj.tour(e, cfg, sts, {"jobs": []}, [vessel], {}))  # noqa: E731
    assert "reading its board" in run() and cfg["tour"]["at"] == "a"
    assert run() is None and not e.made                              # board not read since it came
    e.kv[sj.KV_BOARDS]["a"]["at"] = "2999-01-01T00:00:00+00:00"
    assert run() is None and not e.made                              # a job it can take: stays
    e.kv[sj.KV_BOARDS]["a"]["jobs"] = []
    note = run()
    assert e.made == [("station jobs tour: HV → B Station", "B-OBJ-2")] and "no job to take at A Station" in note
    # the next one: never visited first, then the nearest
    assert sj.tour_next(sts, {"tour": {"visited": {"b": "2026-11-25T00:00:00"}}}, "a", "A", {"A", "B", "Z"}, POS)["slug"] == "z"
    assert sj.tour_next(sts, {}, "a", "A", {"A", "B"}, POS)["slug"] == "b"
    assert sj.tour_next(sts, {"stations": ["a"]}, "a", "A", {"A", "B"}, POS) is None
