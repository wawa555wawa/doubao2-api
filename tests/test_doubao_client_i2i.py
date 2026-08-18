import json
import zlib
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


def test_sign_imagex_v4_uses_aws4_prefix():
    """锁住签名实现：密钥派生必须带 AWS4 前缀（漏掉会被服务端拒签）。"""
    token = {"access_key": "ak", "secret_key": "sk", "session_token": "token"}
    signed = dc._sign_imagex_v4(token, "GET", "Action=ApplyImageUpload")
    assert signed["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=ak/")
    assert signed["x-amz-security-token"] == "token"

    # 用无前缀派生重算，签名必须不同（回归保护）
    import hashlib
    import hmac

    amz_date = signed["x-amz-date"]
    date_stamp = amz_date[:8]
    scope = f"{date_stamp}/cn-north-1/imagex/aws4_request"
    query = "Action=ApplyImageUpload"
    canonical_headers = f"x-amz-date:{amz_date}\nx-amz-security-token:token\n"
    canonical_request = (
        f"GET\n/top/v1\n{query}\n{canonical_headers}\n"
        f"x-amz-date;x-amz-security-token\n{hashlib.sha256(b'').hexdigest()}"
    )
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
        + hashlib.sha256(canonical_request.encode()).hexdigest()
    )
    key_date = hmac.new(b"sk", date_stamp.encode(), hashlib.sha256).digest()
    key_region = hmac.new(key_date, b"cn-north-1", hashlib.sha256).digest()
    key_service = hmac.new(key_region, b"imagex", hashlib.sha256).digest()
    key_signing = hmac.new(key_service, b"aws4_request", hashlib.sha256).digest()
    no_prefix_sig = hmac.new(key_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    assert signed["Authorization"].split("Signature=")[1].strip() != no_prefix_sig


@respx.mock
async def test_generate_with_reference_image(tmp_path):
    respx.post(dc.PREPARE_UPLOAD_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("prepare_upload_success.json"))
    )
    respx.get(url__startswith=dc.APPLY_UPLOAD_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("apply_upload_success.json"))
    )
    upload_route = respx.post(
        url__regex=r"https://tos-[^/]+/upload/v1/tos-cn-i-a9rns2rl98/"
    ).mock(return_value=httpx.Response(200, json={"code": 2000}))
    respx.post(url__startswith=dc.COMMIT_UPLOAD_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("commit_upload_success.json"))
    )
    pre_handle_route = respx.post(dc.PRE_HANDLE_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("pre_handle_success.json"))
    )
    completion_route = respx.post(dc.COMPLETION_URL).mock(
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

    assert upload_route.called
    assert upload_route.calls[0].request.method == "POST"
    assert upload_route.calls[0].request.content == PNG_BYTES
    assert upload_route.calls[0].request.headers["Content-CRC32"] == f"{zlib.crc32(PNG_BYTES) & 0xFFFFFFFF:08x}"
    assert upload_route.calls[0].request.headers["Authorization"]
    assert urls  # 走到轮询并取到图

    # 附件消息：block_type 10052 attachment_block，image.uri 来自 commit 响应
    completion_body = json.loads(completion_route.calls[0].request.content)
    attachment_msg = completion_body["messages"][0]
    block = attachment_msg["content_block"][0]
    assert block["block_type"] == 10052
    commit_fixture = load_fixture("commit_upload_success.json")
    expected_uri = commit_fixture["Result"]["Results"][0]["Uri"]
    assert block["content"]["attachment_block"]["attachments"][0]["image"]["uri"] == expected_uri

    # I-2：附件消息的 local_message_id 必须与 pre_handle 请求体中的一致
    pre_handle_body = json.loads(pre_handle_route.calls[0].request.content)
    assert attachment_msg["local_message_id"] == pre_handle_body["local_message_id"]
