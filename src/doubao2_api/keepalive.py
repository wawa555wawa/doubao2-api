"""登录态保活：用持久化浏览器 profile 定时访问豆包页面，让登录态自动续期。

背景：豆包 `sid_guard` 声明 30 天硬过期（见 notes §登录态标记）。服务端是否会因为
"合法登录态访问页面"而重新签发（滚动续期）**未经验证**，因此这里不假设它一定会续，
而是每次访问后比对签发时间：

- 新到期时间晚于旧凭证 → 说明拿到了新签发的登录态，落盘（顺带刷新 device 参数）；
- 没变 → 只记录一次访问，不动凭证（不能让保活把凭证改差）；
- profile 里没有合法登录态 → 返回"未登录"，服务照旧返回 401，等人工扫码。

浏览器与 Playwright 的同步 API 都要求同线程调用，所以保活在独立线程里跑
（见 `run_keepalive_cycles` 的调用方 `main._keepalive_loop`）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .credentials import CredentialStore, parse_sid_guard
from .session_refresh import (
    LOGIN_URL,
    extract_device_params,
    extract_login_cookies,
    is_fresh_session,
    wait_for_fresh_session,
)

logger = logging.getLogger("doubao2_api")

DEFAULT_KEEPALIVE_INTERVAL = 6 * 3600.0
DEFAULT_VISIT_WAIT_MS = 10000.0


@dataclass(frozen=True)
class KeepaliveResult:
    """一次保活访问的结果。"""

    visited_at: float
    renewed: bool
    expires_at: float | None
    detail: str


class BrowserSession:
    """持久化 profile 的浏览器会话。

    用 `launch_persistent_context` 直接打开 profile 目录：profile 里已登录时
    打开页面即可拿 Cookie；未登录时会停在扫码页，人工扫码后同样能被取到。
    """

    def __init__(
        self,
        profile_dir: Path,
        headless: bool = False,
        channel: str | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.channel = channel
        self._playwright = None
        self._context = None

    @property
    def profile_exists(self) -> bool:
        return self.profile_dir.exists()

    def __enter__(self) -> "BrowserSession":
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        from playwright.sync_api import sync_playwright

        kwargs: dict = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            # 让我们自己负责关闭；异常路径由 __exit__/finally 兜底
            "args": ["--no-first-run", "--no-default-browser-check"],
        }
        if self.channel:
            kwargs["channel"] = self.channel
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(**kwargs)
        except BaseException:
            self._close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self._close()

    def _close(self) -> None:
        context, self._context = self._context, None
        if context is not None:
            try:
                context.close()
            except Exception as exc:  # 关闭失败不该掩盖真正的错误
                logger.debug("关闭浏览器 context 失败: %s", exc)
        playwright, self._playwright = self._playwright, None
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as exc:
                logger.debug("停止 playwright 失败: %s", exc)

    def open_page(self, url: str, prepare=None):
        """新建/复用页面 → 先 prepare（挂监听）→ 再 goto，避免漏掉导航期间的请求。

        prepare 的返回值会与 page 一起返回，方便把"活引用"（如持续填充的
        device 参数字典）带到访问结束。
        """
        if self._context is None:
            raise RuntimeError("BrowserSession 必须先进入 with 语句")
        page = self._context.pages[0] if self._context.pages else self._context.new_page()
        prepared = prepare(page) if prepare is not None else None
        page.goto(url)
        return page, prepared

    def cookies(self) -> list[dict]:
        if self._context is None:
            raise RuntimeError("BrowserSession 必须先进入 with 语句")
        return self._context.cookies()


def collect_device_params(page) -> dict[str, str]:
    """把页面上任意豆包请求 URL 里的 device 参数收集起来（保留首次出现的值）。

    返回的 dict 由 response 回调持续填充，调用方随时读取即可。
    注意：这里刻意只挂监听、不摘除 —— 摘掉会连带删掉页面自己的监听器
    （豆包页面自己可能也在用），属于典型的搬起石头砸自己脚。
    """
    device: dict[str, str] = {}

    def on_response(response) -> None:
        if "doubao.com" not in response.url:
            return
        for key, value in extract_device_params(response.url).items():
            device.setdefault(key, value)

    page.on("response", on_response)
    return device


def visit_once(
    session: BrowserSession,
    store: CredentialStore,
    chat_url: str = LOGIN_URL,
    page_wait_ms: float = DEFAULT_VISIT_WAIT_MS,
) -> KeepaliveResult:
    """打开一次豆包页面，若服务端重新签发了登录态就落盘。"""
    previous_expires_at = store.session_expires_at

    with session as browser:
        # 监听必须在 goto 之前挂上（否则漏掉导航期间的请求）；返回的 dict 是
        # "活引用"，回调会在整个访问期间持续往里补 device 参数。
        page, device = browser.open_page(
            chat_url, prepare=lambda p: collect_device_params(p)
        )
        device = device or {}
        if page_wait_ms:
            page.wait_for_timeout(page_wait_ms)
        found = extract_login_cookies(browser.cookies())

    if found is None:
        return KeepaliveResult(
            visited_at=time.time(),
            renewed=False,
            expires_at=None,
            detail="profile 里没有登录态（需要扫码登录一次）",
        )

    guard = parse_sid_guard(found.get("sid_guard"))
    if guard is None:
        return KeepaliveResult(
            visited_at=time.time(),
            renewed=False,
            expires_at=None,
            detail="拿到 sessionid 但解析不出 sid_guard，未落盘",
        )
    expires_at = float(guard[1] + guard[2])
    now = time.time()

    if is_fresh_session(
        expires_at=expires_at,
        issue_time=float(guard[1]),
        now=now,
        previous_expires_at=previous_expires_at,
        min_ttl=0.0,  # 续期只看"是否变新"，不再要求剩余时长
    ):
        store.save(found, device=device or None)
        return KeepaliveResult(
            visited_at=now,
            renewed=True,
            expires_at=expires_at,
            detail="登录态已续期并落盘",
        )

    remaining = expires_at - now
    if remaining <= 0:
        detail = "登录态已过期且服务端没有重新签发（需要重新扫码）"
    else:
        detail = "服务端没有重新签发，沿用现有登录态"
    return KeepaliveResult(
        visited_at=now, renewed=False, expires_at=expires_at, detail=detail
    )


def _interruptible_wait(stop: threading.Event, seconds: float) -> None:
    """把长等待切碎，保证 stop 置位后在 1 秒内返回（否则关闭要等满一个 interval）。"""
    deadline = time.monotonic() + seconds
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        stop.wait(min(1.0, remaining))


def run_keepalive_cycles(
    session_factory: Callable[[], BrowserSession],
    store: CredentialStore,
    interval: float,
    stop: threading.Event,
    chat_url: str = LOGIN_URL,
    page_wait_ms: float = DEFAULT_VISIT_WAIT_MS,
    on_result: Callable[[KeepaliveResult], None] | None = None,
    max_consecutive_failures: int = 5,
) -> None:
    """按 interval 反复保活，直到 stop 被置位。

    单轮失败只记日志并退避重试；连续失败到上限就退出循环 —— 否则配置错/依赖缺失
    时会变成 CPU 打满的死循环（浏览器启动失败是毫秒级返回，不是等 interval）。
    """
    failures = 0
    while not stop.is_set():
        try:
            session = session_factory()
            result = visit_once(
                session, store, chat_url=chat_url, page_wait_ms=page_wait_ms
            )
        except Exception as exc:
            failures += 1
            logger.warning(
                "登录态保活失败（第 %d/%d 次，%s 后重试）: %s",
                failures,
                max_consecutive_failures,
                interval,
                exc,
            )
            if failures >= max_consecutive_failures:
                logger.error("登录态保活连续失败 %d 次，停止保活", failures)
                return
            _interruptible_wait(stop, interval)
            continue
        failures = 0
        level = logging.INFO if result.renewed else logging.DEBUG
        logger.log(level, "登录态保活：%s", result.detail)
        if on_result is not None:
            on_result(result)
        _interruptible_wait(stop, interval)


def env_display_available() -> bool:
    """有头模式需要可用的显示服务；无头模式不需要。"""
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def login_with_profile(
    store: CredentialStore,
    profile_dir: Path,
    timeout: float = 180.0,
    headless: bool = False,
    channel: str | None = None,
    chat_url: str = LOGIN_URL,
) -> tuple[dict[str, str], float]:
    """在持久化 profile 里完成登录（profile 已登录时直接取用，无需扫码）。

    与保活的区别：这里会一直等到"新签发的登录态"（等待上限 timeout），
    所以 profile 未登录时人工扫码即可，扫完凭证同时落盘。
    """
    with BrowserSession(profile_dir, headless=headless, channel=channel) as browser:
        page, device = browser.open_page(
            chat_url, prepare=lambda p: collect_device_params(p)
        )
        device = device or {}
        found = wait_for_fresh_session(
            browser.cookies,
            sleep=page.wait_for_timeout,
            timeout=timeout,
            previous_expires_at=store.session_expires_at,
        )
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
