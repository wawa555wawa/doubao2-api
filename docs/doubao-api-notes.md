# 豆包网页版生图接口抓包笔记

> 来源：2026-08-18 通过 `tools/capture_doubao.py`（禁用 Service Worker）抓包 + httpx 重放验证。
> 豆包网页版在 Service Worker 被禁用后会退化为纯 HTTP 通道：**发送走 `POST /chat/completion`（SSE 流），结果走 IM 轮询 `POST /im/chain/single`**。不存在独立的"任务提交/任务查询"接口。

## §提交任务（文生图 / 图生图发送）

- **方法/URL**：`POST https://www.doubao.com/chat/completion?<公共query参数>`
- **公共 query 参数**（重放验证可用的最小集）：
  `aid=497858&device_platform=web&language=zh&pkg_type=release_version&real_aid=497858&region=CN&samantha_web=1&sys_region=CN&use-olympus-account=1&version_code=20800&web_platform=browser`
  - 浏览器实际还携带 `device_id`、`web_id`、`tea_uuid`、`web_tab_id`、`fp`、`msToken`、`a_bogus`。**重放验证结论：不带 fp/msToken/a_bogus 也能成功**；但风控敏感操作可能要求更多参数（见 §失败样本）。
- **必需请求头**：
  - `Content-Type: application/json`
  - `Cookie: <完整登录 Cookie>`
  - `User-Agent: <常见桌面浏览器 UA>`
  - `Referer: https://www.doubao.com/chat/`、`Origin: https://www.doubao.com`
- **响应**：`200`，`Content-Type: text/event-stream`，SSE 事件流。事件格式：
  ```
  id: 0
  event: DOWNLINK_CMD
  data: {"cmd":50080,"sequence_id":"...","downlink_body":{"pull_msg_notify":{"conversation_id":"<会话ID>","message_index":"1","limit":2}},"version":"1"}

  id: 1
  event: SSE_REPLY_END
  data: {"end_type":3}
  ```
  - `DOWNLINK_CMD` 事件的 `data.downlink_body.pull_msg_notify.conversation_id` 即新会话 ID（**JSON 路径**：解析 `data:` 后的 JSON）。
  - `SSE_REPLY_END` 表示流结束；`end_type=3` 表示结果通过 IM 通道下发（需转去轮询）。
  - 心跳事件 `SSE_HEARTBEAT`（`data: {}`）可忽略。
  - 出错时发 `STREAM_ERROR` 事件（见 §失败样本）。

### 请求体（文生图）

顶层字段：`client_meta`、`messages`、`option`、`chat_ability`、`user_context`、`ext`。完整样本见 `tests/fixtures/`（发送体样本未入 fixture，以下为准）：

- `client_meta`: `{"local_conversation_id": "local_<随机数>", "conversation_id": "", "bot_id": "7338286299411103781", "last_section_id": "", "last_message_index": null}`
  - `bot_id` 固定为 `7338286299411103781`（豆包主 bot，生图走 `chat_ability` 声明）。
  - 新会话时 `conversation_id` 留空 + `option.need_create_conversation: true`。
- `messages`：`[{"local_message_id": "<uuid1>", "content_block": [{"block_type": 10000, "content": {"text_block": {"text": "生成图片：<提示词>", "icon_url": "", "icon_url_dark": "", "summary": ""}, "pc_event_block": ""}, "block_id": "<uuid4>", "parent_id": "", "meta_info": [], "append_fields": []}], "message_status": 0}]`
  - 提示词前缀 `生成图片：` 是网页版实际行为，照抄。
- `option`：大量布尔/字符串字段，照抄抓包样本即可；关键字段：
  - `create_time_ms`: 当前毫秒时间戳
  - `unique_key`: 新 uuid4
  - `need_create_conversation: true`、`conversation_init_option: {"need_ack_conversation": true}`
- `chat_ability`（**生图能力声明，尺寸在这里**）：
  ```json
  {"ability_type": 3, "ability_param": "{\"ability_param\":{\"model\":\"Seedream 4.5\",\"ratio\":\"auto\"},\"ability_type\":1}"}
  ```
  `ability_param` 是**字符串内嵌 JSON**。`ratio` 取值见 §尺寸映射。
- `user_context`: `[]`；`ext` 照抄样本（含 `"is_finish":"1"` 等字符串键值对）。

> ⚠️ **所有 ID 必须每次新生成**：`local_message_id`、`block_id`、`unique_key`、`local_conversation_id`。重放时复用抓包 ID 会被服务端去重、直接返回旧会话。

### 请求体（图生图）

`messages` 变为两条，附件消息在前：

1. 附件消息：`content_block[0].block_type = 10052`，`content.attachment_block.attachments = [{"type": 1, "identifier": "<与 pre_handle 一致>", "image": {"name": "<文件名>", "uri": "<上传得到的 tos uri>", "image_ori": {"url": "", "width": <宽>, "height": <高>, "format": "", "url_formats": {}}}, "parse_state": 0, "review_state": 1, "upload_status": 1, "progress": 100, "src": ""}]`
2. 文本消息：同文生图（`"生成图片：<提示词>"`）。

其余字段同文生图。参考图数量上限：未实测，网页版 UI 允许 1 张参考图（按 1 张处理，多张未验证）。

## §轮询（取结果）

- **方法/URL**：`POST https://www.doubao.com/im/chain/single?<公共query参数>`（同上）
- **必需请求头**：同 §提交任务，但 `Content-Type` 必须是 **`application/json; encoding=utf-8`**（注意是 `encoding` 不是 `charset`；写成标准 `application/json` 会报 `712012002 不支持编码类型`）。另加 `Accept: application/json, text/plain, */*`。
- **请求体**：
  ```json
  {"cmd": 3100,
   "uplink_body": {"pull_singe_chain_uplink_body": {
     "conversation_id": "<SSE 下发的会话ID>",
     "anchor_index": 9007199254740991,
     "conversation_type": 3,
     "direction": 1,
     "limit": 20,
     "ext": {},
     "filter": {"index_list": []}}},
   "sequence_id": "<uuid4>",
   "channel": 2,
   "version": "1"}
  ```
- **响应**：`{"cmd":3100, "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [...]}}}`
  - 消息数组按 `index_in_conv` 排列；bot 的回复消息（`user_type: 2`）内含 `content_block` 数组。
  - **生成中**：回复消息只有 `text_block`（block_type 10000，闲聊文本）和 `thinking_block`（block_type 10040，`streaming_title: "正在生成图片"`）。见 `tests/fixtures/poll_running.json`。
  - **生成完成**：出现 **block_type 2074** 的块，`content.creation_block.creations[]` 每项一张图：
    - 原图 URL JSON 路径：`creation_block.creations[i].image.image_ori.url`（带签名，长期有效）
    - 备用：`image.image_thumb.url` / `image.image_preview.url`；`image.key` 为 tos uri。
    - 块级 `is_finish: true`。
    - 见 `tests/fixtures/poll_done.json`。
  - 判定完成的建议逻辑：找到 user_type=2 的最新消息，若含 block_type 2074 且 `is_finish == true` 且 creations 非空 → 取全部 `image_ori.url`；否则继续轮询（间隔 2 秒）。
  - 回复消息里没有 2074 块且 `is_finish` 全 true 且无进行中的 thinking_block → 视为失败（拒答/审核），取 text_block 文本作为错误信息。

## §上传（图生图参考图）

四步（样本见 fixtures）：

1. **`POST /alice/resource/prepare_upload`**（Content-Type 用标准 `application/json`）
   请求体：`{"tenant_id":"5","scene_id":"5","resource_type":2}`
   响应：`data.service_id`（如 `a9rns2rl98`）、`data.upload_auth_token`（access_key/secret_key/session_token，临时凭证）。
   fixture：`prepare_upload_success.json`
2. **`GET /top/v1?Action=ApplyImageUpload&Version=2018-08-01&ServiceId=<service_id>&NeedFallback=true&FileSize=<字节数>&FileExtension=<.png 等>&...`**
   （query 还含 `s=<随机>` 等，照抄样本；此请求虽为 GET 但走 `/top/v1` 代理到 imagex）
   响应：`Result.UploadAddress.StoreInfos[0].StoreUri`（tos uri）、`UploadHosts`、`Result.UploadAddress.StoreInfos[0].Auth`（PUT 授权头）。
   fixture：`apply_upload_success.json`
3. **PUT 图片二进制到上传地址**（抓包中未直接捕获到该请求，按火山引擎 ImageX 上传协议实现）：
   `PUT https://<UploadHosts[0]>/<StoreUri>`，请求头 `Authorization: <Auth>`，`Content-Type: application/octet-stream`，body 为图片字节。
   ⚠️ 此步未经重放验证，实现时需在联调中确认。
4. **`POST /top/v1?Action=CommitImageUpload&Version=2018-08-01&ServiceId=<service_id>`**（Content-Type: `application/json`）
   请求体：`{"SessionKey": "<base64 JSON，含 storeInfos/StoreUri+Auth、uploadHost、uri>"}`——网页版直接复用第 2 步返回里的 SessionKey 结构（见抓包 `0258-v1.json` 的请求体）。
   响应：`Result.Results[0].Uri` = StoreUri；`Result.PluginResult[0]` 含图片元信息（宽/高）。
   fixture：`commit_upload_success.json`
5. **`POST /alice/message/pre_handle_v2_without_conv`**（Content-Type: `application/json; encoding=utf-8`）
   请求体：`{"uplink_entity":{"entity_type":2,"entity_content":{"image":{"key":"<StoreUri>"}},"identifier":"<uuid1>"},"bot_id":"7338286299411103781","local_message_id":"<uuid1>"}`
   响应：`{"code":0,"data":{"pre_generate_id":"..."}}`。其中 `identifier`/`local_message_id` 需与随后 §提交任务 附件消息中的 `identifier`/`local_message_id` 一致。
   fixture：`pre_handle_success.json`

## §未登录特征

- `/chat/completion`：HTTP 200，`Content-Type: application/json`，body：
  `{"code":710012001,"msg":"登录已过期，请重新登录","message":"login invalid","error":{"code":710012001,"message":"登录已过期，请重新登录","locale":"zh"}}`
  （已用无 Cookie 重放验证。）fixture：`auth_expired.json`
- IM 接口（chain/single 等）未登录时的表现未单独抓样本；实现时按统一策略：响应 JSON 顶层出现 `code == 710012001` 或 `status_code == 710012001` 即判定 `AuthExpired`。

## §失败样本

- **频率限制/风控**（SSE 流内）：`event: STREAM_ERROR`，`data.error_code == 710022004`，`error_msg: "rate limited"`，`extra.decision` 内含 `type: "verify"`（滑块验证，`verify_scene: "doubao_message_web"`）。→ 映射为 `RateLimited`。
  fixture：`sse_rate_limited.txt`（已脱敏）
- **编码错误**：`{"status_code":712012002,"status_desc":"不支持编码类型"}`（Content-Type 写错时）。
- 内容审核拒答样本未抓到；按 §轮询 的失败判定逻辑处理（无 2074 块 + 回复已结束），把 bot 文本作为错误信息。

## §登录态标记

登录成功后出现的关键 Cookie：**`sessionid`**（另有 `sessionid_ss`、`sid_tt` 等）。扫码续期时检测 `sessionid` 存在即视为登录成功。
（已从浏览器 profile 直接提取验证：`sessionid`、`sessionid_ss`、`sid_guard`、`sid_tt`、`ttwid`、`passport_csrf_token` 等均存在。）

## §尺寸映射

`chat_ability.ability_param` 内嵌 JSON 的 `ratio` 字段控制画幅，抓包样本值为 `"auto"`。网页版可选值为常见比例字符串：`"1:1"`、`"4:3"`、`"3:4"`、`"16:9"`、`"9:16"`、`"auto"`（按网页版 UI 的可选项；除 auto 外未逐一抓包验证）。
OpenAI 风格 `size`（`"宽x高"`）→ ratio 的映射：计算宽高比，取最接近的上表比例；无法解析时用 `"auto"`。
`model` 固定 `"Seedream 4.5"`。

## §运行时注意事项（重放实验结论）

1. **ID 去重**：`local_message_id`/`block_id`/`unique_key`/`local_conversation_id` 必须每次新生成，否则服务端去重返回旧会话。
2. **风控**：连续快速发消息可能触发 `STREAM_ERROR 710022004`（滑块验证）。低频使用问题不大；触发后映射 429，人工在浏览器里发一条消息通过验证即可恢复。
3. **SSE 只需浅解析**：逐行读 `event:`/`data:`，只需识别 `DOWNLINK_CMD`（取 conversation_id）、`STREAM_ERROR`（报错）、`SSE_REPLY_END`（结束）；其余忽略。
4. 生成耗时参考：文生图约 10-30 秒；轮询间隔建议 2 秒。
