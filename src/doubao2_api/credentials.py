from __future__ import annotations

import json
import time
from pathlib import Path


class CredentialStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._cookies: dict[str, str] = {}
        self._device: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._cookies = data.get("cookies", {})
            self._device = data.get("device", {})

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    @property
    def device(self) -> dict[str, str]:
        """设备标识（device_id/web_id/tea_uuid），重放请求需要携带以通过风控。"""
        return dict(self._device)

    def is_empty(self) -> bool:
        return not self._cookies

    def save(self, cookies: dict[str, str], device: dict[str, str] | None = None) -> None:
        self._cookies = dict(cookies)
        if device is not None:
            self._device = dict(device)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cookies": self._cookies,
            "device": self._device,
            "saved_at": time.time(),
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
