# 豆包网页版生图接口抓包笔记

> 来源：2026-08-18 通过 `tools/capture_doubao.py`（禁用 Service Worker）抓包 + httpx 重放验证。
> 豆包网页版在 Service Worker 被禁用后会退化为纯 HTTP 通道：**发送走 `POST /chat/completion`（SSE 流），结果走 IM 轮询 `POST /im/chain/single`**。不存在独立的"任务提交/任务查询"接口。

## §提交任务（文生图 / 图生图发送）

- **方法/URL**：`POST https://www.doubao.com/chat/completion?<公共query参数>`
- **公共 query 参数**：
  最小集（实测可发请求但**会立即被风控 rate limited**）：
  `aid=497858&device_platform=web&language=zh&pkg_type=release_version&real_aid=497858&region=CN&samantha_web=1&sys_region=CN&use-olympus-account=1&version_code=20800&web_platform=browser&pc_version=3.32.51&doubao_pc_version=3.32.51&doubao_device_platform=web`
  - **风控必需**：`device_id`、`web_id`、`tea_uuid`（实测补齐后不再触发限流；这三个值是账号/设备级稳定标识，由扫码登录流程从浏览器页面请求中提取并保存在凭证里），外加 `web_tab_id`（每次随机 uuid 即可）。
  - 浏览器实际还携带 `fp`、`msToken`、`a_bogus`（JS 签名）。**实测结论：不带这三个签名参数也能成功**，无需逆向签名算法。
- **必需请求头**：
  - `Content-Type: application/json`
  - `Cookie: <完整登录 Cookie>`
  - `User-Agent: <常见桌面浏览器 UA>`
  - `Referer: https://www.doubao.com/chat/`、`Origin: https://www.doubao.com`
- **响应**：`200`，`Content-Type: text/event-stream`，SSE 事件流。事件格式：
  - `SSE_ACK`（实测最常见）：`data.ack_client_meta.conversation_id` 即新会话 ID（**JSON 路径**）。
  - `DOWNLINK_CMD`（另一形态）：`data.downlink_body.pull_msg_notify.conversation_id` 为会话 ID。两种都实现了解析，优先 ACK。
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
  - **生成完成（形态 A）**：`content_block` 中出现 **block_type 2074** 的块，`content.creation_block.creations[]` 每项一张图。见 `tests/fixtures/poll_done.json`。
  - **生成完成（形态 B，重放实测出现）**：回复消息 `content_block` 为空数组、`content_type: 1`，真正的内容在 **`ext.creation_full_content`**（JSON 字符串，元素形如 `{"BlockInfo": {"BlockType": 2074, "BlockContent": {"content": {"creation_block": ...}}, "BlockMeta": {"is_finish": true}}}`）。2074 包且 `BlockMeta.is_finish == true` 时取图。见 `tests/fixtures/poll_done_ext.json`。两种形态都要兼容解析。
  - **图片 URL JSON 路径**：`creation_block.creations[i].image.image_ori.url`（带签名，长期有效）；备用 `image.image_thumb.url` / `image.image_preview.url`；`image.key` 为 tos uri。
  - 判定完成的建议逻辑：**遍历所有 user_type=2 的消息**，两种形态任一取到完成状态的 creations → 返回全部 `image_ori.url`；否则继续轮询（间隔 2 秒）。
  - 实测模型常先回一条纯文字消息（"已生成……"）、图片结果在后续 bot 消息里；找不到结果块时继续轮询，不提前判失败。

## §上传（图生图参考图）

四步（样本见 fixtures）：

1. **`POST /alice/resource/prepare_upload`**（Content-Type 用标准 `application/json`）
   请求体：`{"tenant_id":"5","scene_id":"5","resource_type":2}`
   响应：`data.service_id`（如 `a9rns2rl98`）、`data.upload_auth_token`（access_key/secret_key/session_token，临时凭证）。
   fixture：`prepare_upload_success.json`
2. **`GET /top/v1?Action=ApplyImageUpload&Version=2018-08-01&ServiceId=<service_id>&NeedFallback=true&FileSize=<字节数>&FileExtension=<.png 等>`**
   （此请求虽为 GET 但走 `/top/v1` 代理到 imagex；样本里的 `s=<随机>` 实测不需要）
   **必须做 AWS4-HMAC-SHA256 签名**：用 `prepare_upload` 返回的临时凭证（access_key/secret_key/session_token），
   `SignedHeaders=x-amz-date;x-amz-security-token`，region=`cn-north-1`、service=`imagex`，
   密钥派生带 `"AWS4"` 前缀；GET 的 payload hash 为 `sha256("")`。
   响应：`Result.UploadAddress.StoreInfos[0].StoreUri`（tos uri）、`UploadHosts`、`Result.UploadAddress.StoreInfos[0].Auth`。
   fixture：`apply_upload_success.json`
3. **`POST` 图片二进制到对象存储**（通过 fetch/XHR hook 抓包 + 重放验证）：
   `POST https://<UploadHosts[0]>/upload/v1/<StoreUri>`（注意 `/upload/v1/` 前缀和 POST，不是 PUT！），
   请求头：`Authorization: <Auth>`（SpaceKey/...）、`Content-CRC32: <zlib.crc32 的 8 位小写十六进制>`、
   `Content-Type: application/octet-stream`（`Content-Disposition: attachment; filename=...` 与 `X-Storage-U` 均实测可省略）。
   成功响应：`{"code":2000}`。
4. **`POST /top/v1?Action=CommitImageUpload&Version=2018-08-01&ServiceId=<service_id>`**（Content-Type: `application/json`）
   请求体：`{"SessionKey": "<base64 JSON，含 storeInfos/StoreUri+Auth、uploadHost、uri>"}`——网页版直接复用第 2 步返回里的 SessionKey 结构（见抓包 `0258-v1.json` 的请求体）。
   **同样要做 SigV4 签名**（对 POST body 的 SHA256 签名）。
   响应：`Result.Results[0].Uri` = StoreUri 且 `UriStatus == 2000` 表示上传成功；`Result.PluginResult[0]` 含图片元信息（宽/高）。
   fixture：`commit_upload_success.json`
5. **`POST /alice/message/pre_handle_v2_without_conv`**（Content-Type: 标准 `application/json`，抓包确认）
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

登录成功后出现的关键 Cookie：**`sessionid`**（另有 `sessionid_ss`、`sid_tt` 等）。
（已从浏览器 profile 直接提取验证：`sessionid`、`sessionid_ss`、`sid_guard`、`sid_tt`、`ttwid`、`passport_csrf_token` 等均存在。）

### 有效期：`sid_guard` 声明 30 天硬过期（2026-10-17 补充）

`sid_guard` 的值是 **URL 编码**的（`|` 写作 `%7C`），解码后为四段：

```
<session_id>|<签发时间戳(秒)>|<TTL(秒)>|<到期日 GMT>
99af…9e4a%7C1787101589%7C2592000%7CFri,+18-Sep-2026+01:06:29+GMT
```

- TTL 实测 `2592000` 秒 = **30 天**；`签发时间 + TTL` 与第 4 段的 GMT 日期一致，可交叉校验。
- 该 Cookie 的签发时间与"登录落盘时间"只差几秒 —— 说明 **会话不会因为持续调用而滚动续期**，到点即失效，之后所有请求都返回 `710012001`（见 §未登录特征）。
- 因此**只看 `sessionid` 是否存在判断登录态是不够的**（旧会话同样存在）。判定"是否新登录"必须用 `sid_guard` 的签发时间：新签发的 `到期时间` 必须晚于旧凭证的 `到期时间`，且距当前至少还有一段有效期（实现见 `session_refresh.is_fresh_session`，阈值 `MIN_FRESH_SESSION_TTL`）。
- 本地可据此提前发现过期：`credentials.CredentialStore.session_expires_at / is_expired()`，无需发请求。

## §尺寸映射

`chat_ability.ability_param` 内嵌 JSON 的 `ratio` 字段控制画幅，抓包样本值为 `"auto"`。网页版可选值为常见比例字符串：`"1:1"`、`"4:3"`、`"3:4"`、`"16:9"`、`"9:16"`、`"auto"`（按网页版 UI 的可选项；除 auto 外未逐一抓包验证）。
OpenAI 风格 `size`（`"宽x高"`）→ ratio 的映射：计算宽高比，取最接近的上表比例；无法解析时用 `"auto"`。
`model` 固定 `"Seedream 4.5"`。

**size 注入提示词（实测有效）**：`chat_ability.ability_param` 里的 `ratio` 实测不总是生效（请求 1:1 曾返回 16:9 图）。客户端会把 size 转成自然语言附在提示词后：可解析时附 `，图片比例为X:Y，图片尺寸为WxH`；实测 `1024x1024` 由此稳定产出 2048x2048 方图。

## §运行时注意事项（重放实验结论）

1. **ID 去重**：`local_message_id`/`block_id`/`unique_key`/`local_conversation_id` 必须每次新生成，否则服务端去重返回旧会话。
2. **设备参数是风控关键**：只带最小公共参数重放会立即 `STREAM_ERROR 710022004`（滑块验证）；补齐 `device_id`/`web_id`/`tea_uuid`（+ 随机 `web_tab_id`）后正常。这三个值通过扫码登录流程从浏览器页面请求 URL 中提取（见 `session_refresh.extract_device_params`），存于 `credentials.json` 的 `device` 字段。风控一旦触发，需要人工在浏览器正常发一条消息（可能需完成滑块）才会恢复。
3. **SSE 只需浅解析**：逐行读 `event:`/`data:`，识别 `SSE_ACK`/`DOWNLINK_CMD`（取 conversation_id）、`STREAM_ERROR`（报错）、`SSE_REPLY_END`（结束）；其余忽略。
4. 生成耗时参考：文生图约 20-40 秒（含排队）；轮询间隔建议 2 秒。
5. 每次提示词默认产出 4 张图（ creations 数组 4 项）。
6. **尺寸控制靠提示词**：`chat_ability` 的 ratio 字段实测不总是生效；把"图片比例为1:1，图片尺寸为1024x1024"写进提示词后稳定产出方图（见 §尺寸映射）。
7. **扫码续期的判定不能只看 `sessionid` 是否存在**：旧 `sessionid` 同样存在，"看到 sessionid 就算登录成功"会把残留的旧会话当成本次登录结果落盘。改为以 `sid_guard` 的签发时间为准（新到期时间必须晚于旧凭证，且至少还剩 1 小时），并显式用空白 context（`storage_state={"cookies": [], "origins": []}`）启动。实测默认 `browser.new_context()` 本身不会带上磁盘 Cookie（无 `user_data_dir`），所以旧实现并没有"自我满足"；这条约束是防回归：一旦有人改成持久化 profile，旧会话就会被立刻误判。回归测试：`tests/test_session_refresh.py::test_stale_session_is_never_taken_as_login`。
8. **服务端下发的 `Set-Cookie` 目前未回写**：豆包会话若在浏览期间续签/轮换，httpx 客户端拿到的 `Set-Cookie` 会被丢弃（客户端每次请求都用磁盘快照）。未观察到实际影响（30 天硬过期，无滑动续期），但若后续出现"明明没过期却 401"，优先查这里。
9. **保活（keepalive）与"页面访问能否滚动续签"**：`launch_persistent_context(user_data_dir=...)` 常驻 profile 后，定时（默认 6h）打开 `https://www.doubao.com/chat/` 并比对 `sid_guard` 签发时间。实测记录（2026-10-17，未登录 profile）：
   - 持久化 profile 在有头/无头下都能正常启动并加载豆包页面（有头 7.2s、无头 6.7s；页面标题 `豆包 - 字节跳动旗下 AI 智能助手`，右上角有"登录"入口即扫码位）；
   - **`device_id`/`web_id`/`tea_uuid` 在页面加载时就会出现在 `/chat/completion` 请求 URL 上**，无需登录即可捕获 —— 这也是 `login_with_profile` 里 device 参数的来源；
   - 未登录时 context 里只有 `ttwid`/`passport_csrf_token`/`s_v_web_id` 等匿名 Cookie，**没有 `sessionid`**，所以"没登录"能被明确识别，不会误判。
   - **仍未验证**：带着合法 `sessionid` 访问页面时服务端会不会重新签发（滚动续签）。这是保活能否"免扫码"的唯一前提；代码不做假设 —— 续了才落盘，没续只记日志。第一次成功登录后的保活日志即为判据。
