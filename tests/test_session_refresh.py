import time

import pytest

from _auth_fixtures import (
    FAKE_SESSION_ID,
    FakeBrowser,
    FakePage,
    FakePlaywright,
    FakeResponse,
    cookies_to_dict,
    make_cookie,
    make_login_cookies,
)

from doubao2_api.credentials import CredentialStore
from doubao2_api.session_refresh import (
    SESSION_COOKIE_NAMES,
    SessionRefreshError,
    extract_device_params,
    extract_login_cookies,
    refresh_credentials,
    wait_for_fresh_session,
)


# ---- 既有行为（不得回归） ----


def test_extract_returns_none_when_not_logged_in():
    cookies = [make_cookie("ttwid"), make_cookie("other")]
    assert extract_login_cookies(cookies) is None


def test_extract_returns_all_cookies_when_logged_in():
    marker = next(iter(SESSION_COOKIE_NAMES))
    cookies = [make_cookie("ttwid"), make_cookie(marker)]
    result = extract_login_cookies(cookies)
    assert result == {
        "ttwid": "SANITIZED-ttwid",
        marker: f"SANITIZED-{marker}",
    }


def test_extract_device_params():
    url = "https://www.doubao.com/chat/completion?aid=497858&device_id=123&web_id=456&tea_uuid=456&foo=bar"
    assert extract_device_params(url) == {"device_id": "123", "web_id": "456", "tea_uuid": "456"}
    assert extract_device_params("https://www.doubao.com/im/chain/single?aid=1") == {}


# ---- 扫码续期：只接受"新签发"的会话 ----


def test_fresh_session_is_accepted_immediately():
    cookies = make_login_cookies(time.time())
    polls: list[int] = []

    def poll() -> list[dict]:
        polls.append(1)
        return cookies

    result = wait_for_fresh_session(poll, sleep=lambda _s: None, timeout=1)
    assert result["sessionid"] == FAKE_SESSION_ID
    assert polls == [1]


def test_stale_session_is_never_taken_as_login():
    """防回归：轮询里出现的旧会话（sessionid 存在但已过期）绝不能被当成登录成功。

    实测默认 new_context() 不带磁盘 Cookie，所以旧实现并不会"自我满足"；但只要有
    人把 context 改成持久化 profile 或传入 storage_state，这个判定就是最后一道闸。
    """
    stale = make_login_cookies(time.time() - 2592000 - 3600)

    def poll() -> list[dict]:
        return stale  # 浏览器里始终没登录

    with pytest.raises(SessionRefreshError) as err:
        wait_for_fresh_session(poll, sleep=lambda _s: None, timeout=0)
    assert "仍是旧登录态" in str(err.value)


def test_stale_cookies_are_skipped_then_fresh_login_wins():
    """前几轮仍是旧 Cookie，用户扫码后才拿到新会话。"""
    stale = make_login_cookies(time.time() - 2592000 - 3600, session_id="SANITIZED-OLD")
    fresh = make_login_cookies(time.time(), session_id="SANITIZED-NEW")
    answers = [stale, stale]

    def poll() -> list[dict]:
        return answers.pop(0) if answers else fresh

    result = wait_for_fresh_session(poll, sleep=lambda _s: None, timeout=10)
    assert result["sessionid"] == "SANITIZED-NEW"


def test_relogin_with_still_valid_old_session_is_detected():
    """老会话还没过期时用户重新扫码：必须以 sid_guard 签发时间变新为准。"""
    old_issued = time.time() - 100
    stale = make_login_cookies(old_issued, session_id="SANITIZED-OLD")
    fresh = make_login_cookies(time.time(), session_id="SANITIZED-NEW")
    answers = [stale, fresh]

    def poll() -> list[dict]:
        return answers.pop(0) if answers else fresh

    result = wait_for_fresh_session(
        poll,
        sleep=lambda _s: None,
        timeout=10,
        previous_expires_at=old_issued + 2592000,
    )
    assert result["sessionid"] == "SANITIZED-NEW"


def test_expired_session_without_sid_guard_is_rejected():
    expired = [make_cookie("ttwid"), make_cookie("sessionid")]

    with pytest.raises(SessionRefreshError):
        wait_for_fresh_session(lambda: expired, sleep=lambda _s: None, timeout=0)


def test_extract_login_cookies_still_returns_plain_dict():
    cookies = make_login_cookies(time.time())
    found = extract_login_cookies(cookies)
    assert set(found) == {"ttwid", "sessionid", "sid_guard"}
    assert set(cookies_to_dict(cookies)) == set(found)


# ---- refresh_credentials 端到端接线（假 Playwright，不联网、不启浏览器） ----


def make_fake_playwright(monkeypatch, script, device_url):
    browser = FakeBrowser(FakePage([FakeResponse(device_url)]), script)
    monkeypatch.setattr(
        "doubao2_api.session_refresh.sync_playwright", lambda: FakePlaywright(browser)
    )
    return browser


def test_refresh_credentials_waits_past_stale_session_then_saves_new_one(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / "credentials.json")
    store.save(cookies_to_dict(make_login_cookies(time.time() - 2592000 - 3600)))

    stale = make_login_cookies(time.time() - 2592000 - 3600)
    empty: list[dict] = []
    fresh = make_login_cookies(time.time(), session_id="SANITIZED-NEW")
    device_url = (
        "https://www.doubao.com/chat/completion?device_id=D1&web_id=W1&tea_uuid=T1"
    )
    browser = make_fake_playwright(monkeypatch, [stale, empty, fresh], device_url)

    page = browser.context._page
    original_cookies = browser.context.cookies

    def cookies_with_emit():
        result = original_cookies()
        if result:
            page.emit(FakeResponse(device_url))
        return result

    monkeypatch.setattr(browser.context, "cookies", cookies_with_emit)

    found, expires_at = refresh_credentials(store, timeout=30)

    assert found["sessionid"] == "SANITIZED-NEW"
    assert browser.context.calls == 3  # 旧会话没有被当成登录成功
    assert expires_at > time.time() + 2592000 - 60
    assert store.cookies["sessionid"] == "SANITIZED-NEW"
    assert store.device == {"device_id": "D1", "web_id": "W1", "tea_uuid": "T1"}
    assert browser.context.new_context_kwargs == {"storage_state": {"cookies": [], "origins": []}}
    assert page.waits.count(5000) == 1  # 登录后仍留时间收集 device 参数
    assert browser.closed is True


def test_refresh_credentials_fails_when_user_never_logs_in(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / "credentials.json")
    stale_dict = cookies_to_dict(make_login_cookies(time.time() - 2592000 - 3600))
    store.save(stale_dict)

    browser = make_fake_playwright(monkeypatch, [], "https://www.doubao.com/chat/")
    with pytest.raises(SessionRefreshError):
        refresh_credentials(store, timeout=0)

    assert store.cookies == stale_dict  # 失败时不覆盖凭证
    assert browser.closed is True


def test_refresh_credentials_handles_first_time_login(tmp_path, monkeypatch):
    """没有旧凭证（首次登录）时 previous_expires_at 为 None，不应误判。"""
    store = CredentialStore(tmp_path / "credentials.json")
    fresh = make_login_cookies(time.time(), session_id="SANITIZED-FIRST")
    make_fake_playwright(monkeypatch, [fresh], "https://www.doubao.com/chat/")

    found, expires_at = refresh_credentials(store, timeout=30)

    assert found["sessionid"] == "SANITIZED-FIRST"
    assert expires_at > time.time()
    assert store.cookies["sessionid"] == "SANITIZED-FIRST"
