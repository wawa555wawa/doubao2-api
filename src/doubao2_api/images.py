from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx


class ImageDownloadFailed(Exception):
    """图片下载失败（已重试一次）。"""


@dataclass
class StoredImage:
    id: str
    path: Path
    data: bytes

    def b64_json(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


_EXT_BY_CONTENT_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


class ImageStore:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    async def fetch_and_store(self, url: str) -> StoredImage:
        last_error: Exception | None = None
        for _ in range(2):  # 失败重试一次
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=60
                ) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "").split(";")[0]
                ext = _EXT_BY_CONTENT_TYPE.get(content_type, ".png")
                image_id = uuid.uuid4().hex
                path = self._dir / f"{image_id}{ext}"
                path.write_bytes(resp.content)
                return StoredImage(id=image_id, path=path, data=resp.content)
            except httpx.HTTPError as exc:
                last_error = exc
        raise ImageDownloadFailed(f"下载图片失败: {url}: {last_error}")
