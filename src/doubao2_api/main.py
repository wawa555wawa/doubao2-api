from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings, get_settings
from .credentials import CredentialStore, format_expiry
from .doubao_client import (
    AuthExpired,
    DoubaoClient,
    GenerationFailed,
    GenerationTimeout,
    RateLimited,
)
from .images import ImageDownloadFailed, ImageStore, StoredImage
from .keepalive import (
    BrowserSession,
    env_display_available,
    run_keepalive_cycles,
)
from .session_refresh import (
    LOGIN_URL,
    SessionRefreshError,
    refresh_credentials,
)

logger = logging.getLogger("doubao2_api")


class _AuthRefreshFailed(Exception):
    pass


class GenerationRequest(BaseModel):
    prompt: str
    size: str = "1024x1024"
    n: int = 1
    response_format: Literal["b64_json", "url"] = "b64_json"


def _error(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": "server_error" if status >= 500 else "invalid_request_error",
                "code": code,
            }
        },
    )


def log_credentials_health(store: CredentialStore, warn_days: float) -> None:
    """启动体检：登录态有效期由 `sid_guard` 给出，提前告警而不是等请求 401。"""
    if store.is_empty():
        logger.warning(
            "未找到登录凭证（%s），请求会返回 401；先运行 `doubao2-api login` 扫码",
            store.path,
        )
        return

    expires_at = store.session_expires_at
    remaining = store.session_time_remaining()
    if remaining is not None and remaining <= 0:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at))
        logger.error(
            "登录态已于 %s 过期（%.1f 天前），请求会返回 401；"
            "请运行 `doubao2-api login` 重新扫码",
            when,
            -remaining / 86400,
        )
    elif remaining is not None and remaining <= warn_days * 86400:
        logger.warning(
            "登录态即将过期：%s，到期后需重新扫码（`doubao2-api login`）",
            format_expiry(expires_at),
        )
    else:
        logger.info("登录态有效期：%s", format_expiry(expires_at))


def create_app(
    settings: Settings | None = None,
    doubao: DoubaoClient | None = None,
    images: ImageStore | None = None,
    store: CredentialStore | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    store = store or CredentialStore(settings.data_dir / "credentials.json")
    doubao = doubao or DoubaoClient(store, timeout=settings.generation_timeout)
    images = images or ImageStore(settings.data_dir / "images")
    semaphore = asyncio.Semaphore(settings.max_concurrent)

    keepalive_stop = threading.Event()
    keepalive_thread: threading.Thread | None = None

    def log_keepalive_result(result) -> None:
        if result.renewed:
            logger.info(
                "保活成功：登录态已续期，新到期时间 %s", format_expiry(result.expires_at)
            )
        else:
            logger.warning("保活未续期：%s", result.detail)

    def start_keepalive() -> threading.Thread:
        """保活用守护线程跑：同步 Playwright 必须独占线程，且不能拖住服务退出。

        不用 run_in_threadpool：asyncio 取消不会中断已提交的线程任务，若正卡在
        一个 6 小时 interval 上，uvicorn 关闭时会一直等线程池。
        """
        logger.info(
            "登录态保活已启用：每 %.1f 小时访问一次 %s（profile=%s，headless=%s）",
            settings.keepalive_interval / 3600,
            LOGIN_URL,
            settings.resolved_profile_dir,
            settings.headless,
        )
        if not settings.headless and not env_display_available():
            logger.warning(
                "没有可用的显示服务（DISPLAY/WAYLAND_DISPLAY 均未设置），"
                "保活会因浏览器启动失败而停止；请改用 HEADLESS=true"
            )

        def session_factory() -> BrowserSession:
            return BrowserSession(
                settings.resolved_profile_dir,
                headless=settings.headless,
                channel=settings.browser_channel,
            )

        thread = threading.Thread(
            target=run_keepalive_cycles,
            args=(session_factory, store, settings.keepalive_interval, keepalive_stop),
            kwargs={
                "chat_url": LOGIN_URL,
                "page_wait_ms": settings.keepalive_page_wait_ms,
                "on_result": log_keepalive_result,
            },
            name="doubao2-api-keepalive",
            daemon=True,
        )
        thread.start()
        return thread

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal keepalive_thread
        log_credentials_health(store, settings.session_warn_days)
        if settings.keepalive_enabled:
            keepalive_thread = start_keepalive()
        try:
            yield
        finally:
            if keepalive_thread is not None:
                keepalive_stop.set()
                keepalive_thread.join(timeout=2.0)

    app = FastAPI(title="doubao2-api", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.doubao = doubao
    app.state.images = images
    app.state.keepalive_stop = keepalive_stop

    images_dir = settings.data_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/images", StaticFiles(directory=images_dir), name="images")

    @app.exception_handler(GenerationFailed)
    async def _(request: Request, exc: GenerationFailed):
        return _error(400, str(exc), "generation_failed")

    @app.exception_handler(RateLimited)
    async def _(request: Request, exc: RateLimited):
        return _error(429, str(exc), "rate_limited")

    @app.exception_handler(GenerationTimeout)
    async def _(request: Request, exc: GenerationTimeout):
        return _error(504, str(exc), "generation_timeout")

    @app.exception_handler(ImageDownloadFailed)
    async def _(request: Request, exc: ImageDownloadFailed):
        return _error(502, str(exc), "image_download_failed")

    @app.exception_handler(_AuthRefreshFailed)
    async def _(request: Request, exc: _AuthRefreshFailed):
        return _error(
            401,
            f"登录态过期，自动续期未拿到新凭证（{exc}），"
            "请运行 `doubao2-api login` 重新扫码",
            "auth_expired",
        )

    @app.exception_handler(AuthExpired)
    async def _(request: Request, exc: AuthExpired):
        return _error(
            401,
            "登录态过期且自动续期后仍失败，请运行 `doubao2-api login` 重新登录",
            "auth_expired",
        )

    async def refresh_or_fail() -> None:
        try:
            await run_in_threadpool(
                refresh_credentials,
                store,
                settings.login_timeout,
                settings.headless,
            )
        except (TimeoutError, SessionRefreshError) as exc:
            raise _AuthRefreshFailed(str(exc))

    async def run_generation(
        prompt: str, size: str, n: int, reference_images: list[bytes] | None
    ) -> list[StoredImage]:
        async with semaphore:
            if store.is_expired():
                # 本地就能判定过期：先续期再发请求，省掉必然 401 的一轮
                logger.warning(
                    "登录态已过期（%s），先走扫码续期", format_expiry(store.session_expires_at)
                )
                await refresh_or_fail()
            try:
                urls = await doubao.generate(prompt, size, n, reference_images)
            except AuthExpired:
                await refresh_or_fail()
                urls = await doubao.generate(prompt, size, n, reference_images)
        results = []
        for url in urls:
            results.append(await images.fetch_and_store(url))
        return results

    def build_response(
        stored: list[StoredImage], fmt: str, request: Request
    ) -> dict:
        data = []
        for s in stored:
            if fmt == "b64_json":
                data.append({"b64_json": s.b64_json()})
            else:
                data.append(
                    {"url": f"{str(request.base_url).rstrip('/')}/images/{s.path.name}"}
                )
        return {"created": int(time.time()), "data": data}

    @app.post("/v1/images/generations")
    async def generations(req: GenerationRequest, request: Request):
        stored = await run_generation(req.prompt, req.size, req.n, None)
        return build_response(stored, req.response_format, request)

    @app.post("/v1/images/edits")
    async def edits(
        request: Request,
        image: list[UploadFile] = File(...),
        prompt: str = Form(...),
        size: str = Form("1024x1024"),
        n: int = Form(1),
        response_format: Literal["b64_json", "url"] = Form("b64_json"),
    ):
        reference_images = [await f.read() for f in image]
        stored = await run_generation(prompt, size, n, reference_images)
        return build_response(stored, response_format, request)

    return app


app = create_app()
