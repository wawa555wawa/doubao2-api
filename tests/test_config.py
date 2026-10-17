from pathlib import Path

from doubao2_api.config import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.host == "127.0.0.1"
    assert s.port == 9756
    assert s.data_dir == Path("data")
    assert s.generation_timeout == 120.0
    assert s.login_timeout == 180.0
    assert s.max_concurrent == 1
    assert s.headless is False
    assert s.session_warn_days == 3.0


def test_env_override(monkeypatch):
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("MAX_CONCURRENT", "2")
    s = Settings(_env_file=None)
    assert s.port == 9000
    assert s.max_concurrent == 2


# ---- 保活配置 ----


def test_keepalive_defaults_off():
    s = Settings(_env_file=None)
    assert s.keepalive_enabled is False
    assert s.profile_dir == Path("browser-profile")
    assert s.keepalive_interval == 6 * 3600.0
    assert s.keepalive_page_wait_ms == 10000.0
    assert s.browser_channel is None


def test_profile_dir_resolves_under_data_dir():
    s = Settings(_env_file=None, data_dir=Path("/tmp/dd"))
    assert s.resolved_profile_dir == Path("/tmp/dd/browser-profile")


def test_absolute_profile_dir_is_respected():
    s = Settings(_env_file=None, data_dir=Path("/tmp/dd"), profile_dir=Path("/opt/prof"))
    assert s.resolved_profile_dir == Path("/opt/prof")


def test_keepalive_env_override(monkeypatch):
    monkeypatch.setenv("KEEPALIVE_ENABLED", "true")
    monkeypatch.setenv("KEEPALIVE_INTERVAL", "600")
    monkeypatch.setenv("BROWSER_CHANNEL", "chrome")
    s = Settings(_env_file=None)
    assert s.keepalive_enabled is True
    assert s.keepalive_interval == 600
    assert s.browser_channel == "chrome"
