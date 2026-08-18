from __future__ import annotations

import json
import time
from pathlib import Path


class CredentialStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._cookies: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._cookies = data.get("cookies", {})

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    def is_empty(self) -> bool:
        return not self._cookies

    def save(self, cookies: dict[str, str]) -> None:
        self._cookies = dict(cookies)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cookies": self._cookies, "saved_at": time.time()}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
