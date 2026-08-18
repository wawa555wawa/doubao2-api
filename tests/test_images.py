import httpx
import pytest
import respx

from doubao2_api.images import ImageDownloadFailed, ImageStore

PNG_BYTES = b"\x89PNG\r\n\x1a\n fake image bytes"


@respx.mock
async def test_fetch_and_store(tmp_path):
    respx.get("https://cdn.example.com/a.png").mock(
        return_value=httpx.Response(
            200, content=PNG_BYTES, headers={"Content-Type": "image/png"}
        )
    )
    store = ImageStore(tmp_path / "images")
    stored = await store.fetch_and_store("https://cdn.example.com/a.png")
    assert stored.data == PNG_BYTES
    assert stored.path.read_bytes() == PNG_BYTES
    assert stored.b64_json()
    import base64

    assert base64.b64decode(stored.b64_json()) == PNG_BYTES


@respx.mock
async def test_retry_once_then_fail(tmp_path):
    route = respx.get("https://cdn.example.com/b.png").mock(
        return_value=httpx.Response(500)
    )
    store = ImageStore(tmp_path / "images")
    with pytest.raises(ImageDownloadFailed):
        await store.fetch_and_store("https://cdn.example.com/b.png")
    assert route.call_count == 2
