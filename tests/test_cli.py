import sys
from unittest.mock import patch

from doubao2_api.cli import main


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
