"""MCP stdio server：把豆包生图能力暴露给 OpenClaw 等 MCP 客户端。

stdio 模式下 stdout 是 JSON-RPC 专用通道；本模块的日志全部走 stderr，
并且启动时会强制把根 logger 指向 stderr，避免第三方库的日志污染协议流。
"""

from __future__ import annotations

import base64
import binascii
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .config import get_settings
from .credentials import CredentialStore
from .doubao_client import AuthExpired, DoubaoClient, RateLimited
from .images import ImageStore

logger = logging.getLogger(__name__)

# 契约要求：生成超时不得低于 120 秒；即使 GENERATION_TIMEOUT 配得更小也按 120 秒算。
MIN_TIMEOUT_SECONDS = 120.0

# 参考图限制：防止一次工具调用读入/解码过多数据。
MAX_REFERENCE_IMAGES = 8
MAX_REFERENCE_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_REFERENCE_IMAGE_MB = MAX_REFERENCE_IMAGE_BYTES // (1024 * 1024)

TOOL_DESCRIPTION = (
    "使用豆包网页版生成图片。prompt 是提示词；size 形如 1024x1024；n 是生成数量。"
    "只传 prompt 是文生图；提供 reference_image_paths 或 reference_images_base64 是图生图，"
    f"最多 {MAX_REFERENCE_IMAGES} 张参考图、单张不超过 {_MAX_REFERENCE_IMAGE_MB} MB，"
    "两类可同时使用（路径在前、base64 在后）。"
    "reference_image_paths 是 server 本机可读的文件路径；"
    "reference_images_base64 是 base64 字符串（支持 data:image/...;base64, 前缀）。"
    "生图通常需要 30-120 秒，服务端轮询超时至少 120 秒，请为客户端设置不短于 120 秒的等待时间。"
    "成功时返回图片的本地绝对路径列表（图片已保存到 data/images/）。"
)

mcp = FastMCP(
    "doubao2-api",
    instructions=(
        "调用 generate_image 生成图片；返回本地图片绝对路径。"
        "若提示登录过期，请先运行 `doubao2-api login` 扫码登录。"
    ),
)


@dataclass
class _Runtime:
    store: CredentialStore
    client: DoubaoClient
    images: ImageStore


def _build_runtime() -> _Runtime:
    """按现有方式加载凭证，并复用 DoubaoClient / ImageStore。"""
    settings = get_settings()
    store = CredentialStore(settings.data_dir / "credentials.json")
    client = DoubaoClient(
        store,
        timeout=max(settings.generation_timeout, MIN_TIMEOUT_SECONDS),
    )
    images = ImageStore(settings.data_dir / "images")
    return _Runtime(store=store, client=client, images=images)


def _summarize(exc: BaseException, limit: int = 500) -> str:
    detail = " ".join(str(exc).split()) or "无错误详情"
    if len(detail) > limit:
        detail = detail[:limit].rstrip() + "…"
    return f"{type(exc).__name__}: {detail}"


def _rate_limited_error(exc: BaseException) -> ToolError:
    return ToolError(
        f"豆包侧触发限流（429），请等待几分钟后重试。原始错误：{_summarize(exc)}"
    )


def _check_reference_size(data: bytes, label: str) -> None:
    if len(data) > MAX_REFERENCE_IMAGE_BYTES:
        raise ToolError(
            f"参考图过大：{label}（{len(data)} 字节，单张上限 "
            f"{MAX_REFERENCE_IMAGE_BYTES} 字节）"
        )


def _read_reference_file(path: Path) -> bytes:
    if not path.is_file():
        raise ToolError(f"参考图不存在或不是文件：{path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ToolError(f"读取参考图失败：{path}（{exc}）") from exc
    if not data:
        raise ToolError(f"参考图为空：{path}")
    _check_reference_size(data, str(path))
    return data


def _decode_reference_base64(value: str, index: int) -> bytes:
    raw = value.strip()
    if raw.startswith("data:"):
        _header, separator, payload = raw.partition(",")
        if not separator:
            raise ToolError(f"第 {index} 张 base64 参考图格式错误：缺少 data URI 逗号")
        raw = payload
    raw = "".join(raw.split())
    if not raw:
        raise ToolError(f"第 {index} 张 base64 参考图为空")
    try:
        # 允许调用方省略结尾的 padding；validate=True 仍会拒绝非法字符。
        data = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolError(f"第 {index} 张 base64 参考图解码失败：{exc}") from exc
    if not data:
        raise ToolError(f"第 {index} 张 base64 参考图为空")
    _check_reference_size(data, f"base64 第 {index} 张")
    return data


def _load_reference_images(
    paths: list[str] | None, encoded: list[str] | None
) -> list[bytes]:
    path_values = list(paths or [])
    encoded_values = list(encoded or [])
    total = len(path_values) + len(encoded_values)
    if total == 0:
        return []
    if total > MAX_REFERENCE_IMAGES:
        raise ToolError(
            f"参考图最多 {MAX_REFERENCE_IMAGES} 张，当前 {total} 张"
        )
    images = [
        _read_reference_file(Path(value).expanduser()) for value in path_values
    ]
    images.extend(
        _decode_reference_base64(value, index)
        for index, value in enumerate(encoded_values, start=1)
    )
    return images


@mcp.tool(name="generate_image", description=TOOL_DESCRIPTION)
async def generate_image(
    prompt: str,
    size: str = "1024x1024",
    n: int = 1,
    reference_image_paths: list[str] | None = None,
    reference_images_base64: list[str] | None = None,
) -> list[str]:
    """生成图片并返回 data/images/ 下的绝对路径列表。

    提供参考图时走图生图；路径和 base64 可以同时使用。
    """
    runtime = _build_runtime()

    if runtime.store.is_empty():
        raise ToolError("未找到登录凭证，请先运行 `doubao2-api login` 扫码登录。")
    if runtime.store.is_expired():
        raise ToolError("登录态已过期，请运行 `doubao2-api login` 重新扫码登录。")

    reference_images = _load_reference_images(
        reference_image_paths, reference_images_base64
    )

    try:
        urls = await runtime.client.generate(
            prompt, size, n, reference_images=reference_images or None
        )
        stored = [await runtime.images.fetch_and_store(url) for url in urls]
    except AuthExpired as exc:
        raise ToolError("登录态已过期，请运行 `doubao2-api login` 重新扫码登录。") from exc
    except RateLimited as exc:
        raise _rate_limited_error(exc) from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            raise _rate_limited_error(exc) from exc
        raise ToolError(f"生图失败：{_summarize(exc)}") from exc
    except Exception as exc:
        raise ToolError(f"生图失败：{_summarize(exc)}") from exc

    return [str(item.path.resolve()) for item in stored]


def run_server() -> None:
    """启动 stdio server（阻塞）。stdout 只能输出 JSON-RPC。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    logger.info("启动 MCP stdio server（stdout 仅用于 JSON-RPC）")
    mcp.run(transport="stdio")
