from rsweb import mapmarks
from test_ops import H, HX, client  # noqa: F401  (fixture)


def test_saved_map_markers_add_replace_remove(client):
    assert client.get("/api/markers.json", headers=H).json()["markers"] == []
    assert client.post("/api/markers", json={"add": [{"star": "sol"}]}, headers=H).status_code == 403   # needs HX-Request
    r = client.post("/api/markers", json={"add": [{"star": "sol", "shape": "star", "color": "#FF0000", "note": "home"},
                                                  {"star": "FALQUORYX", "shape": "bogus", "color": "red"},
                                                  {"star": "<script>"}]}, headers=HX).json()
    by = {m["star"]: m for m in r["markers"]}
    assert set(by) == {"SOL", "FALQUORYX"}                         # a bad name is dropped
    assert by["SOL"]["shape"] == "star" and by["SOL"]["color"] == "#ff0000" and by["SOL"]["note"] == "home"
    assert by["FALQUORYX"]["shape"] == "circle" and by["FALQUORYX"]["color"] == mapmarks.DEFAULT_COLOR
    r = client.post("/api/markers", json={"add": [{"star": "SOL", "shape": "flag"}]}, headers=HX).json()
    assert len(r["markers"]) == 2 and "changed 1" in r["msg"]       # one marker per system: replaced
    r = client.post("/api/markers", json={"remove": ["sol"]}, headers=HX).json()
    assert [m["star"] for m in r["markers"]] == ["FALQUORYX"]
    assert client.get("/api/markers.json", headers=H).json()["markers"][0]["star"] == "FALQUORYX"
    assert client.post("/api/markers", json={"clear": True}, headers=HX).json()["markers"] == []
    assert client.post("/api/markers", content=b"[1]", headers={**HX, "Content-Type": "application/json"}).status_code == 400
