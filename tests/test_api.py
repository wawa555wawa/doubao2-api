import base64
import logging
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _auth_fixtures import cookies_to_dict, make_login_cookies

from doubao2_api.config import Settings
from doubao2_api.credentials import CredentialStore
from doubao2_api.doubao_client import AuthExpired, GenerationFailed, RateLimited
from doubao2_api.images import StoredImage
from doubao2_api.main import create_app, log_credentials_health

PNG_BYTES = b"\x89PNG\r\n\x1a\n fake"


class FakeDoubao:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    async def generate(self, prompt, size, n=1, reference_images=None):
        self.calls.append(
            {"prompt": prompt, "size": size, "n": n, "reference_images": reference_images}
        )
        effect = self.behavior(len(self.calls))
        if isinstance(effect, Exception):
            raise effect
        return effect


class FakeImages:
    def __init__(self, tmp_path):
        self.dir = tmp_path / "images"
        self.dir.mkdir()

    async def fetch_and_store(self, url):
        path = self.dir / "fake.png"
        path.write_bytes(PNG_BYTES)
        return StoredImage(id="fake", path=path, data=PNG_BYTES)


def make_client(tmp_path, behavior, store=None):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    doubao = FakeDoubao(behavior)
    app = create_app(
        settings=settings, doubao=doubao, images=FakeImages(tmp_path), store=store
    )
    return TestClient(app), doubao


def test_generations_b64(tmp_path):
    client, doubao = make_client(tmp_path, lambda _: ["https://cdn.example.com/a.png"])
    resp = client.post(
        "/v1/images/generations", json={"prompt": "一只猫", "size": "1024x1024", "n": 1}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert base64.b64decode(body["data"][0]["b64_json"]) == PNG_BYTES
    assert doubao.calls[0]["prompt"] == "一只猫"


def test_generations_url_format(tmp_path):
    client, _ = make_client(
        tmp_path, lambda _: ["https://cdn.example.com/a.png"]
    )
    resp = client.post(
        "/v1/images/generations",
        json={"prompt": "x", "response_format": "url"},
    )
    assert resp.status_code == 200
    url = resp.json()["data"][0]["url"]
    assert url.endswith("/images/fake.png")
    assert client.get(url).status_code == 200


def test_edits_multipart(tmp_path):
    client, doubao = make_client(tmp_path, lambda _: ["https://cdn.example.com/a.png"])
    resp = client.post(
        "/v1/images/edits",
        files={"image": ("ref.png", PNG_BYTES, "image/png")},
        data={"prompt": "改成油画风"},
    )
    assert resp.status_code == 200
    assert doubao.calls[0]["reference_images"] == [PNG_BYTES]


def test_generation_failed_maps_to_400(tmp_path):
    client, _ = make_client(tmp_path, lambda _: GenerationFailed("内容违规"))
    resp = client.post("/v1/images/generations", json={"prompt": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "内容违规"


def test_rate_limited_maps_to_429(tmp_path):
    client, _ = make_client(tmp_path, lambda _: RateLimited("太快了"))
    resp = client.post("/v1/images/generations", json={"prompt": "x"})
    assert resp.status_code == 429


def test_auth_expired_triggers_refresh_and_retry(tmp_path, monkeypatch):
    behavior = lambda n: AuthExpired() if n == 1 else ["https://cdn.example.com/a.png"]
    client, doubao = make_client(tmp_path, behavior)
    refreshed = []
    monkeypatch.setattr(
        "doubao2_api.main.refresh_credentials",
        lambda store, timeout, headless: refreshed.append(True),
    )
    resp = client.post("/v1/images/generations", json={"prompt": "x"})
    assert resp.status_code == 200
    assert refreshed == [True]
    assert len(doubao.calls) == 2


def test_auth_expired_refresh_timeout_maps_to_401(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, lambda _: AuthExpired())

    def fake_refresh(store, timeout, headless):
        raise TimeoutError("扫码登录等待超时")

    monkeypatch.setattr("doubao2_api.main.refresh_credentials", fake_refresh)
    resp = client.post("/v1/images/generations", json={"prompt": "x"})
    assert resp.status_code == 401
    assert "doubao2-api login" in resp.json()["error"]["message"]


def test_auth_expired_after_refresh_maps_to_401(tmp_path, monkeypatch):
    client, doubao = make_client(tmp_path, lambda _: AuthExpired())
    monkeypatch.setattr(
        "doubao2_api.main.refresh_credentials",
        lambda store, timeout, headless: None,
    )
    resp = client.post("/v1/images/generations", json={"prompt": "x"})
    assert resp.status_code == 401
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "auth_expired"
    assert "doubao2-api login" in body["error"]["message"]
    assert len(doubao.calls) == 2


def test_session_refresh_error_maps_to_401(tmp_path, monkeypatch):
    """续期没拿到新会话时抛的 SessionRefreshError 必须是 401 而不是 500。"""
    from doubao2_api.session_refresh import SessionRefreshError

    client, doubao = make_client(tmp_path, lambda _: AuthExpired())

    def fake_refresh(store, timeout, headless):
        raise SessionRefreshError("扫码登录等待超时（0s）：检测到的仍是旧登录态")

    monkeypatch.setattr("doubao2_api.main.refresh_credentials", fake_refresh)
    resp = client.post("/v1/images/generations", json={"prompt": "x"})
    assert resp.status_code == 401
    message = resp.json()["error"]["message"]
    assert "仍是旧登录态" in message
    assert "doubao2-api login" in message
    assert len(doubao.calls) == 1  # 续期失败就别再打豆包了


# ---- 本地过期检测：不发必然失败的请求 ----


def test_expired_credentials_refresh_before_first_request(tmp_path, monkeypatch):

    store = CredentialStore(tmp_path / "credentials.json")
    store.save(cookies_to_dict(make_login_cookies(time.time() - 2592000 - 3600)))
    assert store.is_expired()

    client, doubao = make_client(tmp_path, lambda _: ["https://cdn.example.com/a.png"], store)
    refreshed = []

    def fake_refresh(store, timeout, headless):
        refreshed.append(True)
        store.save(cookies_to_dict(make_login_cookies(time.time())))

    monkeypatch.setattr("doubao2_api.main.refresh_credentials", fake_refresh)
    resp = client.post("/v1/images/generations", json={"prompt": "x"})
    assert resp.status_code == 200
    assert refreshed == [True]
    assert len(doubao.calls) == 1  # 只发一次：先续期再请求
    assert store.is_expired() is False


def test_expired_credentials_refresh_failure_maps_to_401(tmp_path, monkeypatch):

    from doubao2_api.session_refresh import SessionRefreshError

    store = CredentialStore(tmp_path / "credentials.json")
    store.save(cookies_to_dict(make_login_cookies(time.time() - 2592000 - 3600)))

    client, doubao = make_client(tmp_path, lambda _: ["https://cdn.example.com/a.png"], store)

    def fake_refresh(store, timeout, headless):
        raise SessionRefreshError("扫码登录等待超时（1s）：检测到的仍是旧登录态")

    monkeypatch.setattr("doubao2_api.main.refresh_credentials", fake_refresh)
    resp = client.post("/v1/images/generations", json={"prompt": "x"})
    assert resp.status_code == 401
    assert "doubao2-api login" in resp.json()["error"]["message"]
    assert doubao.calls == []  # 没发请求


# ---- 启动体检日志 ----


def test_health_log_flags_expired_credentials(tmp_path, caplog):

    store = CredentialStore(tmp_path / "credentials.json")
    store.save(cookies_to_dict(make_login_cookies(time.time() - 2592000 - 3600)))
    with caplog.at_level(logging.ERROR, logger="doubao2_api"):
        log_credentials_health(store, warn_days=3)
    assert "已" in caplog.text and "过期" in caplog.text
    assert "doubao2-api login" in caplog.text


def test_health_log_warns_before_expiry(tmp_path, caplog):

    store = CredentialStore(tmp_path / "credentials.json")
    store.save(cookies_to_dict(make_login_cookies(time.time() - 2592000 + 86400)))
    with caplog.at_level(logging.INFO, logger="doubao2_api"):
        log_credentials_health(store, warn_days=3)
    assert caplog.records[-1].levelno == logging.WARNING
    assert "即将过期" in caplog.text


def test_health_log_when_credentials_missing(tmp_path, caplog):
    store = CredentialStore(tmp_path / "credentials.json")
    with caplog.at_level(logging.WARNING, logger="doubao2_api"):
        log_credentials_health(store, warn_days=3)
    assert "未找到登录凭证" in caplog.text


def test_health_log_quiet_when_plenty_of_time_left(tmp_path, caplog):

    store = CredentialStore(tmp_path / "credentials.json")
    store.save(cookies_to_dict(make_login_cookies(time.time())))
    with caplog.at_level(logging.INFO, logger="doubao2_api"):
        log_credentials_health(store, warn_days=3)
    assert caplog.records[-1].levelno == logging.INFO
    assert "登录态有效期" in caplog.text


# ---- 保活接入 ----


def test_keepalive_disabled_by_default_does_not_start(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(
        "doubao2_api.main.run_keepalive_cycles",
        lambda *a, **k: started.append(True),
    )
    settings = Settings(_env_file=None, data_dir=tmp_path)
    app = create_app(settings=settings, doubao=FakeDoubao(lambda _: []), images=FakeImages(tmp_path))
    with TestClient(app):
        pass
    assert started == []


def test_keepalive_starts_and_stops_with_app(tmp_path, monkeypatch, caplog):
    """启用保活时，lifespan 要真的把循环拉起来，并在关闭时停掉。"""
    import threading

    calls = []

    def fake_cycles(session_factory, store, interval, stop, *args, **kwargs):
        calls.append({"interval": interval, "profile": session_factory().profile_dir})
        stop.wait(5)  # 模拟常驻，等 lifespan 关闭时置位

    monkeypatch.setattr("doubao2_api.main.run_keepalive_cycles", fake_cycles)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        keepalive_enabled=True,
        keepalive_interval=1234.0,
        headless=True,
    )
    app = create_app(
        settings=settings, doubao=FakeDoubao(lambda _: []), images=FakeImages(tmp_path)
    )
    with caplog.at_level(logging.INFO, logger="doubao2_api"):
        with TestClient(app):
            for _ in range(100):  # 等后台线程进到 fake_cycles
                if calls:
                    break
                time.sleep(0.02)
        stop_event = app.state.keepalive_stop
    assert calls and calls[0]["interval"] == 1234.0
    assert calls[0]["profile"] == tmp_path / "browser-profile"
    assert stop_event.is_set()  # 服务关闭时通知保活线程退出
    assert "登录态保活已启用" in caplog.text
