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


def test_job_rows_go_to_logistics_as_a_deal():
    mine = {"jobs": [{"id": 7, "status": "accepted", "title": "Supply", "station_slug": "new_havana",
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
    assert r["need"] == {"carbon": 50} and r["category"] == "deals" and "station job" in r["label"]
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
