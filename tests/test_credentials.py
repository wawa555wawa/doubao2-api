import datetime
import time

import pytest

from _auth_fixtures import cookies_to_dict, make_cookie, make_login_cookies, make_sid_guard

from doubao2_api.credentials import (
    CredentialStore,
    format_expiry,
    session_expires_at,
    session_time_remaining,
)


def test_empty_when_file_missing(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    assert store.is_empty()
    assert store.cookies == {}


def test_save_and_reload(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialStore(path)
    store.save({"sessionid": "abc", "uid": "1"})
    assert store.cookies == {"sessionid": "abc", "uid": "1"}

    reloaded = CredentialStore(path)
    assert reloaded.cookies == {"sessionid": "abc", "uid": "1"}
    assert not reloaded.is_empty()


def test_save_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "dir" / "credentials.json"
    CredentialStore(path).save({"a": "b"})
    assert path.exists()


def test_device_roundtrip(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialStore(path)
    store.save({"sessionid": "abc"}, device={"device_id": "1", "web_id": "2", "tea_uuid": "2"})
    assert CredentialStore(path).device == {"device_id": "1", "web_id": "2", "tea_uuid": "2"}


def test_save_without_device_keeps_existing(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialStore(path)
    store.save({"a": "b"}, device={"device_id": "1"})
    store.save({"a": "c"})
    assert store.device == {"device_id": "1"}


# ---- 有效期（sid_guard）：以前 saved_at 只写不读，过期只能靠 401 才发现 ----


def test_session_expiry_is_parsed_from_sid_guard():
    issued = time.time() - 60
    cookies = cookies_to_dict(make_login_cookies(issued))
    assert session_expires_at(cookies) == pytest.approx(issued + 2592000, abs=1)


def test_expiry_missing_or_malformed_is_none():
    assert session_expires_at({"sessionid": "x"}) is None
    assert session_expires_at({"sid_guard": "garbage"}) is None
    assert session_expires_at({"sid_guard": "%%7Cnot-a-ts%7C2592000%7Cx"}) is None


def test_time_remaining_negative_after_expiry():
    expired = cookies_to_dict(make_login_cookies(time.time() - 2592000 - 120))
    remaining = session_time_remaining(expired)
    assert remaining is not None and remaining < 0


def test_store_exposes_expiry_after_reload(tmp_path):
    issued = time.time() - 10
    path = tmp_path / "credentials.json"
    CredentialStore(path).save(cookies_to_dict(make_login_cookies(issued)))

    reloaded = CredentialStore(path)
    assert reloaded.session_expires_at == pytest.approx(issued + 2592000, abs=1)
    assert reloaded.is_expired() is False
    assert reloaded.session_time_remaining() > 0


def test_store_is_expired_for_stale_credentials(tmp_path):
    path = tmp_path / "credentials.json"
    CredentialStore(path).save(
        cookies_to_dict(make_login_cookies(time.time() - 2592000 - 3600))
    )
    assert CredentialStore(path).is_expired() is True


def test_store_without_expiry_is_not_reported_expired(tmp_path):
    """解析不出有效期时不能误报过期（老凭证文件没有 sid_guard）。"""
    store = CredentialStore(tmp_path / "credentials.json")
    store.save({"sessionid": "SANITIZED"})
    assert store.session_expires_at is None
    assert store.is_expired() is False


def test_saved_at_survives_reload(tmp_path):
    path = tmp_path / "credentials.json"
    CredentialStore(path).save({"sessionid": "SANITIZED"})
    assert CredentialStore(path).saved_at == pytest.approx(time.time(), abs=5)


def test_format_expiry_reports_state():
    now = time.time()
    assert "已过期" in format_expiry(now - 86400)
    assert "剩余" in format_expiry(now + 86400 * 3)
    assert "未知" in format_expiry(None)


def test_sid_guard_fixture_matches_doubao_encoding():
    """防手滑：fixture 必须复刻真实的 %7C 编码 + GMT 到期日字段。"""
    guard = make_sid_guard("SANITIZED-SESSION", 1787101589.0)
    assert guard.startswith("SANITIZED-SESSION%7C1787101589%7C2592000%7C")
    assert guard.endswith("GMT")
    assert datetime.datetime.fromtimestamp(1787101589, datetime.timezone.utc).year == 2026
