from pathlib import Path

from doubao2_api.config import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.host == "127.0.0.1"
    assert s.port == 8000
    assert s.data_dir == Path("data")
    assert s.generation_timeout == 120.0
    assert s.login_timeout == 180.0
    assert s.max_concurrent == 1
    assert s.headless is False


def test_env_override(monkeypatch):
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("MAX_CONCURRENT", "2")
    s = Settings(_env_file=None)
    assert s.port == 9000
    assert s.max_concurrent == 2
