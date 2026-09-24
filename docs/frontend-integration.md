# 人机对话台（team_chat）· 前后端对接说明

- **版本**：v1.0（首版交付，2026-09-21）
- **交付人**：hermes（后端 + 服务落点 owner）
- **接收人**：openclaw-nb（前端 owner）
- **依据会议**：`meet-team_chat-handoff-20260921`（纪要：`agent-team/reports/meet-team_chat-handoff-20260921.md`）
- **契约基线**：`API契约-人机对话台-v1.md`（v1.0，已冻结）+ `API契约-人机对话台-v1.1-前端增强增补.md`（含 §H 落点收口）
- **对方回执**：`API契约-v1.1-OpenClaw回执.md`

> **边界**：本文档只写**接口与落点**。前端产物 `agent-team/ui/` 归 openclaw-nb，后端不碰前端代码；后端代码 `chat_svc.py` 与部署归 hermes，前端不碰。
> **效力**：本文档是"怎么用契约"的操作说明。**与契约冲突时以契约（v1.0 + v1.1 增补）为准**；契约改动走 v1.2 双方确认。
> **本文件不含任何凭据**：所有口令/密钥只在服务端环境变量与受保护文件里，不下发浏览器。

---

## 0. 一句话

龙虾机 `<host>:9980`（systemd `agent-team-chat`）**同源**对外提供 **JSON API + 前端静态产物**；
前端零构建、只用相对路径、鉴权走 `view_auth` cookie，SSE 全程只开 **1 条汇总流**。

---

## 1. 服务落点与同源托管

### 1.1 落点速查

| 项 | 值 |
|---|---|
| 服务名 | `agent-team-chat`（龙虾 systemd，`agent-team-chat.service`） |
| 监听 | `<host>:9980`（tailnet）+ `127.0.0.1:9980` |
| 协议 | v1 只支持 `http`（http/2 由 CF 隧道侧提供） |
| 静态根 | `agent-team/ui/`（交付物，零构建） |
| 数据库 | 龙虾 PG `127.0.0.1:9950`，表 `chat_sessions` / `chat_messages` / `chat_observers`（**浏览器不直连 PG/NATS/黑板**） |
| 鉴权 | 沿用看板 `view_auth` 会话 cookie（同源、HttpOnly） |
| 公网 | **默认不开**；需要时在同一条 CF 隧道加 `chat.example.com → 127.0.0.1:9980`，不新建隧道、不动 `board.example.com` |
| 深链 | `GET /?session=<sid>`、`GET /?session=<sid>&pane=<n>`（前端自行解析） |

### 1.2 路由优先级（冻结）

```
/healthz、/api/*、/login、/account   → 后端动态处理（优先级最高）
静态文件命中（ui/ 下真实存在的文件）  → 直接返回文件
其它 GET                              → index.html（SPA fallback，200）
其它方法                              → 501 not_implemented
```

### 1.3 后端配合清单（8 条 · 本会确认，作为实现依据）

| # | 要求 | 验收方式 |
|---|---|---|
| 1 | 静态根 = `agent-team/ui/`，**排除** `serve.py` / `mock.js`（开发辅助件，不纳入托管） | 请求 `/serve.py`、`/mock.js` 不得返回源码（404 或 fallback 到 index.html） |
| 2 | `/api/*`、`/healthz`、`/login`、`/account` **优先于静态**；静态不得覆盖 `/api/` 前缀 | `GET /api/does-not-exist` 仍返 501 信封，**不得**被 SPA fallback 吞成 200 HTML |
| 3 | SPA fallback：非 API 命中、非静态文件的 `GET` → 回 `index.html`（**200，非 404**） | `GET /?session=dm_xxx` 返回 index.html，状态 200 |
| 4 | `/login`、`/account` **复用 `view_auth`** 既有口径，前端不自建登录页 | 未登录访问 `/api/*` → 401 → 跳 `/login` |
| 5 | 缓存：`index.html` 走 `no-cache`；v1 阶段 `*.js` / `*.css` 同样 `no-cache`（零构建、文件名不变） | 响应头核验 |
| 6 | SSE 同源：`Content-Type: text/event-stream`、`X-Accel-Buffering: no`、**禁用压缩缓冲** | 事件逐条到达，不被攒批 |
| 7 | MIME：`.js` = `application/javascript`、`.css` = `text/css`（防 Windows 注册表把 `.js` 报成 `text/plain`） | `Content-Type` 核验（ES module 场景更严） |
| 8 | 后端**不代管前端构建**：交付物就是纯静态文件，前端改完直接落 `ui/` | —— |

### 1.4 退路（**不启用**）

「前端独立服务 + 反代 `/api`」需额外配 CORS + cookie `SameSite`/`Domain`，本会决定**按同源做，不启用退路**。前端一律写相对路径，不写 host、不拼端口。

---

## 2. 当前实现状态（实测：2026-09-21 10:30 北京时间，tailnet 直连）

### 2.1 端点状态表

| 方法 | 路径 | 状态 | 实测证据（本文档撰写时） |
|---|---|---|---|
| GET | `/healthz` | ✅ 可用 | `200`，`{"ok":true,"service":"agent-team-chat","version":"1.0","phase":"read-only","db":true,"members":1,"uptime_s":41592,...}` |
| GET | `/api/whoami` | ✅ 可用 | `200`，tailnet 直连时 `role:"owner"`、`source:"tailnet"`；浏览器走 cookie 时为登录用户 |
| GET | `/api/agents` | ⚠️ 可用但字段未满 | `200`，`agents[0] = {id:"lobster",display:"龙虾",online:null,busy:null,last_hb_at:null,meta:{...}}`；v1.1 §B.1 的 `presence/heartbeat/workload/sessions/caps` **尚未下发** |
| GET | `/api/sessions` | ⚠️ 可用但字段未满 | `200`，`count:3`；条目为 v1.0 形状 + `ref`；**缺** v1.1 的 `groups` / `last_seq` / `last_author` / `last_activity_at` / `context` |
| GET | `/api/sessions/{sid}` | ✅ 可用 | `200`，`{"ok":true,"session":{...}}` |
| GET | `/api/sessions/{sid}/messages` | ✅ 可用 | `200`，`{"ok":true,"session_id":"...","messages":[],"has_more":false,"next_before_seq":null}`；不存在/无权 → `404 not_found` |
| GET | `/api/overview` | ⛔ 501 | `501 {"ok":false,"error":{"code":"not_implemented",...}}` |
| GET | `/api/stream` | ⛔ 501 | 同上（SSE 属第二档） |
| GET | `/api/sessions/{sid}/meeting` `/turns` `/participants` `/observers` `/refs` | ⛔ 501 | 同上（属第四/五档） |
| POST/PATCH/DELETE | `POST /api/sessions`、`POST …/messages`、`PATCH …/sessions/{sid}`、`POST /api/meetings`、`POST …/observers` 等 | ⛔ 501 | 写端点本版**一律 501** |
| GET | `/`、`/index.html`、`/login`、`/account` | ⛔ 404（**尚未实现**） | 非 `/api/` 路径当前一律 `404 not_found`；静态托管与 `/login`、`/account` 按排期 **2026-09-22** 落地，此前前端仍用本地 `ui/serve.py` 预览 |

> **结论**：接口契约（路径/字段）**已定死、不阻塞前端开工**；当前可直接联调的是**只读四项**（`healthz` / `whoami` / `agents` / `sessions` / `messages`），其余按 **501 → 按钮置灰/演示模式**处理。

### 2.2 501 语义（**不是 404**）

未实现端点（含未实现方法）统一返 **501**，带统一错误信封，前端据此开关按钮：

```http
GET /api/overview HTTP/1.1

HTTP/1.1 501 Not Implemented
Content-Type: application/json; charset=utf-8

{"ok":false,"error":{"code":"not_implemented","message":"本版尚未实现（契约 §12.4 实现顺序中）：GET /api/overview"}}
```

前端判定口径（`ui/api.js` 已实现）：`error.code === 'not_implemented'` **或** `status === 501` → `notImplemented`。

⚠️ **注意区分**：`404 not_found` 表示"不存在**或无权可见**"（如不存在的会话），**不是**"未实现"；两者不可混用。

### 2.3 已知差异（gap）与排期

| # | 差异 | 影响前端 | 排期 |
|---|---|---|---|
| 1 | `/api/agents` 的 `online/busy/presence/heartbeat/workload` 均未下发（现在恒为 `null`） | 花名册的在线态点只能用"⚠ 未心跳"占位 | 第二档随只读面补全（v1.1 §B.1） |
| 2 | `/api/sessions` 无 `groups` 分组与 `last_seq` 等 v1.1 字段 | Tab 分组/LocalUnread 只能用 `last_message` 或 mock | 第二档 |
| 3 | `unread` 恒为 `null` | 未读一律走前端本地记账（契约 §B.5.1 `mode:"client"`，本就是设计口径） | 不需后端改动 |
| 4 | `session.context` / `message.refs` / `meeting.context_refs` 未出现 | 右栏先渲染空数组占位 | 随对应端点补字段（字段必须出现，值可为 `[]`） |
| 5 | 静态托管未开（`/` = 404） | 暂时只能用 `ui/serve.py` 本地预览 | **2026-09-22** |

---

## 3. 端点对接要点

统一规则（契约 §1）：前缀 `/api`；请求/响应 `application/json; charset=utf-8`（SSE 除外）；时间一律 ISO8601 带 `+08:00`，字段名 `*_at`；分页一律游标式（`limit` + `before_seq`/`cursor`），**不用 offset**；未知字段双方都必须忽略。

### 3.1 `GET /healthz`（公开，不带 `/api`）

```json
{"ok":true,"service":"agent-team-chat","version":"1.0","phase":"read-only","db":true,
 "members":1,"uptime_s":41592,"server_time":"2026-09-21T10:30:27+08:00"}
```

用途：连接态灯、`phase` 判断后端处于哪一档（`read-only` → 后续 `write` / `stream`）。

### 3.2 `GET /api/whoami`

```json
{"ok":true,"user":{"name":"tailnet","display":"tailnet","role":"owner","role_cn":"主人","source":"tailnet"},
 "caps":{"can_write":true,"can_create_meeting":true,"can_observe":true,"can_admin":true},
 "server_time":"2026-09-21T10:30:35+08:00"}
```

- 角色三级：`viewer(0) < member(1) < owner(2)`；刚哥 = `owner`。
- **能力位以 `caps` 为准**，前端不要用角色自己推按钮可用性。

### 3.3 `GET /api/agents`

当前实际返回（v1.0 形状）：

```json
{"ok":true,"agents":[
  {"id":"lobster","display":"龙虾","online":null,"busy":null,"last_hb_at":null,
   "meta":{"node_id":"lobster","host":"<host>","model":"openclaw/default","description":"lobster 助手（云）"}}
],"server_time":"2026-09-21T10:30:35+08:00"}
```

目标形状（v1.1 §B.1，第二档补齐）：`order` / `presence{state,since,reason}` / `heartbeat{last_at,age_s,interval_s,missed,host,uptime_s,agent_version,source}` / `workload{running,queued,max_concurrent,queue_depth}` / `sessions{active,dm,meet,current[]}` / `caps{dm,meet,observe}`。

纪律：

- **`online:null` = "未知"，不是"离线"**：本版未接 NATS 心跳，`online`/`busy`/`last_hb_at` 恒为 `null`。前端遇到 `null` 必须显示「未知 / 未接入」，**不得渲染成「离线」**（否则会误导成人不在）。
- **前端不得本地推断在线态**（不许用 `last_hb_at` 自己减时间）；`presence.state` 由服务端算好下发。
- `presence.state` 五态（冻结阈值 75s / 180s）：`busy` / `queued` / `online` / `degraded` / `offline`。
- `offline` 只做"⚠ 未心跳"提示，**不得**断言"人不在"（云机成员不在线按缺陷追）。
- 兜底轮询：`GET /api/agents?since=<version>` → 无变化 **304**（带 `ETag: "v<version>"`），最小间隔 ≥15s。

### 3.4 `GET /api/sessions`

查询参数：`kind`、`member`、`observing=1`、`state`、`limit`（缺省 20）、`cursor`。

当前实际返回（节选）：

```json
{"ok":true,"count":3,"next_cursor":null,"sessions":[
  {"session_id":"dm_20260920_e2e001","kind":"dm","title":"e2e 自测会话 · 后端只读验收",
   "sensitivity":"internal","members":[{"id":"lobster","display":"龙虾"}],
   "created_by":"hermes","created_at":"2026-09-20T22:57:39+08:00","updated_at":"2026-09-20T22:57:39+08:00",
   "state":"open","ref":null,
   "last_message":{"seq":10,"author":{"type":"agent","id":"lobster","display":"龙虾"},
                   "excerpt":"e2e 第 10 条","created_at":"2026-09-20T22:57:40+08:00"},
   "unread":null,"observing":false,"my_role":"owner"}
],"server_time":"2026-09-21T10:30:43+08:00"}
```

- `kind=meet` 条目额外带 `meeting` 块（`goal/state/round_no/turn_count/expected_count/present_count/absent/agenda_total/agenda_done`）；无会议真源时该块可能为空对象 `{}`。
- v1.1 §B.2 目标形状：顶层 `groups[]`（`key:"member:lobster"` / `key:"meetings"`）+ 每条会话 `last_seq` / `last_author` / `last_activity_at` / `group_key` / `context`。
- **`last_seq` 是前端算本地未读的唯一依据**；服务端 `unread` 只是初值（v1 不做服务端已读游标）。
- **无权即不可见**：无权会话不出现在列表里；直取单会话返 `404`。

### 3.5 `GET /api/sessions/{sid}`

```json
{"ok":true,"session":{"session_id":"meet_20260920_e2e001","kind":"meet","title":"e2e 会议会话",
 "sensitivity":"internal","members":[{"id":"lobster","display":"龙虾"}],
 "created_by":"hermes","created_at":"2026-09-20T22:57:40+08:00","updated_at":"2026-09-20T22:57:40+08:00",
 "state":"open","ref":{"table":"roundtable_sessions","id":"rt_20260920_notexist"},"meeting":{}}}
```

### 3.6 `GET /api/sessions/{sid}/messages`

参数：`limit`（夹 `1..200`，缺省 50）、`before_seq`（取 `seq < before_seq` 的**最近** N 条，向上翻页）、`after_seq`（取 `seq > after_seq` 的**最早** N 条，补缺口）。

```json
{"ok":true,"session_id":"dm_20260920_e2e001","messages":[ /* §5 消息对象，按 seq 升序 */ ],
 "has_more":false,"next_before_seq":null,"server_time":"2026-09-21T10:30:45+08:00"}
```

消息对象要点（契约 v1 §5，dm 与 meet **共用同一形状**，前端无感）：

```json
{"message_id":"msg_20260920183012_9ab7xz","session_id":"dm_20260920_k7f3qa","seq":42,
 "author":{"type":"human|agent|system","id":"<human-id>","display":"陈刚"},
 "kind":"text","text":"…","status":"done","created_at":"2026-09-20T18:30:12+08:00","reply_to":null,
 "run":{"agent":"lobster","model":"lobster-cli","elapsed_ms":12345,"queued_ms":800},
 "meeting":{"round_no":2,"role":"speaker|moderator|system","stance":"agree|object|abstain|null",
            "turn_id":418,"ref":"roundtable_turns:418"},
 "client_msg_id":"c-7f2a"}
```

- `kind` v1 只有 `text`；`status ∈ queued|running|done|failed|canceled`。
- **只有 `done/failed/canceled` 落库**；`queued/running` 只在内存、**只经 SSE 可见**（历史接口不会返回）。
- meet 场正文是**投影**（真源 `roundtable_turns`），`text` 从真源取，前端**不得写回**。

### 3.7 写端点（第二档起开放，现在一律 501）

**① `POST /api/sessions/{sid}/messages`**（契约 §5.1）

```json
请求: {"text":"…","client_msg_id":"c-7f2a","wait":false,"wait_s":30}
202: {"ok":true,"accepted":true,"message":{"message_id":"…","seq":42,"status":"queued"},
      "queue_position":1,"session_queue_depth":1}
```

- `client_msg_id` **必填**：同 id 重发 → 返回**同一条**消息（不重复执行），前端乐观 UI 与去重都靠它。
- `wait=false`（缺省、推荐）：立即 `202`，后续进度与最终回复**只经 SSE**（`message` / `message.done` / `message.failed`）。
- `wait=true`：同步等最终回复，最长 `wait_s`（缺省 30，夹 `1..60`）；等到 → `200` 完整消息；超时 → `202`（**不是错误**），改由 SSE 收尾。
- **`202` 只代表"已受理"**，不代表 Agent 已开跑。
- `text` 非空、≤8000 字，超限 `422 text_too_long`。

**② `DELETE /api/sessions/{sid}/messages/{mid}`**：`queued` → `204` + SSE `message.canceled`；`running` → `409 cannot_cancel_running`；已 `done` → `409 already_done`。

**③ 其余**：`POST /api/sessions`（建 dm，`kind != dm` → `422 use_meetings_endpoint`，幂等口子 `client_ref`）、`PATCH /api/sessions/{sid}`（改名 `title`）、`POST /api/meetings`（四要素门禁，§6.1 门禁码）、`POST/DELETE /api/sessions/{sid}/observers`、`POST /api/sessions/{sid}/speak-requests`（**预留 501**）。

### 3.8 v1.1 新增端点（第三/四/五档开放）

`GET /api/overview`（顶部全局态，`?since=` → 304）、`GET /api/sessions/{sid}/meeting`（会议快照，带 `version`）、`GET /api/sessions/{sid}/turns`（游标 `turn_id`）、`GET /api/sessions/{sid}/participants`、`GET /api/sessions/{sid}/observers`（增 `visibility`/`capabilities`/`speak`）、`GET /api/sessions/{sid}/refs`（**501 预留**，但 `context`/`refs`/`context_refs` 字段必须出现，空数组占位）。
字段全集见 v1.1 增补 §B / §D，本文不复制。

---

## 4. 错误码与前端处理

统一信封：`{"ok":false,"error":{"code":"busy","message":"…","retry_after_s":5}}`（既有 9920/9970 是扁平字符串信封，新服务**只**用对象信封）。

| HTTP | `code` | 前端处理 |
|---|---|---|
| 400 | `bad_request` | 提示参数错误 |
| 401 | `unauthorized` | 跳 `/login` |
| 403 | `forbidden` / `observer_readonly` | 旁听视图写操作被拒 → 按钮保持只读态 |
| 404 | `not_found` | 当"不存在"处理（**不要**弹 403 文案），从 Tab 列表移除 |
| 409 | `conflict` / `cannot_cancel_running` / `already_done` | 提示状态冲突 |
| 422 | `missing_goal` / `missing_agenda` / `agenda_item_missing_expected` / `missing_members` / `unknown_member` / `moderator_not_in_members` / `invalid_deadline` / `text_too_long` / `use_meetings_endpoint` | 建会表单本地已做同样校验（§6.1 六条），服务端为准 |
| 429 | `busy` / `queue_full` / `queue_timeout` | **可重试**：按 `retry_after_s`（缺省 5s）指数退避，封顶 30s；**必须显示队列态**，否则用户以为卡死 |
| 500 | `internal` | 提示服务端异常 |
| 501 | `not_implemented` | **置灰按钮 / 进演示模式**（不是错误弹窗） |
| 502 | `backend_error` | Agent CLI 执行异常 |
| 503 | `upstream_unavailable` | 成员上游/中枢不可达 |

并发口径（契约 §7，冻结）：会话内**严格串行**（后到进 FIFO 队列，深度 >20 → `429 queue_full`；单条排队 >120s → 落 `failed` + `429 queue_timeout`）；成员级跨会话并行上限 `member_max_concurrent` **默认 2**（超限 `429 busy` + `Retry-After`）；多场会可并存，同一成员被多场点名 → 排队不 abort。

---

## 5. SSE 对接

### 5.1 连接口径（冻结）

- **只有 1 条汇总流**：`GET /api/stream`（多 Tab / 双窗格按事件体 `session_id` 分发）。HTTP/1.1 上限 6 连接，**不因开窗格增连**。
- 可选过滤：`GET /api/stream?sessions=<sid1>,<sid2>`（缺省 = 全部可见会话）；单会话流 `GET /api/sessions/{sid}/stream` 存在，但推荐只用汇总流。
- **鉴权走 cookie**（`EventSource` 不能带自定义头）：`new EventSource(url, {withCredentials:true})`；**同源相对路径**。
- 续推：URL `since_seq` 优先，缺省回退浏览器自动带的 `Last-Event-ID`；两者都没有 = 从"当前最新"开始（**不回放全历史**，历史一律走 §3.6）。
- 事件 `id:` = `seq`；快照类事件（`hello`/`roster`/`overview`/`meeting.state`/`speaker`/`convergence`/`agenda`/`observers`）**不写 `id:`**，重连不重放。
- 心跳：每 **15s** 一条注释行 `: hb`（防 CF 100s idle 断流）。
- 传输要求（后端侧）：`Content-Type: text/event-stream`、`X-Accel-Buffering: no`、**禁压缩缓冲**，否则事件被攒批。
- 会议推流延迟承诺：一条发言落库 → 上屏 **≤ 2s**。

### 5.2 事件表（v1 §6.3 + v1.1 §B.1/B.3.3/B.5.2，**冻结**）

| `event:` | `data` 关键字段 | 触发 |
|---|---|---|
| `hello` | `{session_id?, since_seq, server_ts, stream:"all"\|"session", subscribed:[sid…]}` + `roster`/`overview`/`meeting` 快照 | 连接建立后第一帧 |
| `roster` | `{version, server_ts, agents:[…]}` | 任一成员 presence/workload/sessions 变化（整表，去抖 ≤1s） |
| `overview` | `{version, server_ts, …计数块…}` | 上下线/忙闲/开会散会/会话增删（去抖 ≤1s） |
| `message` | 完整消息对象，`status ∈ {queued,running}` | 受理/开跑（占位） |
| `message.done` | 完整消息对象，`status=done` | 最终回复落定（**前端以此替换占位**） |
| `message.failed` | 完整消息对象，`status=failed` + `error.code` | 失败/不可达/排队超时 |
| `message.canceled` | `{message_id,seq}` | 排队中被取消 |
| `status` | `{member, session_id, state, queue_position?, queue_depth?, retry_after_s?}` | 成员忙/排/离线变化 |
| `turn` | `{session_id, round_no, agenda_index, agenda_total, phase}` | 会议轮次/议程推进 |
| `conclusion` | `{session_id, scope:"agenda"\|"summary", agenda_index?, text, confirm_summary}` | 议程结论 / 总结论 |
| `meeting.state` | `{session_id, version, state, progress, agenda, speak_queue}` | 会议态粗变化；连接后立即发一次；去抖 ≤1s |
| `speaker` | `{session_id, version, round_no, speaker, speak_queue}` | 发言人切换（含开新一轮/跳过缺席者） |
| `turn.spoke` | `{session_id, version, …单条发言对象…}` | **一条发言落库即推**（≤2s） |
| `convergence` | `{session_id, version, …convergence 块…}` | 收敛票数/阶段变化 |
| `agenda` | `{session_id, version, seq, status, conclusion?, conclusion_entry?}` | 议程项推进/出结论 |
| `meeting.ended` | `{session_id, version, state, ended_at, conclusion_text, conclusion_entry, followups}` | 会终（任意终态） |
| `session.created` / `session.updated` / `session.closed` | 会话对象 / `{session_id,title?,state?,members?}` / `{session_id,state,ended_at}` | **仅汇总流**：会话增/改/闭 |
| `observers` | `{session_id, joined:[…], left:[…], count}` | 旁听进出 |
| `error` | `{session_id?, code, message}` | 流内错误（**连接不断**） |
| `context` | —— | **预留不发** |
| `message.delta` | —— | **v1 不做，预留不发**（"正在输入"用 `message(status=running)` 表达） |

纪律：发言类事件必须自带 `member`/`display`/`stance`/`turn_id`/`round_no`，**前端一行一渲染，不许回查**。

### 5.3 重连顺序（冻结）

```
① GET 快照（GET /api/sessions/{sid}/meeting，或 /api/agents、/api/overview）
② 带 since_seq 连流补增量（EventSource + since_seq）
③ 丢弃 version ≤ 快照 version 的会议态事件
```

### 5.4 前端分派建议

`api.js` 的 `KNOWN_EVENTS` 已列全（v1 + v1.1）；`Stream` 已实现 `since_seq` 推进、`Last-Event-ID` 记录、1s→30s 指数退避重连、`onState` 连接态回调。建议再补：按 `session_id` 分发到窗口/窗格的注册表，会议态事件按 `version` 判重。

---

## 6. 关键时序

### 6.1 发消息（`wait=false`，推荐路径）

```
前端                        chat_svc                     cli_backend/Agent
 |  POST …/messages (client_msg_id)  |                          |
 |---------------------------------->|  入会话 FIFO 队列        |
 |  202 {accepted, message.seq, queue_position}                 |
 |<----------------------------------|                          |
 |  (SSE) message {status:queued}    |                          |
 |<----------------------------------|                          |
 |  (SSE) status {state:running}     |--- 派发 --->            |
 |<----------------------------------|                          |
 |  (SSE) message {status:running}   |                          |
 |<----------------------------------|                          |
 |  (SSE) message.done {status:done} |<-- 结果落库 + 推流 -->   |
 |<----------------------------------|                          |
```

失败/超时：`message.failed`（带 `error.code`）；排队被取消：`message.canceled`。**`202` 不等于已开跑**。

### 6.2 会议推流（chat 服务只读真源 + 投影，`roundtable.py` 零改动）

```
roundtable.py ──写──> roundtable_turns (真源, 1s 轮询增量 turn_id > 游标)
                                   |
                        chat_svc 投影成统一消息形状
                                   |
              (SSE) turn.spoke / speaker / meeting.state / convergence / agenda
                                   |
                                前端会议视图（≤2s 上屏）
```

### 6.3 断线重连

```
EventSource onerror → 退避(1s→30s) → GET 快照 → EventSource(?since_seq=<最后 seq>)
                                    → 丢弃 version ≤ 快照 version 的会议态事件
```

---

## 7. 前端联调清单（按 `ui/api.js` 方法名对齐）

| `api.js` 方法 | 端点 | 现在可用？ |
|---|---|---|
| `health()` | `GET /healthz` | ✅ |
| `whoami()` | `GET /api/whoami` | ✅ |
| `agents(since)` | `GET /api/agents[?since=]` | ✅（字段未满） |
| `sessions(q)` | `GET /api/sessions` | ✅（无 `groups`） |
| `session(sid)` | `GET /api/sessions/{sid}` | ✅ |
| `messages(sid, q)` | `GET …/messages` | ✅ |
| `overview(since)` | `GET /api/overview` | ⛔ 501 |
| `meeting(sid)` / `turns(sid,q)` / `participants(sid)` / `observers(sid)` / `refs(sid)` | v1.1 新增 | ⛔ 501 |
| `sendMessage(sid, body)` | `POST …/messages` | ⛔ 501（第二档） |
| `createDm(body)` / `patchSession(sid, body)` | `POST /api/sessions` / `PATCH …` | ⛔ 501（第三档） |
| `TC.Stream` | `GET /api/stream` | ⛔ 501（第二档） |

**看到 501 的正确姿势**：按钮置灰 + tooltip「后端第 N 档上线后可用」，或直接进 `?mock=1` 演示模式（前端已有自动降级）。

---

## 8. 后端排期（契约 §12.4 顺序）

| 档 | 内容 | 截止 | 状态 |
|---|---|---|---|
| ① | 只读端点：`/healthz`、`/api/whoami`、`/api/agents`、`/api/sessions`、`/api/sessions/{sid}`、`…/messages` | —— | ✅ 已上线 |
| ①.5 | **同源静态托管**（本文 §1.3 八条）+ **服务端实现 `/login`、`/account`**（复用 `view_auth` 既有口径） | **2026-09-22** | ⏳ 待做 |
| ② | `POST messages` + SSE（`/api/stream`、`/api/sessions/{sid}/stream`）+ `agents`/`sessions` 的 v1.1 字段补齐 | 按契约 §12.4 | ⏳ |
| ③ | `POST /api/sessions`、`PATCH /api/sessions/{sid}` | 同上 | ⏳ |
| ④ | `POST /api/meetings` + 会议推流（`meeting`/`turns`/`turn.spoke` …） | 同上 | ⏳ |
| ⑤ | 旁听（`observers`/`participants` 端点 + 只读订阅，写操作 `403 observer_readonly`） | 同上 | ⏳ |

**交付方式**：每档上线后，我在本文档**增量补事件名与字段**并回报；契约字段/路径/事件名不改，要改走 v1.2。

---

## 9. 变更记录

| 日期 | 版本 | 内容 |
|---|---|---|
| 2026-09-21 | v1.0 | 首版：落点 + 同源托管 8 条 + 端点对接要点 + 错误码 + SSE 事件表/时序 + 联调清单 + 排期。实测基线：`<host>:9980`，`phase=read-only`，静态托管未开。 |

---

_本文件由 hermes（后端 owner）维护；前端侧发现字段/口径不一致，直接在 A2A 抬一次短会，别自己猜。_
