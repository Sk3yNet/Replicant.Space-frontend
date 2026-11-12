"""Fleet tenders (2026-11-11): compacted pill for every modular device, commands only from the game list."""
from rsweb.web import fold_mark, with_attach


def test_fold_mark_for_any_modular_device():
    tender = {"device_type": "fleet_tender", "features": ["cruise", "repair", "modular"], "status": "idle", "folded": True}
    assert "compacted" in str(fold_mark(tender))
    by_cmd = {"device_type": "new_thing", "features": [], "available_commands": ["compact", "unfurl"], "status": "idle",
              "folded": True}
    assert "compacted" in str(fold_mark(by_cmd))
    assert str(fold_mark({**tender, "folded": False})) == ""
    assert str(fold_mark({**tender, "status": "compacted"})) == ""   # the status says it already
    assert str(fold_mark({"features": ["mine"], "status": "idle", "folded": True})) == ""


def test_commands_follow_the_game_list():
    tender = {"device_type": "fleet_tender", "features": ["cruise", "repair", "modular"],
              "available_commands": ["compact", "unfurl", "travel"]}
    assert "repair" not in with_attach(tender, [])
