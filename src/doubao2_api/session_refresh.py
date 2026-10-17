from __future__ import annotations

import json
import time
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

from .credentials import SESSION_COOKIE_NAME, CredentialStore, parse_sid_guard

LOGIN_URL = "https://www.doubao.com/chat/"

# 【笔记 §登录态标记】登录成功后出现的关键 Cookie 名
SESSION_COOKIE_NAMES = {SESSION_COOKIE_NAME}

# 重放请求需要通过风控的设备标识参数（见笔记 §运行时注意事项）
DEVICE_PARAM_NAMES = ("device_id", "web_id", "tea_uuid")

# 扫码后新会话至少还应有这么久的有效期，否则说明拿到的还是旧会话
MIN_FRESH_SESSION_TTL = 3600.0

ErrorFactory = Callable[[str], Exception]


class SessionRefreshError(RuntimeError):
    """扫码续期没拿到可用凭证（等待超时，或只看到失效的旧登录态）。"""


def extract_login_cookies(cookies: list[dict]) -> dict[str, str] | None:
    names = {c["name"] for c in cookies}
    if SESSION_COOKIE_NAMES & names:
        return {c["name"]: c["value"] for c in cookies}
    return None


def extract_device_params(url: str) -> dict[str, str]:
    """从页面发出的请求 URL 中提取设备标识参数（不含则返回空 dict）。"""
    query = parse_qs(urlparse(url).query)
    return {k: query[k][0] for k in DEVICE_PARAM_NAMES if query.get(k)}


def is_fresh_session(
    expires_at: float | None,
    issue_time: float | None,
    now: float,
    previous_expires_at: float | None = None,
    min_ttl: float = MIN_FRESH_SESSION_TTL,
) -> bool:
    """判断这份登录态是不是"刚刚签发"的，而不是 context/凭证里残留的旧会话。

    只看 `sessionid` 存在是不够的：旧 `sessionid` 同样存在，只要它出现在轮询的
    context 里就会被当成"登录成功"保存。因此以 `sid_guard` 的签发时间为准：
    到期时间必须晚于旧凭证，且距当前至少还剩 `min_ttl` 的有效期。
    （实测默认 `browser.new_context()` 是空 context，不会自动带上磁盘 Cookie；
    这条判定是防回归的硬约束 —— 一旦有人改成持久化 profile / 传 storage_state，
    旧会话就会被立刻误判成登录成功。）
    """
    if expires_at is None or issue_time is None:
        return False
    if previous_expires_at is not None and expires_at <= previous_expires_at:
        return False
    return (expires_at - now) >= min_ttl


def wait_for_fresh_session(
    poll_cookies: Callable[[], list[dict]],
    sleep: Callable[[float], None],
    timeout: float,
    interval: float = 2.0,
    previous_expires_at: float | None = None,
    now: Callable[[], float] = time.time,
    error_factory: ErrorFactory = SessionRefreshError,
) -> dict[str, str]:
    """轮询到"新签发的登录态"才返回 Cookie；否则抛 error_factory 的异常。"""
    deadline = now() + timeout
    saw_sessionid = False
    stale_note = ""
    while True:
        found = extract_login_cookies(poll_cookies())
        if found is not None:
            saw_sessionid = True
            guard = parse_sid_guard(found.get("sid_guard"))
            if guard is None:
                stale_note = (
                    "只看到 sessionid、取不到 sid_guard，无法确认是本次新签发的登录态"
                )
            else:
                expires_at = float(guard[1] + guard[2])
                if is_fresh_session(
                    expires_at, float(guard[1]), now(), previous_expires_at
                ):
                    return found
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at))
                stale_note = f"检测到的仍是旧登录态（sid_guard 未更新，到期时间 {when}）"
        if now() >= deadline:
            detail = stale_note or "页面未出现登录态"
            raise error_factory(f"扫码登录等待超时（{timeout:g}s）：{detail}")
        sleep(interval)


def refresh_credentials(
    store: CredentialStore, timeout: float = 180.0, headless: bool = False
) -> tuple[dict[str, str], float]:
    """弹浏览器扫码，确认拿到**新签发**的凭证后落盘。

    判定用 `sid_guard` 的签发时间（见 `is_fresh_session`）：只有"本次扫码新签发"
    的登录态才会被保存，避免把旧会话或半截状态当成登录成功。
    显式以空白 context 启动，确保 context 里不会出现磁盘上的旧 Cookie。
    返回 (cookies, 到期时间戳)。
    """
    device: dict[str, str] = {}
    previous_expires_at = store.session_expires_at

    def on_response(response) -> None:
        nonlocal device
        if "doubao.com" not in response.url:
            return
        for key, value in extract_device_params(response.url).items():
            device.setdefault(key, value)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(storage_state={"cookies": [], "origins": []})
            page = context.new_page()
            page.on("response", on_response)
            page.goto(LOGIN_URL)
            found = wait_for_fresh_session(
                context.cookies,
                sleep=page.wait_for_timeout,
                timeout=timeout,
                previous_expires_at=previous_expires_at,
            )
            # 登录成功后页面还会继续发带 device_id 的请求，多等几秒收集；
            # 兜底：device_id 也存在于 localStorage 的 samantha_web_web_id。
            page.wait_for_timeout(5000)
            if not device.get("device_id"):
                try:
                    raw = page.evaluate("localStorage.getItem('samantha_web_web_id')")
                    fallback = (json.loads(raw or "{}") or {}).get("web_id")
                    if fallback:
                        device["device_id"] = str(fallback)
                except Exception:
                    pass
            store.save(found, device=device or None)
            return found, store.session_expires_at or 0.0
        finally:
            browser.close()
