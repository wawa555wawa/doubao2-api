from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

from .credentials import CredentialStore

LOGIN_URL = "https://www.doubao.com/chat/"

# 【笔记 §登录态标记】按笔记填写登录成功后出现的关键 Cookie 名
SESSION_COOKIE_NAMES = {"sessionid"}

# 重放请求需要通过风控的设备标识参数（见笔记 §运行时注意事项）
DEVICE_PARAM_NAMES = ("device_id", "web_id", "tea_uuid")


def extract_login_cookies(cookies: list[dict]) -> dict[str, str] | None:
    names = {c["name"] for c in cookies}
    if SESSION_COOKIE_NAMES & names:
        return {c["name"]: c["value"] for c in cookies}
    return None


def extract_device_params(url: str) -> dict[str, str]:
    """从页面发出的请求 URL 中提取设备标识参数（不含则返回空 dict）。"""
    query = parse_qs(urlparse(url).query)
    return {k: query[k][0] for k in DEVICE_PARAM_NAMES if query.get(k)}


def refresh_credentials(
    store: CredentialStore, timeout: float = 180.0, headless: bool = False
) -> None:
    device: dict[str, str] = {}

    def on_response(response) -> None:
        nonlocal device
        if "doubao.com" not in response.url:
            return
        for key, value in extract_device_params(response.url).items():
            device.setdefault(key, value)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.on("response", on_response)
            page.goto(LOGIN_URL)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                found = extract_login_cookies(context.cookies())
                if found is not None:
                    # 登录成功后页面还会继续发带 device_id 的请求，多等几秒收集；
                    # 兜底：device_id 也存在于 localStorage 的 samantha_web_web_id。
                    page.wait_for_timeout(5000)
                    if not device.get("device_id"):
                        try:
                            raw = page.evaluate(
                                "localStorage.getItem('samantha_web_web_id')"
                            )
                            fallback = (json.loads(raw or "{}") or {}).get("web_id")
                            if fallback:
                                device["device_id"] = str(fallback)
                        except Exception:
                            pass
                    store.save(found, device=device or None)
                    return
                page.wait_for_timeout(2000)
            raise TimeoutError(f"扫码登录等待超时（{timeout}s）")
        finally:
            browser.close()
