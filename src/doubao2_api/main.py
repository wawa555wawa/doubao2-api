from __future__ import annotations

import asyncio
import time
from typing import Literal

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings, get_settings
from .credentials import CredentialStore
from .doubao_client import (
    AuthExpired,
    DoubaoClient,
    GenerationFailed,
    GenerationTimeout,
    RateLimited,
)
from .images import ImageDownloadFailed, ImageStore, StoredImage
from .session_refresh import refresh_credentials


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


def create_app(
    settings: Settings | None = None,
    doubao: DoubaoClient | None = None,
    images: ImageStore | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    store = CredentialStore(settings.data_dir / "credentials.json")
    doubao = doubao or DoubaoClient(store, timeout=settings.generation_timeout)
    images = images or ImageStore(settings.data_dir / "images")
    semaphore = asyncio.Semaphore(settings.max_concurrent)

    app = FastAPI(title="doubao2-api")
    app.state.settings = settings
    app.state.store = store
    app.state.doubao = doubao
    app.state.images = images

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
            "登录态过期，自动续期超时，请重试或运行 `doubao2-api login`",
            "auth_expired",
        )

    async def run_generation(
        prompt: str, size: str, n: int, reference_images: list[bytes] | None
    ) -> list[StoredImage]:
        async with semaphore:
            try:
                urls = await doubao.generate(prompt, size, n, reference_images)
            except AuthExpired:
                try:
                    await run_in_threadpool(
                        refresh_credentials,
                        store,
                        settings.login_timeout,
                        settings.headless,
                    )
                except TimeoutError:
                    raise _AuthRefreshFailed()
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
