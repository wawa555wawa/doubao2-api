"""测试共用的假凭证构造器（全部为脱敏占位值，禁止写入真实 Cookie）。"""

from __future__ import annotations

import datetime

DEFAULT_TTL = 2592000  # 豆包 sid_guard 的 TTL：30 天
FAKE_SESSION_ID = "SANITIZED-SESSION"


def make_sid_guard(session_id: str, issued_at: float, ttl: int = DEFAULT_TTL) -> str:
    """按豆包真实格式拼 sid_guard：URL 编码 + `%7C` 分隔的四个字段。"""
    expiry = datetime.datetime.fromtimestamp(
        issued_at + ttl, datetime.timezone.utc
    ).strftime("%a, %d-%b-%Y %H:%M:%S GMT")
    encoded_expiry = expiry.replace(" ", "+").replace(":", "%3A").replace(",", "%2C")
    return f"{session_id}%7C{int(issued_at)}%7C{ttl}%7C{encoded_expiry}"


def make_cookie(name: str, value: str | None = None, **extra) -> dict:
    cookie = {
        "name": name,
        "value": value if value is not None else f"SANITIZED-{name}",
        "domain": ".doubao.com",
        "path": "/",
    }
    cookie.update(extra)
    return cookie


def make_login_cookies(
    issued_at: float, session_id: str = FAKE_SESSION_ID, ttl: int = DEFAULT_TTL
) -> list[dict]:
    """模拟登录成功时 context.cookies() 的返回。"""
    return [
        make_cookie("ttwid"),
        make_cookie("sessionid", session_id, httpOnly=True, secure=True, sameSite="Lax"),
        make_cookie("sid_guard", make_sid_guard(session_id, issued_at, ttl), expires=issued_at + ttl),
    ]


def cookies_to_dict(cookies: list[dict]) -> dict[str, str]:
    return {c["name"]: c["value"] for c in cookies}


class FakeResponse:
    def __init__(self, url: str) -> None:
        self.url = url


class FakePage:
    """替代 Playwright Page：记录等待时长，可按需触发 response 回调。"""

    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self._handlers: list = []
        self._responses = responses or []
        self.waits: list[float] = []

    def on(self, event: str, handler) -> None:
        assert event in ("response", "load")
        self._handlers.append(handler)

    def goto(self, url: str) -> None:
        self.url = url

    def emit(self, response: FakeResponse) -> None:
        for handler in self._handlers:
            handler(response)

    def wait_for_timeout(self, ms: float) -> None:
        self.waits.append(ms)

    def evaluate(self, _script: str) -> None:
        return None


class FakeContext:
    def __init__(self, page: FakePage, script: list[list[dict]]) -> None:
        self._page = page
        self._script = script
        self.calls = 0
        self.new_context_kwargs: dict | None = None

    def cookies(self) -> list[dict]:
        self.calls += 1
        return self._script.pop(0) if self._script else []

    def new_page(self) -> FakePage:
        return self._page

    def clear_cookies(self) -> None:
        pass


class FakeBrowser:
    def __init__(self, page: FakePage, script: list[list[dict]]) -> None:
        self.context = FakeContext(page, script)
        self.closed = False

    def new_context(self, **kwargs) -> FakeContext:
        self.context.new_context_kwargs = kwargs
        return self.context

    def close(self) -> None:
        self.closed = True


class FakePlaywright:
    """替代 sync_playwright() 上下文管理器。"""

    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser

    def __enter__(self) -> "FakePlaywright":
        return self

    def __exit__(self, *exc) -> None:
        return None

    @property
    def chromium(self) -> "_FakeChromium":
        return _FakeChromium(self._browser)


class _FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser

    def launch(self, **_kwargs) -> FakeBrowser:
        return self._browser
