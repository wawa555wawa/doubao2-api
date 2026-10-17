from __future__ import annotations

import datetime
import json
import time
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

SESSION_COOKIE_NAME = "sessionid"
SESSION_GUARD_COOKIE_NAME = "sid_guard"


def parse_sid_guard(value: str | None) -> tuple[str, int, int] | None:
    """解析 `sid_guard`：`<session_id>|<签发时间戳>|<TTL 秒>|<到期日>`。

    该值在 Cookie 里是 URL 编码的（`%7C` 代替 `|`），可能带引号，都要还原。
    解析失败返回 None —— 凭证可能来自更早的版本或服务端改了格式，
    此时按"有效期未知"处理，绝不能误判成已过期。
    """
    if not value:
        return None
    decoded = urllib.parse.unquote(str(value).strip().strip('"'))
    parts = decoded.split("|")
    if len(parts) < 3:
        return None
    try:
        return parts[0], int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return None


def session_expires_at(cookies: Mapping[str, str]) -> float | None:
    """会话到期时间戳（秒）；解析不出返回 None（有效期未知）。"""
    guard = parse_sid_guard(cookies.get(SESSION_GUARD_COOKIE_NAME))
    if guard is None:
        return None
    _session_id, issued_at, ttl = guard
    if ttl <= 0:
        return None
    return float(issued_at + ttl)


def session_time_remaining(
    cookies: Mapping[str, str], now: float | None = None
) -> float | None:
    """距到期还有多少秒；负数表示已过期，None 表示有效期未知。"""
    expires_at = session_expires_at(cookies)
    if expires_at is None:
        return None
    return expires_at - (time.time() if now is None else now)


def format_expiry(expires_at: float | None, now: float | None = None) -> str:
    """把到期时间格式化成给日志/CLI 看的一句话。"""
    if expires_at is None:
        return "有效期未知（凭证里没有 sid_guard）"
    now = time.time() if now is None else now
    local = datetime.datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M:%S")
    if expires_at <= now:
        return f"{local} 已过期 {(now - expires_at) / 86400:.1f} 天"
    return f"{local}（剩余 {(expires_at - now) / 86400:.1f} 天）"


class CredentialStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._cookies: dict[str, str] = {}
        self._device: dict[str, str] = {}
        self._saved_at: float | None = None
        self.load()

    def load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._cookies = data.get("cookies", {})
            self._device = data.get("device", {})
            saved_at = data.get("saved_at")
            self._saved_at = float(saved_at) if saved_at else None

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    @property
    def device(self) -> dict[str, str]:
        """设备标识（device_id/web_id/tea_uuid），重放请求需要携带以通过风控。"""
        return dict(self._device)

    @property
    def saved_at(self) -> float | None:
        return self._saved_at

    @property
    def session_expires_at(self) -> float | None:
        """登录态到期时间戳；由 `sid_guard` 推导，未知时为 None。"""
        return session_expires_at(self._cookies)

    def session_time_remaining(self, now: float | None = None) -> float | None:
        return session_time_remaining(self._cookies, now)

    def is_expired(self, now: float | None = None) -> bool:
        """是否已过期；有效期未知时返回 False（这种情况交给服务端判定）。"""
        remaining = self.session_time_remaining(now)
        return remaining is not None and remaining <= 0

    def is_empty(self) -> bool:
        return not self._cookies

    def save(self, cookies: dict[str, str], device: dict[str, str] | None = None) -> None:
        self._cookies = dict(cookies)
        if device is not None:
            self._device = dict(device)
        self._saved_at = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cookies": self._cookies,
            "device": self._device,
            "saved_at": self._saved_at,
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
