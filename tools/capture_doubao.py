#!/usr/bin/env python3
"""豆包网页版抓包辅助脚本。

用法：
    .venv/bin/python tools/capture_doubao.py

脚本会打开一个 Chromium 窗口（登录态持久化在 .superpowers/browser-profile/，
首次使用需扫码登录一次）。在窗口里正常操作豆包生成图片，页面产生的所有
fetch/XHR 请求与响应会记录到 .superpowers/capture/ 下，每个请求一个 JSON 文件。

操作完毕后回到终端按回车结束抓包。

注意：抓包文件包含 Cookie 等敏感信息，仅保存在本地，不要分享、不要提交 git。
"""

from __future__ import annotations

import base64
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

CAPTURE_DIR = Path(".superpowers/capture")
PROFILE_DIR = Path(".superpowers/browser-profile")
TARGET_URL = "https://www.doubao.com/chat/"

MAX_BODY_CHARS = 500_000


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[-80:]


def main() -> None:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    session_dir = CAPTURE_DIR / time.strftime("%Y%m%d-%H%M%S")
    session_dir.mkdir()
    print(f"抓包文件将保存到: {session_dir}")

    counter = 0

    def next_seq() -> int:
        nonlocal counter
        counter += 1
        return counter

    def write_record(name: str, record: dict) -> None:
        path = session_dir / f"{record['seq']:04d}-{name}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    def on_response(response) -> None:
        request = response.request
        if request.resource_type not in ("xhr", "fetch"):
            return
        seq = next_seq()
        try:
            response_headers = dict(response.headers)
        except Exception:
            response_headers = {}
        is_sse = "text/event-stream" in response_headers.get("content-type", "")
        record = {
            "seq": seq,
            "kind": "http",
            "method": request.method,
            "url": response.url,
            "status": response.status,
            "request_headers": dict(request.headers),
            "request_post_data": request.post_data,
            "response_headers": response_headers,
            "response_body": None,
            "body_error": None,
        }
        name = sanitize(response.url.split("/")[-1].split("?")[0] or "root")
        if is_sse:
            # SSE 流不能在事件回调里同步读（会阻塞事件循环），延迟到结束时统一读
            pending_sse.append((name, record, response))
        else:
            try:
                body = response.json()
            except Exception:
                try:
                    body = response.text()
                except Exception as exc:
                    body = None
                    record["body_error"] = f"{type(exc).__name__}: {exc}"
            if isinstance(body, str) and len(body) > MAX_BODY_CHARS:
                body = body[:MAX_BODY_CHARS] + "...[truncated]"
            record["response_body"] = body
        write_record(name, record)
        print(f"  [{seq:04d}] {request.method} {response.status} {response.url[:120]}")

    def on_websocket(ws) -> None:
        seq = next_seq()
        frames: list[dict] = []
        record = {"seq": seq, "kind": "websocket", "url": ws.url, "frames": frames}
        open_ws.append(record)
        print(f"  [{seq:04d}] WebSocket 连接: {ws.url[:120]}")

        def on_frame(direction: str, payload) -> None:
            if isinstance(payload, bytes):
                data = {"encoding": "base64", "data": base64.b64encode(payload).decode("ascii")}
                preview_len = len(payload)
            else:
                data = {"encoding": "text", "data": payload[:MAX_BODY_CHARS]}
                preview_len = len(payload)
            frames.append({"time": time.time(), "direction": direction, **data})
            print(f"       ws-{direction} {preview_len}B {ws.url[:80]}")

        ws.on("framesent", lambda payload: on_frame("sent", payload))
        ws.on("framereceived", lambda payload: on_frame("received", payload))

    open_ws: list[dict] = []
    pending_sse: list[tuple[str, dict, object]] = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1400, "height": 900},
            # 禁用 Service Worker，迫使页面把 IM WebSocket 建在页面上下文里，才能被捕获
            service_workers="block",
        )
        # 拦截 fetch/XHR 调用参数（上传二进制走的是流式 body，网络事件抓不到，只能从 JS 层钩）
        context.add_init_script(
            """
(() => {
  const log = (rec) => { window.__uploadLog = window.__uploadLog || []; window.__uploadLog.push(rec); };
  const match = (u) => /snssdk|bytedancevod|tos-/.test(u || "");
  const of = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : input.url;
      if (match(url)) {
        const headers = {};
        const h = (init && init.headers) || (typeof input !== "string" ? input.headers : null);
        if (h) new Headers(h).forEach((v, k) => (headers[k] = v));
        const body = init && init.body;
        log({ kind: "fetch", method: (init && init.method) || (typeof input !== "string" ? input.method : "GET"),
              url, headers, bodyType: body ? body.constructor.name : null,
              bodySize: body && body.size != null ? body.size : null });
      }
    } catch (e) {}
    return of.apply(this, arguments);
  };
  const oopen = XMLHttpRequest.prototype.open;
  const oset = XMLHttpRequest.prototype.setRequestHeader;
  const osend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) { this.__rec = { kind: "xhr", method, url }; return oopen.apply(this, arguments); };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) { if (this.__rec) { (this.__rec.headers = this.__rec.headers || {})[k] = v; } return oset.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function (body) {
    if (this.__rec && match(this.__rec.url)) {
      this.__rec.bodyType = body ? body.constructor.name : null;
      this.__rec.bodySize = body && body.size != null ? body.size : null;
      log(this.__rec);
    }
    return osend.apply(this, arguments);
  };
})();
"""
        )
        page = context.pages[0] if context.pages else context.new_page()
        # 监听挂在 context 上，覆盖所有标签页（包括新生成的会话页）
        context.on("response", on_response)
        context.on("websocket", on_websocket)
        page.goto(TARGET_URL)

        print()
        print("浏览器已打开。请：")
        print("  1. 如未登录，先扫码登录")
        print("  2. 发一个文生图请求（例如：一只在月球上的猫），等图片生成完毕")
        print("  3. 上传一张参考图 + 提示词（例如：改成油画风），等图片生成完毕")
        print("  4. 回到本终端按回车结束")
        print()
        try:
            input("按回车结束抓包...")
        except (EOFError, KeyboardInterrupt):
            pass

        # 结束前统一读取 SSE 响应体（此时流已完结）
        for name, record, response in pending_sse:
            try:
                body = response.text()
            except Exception as exc:
                body = None
                record["body_error"] = f"{type(exc).__name__}: {exc}"
            if isinstance(body, str) and len(body) > MAX_BODY_CHARS:
                body = body[:MAX_BODY_CHARS] + "...[truncated]"
            record["response_body"] = body
            write_record(name, record)
            print(f"  SSE 响应体已补录: [{record['seq']:04d}] {record['url'][:100]}")

        # 收集各页面 JS 钩子记录的上传调用
        for pg in context.pages:
            try:
                upload_log = pg.evaluate("window.__uploadLog || []")
            except Exception:
                continue
            if upload_log:
                path = session_dir / "upload-calls.json"
                path.write_text(json.dumps(upload_log, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  上传调用记录已保存: {path}（{len(upload_log)} 条）")

        context.close()

    for record in open_ws:
        write_record("websocket", record)
    print(f"共记录 {counter} 条（含 {len(open_ws)} 条 WebSocket），保存在 {session_dir}")


if __name__ == "__main__":
    sys.exit(main())
