"""保活（keepalive）相关测试：用假 BrowserSession，不启浏览器、不联网。"""

import time

from _auth_fixtures import (
    FakePage,
    FakeResponse,
    cookies_to_dict,
    make_login_cookies,
)

from doubao2_api.credentials import CredentialStore
from doubao2_api.keepalive import (
    KeepaliveResult,
    run_keepalive_cycles,
    visit_once,
)

CHAT_URL = "https://www.doubao.com/chat/"
DEVICE_URL = "https://www.doubao.com/chat/completion?device_id=D1&web_id=W1&tea_uuid=T1"


class FakeSession:
    """替代 BrowserSession：按脚本返回登录态，记录访问次数。"""

    def __init__(self, script, device_url=DEVICE_URL, saved_at_profile=False):
        self._script = list(script)
        self._device_url = device_url
        self.visits = 0
        self.page = FakePage()
        self.saved_at_profile = saved_at_profile
        self.entered = 0
        self.exited = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self.exited += 1
        return None

    def open_page(self, url, prepare=None):
        """对齐真实现：先挂监听（prepare），再 goto 并在导航期间触发响应。"""
        self.visits += 1
        prepared = prepare(self.page) if prepare is not None else None
        self.page.goto(url)
        if self._device_url:
            self.page.emit(FakeResponse(self._device_url))
        return self.page, prepared

    def cookies(self):
        return self._script.pop(0) if self._script else []


def make_store(tmp_path, issued_at=None, device=None):
    store = CredentialStore(tmp_path / "credentials.json")
    if issued_at is not None:
        store.save(
            cookies_to_dict(make_login_cookies(issued_at)),
            device=device or {"device_id": "OLD", "web_id": "OLD", "tea_uuid": "OLD"},
        )
    return store


# ---- visit_once：续期成功 / 没续期 / 已过期 ----


def test_visit_saves_credentials_when_server_renews(tmp_path):
    issued = int(time.time()) - 86400 * 10  # 10 天前签发，还剩 20 天
    store = make_store(tmp_path, issued)
    session = FakeSession([make_login_cookies(time.time())])  # 服务端重新签发

    result = visit_once(session, store, chat_url=CHAT_URL, page_wait_ms=0)

    assert result.renewed is True
    assert result.expires_at > issued + 2592000
    assert store.session_expires_at == result.expires_at
    assert store.cookies["sessionid"] == "SANITIZED-SESSION"
    # 设备参数随页面响应一起刷新
    assert store.device == {"device_id": "D1", "web_id": "W1", "tea_uuid": "T1"}


def test_visit_keeps_old_credentials_when_server_does_not_renew(tmp_path):
    issued = int(time.time()) - 86400 * 10
    store = make_store(tmp_path, issued)
    before = store.cookies
    session = FakeSession([make_login_cookies(issued)])  # 还是同一份登录态

    result = visit_once(session, store, chat_url=CHAT_URL, page_wait_ms=0)

    assert result.renewed is False
    assert result.expires_at == issued + 2592000
    assert store.cookies == before  # 不覆盖
    assert store.saved_at is not None
    assert session.page.waits == []  # page_wait_ms=0 时不额外等待


def test_visit_reports_no_login_when_cookies_missing(tmp_path):
    store = make_store(tmp_path)  # 没有凭证
    session = FakeSession([[]])

    result = visit_once(session, store, chat_url=CHAT_URL, page_wait_ms=0)

    assert result.renewed is False
    assert result.expires_at is None
    assert store.is_empty()


def test_visit_never_accepts_shorter_session(tmp_path):
    """服务端若返回更早到期的会话（异常/降级），不能把凭证改差。"""
    issued = int(time.time()) - 86400 * 10
    store = make_store(tmp_path, issued)
    worse = make_login_cookies(issued - 86400 * 5)  # 比现有更早到期
    session = FakeSession([worse])

    result = visit_once(session, store, chat_url=CHAT_URL, page_wait_ms=0)

    assert result.renewed is False
    assert store.session_expires_at == issued + 2592000


def test_visit_visits_configured_url(tmp_path):
    store = make_store(tmp_path, time.time() - 86400)
    session = FakeSession([make_login_cookies(time.time())])

    visit_once(session, store, chat_url=CHAT_URL, page_wait_ms=1500)

    assert session.visits == 1
    assert session.page.url == CHAT_URL
    assert 1500 in session.page.waits


# ---- run_keepalive_cycles：周期调度 ----


def test_keepalive_cycles_stop_on_event(tmp_path):
    import threading

    store = make_store(tmp_path, time.time() - 86400)
    session = FakeSession([make_login_cookies(time.time())] * 5)
    stop = threading.Event()
    seen: list[KeepaliveResult] = []

    def on_result(result):
        seen.append(result)
        stop.set()  # 第一轮结束就停

    run_keepalive_cycles(
        lambda: session,
        store,
        interval=0.01,
        stop=stop,
        chat_url=CHAT_URL,
        page_wait_ms=0,
        on_result=on_result,
    )

    assert len(seen) == 1
    assert seen[0].renewed is True
    assert session.entered == 1 and session.exited == 1


def test_keepalive_cycles_survive_failures(tmp_path):
    """单轮失败（浏览器起不来/被锁）不能让保活线程退出。"""
    import threading

    store = make_store(tmp_path, time.time() - 86400)
    attempts = []
    stop = threading.Event()

    class BoomSession(FakeSession):
        def __enter__(self):
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("launch failed: profile locked")
            return self

    session = BoomSession([make_login_cookies(time.time())] * 5)

    def on_result(result):
        stop.set()

    run_keepalive_cycles(
        lambda: session,
        store,
        interval=0.01,
        stop=stop,
        chat_url=CHAT_URL,
        page_wait_ms=0,
        on_result=on_result,
    )

    assert len(attempts) == 3  # 前两轮抛错被吞掉，第三轮成功
    assert store.session_expires_at is not None


def test_visit_extracts_device_from_any_response(tmp_path):
    """device 参数可能只在某个子请求 URL 上出现。"""
    store = make_store(tmp_path, time.time() - 86400)
    session = FakeSession(
        [make_login_cookies(time.time())],
        device_url="https://www.doubao.com/im/chain/single?web_id=W9&tea_uuid=T9&device_id=D9",
    )

    visit_once(session, store, chat_url=CHAT_URL, page_wait_ms=0)

    assert store.device == {"device_id": "D9", "web_id": "W9", "tea_uuid": "T9"}


def test_keepalive_cycles_gives_up_after_consecutive_failures(tmp_path):
    """持续失败必须退出循环，不能变成 CPU 打满的死循环。"""
    import threading

    store = make_store(tmp_path, time.time() - 86400)
    attempts = []

    class BoomSession(FakeSession):
        def __enter__(self):
            attempts.append(1)
            raise RuntimeError("launch failed: profile locked")

    session = BoomSession([])
    stop = threading.Event()

    run_keepalive_cycles(
        lambda: session,
        store,
        interval=0.01,
        stop=stop,
        chat_url=CHAT_URL,
        page_wait_ms=0,
        max_consecutive_failures=3,
    )

    assert len(attempts) == 3
    assert stop.is_set() is False  # 自己退出，不是被 stop 唤醒


def test_keepalive_cycles_reports_each_result(tmp_path):
    import threading

    store = make_store(tmp_path, time.time() - 86400)
    issued = int(time.time()) - 86400 * 10
    # 第一轮不续期，第二轮续期
    session = FakeSession([make_login_cookies(issued), make_login_cookies(time.time())])
    stop = threading.Event()
    seen: list[KeepaliveResult] = []

    def on_result(result):
        seen.append(result)
        if len(seen) >= 2:
            stop.set()

    run_keepalive_cycles(
        lambda: session,
        store,
        interval=0.01,
        stop=stop,
        chat_url=CHAT_URL,
        page_wait_ms=0,
        on_result=on_result,
    )

    assert [r.renewed for r in seen] == [False, True]
