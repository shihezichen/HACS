# 人机对话台（team_chat）API 契约 v1.0 —— 已冻结

冻结时间：2026-09-20（北京时间）　冻结人：Hermes 总控（通信系统责任田）
范围：人（刚哥）与团队 Agent 的网页对话台 —— 会话枚举/命名/取历史、发消息、建会、会话级并发、会议 SSE 推流、旁听只读订阅。
前端 owner：陈刚的OpenClaw（本机 main）；后端与服务落点 owner：Hermes 总控。

冻结规则：契约 v1 内**字段名/路径/事件名/错误码不得单方改动**；需要变更则提 v1.1 并双方确认，前端不得依赖未写入本文的字段。

## 0. 三件已拍板事项在本契约中的落法

| 拍板 | 契约落法 |
|---|---|
| 聊天台不走公网 | 服务默认**只绑 tailnet + loopback**（`<host>:9980` / `127.0.0.1:9980`），公网零新增暴露面 |
| 外网手机接入沿用看板方案、不新开通道 | 需要时在**同一条** Cloudflare 命名隧道 `<tunnel-id>` 上加一个 hostname `chat.example.com → 127.0.0.1:9980`；**不新建隧道**、不改 `board.example.com` |
| 会话历史落库（PG 新表：会话索引 + 消息元数据） | 新表 `chat_sessions`（索引层）/ `chat_messages`（消息）/ `chat_observers`（旁听）；对已有会议**只存引用不复制正文**（见 §10） |
| 旁听者暂不发言、"申请发言"另做 | 旁听者写操作一律 `403 observer_readonly`；预留 `POST /api/sessions/{sid}/speak-requests` → **501 not_implemented**（前端按钮先置灰） |
| 前端形态：单页多 Tab + 可选新开窗格 | 前端产物放 `agent-team/ui/`，由 chat 服务**同源托管**；深链 `GET /?session=<sid>` 由前端自行处理 |

## 1. 通用规则

- 全部端点前缀 `/api`，请求/响应 `application/json; charset=utf-8`（SSE 端点除外）。
- 服务与前端**同源**：前端只用相对路径，不写 host、不拼端口。
- 鉴权：**沿用看板 `view_auth` 会话 cookie**（同源、HttpOnly）。SSE 用浏览器原生 `EventSource`（不能带自定义头），故**一律走 cookie，不用 Authorization**。
- 时间一律 ISO8601 带 `+08:00`（北京时间），字段名 `*_at`。
- 未知字段：服务端忽略；客户端遇到未知字段必须忽略、不许报错（向后兼容口子）。
- 分页一律游标式（`limit` + `before_seq` / `cursor`），**不用 offset**。

## 2. ID 与命名规范（不透明，客户端不得解析）

| 对象 | 形态 | 说明 |
|---|---|---|
| 会话 `session_id` | `<kind>_<yyyyMMdd>_<6位小写base32>`，如 `dm_20260920_k7f3qa`、`meet_20260920_x2m9bd` | `kind ∈ {dm, meet}`；全局唯一、不可变、用于 URL 与 SSE 频道 |
| 消息 `message_id` | `msg_<yyyyMMddHHmmss>_<6位小写base32>` | 全局唯一 |
| 会话内序号 `seq` | 整数，**会话内从 1 严格递增** | 历史分页与 SSE 断线续推的唯一依据 |
| 会话显示名 `title` | UTF-8 ≤ 120 字 | 可变（`PATCH`），**改名不改 `session_id`**；缺省由服务端生成（dm：`与 <成员显示名> 的对话 · <M月D日 HH:mm>`；meet：`<topic> · <M月D日>`） |
| 成员 id | `agents.json` 的键（如 `lobster`/`top001`/`openclaw`/`claudecode`） | **不是**成员自拟标签 |

`kind` 语义：`dm` = 人 ↔ 单个成员的多轮对话（同一成员可有多条会话）；`meet` = 一场会议（= 现有 `roundtable_sessions` 的一场）。

## 3. 鉴权、角色与可见性

角色沿用 `view_auth` 三级：`viewer(0) < member(1) < owner(2)`；刚哥 = `owner`。

| 动作 | 最低角色 |
|---|---|
| 列会话 / 看会话 / 取历史 / 旁听 / 订阅 SSE | `viewer`（且须满足会话密级） |
| 发消息 / 建会 / 改名 / 加入旁听 | `member` |
| 看旁听名单、删会话 | `owner` |

- 会话密级 `sensitivity ∈ {public, internal}`，**缺省 `internal`**；`viewer` 只能看 `public`。
- **无权即不可见**：无权访问的会话在列表里不出现，直取单会话返回 `404 not_found`（不泄露存在性）。
- 服务端**不向浏览器下发任何凭据**（无 NATS/黑板/PG token）。

## 4. 端点总表

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/whoami` | 当前身份与能力位 |
| GET | `/api/agents` | 成员清单（含在线/忙碌） |
| GET | `/api/sessions` | 会话枚举（dm + meet 合并） |
| POST | `/api/sessions` | 建 **dm** 会话 |
| GET | `/api/sessions/{sid}` | 会话详情 |
| PATCH | `/api/sessions/{sid}` | 改名（`title`） |
| GET | `/api/sessions/{sid}/messages` | 取历史（游标分页） |
| POST | `/api/sessions/{sid}/messages` | 发消息 |
| DELETE | `/api/sessions/{sid}/messages/{mid}` | 取消**排队中**的消息 |
| GET | `/api/sessions/{sid}/stream` | 单会话 SSE |
| GET | `/api/stream` | **汇总 SSE（多 Tab 推荐）** |
| POST | `/api/meetings` | 建会（走会议四要素门禁） |
| POST | `/api/sessions/{sid}/observers` | 加入旁听（只读订阅） |
| DELETE | `/api/sessions/{sid}/observers` | 退出旁听 |
| GET | `/api/sessions/{sid}/observers` | 旁听名单（owner；须登录） |
| POST | `/api/sessions/{sid}/speak-requests` | **预留 → 501** |
| GET | `/healthz` | 健康检查（公开，不带 `/api`） |

### 4.1 GET /api/whoami
```json
{"ok":true,"user":{"name":"<human-id>","role":"owner","role_cn":"主人"},
 "caps":{"can_write":true,"can_create_meeting":true,"can_observe":true,"can_admin":true}}
```

### 4.2 GET /api/agents
```json
{"ok":true,"agents":[
  {"id":"lobster","display":"龙虾","online":true,"busy":false,"last_hb_at":"2026-09-20T18:20:03+08:00",
   "meta":{"host":"iZrj…","model":"…"}}
]}
```
- `online` = 最近 60s 内有 `team.hb.<成员>` 心跳；`busy` = 上游 `cli_backend /healthz` 报该成员在跑任务。
- 前端**不得**用 `online=false` 直接断言"人不在"（云机成员不在线按缺陷追，不是常态）—— 只做"⚠ 未心跳"提示。

### 4.3 GET /api/sessions?kind=&member=&observing=1&state=&limit=20&cursor=
```json
{"ok":true,"count":2,"next_cursor":null,"sessions":[
 {"session_id":"dm_20260920_k7f3qa","kind":"dm","title":"与 龙虾 的对话 · 9月20日 18:20",
  "members":[{"id":"lobster","display":"龙虾"}],"sensitivity":"internal","state":"open",
  "created_by":"<human-id>","created_at":"2026-09-20T18:20:11+08:00","updated_at":"2026-09-20T18:31:02+08:00",
  "last_message":{"seq":12,"author":{"type":"agent","id":"lobster","display":"龙虾"},
                  "excerpt":"…前 80 字…","created_at":"2026-09-20T18:31:02+08:00"},
  "unread":3,"observing":false,"my_role":"owner"}
]}
```
- `kind=meet` 的条目额外带会议块（字段取自既有只读真源，**不复制**）：
```json
 {"meeting":{"goal":"…","state":"running","round_no":3,"turn_count":41,
             "expected_count":4,"present_count":3,"absent":["quantum"],"agenda_total":3,"agenda_done":1}}
```
- `unread` 由前端自行记账；服务端返回的 `unread` 仅供初值（v1 不做服务端已读游标）。

### 4.4 POST /api/sessions （建 dm）
请求：
```json
{"kind":"dm","member":"lobster","title":null,"sensitivity":"internal"}
```
- 幂等口子：`client_ref`（可选，字符串）——同 `member`+`client_ref` 重复提交返回**同一条**会话（200，非 201）。
响应 `201`：`{"ok":true,"session":{…同 4.3 单条…}}`
- `kind != "dm"` → `422 {"error":{"code":"use_meetings_endpoint"}}`（建会必须走 `/api/meetings`）。

### 4.5 GET /api/sessions/{sid}/messages?limit=50&before_seq=&after_seq=
```json
{"ok":true,"session_id":"…","messages":[ /* 见 §5 消息对象，按 seq 升序 */ ],
 "has_more":false,"next_before_seq":null}
```
- `before_seq`：取 `seq < before_seq` 的**最近** `limit` 条（历史向上翻页）。
- `after_seq`：取 `seq > after_seq` 的**最早** `limit` 条（补缺口）。
- `limit` 夹在 `1..200`，缺省 50。
- **dm 消息正文**来自 `chat_messages`；**meet 消息正文**取自会议真源（`roundtable_turns`）——对外形状统一（§5），前端无感。

## 5. 消息对象（统一形状，dm 与 meet 共用）

```json
{"message_id":"msg_20260920183012_9ab7xz","session_id":"dm_20260920_k7f3qa","seq":42,
 "author":{"type":"human|agent|system","id":"<human-id>","display":"陈刚"},
 "kind":"text","text":"…",
 "status":"done",
 "created_at":"2026-09-20T18:30:12+08:00",
 "reply_to":null,
 "run":{"agent":"lobster","model":"lobster-cli","elapsed_ms":12345,"queued_ms":800},
 "meeting":{"round_no":2,"role":"speaker|moderator|system","stance":"agree|object|abstain|null",
            "turn_id":418,"ref":"roundtable_turns:418"},
 "client_msg_id":"c-7f2a"}
```
- `kind`：v1 **只有 `text`**；未来 `file`/`image`（附件上传 v1 不做，端点未开）。
- `status`：`queued | running | done | failed | canceled`。
  - **只有 `done` / `failed` / `canceled` 的消息会落库**；`queued`/`running` 只在内存，且**只经 SSE 可见**（落库表保持 append-only，§10）。
- `meeting.ref` 非空时，`text` 是**从真源取的投影**，不得写回。
- 人发出的消息 `author.type="human"`，`id` = 登录用户名；Agent 回复 `author.type="agent"`，`id` = 成员 id。

### 5.1 POST /api/sessions/{sid}/messages
请求：
```json
{"text":"…","client_msg_id":"c-7f2a","wait":false,"wait_s":30}
```
- `text` 必填、非空、≤ 8000 字；超限 → `422 text_too_long`。
- `client_msg_id` 必填（前端生成，用于去重与乐观 UI；重发同 id → 返回**同一条**消息，不重复执行）。
- `wait=false`（**缺省，推荐**）：立即 `202`：
```json
{"ok":true,"accepted":true,"message":{"message_id":"…","seq":42,"status":"queued"},
 "queue_position":1,"session_queue_depth":1}
```
  后续进度与最终回复**只经 SSE**（`message` / `message.done` / `message.failed`）。
- `wait=true`：同步等最终回复，最长 `wait_s`（缺省 30，夹 `1..60`）；等到 → `200` + 完整消息对象；超时 → `202`，与上同形（**不是错误**），前端改由 SSE 收尾。
- 忙碌/排队语义见 §7。**注意**：`202` 只代表"已受理"，不代表 Agent 已开始跑。

### 5.2 DELETE /api/sessions/{sid}/messages/{mid}
- 目标消息仍在 `queued` → `204`，并推 SSE `message.canceled`。
- 目标 `running` → `409 {"error":{"code":"cannot_cancel_running"}}`（v1 不支持中断运行中的 Agent）。
- 目标已 `done` → `409 {"error":{"code":"already_done"}}`。

## 6. 会议：建会 + SSE 推流

### 6.1 POST /api/meetings（会议四要素门禁；不过就是开不起来）
请求：
```json
{"title":"…","topic":"…","question":"…","goal":"本次要达成什么",
 "agenda":[{"title":"…","expected":"预期产出…"},{"title":"…","expected":"…"}],
 "members":["lobster","top001","openclaw","quantum"],
 "moderator":"hermes","max_rounds":3,"deadline_s":1800,
 "sensitivity":"internal","client_ref":"…"}
```
门禁（缺哪条报哪条，`422`）：
`goal` 空 → `missing_goal`；`agenda` 空 → `missing_agenda`；任一 `agenda[i].expected` 空 → `agenda_item_missing_expected`；`members` 空 → `missing_members`；`members` 含非注册成员 → `unknown_member`；`moderator` 不在 `members` → `moderator_not_in_members`；`deadline_s <= 0` → `invalid_deadline`。
响应 `201`：`{"ok":true,"session":{"session_id":"meet_…","kind":"meet","state":"scheduled","meeting":{"goal":"…","agenda_total":2}}}`

约定：
- 建会成功后，服务端在龙虾本机起 `roundtable.py --session-id <sid> --commit`（四要素与留痕仍由它写自己的表，**责任田不变**）；`state`：`scheduled → running → concluded | incomplete | deferred`。
- **`goal`/`agenda`/`members` 一经落库即冻结，会后不可改**（会议留痕不可篡改）；改名只改对话台 `title` 别名。
- 人在会议场的发言：`202` 受理、落库、推流（作为 `round_no` 之外的插话显示），**v1 不注入 Agent 轮次 prompt**（v2 再谈）。
- 会议真值仍以 `roundtable_sessions` / `roundtable_turns` 为唯一真源；对话台只是**投影 + 推流**。

### 6.2 SSE 推流口径（延迟承诺）
- 推流实现 = chat 服务按 **1s** 轮询会议真源的增量（`turn_id > 上次游标`）+ 本服务的消息事件；**不要求 `roundtable.py` 改一行代码**。
- 承诺：会议中一条发言落库后 **≤ 2s** 出现在 SSE 流里。
- 断线续推：SSE `id:` = `seq`（meet 场为投影后的会话内 seq）；重连时**以 URL 的 `since_seq` 为准**，缺省时回退浏览器自动带的 `Last-Event-ID`；两者都没有 = 从"当前最新"开始（不回放全历史，历史一律走 §4.5）。

### 6.3 SSE 事件表（**冻结**，v1 只发这些）

| `event:` | `data` 关键字段 | 触发 |
|---|---|---|
| `hello` | `{session_id?, since_seq, server_ts, stream:"session"\|"all", subscribed:[sid…]}` | 连接建立后第一帧 |
| `message` | 完整消息对象，`status ∈ {queued, running}` | 受理/开跑（占位用） |
| `message.done` | 完整消息对象，`status=done` | 最终回复落定（**前端以此为准替换占位**） |
| `message.failed` | 完整消息对象，`status=failed`，`error.code` | 执行失败/成员不可达/排队超时 |
| `message.canceled` | `{message_id,seq}` | 排队中被取消 |
| `status` | `{member, session_id, state:"queued"\|"running"\|"idle"\|"offline", queue_position?, queue_depth?, retry_after_s?}` | 成员忙碌/排队/离线变化 |
| `turn` | `{session_id, round_no, agenda_index, agenda_total, phase}` | 会议轮次/议程推进 |
| `conclusion` | `{session_id, scope:"agenda"\|"summary", agenda_index?, text, confirm_summary}` | 议程结论 / 总结论（含当场确认结果） |
| `session.created` | 会话对象（同 4.3 单条） | **仅汇总流**：新会话出现 |
| `session.updated` | `{session_id, title?, state?, members?}` | 改名/成员变化/状态变化 |
| `session.closed` | `{session_id, state, ended_at}` | 会话/会议结束 |
| `observers` | `{session_id, joined:[user…], left:[user…]}` | 旁听进出 |
| `error` | `{session_id?, code, message}` | 流内错误（连接不断） |
| `: hb`（注释行） | —— | **每 15s** 心跳，防代理断流（CF 100s idle） |

- **v1 不做 token 级流式增量**：`message.delta` 事件名**预留但不发送**，前端不要依赖；"Agent 正在输入"用 `message(status=running)` 占位表达。
- 事件 `id:` 一律给 `seq`；无 seq 的事件（`hello`/`observers`）不写 `id:`，重连不会重放它们。
- 汇总流 `/api/stream`：事件体一律带 `session_id`；`hello.subscribed` 列出当前可见的活动会话；可见性变化（新会话/被授权）即推 `session.created`。

## 7. 会话级并发口径（**冻结数字**）

| 维度 | 口径 | 超限表现 |
|---|---|---|
| **会话内** | 严格串行：同一会话同一时刻最多 1 条在跑；后到者进该会话 **FIFO 队列**（**不拒绝**） | 队列深度 > 20 → `429 queue_full`；单条排队 > 120s → 该消息落 `failed` + `429 queue_timeout`（SSE 也推 `message.failed`） |
| **成员级（跨会话）** | 同一成员跨会话可并行，上限 `member_max_concurrent`，**默认 2** | 超限 → `429 busy`，带 `Retry-After` 与 `retry_after_s`（缺省 5s，前端指数退避封顶 30s） |
| **跨成员** | 无上限，各自独立 | —— |
| **会议** | 一场会 = 一条会话队列 → 场内发言天然串行；**多场会可并存**（各队列），但同一成员被多场同时点名 → 排到该成员槽位**排队，不 abort、不 503** | 同上 `429 busy`（表现为"稍后自动跑"） |
| **并发上限可见** | `GET /healthz` 暴露 `member_max_concurrent` 与各成员 `busy/running/queue_depth` | —— |

- 队列位置经 `POST` 响应的 `queue_position` 与 SSE `status` 双重暴露，**前端必须显示队列态**，否则用户以为卡死。
- 排队消息若因会话关闭/成员离线而无法执行 → 落一条 `author.type="system"` 的 `failed` 消息并推流。
- 底层改造：`cli_backend` 的锁粒度从"每成员一把"降到 **(成员, 会话键)**，并加成员级并发上限；上游会话键由 chat 服务按 **`chat:<session_id>`** 稳定派生（1:1），保证服务重启后仍能续上同一 CLI 上下文。**此项为后端改造，前端不感知，只需按上表处理 429/队列态。**

## 8. 错误与状态码（统一信封）

```json
{"ok":false,"error":{"code":"busy","message":"lobster 正在执行其它任务","retry_after_s":5}}
```
| HTTP | `code`（常用） | 何时 |
|---|---|---|
| 400 | `bad_request` | JSON 不合法 |
| 401 | `unauthorized` | 未登录/会话过期 → 前端跳 `/login` |
| 403 | `forbidden` / `observer_readonly` | 角色不足 / 旁听者尝试写 |
| 404 | `not_found` | 目标不存在**或无权可见** |
| 409 | `conflict` / `cannot_cancel_running` / `already_done` | 状态冲突 |
| 422 | 见 §6.1 门禁码、`text_too_long`、`use_meetings_endpoint` | 参数/门禁不过 |
| 429 | `busy` / `queue_full` / `queue_timeout` | 并发口径（**可重试**，带 `retry_after_s`） |
| 500 | `internal` | 服务端异常 |
| 501 | `not_implemented` | 预留端点（如 `speak-requests`） |
| 502 | `backend_error` | Agent CLI 执行异常 |
| 503 | `upstream_unavailable` | 成员上游/中枢不可达 |

说明：既有服务（`9920`/`9970`）用扁平 `{"ok":false,"error":"busy"}`；**新服务统一用对象信封**，前端只按本表实现。

## 9. 网络与部署落点（我的责任田，已核）

- 服务名 `agent-team-chat`（龙虾 systemd），监听 **`<host>:9980`（tailnet）+ `127.0.0.1:9980`**；端口 9980 现为空闲（已 `ss -lnt` 核）。
- 静态托管：前端产物目录 `agent-team/ui/`，由 chat 服务同源托管（`/`、`/login`、`/account` 复用看板 `view_auth` 口径）；**前端零构建步骤优先**（纯 HTML/CSS/JS，便于跨 OS 迁移）。
- 数据库：复用龙虾 PG `127.0.0.1:9950`，新表见 §10；**服务是唯一 DB 访问者，浏览器不直连 PG/NATS/黑板**。
- 公网：默认不开；需要时在同一隧道加 `chat.example.com → 127.0.0.1:9980`（见 §0）。
- v1 只支持 `http`（http/2 由 CF 隧道侧提供）；前端连接数按 HTTP/1.1 上限 6 设计 → **多 Tab 优先用汇总流 `/api/stream`**。
- 深链：`GET /?session=<sid>`（新开窗格用），前端自行解析。

## 10. PG 新表（后端实现细节，前端不得依赖表名/列名）

沿用"**唯一实体引用不复制冗余**"原则：

```sql
chat_sessions(                       -- 会话索引层（唯一枚举真源）
  session_id text primary key, kind text not null check (kind in ('dm','meet')),
  title text not null, sensitivity text not null default 'internal',
  members jsonb not null default '[]',           -- 成员 id 快照
  created_by text not null, created_at timestamptz not null default now(),
  updated_at timestamptz not null, state text not null default 'open',
  ref_table text, ref_id text,                   -- meet 场指向 roundtable_sessions.session_id
  client_ref text,                               -- 幂等键（dm 建会话用）
  unique (created_by, client_ref)
);
chat_messages(                       -- 消息层，append-only 触发器
  message_id text primary key, session_id text not null references chat_sessions(session_id),
  seq bigint not null, author_type text not null, author_id text not null,
  kind text not null default 'text',
  text text,                                     -- dm：正文；meet：NULL
  ref_table text, ref_id text,                   -- meet 场指向 roundtable_turns.turn_id
  status text not null, reply_to text, run jsonb, meta jsonb,
  created_at timestamptz not null,
  unique (session_id, seq)
);
chat_observers(session_id text, username text, joined_at timestamptz, left_at timestamptz,
               note text, primary key (session_id, username));
```
- `chat_messages` **只写落定消息**（`done/failed/canceled`），`queued/running` 不落库 → 表保持 append-only。
- **meet 场消息正文不复制**：`text` 留空、以 `ref_id` 指 `roundtable_turns.turn_id`，读时从真源投影（历史接口对内合并、对外形状统一）。
- 既有会议不迁移正文；`chat_sessions` 只回填**索引**（`session_id/title/kind=meet/ref`）。
- 迁移脚本 `sql/chat_schema.sql`（建表 + 索引 + append-only 触发器），与看板 `view_auth` 同库、不同表。

## 11. v1 明确不做（前端别提前做）

1. token 级流式增量（`message.delta` 预留不发）。
2. 附件/图片上传（`kind=file` 未开，无上传端点）。
3. 旁听者发言、"申请发言"（`501`）。
4. 人在会议场发言注入 Agent 轮次（只落库+推流）。
5. 服务端已读游标/未读计数持久化（`unread` 仅初值）。
6. 中断运行中的 Agent（只能取消排队中的消息）。
7. 会员管理 UI（账号仍走 `view_auth.py` CLI）。

## 12. 待对方回报 / 需对方配合

1. **OpenClaw 侧 `9920` 并发口径** —— 你已承诺单独回报；请**同时**给出：单成员并行上限、超限时的响应码与字段、是否有队列。§7 的成员级上限需按你的实测口径收口（本契约暂时默认 2）。
2. 前端产物落点确认：`agent-team/ui/`（同源托管）还是你自建静态服务（则需反向代理到 `/api`）——**建议前者**。
3. 是否要 `GET /?session=<sid>` 之外的深链形态（如 `#/session/<sid>`）——不影响契约端点。
4. 骨架期可用性：**未实现端点的状态** —— 我实现顺序为 ①`whoami`/`agents`/`sessions`(读)/`messages`(读) ②`POST messages` + SSE ③`POST sessions`/`PATCH` ④`POST meetings` + 会议推流 ⑤旁听。期间未实现的端点按 §8 返回 `501 not_implemented`（**不是 404**），前端可据此开关按钮。

---

契约冻结完成。变更走 v1.1 + 双方确认；实现进度我按 §12.4 的顺序推进并回报。
