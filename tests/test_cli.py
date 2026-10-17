import sys
import time
from unittest.mock import patch

import pytest

from _auth_fixtures import cookies_to_dict, make_login_cookies

from doubao2_api.cli import main
from doubao2_api.credentials import CredentialStore


def test_login_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr(
        "doubao2_api.cli.refresh_credentials",
        lambda store, timeout, headless: called.append(True),
    )
    with patch.object(sys, "argv", ["doubao2-api", "login"]):
        main()
    assert called == [True]
    assert "登录成功" in capsys.readouterr().out


def test_login_reports_failure_without_traceback(tmp_path, monkeypatch, capsys):
    from doubao2_api.session_refresh import SessionRefreshError

    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def fake_refresh(store, timeout, headless):
        raise SessionRefreshError("扫码登录等待超时（0s）：检测到的仍是旧登录态")

    monkeypatch.setattr("doubao2_api.cli.refresh_credentials", fake_refresh)
    with patch.object(sys, "argv", ["doubao2-api", "login"]):
        with pytest.raises(SystemExit) as exit_info:
            main()
    assert exit_info.value.code == 1
    assert "登录失败" in capsys.readouterr().err


def test_status_command_shows_remaining_validity(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    CredentialStore(tmp_path / "credentials.json").save(
        cookies_to_dict(make_login_cookies(time.time())),
        device={"device_id": "1", "web_id": "2", "tea_uuid": "2"},
    )
    with patch.object(sys, "argv", ["doubao2-api", "status"]):
        main()
    out = capsys.readouterr().out
    assert "登录态：有效" in out
    assert "到期时间：" in out
    assert "device_id" in out


def test_status_command_flags_expired(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    CredentialStore(tmp_path / "credentials.json").save(
        cookies_to_dict(make_login_cookies(time.time() - 2592000 - 3600))
    )
    with patch.object(sys, "argv", ["doubao2-api", "status"]):
        main()
    out = capsys.readouterr().out
    assert "登录态：已过期" in out
    assert "doubao2-api login" in out


def test_status_command_without_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with patch.object(sys, "argv", ["doubao2-api", "status"]):
        main()
    out = capsys.readouterr().out
    assert "登录态：无" in out
    assert "doubao2-api login" in out


# ---- 保活/专用 profile ----


def test_status_reports_keepalive_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KEEPALIVE_ENABLED", "true")
    with patch.object(sys, "argv", ["doubao2-api", "status"]):
        main()
    out = capsys.readouterr().out
    assert "保活：已启用" in out
    assert "浏览器 profile：" in out
    assert "尚未创建" in out
    assert "login --profile" in out


def test_status_reports_profile_created(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "browser-profile").mkdir()
    with patch.object(sys, "argv", ["doubao2-api", "status"]):
        main()
    out = capsys.readouterr().out
    assert "已创建" in out


def test_login_with_profile_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr(
        "doubao2_api.keepalive.login_with_profile",
        lambda store, profile, timeout, headless, channel: called.append(
            (profile, headless, channel)
        ),
    )
    with patch.object(sys, "argv", ["doubao2-api", "login", "--profile"]):
        main()
    out = capsys.readouterr().out
    assert len(called) == 1
    profile, headless, channel = called[0]
    assert profile == tmp_path / "browser-profile"
    assert headless is False
    assert channel is None
    assert "登录成功" in out
    assert "无需扫码" in out


def test_login_with_profile_headless_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr(
        "doubao2_api.keepalive.login_with_profile",
        lambda store, profile, timeout, headless, channel: called.append(headless),
    )
    with patch.object(sys, "argv", ["doubao2-api", "login", "--profile", "--headless"]):
        main()
    assert called == [True]
