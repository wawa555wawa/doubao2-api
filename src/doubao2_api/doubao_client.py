from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import time
import uuid
import zlib
from urllib.parse import quote

import httpx

from .credentials import CredentialStore


class AuthExpired(Exception):
    """登录态失效（Cookie 过期或未登录）。"""


class GenerationFailed(Exception):
    """豆包侧生图失败（含内容审核拦截）。"""


class RateLimited(Exception):
    """触发豆包频率限制/风控。"""


class GenerationTimeout(Exception):
    """轮询等待生图结果超时。"""


BASE_QUERY = (
    "aid=497858&device_platform=web&language=zh&pkg_type=release_version"
    "&real_aid=497858&region=CN&samantha_web=1&sys_region=CN"
    "&use-olympus-account=1&version_code=20800&web_platform=browser"
    "&pc_version=3.32.51&doubao_pc_version=3.32.51&doubao_device_platform=web"
)
COMPLETION_URL = f"https://www.doubao.com/chat/completion?{BASE_QUERY}"
CHAIN_SINGLE_URL = f"https://www.doubao.com/im/chain/single?{BASE_QUERY}"
PREPARE_UPLOAD_URL = f"https://www.doubao.com/alice/resource/prepare_upload?{BASE_QUERY}"
APPLY_UPLOAD_URL = "https://www.doubao.com/top/v1"
COMMIT_UPLOAD_URL = "https://www.doubao.com/top/v1"
PRE_HANDLE_URL = f"https://www.doubao.com/alice/message/pre_handle_v2_without_conv?{BASE_QUERY}"
BOT_ID = "7338286299411103781"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
IM_CONTENT_TYPE = "application/json; encoding=utf-8"
AUTH_EXPIRED_CODE = 710012001
RATE_LIMITED_CODE = 710022004

_RATIOS = [(1 / 1, "1:1"), (4 / 3, "4:3"), (3 / 4, "3:4"), (16 / 9, "16:9"), (9 / 16, "9:16")]


def size_to_ratio(size: str) -> str:
    try:
        w, h = size.lower().split("x")
        r = int(w) / int(h)
    except (ValueError, ZeroDivisionError):
        return "auto"
    return min(_RATIOS, key=lambda t: abs(t[0] - r))[1]


def _encode_query(params: dict[str, str]) -> str:
    """构造 imagex 网关 query（canonical 与真实 URL 共用同一编码）。"""
    return "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
        for k, v in sorted(params.items())
    )


def _sign_imagex_v4(
    token: dict[str, str], method: str, query: str, body: bytes = b""
) -> dict[str, str]:
    """用 prepare_upload 返回的临时凭证对 /top/v1 请求做 AWS4-HMAC-SHA256 签名。

    抓包核实的 SignedHeaders 仅 x-amz-date 与 x-amz-security-token；密钥派生
    带 "AWS4" 前缀，region=cn-north-1、service=imagex 均由服务端实测验证。
    """
    access_key = token["access_key"]
    secret_key = token["secret_key"]
    session_token = token["session_token"]
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    scope = f"{date_stamp}/cn-north-1/imagex/aws4_request"

    canonical_headers = f"x-amz-date:{amz_date}\nx-amz-security-token:{session_token}\n"
    signed_headers = "x-amz-date;x-amz-security-token"
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = (
        f"{method}\n/top/v1\n{query}\n{canonical_headers}\n"
        f"{signed_headers}\n{payload_hash}"
    )
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
        + hashlib.sha256(canonical_request.encode()).hexdigest()
    )
    # 注意：火山 imagex 的密钥派生带 "AWS4" 前缀（与 AWS 一致），实测无前缀会被拒签。
    key_date = hmac.new(("AWS4" + secret_key).encode(), date_stamp.encode(), hashlib.sha256).digest()
    key_region = hmac.new(key_date, b"cn-north-1", hashlib.sha256).digest()
    key_service = hmac.new(key_region, b"imagex", hashlib.sha256).digest()
    key_signing = hmac.new(key_service, b"aws4_request", hashlib.sha256).digest()
    signature = hmac.new(key_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    return {
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "x-amz-date": amz_date,
        "x-amz-security-token": session_token,
    }


def parse_sse_events(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event: str | None = None
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line == "" and event is not None:
            try:
                data = json.loads("".join(data_lines) or "{}")
            except json.JSONDecodeError:
                data = {}
            events.append((event, data))
            event, data_lines = None, []
    return events


def sign_v4(
    method: str,
    path: str,
    params: dict[str, str],
    body: bytes,
    access_key: str,
    secret_key: str,
    session_token: str,
    amzdate: str,
) -> str:
    """火山引擎 ImageX 的 AWS4-HMAC-SHA256 签名（/top/v1 的 apply/commit 需要）。

    抓包确认 SignedHeaders 只有 x-amz-date 与 x-amz-security-token。
    """
    scope = f"{amzdate[:8]}/cn-north-1/imagex/aws4_request"
    qs = "&".join(
        f"{quote(k, safe='-_.~')}={quote(str(v), safe='-_.~')}" for k, v in sorted(params.items())
    )
    canon = (
        f"{method}\n{path}\n{qs}\n"
        f"x-amz-date:{amzdate}\nx-amz-security-token:{session_token}\n\n"
        f"x-amz-date;x-amz-security-token\n{hashlib.sha256(body).hexdigest()}"
    )
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amzdate}\n{scope}\n{hashlib.sha256(canon.encode()).hexdigest()}"
    )

    def _hm(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k = _hm(("AWS4" + secret_key).encode(), amzdate[:8])
    k = _hm(k, "cn-north-1")
    k = _hm(k, "imagex")
    k = _hm(k, "aws4_request")
    sig = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders=x-amz-date;x-amz-security-token, Signature={sig}"
    )


def _utcnow_amzdate() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _size_hint(size: str) -> str:
    """把 size 转成自然语言提示，附在提示词后。

    实测 chat_ability 里的 ratio 字段并不总是生效，模型对提示词中的
    "图片比例为X:Y" / "图片尺寸为WxH" 描述更稳定。
    """
    parts = []
    ratio = size_to_ratio(size)
    if ratio != "auto":
        parts.append(f"图片比例为{ratio}")
    try:
        w, h = (int(v) for v in size.lower().split("x"))
        if w > 0 and h > 0:
            parts.append(f"图片尺寸为{size.lower()}")
    except (TypeError, ValueError):
        pass
    return "，" + "，".join(parts) if parts else ""


def build_completion_body(
    prompt: str,
    size: str,
    attachments: list[dict] | None = None,
    attachment_local_message_ids: list[str] | None = None,
) -> dict:
    # attachment_local_message_ids[i] 对应 attachments[i]：抓包确认每个附件消息的
    # local_message_id 必须与该附件 pre_handle 请求体中的 local_message_id 一致。
    ratio = size_to_ratio(size)
    messages: list[dict] = []
    for i, attachment in enumerate(attachments or []):
        if attachment_local_message_ids is not None:
            local_message_id = attachment_local_message_ids[i]
        else:
            local_message_id = str(uuid.uuid1())
        messages.append(
            {
                "local_message_id": local_message_id,
                "content_block": [
                    {
                        "block_type": 10052,
                        "content": {
                            "attachment_block": {"attachments": [attachment]},
                            "pc_event_block": "",
                        },
                        "block_id": str(uuid.uuid4()),
                        "parent_id": "",
                        "meta_info": [],
                        "append_fields": [],
                    }
                ],
                "message_status": 0,
            }
        )
    messages.append(
        {
            "local_message_id": str(uuid.uuid1()),
            "content_block": [
                {
                    "block_type": 10000,
                    "content": {
                        "text_block": {
                            "text": f"生成图片：{prompt}{_size_hint(size)}",
                            "icon_url": "",
                            "icon_url_dark": "",
                            "summary": "",
                        },
                        "pc_event_block": "",
                    },
                    "block_id": str(uuid.uuid4()),
                    "parent_id": "",
                    "meta_info": [],
                    "append_fields": [],
                }
            ],
            "message_status": 0,
        }
    )
    now_ms = int(time.time() * 1000)
    return {
        "client_meta": {
            "local_conversation_id": f"local_{uuid.uuid4().int % 10**16}",
            "conversation_id": "",
            "bot_id": BOT_ID,
            "last_section_id": "",
            "last_message_index": None,
        },
        "messages": messages,
        "option": {
            "send_message_scene": "",
            "create_time_ms": now_ms,
            "collect_id": "",
            "is_audio": False,
            "answer_with_suggest": False,
            "tts_switch": False,
            "need_deep_think": 0,
            "click_clear_context": False,
            "from_suggest": False,
            "is_regen": False,
            "is_replace": False,
            "is_from_click_option": False,
            "is_from_click_softlink": False,
            "disable_sse_cache": False,
            "select_text_action": "",
            "is_select_text": False,
            "resend_for_regen": False,
            "scene_type": 0,
            "unique_key": str(uuid.uuid4()),
            "start_seq": 0,
            "need_create_conversation": True,
            "conversation_init_option": {"need_ack_conversation": True},
            "regen_query_id": [],
            "edit_query_id": [],
            "regen_instruction": "",
            "no_replace_for_regen": False,
            "message_from": 0,
            "shared_app_name": "",
            "shared_app_id": "",
            "sse_recv_event_options": {"support_chunk_delta": True},
            "is_ai_playground": False,
            "is_old_user": True,
            "recovery_option": {
                "is_recovery": False,
                "req_create_time_sec": now_ms // 1000,
                "append_sse_event_scene": 0,
            },
            "message_storage_type": 0,
            "related_deleted_message_ids": {},
            "connector_info_list": [],
            "model_config": {"model_item_key": "", "model_extra_params": {}},
            "aggregate_params": {
                "conversation_mode": "",
                "mode_id": "",
                "model_item_key": "",
                "agent_mode": "",
                "reasoning_effort": "",
                "provider_id": "",
            },
        },
        "chat_ability": {
            "ability_type": 3,
            "ability_param": json.dumps(
                {"ability_param": {"model": "Seedream 4.5", "ratio": ratio}, "ability_type": 1}
            ),
        },
        "user_context": [],
        "ext": {
            "answer_with_suggest": "0",
            "sub_conv_firstmet_type": "1",
            "collection_id": "",
            "is_finish": "1",
            "conversation_init_option": '{"need_ack_conversation":true}',
            "commerce_credit_config_enable": "0",
        },
    }


class DoubaoClient:
    def __init__(self, store: CredentialStore, timeout: float = 120.0, poll_interval: float = 2.0) -> None:
        self._store = store
        self._timeout = timeout
        self._poll_interval = poll_interval

    def _query(self) -> str:
        # 风控要求携带设备标识参数（实测缺 device_id/web_id 会被 rate limited）；
        # 它们由扫码登录时从浏览器提取并保存在凭证里。无设备信息时退化为最小参数集。
        q = BASE_QUERY
        device = self._store.device
        for key in ("device_id", "web_id", "tea_uuid"):
            if device.get(key):
                q += f"&{key}={device[key]}"
        if device:
            q += f"&web_tab_id={uuid.uuid4()}"
        return q

    def _headers(self, im: bool = False) -> dict[str, str]:
        return {
            "Content-Type": IM_CONTENT_TYPE if im else "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": USER_AGENT,
            "Referer": "https://www.doubao.com/chat/",
            "Origin": "https://www.doubao.com",
        }

    async def generate(
        self,
        prompt: str,
        size: str,
        n: int = 1,
        reference_images: list[bytes] | None = None,
    ) -> list[str]:
        attachments: list[dict] = []
        attachment_local_message_ids: list[str] = []
        async with httpx.AsyncClient(cookies=self._store.cookies, timeout=60) as client:
            for img in reference_images or []:
                attachment, local_message_id = await self._upload(client, img)
                attachments.append(attachment)
                attachment_local_message_ids.append(local_message_id)
            body = build_completion_body(
                prompt,
                size,
                attachments or None,
                attachment_local_message_ids or None,
            )
            conversation_id = await self._send(client, body)
            urls = await self._poll(client, conversation_id)
        return urls[:n]

    async def _upload(self, client: httpx.AsyncClient, image: bytes) -> tuple[dict, str]:
        """上传单张参考图，返回 (附件字典, pre_handle 用的 local_message_id)。

        抓包确认：附件消息的 local_message_id 必须与 pre_handle 请求体中的一致。
        """
        # 1. prepare
        resp = await client.post(
            PREPARE_UPLOAD_URL,
            headers=self._headers(),
            json={"tenant_id": "5", "scene_id": "5", "resource_type": 2},
        )
        payload = resp.json()
        if payload.get("code") != 0:
            if payload.get("code") == AUTH_EXPIRED_CODE:
                raise AuthExpired("登录已过期")
            raise GenerationFailed(f"准备上传失败: {payload}")
        data = payload.get("data") or {}
        service_id = data["service_id"]
        token = data.get("upload_auth_token") or {}

        # 2. apply（GET /top/v1?Action=ApplyImageUpload...，需 SigV4 签名）
        apply_query = _encode_query(
            {
                "Action": "ApplyImageUpload",
                "Version": "2018-08-01",
                "ServiceId": service_id,
                "NeedFallback": "true",
                "FileSize": str(len(image)),
                "FileExtension": ".png",
            }
        )
        resp = await client.get(
            f"{APPLY_UPLOAD_URL}?{apply_query}",
            headers={**self._headers(), **_sign_imagex_v4(token, "GET", apply_query)},
        )
        payload = resp.json()
        if payload.get("ResponseMetadata", {}).get("Error"):
            raise GenerationFailed(f"申请上传地址失败: {payload}")
        address = payload["Result"]["UploadAddress"]
        store_info = address["StoreInfos"][0]
        store_uri = store_info["StoreUri"]
        upload_host = address["UploadHosts"][0]
        session_key = address["SessionKey"]

        # 3. POST 二进制到对象存储（抓包核实：/upload/v1/ 前缀 + Content-CRC32 十六进制）
        upload_headers = {
            "Authorization": store_info["Auth"],
            "Content-CRC32": f"{zlib.crc32(image) & 0xFFFFFFFF:08x}",
            "Content-Type": "application/octet-stream",
            'Content-Disposition': 'attachment; filename="reference.png"',
        }
        resp = await client.post(
            f"https://{upload_host}/upload/v1/{store_uri}",
            headers=upload_headers,
            content=image,
        )
        resp.raise_for_status()
        upload_payload = resp.json()
        if upload_payload.get("code") != 2000:
            raise GenerationFailed(f"上传图片失败: {upload_payload}")

        # 4. commit（同样需要 SigV4 签名）
        commit_body = json.dumps({"SessionKey": session_key}, separators=(",", ":"))
        commit_query = _encode_query(
            {
                "Action": "CommitImageUpload",
                "Version": "2018-08-01",
                "ServiceId": service_id,
            }
        )
        resp = await client.post(
            f"{COMMIT_UPLOAD_URL}?{commit_query}",
            headers={
                **self._headers(),
                **_sign_imagex_v4(token, "POST", commit_query, commit_body.encode()),
            },
            content=commit_body,
        )
        payload = resp.json()
        if payload.get("ResponseMetadata", {}).get("Error"):
            raise GenerationFailed(f"确认上传失败: {payload}")
        result = payload["Result"]
        results = result.get("Results") or []
        if not results or results[0].get("UriStatus") != 2000:
            raise GenerationFailed(f"确认上传未完成: {payload}")
        uri = results[0]["Uri"]
        plugin = (result.get("PluginResult") or [{}])[0]
        width = plugin.get("ImageWidth") or 0
        height = plugin.get("ImageHeight") or 0

        # 5. pre_handle（注册附件实体）
        identifier = str(uuid.uuid1())
        local_message_id = str(uuid.uuid1())
        resp = await client.post(
            PRE_HANDLE_URL,
            headers=self._headers(),
            json={
                "uplink_entity": {
                    "entity_type": 2,
                    "entity_content": {"image": {"key": uri}},
                    "identifier": identifier,
                },
                "bot_id": BOT_ID,
                "local_message_id": local_message_id,
            },
        )
        payload = resp.json()
        if payload.get("code") != 0:
            if payload.get("code") == AUTH_EXPIRED_CODE:
                raise AuthExpired("登录已过期")
            raise GenerationFailed(f"附件注册失败: {payload}")

        attachment = {
            "type": 1,
            "identifier": identifier,
            "image": {
                "name": "reference.png",
                "uri": uri,
                "image_ori": {"url": "", "width": width, "height": height, "format": "", "url_formats": {}},
            },
            "parse_state": 0,
            "review_state": 1,
            "upload_status": 1,
            "progress": 100,
            "src": "",
        }
        return attachment, local_message_id

    async def _send(self, client: httpx.AsyncClient, body: dict) -> str:
        async with client.stream(
            "POST", f"https://www.doubao.com/chat/completion?{self._query()}", json=body, headers=self._headers()
        ) as resp:
            content_type = resp.headers.get("content-type", "")
            text = "".join([chunk async for chunk in resp.aiter_text()])
        if "text/event-stream" not in content_type:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                raise GenerationFailed(f"生图请求返回非预期响应: {text[:200]}")
            if payload.get("code") == AUTH_EXPIRED_CODE:
                raise AuthExpired(payload.get("msg") or "登录已过期")
            raise GenerationFailed(f"生图请求失败: {text[:200]}")
        conversation_id = None
        for event, data in parse_sse_events(text):
            if event == "STREAM_ERROR":
                if data.get("error_code") == RATE_LIMITED_CODE:
                    raise RateLimited(data.get("error_msg") or "rate limited")
                raise GenerationFailed(f"生图流错误: {data}")
            if event == "SSE_ACK":
                # 实测最常见：ACK 里直接给出 conversation_id
                ack_meta = data.get("ack_client_meta") or {}
                if ack_meta.get("conversation_id"):
                    conversation_id = ack_meta["conversation_id"]
            if event == "DOWNLINK_CMD":
                notify = (data.get("downlink_body") or {}).get("pull_msg_notify") or {}
                if notify.get("conversation_id"):
                    conversation_id = notify["conversation_id"]
        if conversation_id is None:
            raise GenerationFailed("未能从生图响应中解析出会话 ID")
        return conversation_id

    async def _poll(self, client: httpx.AsyncClient, conversation_id: str) -> list[str]:
        deadline = asyncio.get_running_loop().time() + self._timeout
        while True:
            resp = await client.post(
                f"https://www.doubao.com/im/chain/single?{self._query()}",
                headers=self._headers(im=True),
                json={
                    "cmd": 3100,
                    "uplink_body": {
                        "pull_singe_chain_uplink_body": {
                            "conversation_id": conversation_id,
                            "anchor_index": 9007199254740991,
                            "conversation_type": 3,
                            "direction": 1,
                            "limit": 20,
                            "ext": {},
                            "filter": {"index_list": []},
                        }
                    },
                    "sequence_id": str(uuid.uuid4()),
                    "channel": 2,
                    "version": "1",
                },
            )
            payload = resp.json()
            if payload.get("status_code") == AUTH_EXPIRED_CODE or payload.get("code") == AUTH_EXPIRED_CODE:
                raise AuthExpired("登录已过期")
            done, failure = self._parse_poll(payload)
            if done is not None:
                return done
            if failure is not None:
                raise GenerationFailed(failure)
            if asyncio.get_running_loop().time() >= deadline:
                raise GenerationTimeout(f"生图超时（{self._timeout}s）")
            await asyncio.sleep(self._poll_interval)

    @staticmethod
    def _parse_poll(payload: dict) -> tuple[list[str] | None, str | None]:
        """返回 (图片URL列表, 失败原因)；两者都为 None 表示仍在生成中。

        实测豆包经常先回一条纯文字消息（如"已生成……"）、图片结果在后续消息里，
        所以必须遍历全部 bot 消息；没找到结果块就继续轮询，不提前判失败。
        """
        messages = (
            (payload.get("downlink_body") or {})
            .get("pull_singe_chain_downlink_body", {})
            .get("messages")
        ) or []
        bot_msgs = [m for m in messages if m.get("user_type") == 2]
        if not bot_msgs:
            return None, None

        # 形态一：图片在消息 content_block 的 creation_block 里
        for msg in bot_msgs:
            for b in msg.get("content_block") or []:
                creation = (b.get("content") or {}).get("creation_block")
                if not creation or not b.get("is_finish"):
                    continue
                urls = [c["image"]["image_ori"]["url"] for c in creation.get("creations") or [] if c.get("image")]
                if urls:
                    return urls, None

        # 形态二：content_block 为空，图片在 ext.creation_full_content
        # （JSON 字符串，元素为 {BlockInfo: {BlockType, BlockContent, BlockMeta}}）。
        for msg in bot_msgs:
            full_content = (msg.get("ext") or {}).get("creation_full_content")
            if not isinstance(full_content, str) or not full_content:
                continue
            try:
                packets = json.loads(full_content)
            except json.JSONDecodeError:
                continue
            for packet in packets:
                info = packet.get("BlockInfo") or {}
                if info.get("BlockType") != 2074:
                    continue
                if not (info.get("BlockMeta") or {}).get("is_finish"):
                    return None, None  # 结果块已出现但未完成，继续等
                block_content = info.get("BlockContent") or {}
                creation = (block_content.get("content") or {}).get("creation_block") or {}
                urls = [c["image"]["image_ori"]["url"] for c in creation.get("creations") or [] if c.get("image")]
                if urls:
                    return urls, None
        return None, None
