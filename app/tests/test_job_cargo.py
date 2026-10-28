from rsweb.automations import job_cargo, step
from rsweb.web import templates


def _job():
    steps = [step("C1 → SOLARES-4-L4", "/devices/C1", {"command": "travel", "destination": "SOLARES-4-L4"}),
             step("stow R1 in C1", "/devices/R1", {"command": "stow", "target": "C1"}),
             step("C1: attach W1", "/devices/C1", {"command": "attach", "device": "W1"}),
             step("C1: load", "/devices/C1", {"command": "collect_resources", "resources": {"iron": 200, "rare_earths": 5}}),
             step("C1: detach W1", "/devices/C1", {"command": "detach", "device": "W1"})]
    return {"id": "loadouts-1", "rule": "loadouts", "title": "loadouts: C1 carries 3", "device": "C1", "steps": steps,
            "idx": 2, "status": "waiting", "created_at": "2026-10-28T00:00:00Z", "meta": {"devices": ["R1", "W1", "B1", "C1"]}}


def test_job_cargo_reads_steps_and_meta():
    job = _job()
    job["steps"][1]["status"] = "done"
    c = job_cargo(job, [{"device_code": "R1", "device_type": "ftl_relay"}])
    assert c["devices"] == [{"code": "R1", "type": "ftl_relay", "state": "aboard"},
                            {"code": "W1", "type": None, "state": "to pick up"},
                            {"code": "B1", "type": None, "state": "to pick up"}]
    assert c["materials"] == {"iron": 200, "rare_earths": 5} and not c["loaded"]
    for st in job["steps"]:
        st["status"] = "done"
    c = job_cargo(job)
    assert {d["code"]: d["state"] for d in c["devices"]} == {"R1": "aboard", "W1": "dropped", "B1": "to pick up"}
    assert c["loaded"]


def test_job_cargo_ignores_meta_outside_loadouts():
    job = {**_job(), "rule": "fleets", "steps": []}
    assert job_cargo(job) == {"devices": [], "materials": {}, "loaded": False}


def test_job_row_shows_cargo():
    job = _job()
    html = templates.get_template("partials/automation_jobs.html").render(
        active=[job], cargo={job["id"]: job_cargo(job, [{"device_code": "R1", "device_type": "ftl_relay"}])})
    assert "Cargo:" in html and "ftl relay" in html and "/devices/R1" in html and "rare earths" in html
    assert "to load" in html
    assert "Cargo:" not in templates.get_template("partials/automation_jobs.html").render(active=[{**job, "steps": job["steps"][:1], "meta": {}}])
