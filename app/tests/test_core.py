"""Core behavior: SSE parsing, rate limiting, safety blocks, timers, notifications, digest, auth."""
from corelib import *  # noqa: F401,F403


def test_parse_sse_merges_id_and_skips_keepalive():
    text = ('id: 1-0\nevent: mining.started\ndata: {"event":"mining.started","payload":{"a":1}}\n\n'
            ": keepalive\n\n"
            'id: 2-0\nevent: travel.arrived\ndata: {"payload":{}}\n\n')

    async def collect():
        return [e async for e in parse_sse(_lines(text))]

    evs = run(collect())
    assert [e["id"] for e in evs] == ["1-0", "2-0"]
    assert evs[1]["event"] == "travel.arrived"  # filled from the SSE event: line


def test_bucket_reserve_and_refill():
    b = Bucket(60)
    for _ in range(50):
        assert b.wait_time() == 0
        b.take()
    # 10 left: background with reserve 20 must wait, interactive need not.
    assert b.wait_time(reserve=20) > 0
    assert b.wait_time() == 0
    assert b.used_last_minute() == 50


def test_blocked_paths():
    for m, p in [("DELETE", "/accounts/me"), ("DELETE", "/v1/accounts/me/"), ("POST", "/accounts/recover")]:
        with pytest.raises(ApiError):
            RSClient.check_allowed(m, p)
    RSClient.check_allowed("GET", "/accounts/me")
    RSClient.check_allowed("PATCH", "/accounts/me")


def test_429_backoff_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"code": 429, "status": "Too Many Requests"})
        return httpx.Response(200, json={"ok": True}, headers={"X-Replicant-Space-Unread-Count": "3"})

    async def go():
        c = RSClient(Settings(api_token="t", api_base="http://x/v1"), transport=httpx.MockTransport(handler))
        body = await c.get("/accounts/me")
        await c.close()
        return body, c

    body, c = run(go())
    assert body == {"ok": True} and calls["n"] == 2 and c.unread_count == 3


def test_error_envelope():
    def handler(request):
        return httpx.Response(400, json={"error": "Insufficient conductive resource"})

    async def go():
        c = RSClient(Settings(api_token="t", api_base="http://x/v1"), transport=httpx.MockTransport(handler))
        try:
            await c.post("/replicants/X/print", {"device_type": "mining_drone"})
        finally:
            await c.close()

    with pytest.raises(ApiError) as ei:
        run(go())
    assert ei.value.status == 400 and "conductive" in ei.value.message


def test_visit_baseline(tmp_path):
    async def go():
        db = DB(str(tmp_path / "v.sqlite"))
        await db.open()
        first = await notify.touch_visit(db, "a@b.c", 30)
        assert first["new_visit"]
        again = await notify.touch_visit(db, "a@b.c", 30)
        assert not again["new_visit"] and again["baseline_at"] == first["baseline_at"]
        old = iso(datetime.now(timezone.utc) - timedelta(hours=3))
        await db.execute("UPDATE visitors SET last_seen_at=?, digest_dismissed=1", (old,))
        later = await notify.touch_visit(db, "a@b.c", 30)
        await db.close()
        return later, old

    later, old = run(go())
    assert later["new_visit"] and later["baseline_at"] == old and later["digest_dismissed"] == 0


def test_auth_required(client):
    assert client.get("/").status_code == 401
    assert client.get("/", headers={"X-Auth-Request-Email": "evil@example.com"}).status_code == 403
    # state-changing request without HX-Request header is refused (CSRF guard)
    assert client.post("/digest/dismiss", headers=H).status_code == 403


@pytest.mark.parametrize("path", ["/", "/fleet", "/devices/2AC61210", "/replicants/77F75255", "/systems", "/systems/SOL",
                                  "/map", "/api/map.json", "/blueprints", "/ami", "/events", "/notifications",
                                  "/messages", "/account", "/console", "/digest?hours=24", "/locations/SOL-BELT-1",
                                  "/traffic", "/defence", "/maintenance", "/shop", "/automations"])
def test_pages_render(client, path):
    r = client.get(path, headers=H)
    assert r.status_code == 200, r.text[:500]


def test_actions(client):
    r = client.post("/replicants/77F75255/travel", data={"destination": "abotein", "dry_run": "1"}, headers=HX)
    assert "Go — travel to ABOTEIN" in r.text
    r = client.post("/replicants/77F75255/travel", data={"destination": "ABOTEIN"}, headers=HX)
    assert "result ok" in r.text and "Arrives" in r.text
    r = client.post("/devices/2AC61212/command", data={"command": "nope", "args": ""}, headers=HX)
    assert "result err" in r.text and "not available" in r.text
    r = client.post("/console", data={"method": "DELETE", "path": "/accounts/me", "body": ""}, headers=HX)
    assert "blocked" in r.text
    r = client.post("/blueprints/plan", data={"location": "SOL-BELT-1", "qty:mining_drone": "2"}, headers=HX)
    assert "structural" in r.text
    timers = client.get("/partials/timers", headers=HX).text
    assert "ABOTEIN" in timers
    acts = client.get("/account", headers=H).text
    assert "/replicants/77F75255/travel" in acts


def test_as_amounts_shapes():
    from rsweb.shapes import as_amounts, normalize_inventory
    assert as_amounts({"carbon": 5, "rares": "2"}) == {"carbon": 5.0, "rares": 2.0}
    assert as_amounts([{"resource": "carbon", "quantity": 5}, {"resource": "carbon", "quantity": 1}]) == {"carbon": 6.0}
    assert as_amounts([{"name": "rares", "amount": 3}]) == {"rares": 3.0}
    assert as_amounts([["silicates", 4]]) == {"silicates": 4.0}
    assert as_amounts([{"structural": 9}]) == {"structural": 9.0}
    assert as_amounts(None) == {} and as_amounts("junk") == {}
    inv = normalize_inventory([{"location": "SOL-BELT-1", "items": [{"resource": "carbon", "quantity": 2}]}])
    assert inv[0]["items"] == {"carbon": 2.0}


def test_parse_fields_builds_nested_bodies():
    from rsweb import commands as c
    body = c.parse_fields(c.COMMANDS["enqueue_print"], _Form({
        "f.device_type": "mining_drone", "f.quantity": "3", "f.tags": "fleet-1, belt",
        "f.oncomplete.command": "travel", "f.oncomplete.destination": "sol-belt-1",
        "f.oncomplete.resource_type": "carbon", "f.flatpack": "false"}))
    assert body == {"device_type": "mining_drone", "quantity": 3, "tags": ["fleet-1", "belt"],
                    "oncomplete": {"command": "travel", "destination": "SOL-BELT-1"}, "flatpack": False}
    body = c.parse_fields(c.COMMANDS["enqueue_print"], _Form({"f.device_type": "x", "f.oncomplete.command": ""}))
    assert "oncomplete" not in body
    body = c.parse_fields(c.DIRECTIVES["transport"]["delivery"], _Form({
        "f.route.collect": "SOL-BELT-1", "f.route.deliver": "SOL-3-L4", "f.requirement.carbon": "50"}))
    assert body == {"route": {"collect": "SOL-BELT-1", "deliver": "SOL-3-L4"}, "requirement": {"carbon": 50}}
    body = c.parse_fields(c.DIRECTIVES["mining"]["maintain_ratios"], _Form({"f.ratios.structural": "0.5", "f.ratios.rares": "0.1"}))
    assert body == {"ratios": {"structural": 0.5, "rares": 0.1}}
    with pytest.raises(c.FormError):
        c.parse_fields(c.COMMANDS["travel"], _Form({}))
    assert c.parse_fields(c.COMMANDS["adopt"], _Form({"f.devices": ["A", "B"]})) == {"devices": ["A", "B"]}


def test_command_forms_render_with_suggestions(client):
    r = client.get("/devices/2AC61212/command-form?command=travel", headers=HX)
    assert r.status_code == 200 and 'list="c-2AC61212-loc-sys"' in r.text and "SOL-BELT-1" in r.text and "SOL-3-L4" in r.text
    r = client.get("/devices/AF00BEEF/command-form?command=enqueue_print", headers=HX)
    assert "mining_drone" in r.text and "MC91FF22" in r.text  # device types + AMI controller choices
    r = client.get("/devices/MC91FF22/command-form?command=adopt", headers=HX)
    assert 'type="checkbox"' in r.text and "2AC61210" in r.text
    r = client.get("/devices/2AC61212/command-form?command=mystery", headers=HX)
    assert "No field list" in r.text
    r = client.get("/ami/MC91FF22/directive-form?directive=gather_resources", headers=HX)
    assert 'name="f..structural"' in r.text
    # submit through the fields
    r = client.post("/devices/2AC61212/command", data={"command": "start_mining", "f.resource_type": "rares"}, headers=HX)
    assert "result ok" in r.text
    r = client.post("/devices/2AC61212/command", data={"command": "travel"}, headers=HX)
    assert "destination is required" in r.text
    r = client.post("/ami/MC91FF22/directive", data={"directive": "gather_resources", "f..structural": "500"}, headers=HX)
    assert "result ok" in r.text
    acts = client.get("/account", headers=H).text  # audit log shows the body that was sent
    assert "&#34;configuration&#34;: {&#34;structural&#34;: 500}" in acts


def test_duplicate_timers_collapse_but_parallel_devices_stay():
    from rsweb.ingest import duplicate_timers
    t0 = "2026-09-30T12:00:00+00:00"
    t1 = "2026-09-30T12:00:05+00:00"
    rows = [
        # same trip reported three ways
        {"key": "travel:77F75255", "kind": "travel", "label": "77F75255 → ABOTEIN", "device_code": "77F75255", "ends_at": t0, "source": "action"},
        {"key": "travel:11ADA230", "kind": "travel", "label": "heaven vessel 11ADA230 → ABOTEIN", "device_code": "11ADA230", "ends_at": t1, "source": "travel.departed"},
        {"key": "travel:REP", "kind": "travel", "label": "device REP → ABOTEIN", "device_code": None, "ends_at": t1, "source": "travel.departed"},
        # two drones launched together to the same belt: both real
        {"key": "travel:D1", "kind": "travel", "label": "mining drone D1 → SOL-BELT-1", "device_code": "D1", "ends_at": t0, "source": "travel.departed"},
        {"key": "travel:D2", "kind": "travel", "label": "mining drone D2 → SOL-BELT-1", "device_code": "D2", "ends_at": t0, "source": "travel.departed"},
        # a print finishing at the same moment is a different kind
        {"key": "print:11ADA230", "kind": "print", "label": "print mining_drone @ SOL", "device_code": "11ADA230", "ends_at": t0, "source": "print.started"},
    ]
    kept, dups = duplicate_timers(rows)
    assert sorted(dups) == ["travel:77F75255", "travel:REP"]
    assert {k["key"] for k in kept} == {"travel:11ADA230", "travel:D1", "travel:D2", "print:11ADA230"}


def test_worker_removes_duplicate_timer_rows(tmp_path):
    async def go():
        db = DB(str(tmp_path / "d.sqlite")); await db.open()
        s = Settings(api_token="t", api_base="http://x/v1")
        w = Worker(s, db, RSClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))), Hub())
        end = datetime.now(timezone.utc) + timedelta(minutes=5)
        await w.handle_event({"id": "1-0", "event": "travel.departed", "device_code": "11ADA230", "device_type": "heaven_vessel",
                              "payload": {"destination": "ABOTEIN", "arrives_at": iso(end)}, "created_at": iso(datetime.now(timezone.utc))})
        await w.handle_event({"id": "2-0", "event": "travel.departed", "device_code": None, "replicant_code": "77F75255",
                              "payload": {"destination": "ABOTEIN", "arrives_at": iso(end)}, "created_at": iso(datetime.now(timezone.utc))})
        rows = await db.fetchall("SELECT key FROM timers")
        await db.close()
        return rows
    assert [r["key"] for r in run(go())] == ["travel:11ADA230"]


def test_dry_run_sends_nothing(client):
    eng = client.app.state.worker.automations
    client.post("/automations/dry-run", data={"dry_run": "on"}, headers=HX)
    client.post("/automations/rules/auto_survey", data={"enabled": "on", "max_targets": "2"}, headers=HX)
    before = client.portal.call(client.app.state.db.fetchone, "SELECT COUNT(*) n FROM actions WHERE user='automation'")
    r = client.post("/automations/survey-now", data={"vessel": "11ADA230"}, headers=HX)
    assert r.status_code == 200
    arrival = {"id": "8888888888888-0", "event": "travel.arrived", "device_code": "11ADA230", "device_type": "heaven_vessel",
               "location": "SOL-BELT-1", "payload": {"destination": "SOL-BELT-1", "origin": "ABOTEIN-OORT", "travel_type": "surge"}, "created_at": iso(datetime.now(timezone.utc))}
    client.portal.call(client.app.state.worker.handle_event, arrival)
    after = client.portal.call(client.app.state.db.fetchone, "SELECT COUNT(*) n FROM actions WHERE user='automation'")
    assert after["n"] == before["n"] == 0
    assert client.portal.call(eng.jobs) == []
    page = client.get("/automations", headers=H).text
    assert "[dry run] survey SOL" in page

def test_travel_chain_on_device(client):
    world = client.app.state.api.http._transport.app.state.world
    world.move_seconds = 0.03
    client._seen = len(world.events)
    r = client.get("/devices/2AC61212/command-form?command=travel", headers=HX)
    assert "Then, on arrival" in r.text and 'name="then_command_0"' in r.text and "start mining" in r.text
    r = client.post("/devices/2AC61212/command", data={
        "command": "travel", "f.destination": "SOL-3-L4",
        "then_device_0": "__self__", "then_command_0": "start_mining", "then_arg_0": "carbon"}, headers=HX)
    assert "follow on Automations" in r.text, r.text
    eng = client.app.state.worker.automations

    async def finished():
        return all(j["status"] not in ("running", "waiting") for j in await eng.jobs())
    _pump(client, finished)
    job = client.portal.call(eng.jobs)[-1]
    assert job["status"] == "done", [(s["desc"], s["status"], s["error"]) for s in job["steps"]]
    acts = client.portal.call(client.app.state.db.fetchall, "SELECT path, body FROM actions WHERE user='automation' ORDER BY id")
    bodies = [a["body"] for a in acts if a["path"] == "/devices/2AC61212"]
    assert '"travel"' in bodies[0] and '"start_mining"' in bodies[1] and '"carbon"' in bodies[1]


def test_chain_validation(client):
    r = client.post("/devices/2AC61212/command", data={
        "command": "travel", "f.destination": "SOL-3-L4", "then_device_0": "__self__", "then_command_0": "start_mining"}, headers=HX)
    assert "needs a resource" in r.text


def test_fill_plan():
    from rsweb.cargo import fill_plan
    assert fill_plan({"structural": 100, "carbon": 50}, 15) == {"structural": 10, "carbon": 5}
    p = fill_plan({"structural": 7, "carbon": 3, "rares": 1}, 5)
    assert sum(p.values()) == 5 and all(p[r] <= q for r, q in {"structural": 7, "carbon": 3, "rares": 1}.items() if r in p)
    assert fill_plan({"carbon": 3}, 50) == {"carbon": 3}          # less stock than space: take it all
    assert fill_plan({"carbon": 30}, 0) == {} and fill_plan({}, 10) == {}


def test_collect_resources_shows_stock_and_collects_all(client):
    r = client.get("/devices/TR000001/command-form?command=collect_resources", headers=HX)
    assert "At <b>SOL-BELT-1</b>" in r.text and "15" in r.text  # 20 capacity - 5 carbon aboard
    assert "Fill to capacity" in r.text and "Collect all now (15 units)" in r.text
    assert 'max="' in r.text and 'data-fill="' in r.text
    r = client.post("/devices/TR000001/collect-all", headers=HX)
    assert "result ok" in r.text and "collect all (15 units)" in r.text
    world = client.app.state.api.http._transport.app.state.world
    tr = next(d for d in world.devices if d["device_code"] == "TR000001")
    assert sum(tr["cargo"].values()) == 20
    r = client.post("/devices/TR000001/collect-all", headers=HX)
    assert "the hold is full" in r.text


def test_build_tree_nesting():
    from rsweb.tree import build_tree
    devs = [
        {"device_code": "V", "device_type": "heaven_vessel", "location": "A-BELT-1", "status": "stationary"},
        {"device_code": "C", "device_type": "cargo_vessel", "location": "A-3-L4", "status": "idle"},
        {"device_code": "S1", "device_type": "survey_drone", "location": "A-BELT-1", "status": "stowed"},
        {"device_code": "S2", "device_type": "surge_plate", "location": "A-3-L4", "status": "stowed"},     # guessed into C
        {"device_code": "M", "device_type": "mining_drone", "location": "A-BELT-1", "status": "mining (rares)"},
        {"device_code": "X", "device_type": "mining_drone", "location": "B-BELT-1", "status": "idle"},
        {"device_code": "Q", "device_type": "probe", "location": "B-2", "status": "stowed"},               # no carrier known
    ]
    reps = {"R1": {"name": "bob-1", "hosted_device_code": "V", "location": "A-BELT-1",
                   "stowed_devices": [{"device_code": "S1"}, {"device_code": "NEW1", "device_type": "replicant_matrix"}]}}
    systems = build_tree(devs, reps, {"C": []}, {"V", "C"})
    a = next(s for s in systems if s["star"] == "A")
    assert systems[0]["star"] == "A"  # system with the replicant first
    top = {n["d"]["device_code"]: n for n in a["nodes"]}
    assert set(top) == {"V", "C", "M"}
    assert {k["d"]["device_code"] for k in top["V"]["children"]} == {"S1", "NEW1"}  # NEW1 known only from the replicant
    assert top["V"]["replicant"]["name"] == "bob-1"
    assert [k["d"]["device_code"] for k in top["C"]["children"]] == ["S2"] and top["C"]["children"][0]["guessed"]
    b = next(s for s in systems if s["star"] == "B")
    assert [n["d"]["device_code"] for n in b["unknown"]] == ["Q"] and b["counts"]["idle"] == 1
    # system → device type → devices; the replicant's host type first
    assert [g["type"] for g in a["groups"]] == ["heaven_vessel", "cargo_vessel", "mining_drone"]
    assert a["groups"][2]["counts"] == {"active": 1}


def test_tree_groups_many_devices_by_type():
    from rsweb.tree import build_tree
    devs = [{"device_code": f"M{i}", "device_type": "mining_drone", "location": "A-BELT-1",
             "status": "idle" if i % 3 == 0 else "mining (carbon)", "operational_capacity": 30 if i == 1 else 90} for i in range(9)]
    devs += [{"device_code": "T1", "device_type": "transport", "location": "A-3", "status": "idle"}]
    a = build_tree(devs, {}, {}, set())[0]
    mining = next(g for g in a["groups"] if g["type"] == "mining_drone")
    assert len(mining["nodes"]) == 9 and mining["counts"] == {"idle": 3, "active": 6, "low": 1}
    assert [g["type"] for g in a["groups"]] == ["mining_drone", "transport"]


def test_ami_schedule_add_run_and_idle_check(client):
    page = client.get("/ami", headers=H).text   # schedules live on the AMI page since 1.15.0
    assert "AMI schedules" in page and 'value="kind:mining"' in page
    r = client.get("/automations/schedule-form?target=TC000001", headers=HX)
    assert '<option value="delivery">' in r.text or 'value="shuttle"' in r.text
    # transport controller TC000001 is idle; TR000001 is an idle transport drone at the same location
    r = client.post("/automations/schedules", data={"target": "TC000001", "directive": "shuttle", "f.collect": "SOL-BELT-1",
                                                    "f.deliver": "SOL-3-L4", "every_minutes": "15", "only_idle": "on",
                                                    "adopt": "on", "launch": "on", "name": "belt shuttle"}, headers=HX)
    assert r.headers.get("HX-Refresh") == "true", r.text
    eng = client.app.state.worker.automations
    sched = client.portal.call(eng.schedules)[0]
    assert sched["configuration"] == {"collect": "SOL-BELT-1", "deliver": "SOL-3-L4"} and sched["every_minutes"] == 15
    r = client.post(f"/automations/schedules/{sched['id']}/run", headers=HX)
    assert "TC000001: started, adopting 1" in r.text, r.text
    job = client.portal.call(eng.jobs)[-1]
    assert [s["body"]["command"] for s in job["steps"]] == ["adopt", "set_directive", "launch"]
    assert job["steps"][0]["body"]["devices"] == ["TR000001"] and job["status"] == "done"
    # the mining controller is coordinating -> skipped while busy, run once its directive completes
    client.post("/automations/schedules", data={"target": "kind:mining", "directive": "gather_evenly", "every_minutes": "30",
                                                "only_idle": "on", "launch": "on"}, headers=HX)
    msched = client.portal.call(eng.schedules)[1]
    r = client.post(f"/automations/schedules/{msched['id']}/run", headers=HX)
    assert "MC91FF22: busy" in r.text
    client.portal.call(client.app.state.worker.handle_event, {"id": "5555555555555-0", "event": "directive.completed",
        "device_code": "MC91FF22", "device_type": "ami_mining_controller", "payload": {"directive": "gather_evenly"},
        "created_at": "2026-09-30T12:00:00+00:00"})
    r = client.post(f"/automations/schedules/{msched['id']}/run", headers=HX)
    assert "MC91FF22: started" in r.text and "last directive completed" in r.text


def test_ami_schedules_need_master_switch_and_respect_interval(client):
    eng = client.app.state.worker.automations
    client.post("/automations/schedules", data={"target": "TC000001", "directive": "consolidate", "f.deliver": "SOL-3-L4",
                                                "every_minutes": "60", "launch": "on"}, headers=HX)
    client.portal.call(eng.tick)
    assert client.portal.call(eng.schedules)[0]["last_run"] is None  # master switch off
    client.post("/automations/rules/ami_schedules", data={"enabled": "on"}, headers=HX)
    client.portal.call(eng.tick)
    first = client.portal.call(eng.schedules)[0]["last_run"]
    assert first
    client.portal.call(eng.tick)
    assert client.portal.call(eng.schedules)[0]["last_run"] == first  # not due again for an hour


def test_material_routes_source_to_nearest_destination():
    from rsweb import loadouts as lo
    stars = {"AAA": {"position": {"x": 0, "y": 0, "z": 0}}, "BBB": {"position": {"x": 3, "y": 0, "z": 0}, "entry_point": "BBB-5-L4"},
             "CCC": {"position": {"x": 9, "y": 0, "z": 0}}, "DDD": {"position": {"x": 20, "y": 0, "z": 0}}}
    ctrl = {"device_code": "TA", "device_type": "ami_transport_controller", "location": "AAA-BELT-1", "status": "idle",
            "available_commands": ["set_directive", "launch", "adopt"]}
    devices = [ctrl,
               {"device_code": "FR1", "device_type": "cargo_freighter", "location": "AAA-BELT-1", "status": "idle", "features": ["surge"]},
               {"device_code": "FR2", "device_type": "cargo_freighter", "location": "AAA-3-L4", "status": "idle", "features": ["surge"]},
               {"device_code": "FB", "device_type": "autofactory", "location": "CCC-3-L4", "available_commands": ["enqueue_print"]}]
    inv = {"AAA-BELT-1": {"carbon": 500}, "AAA-2": {"carbon": 10}, "BBB-4-L5": {"structural": 40}}
    cfg = {"roles": {"AAA": "source", "BBB": "destination", "CCC": "destination", "DDD": "source"}}
    routes, unmet = lo.material_routes(cfg, devices, inv, stars, set(), {}, {})
    assert len(routes) == 1
    r = routes[0]
    assert (r["controller"], r["dest"], r["collect"], r["deliver"]) == ("TA", "BBB", "AAA-BELT-1", "BBB-4-L5")
    assert [a["code"] for a in r["adopt"]] == ["FR1", "FR2"] and r["tag"]
    assert any(u["star"] == "DDD" and "transport controller" in u["why"] for u in unmet)
    bodies = [st["body"] for st in lo.ferry_steps(r)]
    assert bodies[0] == {"configuration": {"add_tags": ["ferry"]}}
    assert {"command": "travel", "destination": "AAA-BELT-1"} in bodies          # FR2 flies over to join
    assert {"command": "adopt", "devices": ["FR1", "FR2"]} in bodies
    assert {"command": "set_directive", "directive": "ferry",
            "configuration": {"collect": "AAA-BELT-1", "deliver": "BBB-4-L5"}} in bodies
    # running the same ferry with all freighters adopted → nothing to do; a new idle freighter → adopt only
    ctrl["tags"] = ["ferry"]
    managed = {"FR1": "TA", "FR2": "TA"}
    cur = {"TA": {"directive": "ferry", "configuration": {"collect": "AAA-BELT-1", "deliver": "BBB-4-L5"}, "finished": False}}
    assert lo.material_routes(cfg, devices, inv, stars, set(), cur, managed)[0] == []
    devices.append({"device_code": "FR3", "device_type": "cargo_freighter", "location": "AAA-BELT-1", "status": "idle"})
    r = lo.material_routes(cfg, devices, inv, stars, set(), cur, managed)[0][0]
    assert [a["code"] for a in r["adopt"]] == ["FR3"] and not r["resend"] and not r["tag"]
    assert not any(st["body"] and st["body"].get("command") == "set_directive" for st in lo.ferry_steps(r))
    # the only controller is on in-system work (consolidate) → it is not given the ferry
    devices2 = [dict(ctrl, tags=[]), {"device_code": "TD1", "device_type": "transport_drone", "location": "AAA-BELT-1", "status": "idle"},
                devices[1]]
    cur2 = {"TA": {"directive": "consolidate", "configuration": {}, "finished": False}}
    routes, unmet = lo.material_routes(cfg, devices2, inv, stars, set(), cur2, {"TD1": "TA"})
    assert routes == [] and any("in-system work" in u["why"] for u in unmet)
    # a controller already ferrying with drones on taxi plates is fine to keep (the game's own ferry does that)
    cur3 = {"TA": {"directive": "ferry", "configuration": {"collect": "X", "deliver": "Y"}, "finished": False}}
    routes, _ = lo.material_routes(cfg, devices2, inv, stars, set(), cur3, {"TD1": "TA"})
    assert routes and routes[0]["controller"] == "TA" and routes[0]["resend"]
    # no freighter at all → says so
    routes, unmet = lo.material_routes(cfg, [dict(ctrl, tags=[])], inv, stars, set(), {}, {})
    assert routes == [] and any("no cargo freighter" in u["why"] for u in unmet)


def test_arrived_from_elsewhere(client):
    eng = client.app.state.worker.automations
    f = lambda p: client.portal.call(eng.arrived_from_elsewhere, {"device_code": "X1", "payload": p}, "SOL")  # noqa: E731
    assert f({"origin": "SOL-3"}) is False
    assert f({"origin": "ABOTEIN-OORT"}) is True
    assert f({"travel_type": "surge_hop"}) is True
    assert f({}) is False      # no origin known at all: treated as a local move



def test_job_stops_after_repeated_failures(client):
    eng = client.app.state.worker.automations
    from rsweb.automations import step
    steps = [step(f"travel {i}", "/devices/NOPE0000", {"command": "travel", "destination": f"SOL-{i}"}) for i in range(10)]
    job = client.portal.call(eng.create_job, "auto_survey", "doomed", "NOPE0000", steps, {}, True)
    job = next(j for j in client.portal.call(eng.jobs) if j["id"] == job["id"])
    assert job["status"] == "failed" and sum(1 for st in job["steps"] if st["status"] == "skipped") == 3


def test_a_watch_waits_for_a_fresh_device_list():
    """Live 2026-10-28: Surveyor 3's watch read a cached list from before the launch, saw no directive and recalled."""
    from rsweb import fleets as fl
    fleet = {"id": "s", "role": "explore", "wants": {}}
    devs = [{"device_code": "SC1", "device_type": "ami_survey_controller", "location": "A-1", "status": "idle",
             "tags": ["fleet:s"]}]
    m = {"phase": "watch", "phase_at": "2026-10-28T19:54:28+00:00"}
    assert not fl.watch_done(fleet, m, devs, "2026-10-28T19:55:33+00:00")[0]
    assert fl.watch_done(fleet, m, devs, "2026-10-28T19:58:00+00:00")[0]


def test_rewind_drops_the_stalled_job():
    """Live 2026-10-28: the auto retry left the failed recall job in place, so each retry stalled again at once one
    phase earlier (watch, work, deploy), each a new 'try 1 of 8' alert."""
    from rsweb import fleets as fl
    m = {"status": "stalled", "phase": "recall", "job": "j1"}
    fl.rewind({"role": "explore"}, m)
    assert "job" not in m and m["phase"] == "watch" and m["recall_now"]


def test_managed_by_uses_controller_device_code(client):
    from rsweb.ami_schedule import managed_by
    db = client.app.state.db

    async def go():
        await db.kv_set("devices", [
            {"device_code": "57C506F0", "device_type": "cargo_freighter", "controller_device_code": "DF451241",
             "features": ["surge", "cruise", "transport"], "cargo_capacity": 500, "cargo_used": 500},
            {"device_code": "FREE0001", "device_type": "cargo_freighter", "controller_device_code": None}])
        return await managed_by(db)
    m = client.portal.call(go)
    assert m.get("57C506F0") == "DF451241" and "FREE0001" not in m
    from rsweb import loadouts as lo
    assert lo.is_freighter({"device_type": "cargo_freighter"}) and not lo.is_transport_controller({"device_type": "cargo_freighter"})


def test_real_device_list_quirks():
    from rsweb import loadouts as lo
    devices = _real_devices()
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"mining_drone": 3, "transport_drone": 2}}],
           "systems": {"AEMEROTH": "p", "FALQUORYX": "p", "ITHVALAI": "p"},
           "roles": {"FALQUORYX": "source", "AEMEROTH": "destination"}}
    stars = {k: {"position": {"x": i, "y": 0, "z": 0}} for i, k in enumerate(["AEMEROTH", "FALQUORYX", "ITHVALAI"])}
    p = lo.plan(cfg, devices, [], {}, stars, {}, set(), [], {})
    # 512FE0F9 works for AEMEROTH's controller, in AEMEROTH: it joins AEMEROTH's stationed fleet, its old
    # home:falquoryx tag goes
    a = {r["type"]: r for r in _rep(p, "AEMEROTH")["rows"]}
    assert a["mining_drone"]["have"] == 1
    assert "fleet:aemeroth-home" in p["tag_add"]["512FE0F9"] and "home:falquoryx" in p["tag_remove"]["512FE0F9"]
    # 926637CA is in ITHVALAI but run by FALQUORYX's controller: released, joins ITHVALAI's fleet, both old home tags go
    assert p["releases"] == {"F32E05A7": ["926637CA"]}
    assert p["tag_add"]["926637CA"] == ["fleet:ithvalai-home"]
    assert p["tag_remove"]["926637CA"] == ["home:falquoryx", "home:ithvalai"]
    # the ferry is not given to DF451241: it's on in-system work (consolidate)
    cur = {"DF451241": {"directive": "consolidate", "configuration": {"deliver": "FALQUORYX-BELT-1"}, "finished": False}}
    managed = {d["device_code"]: d["controller_device_code"] for d in devices if d.get("controller_device_code")}
    routes, unmet = lo.material_routes(cfg, devices, {"FALQUORYX-BELT-1": {"carbon": 100}}, stars, set(), cur, managed)
    assert routes == [] and any("in-system work" in u["why"] for u in unmet)
    # with a second controller, the freighter is released by DF451241 and adopted by the new one
    devices.append({"device_code": "TF000001", "device_type": "ami_transport_controller", "location": "FALQUORYX-BELT-1",
                    "status": "idle", "features": ["ami"], "available_commands": ["adopt", "set_directive"], "tags": []})
    routes, _ = lo.material_routes(cfg, devices, {"FALQUORYX-BELT-1": {"carbon": 100}}, stars, set(), cur, managed)
    r = routes[0]
    assert r["controller"] == "TF000001" and r["release"] == {"DF451241": ["57C506F0"]}
    bodies = [st["body"] for st in lo.ferry_steps(r)]
    assert bodies.index({"command": "release", "devices": ["57C506F0"]}) < bodies.index({"command": "adopt", "devices": ["57C506F0"]})


def test_controller_idle_reads_ami_directive(client):
    from rsweb.ami_schedule import controller_idle
    d = _real_devices()
    assert client.portal.call(controller_idle, client.app.state.db, d[0])[0] is True        # exhausted
    assert client.portal.call(controller_idle, client.app.state.db, d[2])[0] is True        # idle:no_sources
    busy = dict(d[0], ami_directive={"_eval_state": "mining", "name": "gather_evenly", "config": {}})
    assert client.portal.call(controller_idle, client.app.state.db, busy)[0] is False


def test_schedule_skips_exhausted_and_salvaging_controllers(client):
    eng = client.app.state.worker.automations
    base = {"device_type": "ami_mining_controller", "location": "X-BELT-1", "status": "coordinating", "features": ["ami"],
            "available_commands": ["set_directive", "launch", "adopt"]}
    client.portal.call(client.app.state.db.kv_set, "devices", [
        {**base, "device_code": "M1", "ami_directive": {"name": "gather_evenly", "_eval_state": "exhausted:['rares']:X-BELT-1", "config": {}},
         "ami_directive_status": "active"},
        {**base, "device_code": "M2", "ami_directive": {"name": "gather_salvage", "_eval_state": "active", "config": {"location": "X-2"}},
         "ami_directive_status": "active"},
        {**base, "device_code": "M3", "status": "out_of_range", "in_control_range": False}])
    sched = {"id": "s", "name": "All gather", "target": "kind:mining", "directive": "gather_evenly", "configuration": {},
             "only_idle": True, "adopt": True, "launch": True}
    res = client.portal.call(eng.run_schedule, sched)
    assert any("M1: exhausted" in r for r in res) and any(r.startswith("M2:") and ("salvage" in r or "busy" in r) for r in res)
    assert "M3: out of comms range — skipped" in res
    assert not [j for j in client.portal.call(eng.jobs) if j["rule"] == "ami_schedules"]


def test_device_snapshot_guard_while_replicant_travels():
    from rsweb.ingest import merge_device_snapshot
    from rsweb import loadouts as lo
    prev = [{"device_code": f"D{i}", "device_type": "mining_drone", "location": "AEM-BELT-1", "status": "idle", "tags": ["home:aem"]}
            for i in range(6)]
    assert merge_device_snapshot(prev, []) is None                       # empty → keep the old list
    assert merge_device_snapshot(prev, prev[:2]) is None                 # far shorter → keep the old list
    blank = [{**d, "location": None} for d in prev]                      # mid-surge: no locations
    merged = merge_device_snapshot(prev, blank)
    assert all(d["location"] == "AEM-BELT-1" and d["location_stale"] for d in merged)
    stowed = [{**prev[0], "location": None, "stowed_in_device_code": "V1"}]
    assert merge_device_snapshot(prev[:1], stowed)[0]["location"] is None   # genuinely stowed stays as is
    cfg = {"phases": [{"id": "p", "name": "P", "order": 1, "wants": {"mining_drone": 2}}], "systems": {"AEM": "p"}}
    p = lo.plan(cfg, merged, [], {}, {}, {}, set(), [], {})
    assert p["tag_add"] == {} and p["moves"] == {} and any(u["type"] == "data" for u in p["unmet"])
    assert any("waiting for good data" in line for line in lo.describe(p))


def test_partial_device_pages_while_travelling():
    from rsweb.ingest import merge_device_snapshot
    prev = [{"device_code": f"D{i}", "location": "AEM-BELT-1", "status": "idle"} for i in range(80)]
    page2 = [{"device_code": f"D{i}", "location": "FAL-BELT-1", "status": "idle"} for i in range(50, 80)]
    merged = merge_device_snapshot(prev, page2, partial=True)       # empty first page + cursor → partial
    assert len(merged) == 80
    assert sum(1 for d in merged if d.get("location_stale")) == 50
    assert next(d for d in merged if d["device_code"] == "D60")["location"] == "FAL-BELT-1"   # fresh data wins


def test_sync_devices_with_empty_first_page(client):
    world = client.app.state.api.http._transport.app.state.world
    w = client.app.state.worker
    client.portal.call(w.sync_devices)
    n = len(client.portal.call(client.app.state.db.kv_get, "devices"))
    app = client.app.state.api.http._transport.app
    # make the mock behave like the game mid-travel: first page empty but with a cursor
    from starlette.routing import Route
    orig = [r for r in app.router.routes if getattr(r, "path", "") == "/v1/devices"][0]
    async def travelling(request):
        from starlette.responses import JSONResponse
        cur = request.query_params.get("cursor")
        if not cur:
            return JSONResponse({"devices": [], "next_cursor": 5})
        return JSONResponse({"devices": world.devices[5:], "next_cursor": None})
    app.router.routes.insert(0, Route("/v1/devices", travelling))
    try:
        client.portal.call(w.sync_devices)
    finally:
        app.router.routes.pop(0)
    devs = client.portal.call(client.app.state.db.kv_get, "devices")
    assert len(devs) == n and sum(1 for d in devs if d.get("location_stale")) == 5


def test_bound_controller_drops_its_work_and_leaves():
    from rsweb import loadouts as lo
    stars = {"FAL": {"position": {"x": 0, "y": 0, "z": 0}}, "ITHVALAI": {"position": {"x": 1, "y": 0, "z": 0}, "entry_point": "ITHVALAI-2-L4"}}
    p = lo.plan({"phases": [], "systems": {}}, _bound_controller_world(), [], {}, stars, {}, set(), [], {})
    dl = next(d for d in p["deliveries"] if "SC000001" in d["devices"])
    assert dl["to"] == "ITHVALAI" and dl["mode"] == "attach"
    steps = lo.delivery_steps(dl, p["by_code"], stars, True, p["managed"])
    bodies = [(s["path"], s["body"]) for s in steps]
    rel = bodies.index(("/devices/SC000001", {"command": "release", "devices": ["SD000001"]}))
    clr = bodies.index(("/devices/SC000001", {"command": "clear_directive"}))
    att = bodies.index(("/devices/PL000001", {"command": "attach", "device": "SC000001"}))
    assert rel < clr < att
    # a busy bound device says why it isn't moving
    p = lo.plan({"phases": [], "systems": {}}, _bound_controller_world(), [], {}, stars, {}, {"SC000001"}, [], {})
    assert any("SC000001 can't leave FAL yet" in u["why"] for u in p["unmet"])

    # riding a carrier a running job holds: it waits for the carrier, and says so
    world = _bound_controller_world() + [
        {"device_code": "MF000001", "device_type": "mobile_fleet", "location": "FAL-BELT-1", "status": "idle",
         "features": ["surge", "cruise", "attach"], "tags": []}]
    next(d for d in world if d["device_code"] == "SD000002")["attached_to_device_code"] = "MF000001"
    p = lo.plan({"phases": [], "systems": {}}, world, [], {}, stars, {}, {"MF000001"}, [], {})
    assert not any("SD000002" in d["devices"] for d in p["deliveries"])
    assert any("SD000002 can't leave FAL yet: it rides MF000001" in u["why"] for u in p["unmet"])


def test_work_rules_skip_devices_bound_elsewhere():
    from rsweb.ami_schedule import adoptable, targets_of, in_transit
    devices = _bound_controller_world()
    assert in_transit(devices[1]) and not in_transit(devices[0])
    assert not in_transit({"device_code": "X", "location": "ITHVALAI-2-L4", "tags": ["to:ithvalai"]})   # arrived
    assert [d["device_code"] for d in targets_of({"target": "kind:survey"}, devices)] == []
    other = {"device_code": "SC2", "device_type": "ami_survey_controller", "location": "FAL-BELT-1", "status": "idle", "tags": []}
    assert adoptable(devices + [other], other, {"SD000001": "SC000001"}) == []    # SD000002 is bound for ITHVALAI


def test_inactive_arrivals_are_activated_once():
    from rsweb.ami_schedule import wakeups
    w = _arrival_world()
    from rsweb.ami_schedule import wakeup_steps
    assert wakeups(w, set(), {}) == [{"code": "MT1", "activate": False, "patrol": True}]
    assert [s["body"] for s in wakeup_steps(wakeups(w, set(), {})[0])] == [{"command": "set_directive", "directive": "patrol"}]
    ctrl = {"device_code": "SC9", "device_type": "ami_survey_controller", "location": "ITH-3", "status": "inactive",
            "available_commands": ["activate"], "tags": ["home:ith"]}
    assert wakeups([ctrl], set(), {}) == [{"code": "SC9", "activate": True, "patrol": False}]
    assert wakeups(w, set(), {"MT1": "ITH"}) == []           # already done on this arrival
    # active at home but idle with no directive: patrol only; already patrolling: nothing
    w[10].update(status="idle")
    assert wakeups(w, set(), {}) == [{"code": "MT1", "activate": False, "patrol": True}]
    w[10].update(status="patrolling", ami_directive={"name": "patrol"})
    assert wakeups(w, set(), {}) == []


def test_server_runs_and_versioned_log(client):
    from rsweb import version as ver
    db = client.app.state.db
    runs = client.portal.call(db.kv_get, "server_runs", [])
    assert runs[-1]["run"] == ver.RUN_ID and runs[-1]["version"] == ver.VERSION and runs[-1]["fingerprint"] == ver.FINGERPRINT
    log = client.portal.call(db.kv_get, "automation_log", [])
    start = next(e for e in log if e["text"].startswith("server started"))
    assert start["v"] == ver.VERSION and start["run"] == ver.RUN_ID and start["rule"] == "engine"
    # an entry from before versioning and one from an older version are told apart
    log += [{"at": "2026-09-30T10:00:00", "rule": "loadouts", "level": "alert", "text": "old problem"},
            {"at": "2026-10-01T10:00:00", "rule": "loadouts", "level": "alert", "text": "older version problem", "v": "1.3.0", "run": "x"}]
    client.portal.call(db.kv_set, "automation_log", log)
    page = client.get("/automations", headers=H).text
    assert "unversioned" in page and "v1.3.0 older version" in page and "<b>server started" in page
    acct = client.get("/account", headers=H).text
    assert 'id="server"' in acct and ver.RUN_ID in acct and f"v{ver.VERSION}" in acct and "Version history" in acct
    assert f"v{ver.VERSION}" in client.get("/", headers=H).text      # header label


def test_diagnosis_handles_null_locations():
    from rsweb.snapshot import diagnose
    devs = [{"device_code": "MD1", "device_type": "mining_drone", "location": None, "status": "stowed", "stowed_in_device_code": "V1"},
            {"device_code": "MD2", "device_type": "mining_drone", "location": "K-BELT-1", "status": "idle"},
            {"device_code": "SD1", "device_type": "survey_drone", "location": None, "status": "stowed"},
            {"device_code": "MC1", "device_type": "ami_mining_controller", "location": None, "features": ["ami"], "status": "stowed"}]
    snap = {"calls": [{"path": "/devices", "status": 200, "body": {"devices": devs}},
                      {"path": "/locations/K-BELT-1", "status": 200, "body": {"resource_sites": []}}],
            "app": {"rules": {}, "schedules": [], "jobs": [], "log": [{"level": "alert", "text": None}]}}
    d = diagnose(snap)
    rows = {r["code"]: r for r in d["drones"]}
    assert rows["MD1"]["state"] == "stowed" and any("stowed" in w for w in rows["MD1"]["why"])
    assert "diagnosis failed" not in " ".join(d["headline"])


def test_live_diagnosis_runs_clean():
    from rsweb.snapshot import diagnose
    snap = _live()
    snap["app"].update(jobs=[], log=[])
    d = diagnose(snap)
    assert "0 of 12 mining drones are mining" in d["headline"]
    ctrl = {c["code"]: c for c in d["controllers"]}
    assert any("drones are away" in n or "back to" in n for n in ctrl["84EE1EF1"]["notes"])
    assert any("until a mission puts it to work" in n for n in ctrl["D0011B15"]["notes"])   # fleet controllers wait for a mission
    assert not any("D0011B15" in h for h in d["headline"])


def test_live_after_recovery_leaves_partly_exhausted_controller_alone():
    """Second snapshot (20:39Z): every drone mining; F32E05A7 reports exhausted for silicates/structural only."""
    from rsweb import salvage as sv
    from rsweb.snapshot import devices_of, diagnose
    snap = _live_b()
    devices = devices_of(snap)
    belts = {c["path"].split("/")[2]: c["body"] for c in snap["calls"] if c["path"].startswith("/locations/")}
    open_sites = {b: sv.open_site_count(d) for b, d in belts.items()}
    ctrls = [d for d in devices if d.get("device_type") == "ami_mining_controller"]
    assert sv.back_to_belt_plan(ctrls, devices, snap["app"]["managed_by"], open_sites,
                                {b.split("-")[0]: [b] for b in belts}, set()) == []
    snap["app"].update(jobs=[], log=[])
    d = diagnose(snap)
    assert "12 of 12 mining drones are mining" in d["headline"]
    assert not any("reporting exhausted" in h for h in d["headline"])
    f = next(c for c in d["controllers"] if c["code"] == "F32E05A7")
    assert any("partly exhausted" in n for n in f["notes"]) and not any("stale" in n for n in f["notes"])


def test_detach_already_released_counts_as_done(client):
    from rsweb.automations import step
    eng = client.app.state.worker.automations
    world = client.app.state.api.http._transport.app.state.world
    async def fake_send(method, path, body, why):
        return False, None, "Target device is not attached to this carrier"
    orig = eng.send
    eng.send = fake_send
    try:
        job = client.portal.call(eng.create_job, "loadouts", "detach test", "SP000001",
                                 [step("SP000001: detach X", "/devices/SP000001", {"command": "detach", "device": "X"}, critical=True),
                                  step("tag X", "/devices/X", {"configuration": {"add_tags": ["home:sol"]}}, method="PATCH")],
                                 {"devices": []}, True)
    finally:
        eng.send = orig
    j = next(x for x in client.portal.call(eng.jobs) if x["id"] == job["id"])
    assert j["steps"][0]["status"] == "done" and j["status"] != "failed"


def test_surging_devices_missing_from_the_list_are_kept():
    """Live (2026-10-02T21:24Z): two cargo freighters surging between systems were absent from GET /devices."""
    from rsweb.ingest import merge_device_snapshot
    prev = [{"device_code": f"D{i}", "device_type": "mining_drone", "location": "AEM-BELT-1", "status": "mining"} for i in range(6)]
    prev.append({"device_code": "57C506F0", "device_type": "cargo_freighter", "location": "AEMEROTH-6-33", "status": "collecting",
                 "tags": ["home:aemeroth"], "controller_device_code": "0E158313"})
    new = [dict(d) for d in prev[:6]]
    out = merge_device_snapshot(prev, new, False, set(), now="2026-10-02T21:24:43+00:00")
    f = next(d for d in out if d["device_code"] == "57C506F0")
    assert f["unlisted"] and f["location_stale"] and f["location"] == "AEMEROTH-6-33" and f["tags"] == ["home:aemeroth"]
    assert f["unlisted_since"] == "2026-10-02T21:24:43+00:00"
    # still missing next sync: keeps its first-missed time; gone after 12 h
    out2 = merge_device_snapshot(out, new, False, set(), now="2026-10-02T23:00:00+00:00")
    assert next(d for d in out2 if d["device_code"] == "57C506F0")["unlisted_since"] == "2026-10-02T21:24:43+00:00"
    assert not any(d["device_code"] == "57C506F0" for d in merge_device_snapshot(out, new, False, set(), now="2026-10-03T10:00:00+00:00"))
    # back in the list: flags cleared
    back = new + [{**prev[6], "location": "ITHVALAI-2-L4", "status": "idle"}]
    f = next(d for d in merge_device_snapshot(out, back, False, set()) if d["device_code"] == "57C506F0")
    assert "unlisted" not in f and "location_stale" not in f and f["location"] == "ITHVALAI-2-L4"
    # decommissioned (an event says so): dropped at once
    assert not any(d["device_code"] == "57C506F0" for d in merge_device_snapshot(out, new, False, {"57C506F0"}))


def test_audit_flags_mismatches():
    from rsweb import loadouts as lo
    devs = [
        {"device_code": "MC1", "device_type": "ami_mining_controller", "location": "AEM-BELT-1", "status": "coordinating",
         "tags": ["fleet:aem"]},
        {"device_code": "MD1", "device_type": "mining_drone", "location": "AEM-BELT-1", "status": "mining", "controller_device_code": "MC1",
         "tags": ["fleet:fal"]},                                             # run by another fleet's controller
        {"device_code": "MD2", "device_type": "mining_drone", "location": "FAL-1-L4", "status": "idle", "tags": ["fleet:fal", "fleet:aem"]},
        {"device_code": "MD3", "device_type": "mining_drone", "location": "AEM-2-L4", "status": "idle", "tags": ["to:aem"]},   # arrived
        {"device_code": "MD4", "device_type": "mining_drone", "location": "AEM-2-L4", "status": "idle", "tags": ["home:aem"]},  # old tag
        {"device_code": "MD5", "device_type": "mining_drone", "location": "AEM-2-L4", "status": "idle", "tags": ["fleet:aem", "spare"]},
        {"device_code": "MD6", "device_type": "mining_drone", "location": "AEM-2-L4", "status": "idle", "tags": ["fleet:ghost"]},
        {"device_code": "FL1", "device_type": "mining_drone", "location": "AEM-2-L4", "status": "idle", "tags": ["fleet:x", "spare"]},
    ]
    fleets = [{"id": "aem", "name": "Aem", "home": "AEM", "station": True, "wants": {"mining_drone": 3}},
              {"id": "fal", "name": "Fal", "home": "FAL", "station": True, "wants": {}},
              {"id": "x", "name": "X", "home": "AEM", "station": False, "wants": {}, "mission": {"status": "running"}}]
    cfg = {"phases": [], "fleets": fleets, "fleets_migrated": True}
    p = lo.plan(cfg, devs, [], {}, {"AEM": {}, "FAL": {}}, {}, set(), [], {})
    by = {}
    for a in lo.audit(cfg, devs, {"AEM": {}, "FAL": {}}, p, {"MD3"}):
        by.setdefault(a["code"], []).append(a)
    assert any("a controller of fleet:aem, but not in that fleet" in a["issue"] for a in by["MD1"])
    assert any("2 fleet tags" in a["issue"] for a in by["MD2"])
    assert any("still tagged to:aem" in a["issue"] and a["fixed"] for a in by["MD3"])
    assert any("old home:aem tag" in a["issue"] and a["fixed"] for a in by["MD4"])
    assert any("tagged spare and fleet:aem" in a["issue"] and a["fixed"] for a in by["MD5"])   # needed: spare removed
    assert any("fleet:ghost: no such fleet" in a["issue"] for a in by["MD6"])
    assert "FL1" not in by                                                      # a fleet on a mission: the mission's business


def test_pinned_devices_reach_their_spot():
    from rsweb import loadouts as lo
    from rsweb.ami_schedule import handoffs
    devs = [
        # delivered to ITHVALAI (still tagged to:), pinned to its belt
        {"device_code": "MD1", "device_type": "mining_drone", "location": "ITHVALAI-2-L4", "status": "idle",
         "tags": ["to:ithvalai", "at:ithvalai-belt-1"]},
        # already home, pinned to planet 3, but sitting at the entry point
        {"device_code": "SD1", "device_type": "survey_drone", "location": "ITHVALAI-2-L4", "status": "idle",
         "tags": ["home:ithvalai", "at:ithvalai-3"]},
        {"device_code": "SC1", "device_type": "ami_survey_controller", "location": "ITHVALAI-BELT-1", "status": "idle",
         "features": ["ami"], "available_commands": ["adopt"], "tags": ["home:ithvalai"]},
    ]
    cfg = {"phases": [], "systems": {}}
    p = lo.plan(cfg, devs, [], {}, {"ITHVALAI": {}}, {}, set(), [], {})
    assert "MD1" in p["arrived"] and ("SD1", "ITHVALAI-3") in p["pins"]
    steps = lo.arrived_steps("MD1", devs[0], {})
    assert steps[-1]["body"] == {"command": "travel", "destination": "ITHVALAI-BELT-1"}
    assert any("goes to ITHVALAI-3 (pinned there)" in l for l in lo.describe(p))
    # the survey controller at the belt doesn't pull the pinned drone away
    assert handoffs(devs, {}, set()) == []
    # moving to another system drops a pin for the old one
    rh = lo.rehome_step("SD1", devs[1], "AEMEROTH")
    assert "at:ithvalai-3" in rh["body"]["configuration"]["remove_tags"]


def test_a_grounded_pinned_device_is_carried_to_its_spot():
    """Live 2026-10-21: 3 compute cores for the TWAFFY-OORT locker were dropped at TWAFFY's entry point (TWAFFY-11-L4)
    and could not fly to their pin on their own."""
    from rsweb import loadouts as lo
    core = lambda c, loc, tags: {"device_code": c, "device_type": "compute_core", "location": loc, "status": "idle",  # noqa: E731
                                 "features": ["stow"], "available_commands": ["deploy", "stow"], "tags": tags}
    cv = {"device_code": "CV1", "device_type": "cargo_vessel", "location": "TWAFFY-11-L4", "status": "idle",
          "features": ["surge", "transport"], "available_commands": ["travel", "stow"], "stow_capacity": 9, "tags": []}
    devs = [core("C1", "TWAFFY-11-L4", ["at:twaffy-oort"]), core("C2", "TWAFFY-11-L4", ["at:twaffy-oort"]), cv]
    stars = {"TWAFFY": {"entry_point": "TWAFFY-11-L4"}}
    p = lo.plan({"phases": [], "systems": {}}, devs, [], {}, stars, {}, set(), [], {})
    dl = p["deliveries"]
    assert len(dl) == 1 and sorted(dl[0]["devices"]) == ["C1", "C2"] and dl[0]["to"] == "TWAFFY"
    bodies = [s["body"] for s in lo.delivery_steps(dl[0], p["by_code"], stars, False)]
    i = bodies.index({"command": "travel", "destination": "TWAFFY-OORT"})
    assert {"command": "deploy"} in bodies[i:] and {"command": "travel", "destination": "TWAFFY-11-L4"} not in bodies[:i]
    # arriving on its own somewhere else in the system, a grounded device doesn't try to fly to its pin
    assert not any((s["body"] or {}).get("command") == "travel" for s in lo.arrived_steps("C1", devs[0], {}))
    # from another system, a pinned device is dropped at its pin, not at the entry point
    devs2 = [core("C3", "FAL-BELT-1", ["to:twaffy", "at:twaffy-oort"]), {**cv, "location": "FAL-1-L4"}]
    stars2 = {**stars, "FAL": {"entry_point": "FAL-1-L4"}}
    p2 = lo.plan({"phases": [], "systems": {}}, devs2, [], {}, stars2, {}, set(), [], {})
    bodies2 = [s["body"] for s in lo.delivery_steps(p2["deliveries"][0], p2["by_code"], stars2, False)]
    assert {"command": "travel", "destination": "TWAFFY-OORT"} in bodies2
    assert {"command": "travel", "destination": "TWAFFY-11-L4"} not in bodies2


def test_engine_lock_is_reentrant_and_watchdog_alerts():
    from rsweb.automations import EngineLock
    import rsweb.automations as au

    async def go():
        lk = EngineLock()
        async with lk:
            async with lk:          # same task: no deadlock
                assert lk.locked() and lk.holder
            assert lk.locked()
        assert not lk.locked() and lk.holder is None
        # another task still waits for it
        order = []

        async def other():
            async with lk:
                order.append("other")
        async with lk:
            t = asyncio.create_task(other())
            await asyncio.sleep(0.01)
            order.append("first")
        await t
        assert order == ["first", "other"]

    run(go())

    class Eng(au.AutomationEngine):
        def __init__(self):
            super().__init__(None, None, None, None)
            self.logged = []

        async def log(self, rule, text, level="info", notify=False):
            self.logged.append((level, text))

    async def wd():
        e = Eng()
        await e.lock.acquire()
        e.lock.since -= au.LOCK_ALERT_SECONDS + 5
        e.stage = "run_fleets"
        await e.watchdog()
        await e.watchdog()                       # alerts once per stall
        assert len(e.logged) == 1 and e.logged[0][0] == "error" and "run_fleets" in e.logged[0][1]
        e.lock.release()
        await e.watchdog()
        assert "running again" in e.logged[-1][1]

    run(wd())


def test_deploy_already_deployed_counts_as_done(client):
    eng = client.app.state.worker.automations
    from rsweb.automations import step
    from rsweb.api import ApiError

    async def go():
        real = eng.send

        async def fake(method, path, body, label):
            if (body or {}).get("command") == "deploy":
                return False, None, "Device is already deployed"
            return await real(method, path, body, label)
        eng.send = fake
        steps = [step(f"deploy D{i}", f"/devices/D{i}", {"command": "deploy"}, wait=["device.deployed"]) for i in range(4)]
        async with eng.lock:
            job = await eng.create_job("fleets", "Surveyors: deploy", None, steps, force=True)
        return next(j for j in await eng.jobs() if j["id"] == job["id"])

    j = client.portal.call(go)
    assert j["status"] == "done" and all(s["status"] == "done" for s in j["steps"])


# --- 1.13.0: placement (relays at Lagrange points, controllers at the belt), slingshot ---------------------------------

def test_placement_rules():
    from rsweb import placement as pl
    scan = {"entry_point": "AAA-5-L4", "asteroid_belt": {"belts": [{"designation": "AAA-BELT-1"}]},
            "planets": [{"designation": "AAA-5", "orbital_distance_au": 1.2}, {"designation": "AAA-1", "orbital_distance_au": 0.1}]}
    g = pl.geography("AAA", [], scan, "AAA-5-L4")
    assert g["belts"] == ["AAA-BELT-1"] and g["lagrange"][0] == "AAA-5-L4" and g["inner"][0] == "AAA-1"
    assert pl.ok("ftl_relay", "AAA-5-L4", g) and not pl.ok("ftl_relay", "AAA-BELT-1", g) and not pl.ok("ftl_relay", "AAA-5", g)
    assert pl.target("ftl_relay", g) == "AAA-5-L4"
    assert pl.ok("ami_mining_controller", "AAA-BELT-1", g) and not pl.ok("ami_mining_controller", "AAA-5-L4", g)
    assert pl.target("ami_mining_controller", g) == "AAA-BELT-1"
    assert not pl.ok("ami_survey_controller", "AAA-1", g)            # there is a belt: it goes there
    nobelt = pl.geography("BBB", [], {"planets": [{"designation": "BBB-2", "orbital_distance_au": 0.3},
                                                   {"designation": "BBB-1", "orbital_distance_au": 0.1}]}, "BBB-2-L4")
    assert pl.target("ami_survey_controller", nobelt) == "BBB-1"
    assert pl.ok("ami_survey_controller", "BBB-2-L4", nobelt) and not pl.ok("ami_survey_controller", "BBB-KUIPER", nobelt)
    assert pl.target("ami_mining_controller", nobelt) is None
    assert pl.ok("transport_drone", "BBB-KUIPER", nobelt)            # no rule for other types


def test_replicant_slingshot_link_and_fire(client):
    """Docs /docs/ftl-slingshots/: link to an empty matrix at the slingshot's location (PATCH linked_device); the
    replicant fires the slingshot where it is (teleport target = slingshot); ≥80 % capacity, 5 % after."""
    from rsweb.mock import REP
    world = client.app.state.api.http._transport.app.state.world
    sl = {"device_code": "SL000001", "device_type": "ftl_slingshot", "location": "SOL-BELT-1", "status": "idle",
          "features": ["slingshot", "stow"], "operational_capacity": 100.0, "linked_device": None, "replicant_code": REP,
          "available_commands": ["deploy", "stow"]}
    far = {**sl, "device_code": "SL000002", "location": "SOL-3-L4", "linked_device": "XX000009"}
    mx = {"device_code": "MX000001", "device_type": "empty_replicant_matrix", "location": "SOL-BELT-1", "status": "idle",
          "features": ["matrix"], "operational_capacity": 100.0, "replicant_code": REP, "available_commands": ["deploy"]}
    mx2 = {**mx, "device_code": "MX000002", "location": "SOL-5"}
    world.devices += [sl, far, mx, mx2]
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get(f"/replicants/{REP}", headers=H).text
    assert "FTL slingshot" in page and "SL000001" in page and 'value="MX000001"' in page and '<option value="MX000002"' not in page
    r = client.post(f"/replicants/{REP}/slingshot", data={"slingshot": "SL000001"}, headers=HX)
    assert "linked to a matrix yet" in r.text and not world.teleports
    r = client.post(f"/replicants/{REP}/slingshot", data={"slingshot": "SL000002"}, headers=HX)
    assert "go to the slingshot first" in r.text and not world.teleports
    r = client.post("/slingshots/SL000001/link", data={"matrix": "MX000002"}, headers=HX)
    assert "must be together" in r.text and not sl["linked_device"]
    client.post("/slingshots/SL000001/link", data={"matrix": "MX000001"}, headers=HX)
    assert sl["linked_device"] == "MX000001"
    client.portal.call(client.app.state.worker.sync_devices)
    client.post(f"/replicants/{REP}/slingshot", data={"slingshot": "SL000001"}, headers=HX)
    assert world.teleports == [(REP, "SL000001")]
    client.portal.call(client.app.state.worker.sync_devices)
    r = client.post(f"/replicants/{REP}/slingshot", data={"slingshot": "SL000001"}, headers=HX)
    assert "at least 80" in r.text and len(world.teleports) == 1


def test_rules_live_on_their_pages_and_nav_is_grouped(client):
    eng = client.app.state.worker.automations
    # the Automations page lists every rule with a link to where its settings are
    page = client.get("/automations", headers=H).text
    assert "Asteroid defense" in page and 'href="/defence">Map › Defense →' in page
    assert 'name="max_prints"' not in page                       # the settings cards moved out
    # each page loads its own rules' cards
    assert 'rules-panel?ids=asteroid_defence,asteroid_mining"' in client.get("/defence", headers=H).text
    r = client.get("/automations/rules-panel?ids=asteroid_defence,nope", headers=HX)
    assert 'hx-post="/automations/rules/asteroid_defence"' in r.text and 'name="max_prints"' in r.text and "nope" not in r.text
    assert "Survey this system now" in client.get("/automations/rules-panel?ids=auto_survey", headers=HX).text
    # the overview toggle flips the switch and keeps the options
    s = client.portal.call(eng.settings)
    before = dict(s["rules"]["asteroid_defence"])
    client.post("/automations/rules/asteroid_defence/toggle", headers=HX)
    after = client.portal.call(eng.settings)["rules"]["asteroid_defence"]
    assert after["enabled"] != before["enabled"] and {k: v for k, v in after.items() if k != "enabled"} == \
           {k: v for k, v in before.items() if k != "enabled"}
    # AMI schedules moved to the AMI page
    assert "AMI schedules" in client.get("/ami", headers=H).text
    # nav: 6 groups, the current one's pages as sub-tabs; old URLs still work
    page = client.get("/defence", headers=H).text
    nav = page[page.index('<nav class="main">'):page.index("</nav>")]
    for g in ("Dashboard", "Devices", "Map", "Fleets", "Economy", "Activity"):
        assert f">{g}<" in nav or f">{g}<span" in nav
    sub = page[page.index('<nav class="sub">'):]
    sub = sub[:sub.index("</nav>")]
    assert all(t in sub for t in ("Galaxy", "Systems", "Traffic", "Defense", "Upkeep")) and "Blueprints" not in sub
    assert 'href="/diagnostics"' in page and 'href="/console"' in page   # account menu
    fp = client.get("/loadouts", headers=H).text   # the old Home fleets URL lands on Fleets
    assert "<h1>Fleets" in fp and ">Fleets</a>" in fp and ">Reset &amp; reform</a>" in fp
    assert "Home fleets" not in fp and "Mobile fleets" not in fp


def test_reform_rebuilds_assignments_from_where_devices_are():
    from rsweb import reform
    D = lambda code, t, loc, tags=(), **kw: {"device_code": code, "device_type": t, "location": loc, "status": "idle",  # noqa: E731
                                             "tags": list(tags), **kw}
    devices = [
        D("MF1", "mobile_fleet", "AAA-5-L4", ["fleet:f1"]),
        D("M1", "mining_drone", "AAA-5-L4", ["fleet:f1", "home:aaa"]),       # member with a stray home tag
        D("M2", "mining_drone", "AAA-5-L4", ["spare", "to:bbb"]),            # stale to: — in AAA, idle
        D("M3", "mining_drone", "AAA-5-L4", ["home:bbb"], controller_device_code="CB"),  # wrong home, run from BBB
        D("M4", "mining_drone", "AAA-5-L4", ["home:aaa", "keepme"], controller_device_code="CA"),
        D("CA", "ami_mining_controller", "AAA-BELT-1", ["home:aaa"], features=["ami"]),
        D("CB", "ami_mining_controller", "BBB-BELT-1", ["home:bbb"], features=["ami"]),
        D("AF", "autofactory", "AAA-3-L4", []),
        D("P1", "surge_plate", "AAA-5-L4", ["taxi", "home:aaa"]),            # ferry gear: untouched
        D("K1", "mining_drone", "AAA-5-L4", ["keep", "spare"]),              # ignored
        D("MV", "mining_drone", "AAA-5-L4", ["to:ccc"], status="moving"),     # in flight: untouched
        D("X1", "survey_drone", "ZZZ-1", ["spare"]), D("X2", "survey_drone", "ZZZ-1", ["home:zzz"]),
    ]
    cfg = {"phases": [{"id": "p", "name": "P", "wants": {"mining_drone": 1, "ami_mining_controller": 1, "autofactory": None}}],
           "ignore_tags": ["keep"]}
    st_wants = {"mining_drone": 1, "ami_mining_controller": 1}     # template p, resolved (blank = don't care)
    fleets = [{"id": "f1", "name": "F1", "home": "AAA", "wants": {"mobile_fleet": 1, "mining_drone": 2}},
              {"id": "aaa", "name": "Aaa", "home": "AAA", "station": True, "template": "p", "wants": st_wants},
              {"id": "bbb", "name": "Bbb", "home": "BBB", "station": True, "template": "p", "wants": st_wants}]
    p = reform.plan(cfg, devices, fleets, busy=set(), hosts=set())
    to = {r["code"]: (r["to"], sorted(r["remove"])) for r in p["retag"]}
    assert to["M1"] == ("fleet:f1", ["home:aaa"])
    assert to["M2"] == ("fleet:f1", ["spare", "to:bbb"])                    # fills the fleet's gap
    assert to["M3"] == ("spare", ["home:bbb"])                              # AAA's fleet wants 1 miner: M4, home there before
    assert to["M4"] == ("fleet:aaa", ["home:aaa"])                          # joins AAA's stationed fleet (keeps 'keepme')
    assert to["CA"] == ("fleet:aaa", ["home:aaa"]) and to["CB"] == ("fleet:bbb", ["home:bbb"])
    assert "AF" not in to                                                   # blank in the template: stays untagged
    assert "MF1" not in to                                                  # already right
    assert to["X2"] == ("(untagged)", ["home:zzz"])                        # no fleet stationed in ZZZ
    assert "X1" not in to                                                   # spare in an unmanaged system stays spare
    assert {s["code"] for s in p["skipped"]} >= {"P1", "K1", "MV"}
    assert p["releases"] == {"CB": ["M3"]}                                 # spare now, and run from another system
    st = reform.steps(p)
    assert st[0]["body"] == {"command": "release", "devices": ["M3"]} and st[0]["path"] == "/devices/CB"
    assert reform.plan(cfg, devices, fleets, set(), set(), full=True)["releases"] == {"CA": ["M4"], "CB": ["M3"]}


def test_reform_and_fill_pages(client):
    page = client.get("/fleets/reform", headers=H).text
    assert "<h1>Reset &amp; reform" in page and ">Reset &amp; reform</a>" in page
    r = client.post("/fleets/reform/preview", data={}, headers=HX)
    assert "Tag changes" in r.text and "Controller releases" in r.text
    r = client.post("/fleets/reform/apply", data={}, headers=HX)
    assert "Started job" in r.text or "Nothing to change" in r.text
    client.post("/fleets", data={"name": "Scouts", "role": "explore", "home": "SOL"}, headers=HX)
    client.portal.call(client.app.state.db.kv_set, "loadouts",
                       {"phases": [{"id": "sc", "name": "Scout set", "order": 1, "wants": {"survey_drone": 2}}], "systems": {}})
    client.post("/fleets/scouts/edit", data={"template": "sc"}, headers=HX)
    page = client.get("/fleets/details?all=1", headers=H).text
    assert '<option value="sc" selected>Scout set</option>' in page and "fieldset disabled" in page
    assert "Fill from spares" in page and 'rules-panel?ids=loadouts,fleet_fill"' in client.get("/fleets", headers=H).text
    assert "Scouts" in client.post("/fleets/scouts/fill", headers=HX).text or "Nothing to fill" in client.post("/fleets/scouts/fill", headers=HX).text


def test_far_passengers_are_fetched_near_ones_fly_over():
    from rsweb import fleets as fl, loadouts as lo
    scan = {"planets": [{"designation": "OTH-2", "orbital_distance_au": 0.7}, {"designation": "OTH-3", "orbital_distance_au": 1.0}],
            "asteroid_belt": {"belts": [{"designation": "OTH-BELT-1", "inner_radius_au": 2, "outer_radius_au": 4}]},
            "outer_system": {"kuiper": {"designation": "OTH-KUIPER", "distance_au": 48.0},
                             "oort": {"designation": "OTH-OORT", "distance_au": 1800.0}}}
    radii = fl.system_radii(scan)
    assert radii == {"OTH-2": 0.7, "OTH-3": 1.0, "OTH-BELT-1": 3.0, "OTH-KUIPER": 48.0, "OTH-OORT": 1800.0}
    assert fl.radius_au("OTH-3-L4", radii) == 1.0 and fl.radius_au("OTH-3-2", radii) == 1.0   # L-points, moons: their planet
    assert fl.cruise_au("OTH-2", "OTH-BELT-1", radii) == pytest.approx(2.3) and fl.far_apart("OTH-3", "OTH-KUIPER", radii)
    assert fl.far_apart("ZZ-1", "ZZ-OORT") and not fl.far_apart("ZZ-1", "ZZ-BELT-1")   # unscanned: guessed from the codes
    fleet = {"id": "f", "name": "F", "role": "mining", "home": "OTH", "wants": {}}
    D = lambda code, loc: {"device_code": code, "device_type": "survey_drone", "location": loc, "status": "idle",  # noqa: E731
                           "features": ["stow", "cruise"], "tags": ["fleet:f"]}
    devices = [{"device_code": "HV", "device_type": "cargo_vessel", "location": "OTH-3", "status": "idle", "features": ["surge"],
                "stow_capacity": 50, "tags": ["fleet:f"]},
               D("N1", "OTH-2"), D("N2", "OTH-BELT-1"), D("F1", "OTH-KUIPER"), D("F2", "OTH-OORT")]
    plan = [s["desc"] for s in fl.assemble_steps(fleet, devices, radii)[0]]
    assert "N1 → OTH-3 (board)" in plan and "N2 → OTH-3 (board)" in plan        # short hops: they fly over
    assert plan[-4:] == ["HV → OTH-KUIPER (pick up F1)", "stow F1 in HV", "HV → OTH-OORT (pick up F2)", "stow F2 in HV"]
    assert plan.index("stow N1 in HV") < plan.index("HV → OTH-KUIPER (pick up F1)")   # near ones board before it leaves
    plan = [s["desc"] for s in fl.assemble_steps(fleet, devices, radii, limit=5000)[0]]
    assert "F2 → OTH-3 (board)" in plan                                          # the limit is a setting
    # loadout deliveries: same rule, and everyone at the pick-up point boards before the carrier goes fetching
    by = {d["device_code"]: d for d in devices}
    by["BC"] = {"device_code": "BC", "device_type": "ftl_beacon", "location": "OTH-2", "available_commands": ["stow"]}
    dl = {"carrier": "HV", "carrier_loc": "OTH-3", "from": "OTH", "to": "AEM", "devices": ["N1", "F2", "BC"], "mode": "stow"}
    steps = [s["desc"] for s in lo.delivery_steps(dl, by, {}, False, radii=radii)]
    assert "N1 → OTH-3 (to board HV)" in steps and not any(x.startswith("F2 →") for x in steps)
    assert steps.index("stow N1 in HV") < steps.index("HV → OTH-2 (pick up BC)") < steps.index("HV → OTH-OORT (pick up F2)")


def test_travel_destination_picker(client):
    client.portal.call(client.app.state.worker.sync_devices)
    client.portal.call(client.app.state.db.kv_set, "census_stars",
                       {"ZALDANAL": {"designation": "ZALDANAL", "explored": False, "position": {"x": 5, "y": 3, "z": 0}}})
    client.portal.call(client.app.state.worker.sync_catalogue)
    page = client.get("/replicants/77F75255", headers=H).text
    # one field: type anything; its list suggests your systems, then the nearest explored (unexplored only typed)
    assert 'name="destination__star"' in page and 'list="nearby-stars-sys"' in page and 'value="SOL">yours' in page
    assert 'value="ZALDANAL"' not in page
    assert "page 1" in page
    r = client.get("/print-queue/locations?dest_star=SOL", headers=H)   # the second list: spots in the system
    assert "anywhere in SOL" in r.text
    # the system alone, the spot, or the typed code
    for form, dest in (({"destination__star": "ZALDANAL"}, "ZALDANAL"),
                       ({"destination__star": "SOL", "destination": "SOL-BELT-1"}, "SOL-BELT-1"),
                       ({"destination__star": "SOL", "destination__custom": "sol-3-l4"}, "SOL-3-L4")):
        r = client.post("/replicants/77F75255/travel", data={**form, "dry_run": "1"}, headers=HX)
        assert dest in r.text, r.text[:300]
    assert "Pick a system" in client.post("/replicants/77F75255/travel", data={"dry_run": "1"}, headers=HX).text
    from rsweb import commands
    from starlette.datastructures import FormData
    travel = commands.COMMANDS["travel"]
    assert commands.parse_fields(travel, FormData({"f.destination__star": "ZALDANAL"})) == {"destination": "ZALDANAL"}
    assert commands.parse_fields(travel, FormData({"f.destination__star": "SOL", "f.destination": "SOL-3"})) == {"destination": "SOL-3"}
    # the device page's command box offers the same picker
    form = client.get("/devices/11ADA230/command-form?command=travel", headers=H).text
    assert 'name="f.destination__star"' in form and 'value="SOL">yours' in form


def test_change_owner_sends_target():
    """Live 2026-10-06: 'target: Missing data for required field.; replicant_code: Unknown field.'"""
    from starlette.datastructures import FormData
    from rsweb import commands
    assert commands.parse_fields(commands.COMMANDS["change_owner"], FormData({"f.target": "D9351B81"})) == {"target": "D9351B81"}


def test_snapshot_2026_10_06_fixes(client):
    """Live 2026-10-06 (19:54): a mission to LORALEL (typo), a fleet controller given a production gather order, and
    to:/at: tags naming a fleet."""
    from rsweb import loadouts as lo, production
    from rsweb.ami_schedule import set_stationed
    # 1. unknown mission targets are refused, with a suggestion
    client.portal.call(client.app.state.db.kv_set, "census_stars",
                       {"LORALAEL": {"designation": "LORALAEL", "explored": False, "position": {"x": 9, "y": 9, "z": 0}}})
    client.portal.call(client.app.state.worker.sync_catalogue)
    client.post("/fleets", data={"name": "Scouts", "role": "explore", "home": "SOL"}, headers=HX)
    r = client.post("/fleets/scouts/mission", data={"targets": "LORALEL"}, headers=HX)
    assert "Unknown system" in r.text and "did you mean LORALAEL" in r.text
    assert 'value="LORALAEL"' in client.get("/fleets/details?all=1", headers=H).text            # census stars are offered
    r = client.post("/fleets/scouts/mission", data={"targets": "LORALAEL"}, headers=HX)
    assert r.headers.get("HX-Refresh")
    # 2. the production planner skips controllers of fleets that aren't at their station
    ctrls = [{"device_code": "FC", "device_type": "ami_mining_controller", "location": "FAL-BELT-1", "status": "idle",
              "features": ["ami"], "tags": ["fleet:prospectors"]},
             {"device_code": "HC", "device_type": "ami_mining_controller", "location": "FAL-BELT-1", "status": "coordinating",
              "features": ["ami"], "tags": ["fleet:fal-home"]}]
    try:
        set_stationed({"fleet:fal-home": "FAL"})
        assert [c["device_code"] for c in production.controllers_in(ctrls, "FAL", "mining")] == ["HC"]
    finally:
        set_stationed({})
    # 3. to:/at: tags that aren't locations are ignored
    d = {"device_code": "CV", "tags": ["at:fleet:prospectors", "fleet:prospectors", "to:fleet:prospectors"]}
    assert lo.bound_for(d, {"FAL"}) is None and lo.pinned_at(d) is None
    assert lo.is_place("FALQUORYX-BELT-1") and not lo.is_place("fleet:prospectors")


def test_change_owner_already_owned_counts_as_done(client):
    """Live 2026-10-06: a second owner pass got 'Device already belongs to that replicant' and failed the job."""
    import time
    from rsweb.automations import step
    eng = client.app.state.worker.automations
    world = client.app.state.api.http._transport.app.state.world
    d = next(x for x in world.devices if "change_owner" in x["available_commands"])
    d["replicant_code"] = "AAAA0001"
    job = client.portal.call(eng.create_job, "fleets", "owners", None,
                             [step("owner", f"/devices/{d['device_code']}", {"command": "change_owner", "target": "AAAA0001"})],
                             {}, True)
    for _ in range(20):
        client.portal.call(eng.tick)
        j = next(x for x in client.portal.call(eng.jobs) if x["id"] == job["id"])
        if j["status"] not in ("running", "waiting"):
            break
        time.sleep(0.05)
    assert j["status"] == "done" and "already belongs" in j["steps"][0]["note"]


def test_new_home_brings_the_whole_working_group():
    """Live 2026-10-06: Miner 1's home moved ITHVALAI → KELMORNEA; its coordinating controllers, their adopted drones
    and the searching survey drones were all left behind."""
    from rsweb import loadouts as lo
    D = lambda code, t, loc, status="idle", **kw: {"device_code": code, "device_type": t, "location": loc, "status": status,  # noqa: E731
                                                  "tags": kw.pop("tags", ["fleet:m1"]), **kw}
    devices = [
        D("MC", "ami_mining_controller", "ITH-BELT-1", "coordinating", ami_directive={"name": "gather_evenly"},
          available_commands=["set_directive", "release", "travel", "stow"]),
        D("SC", "ami_survey_controller", "ITH-BELT-1", "coordinating", ami_directive={"name": "belt_search"},
          available_commands=["set_directive", "release", "travel", "stow"]),
        D("FC", "ami_transport_controller", "ITH-2-L4", "coordinating", tags=["ferry", "fleet:m1"],
          ami_directive={"name": "ferry", "config": {"collect": "ITH-BELT-1", "deliver": "FAL-BELT-1"}},
          available_commands=["set_directive", "release", "travel", "stow"]),
        D("MD1", "mining_drone", "ITH-BELT-1", controller_device_code="MC"),
        D("MD2", "mining_drone", "ITH-BELT-1", controller_device_code="MC"),
        D("SD1", "survey_drone", "ITH-BELT-1", "searching", controller_device_code="SC"),
        D("SD2", "survey_drone", "ITH-BELT-1", "tracking", controller_device_code="SC", tags=["fleet:m1", "to:kel"]),
        D("BUSY", "mining_drone", "ITH-BELT-1", "mining (carbon)"),           # can't cruise while mining: next pass
        D("CV", "cargo_vessel", "ITH-2-L4", features=["surge", "cruise", "stow"], stow_capacity=50, tags=[]),
        # a bigger carrier right there, but its fleet is on a mission: not the planner's to use
        D("PV", "cargo_vessel", "ITH-2-L4", features=["surge", "cruise", "stow"], stow_capacity=99, tags=["fleet:p"]),
        # the new home's own ferry: its freighter out delivering is not "left behind"
        D("FC2", "ami_transport_controller", "KEL-4-L4", "coordinating", tags=["ferry", "fleet:m1"],
          ami_directive={"name": "ferry", "config": {"collect": "KEL-BELT-1", "deliver": "FAL-BELT-1"}}),
        D("FR", "cargo_freighter", "FAL-BELT-1", controller_device_code="FC2", features=["surge"]),
    ]
    stars = {"ITH": {"position": {"x": 0, "y": 0, "z": 0}}, "KEL": {"position": {"x": 1, "y": 0, "z": 0}, "entry_point": "KEL-4-L4"},
             "FAL": {"position": {"x": 2, "y": 0, "z": 0}}}
    fleets = [{"id": "m1", "name": "Miner 1", "home": "KEL", "station": True, "wants": {}},
              {"id": "p", "name": "Prospectors", "home": "FAL", "station": False, "wants": {}, "mission": {"status": "running"}}]
    p = lo.plan({"phases": [], "fleets": fleets, "fleets_migrated": True}, devices, [], {}, stars, {}, set(), [], {})
    assert sorted(p["returning"]) == ["FC", "MC", "MD1", "MD2", "SC", "SD1"]
    dl = p["deliveries"][0]
    assert "SD2" in dl["devices"]                    # tagged to go home on an earlier pass: its site doesn't hold it
    assert dl["carrier"] == "CV" and dl["to"] == "KEL"
    steps = lo.delivery_steps(dl, p["by_code"], stars, True, {"MC": ["MD1", "MD2"], "SC": ["SD1"]})
    descs = [s["desc"] for s in steps]
    assert "MC: release 2 device(s) before leaving" in descs and "MC: clear directive before leaving" in descs
    assert "SC: clear directive before leaving" in descs and "FC: clear directive before leaving" in descs
    assert not any(d.startswith("MC: release MD") for d in descs)          # let go once, by the controller leaving with them


def test_modular_devices_compact_before_moving_and_unfurl_after():
    """Autofactories and galactic observatories (feature `modular`) must be compacted before they move."""
    from rsweb import loadouts as lo
    from rsweb.modular import with_compaction
    af = {"device_code": "AF1", "device_type": "autofactory", "location": "FAL-BELT-1", "status": "idle",
          "features": ["cruise", "modular", "print"], "available_commands": ["compact", "unfurl", "travel"], "tags": []}
    plate = {"device_code": "MF", "device_type": "mobile_fleet", "location": "FAL-1-L4", "status": "idle",
             "features": ["surge", "attach"], "attach_capacity": 36}
    md = {"device_code": "MD1", "device_type": "mining_drone", "location": "FAL-BELT-1", "status": "idle", "features": ["cruise", "stow"]}
    by = {d["device_code"]: d for d in (af, plate, md)}
    dl = {"carrier": "MF", "carrier_loc": "FAL-1-L4", "from": "FAL", "to": "KEL", "devices": ["AF1", "MD1"], "mode": "attach"}
    steps = lo.delivery_steps(dl, by, {"KEL": {"entry_point": "KEL-4-L4"}}, False)
    out = with_compaction(steps, [af, plate, md], {"autofactory": {"print_time": 36000}})
    descs = [s["desc"] for s in out]
    ci, ti = descs.index("AF1: compact before moving"), descs.index("AF1 → FAL-1-L4 (to board MF)")
    assert ci < ti and out[ci]["wait"] == ["device.compacted"] and out[ci]["timeout"] >= 36000 * 0.3
    assert descs.index("MF: detach AF1 in KEL") + 1 == descs.index("AF1: unfurl")
    assert not any(d.startswith("MD1: compact") for d in descs)              # not modular: unchanged
    # WAIT steps still point at the right travel step after the insertions
    for s in out:
        if "seq0_from" in s:
            assert out[s["seq0_from"]]["body"]["command"] == "travel"
            assert out[s["seq0_from"]]["path"].split("/")[2] == s["wait_device"]
    # already compacted (e.g. printed compacted): no compact, still unfurls on landing
    af["status"] = "compacted"
    out = with_compaction(steps, [af, plate, md], {})
    assert not any("compact before" in s["desc"] for s in out) and any(s["desc"] == "AF1: unfurl" for s in out)
    # flying itself inside the system: compact, travel, unfurl on arrival
    af["status"] = "idle"
    st = lo.step("AF1 → FAL-2", "/devices/AF1", {"command": "travel", "destination": "FAL-2"}, wait=["travel.arrived"])
    assert [s["desc"] for s in with_compaction([st], [af], {})] == ["AF1: compact before moving", "AF1 → FAL-2", "AF1: unfurl"]


def test_station_unfurls_fully_before_switch_on_at_the_drop():
    """Live 2026-10-30: 'activate AC330D54' was refused (Device is unfurling) at KELAAXIX, the carrier left, and the
    deep space relay station sat off out of reach until a rescue brought it home spare."""
    from rsweb import loadouts as lo
    from rsweb.automations import _already_unfurled
    from rsweb.modular import with_compaction
    stn = {"device_code": "ST1", "device_type": "deep_space_relay_station", "location": "FAL-BELT-1", "status": "inactive",
           "features": ["cruise", "relay", "modular"], "available_commands": ["activate", "compact", "unfurl", "travel"], "tags": []}
    plate = {"device_code": "MF", "device_type": "mobile_fleet", "location": "FAL-1-L4", "status": "idle",
             "features": ["surge", "attach"], "attach_capacity": 36}
    by = {d["device_code"]: d for d in (stn, plate)}
    dl = {"carrier": "MF", "carrier_loc": "FAL-1-L4", "from": "FAL", "to": "KEL", "devices": ["ST1"], "mode": "attach"}
    out = with_compaction(lo.delivery_steps(dl, by, {"KEL": {"entry_point": "KEL-4-L4"}}, False), [stn, plate],
                          {"deep_space_relay_station": {"print_time": 3600}})
    descs = [s["desc"] for s in out]
    u = descs.index("ST1: unfurl before switching on")
    assert descs.index("MF: detach ST1 in KEL") < u < descs.index("activate ST1")
    assert out[u]["wait"] == ["device.unfurled"] and out[u]["wait_device"] == "ST1"
    assert _already_unfurled("Device is unfurling")


def test_feedback_goes_to_the_developers(client):
    world = client.app.state.api.http._transport.app.state.world
    page = client.get("/diagnostics", headers=H).text
    assert "Send feedback to the game" in page
    r = client.post("/diagnostics/feedback", data={"kind": "bug", "body": "Slingshot has no location after a deploy mid-surge",
                                                   "context": "on"}, headers=HX)
    assert "Sent" in r.text and world.feedback[-1]["type"] == "bug"
    assert world.feedback[-1]["body"].startswith("Slingshot has no location") and "web client" in world.feedback[-1]["body"]
    assert "Slingshot has no location" in client.get("/diagnostics", headers=H).text      # listed under Sent
    assert "Write a few words" in client.post("/diagnostics/feedback", data={"kind": "idea", "body": ""}, headers=HX).text


def test_a_cancelled_trip_stops_the_wait(client):
    """travel.cancelled: the device turns back to its origin — a job waiting for its arrival stops waiting."""
    from rsweb.automations import step
    eng = client.app.state.worker.automations
    st = step("SD1 → KEL-3", "/devices/SD1", {"command": "travel", "destination": "KEL-3"}, wait=["travel.arrived"],
              match={"destination": "KEL-3"}, critical=True)
    st["wait_device"] = "SD1"
    job = {"id": "j-cancel", "rule": "fleets", "title": "move SD1", "device": "SD1", "steps": [st], "idx": 0,
           "status": "waiting", "created_at": "2026-10-07T12:00:00+00:00", "meta": {}}
    st["status"] = "waiting"
    client.portal.call(eng.save_jobs, [job])
    client.portal.call(eng.on_event, {"event": "travel.cancelled", "device_code": "SD1",
                                      "payload": {"origin": "KEL-1-L4", "destination": "KEL-3", "return_time_seconds": 300}})
    j = next(x for x in client.portal.call(eng.jobs) if x["id"] == "j-cancel")
    assert j["status"] == "failed" and "returns to KEL-1-L4 (≈5 min)" in j["steps"][0]["error"]


def test_cancel_travel_from_the_device_page(client):
    world = client.app.state.api.http._transport.app.state.world
    world.move_seconds = 300
    drone = next(d for d in world.devices if d["device_type"] == "survey_drone")
    start = drone["location"]
    r = client.post(f"/devices/{drone['device_code']}/command", data={"command": "travel", "f.destination": "SOL-4"}, headers=HX)
    assert "result ok" in r.text, r.text
    client.portal.call(client.app.state.worker.sync_devices)
    page = client.get(f"/devices/{drone['device_code']}", headers=H).text
    assert "Traveling to <b>SOL-4</b>" in page and "Cancel travel" in page
    r = client.post(f"/devices/{drone['device_code']}/cancel-travel", headers=HX)
    assert "result ok" in r.text and "DELETE" in r.text
    assert drone["location"] == start and not drone.get("travel")
    assert any(e["event"] == "travel.cancelled" for e in world.events)
    # the vessel hosting the replicant cancels through its replicant
    rep_code = next(iter(client.portal.call(client.app.state.db.kv_get, "replicants")))
    host = next(d for d in world.devices if d["device_code"] == client.portal.call(client.app.state.db.kv_get, "replicants")[rep_code]["hosted_device_code"])
    r = client.post(f"/devices/{host['device_code']}/cancel-travel", headers=HX)
    assert f"/replicants/{rep_code}/travel" in r.text


def test_desktop_wallpaper_routes_need_a_key_and_stay_read_only(client, monkeypatch):
    import re as _re
    from rsweb import wallpaper as wp
    client.portal.call(client.app.state.worker.sync_devices)
    # off until a link is made: the page and the data are 404, the API without a key too
    assert client.get("/wallpaper/me/").status_code == 404
    assert client.get("/wallpaper/me/api/map.json").status_code == 404
    page = client.get("/account", headers=H).text
    assert "Desktop wallpaper" in page and "Create wallpaper link" in page
    r = client.post("/wallpaper-settings", data={"action": "create", "label": "office PC"}, headers=HX)
    link = _re.search(r'value="(https?://[^"]+/wallpaper/me/#key=(rsw_[^"]+))"', r.text)
    assert link, r.text
    key = link.group(2)
    st = client.portal.call(client.app.state.db.kv_get, wp.KV)
    assert st["enabled"] and key not in str(st) and st["keys"][0]["label"] == "office PC"   # only the hash is stored
    # the page is served (no data in it); the data needs the key, sent as a header
    page = client.get("/wallpaper/me/").text
    assert "wallpaper.js" in page and "importmap" in page and key not in page
    from rsweb.version import VERSION
    assert f"wallpaper.js?v={VERSION}" in page   # versioned: a redeploy is never hidden by a cached script
    assert client.get("/wallpaper/me/").headers["cache-control"] == "no-cache"
    assert client.get("/wallpaper/me/api/map.json").status_code == 401
    assert client.get("/wallpaper/me/api/map.json", headers={wp.HEADER: "rsw_wrong"}).status_code == 401
    good = {wp.HEADER: key}
    data = client.get("/wallpaper/me/api/map.json", headers=good).json()
    assert "stars" in data and "replicants" in data
    systems = client.get("/wallpaper/me/api/systems.json", headers=good).json()["systems"]
    if systems:
        svg = client.get(f"/wallpaper/me/api/system/{systems[0]['star']}", headers=good).text
        assert '<svg class="system"' in svg
    assert client.get("/wallpaper/me/api/system/NOWHERE", headers=good).status_code == 404
    # only the wallpaper's own files are served without signing in
    assert client.get("/wallpaper/me/static/map.js").status_code == 200
    assert client.get("/wallpaper/me/static/vendor/three.module.min.js").status_code == 200
    assert client.get("/wallpaper/me/static/app.js").status_code == 404
    assert client.get("/wallpaper/me/static/../web.py").status_code == 404
    # it can't change anything: no write routes under /wallpaper/
    assert client.post("/wallpaper/me/api/map.json", headers=good).status_code in (404, 405)
    # multi-user: a server answers only for its own slug
    monkeypatch.setenv("WALLPAPER_SLUG", "joe-abc123")
    assert client.get("/wallpaper/me/api/map.json", headers=good).status_code == 404
    assert client.get("/wallpaper/joe-abc123/api/map.json", headers=good).status_code == 200
    monkeypatch.delenv("WALLPAPER_SLUG")
    # turning it off stops every link; revoking stops one
    client.post("/wallpaper-settings", data={"action": "disable"}, headers=HX)
    assert client.get("/wallpaper/me/api/map.json", headers=good).status_code == 404
    client.post("/wallpaper-settings", data={"action": "enable"}, headers=HX)
    assert client.get("/wallpaper/me/api/map.json", headers=good).status_code == 200
    kid = client.portal.call(client.app.state.db.kv_get, wp.KV)["keys"][0]["id"]
    client.post("/wallpaper-settings", data={"action": "revoke", "key_id": kid}, headers=HX)
    assert client.get("/wallpaper/me/api/map.json", headers=good).status_code == 401
    # the settings endpoint itself needs the signed-in user (an htmx request from the app)
    assert client.post("/wallpaper-settings", data={"action": "enable"}, headers=H).status_code == 403


def test_compact_on_an_already_compacted_device_counts_as_done(client, monkeypatch):
    """A move's compact step refused because the device is already compacted is what the step wanted: the job goes
    on. Still compacting: it keeps waiting for device.compacted. Anything else still fails the job."""
    import time
    from rsweb.automations import _already_compact, _already_unfurled, step
    assert _already_compact("Device is already compacted") and _already_compact("Cannot compact: device is compacted")
    assert _already_compact("Device is already compacting") and not _already_compact("Device is not compacted")
    assert not _already_compact("Cannot compact while traveling")
    assert _already_unfurled("Device is not compacted") and _already_unfurled("Device is already unfurled")
    eng = client.app.state.worker.automations
    reply = {}

    async def fake_send(method, path, body, label):
        return False, None, reply["err"]
    monkeypatch.setattr(eng, "send", fake_send)

    def run(err):
        reply["err"] = err
        job = client.portal.call(eng.create_job, "loadouts", "move", None,
                                 [step("X: compact before moving", "/devices/X", {"command": "compact"}, wait=["device.compacted"],
                                       critical=True)], {}, True)
        for _ in range(10):
            client.portal.call(eng.tick)
            j = next(x for x in client.portal.call(eng.jobs) if x["id"] == job["id"])
            if j["status"] not in ("running", "waiting"):
                break
            time.sleep(0.05)
        return j
    j = run("Device is already compacted")
    assert j["status"] == "done" and "already compacted" in j["steps"][0]["note"]
    from rsweb.modular import FOLDED_KV
    assert "X" in client.portal.call(client.app.state.db.kv_get, FOLDED_KV)   # remembered: the next pass sends the carrier
    j = run("Device is already compacting")
    assert j["status"] in ("running", "waiting") and j["steps"][0]["wait"] == ["device.compacted"]
    assert run("Cannot compact while traveling")["status"] == "failed"


def test_tag_changes_never_add_and_remove_the_same_tag():
    """Live 2026-10-07: 'A53A86C0 joins trader-1' was refused: 'Tag appears in both add_tags and remove_tags'."""
    from rsweb import loadouts as lo
    st = lo.tag_step("X", add=["fleet:a"], remove=["fleet:a", "spare"])
    assert st["body"]["configuration"] == {"add_tags": ["fleet:a"], "remove_tags": ["spare"]}


def test_automation_log_kinds_and_toggles(client):
    from rsweb.automations import severity, stop_level
    # owner 2026-10-30: an error is only what no automation will fix; a stopped automated job is a warning
    assert stop_level({"rule": "loadouts"}) == "alert" and stop_level({"rule": "loadouts", "forced": True}) == "error"
    assert severity({"level": "alert", "text": "stopped: loadouts: x — y failed: out of range"}) == "warning"
    assert severity({"level": "alert", "text": "scan of SOL failed: busy"}) == "warning"
    assert severity({"level": "error", "text": "x: waits for your Confirm on the Automations page"}) == "error"
    assert severity({"level": "alert", "text": "Miner 1: stalled in work — no more automatic retries (8 tried)"}) == "error"
    assert severity({"level": "alert", "text": "fleet: 1 change(s): skipped 'A joins b' (Tag appears in both)"}) == "warning"
    assert severity({"level": "alert", "text": "Miner 1: warning: no relay of yours in X"}) == "warning"
    assert severity({"level": "info", "text": "finished: loadouts: X"}) == "info"
    eng = client.app.state.worker.automations
    client.portal.call(eng.log, "chain", "stopped: a — b failed: c", stop_level({"rule": "chain"}))
    client.portal.call(eng.log, "fleets", "skipped 'x' (y)", "alert")
    client.portal.call(eng.log, "fleets", "finished: z")
    page = client.get("/automations", headers=H).text
    assert 'id="log-toggles"' in page and 'data-sev="error"' in page and 'data-sev="warning"' in page and 'data-sev="info"' in page
    assert "errors (1)" in page and "warnings (1)" in page


def test_my_names_include_the_base_name(client):
    from rsweb import notify
    db = client.app.state.db
    client.portal.call(db.kv_set, "replicants", {"A": {"name": "Sk3y-1"}, "B": {"name": "Sk3y-4"}})
    names = client.portal.call(notify.my_names, db)
    assert names == ["Sk3y", "Sk3y-1", "Sk3y-4"]
    assert all(notify.mentions(t, names) for t in ("hi Sk3y", "Sk3y-4 where are you", "sk3y-1?"))
    assert not notify.mentions("Sk3yNet", names)


def test_profile_edit_and_reputation(client):
    from rsweb.web_profile import profile_changes
    world = client.app.state.api.http._transport.app.state.world
    client.portal.call(client.app.state.worker.sync_devices)
    rep = next(iter(client.portal.call(client.app.state.db.kv_get, "replicants")))
    page = client.get(f"/replicants/{rep}", headers=H).text
    assert f"/replicants/{rep}/profile" in page and f"/replicants/{rep}/reputation" in page
    r = client.post(f"/replicants/{rep}/profile", data={"name": "bob-1", "orig_name": "bob-1", "pronouns": "they/them",
                                                        "orig_pronouns": "", "plan": "", "orig_plan": ""}, headers=HX)
    assert world.profile_patches[-1] == {"code": rep, "pronouns": "they/them"}       # only what changed
    r = client.post(f"/replicants/{rep}/profile", data={"name": "Bill", "orig_name": "bob-1"}, headers=HX)
    assert "Name already taken" in r.text
    assert "Nothing changed" in client.post(f"/replicants/{rep}/profile", data={"name": "x", "orig_name": "x"}, headers=HX).text
    assert profile_changes({"pronouns": "x" * 51, "orig_pronouns": ""})[1]
    assert profile_changes({"name": "", "orig_name": "bob"})[1]
    reps = client.portal.call(client.app.state.db.kv_get, "replicants")
    other = next((c for c in reps if c != rep), None)
    text = json.dumps({rep: {"name": "Quill_Ferry", "plan": "Haul relays", "pronouns": "they/them"},
                       "NOPE1234": {"name": "Ghost"}, **({other: {"description": "x" * 501}} if other else {})})
    r = client.post("/profiles/preview", data={"profiles": text}, headers=HX).text
    assert "Apply 1 profile" in r and "not one of your replicants" in r and "unchanged: pronouns" in r
    if other:
        assert "501 characters" in r
    r = client.post("/profiles/import", data={"profiles": text}, headers=HX).text
    assert "saved" in r and world.profile_patches[-1] == {"code": rep, "name": "Quill_Ferry", "plan": "Haul relays"}
    assert client.portal.call(client.app.state.db.kv_get, "replicants")[rep]["name"] == "Quill_Ferry"
    assert "not valid JSON" in client.post("/profiles/preview", data={"profiles": "{"}, headers=HX).text
    r = client.post("/profiles/preview", data={"profiles": json.dumps({"Quill_Ferry": {"name": "Bill"}})}, headers=HX).text
    assert "Apply 1 profile" in r
    assert "Name already taken" in client.post("/profiles/import", data={"profiles": json.dumps({rep: {"name": "Bill"}})},
                                                headers=HX).text
    assert "Bulk profile import" in client.get("/reputation", headers=H).text
    assert "acquainted" in client.get(f"/replicants/{rep}/reputation", headers=HX).text
    page = client.get("/reputation", headers=H).text
    assert "Veth" in page and "intelligent" in page and "curious, aquatic" in page and "events completed" in page


# --- regressions from the 2026-10-08 code review -------------------------------------------------------------------
def test_review_api_blocklist_dot_segments_and_write_retries():
    for path in ("/accounts/./me", "/accounts/me/.", "/accounts/x/../me", "/v1/Accounts/Me", "/accounts%2fme"):
        with pytest.raises(ApiError):
            RSClient.check_allowed("DELETE", path)
    RSClient.check_allowed("GET", "/devices/ABCD/audit")
    from rsweb.api import retry_after
    assert retry_after("7", 5) == 7 and retry_after("garbage", 5) == 5
    assert retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 5) == 0      # a date in the past: go now

    calls = []

    def handler(req):
        calls.append(req.method)
        raise httpx.ReadTimeout("slow", request=req)
    s = Settings(api_token="t", api_base="http://x/v1")
    c = RSClient(s, transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError):
        asyncio.run(c.request("POST", "/devices/A", json_body={"command": "travel"}, retries=2))
    assert calls == ["POST"]                         # a command that timed out may have happened: never resent


def test_review_device_snapshot_gone_and_persistent_short_list():
    from rsweb.ingest import merge_device_snapshot
    prev = [{"device_code": c, "location": "SOL-3"} for c in ("A", "B", "C")]
    new = [{"device_code": "A", "location": "SOL-3"}]
    assert merge_device_snapshot(prev, new) is None                          # a glitchy short list is ignored…
    assert [d["device_code"] for d in merge_device_snapshot(prev, new, gone={"B", "C"})] == ["A"]   # …not real losses
    assert merge_device_snapshot(prev, new, accept_short=True) is not None  # and the same short list again is the truth


def test_review_escaping(client):
    r = client.get("/print-queue/locations?dest_star=<img src=x onerror=alert(1)>", headers=H)
    assert "<IMG" not in r.text and "&lt;IMG" in r.text
    r = client.get("/devices/x');alert(1);('/command-form?command=collect_resources", headers=H)
    assert "clearCargo('" not in r.text


def test_review_engine_stage_failure_is_isolated(client):
    eng = client.app.state.worker.automations
    ran = []

    async def boom():
        raise ValueError("bad template")

    async def fine():
        ran.append(1)
        return []
    eng.run_fleets, eng.hub_watch_pass = boom, fine
    client.portal.call(eng.tick)
    client.portal.call(eng.tick)
    assert ran == [1, 1]                                                       # later stages still ran, each tick
    notes = client.portal.call(client.app.state.db.fetchall, "SELECT title FROM notifications WHERE title LIKE '%run_fleets%'")
    assert len(notes) == 1                                                     # said once, not every minute


def test_taxi_plate_no_controller_runs_goes_home():
    """Live 2026-10-08: Printing Hub 1's four surge plates (tagged taxi, no controller, not in taxi mode) sat idle in
    ITHVALAI — the check flagged them, the planner never sent them home."""
    from rsweb import loadouts as lo
    plate = lambda code, **kw: {"device_code": code, "device_type": "surge_plate", "location": "BBB-BELT-1", "status": "idle",  # noqa: E731
                                "attach_capacity": 1, "features": ["surge", "cruise", "attach", "taxi"],
                                "available_commands": ["attach", "detach", "travel"], **kw}
    devices = [plate("ORPHAN01", tags=["fleet:hub", "taxi"]),
               plate("WORKING1", tags=["fleet:hub", "taxi"], taxi_mode="taxi"),
               {"device_code": "TC", "device_type": "ami_transport_controller", "location": "BBB-BELT-1", "status": "idle",
                "tags": ["ferry"]},
               plate("RUN00001", tags=["fleet:hub", "taxi"], controller_device_code="TC")]
    cfg = lo.normalize({"phases": [], "fleets_migrated": True,
                        "fleets": [{"id": "hub", "name": "Hub", "role": "mining", "home": "AAA", "station": True,
                                    "wants": {"surge_plate": 3}}]})
    stars = {"AAA": {"position": {"x": 0, "y": 0, "z": 0}}, "BBB": {"position": {"x": 2, "y": 0, "z": 0}}}
    p = lo.plan(cfg, devices, [], {}, stars, {}, set(), [], {})
    assert p["moves"].get("ORPHAN01") == "AAA"
    assert "WORKING1" not in p["moves"] and "RUN00001" not in p["moves"]
    flagged = {x["code"]: x["fixed"] for x in lo.audit(cfg, devices, stars, p)}
    assert flagged.get("ORPHAN01") is True and "WORKING1" not in flagged   # in taxi mode: at work, not flagged


def test_bulk_command_on_ticked_devices(client):
    page = client.get("/fleet", headers=H).text
    assert 'class="bulk-pick" name="codes" value="2AC61210"' in page and "Send to ticked" in page
    assert ">Recall (4 here)" in page or "recall" in page.lower()
    assert 'value="set_directive"' not in page.split('id="bulk-cmd"')[1].split("</select>")[0]   # per-device only
    form = client.get("/fleet/bulk-form?command=travel", headers=HX).text
    assert 'name="f.destination"' in form
    # the warning names the device that can't take it
    pick = {"codes": ["2AC61210", "2AC61211", "AF00BEEF"], "command": "recall"}
    warn = client.post("/fleet/bulk-check", data=pick, headers=HX).text
    assert "<b>2</b> of 3 ticked" in warn and "AF00BEEF" in warn and "doesn&#x27;t take recall" in warn
    # a device a running job uses is skipped unless included
    eng, db = client.app.state.worker.automations, client.app.state.db
    jobs = client.portal.call(eng.jobs)
    jobs.append({"id": "j1", "rule": "x", "title": "t", "device": "2AC61211", "steps": [], "idx": 0, "status": "running",
                 "meta": {}})
    client.portal.call(eng.save_jobs, jobs)
    r = client.post("/fleet/bulk", data=pick, headers=HX).text
    assert "recall: 1 of 3 sent" in r and "automation job is using it" in r
    sent = client.portal.call(db.fetchall, "SELECT path, body FROM actions WHERE body LIKE '%recall%'")
    assert [x["path"] for x in sent] == ["/devices/2AC61210"]
    r = client.post("/fleet/bulk", data={**pick, "include_busy": "on"}, headers=HX).text
    assert "recall: 2 of 3 sent" in r
    # nothing ticked / a per-device command
    assert "Tick at least one" in client.post("/fleet/bulk", data={"command": "recall"}, headers=HX).text
    r = client.post("/fleet/bulk", data={"codes": ["MC91FF22"], "command": "set_directive"}, headers=HX).text
    assert "0 of 1 sent" in r and "on its page" in r


def test_owner_handoff_skipped_when_cooperation_allows():
    from rsweb.automations import step
    from rsweb.modular import ownership_hint, with_owner_handoff
    devices = [{"device_code": "CAR", "replicant_code": "A"}, {"device_code": "DRN", "replicant_code": "B"}]
    attach = [step("attach", "/devices/CAR", {"command": "attach", "device": "DRN"})]
    stow = [step("stow", "/devices/DRN", {"command": "stow", "target": "CAR"})]
    assert len(with_owner_handoff(attach, devices)) == 2                                      # private: hand over
    assert len(with_owner_handoff(attach, devices, {"shared": True})) == 1                    # shared account
    assert len(with_owner_handoff(attach, devices, {"public": {"B"}})) == 1                   # A may act on B's drone
    assert len(with_owner_handoff(stow, devices, {"public": {"B"}})) == 2                     # B may not use A's carrier
    assert len(with_owner_handoff(stow, devices, {"public": {"A"}})) == 1
    assert "Replicant cooperation" in ownership_hint("Target host device must belong to this replicant")
    assert ownership_hint("Device is already in motion") == ""


def test_replicant_vessel_travels_last():
    """Without a relay the other carriers are only commandable while the replicant is still there."""
    from rsweb import fleets as fl
    D = lambda code, t, **kw: {"device_code": code, "device_type": t, "location": "AAA-3-L4", "status": "idle",  # noqa: E731
                               "tags": ["fleet:x"], "features": ["surge", "cruise"], **kw}
    devices = [D("HV1", "heaven_vessel", hosting_replicant="R1", stow_capacity=10), D("PL1", "surge_plate", attach_capacity=1),
               D("CV1", "cargo_vessel", stow_capacity=50)]
    steps = fl.travel_steps({"id": "x", "home": "AAA"}, devices, "BBB", {})
    sent = [s["path"].split("/")[-1] for s in steps if (s.get("body") or {}).get("command") == "travel"]
    assert sent[-1] == "HV1" and set(sent) == {"HV1", "PL1", "CV1"}


def test_performance_diagnostics(client):
    """Each request is timed (Server-Timing header); the diagnostics page lists slow pages, stages and storage."""
    from rsweb import perf
    r = client.get("/", headers=H)
    assert "db;desc=" in r.headers.get("server-timing", "")
    assert any(x["path"] == "/" and x["db_n"] > 0 and x["kv"] for x in perf.requests)
    perf.note_stage("logistics_pass", 12.5)
    body = client.get("/diagnostics?perf=1", headers=HX).text
    assert "Engine stages" in body and "logistics_pass" in body and "Largest saved values" in body and "events:" in body
    rep = client.portal.call(perf.report, client.app.state.db)
    assert rep["storage"]["tables"]["kv"] > 0 and rep["by_path"]


def test_dashboard_shows_tutorial_progress(client):
    """GET /tutorials lists progress; the first unfinished one's steps show with the current step's hint."""
    assert 'hx-get="/partials/tutorials"' in client.get("/", headers=H).text
    body = client.get("/partials/tutorials", headers=HX).text
    assert "0 of 2 done" in body and "Exploring the Belt" in body and "3 / 4" in body
    assert "Check your vessel and list of stowed devices." in body and "GET /replicants/:code" in body


def test_game_news_from_the_changelog_and_blog(client, monkeypatch):
    """The changelog and blog are read every 6 hours; the first read is the baseline, later new entries ring the bell."""
    from rsweb import news
    page = {"/changelog/": """<ol class="changelog-list"><li class="changelog-entry" id="v3.5.0"><details>
      <span class="entry-version">v3.5.0</span> <span class="entry-codename">alteration</span>
      <time class="entry-date" datetime="2026-10-13">13 October 2026</time></summary><article class="entry-body">
      <p>Asteroids got busier. Evacuate before the ETA.</p><h3 id="api">API updates</h3><ul><li>New <code>system.impact_warning</code> event.</li></ul>
      </article></details></li></ol>""",
            "/blog/": """<article class="featured"><h2><a href="/blog/7/">Crystal Growth</a></h2>
      <p>Written by <span class="byline-author">Kepler</span>, on <time datetime="2026-09-25">25 September</time></p></article>"""}

    async def fetch(path):
        return page[path]
    monkeypatch.setattr(news, "fetch", fetch)
    eng = client.app.state.worker.automations
    db = client.app.state.db
    assert client.portal.call(eng.news_pass, True) == []            # baseline: no bell for history
    s = client.portal.call(db.kv_get, news.KV)
    assert s["changelog"][0]["sections"]["API updates"] == ["New system.impact_warning event."] and s["blog"][0]["author"] == "Kepler"
    page["/changelog/"] = page["/changelog/"].replace('<ol class="changelog-list">', """<ol class="changelog-list">
      <li class="changelog-entry" id="v3.5.1"><span class="entry-codename">alteration</span><time datetime="2026-10-14"></time>
      <article class="entry-body"><p>A patch.</p></article></li>""")
    assert client.portal.call(eng.news_pass, True) == ["3.5.1"]
    bell = client.portal.call(db.fetchall, "SELECT title, link FROM notifications ORDER BY id DESC LIMIT 1")[0]
    assert bell["title"].startswith("Game update v3.5.1 (alteration): A patch.") and bell["link"].endswith("#v3.5.1")
    body = client.get("/partials/news", headers=HX).text
    assert "v3.5.1" in body and "new" in body and "Crystal Growth" in body


def test_docs_review_2026_10_13_fixes(client):
    """From reading the published docs: v3.5.0 impact events key on the envelope location, a partial diversion keeps
    the object open, recalled arrivals don't satisfy waits, evicted_miners is a count, a device sent to another
    replicant is gone (received isn't), and per-endpoint 429s don't block the whole bucket."""
    from rsweb import api, notify, wardhub as wh
    from rsweb.automations import event_matches
    db, eng = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.insert_event, {"id": "w1", "event": "system.impact_warning", "location": "KELM-OBJ-1",
                                          "created_at": "2026-10-13T10:00:00+00:00",
                                          "payload": {"destination": "KELM-3", "impact_eta": "2099-10-13T11:00:00Z",
                                                      "device_codes": ["D1"]}})
    o = client.portal.call(eng.defence_objects)["KELM-OBJ-1"]
    assert o["impact_target"] == "KELM-3" and o["impact_eta"] == "2099-10-13T11:00:00Z" and not o.get("closed")
    client.portal.call(db.insert_event, {"id": "w2", "event": "system.object_diverted", "location": "KELM-OBJ-1",
                                          "payload": {"outcome": "partial", "impact_target": "KELM-3"}})
    assert not client.portal.call(eng.defence_objects)["KELM-OBJ-1"].get("closed")
    client.portal.call(db.insert_event, {"id": "w3", "event": "system.object_impacted", "location": "KELM-OBJ-1",
                                          "payload": {"impact_target": "KELM-3", "terraform_effects": "craters"}})
    assert client.portal.call(eng.defence_objects)["KELM-OBJ-1"]["closed"] == "impacted"
    assert "hits KELM-3" in notify.describe({"event": "system.impact_warning", "location": "KELM-OBJ-1",
                                             "payload": {"destination": "KELM-3", "impact_eta": "x", "device_codes": ["D1"]}})
    st = {"wait": ["travel.arrived"], "match": {"destination": "SOL"}}
    ev = {"event": "travel.arrived", "device_code": "V", "payload": {"destination": "SOL-3", "recalling": False}}
    assert event_matches(st, ev, "V")
    assert not event_matches(st, {**ev, "payload": {"destination": "SOL-3", "recalling": True}}, "V")
    assert len(wh.evicted({"evicted_miners": 3})) == 3 and "count" in wh.eviction_text(wh.evicted({"evicted_miners": 3}))
    assert api.own_limit("/stars") and api.own_limit("/feedback") and not api.own_limit("/stars/SOL")
    from rsweb.ingest import GONE_SQL, GONE_EVENTS
    for i, (code, direction) in enumerate((("G1", "sent"), ("G2", "received"))):
        client.portal.call(db.insert_event, {"id": f"co{i}", "event": "device.changed_owner", "device_code": code,
                                              "payload": {"direction": direction}})
    rows = client.portal.call(db.fetchall, f"SELECT DISTINCT device_code FROM events WHERE {GONE_SQL} AND device_code IN ('G1','G2')",
                              GONE_EVENTS)
    assert {r["device_code"] for r in rows} == {"G1"}


def test_simulation_prep_run_and_discard(client):
    """Queue a run: a spare compute core is pinned to the interface; start it; the run plays with the bootstrap logic
    and records its build order; at the end its devices / events / scans are purged and the report is kept."""
    from rsweb import sim
    db, eng = client.app.state.db, client.app.state.worker.automations
    client.portal.call(eng.sim_save, {"interface": {"code": "IF000001", "location": sim.DATACENTER}})
    devs = client.portal.call(db.kv_get, "devices")
    core = {"device_code": "CC000001", "device_type": "compute_core", "location": "SOL-3", "status": "idle", "tags": ["spare"]}
    devs.append(core)
    client.portal.call(db.kv_set, "devices", devs)
    client.app.state.api.http._transport.app.state.world.devices.append(dict(core))
    assert "Queue a run" in client.get("/simulations", headers=H).text
    client.post("/simulations/queue", data={"scenario": "mining_rush"}, headers=HX)
    out = client.portal.call(eng.sim_pass)
    assert any("CC000001 pinned" in x for x in out)
    row = client.portal.call(db.fetchone, "SELECT path, body FROM actions ORDER BY id DESC LIMIT 1")
    assert row["path"] == "/devices/CC000001" and "at:mirfaka-obj-1" in row["body"]
    ok, text = client.portal.call(eng.sim_start, "mining_rush", "77F75255")
    assert ok, text
    s = client.portal.call(eng.sim_state)
    assert s["active"]["id"] == 42 and "SIM8F2A1" in s["active"]["devices"] and not s.get("prep")
    assert [d for d in client.portal.call(eng.devices) if d.get("device_code", "").startswith("SIM")] == []
    client.portal.call(eng.sim_pass)                       # scans its system
    client.portal.call(eng.sim_pass)                       # then plays it like a bootstrap's home stage
    client.portal.call(db.insert_event, {"id": "simev", "event": "mining.started", "device_code": "SIM3B7C2",
                                          "star": "VIRTUHOMAM", "location": "VIRTUHOMAM-BELT-1"})
    s = client.portal.call(eng.sim_state)
    assert any(x["step"] == "system scanned" for x in s["active"]["timeline"])
    client.portal.call(eng.sim_finish, s, "completed")
    s = client.portal.call(eng.sim_state)
    assert not s.get("active") and s["runs"][-1]["scenario"] == "mining_rush"
    assert client.portal.call(db.fetchone, "SELECT COUNT(*) AS n FROM events WHERE id='simev'")["n"] == 0
    assert "Reports" in client.get("/simulations", headers=H).text


def test_explained_disappearances_show_with_their_reason(client):
    """Devices that leave the list for a known reason are listed on the Lost devices page with it."""
    from rsweb import lost
    db = client.app.state.db
    for i, (ev, code, payload, loc) in enumerate((("device.destroyed", "D1", {}, "SOL-OBJ-1"),
                                                  ("device.lost", "D2", {}, "SOL-OBJ-1"),
                                                  ("device.changed_owner", "D3", {"direction": "sent", "to_replicant": "R9"}, None))):
        client.portal.call(db.insert_event, {"id": f"g{i}", "event": ev, "device_code": code, "location": loc, "payload": payload})
    client.portal.call(db.insert_event, {"id": "g9", "event": "contract.completed", "created_at": lost._now(),
                                          "payload": {"consumed": {"devices": ["D4"]}}})
    why = client.portal.call(lost.reasons, db, ["D1", "D2", "D3", "D4", "D5"])
    assert why["D1"].startswith("destroyed at SOL-OBJ-1 (asteroid impact)") and why["D2"].startswith("lost in space")
    assert why["D3"] == "sent to replicant R9" and why["D4"] == "paid to a contract" and "D5" not in why
    s = lost.normalize({})
    lost.record_gone(s, [{"code": "D1", "type": "mining_drone", "last_location": "SOL-OBJ-1", "reason": why["D1"]}])
    client.portal.call(db.kv_set, lost.KV, s)
    assert "asteroid impact" in client.get("/lost", headers=H).text


def test_an_impact_warning_is_filed_under_the_asteroids_designation(client):
    """Owner 2026-10-15: an incoming asteroid is named by its location designation. Live, the warning's location was
    KELMORNEA-4-L4 (where devices were): it goes under the asteroid tracked in that system, and devices there leave."""
    from rsweb import evacuate as ev
    db, eng = client.app.state.db, client.app.state.worker.automations
    client.portal.call(db.insert_event, {"id": "d1", "event": "system.object_detected", "location": "KELM-OBJ-2",
                                          "created_at": "2026-10-15T09:00:00+00:00",
                                          "payload": {"impact_target": "KELM-4", "impact_eta": "2099-10-15T11:00:00Z"}})
    client.portal.call(db.insert_event, {"id": "w9", "event": "system.impact_warning", "location": "KELM-4-L4",
                                          "created_at": "2099-10-15T10:00:00+00:00",
                                          "payload": {"destination": "KELM-4", "impact_eta": "2099-10-15T11:00:00Z",
                                                      "device_codes": ["D1"]}})
    objs = client.portal.call(eng.defence_objects)
    assert "KELM-4-L4" not in objs and objs["KELM-OBJ-2"]["also_at"] == ["KELM-4-L4"]
    assert objs["KELM-OBJ-2"]["warned_devices"] == ["D1"]
    assert set(ev.due(objs, warned={"KELM-OBJ-2"})) == {"KELM-OBJ-2", "KELM-4-L4"}
    page = client.get("/defence", headers=H).text
    assert "KELM-OBJ-2" in page and "warned your devices at" in page


def test_docs_coverage_pages_2026_10_15(client):
    """Interfaces for the docs not covered before: terraforming, research, simulation leaderboards, equipment lockers."""
    import json as _json
    db = client.app.state.db
    status = {"body": "SOL-3", "attributes": {"temperature": {"value": 361.7, "delta": 0.2}, "pressure": {"value": 0.34, "delta": 0},
                                              "oxygen": {"value": 5.2, "delta": 0.1}, "toxicity": {"value": 63.9, "delta": -0.4},
                                              "hydrosphere": {"value": 8.1, "delta": 0.05}, "tectonic": {"value": 15, "delta": 0},
                                              "biosphere": {"value": 0, "delta": 0}},
              "projections": {"temperature": {"+6": 360.8}}, "warnings": ["temperature approaching Evaporation threshold (373)"],
              "effects": {"devices": {}, "equilibrium": [{"attribute": "temperature", "direction": "decrease", "strength": "weak"}]}}
    client.portal.call(db.insert_event, {"id": "tf1", "event": "terraform.status", "location": "SOL-3", "payload": status})
    page = client.get("/terraforming", headers=H).text
    assert "SOL-3" in page and "Evaporation" in page and "Toxic suppression" in page and "Bio seeders" in page
    assert "MARYR" not in page
    frag = client.get("/terraforming/kuiper/SOL", headers=HX).text
    assert "SOL-KUIPER-001" in frag
    from rsweb import terraform as tf
    assert [c["rule"] for c in tf.coupling(status) if c["active"]] == ["Toxic suppression"]
    assert {b["attr"] for b in tf.bio_ready(status) if not b["ok"]} == {"temperature", "oxygen", "toxicity", "hydrosphere"}
    assert client.get("/lockers", headers=H).status_code == 200
    assert "Leaderboards" in client.get("/simulations", headers=H).text
    assert "Mining Rush" in client.get("/simulations/leaderboard", headers=HX).text
    assert "Bob" in client.get("/simulations/leaderboard?scenario=mining_rush", headers=HX).text
    # a rock you launched isn't a threat
    from rsweb import defence as dfn
    a = dfn.assess({"designation": "SOL-OBJ-9", "impact_eta": "2099-01-01T00:00:00Z", "launched_by_you": True}, [], {})
    assert a["verdict"] == "launched by you" and not a["threat"]


def test_return_load_takes_what_is_left(client):
    from rsweb.automations import step
    eng = client.app.state.worker.automations
    sent = []

    async def fake_send(method, path, body, why):
        sent.append(dict(body.get("resources") or {}))
        if body.get("resources", {}).get("structural", 0) > 5:
            return False, None, "Insufficient structural at location: need 18.0, have 5"
        return True, {}, None
    orig = eng.send
    eng.send = fake_send
    try:
        for req, want in (("return:X-BELT", "done"), ("contract:EV1", "failed")):
            sent.clear()
            job = client.portal.call(eng.create_job, "logistics", f"short load {req}", "F1",
                                     [step("F1: load 14 conductive, 18 structural at X-BELT", "/devices/F1",
                                           {"command": "collect_resources", "resources": {"conductive": 14, "structural": 18}},
                                           critical=True)], {"devices": ["F1"], "logistics": {"req": req}}, True)
            j = next(x for x in client.portal.call(eng.jobs) if x["id"] == job["id"])
            assert j["status"] == want, (req, j["status"])
            if want == "done":   # a pile going home loads what's there; a contract's price can't come up short
                assert sent == [{"conductive": 14, "structural": 18}, {"conductive": 14, "structural": 5}]
    finally:
        eng.send = orig


def test_dashboard_shows_achievements(client):
    """GET /accounts/achievements (yours) against GET /achievements (all): earned with XP, the rest still to earn."""
    assert 'hx-get="/partials/achievements"' in client.get("/", headers=H).text
    body = client.get("/partials/achievements", headers=HX).text
    assert "1 of 2 earned · 100 XP" in body and "First Scan" in body
    assert "1 still to earn" in body and "Signal Fire" in body and "17 players" in body


def test_simulation_interface_known_and_matched_loosely(client):
    from rsweb import sim
    eng = client.app.state.worker.automations
    s = client.portal.call(eng.sim_state)
    assert s["interface"]["code"] == "F4791EDF" and s["interface"]["location"] == "MIRFAKA-OBJ-1"
    assert sim.is_interface({"device_type": "replicant interface"}) and sim.is_interface({"device_type": "replicant_interface"})
    assert not sim.is_interface({"device_type": "compute_core"})


def test_simulated_world_kept_apart(client):
    db = client.app.state.db
    devices = client.portal.call(db.kv_get, "devices")
    n = len(devices)
    devices += [{"device_code": "459B6E13", "device_type": "heaven_vessel", "location": "VIRTCRYSARON-BELT-1", "status": "idle"},
                {"device_code": "E4B28B5D", "device_type": "mining_drone", "location": None, "status": "stowed",
                 "stowed_in_device_code": "459B6E13"}]
    client.portal.call(db.kv_set, "devices", devices)
    assert len(client.portal.call(db.kv_get, "devices")) == n
    assert {d["device_code"] for d in client.portal.call(db.kv_get, "sim:devices")} == {"459B6E13", "E4B28B5D"}
    client.portal.call(db.kv_set, "inventory", [{"location": "VIRTCRYSARON-BELT-1", "items": {"silicates": 40}},
                                                {"location": "SOL-BELT-1", "items": {"structural": 5}}])
    assert [x["location"] for x in client.portal.call(db.kv_get, "inventory")] == ["SOL-BELT-1"]
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("VIRTCRYSARON", '{"star": "VIRTCRYSARON"}', "2026-10-16"))
    assert not client.portal.call(db.fetchall, "SELECT star FROM systems WHERE star='VIRTCRYSARON'")
    assert "VIRTCRYSARON" in client.portal.call(db.kv_get, "sim:systems")
    w = client.app.state.worker
    client.portal.call(w.handle_event, _ev(990, "scan.completed", device="459B6E13", location="VIRTCRYSARON-3"))
    row = client.portal.call(db.fetchone, "SELECT category FROM events WHERE id='1990-0'")
    assert row["category"] == "simulation"
    client.portal.call(db.kv_set, "devices", devices)   # (the mock's own syncs list no simulated devices)
    t = client.get("/simulations", headers=H).text
    assert "Virtual galaxy" in t


def test_build_order_parse_and_play():
    from rsweb import sim_order
    steps, errors = sim_order.parse("scan system\ndeploy all\nmine: structural, conductive\nprint 2 mining_drone\n"
                                    "wait: 500 structural\nmine until objective\n# note\nfly away")
    assert [s["kind"] for s in steps] == ["scan", "deploy", "mine", "print", "wait", "mine_all"]
    assert errors == ["line 8: not understood: fly away"]
    a = {"order": steps}
    belt, now = "VIRTX-BELT-1", "2026-10-16T00:00:00+00:00"
    vessel = {"device_code": "V1", "device_type": "heaven_vessel", "location": "VIRTX-3-L4", "status": "idle"}
    devs = [vessel, {"device_code": "M1", "device_type": "mining_drone", "stowed_in_device_code": "V1"},
            {"device_code": "M2", "device_type": "mining_drone", "stowed_in_device_code": "V1"}]
    acts, note = sim_order.advance(a, devs, vessel, belt, {}, True, now)
    assert acts == [{"kind": "travel", "device": "V1", "to": belt}] and a["order_idx"] == 1   # scan done, flying out
    vessel["location"] = belt
    acts, _ = sim_order.advance(a, devs, vessel, belt, {}, True, now)
    assert [x["kind"] for x in acts] == ["deploy", "deploy"]
    for d in devs[1:]:
        d.pop("stowed_in_device_code"); d["location"], d["status"] = belt, "idle"
    acts, _ = sim_order.advance(a, devs, vessel, belt, {}, True, now)
    assert [(x["device"], x["resource"]) for x in acts] == [("M1", "structural"), ("M2", "conductive")]
    for d in devs[1:]:
        d["status"] = "mining"
    acts, _ = sim_order.advance(a, devs, vessel, belt, {}, True, now)
    assert acts == [{"kind": "print", "device_type": "mining_drone"}] and a["order_idx"] == 3
    vessel["status"] = "printing"
    assert sim_order.advance(a, devs, vessel, belt, {}, True, now)[0] == []
    devs += [{"device_code": f"M{i}", "device_type": "mining_drone", "location": belt, "status": "mining"} for i in (3, 4)]
    vessel["status"] = "idle"
    acts, note = sim_order.advance(a, devs, vessel, belt, {"structural": 120}, True, now)
    assert acts == [] and "120 so far" in note and a["order_idx"] == 4
    acts, note = sim_order.advance(a, devs, vessel, belt, {"structural": 600}, True, now)
    assert a["order_idx"] == 5 and note == "mine until objective"


def test_build_order_saved_and_followed(client):
    eng = client.app.state.worker.automations
    r = client.post("/simulations/orders", data={"name": "Fast", "text": "scan system\nmine: structural 4, conductive 2, carbon, silicates\nprint 1 mining_drone"}, headers=HX)
    assert "Saved Fast (3 steps)" in r.text
    assert "Not saved" in client.post("/simulations/orders", data={"name": "Bad", "text": "dance"}, headers=HX).text
    page = client.get("/simulations", headers=H).text
    assert "Build orders" in page and "the next run follows <b>Fast</b>" in page
    assert "boAdd('deploy all')" in page and 'class="bo-res" value="carbon"' in page and "<option>mining_drone</option>" in page.replace(" selected", "")
    ok, _ = client.portal.call(eng.sim_start, "mining_rush", "77F75255")
    a = client.portal.call(eng.sim_state)["active"]
    assert ok and a["order_name"] == "Fast" and [x["kind"] for x in a["order"]] == ["scan", "mine", "print"]


def test_simulation_runs_end_early_and_world(client):
    eng = client.app.state.worker.automations
    ok, _ = client.portal.call(eng.sim_start, "mining_rush", "77F75255")
    assert ok
    t = client.get("/simulations/active", headers=HX).text
    assert "#42" in t and "End early" in t and 'href="/fleet?sim=42"' in t and 'href="/map?sim=42"' in t
    w = client.get("/fleet?sim=42", headers=H).text
    assert "Simulation #42" in w and "SIM8F2A1" in w and "11ADA230" not in w and "REAL0001" not in w   # only the run's
    assert 'dataUrl: "/api/map.json?sim=42"' in client.get("/map?sim=42", headers=H).text
    j = client.get("/api/map.json?sim=42&part=core", headers=H).json()
    assert "11ADA230" not in str(j)
    assert "11ADA230" in client.get("/fleet", headers=H).text   # the real list is untouched
    r = client.post("/simulations/end", data={"sim_id": "42"}, headers=HX)
    assert r.status_code == 200
    s = client.portal.call(eng.sim_state)
    assert not s.get("active") and s["runs"][-1]["id"] == 42
    assert "No simulation running" in client.get("/simulations/active", headers=HX).text


def test_lookbacks_compare_real_times_not_the_games_offset(client):
    """The game stamps events in -04:00 (live 2026-10-20): a print 46 h ago is inside the 48 h window, though its text
    sorts before a UTC cutoff."""
    from rsweb import lost
    db, w = client.app.state.db, client.app.state.worker
    at = (datetime.now(timezone.utc) - timedelta(hours=46)).astimezone(timezone(timedelta(hours=-4)))
    ev = {**_ev(2101, "print.started", device="FAC00002", device_type="ami_mining_controller",
                component_device_codes=["OLD00001"]), "created_at": at.isoformat(timespec="seconds")}
    client.portal.call(w.handle_event, ev)
    assert client.portal.call(lost.reasons, db, ["OLD00001"]) == {"OLD00001": "used in a print"}


def test_spec_endpoints_pages_and_cards(client):
    t = client.get("/leaderboards", headers=H).text
    assert 'hx-get="/partials/leaderboard/xp"' in t and "Distance" in t
    assert "101" in client.get("/partials/leaderboard/xp", headers=HX).text
    assert "Game API ok" in client.get("/partials/health", headers=HX).text
    x = client.get("/partials/system-extra/SOL", headers=HX).text
    assert "Famine Signal" in x and "structural 120" in x and "FARAWAYX" in x
    cat = client.portal.call(client.app.state.db.kv_get, "stars")
    assert any(s.get("designation") == "FARAWAYX" for s in cat["stars"])   # nearby stars join the map
    assert "Arrived at SOL-3" in client.get("/partials/replicant-events/77F75255", headers=HX).text
    client.post("/devices/HV2E8F3A/permissions", data={"permission_type": "command", "account_id": "42"}, headers=HX)
    p = client.get("/partials/permissions/HV2E8F3A", headers=HX).text
    assert "command" in p and ">42<" in p and "revoke" in p
    b = client.get("/blog", headers=H).text
    assert "New shop location" in b and "Write a post" in b
    client.post("/blog", data={"replicant_name": "Bob", "subject": "Hello", "content": "From the app"}, headers=HX)
    assert "Hello" in client.get("/blog", headers=H).text
    assert 'href="/leaderboards"' in client.get("/messages", headers=H).text and 'hx-get="/partials/health"' in client.get("/", headers=H).text


def test_assemble_stops_controllers_and_scans_are_waited_out(client):
    from rsweb import fleets as fl
    from rsweb.automations import step
    fleet = {"id": "m5", "name": "Miner 0.5", "role": "mining", "home": "BADU", "wants": {}}
    t = ["fleet:m5"]
    devices = [{"device_code": "CAR9", "device_type": "cargo_vessel", "location": "BADU-3-L4", "status": "idle", "tags": t,
                "features": ["surge", "cruise", "attach", "stow"], "attach_capacity": 3, "stow_capacity": 20},
               {"device_code": "TC1", "device_type": "ami_transport_controller", "location": "BADU-3-L4", "status": "coordinating",
                "tags": t, "ami_directive": {"name": "gather"}},
               {"device_code": "TD1", "device_type": "transport_drone", "location": "BADU-BELT-1", "status": "collecting",
                "controller_device_code": "TC1", "tags": t, "available_commands": ["travel"]}]
    steps, _ = fl.assemble_steps(fleet, devices)
    cmds = [(s["path"].split("/")[2], (s.get("body") or {}).get("command")) for s in steps if s.get("body")]
    assert cmds[:2] == [("TC1", "clear_directive"), ("TC1", "release")] and ("TD1", "travel") in cmds
    eng = client.app.state.worker.automations
    calls = []

    async def send(method, path, body, label):
        calls.append((path, (body or {}).get("command")))
        if len(calls) == 1:
            return False, None, "Cannot cruise while scanning"
        return True, {}, None
    real = eng.send
    eng.send = send
    try:
        job = client.portal.call(eng.create_job, "fleets", "scan test", "SD1",
                                 [step("SD1 → X-1 (board)", "/devices/SD1", {"command": "travel", "destination": "X-1"})],
                                 {"devices": []}, True)
    finally:
        eng.send = real
    j = next(x for x in client.portal.call(eng.jobs) if x["id"] == job["id"])
    assert j["status"] != "failed" and j["steps"][1]["wait"] == ["scan.completed", "search.completed"]
    assert j["steps"][2]["scan_retry"] and j["steps"][2]["body"]["command"] == "travel"


def test_device_page_links_host_and_cargo_codes(client):
    db = client.app.state.db
    devices = client.portal.call(db.kv_get, "devices")
    devices += [{"device_code": "HOSTV001", "device_type": "cargo_vessel", "location": "SOL-3", "status": "idle",
                 "stowed_devices": ["RIDE0001"], "attached_devices": [{"device_code": "TOW00001", "device_type": "autofactory"}]},
                {"device_code": "RIDE0001", "device_type": "mining_drone", "location": None, "status": "stowed",
                 "stowed_in_device_code": "HOSTV001"}]
    client.portal.call(db.kv_set, "devices", devices)
    t = client.get("/devices/RIDE0001", headers=H).text
    assert '<a class="mono" href="/devices/HOSTV001">HOSTV001</a>' in t
    t = client.get("/devices/HOSTV001", headers=H).text
    assert 'href="/devices/RIDE0001">RIDE0001</a>' in t and 'href="/devices/TOW00001">TOW00001</a>' in t


def test_simulation_next_step_follows_a_finished_job(client):
    """A run's clock is fast: when one of its jobs finishes, its next step is played at once, not on the next tick."""
    eng = client.app.state.worker.automations
    calls = []

    async def fake_pass(s):
        calls.append(s["active"]["id"])
        return []
    ok, _ = client.portal.call(eng.sim_start, "mining_rush", "77F75255")
    assert ok
    eng.sim_run_pass = fake_pass
    job = {"id": "simjob", "title": "t", "rule": "simulation", "status": "running", "idx": 0, "steps": [],
           "meta": {"sim": 42}}

    async def finish():
        jobs = await eng.jobs()
        await eng.save_jobs(jobs + [job])
        await eng._advance("simjob")
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
    client.portal.call(finish)
    assert calls == [42]


def test_simulations_listed_per_interface_and_approved(client):
    """Every known interface's scenarios (GET /devices/{code}/simulate) are listed on the Simulations page; approving
    one queues it at that interface, like a contract: its entry cost and a replicant go there. A null entry cost is free."""
    from rsweb import others as oth, sim
    db, eng = client.app.state.db, client.app.state.worker.automations
    snap = oth.normalize({})
    snap["stars"]["ALGOL"] = {"scanned_at": "2026-10-17T00:00:00+00:00", "devices": [], "drones": [],
                              "raw": [{"device_code": "IF2", "device_type": "replicant_interface", "location": "ALGOL-OBJ-2",
                                       "owner_name": "Bill"}]}
    client.portal.call(db.kv_set, oth.KV, snap)
    t = client.post("/simulations/scan", headers=HX).text
    assert "IF2 at ALGOL-OBJ-2 lists 2 scenario(s)" in t
    page = client.get("/simulations", headers=H).text
    assert "ALGOL-OBJ-2" in page and "Terraforming Training" in page and "free" in page and "Approve" in page
    t = client.post("/simulations/queue", data={"scenario": "terraforming_training_1", "interface": "IF2"}, headers=HX).text
    assert "a replicant goes (no entry cost) to ALGOL-OBJ-2" in t
    s = client.portal.call(eng.sim_state)
    assert s["interface"]["code"] == "IF2" and s["prep"]["location"] == "ALGOL-OBJ-2"
    assert sim.cost(sim.scenario(s, "terraforming_training_1")) == ("compute_core", 0)
    assert "approved" in client.get("/simulations", headers=H).text


def test_terraforming_training_controls(client):
    """The terraforming training is played by hand: the Terraforming page shows a run's devices and its monitor's
    reports (?sim=), with habitable ranges and a switch for each kind of device; the engine leaves the run alone; a
    run's world never shows among the real ones."""
    import time
    from rsweb import sim
    db, eng = client.app.state.db, client.app.state.worker.automations
    devs = [{"device_code": f"M{i}", "device_type": "orbital_mirror", "location": "VIRTCEL-1", "status": "idle"} for i in range(3)]
    devs.append({"device_code": "RG1", "device_type": "atmospheric_regulator", "location": "VIRTCEL-1", "status": "idle"})
    client.portal.call(db.kv_set, "sim:world:7", {"t": time.time(), "rep": "R1", "devices": devs, "stars": [],
                                                  "inventory": [], "replicants": {}, "totals": {}})
    client.portal.call(db.insert_event, {"id": "tf0", "event": "terraform.status", "location": "VIRTCEL-1",   # an earlier run's
                                          "created_at": "2026-01-01T00:00:00+00:00",
                                          "payload": {"body": "VIRTCEL-1", "attributes": {"temperature": {"value": 999}}}})
    client.portal.call(db.kv_set, sim.KV, {"active": {"id": 7, "name": "Terraforming Training", "scenario": "terraforming_training_1",
                                                      "started_at": "2026-10-01T00:00:00+00:00"}})
    client.portal.call(db.insert_event, {"id": "tf1", "event": "terraform.status", "location": "VIRTCEL-1",
                                          "payload": {"body": "VIRTCEL-1", "attributes": {"temperature": {"value": 410, "delta": -1},
                                                                                         "oxygen": {"value": 12, "delta": 0}}}})
    t = client.get("/terraforming?sim=7", headers=H).text
    # judged for humans by default (GET /species environment, live 2026-10-17)
    assert "VIRTCEL-1" in t and "0/3 on" in t and "270–300 K" in t and "18–25 %" in t and "2 out of range: temperature, oxygen" in t
    assert 'name="codes" value="M0 M1 M2"' in t and "Not ready: temperature, oxygen" in t and "out of range for Humans" in t
    assert 'class="tf-switch"' in t and 'hx-trigger="every 15s' in t   # a console: switches, refreshes itself
    assert "999" not in t   # an earlier run's report: neither shown nor graphed
    assert t.index("<b>Control</b>") < t.index("0/3 on") and "fixes" not in t   # mirrors heat: no help at 410 K
    t = client.get("/terraforming?sim=7&species=branthar", headers=H).text   # another species, remembered
    assert "300–340 K" in t and "1 out of range: temperature" in t
    assert "300–340" in client.get("/terraforming?sim=7", headers=H).text
    client.get("/terraforming?sim=7&species=humans", headers=H)
    assert "VIRTCEL-1" not in client.get("/terraforming", headers=H).text
    # live 2026-10-17: the running monitor carries its report (terraform_status); numbers from the training run
    from rsweb import terraform as tf
    hum = tf.species_targets({"environment": {"temperature": [270, 300], "gravity": [0.7, 1.3]}})
    assert hum == {"temperature": (270, 300)} and tf.in_range("temperature", 305.45, hum) is False
    assert tf.range_eta("toxicity", 60.75, -0.7737) == {"enter": 53} and tf.range_eta("oxygen", 7.81, 0.5658) == {"enter": 4}
    assert tf.range_eta("temperature", 350.15, 0.03) == {"away": True} and tf.range_eta("hydrosphere", 10.15, 0.5) == {"ok": True}
    assert tf.range_eta("temperature", 349.0, 0.5) == {"leave": 3}
    assert tf.range_eta("toxicity", 0, -0.1, {"toxicity": (0, 5)}) == {"ok": True}   # can't go below 0
    devs.append({"device_code": "MON", "device_type": "terraform_monitor", "location": "VIRTOTHER-2", "status": "monitoring",
                 "terraform_status": {"body": "VIRTOTHER-2", "attributes": {"toxicity": {"value": 60.75, "delta": -0.7737}}}})
    client.portal.call(db.kv_set, "sim:world:7", {"t": time.time(), "rep": "R1", "devices": devs, "stars": [],
                                                  "inventory": [], "replicants": {}, "totals": {}})
    t = client.get("/terraforming?sim=7", headers=H).text
    assert "VIRTOTHER-2" in t and "in range in 73 ticks" in t   # humans: toxicity under 5
    assert "1 out of range: toxicity" in t and "2 out of range: temperature, oxygen" in t
    assert tf.out_of_range({"attributes": {"temperature": {"value": 300}, "toxicity": {"value": 5}}}) == []
    devs[3]["status"] = "regulating"   # live 2026-10-17: the regulator can be switched off again; how long it has run shows
    client.portal.call(db.insert_event, {"id": "rg1", "event": "terraform.device_activated", "device_code": "RG1", "created_at": "2026-10-17T22:30:13+00:00",
                                          "location": "VIRTCEL-1", "payload": {}})
    client.portal.call(db.kv_set, "sim:world:7", {"t": time.time(), "rep": "R1", "devices": devs, "stars": [],
                                                  "inventory": [], "replicants": {}, "totals": {}})
    assert "regulating since" in client.get("/terraforming?sim=7", headers=H).text
    # the console's live refresh and a switch: both answer with the world's partial
    w = client.get("/terraforming/world/VIRTCEL-1?sim=7", headers=HX).text
    assert w.lstrip().startswith('<div class="card tf-world"') and "regulating since" in w
    w = client.post("/terraforming/M0/power?sim=7", data={"on": "1", "body": "VIRTCEL-1"}, headers=HX).text
    assert "tf-world" in w and "activate M0" in w
    # what was just commanded shows until the game agrees (the switch doesn't flip back on a stale read)
    import time as _t
    from rsweb.web_terraform import PENDING_KV, _overlay, _remember
    client.portal.call(lambda: _remember(db, "M0", on=True))
    pend = client.portal.call(db.kv_get, PENDING_KV)
    assert pend["M0"]["on"] is True
    row = _overlay({"device_code": "M0", "device_type": "orbital_mirror", "status": "idle"}, pend)
    assert row["_on"] is True and row["_pending"]
    row = _overlay({"device_code": "M0", "device_type": "orbital_mirror", "status": "terraforming"}, pend)
    assert row["_on"] is True and not row.get("_pending")
    old = {"M0": {**pend["M0"], "at": _t.time() - 600}}
    assert _overlay({"device_code": "M0", "status": "idle"}, old)["_on"] is False   # stale command: the game wins
    row = _overlay({"device_code": "A1", "status": "terraforming", "settings": {"direction": "increase"}},
                   {"A1": {"set": {"direction": "decrease"}, "at": _t.time()}})
    assert row["_set"]["direction"] == "decrease" and row["_pending"] and row["_set_pending"]
    # settings are read from the device list's `settings` (live 2026-10-17), so the console shows what's set
    assert tf.settings_of({"settings": {"direction": "decrease", "strength": 0.5}}) == {"strength": 0.5, "direction": "decrease"}
    sp = tf.spark([1, 2, 3], (2, 4))
    assert sp["band"] and len(sp["pts"].split()) == 3
    assert not sim.plays({"code": "terraforming_training_1", "objective_type": "terraform_training"})
    assert sim.plays({"code": "mining_rush", "objective_type": "yield_quota"})
    s = client.portal.call(eng.sim_state)
    s["active"] = {"id": 7, "scenario": "terraforming_training_1", "replicant": "R1", "devices": [], "stars": [], "scans": {}}
    assert client.portal.call(eng.sim_run_pass, s) == []
    client.portal.call(eng.sim_purge, {"id": 7})   # the run ends: its world's reports go, whatever it recorded
    assert not client.portal.call(db.fetchall, "SELECT 1 FROM events WHERE location = 'VIRTCEL-1'")
    client.portal.call(db.kv_set, sim.KV, {})
    assert "410" not in client.get("/terraforming?sim=7", headers=H).text


def test_terraforming_candidates():
    """Map › Terraforming candidates: surveyed planets and moons, read on request, rated for a species and ranked by the
    estimated time to habitable; gravity out of range, gas giants and intelligent natives are dropped."""
    import json
    from rsweb import tfcandidates as tc
    hum = {"temperature": (270, 300), "pressure": (0.8, 1.2), "oxygen": (18, 25), "toxicity": (0, 5),
           "hydrosphere": (40, 70), "biosphere": (30, 70), "tectonic": (5, 30), "gravity": (0.7, 1.3)}
    earthish = {"surface_temp_k": 280, "atmo_pressure_atm": 1.0, "atmo_o2_pct": 20, "atmo_toxicity": 1, "hydrosphere_pct": 50,
                "biosphere_index": 40, "tectonic_index": 10, "surface_gravity": 1.0, "type": "rocky"}
    r = tc.rate_body(earthish, hum)
    assert r["ticks"] == 0 and not r["blocked"] and not r["dropped"]
    cold = {**earthish, "surface_temp_k": 250, "biosphere_index": None}   # 20 K short; no biosphere yet
    r = tc.rate_body(cold, hum, kit=1)
    assert r["attrs"]["temperature"]["ticks"] == 37 and r["attrs"]["temperature"]["devices"] == ["orbital_mirror", "thermal_lance"]
    assert r["attrs"]["biosphere"]["ticks"] == 100 and r["ticks"] == 100   # 250 K meets the bio seeder preconditions already
    assert tc.rate_body({**earthish, "atmo_toxicity": None, "atmosphere": False, "atmo_pressure_atm": None}, hum)["attrs"]["pressure"]["gap"] == 0.8
    assert tc.rate_body({**earthish, "biosphere_index": 90}, hum)["blocked"] == ["biosphere"]   # nothing lowers it
    assert "gravity" in tc.rate_body({**earthish, "surface_gravity": 0.11}, hum)["dropped"]
    assert tc.rate_body({**earthish, "type": "Gas Giant"}, hum)["dropped"]
    assert tc.bodies_to_read({"X": {"planets": [{"designation": "X-1", "moon_count": 2}, {"designation": "X-2", "moon_count": 1}]}},
                             {"X-1": "t", "X-2-1": "t"}, {}) == ["X-1", "X-1-1", "X-1-2", "X-2-1"]
    assert tc.bodies_to_read({"X": {"planets": [{"designation": "X-1", "moon_count": 0}]}}, {}, {"X": "t"}) == ["X-1"]


def test_terraforming_candidates_page(client):
    import json
    db = client.app.state.db
    client.portal.call(db.execute, "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                       ("SOL", json.dumps({"planets": [{"designation": "SOL-3", "moon_count": 1}]}), "2026-10-20T00:00:00+00:00"))
    client.portal.call(db.kv_set, "surveyed", {"SOL-3": "2026-10-20T00:00:00+00:00"})
    t = client.get("/terraforming/candidates", headers=H).text
    assert "2 surveyed body/bodies not read yet" in t and "Terraforming candidates" in t
    out = client.post("/terraforming/candidates/read", headers=HX).text
    assert "Read 2 body/bodies" in out
    assert "Read 0" in client.post("/terraforming/candidates/read", headers=HX).text   # read within the week: left alone
    t = client.get("/terraforming/candidates?species=humans&kit=3", headers=H).text
    assert "SOL-3</a>" in t and "Every surveyed body has been read" in t and "270–300" in t
    assert "1 read but not surveyed yet" not in t
    t = client.get("/terraforming/candidates?max_ly=5", headers=H).text   # the mock's autofactory is in SOL
    assert "SOL-3</a>" in t and "0.0 ly" in t


def test_terraforming_groups_keep_a_fixed_order():
    """Monitor and regulator share one Control card, first; the other kinds follow a fixed order by what they primarily
    do (never moving while in use); what's out of range is only marked (fixes …)."""
    from rsweb import terraform as tf
    st = {"attributes": {"temperature": {"value": 410}, "oxygen": {"value": 12}, "toxicity": {"value": 2}}}
    assert tf.needs(st, {"temperature": (270, 300), "oxygen": (18, 25), "toxicity": (0, 5)}) == {"temperature": -1, "oxygen": 1}
    types = ["bio_seeder", "orbital_shade", "atmospheric_regulator", "gas_separator", "orbital_mirror", "cryo_disperser"]
    assert sorted(types, key=tf.priority) == [
        "atmospheric_regulator", "orbital_mirror", "orbital_shade", "cryo_disperser", "gas_separator", "bio_seeder"]


def test_device_list_filters_by_owning_replicant(client):
    """Fleet › Device list filters by the replicant owning a device (by name), e.g. to reassign the devices of one gone
    into a simulation with change_owner."""
    db = client.app.state.db
    client.portal.call(db.kv_set, "replicants", {"AAAA0001": {"replicant_code": "AAAA0001", "name": "bob-1"},
                                                 "BBBB0002": {"replicant_code": "BBBB0002", "name": "riker-2"}})
    client.portal.call(db.kv_set, "devices", [
        {"device_code": "D1", "device_type": "mining_drone", "location": "SOL-1", "replicant_code": "AAAA0001"},
        {"device_code": "D2", "device_type": "mining_drone", "location": "SOL-1", "replicant_code": "BBBB0002"}])
    page = client.get("/fleet", headers=H).text
    assert "any owner" in page and "riker-2 (BBBB0002)" in page
    t = client.get("/fleet?owner=BBBB0002", headers={**HX, "HX-Target": "fleet-table"}).text
    assert "D2" in t and "D1" not in t


def test_device_scans_keep_lockers(client):
    """Arrival / traffic device scans record equipment lockers (2026-10-22): the Lockers and System pages list them
    with their code, and the row's Retrieve uses it without another scan."""
    from rsweb import others as oth
    db = client.app.state.db
    s = oth.normalize({})
    scanned = [{"device_code": "70C835AC", "device_type": "equipment_locker", "location": "TWAFFY-OORT",
                "owner_name": "Bill", "owner_replicant_code": "A8F48B26"},
               {"device_code": "EL000002", "device_type": "equipment_locker", "location": "NOVA-OORT", "owner_name": "Bill"}]
    oth.record_scan(s, "TWAFFY", scanned, set())
    oth.prune(s)
    assert s["lockers"]["TWAFFY-OORT"]["code"] == "70C835AC" and "NOVA-OORT" in s["lockers"]
    assert any(d["type"] == "equipment_locker" for d in s["stars"]["TWAFFY"]["devices"])
    client.portal.call(db.kv_set, oth.KV, s)
    page = client.get("/lockers", headers=H).text
    assert "70C835AC" in page and "NOVA-OORT" in page and "EL000002" in page
    assert "NOVA-OORT" in client.get("/systems/NOVA", headers=H).text
    r = client.post("/lockers/retrieve_at", data={"location": "TWAFFY-OORT"}, headers=HX).text
    assert "retrieved" in r and "HV2E8F3A" in r, r


def test_dashboard_story_card_shows_the_game_overview(client):
    client.portal.call(client.app.state.worker.sync_overview)
    page = client.get("/partials/story", headers=H).text
    assert "Alteration Protocol" in page and "The Shattering of Ethirel" in page and "/systems/ETHIREL" in page
    assert "Terraforming the Colony Worlds" in page and "Riker" in page
    assert 'id="story"' in client.get("/", headers=H).text
    # the live spec's fields (2026-10-20): visible_until, reward, locations (names or {name, system}), the Ark's schedule
    assert "until 2026-10-31" in page and "Reward: Blueprint for a mining_rig." in page and "stop 12/12" in page
    assert "Season likely ends <b>2026-10-31</b>" in page
    from rsweb import season
    assert season.likely_end({"events": [{"visible_until": "2026-10-31"}], "storylines": [{"visible_until": "2026-10-20"}]})[0] == "2026-10-31"
    assert season.likely_end({"season": {"ends_at": "2026-11-30T00:00:00Z"}})[0] == "2026-11-30" and season.likely_end({}) is None
    assert '/systems/TRYNLLYR">TRYNLLYR</a>' in page and '/systems/MIRFAKA">Deep Space Tracking Array (MIRFAKA)</a>' in page


def test_relocation_waits_for_command_reach_at_the_new_home(client):
    """Live 2026-10-19: Miner_4 relocated to OXETH (no relay, no replicant aboard) and everything aboard went
    out_of_range before it could be unloaded."""
    eng = client.app.state.worker.automations
    ves = {"device_code": "V", "device_type": "cargo_vessel", "location": "AAA-1", "status": "idle", "tags": ["fleet:m"],
           "features": ["surge", "cruise"], "stow_capacity": 2}
    d1 = {"device_code": "D1", "device_type": "survey_drone", "location": "AAA-1", "status": "idle", "tags": ["fleet:m"],
          "features": ["cruise", "stow"]}
    f = {"id": "m", "name": "M", "role": "explore", "home": "BBB", "station": True, "wants": {}}
    reach = {"AAA"}

    async def commandable(devices, stars=None, without=None):
        return reach
    eng.commandable = commandable
    how = client.portal.call(eng.start_relocation, f, "AAA", "BBB", [ves, d1])
    assert "outside an active relay network" in how and f["home"] == "AAA" and f["pending_home"] == "BBB" and "mission" not in f
    # a mission already under way stalls before the travel phase
    m = {"status": "running", "targets": ["BBB"], "idx": 0, "relocate": True, "log": []}
    steps, problems = client.portal.call(eng.fleet_phase_steps, f, m, "travel", [ves, d1])
    assert steps == [] and m.get("stall") and "BBB" in problems[0]
    reach.add("BBB")   # a relay of an active network there now: it goes
    assert "relocation mission" in client.portal.call(eng.start_relocation, f, "AAA", "BBB", [ves, d1])


def test_data_upload_at_interface(client):
    """v3.6.2: a data-feature device deployed at the interface, with a replicant there, can be uploaded (consumed);
    elsewhere or stowed it only says why not. device.discovered raises a notification."""
    from rsweb import notify, sim
    db, eng = client.app.state.db, client.app.state.worker.automations
    devs = client.portal.call(db.kv_get, "devices")
    reps = client.portal.call(db.kv_get, "replicants") or {}
    rep_loc = next(r.get("location") for r in (reps.values() if isinstance(reps, dict) else reps) if r.get("location"))
    client.portal.call(eng.sim_save, {"interface": {"code": "IF000001", "location": rep_loc}})
    chip = {"device_code": "GC000001", "device_type": "guidance_chip", "location": rep_loc, "status": "idle", "features": ["data"]}
    far = {"device_code": "GC000002", "device_type": "guidance_chip", "location": "NOWHERE-1", "status": "idle", "features": ["data"]}
    client.portal.call(db.kv_set, "devices", devs + [chip, far])
    client.app.state.api.http._transport.app.state.world.devices.append(dict(chip))
    assert "Data uploads" in client.get("/simulations", headers=H).text
    frag = client.get("/simulations/uploads", headers=HX).text
    assert "/simulations/upload/GC000001" in frag and "/simulations/upload/GC000002" not in frag and "not at the interface" in frag
    r = client.post("/simulations/upload/GC000001", headers=HX).text
    assert "analysis chute" in r or "upload" in r.lower()
    world = client.app.state.api.http._transport.app.state.world
    assert not any(d.get("device_code") == "GC000001" for d in world.devices)
    # the cached device list may still hold the consumed chip: it leaves the list, a count stays
    frag = client.get("/simulations/uploads", headers=HX).text
    assert "GC000001" not in frag and "Uploaded before: 1 (1 guidance chip)" in frag and "GC000002" in frag
    # uploads sent before 1.272.0 are recovered from the action log once
    client.portal.call(db.kv_set, "sim:uploaded", None)
    client.portal.call(db.execute, "INSERT INTO actions(at, user, method, path, body, status) VALUES(?,?,?,?,?,?)",
                       ("2026-10-25T00:00:00Z", "x", "POST", "/devices/IF000001/upload", '{"device_code": "GC000002"}', 200))
    frag = client.get("/simulations/uploads", headers=HX).text
    assert "GC000002" not in frag and "Uploaded before: 2" in frag
    # a data device deployed at the datacenter that leaves the game's list was consumed: dropped, not kept unlisted
    from rsweb.sim import DATACENTER
    eaten = {"device_code": "SC000009", "device_type": "sensor_core", "location": DATACENTER, "status": "idle",
             "features": ["stow", "data"]}
    client.portal.call(db.kv_set, "devices", (client.portal.call(db.kv_get, "devices") or []) + [eaten])
    client.portal.call(client.app.state.worker.sync_devices)
    assert not any(d.get("device_code") == "SC000009" for d in client.portal.call(db.kv_get, "devices"))
    assert client.portal.call(db.kv_get, "sim:uploaded")["SC000009"]["type"] == "sensor_core"
    msg = notify.describe({"event": "device.discovered", "location": "MIRFAKA-OBJ-1",
                          "payload": {"device_type": "guidance_chip", "device_code": "A1B2C3D4"}})
    assert msg == "Scan discovered a guidance chip A1B2C3D4 at MIRFAKA-OBJ-1"


def test_snapshot_share_budget():
    from rsweb.snapshot import share_budget
    assert share_budget(30, {"controllers": 24, "belts": 9, "idle": 12}) == {"controllers": 11, "belts": 9, "idle": 10}
    assert share_budget(30, {"controllers": 24, "belts": 0, "idle": 0}) == {"controllers": 24, "belts": 0, "idle": 0}
    assert share_budget(0, {"controllers": 4}) == {"controllers": 0}
    assert sum(share_budget(7, {"a": 10, "b": 10, "c": 10}).values()) == 7


def test_snapshot_sections_checkboxes(client):
    import asyncio
    db = client.app.state.db
    page = client.get("/diagnostics", headers=H).text
    assert 'name="section" value="perf" checked' in page and 'name="sections_form"' in page
    client.post("/diagnostics/snapshot", data={"stars": "", "sections_form": "1",
                                               "section": ["controllers", "belts", "idle", "fleets"]}, headers=HX)
    for _ in range(100):
        st = client.portal.call(db.kv_get, "snapshot_status", {})
        if st.get("state") != "running":
            break
        client.portal.call(asyncio.sleep, 0.05)
    assert st["state"] == "done", st
    snap = client.portal.call(db.kv_get, "snapshot_last", None)
    paths = [c["path"] for c in snap["calls"]]
    assert "/inventory" not in paths and "/events" not in paths and any(p.startswith("/locations/") for p in paths)
    assert "perf" not in snap["app"] and "actions" not in snap["app"] and "fleets" in snap["app"]
    assert "changes" not in snap["app"]["server"] and snap["app"]["server"]["version"]
    assert all("omitted" in c["body"] for c in snap["calls"] if c["path"] == "/devices")
    assert snap["diagnosis"]["drones"]   # read from app.devices when the raw pages are left out
    assert set(snap["sections"]) == {"controllers", "belts", "idle", "fleets"}
    page = client.get("/diagnostics", headers=H).text   # the choice is kept
    assert 'value="perf" checked' not in page and 'value="fleets" checked' in page


def test_live_redraw_is_cache_only_and_not_a_visit(client):
    """Live sections fetch the page again with X-Live: that's no visit (the digest's "since" stays) and the Messages page
    doesn't ask the game for messages then; live pages mark their <main> for live cards."""
    db = client.app.state.db
    calls = []
    real = client.app.state.worker.sync_messages

    async def spy():
        calls.append(1)
        return await real()
    client.app.state.worker.sync_messages = spy
    r = client.get("/messages", headers={**H, "X-Live": "1"})
    assert r.status_code == 200 and "data-live-cards" in r.text and not calls
    assert client.portal.call(db.fetchone, "SELECT * FROM visitors") is None
    client.get("/messages", headers=H)
    assert calls and client.portal.call(db.fetchone, "SELECT * FROM visitors")
    assert 'id="live-switch"' in r.text and 'hx-trigger="sse:state"' in r.text
    assert "data-live-cards" not in client.get("/map", headers=H).text


def test_orders_refresh_the_devices_they_touch(client):
    """After your own order the touched devices (path + codes in the body) are asked for again and patched into the
    cached list; events about them refresh them for a while after."""
    from rsweb.ingest import Worker
    w, db = client.app.state.worker, client.app.state.db
    assert Worker.order_codes("/devices/AB12CD34", {"command": "stow", "cargo": "0011AAFF", "x": "travel"}) == {"AB12CD34", "0011AAFF"}
    assert Worker.order_codes("/replicants/R1/travel", {"destination": "SOL-3"}) == set()
    devices = client.portal.call(db.kv_get, "devices")
    code = devices[0]["device_code"]
    devices[0]["status"] = "stale"
    client.portal.call(db.kv_set, "devices", devices)
    client.portal.call(w.refresh_devices_now, {code})
    assert next(d for d in client.portal.call(db.kv_get, "devices") if d["device_code"] == code)["status"] != "stale"
    w._watch.clear()
    w.watched_event({"event": "device.stowed", "device_code": code})
    assert not w._bg
    devices = client.portal.call(db.kv_get, "devices")
    devices[0]["status"] = "stale"
    client.portal.call(db.kv_set, "devices", devices)

    async def arrive():   # an arrival refreshes the device straight away
        w.watched_event({"event": "travel.arrived", "device_code": code})
        await asyncio.gather(*w._bg)
    client.portal.call(arrive)
    assert next(d for d in client.portal.call(db.kv_get, "devices") if d["device_code"] == code)["status"] != "stale"


def test_foreign_devices_stay_out_of_the_list(client):
    """GET /devices/{code} answers for another player's device too (with its owner): it is never added to the cached
    list, and one already there (Bill's interface F4791EDF, 2026-10-27) goes on the next sync, not kept as unlisted."""
    from rsweb.ingest import foreign
    w, db = client.app.state.worker, client.app.state.db
    bill = {"name": "Bill", "replicant_code": "A8F48B26"}
    mine = set(client.portal.call(db.kv_get, "replicants") or {})
    assert foreign({"owner": bill}, mine) and not foreign({"owner": None}, mine)
    assert not foreign({"owner": {"replicant_code": next(iter(mine))}}, mine)
    real = w.api.get

    async def fake(path, *a, **kw):
        if path == "/devices/F4791EDF":
            return {"device_code": "F4791EDF", "device_type": "replicant_interface", "owner": bill}
        return await real(path, *a, **kw)
    w.api.get = fake
    try:
        client.portal.call(w.refresh_devices_now, {"F4791EDF"})
    finally:
        w.api.get = real
    assert "F4791EDF" not in {d["device_code"] for d in client.portal.call(db.kv_get, "devices")}
    devices = client.portal.call(db.kv_get, "devices")
    client.portal.call(db.kv_set, "devices", devices + [{"device_code": "F4791EDF", "device_type": "replicant_interface",
                                                          "owner": bill, "location": "MIRFAKA-OBJ-1"}])
    client.portal.call(w.sync_devices)
    assert "F4791EDF" not in {d["device_code"] for d in client.portal.call(db.kv_get, "devices")}


def test_heavy_passes_spread_over_ticks(client):
    eng = client.app.state.worker.automations
    ran = []
    for name in ("rule_salvage", "rule_restart_idle_miners", "rule_reopen_sites"):
        async def fake(n=name):
            ran.append(n)
            return []
        setattr(eng, name, fake)
    for _ in range(4):   # the first tick runs everything, then one heavy pass per tick
        client.portal.call(eng.tick)
    assert sorted(ran[:3]) == ["rule_reopen_sites", "rule_restart_idle_miners", "rule_salvage"]
    assert ran[3:] == ["rule_restart_idle_miners", "rule_reopen_sites", "rule_salvage"], ran


def test_device_list_filters_in_the_browser(client):
    import re
    """The page holds every device, the ones outside the URL's filter hidden; the search box asks /fleet/match."""
    db = client.app.state.db
    client.portal.call(db.kv_set, "devices", [
        {"device_code": "D1", "device_type": "mining_drone", "location": "SOL-1", "available_commands": ["travel"]},
        {"device_code": "D2", "device_type": "survey_drone", "location": "SOL-1", "available_commands": ["system_scan"]}])
    page = client.get("/fleet?type=survey_drone", headers=H).text
    assert 'data-code="D1" data-type="mining_drone"' in page and 'data-code="D2"' in page
    assert re.search(r'data-code="D1"[^>]* hidden>', page) and not re.search(r'data-code="D2"[^>]* hidden>', page)
    assert re.search(r'data-label="travel"[^>]* hidden>', page) and "system scan (1 here)" in page
    assert client.get("/fleet/match?q=survey", headers=H).json() == ["D2"]


def test_bucket_sliding_window():
    # a full minute's worth sent at once leaves no room until the oldest leaves the window (1.293.0)
    b = Bucket(10)
    for _ in range(10):
        assert b.wait_time() == 0
        b.take()
    assert 59 < b.wait_time() <= 60
    for i in range(10):
        b.recent[i] -= 61
    assert b.wait_time() == 0 and b.used_last_minute() == 0


def test_read_patterns():
    from rsweb import perf
    perf.reads.clear()
    perf.note_read("/devices/15DFB014/logs")
    perf.note_read("/locations/FALQUORYX-BELT-1")
    perf.note_read("/devices")
    rows = {r["path"] for r in perf.reads_report()["rows"]}
    assert rows == {"/devices/{code}/logs", "/locations/{place}", "/devices"}


def test_replicant_details_read_only_when_needed(client):
    # 1.293.0: a replicant's details are re-read when it moved, its vessel had an event, an order named it, or after
    # POLL_REPLICANT — not every account sync
    w = client.app.state.worker
    asked: list[str] = []
    real = w.api.get

    async def counting(path, *a, **kw):
        if path.startswith("/replicants/"):
            asked.append(path)
        return await real(path, *a, **kw)
    w.api.get = counting
    try:
        client.portal.call(w.sync_account)
        first = len(asked)
        client.portal.call(w.sync_account)
        assert len(asked) == first                    # nothing changed: summary only
        code = next(iter(client.portal.call(client.app.state.db.kv_get, "replicants")))
        w.after_order(f"/replicants/{code}/travel", {})
        client.portal.call(w.sync_account)
        assert asked[first:] == [f"/replicants/{code}"]
        reps = client.portal.call(client.app.state.db.kv_get, "replicants")
        hosted = reps[code].get("hosted_device_code")
        if hosted:
            client.portal.call(w.process_event, {"id": "1-0", "event": "travel.arrived", "device_code": hosted, "payload": {}})
            client.portal.call(w.sync_account)
            assert asked[-1] == f"/replicants/{code}" and len(asked) == first + 2
    finally:
        w.api.get = real


def test_order_answer_stands_in_for_a_reread_until_confirmed(client):
    """1.317.0 (Joe): a command whose answer names the device and its status puts that on the cached list, marked
    unconfirmed, instead of two GETs; a GET of the device (event or list) clears the mark."""
    w, db = client.app.state.worker, client.app.state.db
    code = client.portal.call(db.kv_get, "devices")[0]["device_code"]
    asked: list[str] = []
    real = w.api.get

    async def counting(path, *a, **kw):
        asked.append(path)
        return await real(path, *a, **kw)
    w.api.get = counting
    try:
        assert w.reply_codes(f"/devices/{code}/command", {code}, {"status": "preview", "device_code": code}) == {}
        async def order():
            w.after_order(f"/devices/{code}/command", {"command": "start_mining"},
                          {"device_code": code, "status": "mining (carbon)", "collected": 5})
            await asyncio.sleep(0.05)
        client.portal.call(order)
        d = next(x for x in client.portal.call(db.kv_get, "devices") if x["device_code"] == code)
        assert d["status"] == "mining (carbon)" and d["_unconfirmed"]["what"] == "start_mining" and "collected" not in d
        assert f"/devices/{code}" not in asked
        client.portal.call(w.refresh_devices_now, {code})
        assert "_unconfirmed" not in next(x for x in client.portal.call(db.kv_get, "devices") if x["device_code"] == code)
    finally:
        w.api.get = real


def test_page_location_reads_stamp_for_the_engine(client):
    """1.317.0: a page's belt read is stamped, so the engine's open-site count takes that copy instead of a GET."""
    from rsweb import locread
    db = client.app.state.db
    client.portal.call(locread.save, db, "sol-belt-1", {"location": "SOL-BELT-1", "resource_sites": []})
    assert "SOL-BELT-1" in client.portal.call(db.kv_get, "belt_reads")
    assert client.portal.call(locread.fresh, db, "SOL-BELT-1", 600)["location"] == "SOL-BELT-1"
    assert client.portal.call(locread.fresh, db, "SOL-BELT-1", -1) is None


def test_adopt_options_only_what_the_controller_can_adopt():
    from rsweb import commands as c
    ami = ["ami"]
    devs = [
        {"device_code": "MC", "device_type": "ami_mining_controller", "features": ami, "location": "X-BELT-1"},
        {"device_code": "M1", "device_type": "mining_drone", "location": "X-BELT-1", "status": "idle"},
        {"device_code": "M2", "device_type": "mining_drone", "location": "X-5-L4", "status": "idle"},
        {"device_code": "M3", "device_type": "mining_drone", "location": "Y-BELT-1", "status": "idle"},
        {"device_code": "M4", "device_type": "mining_drone", "location": "X-BELT-1", "controller_device_code": "OC"},
        {"device_code": "M5", "device_type": "mining_drone", "location": "X-BELT-1", "controller_device_code": "MC"},
        {"device_code": "S1", "device_type": "survey_drone", "location": "X-BELT-1"},
        {"device_code": "OC", "device_type": "ami_mining_controller", "features": ami, "location": "X-BELT-1"},
        {"device_code": "SC", "device_type": "ami_survey_controller", "features": ami, "location": "X-BELT-1"},
        {"device_code": "BS", "device_type": "belt_surveyor", "location": "X-BELT-1"},
        {"device_code": "TC", "device_type": "ami_transport_controller", "features": ami, "location": "X-BELT-1"},
        {"device_code": "CF", "device_type": "cargo_freighter", "location": "X-BELT-1"},
        {"device_code": "FC", "device_type": "fleet_controller", "features": ami, "location": "Y-OORT"},
        {"device_code": "FC2", "device_type": "fleet_controller", "features": ami + ["surge"], "location": "X-OORT"},
        {"device_code": "V1", "device_type": "racing_vessel", "features": ["surge"], "location": "X-OORT"},
    ]
    f = c.COMMANDS["adopt"][0]
    pick = lambda me: sorted(o["value"] for o in c.device_options({"devices": devs, "here": "X-BELT-1"}, f, me))
    assert pick("MC") == ["M1", "M2"]          # same system, mining drones, not under a controller
    assert pick("SC") == ["BS", "S1"]
    assert pick("TC") == ["CF"]
    assert pick("FC") == ["FC2", "V1"]         # surge-capable, anywhere, fleet controllers too


def test_stock_piles_sector_and_total_sort():
    """1.378.0: Stockpiles by location rows carry the sector (star region) and total, sorted by sector then total."""
    from rsweb.web import stock_piles
    cat = {"stars": [{"designation": "AAA", "region": "beta"}, {"designation": "BBB", "region": "alpha"}]}
    inv = [{"location": "AAA-BELT-1", "items": {"iron": 5}}, {"location": "BBB-BELT-1", "items": {"iron": 1}},
           {"location": "BBB-2", "items": {"iron": 3, "ice": 4}}, {"location": "ZZZ-1", "items": {"iron": 99}},
           {"location": "AAA-1", "items": {"iron": 0}}]
    rows = stock_piles(inv, cat)
    assert [(r["location"], r["region"], r["total"]) for r in rows] == [
        ("BBB-2", "alpha", 7), ("BBB-BELT-1", "alpha", 1), ("AAA-BELT-1", "beta", 5), ("ZZZ-1", "", 99)]


def test_research_at_every_datacenter_found(client):
    """A second datacenter (Delta, 2026-11-09) never showed: interfaces were found only by a scan from a replicant
    already there. Scan interfaces now scans every system a replicant of ours is in, and Research lists each
    interface's topics, researching at the one chosen."""
    w, db = client.app.state.worker, client.app.state.db
    reps = client.portal.call(db.kv_get, "replicants") or {}
    code = sorted(reps)[0]
    client.portal.call(db.kv_set, "replicants", {**reps, code: {**reps[code], "location": "ISOLITHOR-OBJ-3"}})
    real, seen = w.api.get, []

    async def fake(path, *a, **kw):
        if path.endswith("/scan/devices"):
            seen.append(path)
            if path == f"/replicants/{code}/scan/devices":
                return {"devices": [{"device_code": "DC000002", "device_type": "replicant_interface",
                                     "location": "ISOLITHOR-OBJ-3", "owner_name": "Bill"}]}
            return {"devices": []}
        return await real(path, *a, **kw)
    w.api.get = fake
    try:
        t = client.post("/simulations/scan", headers=HX).text
    finally:
        w.api.get = real
    assert f"/replicants/{code}/scan/devices" in seen and "DC000002 found at ISOLITHOR-OBJ-3" in t
    r = client.get("/simulations/research", headers=HX).text
    assert "ISOLITHOR-OBJ-3" in r and "MIRFAKA-OBJ-1" in r and "/simulations/research/precursor_signal?interface=DC000002" in r
    t = client.post("/simulations/research/precursor_signal?interface=DC000002", headers=HX).text
    row = client.portal.call(db.fetchall, "SELECT path FROM actions ORDER BY id DESC LIMIT 1")
    assert row[0]["path"] == "/devices/DC000002/research/precursor_signal", t
def test_station_tour_is_the_shortest_open_path_from_the_nearer_end():
    """1.387.0: stations in the shortest travel order (exact), starting at the end nearer SOLARES."""
    from rsweb.megas import shortest_path
    xyz = {"A": (0, 0, 0), "B": (10, 0, 0), "C": (3, 0, 0), "D": (7, 0, 0), "SOLARES": (12, 0, 0)}
    assert shortest_path(["A", "B", "C", "D"], xyz, "SOLARES") == ["B", "D", "C", "A"]


def test_dashboard_station_card_lists_travel_order(client):
    db = client.app.state.db
    client.portal.call(db.kv_set, "stations", {"at": "x", "stations": [
        {"name": f"S{i}", "slug": f"s{i}", "location": f"ST{i}-OBJ-1"} for i in range(4)]})
    stars = [{"designation": f"ST{i}", "position": {"x": x, "y": 0, "z": 0}} for i, x in enumerate((0, 10, 3, 7))]
    client.portal.call(db.kv_set, "stars", {"stars": stars + [{"designation": "SOLARES", "position": {"x": 12, "y": 0, "z": 0}}]})
    page = client.get("/", headers=H).text
    assert "Travel order (shortest path, 12.0 ly from SOLARES)" in page
    page = page[page.index("Travel order"):]
    i = [page.index(f'<li><a href="/systems/{s}">{s}</a> <span class="muted">S') for s in ("ST1", "ST3", "ST2", "ST0")]
    assert i == sorted(i)
