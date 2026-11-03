import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own AGENTMON_HOME and HOME so nothing touches real files."""
    monkeypatch.setenv("AGENTMON_HOME", str(tmp_path / "agentmon"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))  # Path.home() reads this on Windows, not HOME
    (tmp_path / "home").mkdir()
    return tmp_path
