import pytest


@pytest.fixture(autouse=True)
def _map_answers_not_reused(monkeypatch):
    """Tests change state and read the map again at once: map.json rebuilds on every ask here (web_map.map_fresh_for)."""
    import rsweb.web_map as web
    monkeypatch.setattr(web, "map_fresh_for", lambda part, build_s: -1.0)
    web._MAP_LAST.clear()
    yield
    web._MAP_LAST.clear()
