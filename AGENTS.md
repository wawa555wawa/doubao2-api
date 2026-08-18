# AGENTS.md

面向在本仓库工作的 AI 编码代理与协作者的约定。先读 README.md 和 docs/doubao-api-notes.md。

## 项目是什么

`doubao2-api`：把豆包网页版生图能力包装成 OpenAI 兼容接口。

- `POST /v1/images/generations` 文生图
- `POST /v1/images/edits` 图生图
- `GET /images/{文件名}` 生成图片静态服务

客户端直连 `www.doubao.com`（completion SSE → IM 轮询 → imagex 上传链），不需要常驻浏览器；登录态失效时用 Playwright 弹浏览器扫码续期。

## 常用命令

```bash
. .venv/bin/activate          # 虚拟环境
.venv/bin/pytest -q           # 全量测试（TDD 先写测试）
.venv/bin/doubao2-api login   # 扫码登录，写 data/credentials.json
.venv/bin/doubao2-api serve   # 启动服务，默认 127.0.0.1:9756
```

## 目录结构

```text
src/doubao2_api/
  config.py          # Settings（HOST/PORT/DATA_DIR/...，默认端口 9756）
  credentials.py     # CredentialStore：cookies + device 参数持久化
  doubao_client.py   # 核心协议客户端（SSE 提交、轮询、上传链、SigV4）
  session_refresh.py # Playwright 扫码登录 + 设备参数提取
  images.py          # 结果图下载落地
  main.py            # FastAPI 路由层与错误映射
  cli.py             # serve / login 命令
tests/               # 全部离线测试；fixtures 是脱敏样本
docs/doubao-api-notes.md  # 抓包与重放协议笔记（协议事实来源）
tools/capture_doubao.py   # Playwright 抓包工具
```

## 修改约束

1. **凭证绝不允许入库**：`data/`、`.env`、`.superpowers/` 已在 .gitignore。真实 Cookie、签名、设备 ID 不得写进源码/测试/文档；fixtures 一律脱敏（占位符如 `SANITIZED`）。
2. **测试必须离线可跑**：不要写依赖真实豆包账号或网络的测试；协议行为用 respx 模拟。
3. **改动协议前先读 `docs/doubao-api-notes.md`**。联调中的新发现（新事件、新响应形态、新参数）要同步更新该笔记，并补充脱敏 fixture。
4. 提交保持原子：一个 commit 只做一件事；提交前跑全量 `pytest`。
5. 保持 OpenAI 响应/错误格式不变：`{"error":{"message","type","code"}}`。

## 协议关键点（踩坑索引）

- **风控**：completion/chain/single 请求必须带 `device_id`、`web_id`、`tea_uuid`（+随机 `web_tab_id`），否则立即 429（`710022004` 滑块验证）。设备参数由 `session_refresh` 从页面请求 URL 自动提取并存入 `credentials.json` 的 `device` 字段。
- **SSE**：从 `SSE_ACK.ack_client_meta.conversation_id` 或 `DOWNLINK_CMD` 取会话 ID；`STREAM_ERROR` 处理限流/失败；其余事件忽略。
- **轮询**：`POST /im/chain/single` 的 Content-Type 必须是 `application/json; encoding=utf-8`（写错会 `712012002`）。结果有两种形态：`content_block` 里的 2074 块，或 `ext.creation_full_content`（JSON 字符串）里的 2074 包；要遍历所有 bot 消息，豆包会先回文字消息再回图片消息。
- **图生图上传链**：prepare → apply（AWS4-HMAC-SHA256 签名，密钥派生带 `"AWS4"` 前缀，region `cn-north-1`，service `imagex`，SignedHeaders 仅 `x-amz-date;x-amz-security-token`）→ **POST** `https://{host}/upload/v1/{StoreUri}`（头 `Content-CRC32` 为 zlib.crc32 的 8 位小写十六进制）→ commit（同样 SigV4，`UriStatus==2000` 才算成功）→ pre_handle（identifier/local_message_id 与附件消息一致）。
- **尺寸控制**：`chat_ability` 的 ratio 字段不可靠；`size` 同时注入提示词（`，图片比例为X:Y，图片尺寸为WxH`），实测 `1024x1024` 产出 2048x2048。修改此行为时保留 `_size_hint` 的单测。
- **ID 去重**：`local_message_id`/`block_id`/`unique_key`/`local_conversation_id` 每次请求必须新生成，否则服务端返回旧会话。

## 验证清单（改动 doubao_client 后）

1. `.venv/bin/pytest -q` 全绿。
2. 若改动了请求格式，对照 `docs/doubao-api-notes.md` 和 fixtures 复核字段路径。
3. 有真实账号可用时：`doubao2-api serve` 后跑一次文生图（`response_format=url`，检查返回图尺寸符合 `size`），图生图跑一次 `-F image=@...` 确认上传链。
4. 若改动登录/续期：运行 `doubao2-api login` 真机扫码一次，确认 `data/credentials.json` 自动包含 `sessionid` 和三个设备参数。
