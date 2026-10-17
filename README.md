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

会打开浏览器并停在豆包登录页，用豆包 App 扫码；确认拿到**本次新签发**的登录态后才保存到
`data/credentials.json`（包含 Cookie 和设备标识参数，设备参数是风控必需项）。
未登录直接请求会返回 401；服务运行中凭证过期时，下一次请求会自动弹浏览器续期。

登录态有效期由服务端 `sid_guard` 声明，实测是 **30 天硬过期**，持续调用不会滚动续期。
随时查看还能用多久：

```bash
doubao2-api status
# 凭证文件：data/credentials.json
# 登录态：有效
# 到期时间：2026-09-18 09:06:29（剩余 30.0 天）
# Cookie 数量：27
# 设备参数：device_id、tea_uuid、web_id
```

`serve` 启动时也会体检：已过期打 ERROR、剩余不足 `SESSION_WARN_DAYS`（默认 3 天）打 WARNING，
剩余充足打 INFO —— 不必等某个请求 401 才发现。

## 免重复扫码：专用浏览器 profile + 保活

默认的 `login` 用临时浏览器，扫完就关，所以 30 天后还得再扫。想要"最多扫一次"就用专用 profile：

```bash
doubao2-api login --profile      # 一次性：在 data/browser-profile/ 里扫码登录
KEEPALIVE_ENABLED=true doubao2-api serve
```

`serve` 启动后会后台定时（`KEEPALIVE_INTERVAL`，默认 6 小时）用同一个 profile 打开
`https://www.doubao.com/chat/`，每次访问都检查服务端有没有**重新签发** `sid_guard`：

- 续期了 → 自动落盘新凭证（`保活成功：登录态已续期，新到期时间 …`）；
- 没续期 → 只记一条日志，**不动**现有凭证；
- profile 未登录/已过期 → 记日志提醒你跑 `doubao2-api login --profile`。

⚠️ 说实话：**"访问页面能否滚动续签"取决于豆包服务端，我没有账号可验证**，代码不假定它一定成功。
第一次保活后的日志会直接告诉你答案：出现 `保活成功：登录态已续期` 就说明能自动续；一直是
`保活未续期` 就说明 30 天仍然必须重新扫码（此时 profile 的意义只剩"少打一次 login、免扫码"）。

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
| `SESSION_WARN_DAYS` | `3` | 启动体检时，登录态剩余不足这么多天就打 WARNING |
| `KEEPALIVE_ENABLED` | `false` | 是否后台定时访问豆包页面保活（需先 `login --profile`） |
| `PROFILE_DIR` | `browser-profile` | 专用浏览器 profile（相对路径按 `DATA_DIR` 解析） |
| `KEEPALIVE_INTERVAL` | `21600` | 保活访问间隔（秒，默认 6 小时） |
| `KEEPALIVE_PAGE_WAIT_MS` | `10000` | 每次访问后等页面多久（毫秒），留给服务端续签 |
| `BROWSER_CHANNEL` | 空 | 有头模式建议填 `chrome`，用系统 Chrome 更像真人 |

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
| 401 | `auth_expired` | 登录态过期（自动续期超时，或续期后仍失败） |
| 429 | `rate_limited` | 触发豆包频率限制/风控（需人工在浏览器过滑块） |
| 502 | `image_download_failed` | 结果图下载失败 |
| 504 | `generation_timeout` | 轮询生图结果超时 |

## 已知限制

- **登录态 30 天到期**：豆包 `sid_guard` 声明的会话有效期是 30 天。到期前用 `doubao2-api status` 能看到、`serve` 启动日志会告警。开了保活（见上）就有机会自动续；实测没有滚动续签，所以最坏情况仍是每 30 天 `login --profile` 扫一次。
- 自动扫码续期需要有人在场：请求触发续期时会弹出 Chromium，等待 `LOGIN_TIMEOUT`（默认 180 秒）内无人扫码就返回 401。
- 保活是**后台线程 + 真实浏览器**：有头模式需要可用的显示服务（无显示时用 `HEADLESS=true`）；profile 同时被别的 Chrome 占用会启动失败，连续失败 5 次后保活自动停止并记日志。
- 豆包风控触发（`429 rate_limited`）后，需要人工在浏览器正常发一条消息（可能需要完成滑块验证），之后 API 恢复可用；保活只访问页面，不保证能解风控。
- 豆包在会话期间若下发新的 `Set-Cookie`，客户端目前不回写（每次请求都用磁盘快照）；尚未观察到影响，见 `docs/doubao-api-notes.md` §运行时注意事项 8。
- `size` 会同时写进 `chat_ability` 的 `ratio` 字段和提示词（如"图片比例为1:1，图片尺寸为1024x1024"）；实测后者对 Seedream 生效更稳定，`1024x1024` 现可稳定产出 1:1 图。
- 默认 `b64_json` 响应会包含整张图的 base64（数 MB），批量调用建议显式传 `response_format=url`。
- 协议细节与抓包笔记见 `docs/doubao-api-notes.md`。

## 测试

```bash
.venv/bin/pytest
```
