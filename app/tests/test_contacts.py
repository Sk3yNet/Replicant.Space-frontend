"""Activity › Contacts: other players' replicants gathered from beacons, scans and BobNet, with their profiles."""
from datetime import datetime, timedelta, timezone

from rsweb import contacts as ct
from tests.test_ops import H, HX, REP, add, client, eng, iso, world  # noqa: F401  (client is a fixture)


def test_gather_counts_each_sighting_once_and_skips_ours():
    now = datetime.now(timezone.utc)
    traffic = {"entries": [
        {"id": 1, "device_code": "D1", "replicant_code": "AAAA0001", "star": "SOL", "logged_at": iso(now - timedelta(hours=3))},
        {"id": 2, "device_code": "D2", "replicant_code": "AAAA0001", "star": "LERNA", "logged_at": iso(now - timedelta(hours=1))},
        {"id": 3, "device_code": "D3", "replicant_code": REP, "star": "SOL", "logged_at": iso(now)}]}
    others = {"stars": {"ZED": {"scanned_at": iso(now), "devices": [{"owner": "BBBB0002", "owner_name": "Riker"},
                                                                  {"owner": "BBBB0002", "owner_name": "Riker"}]}}}
    bob = [{"seq": 7, "replicant_code": "CCCC0003", "replicant_name": "Dash", "current_star": "SOL", "time": iso(now)},
           {"seq": 8, "replicant_code": REP, "replicant_name": "me", "current_star": "SOL", "time": iso(now)}]
    state: dict = {}
    assert ct.gather(state, {REP}, traffic, others, bob) == 3
    p = state["people"]
    assert set(p) == {"AAAA0001", "BBBB0002", "CCCC0003"}
    assert p["AAAA0001"]["sources"] == {"beacon": 2} and sorted(p["AAAA0001"]["stars"]) == ["LERNA", "SOL"]
    assert p["BBBB0002"]["name"] == "Riker" and p["BBBB0002"]["sources"] == {"scan": 1}
    assert state["bobnet_seq"] == 8
    assert ct.gather(state, {REP}, traffic, others, []) == 0          # same log again: nothing new, no recount
    assert p["AAAA0001"]["sources"] == {"beacon": 2}
    assert ct.due(p) and len(ct.due(p, limit=2)) == 2


def test_contacts_page_reads_profiles_and_adds_by_name(client):
    add(client, device_code="BCN00002", device_type="ftl_beacon", location="SOL-KUIPER", status="monitoring",
        features=["stow", "audit", "comms"], available_commands=["deploy", "stow"])
    w = world(client)
    w.profiles["F00DF00D"] = {"replicant_code": "F00DF00D", "name": "Quill", "is_npc": False, "pronouns": "they/them",
                              "description": "Cartographer & trader", "plan": "Map the rim", "project": None,
                              "experience_points": 900, "location": "LERNA-3"}
    w.audit["BCN00002"] = [{"id": 5, "device_code": "QQ000001", "device_type": "survey_drone", "replicant_code": "F00DF00D",
                            "travel_type": "arrival", "location": "SOL-KUIPER", "logged_at": iso(datetime.now(timezone.utc)),
                            "vector": None}]
    client.portal.call(eng(client).sync_traffic)
    r = client.get("/contacts", headers=H)
    assert r.status_code == 200
    assert "Quill" in r.text and "Cartographer &amp; trader" in r.text and "Map the rim" in r.text and "they/them" in r.text
    assert "Quill" in r.text and "F00DF00D" in r.text
    assert "Quill" not in client.get("/contacts?q=nobody", headers=H).text
    # the traffic summary names it from the same profile
    assert "Quill" in client.get("/traffic", headers=H).text
    # add by name from the directory; a note; hide
    r = client.post("/contacts/add", data={"name": "Billy-2"}, headers=HX)
    assert "Added Billy-2" in r.text
    assert "B1LLY002" in client.get("/contacts", headers=H).text
    assert "Note saved" in client.post("/contacts/F00DF00D/note", data={"note": "friendly"}, headers=HX).text
    assert "friendly" in client.get("/contacts?q=friend", headers=H).text
    client.post("/contacts/F00DF00D/hide", headers=HX)
    assert "Cartographer" not in client.get("/contacts", headers=H).text
    assert "Cartographer" in client.get("/contacts?hidden=1", headers=H).text
    assert "Profile read" in client.post("/contacts/B1LLY002/profile", headers=HX).text
