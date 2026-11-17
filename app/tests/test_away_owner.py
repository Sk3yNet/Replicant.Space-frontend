from rsweb.web_devices import away_owner
from test_ops import HX, client  # noqa: F401  (fixture)

DEVS = [
    {"device_code": "410EFEBA", "device_type": "crystal_seed", "location": "DELTA-OBJ-2", "replicant_code": "7F509D43"},
    {"device_code": "BE6E8367", "location": "FALQUORYX-1-L4", "replicant_code": "7F509D43",
     "hosting_replicant": {"name": "Augustin", "replicant_code": "7F509D43"}},
    {"device_code": "BFE1BB3A", "location": "DELTA-OBJ-2", "replicant_code": "0BC10C52",
     "hosting_replicant": {"name": "Rufus", "replicant_code": "0BC10C52"}},
]


def test_away_owner_finds_owner_and_replicants_here():
    w = away_owner("410EFEBA", DEVS)
    assert w["owner_name"] == "Augustin" and w["owner_at"] == "FALQUORYX-1-L4"
    assert w["here"] == [("0BC10C52", "Rufus")]
    assert away_owner("410EFEBA", DEVS[:2])["here"] == []
    assert away_owner("NOPE", DEVS) is None
    assert away_owner("BFE1BB3A", DEVS) is None   # never hand over a device hosting a replicant


def test_command_hands_over_and_resends(client, monkeypatch):  # noqa: F811
    from rsweb import web_devices
    sent = []

    async def fake_call(request, user, method, path, body, label):
        sent.append(body)
        ok = body["command"] == "change_owner" or len(sent) > 2
        return {"label": label, "method": method, "path": path, "ok": ok, "status": 200 if ok else 400,
                "error": None if ok else "Replicant must be at the device location", "response": {}}

    async def fake_state(request):
        return {"devices": DEVS}
    monkeypatch.setattr(web_devices, "call_action", fake_call)
    monkeypatch.setattr(web_devices, "load_state", fake_state)
    r = client.post("/devices/410EFEBA/command", data={"command": "activate", "args": ""}, headers=HX)
    assert [b["command"] for b in sent] == ["activate", "change_owner", "activate"]
    assert sent[1]["target"] == "0BC10C52"
    assert "handed it to Rufus" in r.text and "result ok" in r.text
