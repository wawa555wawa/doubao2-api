# doubao2-api

把豆包网页版生图能力包装成 OpenAI 兼容的图片生成 API：

- `POST /v1/images/generations` — 文生图（JSON）
- `POST /v1/images/edits` — 图生图（multipart 上传参考图）
- `GET /images/{文件名}` — 本地静态图片服务（`response_format=url` 时使用）

客户端按真实抓包协议直连 `www.doubao.com`（completion SSE + IM 轮询 + imagex 上传链），无需浏览器常驻；凭证失效时自动弹浏览器扫码续期。

## 安装

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
playwright install chromium
```

要求 Python ≥ 3.11。

## 登录（扫码）

```bash
doubao2-api login
```

会打开浏览器并停在豆包登录页，用豆包 App 扫码；检测到 `sessionid` 后自动保存到
`data/credentials.json`（包含 Cookie 和设备标识参数，设备参数是风控必需项）。
未登录直接请求会返回 401；服务运行中凭证过期时，下一次请求会自动弹浏览器续期。

## 启动

```bash
doubao2-api serve
```

默认监听 `http://127.0.0.1:9756`。环境变量配置（可用 `.env`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 监听地址 |
| `PORT` | `9756` | 监听端口 |
| `DATA_DIR` | `data` | 凭证与生成图片的存放目录 |
| `GENERATION_TIMEOUT` | `120` | 单个生图任务的轮询超时（秒） |
| `LOGIN_TIMEOUT` | `180` | 自动扫码续期的等待超时（秒） |
| `MAX_CONCURRENT` | `1` | 并发生图上限 |
| `HEADLESS` | `false` | 扫码登录是否用无头浏览器（一般保持 false） |

## 调用示例

文生图（`response_format=url` 返回轻量 JSON；默认是 `b64_json`）：

```bash
curl -s http://127.0.0.1:9756/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"一只月球上的猫","size":"1024x1024","n":2,"response_format":"url"}'
```

```json
{
  "created": 1787030000,
  "data": [
    {"url": "http://127.0.0.1:9756/images/ab12....png"},
    {"url": "http://127.0.0.1:9756/images/cd34....png"}
  ]
}
```

图生图（参考图 + 提示词）：

```bash
curl -s -X POST http://127.0.0.1:9756/v1/images/edits \
  -F 'image=@/path/to/reference.png;type=image/png' \
  -F 'prompt=改成油画风' \
  -F 'size=1024x1024' \
  -F 'n=1' \
  -F 'response_format=url'
```

生成过程通常 20–40 秒。豆包每个提示词默认产出 4 张图，`n` 只做截断返回前 `n` 张。

## 错误格式

所有错误统一为 OpenAI 风格：

```json
{"error": {"message": "...", "type": "invalid_request_error", "code": "..."}}
```

| HTTP | code | 触发 |
| --- | --- | --- |
| 400 | `generation_failed` | 豆包侧生图失败 / 上传失败 / 协议异常 |
| 401 | `auth_expired` | 登录态过期（自动续期超时或续期后仍失败） |
| 429 | `rate_limited` | 触发豆包频率限制/风控（需人工在浏览器过滑块） |
| 502 | `image_download_failed` | 结果图下载失败 |
| 504 | `generation_timeout` | 轮询生图结果超时 |

## 已知限制

- 豆包风控触发（`429 rate_limited`）后，需要人工在浏览器正常发一条消息（可能需要完成滑块验证），之后 API 恢复可用。
- `size` 会同时写进 `chat_ability` 的 `ratio` 字段和提示词（如"图片比例为1:1，图片尺寸为1024x1024"）；实测后者对 Seedream 生效更稳定，`1024x1024` 现可稳定产出 1:1 图。
- 默认 `b64_json` 响应会包含整张图的 base64（数 MB），批量调用建议显式传 `response_format=url`。
- 协议细节与抓包笔记见 `docs/doubao-api-notes.md`。

## 测试

```bash
.venv/bin/pytest
```
