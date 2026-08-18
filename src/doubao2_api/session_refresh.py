from __future__ import annotations

import time

from playwright.sync_api import sync_playwright

from .credentials import CredentialStore

LOGIN_URL = "https://www.doubao.com/chat/"

# 【笔记 §登录态标记】按笔记填写登录成功后出现的关键 Cookie 名
SESSION_COOKIE_NAMES = {"sessionid"}


def extract_login_cookies(cookies: list[dict]) -> dict[str, str] | None:
    names = {c["name"] for c in cookies}
    if SESSION_COOKIE_NAMES & names:
        return {c["name"]: c["value"] for c in cookies}
    return None


def refresh_credentials(
    store: CredentialStore, timeout: float = 180.0, headless: bool = False
) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(LOGIN_URL)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                found = extract_login_cookies(context.cookies())
                if found is not None:
                    store.save(found)
                    return
                page.wait_for_timeout(2000)
            raise TimeoutError(f"扫码登录等待超时（{timeout}s）")
        finally:
            browser.close()
