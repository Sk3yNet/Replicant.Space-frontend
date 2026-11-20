import pytest


@pytest.fixture(autouse=True)
def _map_answers_not_reused(monkeypatch):
    """Tests change state and read the map again at once: map.json rebuilds on every ask here (web_map.map_fresh_for)."""
    import rsweb.web_map as web
    monkeypatch.setattr(web, "map_fresh_for", lambda part, build_s: -1.0)
    import rsweb.web_core as wc
    monkeypatch.setattr(wc, "serve_stale", lambda age: False)   # and an old answer is never served while one rebuilds
    web._MAP_LAST.clear()
    yield
    web._MAP_LAST.clear()


@pytest.fixture(autouse=True)
def _borrow_carriers_in_old_tests(monkeypatch):
    """Most fixtures move devices with fleetless carriers, which only the borrow_carriers setting allows since 1.496.0.
    Tests of the new default set it off in their settings."""
    import rsweb.loadouts as lo
    monkeypatch.setitem(lo.DEFAULT_SETTINGS, "borrow_carriers", True)
    yield
