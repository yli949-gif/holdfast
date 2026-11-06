import pytest


@pytest.fixture(autouse=True)
def _isolated_sentinels(tmp_path_factory, monkeypatch):
    """Keep Holdfast's out-of-tree session sentinels out of the real home directory during tests."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("xdg-state")))
