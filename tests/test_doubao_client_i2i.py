import json
from pathlib import Path

import httpx
import respx

from doubao2_api import doubao_client as dc
from doubao2_api.credentials import CredentialStore

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


PNG_BYTES = b"\x89PNG\r\n\x1a\n fake image bytes"


@respx.mock
async def test_generate_with_reference_image(tmp_path):
    respx.post(dc.PREPARE_UPLOAD_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("prepare_upload_success.json"))
    )
    respx.get(url__startswith=dc.APPLY_UPLOAD_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("apply_upload_success.json"))
    )
    put_route = respx.put(url__startswith="https://tos-").mock(
        return_value=httpx.Response(200)
    )
    respx.post(url__startswith=dc.COMMIT_UPLOAD_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("commit_upload_success.json"))
    )
    respx.post(dc.PRE_HANDLE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("pre_handle_success.json"))
    )
    respx.post(dc.COMPLETION_URL).mock(
        return_value=httpx.Response(
            200,
            text=load_text_fixture("sse_success.txt"),
            headers={"Content-Type": "text/event-stream"},
        )
    )
    respx.post(dc.CHAIN_SINGLE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("poll_done.json"))
    )

    store = CredentialStore(tmp_path / "credentials.json")
    store.save({"sessionid": "test"})
    client = dc.DoubaoClient(store, timeout=30, poll_interval=0.01)

    urls = await client.generate("改成油画风", "1024x1024", 1, reference_images=[PNG_BYTES])

    assert put_route.called
    assert put_route.calls[0].request.content == PNG_BYTES
    assert urls  # 走到轮询并取到图
