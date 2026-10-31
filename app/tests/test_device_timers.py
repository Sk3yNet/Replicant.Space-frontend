"""Device page: every running command's finish time (Joe 2026-10-31)."""
from datetime import datetime, timedelta, timezone

from rsweb.web import device_timers

from test_ops import H, client  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc)


def iso(m):
    return (NOW + timedelta(minutes=m)).isoformat()


def test_status_blocks_give_finish_times_and_replace_app_timers():
    dev = {"device_code": "D1", "printing": {"device_type": "ftl_relay", "started_at": iso(-10), "completes_at": iso(10)},
           "scan": {"target": "SOL-3", "eta_seconds": 300}, "repair": {"target_device_code": "R2", "eta_seconds": 60},
           "is_prospecting": True, "prospect_direction": "north", "prospect_completes_at": iso(30),
           "travel": {"destination": "SOL-KUIPER", "final_destination": "SOL-3", "arrives_at": iso(2), "stage": "surge_hop"}}
    rows = [{"device_code": "D1", "kind": "print", "label": "old", "ends_at": iso(99), "started_at": iso(-1)},
            {"device_code": "D1", "kind": "compacting", "label": "compacting D1", "ends_at": iso(5), "started_at": iso(-5)},
            {"device_code": "D1", "kind": "travel", "label": "whole trip", "ends_at": iso(50), "started_at": iso(-5)}]
    t = {x["kind"]: x for x in device_timers(dev, rows, NOW)}
    assert set(t) == {"print", "scan", "repair", "prospect", "leg", "compacting"}
    assert t["print"]["label"] == "printing ftl relay" and t["print"]["pct"] == 50
    assert t["scan"]["ends_at"] == (NOW + timedelta(seconds=300)).isoformat()
    assert t["leg"]["label"] == "surge hop to SOL-KUIPER" and t["prospect"]["label"] == "prospecting north"
    assert [x["kind"] for x in device_timers(dev, rows, NOW)][0] == "repair"   # soonest first
    assert device_timers({"device_code": "D2", "travel": {"destination": "X", "final_destination": "X"}}, rows, NOW) == []


def test_device_page_shows_running_card(client):  # noqa: F811
    db = client.app.state.db
    devices = client.portal.call(db.kv_get, "devices")
    end = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(timespec="seconds")
    devices.append({"device_code": "SCAN0001", "device_type": "survey_drone", "location": "SOL-3", "status": "scanning",
                    "scan": {"target": "SOL-3", "completes_at": end}})
    client.portal.call(db.kv_set, "devices", devices)
    page = client.get("/devices/SCAN0001", headers=H).text
    assert "<h2>Running</h2>" in page and "scanning SOL-3" in page and f'data-ends="{end}"' in page
    assert "<h2>Running</h2>" not in client.get("/devices/11ADA230", headers=H).text
