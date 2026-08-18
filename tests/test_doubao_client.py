import json
from pathlib import Path

import httpx
import pytest
import respx

from doubao2_api import doubao_client as dc
from doubao2_api.credentials import CredentialStore

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_client(tmp_path, **kwargs) -> dc.DoubaoClient:
    store = CredentialStore(tmp_path / "credentials.json")
    store.save({"sessionid": "test"})
    return dc.DoubaoClient(store, poll_interval=0.01, **kwargs)


def expected_urls() -> list[str]:
    payload = load_fixture("poll_done.json")
    msgs = payload["downlink_body"]["pull_singe_chain_downlink_body"]["messages"]
    urls = []
    for m in msgs:
        if m.get("user_type") != 2:
            continue
        for b in m.get("content_block") or []:
            cb = (b.get("content") or {}).get("creation_block")
            if cb:
                for c in cb.get("creations") or []:
                    urls.append(c["image"]["image_ori"]["url"])
    return urls


def sse_response(fixture: str) -> httpx.Response:
    return httpx.Response(
        200,
        text=load_text_fixture(fixture),
        headers={"Content-Type": "text/event-stream"},
    )


@respx.mock
async def test_generate_success(tmp_path):
    respx.post(dc.COMPLETION_URL).mock(return_value=sse_response("sse_success.txt"))
    respx.post(dc.CHAIN_SINGLE_URL).mock(
        side_effect=[
            httpx.Response(200, json=load_fixture("poll_running.json")),
            httpx.Response(200, json=load_fixture("poll_done.json")),
        ]
    )
    client = make_client(tmp_path, timeout=30)
    urls = await client.generate("一只猫", "1024x1024", 4)
    assert urls == expected_urls()


@respx.mock
async def test_generate_slices_to_n(tmp_path):
    respx.post(dc.COMPLETION_URL).mock(return_value=sse_response("sse_success.txt"))
    respx.post(dc.CHAIN_SINGLE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("poll_done.json"))
    )
    client = make_client(tmp_path, timeout=30)
    urls = await client.generate("一只猫", "1024x1024", 1)
    assert urls == expected_urls()[:1]


@respx.mock
async def test_auth_expired_raises(tmp_path):
    respx.post(dc.COMPLETION_URL).mock(
        return_value=httpx.Response(
            200,
            json=load_fixture("auth_expired.json"),
            headers={"Content-Type": "application/json"},
        )
    )
    client = make_client(tmp_path)
    with pytest.raises(dc.AuthExpired):
        await client.generate("一只猫", "1024x1024", 1)


@respx.mock
async def test_rate_limited_raises(tmp_path):
    respx.post(dc.COMPLETION_URL).mock(return_value=sse_response("sse_rate_limited.txt"))
    client = make_client(tmp_path)
    with pytest.raises(dc.RateLimited):
        await client.generate("一只猫", "1024x1024", 1)


@respx.mock
async def test_poll_timeout_raises(tmp_path):
    respx.post(dc.COMPLETION_URL).mock(return_value=sse_response("sse_success.txt"))
    respx.post(dc.CHAIN_SINGLE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("poll_running.json"))
    )
    client = make_client(tmp_path, timeout=0.05)
    with pytest.raises(dc.GenerationTimeout):
        await client.generate("一只猫", "1024x1024", 1)


def test_size_to_ratio():
    assert dc.size_to_ratio("1024x1024") == "1:1"
    assert dc.size_to_ratio("1792x1024") == "16:9"
    assert dc.size_to_ratio("1024x1792") == "9:16"
    assert dc.size_to_ratio("1344x768") == "16:9"
    assert dc.size_to_ratio("garbage") == "auto"


def expected_urls_ext() -> list[str]:
    payload = load_fixture("poll_done_ext.json")
    msgs = payload["downlink_body"]["pull_singe_chain_downlink_body"]["messages"]
    urls = []
    for m in msgs:
        if m.get("user_type") != 2:
            continue
        ext = m.get("ext") or {}
        full = ext.get("creation_full_content")
        if not full:
            continue
        for packet in json.loads(full):
            info = packet.get("BlockInfo") or {}
            if info.get("BlockType") != 2074:
                continue
            creation = ((info.get("BlockContent") or {}).get("content") or {}).get("creation_block") or {}
            for c in creation.get("creations") or []:
                urls.append(c["image"]["image_ori"]["url"])
    return urls


@respx.mock
async def test_generate_via_sse_ack(tmp_path):
    """SSE_ACK 事件直接给出 conversation_id（实测形态）。"""
    respx.post(dc.COMPLETION_URL).mock(return_value=sse_response("sse_ack.txt"))
    respx.post(dc.CHAIN_SINGLE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("poll_done.json"))
    )
    client = make_client(tmp_path, timeout=30)
    urls = await client.generate("一只猫", "1024x1024", 4)
    assert urls == expected_urls()


@respx.mock
async def test_generate_ext_shape_poll(tmp_path):
    """轮询响应的另一种形态：content_block 为空，结果在 ext.creation_full_content。"""
    respx.post(dc.COMPLETION_URL).mock(return_value=sse_response("sse_ack.txt"))
    respx.post(dc.CHAIN_SINGLE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("poll_done_ext.json"))
    )
    client = make_client(tmp_path, timeout=30)
    urls = await client.generate("向日葵", "1024x1024", 4)
    assert urls == expected_urls_ext()
    assert len(urls) == 4


@respx.mock
async def test_device_params_appended_to_urls(tmp_path):
    """凭证含设备信息时，请求 URL 需携带 device_id/web_id/tea_uuid（风控要求）。"""
    completion = respx.post(url__startswith="https://www.doubao.com/chat/completion").mock(
        return_value=sse_response("sse_ack.txt")
    )
    poll = respx.post(url__startswith="https://www.doubao.com/im/chain/single").mock(
        return_value=httpx.Response(200, json=load_fixture("poll_done.json"))
    )
    store = CredentialStore(tmp_path / "credentials.json")
    store.save(
        {"sessionid": "test"},
        device={"device_id": "111", "web_id": "222", "tea_uuid": "222"},
    )
    client = dc.DoubaoClient(store, timeout=30, poll_interval=0.01)
    await client.generate("一只猫", "1024x1024", 1)
    for route in (completion, poll):
        url = str(route.calls[0].request.url)
        assert "device_id=111" in url
        assert "web_id=222" in url
        assert "tea_uuid=222" in url
        assert "web_tab_id=" in url
