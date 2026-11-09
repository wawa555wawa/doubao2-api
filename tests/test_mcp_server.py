import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from doubao2_api import mcp_server
from doubao2_api.doubao_client import AuthExpired, GenerationFailed, RateLimited
from doubao2_api.images import StoredImage

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeStore:
    def __init__(self, *, empty: bool = False, expired: bool = False) -> None:
        self.empty = empty
        self.expired = expired

    def is_empty(self) -> bool:
        return self.empty

    def is_expired(self) -> bool:
        return self.expired


class FakeDoubao:
    def __init__(self, urls=None, error: Exception | None = None) -> None:
        self.urls = urls or []
        self.error = error
        self.calls: list[dict] = []

    async def generate(self, prompt, size, n=1, reference_images=None):
        self.calls.append({"prompt": prompt, "size": size, "n": n})
        if self.error is not None:
            raise self.error
        return self.urls[:n]


class FakeImages:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path / "images"
        self.dir.mkdir()
        self.urls: list[str] = []

    async def fetch_and_store(self, url: str) -> StoredImage:
        self.urls.append(url)
        path = self.dir / f"image-{len(self.urls)}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n fake")
        return StoredImage(id=f"image-{len(self.urls)}", path=path, data=b"fake")


def install_runtime(monkeypatch, tmp_path, *, store=None, client=None):
    images = FakeImages(tmp_path)
    runtime = SimpleNamespace(
        store=store or FakeStore(),
        client=client or FakeDoubao(),
        images=images,
    )
    monkeypatch.setattr(mcp_server, "_build_runtime", lambda: runtime)
    return runtime


async def test_generate_image_is_listed_with_timeout_hint():
    tools = await mcp_server.mcp.list_tools()
    tool = next(t for t in tools if t.name == "generate_image")

    assert "120" in tool.description
    schema = tool.inputSchema
    assert schema["required"] == ["prompt"]
    assert set(schema["properties"]) == {"prompt", "size", "n"}
    assert schema["properties"]["size"]["default"] == "1024x1024"
    assert schema["properties"]["n"]["default"] == 1


async def test_generate_image_returns_absolute_paths(monkeypatch, tmp_path):
    client = FakeDoubao(["https://cdn.example.com/a.png", "https://cdn.example.com/b.png"])
    runtime = install_runtime(monkeypatch, tmp_path, client=client)

    paths = await mcp_server.generate_image("一只月球上的猫", size="1024x1024", n=2)

    assert client.calls == [
        {"prompt": "一只月球上的猫", "size": "1024x1024", "n": 2}
    ]
    assert runtime.images.urls == [
        "https://cdn.example.com/a.png",
        "https://cdn.example.com/b.png",
    ]
    assert paths == [
        str((tmp_path / "images" / "image-1.png").resolve()),
        str((tmp_path / "images" / "image-2.png").resolve()),
    ]
    for path in paths:
        assert Path(path).is_absolute()
        assert Path(path).is_file()


async def test_generate_image_asks_to_login_when_no_credentials(monkeypatch, tmp_path):
    client = FakeDoubao()
    install_runtime(monkeypatch, tmp_path, store=FakeStore(empty=True), client=client)

    with pytest.raises(ToolError) as exc_info:
        await mcp_server.generate_image("x")

    assert "doubao2-api login" in str(exc_info.value)
    assert client.calls == []


async def test_generate_image_asks_to_login_when_credentials_expired(monkeypatch, tmp_path):
    client = FakeDoubao()
    install_runtime(monkeypatch, tmp_path, store=FakeStore(expired=True), client=client)

    with pytest.raises(ToolError) as exc_info:
        await mcp_server.generate_image("x")

    assert "doubao2-api login" in str(exc_info.value)
    assert client.calls == []


async def test_generate_image_maps_auth_expired_to_login_hint(monkeypatch, tmp_path):
    client = FakeDoubao(error=AuthExpired("登录已过期"))
    install_runtime(monkeypatch, tmp_path, client=client)

    with pytest.raises(ToolError) as exc_info:
        await mcp_server.generate_image("x")

    message = str(exc_info.value)
    assert "登录态已过期" in message
    assert "doubao2-api login" in message


async def test_generate_image_maps_rate_limit_to_wait_hint(monkeypatch, tmp_path):
    client = FakeDoubao(error=RateLimited("rate limited"))
    install_runtime(monkeypatch, tmp_path, client=client)

    with pytest.raises(ToolError) as exc_info:
        await mcp_server.generate_image("x")

    message = str(exc_info.value)
    assert "429" in message
    assert "等待" in message
    assert "rate limited" in message


async def test_generate_image_returns_raw_error_summary_for_other_failures(
    monkeypatch, tmp_path
):
    client = FakeDoubao(error=GenerationFailed("生图流错误: {'code': 710022005}"))
    install_runtime(monkeypatch, tmp_path, client=client)

    with pytest.raises(ToolError) as exc_info:
        await mcp_server.generate_image("x")

    message = str(exc_info.value)
    assert "生图失败" in message
    assert "GenerationFailed" in message
    assert "710022005" in message


def test_runtime_client_timeout_is_at_least_120_seconds(monkeypatch, tmp_path):
    captured = {}

    def fake_doubao(store, timeout):
        captured["timeout"] = timeout
        return FakeDoubao()

    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path, generation_timeout=30.0),
    )
    monkeypatch.setattr(mcp_server, "CredentialStore", lambda path: FakeStore())
    monkeypatch.setattr(mcp_server, "DoubaoClient", fake_doubao)
    monkeypatch.setattr(mcp_server, "ImageStore", lambda path: FakeImages(tmp_path))

    mcp_server._build_runtime()

    assert captured["timeout"] >= 120


def test_cli_mcp_stdio_lists_tool_and_stdout_is_json_only(tmp_path):
    env = dict(os.environ)
    env["DATA_DIR"] = str(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "doubao2_api.cli", "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=REPO_ROOT,
        env=env,
    )
    stdout_lines: list[str] = []
    responses: queue.Queue[str | None] = queue.Queue()

    def pump_stdout() -> None:
        for line in proc.stdout:
            stdout_lines.append(line)
            responses.put(line)
        responses.put(None)

    threading.Thread(target=pump_stdout, daemon=True).start()

    def send(message: dict) -> None:
        proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def receive() -> dict:
        line = responses.get(timeout=15)
        assert line is not None, "server closed stdout before responding"
        return json.loads(line)

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            }
        )
        init_response = receive()
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools_response = receive()
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    stderr = proc.stderr.read()
    assert proc.returncode == 0, stderr

    # 每一行 stdout 都必须是合法 JSON-RPC；普通日志只能在 stderr。
    messages = [json.loads(line) for line in stdout_lines if line.strip()]
    assert len(messages) == 2
    assert init_response["result"]["serverInfo"]["name"] == "doubao2-api"
    tools = {t["name"]: t for t in tools_response["result"]["tools"]}
    assert "generate_image" in tools
    assert "120" in tools["generate_image"]["description"]
    assert "Traceback" not in stderr
