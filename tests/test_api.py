import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from doubao2_api.config import Settings
from doubao2_api.doubao_client import AuthExpired, GenerationFailed, RateLimited
from doubao2_api.images import StoredImage
from doubao2_api.main import create_app

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


def make_client(tmp_path, behavior, monkeypatch=None):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    doubao = FakeDoubao(behavior)
    app = create_app(settings=settings, doubao=doubao, images=FakeImages(tmp_path))
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
