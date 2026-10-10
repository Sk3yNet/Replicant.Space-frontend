from rsweb import lost as ls
from test_ops import H, HX, client  # noqa: F401  (fixture)

POS = {"A": {"x": 0, "y": 0, "z": 0}, "B": {"x": 5, "y": 0, "z": 0}, "C": {"x": 20, "y": 0, "z": 0}}
REPS = {"R1": {"location": "A-3"}}
RELAY = {"device_code": "RL", "device_type": "ftl_relay", "location": "A-3-L4", "status": "relaying"}


def test_surge_then_seen_again():
    s = ls.normalize({})
    fr = {"device_code": "F1", "device_type": "cargo_freighter", "location": "A-3", "status": "idle"}
    ls.update(s, [RELAY, fr], REPS, POS, now="2026-10-10T10:00:00+00:00")
    assert s["lost"] == {}
    surging = {**fr, "status": "surging", "travel": {"destination": "C-KUIPER", "arrives_at": "2026-10-10T12:00:00+00:00"}}
    out = ls.update(s, [RELAY, surging], REPS, POS, now="2026-10-10T10:05:00+00:00")
    rec = s["lost"]["F1"]
    assert out["lost"] == ["F1"] and rec["reason"] == "surging" and rec["last_location"] == "A-3" and rec["heading_to"] == "C-KUIPER"
    # out of the device list for a while: still lost, last known A-3
    ls.update(s, [RELAY], REPS, POS, now="2026-10-10T11:00:00+00:00")
    assert "F1" in s["lost"]
    # arrives at C: no relay reaches it, no replicant there — out of range now
    ls.update(s, [RELAY, {**fr, "location": "C-KUIPER", "status": "idle"}], REPS, POS, now="2026-10-10T12:01:00+00:00")
    assert s["lost"]["F1"]["reason"] == "out of range"
    # a replicant arrives at C: seen again
    out = ls.update(s, [RELAY, {**fr, "location": "C-KUIPER", "status": "idle"}], {"R1": {"location": "C-2"}}, POS,
                    now="2026-10-10T13:00:00+00:00")
    assert out["found"] == ["F1"] and not s["lost"] and s["found"][-1]["found_location"] == "C-KUIPER"


def test_riders_and_relay_coverage_and_gone():
    s = ls.normalize({})
    carrier = {"device_code": "SC", "device_type": "surge_carrier", "location": "A-3", "status": "surging",
               "travel": {"destination": "B"}}
    rider = {"device_code": "M1", "device_type": "mining_drone", "location": None, "attached_to_device_code": "SC"}
    in_b = {"device_code": "D2", "device_type": "survey_drone", "location": "B-2", "status": "idle"}   # 5 ly from A's relay
    ls.update(s, [RELAY, carrier, rider, in_b], {}, POS)
    assert s["lost"]["M1"]["reason"].startswith("aboard SC") and s["lost"]["M1"]["last_location"] == "A-3"
    assert "D2" not in s["lost"]
    ls.update(s, [RELAY], {}, POS, gone={"M1"})
    assert "M1" not in s["lost"] and s["found"][-1]["outcome"] == "gone"


def test_lost_page_and_sync_hook(client):
    db = client.app.state.db
    devices = client.portal.call(db.kv_get, "devices")
    devices.append({"device_code": "ZZ1", "device_type": "cargo_freighter", "location": "SOL-3", "status": "surging",
                    "travel": {"destination": "ABOTEIN"}})
    client.portal.call(ls.track, db, devices)
    page = client.get("/lost", headers=H).text
    assert "ZZ1" in page and "surging" in page and "ABOTEIN" in page
    client.post("/lost/forget", data={"code": "ZZ1"}, headers=HX)
    assert "written off" in client.get("/lost", headers=H).text
