# doubao2-api 设计文档

日期：2026-08-15

## 目标

把豆包网页版的生图能力包装成一个本地 HTTP API 服务，供个人低频使用。

- 通过抓包逆向豆包网页版内部接口（HTTP 重放），而非浏览器自动化驱动生图。
- 对外提供 **OpenAI 兼容** 的生图 API，支持**文生图**与**图生图**。
- 生成的图片下载到本地后再返回给调用方（不依赖豆包 CDN 链接时效）。
- 登录态（Cookie）过期时，通过 Playwright 弹出浏览器扫码自动续期。

## 技术栈

- Python + FastAPI + httpx + Playwright + pydantic-settings
- 包管理：`pyproject.toml`
- 测试：pytest + respx

## 总体架构

代码位于 `src/doubao2_api/`，按职责拆为以下模块：

| 模块 | 职责 |
| --- | --- |
| `doubao_client.py` | 核心逆向客户端。用 httpx 重放豆包内部接口：提交生图任务 → 轮询任务状态 → 取得图片 URL；图生图先调上传接口。识别未登录响应并抛出 `AuthExpired`，不关心如何续期。 |
| `credentials.py` | 凭证存储。Cookie 等登录态保存在 `data/credentials.json`，提供读写接口，启动时加载。 |
| `session_refresh.py` | Playwright 续期。启动有头浏览器打开豆包登录页，等待扫码，检测登录成功后提取 Cookie 写回凭证存储。 |
| `images.py` | 图片落地。下载豆包 CDN 图到 `data/images/`，产出 base64 或本地可访问 URL（由 `main.py` 挂载的 `/images/` 静态路由对外提供访问）。 |
| `main.py` | FastAPI 路由层。对外暴露 OpenAI 兼容端点，负责参数映射、错误转换、串联各模块。 |
| `cli.py` | 提供 `doubao2-api login` 命令，手动触发扫码登录。 |

### 数据流

客户端请求 → 路由层解析 OpenAI 格式参数 → `doubao_client` 提交任务并轮询 → 若抛 `AuthExpired`，调用 `session_refresh` 弹浏览器扫码，成功后用新 Cookie 自动重试一次 → 拿到图片 URL 后 `images` 下载落地 → 按 OpenAI 格式响应。

## API 设计

### `POST /v1/images/generations`（文生图）

JSON 请求体，字段对齐 OpenAI：

- `prompt`（必填）
- `size`（如 `1024x1024`，映射到豆包对应尺寸参数）
- `n`（生成数量）
- `response_format`：`b64_json` 或 `url`（url 指向本服务的本地图片地址）

### `POST /v1/images/edits`（图生图）

`multipart/form-data`，字段对齐 OpenAI：

- `image`：参考图文件，支持多张；上限以豆包网页版实际限制为准（抓包确认），超限返回 400。
- `prompt`（必填）、`size`、`n`。

### 响应与错误格式

成功与错误响应均遵循 OpenAI 格式，错误为
`{"error": {"message", "type", "code"}}`。

## 豆包内部接口（逆向模型）

具体地址、参数、上传格式在实现第一步通过抓包确认；设计按以下通用模型：

- 提交生图任务（prompt + 尺寸 [+ 参考图标识]）。
- 异步轮询任务状态直至完成，取得图片 URL 列表。
- 图生图：先调用上传接口获得图片标识（uri/id），再提交任务。
- 抓包结果只影响 `doubao_client` 内部实现，不影响其他模块。
- 抓包时将真实响应样本存为测试 fixture。

## 登录态续期

- `doubao_client` 识别未登录特征（HTTP 401 或业务错误码，抓包确认）后抛 `AuthExpired`。
- 路由层捕获后调用 `session_refresh`：Playwright 启动有头浏览器打开豆包登录页，用户扫码；程序轮询检测登录成功，提取 Cookie 写入 `data/credentials.json`，随后自动重试原请求一次。
- 扫码等待超时（`LOGIN_TIMEOUT`，默认 180 秒）则放弃，返回 401 及提示："登录态过期，自动续期超时，请重试或运行 `doubao2-api login`"。
- 续期流程加锁，同一时间只允许一个，避免并发弹出多个浏览器。
- 支持手动续期：`doubao2-api login` CLI；服务启动时若无有效凭证，提示先登录。

## 错误处理

| 场景 | 响应 |
| --- | --- |
| 提示词触发审核 / 生图失败 | 400，附豆包原始错误信息 |
| 频率限制 | 429 |
| 生图轮询超时（默认 120 秒，可配） | 504 |
| 图片下载失败 | 重试一次，仍失败返回 502 |
| 登录态过期且续期超时 | 401，含明确提示 |

## 并发策略

全局信号量限制同时进行的生图任务数（`MAX_CONCURRENT`，默认 1），避免触发豆包侧风控。

## 配置

`pydantic-settings` 管理，支持环境变量与 `.env` 文件：

- `HOST` / `PORT`（默认 8000）
- `DATA_DIR`（默认 `./data`，存放 `credentials.json` 与 `images/`）
- `GENERATION_TIMEOUT`（默认 120 秒）
- `LOGIN_TIMEOUT`（默认 180 秒）
- `MAX_CONCURRENT`（默认 1）
- `HEADLESS`（默认 `false`，扫码需要有头浏览器）

`data/` 加入 `.gitignore`，避免凭证与图片进入版本库。

## 测试

- 单元测试：`doubao_client` 用 respx mock httpx 请求，基于抓包保存的真实响应样本 fixture，验证参数映射、轮询逻辑、`AuthExpired` 识别。
- 接口测试：FastAPI TestClient + mock 的 `doubao_client`，验证两个端点的请求校验、OpenAI 格式响应、各错误映射。
- 扫码续期涉及真人操作，仅手动验证，不写自动化测试。
