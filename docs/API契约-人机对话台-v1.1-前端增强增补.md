# 人机对话台 API 契约 v1.1 —— 前端增强增补（前端定位硬约束落地）

提出时间：2026-09-20（北京时间）　提出人：Hermes 总控（通信系统责任田 owner）
依据：刚哥 2026-09-20 追加的**前端定位硬约束**（界面必须超越 Open WebUI；多Agent多会话 / 多会并存 / 实时旁听必须一等表达）
状态：**已冻结**（2026-09-21：openclaw-nb 回执 `API契约-v1.1-OpenClaw回执.md` + 会议 `meet-team_chat-handoff-20260921` 三项当场达成、无 object；落点收口见 §H）

> **版本关系**：v1.1 = v1.0 + 本增补。**冲突处以本增补为准**；本增补未提及之处，v1.0 原样有效。
> v1.0 文件（`API契约-人机对话台-v1.md`）**不动**（冻结留痕）。本文件与 v1.0 同目录，同一台机直接读。

本增补只做三件事：**① 把界面诉求写成硬约束；② 为四种视图补齐实时字段与事件（不许前端轮询拼）；③ 修正 v1.0 里与会议真源对不上的三处口径。**

---

## A. 前端定位（硬约束）

### A.1 实现自由，界面必须「超越」

前端用什么实现（改 Open WebUI / 重写 / 自建）**由你定**。但硬约束是：**布局可以学它，模型不能照搬。**

- 它的"多 tab"只是**浏览器多开**，不是一等对象；我们要的是**一 Agent 多会话 + 可选窗格并排**由数据层一等支撑。
- 它没有**会议 / 旁听 / 发言权 / 多会并存**这套模型；我们的会议视图必须是**独立形态**，不是把聊天框换个壳。
- 判定标准：**四件诉求（多Agent多会话、多会并存、实时旁听、全局态）在界面上是原生表达还是外挂拼凑**——后者视为未达标。

### A.2 四件必须一等表达

1. **多 Agent 多会话** —— 同一 Agent 的 N 条会话各占一个 Tab，可并排双窗格；
2. **多会并存** —— 进行中的会议可同时观看，各自独立的圆桌发言流；
3. **实时旁听** —— 只读实时流 + 参会人名单 + 发言权状态；
4. **全局态** —— 在线 Agent 数 / 进行中会议数 / 未读，顶部常驻。

### A.3 布局契约（刚哥定，本契约按此预留字段）

| 区域 | 职责 | 数据来源（见 §B） |
|---|---|---|
| **顶部全局态** | 在线 Agent 数、进行中会议数、未读汇总、连接态 | `GET /api/overview` + SSE `overview` |
| **左栏 Agent 花名册** | 在线态 / 心跳新鲜度 / 当前在干什么 / 忙闲队列 | `GET /api/agents` + SSE `roster`／`presence`／`status` |
| **中部 会话 Tab 区** | 一 Agent 多 Tab；可选新窗格与现有 Tab 并排；点左栏成员即开该成员的新 Tab | `GET /api/sessions`（分组）+ `GET /api/stream` |
| **右栏 上下文 / 黑板引用面板** | 当前会话/当前发言引用了哪些黑板条目、文件、量测 | `session.context` / `message.refs` / `meeting.context_refs`（§B.6） |
| **会议视图（独立形态）** | 圆桌发言流：轮次 / 发言人 / 收敛进度；**不是聊天流** | `GET /api/sessions/{sid}/meeting` + `/turns` + SSE 会议事件（§B.3） |
| **旁听视图（独立形态）** | 只读实时流 + 参会人名单 + 发言权状态 | `GET /api/sessions/{sid}/observers` + `/participants` + 只读订阅（§B.4） |

**不约束**（你自由）：配色、字号、组件库、左右栏是否可折叠、窗格拖拽交互、是否保留 Open WebUI 作为"人↔单 Agent 随手聊天"的旁路入口（建议保留，不裁撤）。

### A.4 实时纪律：「三不 + 一快照」

**不许让前端靠轮询拼装**，具体三条禁令 + 一条要求：

1. **不轮询**：状态变化走 SSE 推送；仅**断线/降级**时允许兜底轮询，且必须带 `since`/`version` 走 `304`（§C 给最小间隔）。
2. **不本地推断在线态**：`presence.state` 一律由服务端算好下发（含阈值判定）；前端不得用 `last_hb_at` 自己减时间判断（客户端时钟不可信）。
3. **不把会议当聊天流**：会议视图以**轮次/发言人/收敛进度**为主轴渲染；`round_no` 与发言序由事件体自带，前端不回查。
4. **一快照**：**每个视图必须有一个"一次调用渲染整屏"的快照端点**，且 SSE 首帧（`hello`）必须携带该快照——刷新/重连零拼装。

### A.5 前端形态与产物（沿用 v1 §0 第 5 行，此处收口）

- 产物落点 `agent-team/ui/`，由 chat 服务**同源托管**（相对路径，不写 host/端口），**零构建优先**（纯 HTML/CSS/JS）。
- **多窗格共用 1 条汇总流** `GET /api/stream`（HTTP/1.1 上限 6 连接，别给每个窗格开一条）。
- 深链：`GET /?session=<sid>`（新窗格）与 `GET /?session=<sid>&pane=<n>`（指定窗格位置）。
- **布局态（Tab 顺序、栏宽、窗格归属、置顶会话）存前端 localStorage，不入契约落库**——避免用户态蔓延到后端。

---

## B. 四种视图的新增 / 扩展字段

> v1.0 已有的字段**全部保留**。下面只列新增与扩展。

### B.1 左栏：Agent 花名册（在线态 / 心跳）

`GET /api/agents` 响应扩展（原 `id/display/online/busy/last_hb_at/meta` 保留）：

```json
{"ok":true,"version":412,"server_ts":"2026-09-20T19:02:11+08:00",
 "agents":[
  {"id":"lobster","display":"龙虾","order":1,
   "presence":{"state":"busy","since":"2026-09-20T18:58:40+08:00","reason":null},
   "heartbeat":{"last_at":"2026-09-20T19:02:05+08:00","age_s":6,"interval_s":30,"missed":0,
                "host":"iZwz97vxjt9efiw…","uptime_s":79860,"agent_version":"…","source":"team.hb.lobster"},
   "workload":{"running":1,"queued":0,"max_concurrent":2,"queue_depth":0},
   "sessions":{"active":2,"dm":1,"meet":1,
               "current":[{"session_id":"meet_20260920_x2m9bd","kind":"meet","state":"running",
                           "since":"2026-09-20T18:58:40+08:00","round_no":1}]},
   "caps":{"dm":true,"meet":true,"observe":true},
   "meta":{"host":"iZwz97vxjt9efiw…","model":"deepseek-v4-flash","via":"9920"}}
]}
```

**`presence.state` 判定口径（冻结，服务端算完后下发）**

| state | 条件 | 界面建议 |
|---|---|---|
| `busy` | 心跳新鲜 且 该成员 `running ≥ 1` | 实心点 +"执行中" |
| `queued` | 心跳新鲜 且 `queue_depth > 0` 且 `running == 0` | 点半亮 +"排队 N" |
| `online` | 心跳新鲜 且 `running == 0` 且 `queue_depth == 0` | 实心点 |
| `degraded` | 心跳 `75 < age_s ≤ 180`，或上游 `healthz` 报异常 | ⚠ 黄点 +"心跳延迟" |
| `offline` | 心跳 `age_s > 180`，或上游明确不可达 | 空心点 +"未心跳" |

- **阈值依据**：`teamd` 实机 `hb_interval = 30.0s`（`teamd.py` 默认值），故取 **2.5×/6×** 作 75s/180s 两档。
- **本条取代 v1.0 §4.2 的"60s 内有心跳"判定**（60s 是 2× 间隔，过于敏感，会把一次丢包误报成掉线）。
- 沿用 v1 的纪律：`offline` **不得**直接断言"人不在"，只做"⚠ 未心跳"提示；云机成员不在线按缺陷追。

**新增 SSE 事件 `roster`**（v1 事件表补一行）：

| `event:` | `data` | 触发 |
|---|---|---|
| `roster` | `{version, server_ts, agents:[…同 上…]}` | 任一成员的 presence/workload/sessions 变化；**整表推送**（成员 ≤10，代价可忽略），服务端**去抖 ≤1s** |

- **`hello` 首帧新增 `roster` 与 `overview` 两个快照**（连接即渲染整屏，零轮询）。
- `roster` 无 `seq` → **不写 `id:`**（沿用 v1 规则），重连以快照为准。
- 兜底轮询：`GET /api/agents?since=<version>` → 无变化返回 **`304 Not Modified`**（带 `ETag: "v<version>"`）。

### B.2 中部：会话 Tab 区（一 Agent 多 Tab + 窗格并排）

`GET /api/sessions` 响应**扩展分组**（服务端算好，前端不拼）：

```json
{"ok":true,"count":9,"next_cursor":null,
 "groups":[
   {"key":"member:lobster","kind":"dm","member":{"id":"lobster","display":"龙虾","presence_state":"busy"},
    "unread_total":3,"sessions":[]},
   {"key":"meetings","kind":"meet","member":null,"unread_total":1,
    "sessions":[]}
 ]}
```

会话条目**新增字段**（上例中 `groups[].sessions` 以空数组占位，元素形状 = v1 §4.3 条目 + 下表）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `last_seq` | int | 该会话当前最大 `seq`（**前端算本地未读的唯一依据**，配合本地已读游标） |
| `last_author` | object\|null | 最后一条的作者（`{type,id,display}`） |
| `last_activity_at` | ts | 最后活动时间（排序/置顶用，比 `updated_at` 更贴"新消息"语义） |
| `group_key` | string | 归属分组键（= `groups[].key`） |
| `context` | object | 右栏引用块（§B.6，v1.1 先发空数组占位） |

**窗格并排的订阅口径（冻结）**：

- 汇总流支持按需过滤：`GET /api/stream?sessions=<sid1>,<sid2>`；**缺省 = 全部可见会话**。
- 硬口径：**全前端只用 1 条汇总流**，多窗格/多 Tab 由前端按事件体 `session_id` 分发。不因开窗格而增连。
- 汇总流**必定带 `session_id`**（v1 已定），`hello.subscribed` 列出当前可见活动会话。

### B.3 会议视图（独立形态：圆桌发言流）

#### B.3.1 新端点 `GET /api/sessions/{sid}/meeting` —— 会议实时态快照

**一次调用渲染整屏**（会议视图的首屏与重连唯一入口；§A.4 第 4 条）。

```json
{"ok":true,"session_id":"meet_20260920_x2m9bd","version":17,"server_ts":"2026-09-20T19:02:11+08:00",
 "meeting":{
  "state":"running","state_cn":"进行中",
  "topic":"…","question":"…","goal":"本次要达成什么",
  "progress":{"round_no":1,"round_no_label":"已完成 1 轮","current_round":2,"max_rounds":3,
              "phase":"discuss","phase_cn":"讨论中","turn_count":41,
              "elapsed_s":612,"deadline_s":1800,"deadline_at":"2026-09-20T19:20:00+08:00","pct":33},
  "agenda":[{"seq":1,"title":"…","expected":"…","status":"concluded",
             "conclusion":"…","conclusion_entry":812,"turns":7},
            {"seq":2,"title":"…","expected":"…","status":"discussing","note":null}],
  "current_agenda_seq":2,
  "speaker":{"member":"lobster","display":"龙虾","role":"speaker","since":"2026-09-20T19:01:40+08:00",
             "round_no":2,"round_seq":2,"of":4},
  "speak_queue":{"round_no":2,"target":["lobster","top001","openclaw","quantum"],
                 "spoken":["lobster"],"speaking":["lobster"],"pending":["openclaw","quantum"],
                 "skipped":[],"absent":["quantum"]},
  "participants":[{"member":"lobster","display":"龙虾","role":"member","presence":"present",
                   "speak":{"state":"speaking","round_no":2,"round_seq":1,"spoke_at":"…"},
                   "stance":"support","vote":"continue","last_turn_id":418},
                  {"member":"hermes","display":"主控","role":"moderator",
                   "presence":"present","speak":{"state":"silent"},"stance":null,"vote":null,
                   "last_turn_id":null}],
  "convergence":{"round_no":2,"stage":"none","denominator":4,"present":3,"spoke":3,
                 "responded":3,"agree":0,"object":0,"abstain":0,"absent":["quantum"],
                 "rule":"no_object_and_agree_ge_floor_present_half_plus_1","status":"pending"},
  "moderator":{"member":"hermes","display":"主控"},
  "observer_count":2,
  "context_refs":[]
 }}
```

**字段口径（真源 → 投影，不新造词）**

| 投影字段 | 真源 | 取值域（冻结） |
|---|---|---|
| `meeting.state` | `roundtable_sessions.state` + 对话台前置态 | `scheduled` \| `running` \| `converged` \| `timeout` \| `aborted` \| `incomplete` |
| `progress.round_no` | `roundtable_sessions.round_no` | **已完成轮次**（真源语义，不许重新解释）；`current_round = round_no + 1`（`running` 时） |
| `progress.phase` | 对话台按状态推 | `discuss` \| `converge` \| `confirm` \| `done` |
| `agenda[].status` | `roundtable_agenda_items.status` | `pending` \| `concluded` \| `deferred` |
| `speak.state` | 由 `roundtable_turns` + 当前轮目标集推 | `speaking` \| `next` \| `waiting` \| `spoke` \| `absent` \| `silent` |
| 发言 `stance` | `roundtable_turns.stance` | `support` \| `oppose` \| `neutral` \| `need_info` |
| 发言 `vote` | `roundtable_turns.vote` | `continue` \| `converge` |
| 发言 `status` | `roundtable_turns.status` | `ok` \| `absent` \| `error` \| `skipped` |
| `convergence.*` | `roundtable.py` 收敛判据 | 判据=`无 object 且 agree ≥ ⌊在场/2⌋+1`（沿用真源，不新造）；`stage ∈ none\|polling\|reached\|forcing` |

- `version`：**会议投影态单调递增整数**，每次变化 `+1`。前端据此判重与丢弃乱序。
- **`turn_count` / `round_no` 一律服务端算好**，前端不得用"数消息条数"推轮次。

#### B.3.2 新端点 `GET /api/sessions/{sid}/turns` —— 圆桌发言流历史

```
GET /api/sessions/{sid}/turns?limit=50&after_turn_id=&before_turn_id=
```
- 游标用 **`turn_id`**（真源 bigserial，会话内单调递增 = 发言顺序），**不用 offset**。
- 排序键 = `(round_no, round_seq)` 升序，等价于 `turn_id` 升序；响应与消息形状（v1 §5）一致，另带会议块。
- 单条发言（投影后）：

```json
{"message_id":"msg_20260920190140_4zq8vm","seq":41,
 "author":{"type":"agent","id":"lobster","display":"龙虾"},
 "kind":"text","text":"…","status":"done","created_at":"2026-09-20T19:01:40+08:00",
 "meeting":{"turn_id":418,"round_no":2,"round_seq":1,"role":"speaker","agenda_seq":2,
            "stance":"support","vote":"continue","confidence":0.8,
            "turn_status":"ok","error":null,
            "refs":[{"type":"entry","value":812},{"type":"file","value":"…"}],
            "entry_id":813,"elapsed_ms":12345,"prompt_chars":4210}}
```
- `refs` / `entry_id` 即**右栏引用面板**的数据源（§B.6），沿用真源 `REF_TYPES = entry|file|measurement|session|turn`。
- **人在会议场的插话**：`author.type="human"`，`meeting.role="interjection"`、`meeting.round_no=null`、`meeting.at_round=<当时轮次>`——会议视图把它渲染在**两轮之间**作为插话行（v1 已定"不注入 Agent prompt"，v2 再谈）。

#### B.3.3 SSE 事件表补行（v1 既有事件不变）

| `event:` | `data` 关键字段 | 触发 |
|---|---|---|
| `meeting.state` | `{session_id, version, state, progress:{…}, agenda:[…], speak_queue:{…}}` | 会议态/轮次/议程的**粗变化**；连接建立后立即发一次；**去抖 ≤1s** |
| `speaker` | `{session_id, version, round_no, speaker:{…}, speak_queue:{…}}` | 发言人切换（含开新一轮/跳过缺席者） |
| `turn.spoke` | `{session_id, version, ...单条发言对象（同上）}` | **一条发言落库即推**（延迟承诺 ≤2s，沿用 v1 §6.2） |
| `convergence` | `{session_id, version, ...convergence 块}` | 收敛票数/阶段变化 |
| `agenda` | `{session_id, version, seq, status, conclusion?, conclusion_entry?}` | 议程项推进/出结论 |
| `meeting.ended` | `{session_id, version, state, ended_at, conclusion_text, conclusion_entry, followups:[…]}` | 会终（任意终态） |

**纪律（冻结）**：

1. **发言流事件必须自带 `member`/`display`/`stance`/`turn_id`/`round_no`** —— 前端一行一渲染，**不许回查**。
2. **`id:` 纪律**：发言类事件（`turn.spoke`）沿用 v1 的投影 `seq` 作 `id:`；**会议态类事件（`meeting.state`/`speaker`/`convergence`/`agenda`）不写 `id:`**——它们是快照，重连后以 `GET /api/sessions/{sid}/meeting` 为准，不进重放序列。
3. **重连顺序（冻结）**：① 先 `GET` 快照 → ② 带 `since_seq` 连流补增量 → ③ 丢弃 `version ≤` 快照 `version` 的所有会议态事件。
4. `message` / `message.done` 事件**照旧对会议场生效**（投影后的消息），会议视图可不用；但**旁听/汇总场景**靠它。

### B.4 旁听视图（只读实时流 + 参会人名单 + 发言权状态）

#### B.4.1 `GET /api/sessions/{sid}/observers` 扩展 + 可见性收口

```json
{"ok":true,"session_id":"meet_…","count":2,
 "visibility":{"full_list_to":"owner","self_entry_to":"viewer"},
 "observers":[{"username":"<human-id>","display":"陈刚","role":"owner","is_self":true,
               "joined_at":"2026-09-20T18:55:00+08:00","state":"watching",
               "speak":{"allowed":false,"can_request":false}}],
 "capabilities":{"can_join":true,"can_leave":true,"can_speak":false,"can_request_speak":false}}
```
- **`viewer`/`member` 只拿到自己那一条 + `count`**（他人身份不外泄）；`visibility.full_list_to` 告诉前端"我这份是不是全量"，前端据此决定是否渲染"旁听名单"标题。
- 名单本身仍 `owner` 全量（沿用 v1 §3）。

#### B.4.2 新端点 `GET /api/sessions/{sid}/participants` —— 参会人 + 发言权（快照子集）

返回 §B.3.1 的 `participants` + `speak_queue` + `speak_policy`（便于 30s 级兜底轮询，形状与快照完全一致，前端可复用同一渲染器）：

```json
{"ok":true,"session_id":"meet_…","version":17,
 "speak_policy":{"order":"round_robin","members_only":true,
                 "human_interject":"allowed_not_in_prompt","observers":"readonly"},
 "participants":[],"speak_queue":{}}
```

#### B.4.3 旁听视图的数据构成（冻结）

| 视图片块 | 来源 |
|---|---|
| 只读实时流 | `GET /api/sessions/{sid}/stream`（订阅即旁听，`POST /observers` 记名单）——**写操作一律 `403 observer_readonly`** |
| 参会人名单 + 发言权状态 | `_participants` 快照 或 `meeting.participants[].speak` |
| 旁听者名单 | `_observers`（按 §B.4.1 可见性） |
| 会议进度/轮次 | `meeting.progress` + SSE `speaker`/`turn.spoke` |

- 旁听者**收全部只读事件**（`turn.spoke`/`meeting.state`/`conclusion`/`observers` 等），只是写被拒——**不额外做"裁剪旁听流"**，避免两套事件形状。
- SSE `observers` 事件 v1.1 **增 `count` 字段**：`{session_id, joined:[…], left:[…], count}`。

### B.5 顶部：全局态

#### B.5.1 新端点 `GET /api/overview`

```json
{"ok":true,"version":88,"server_ts":"2026-09-20T19:02:11+08:00",
 "agents":{"total":8,"online":6,"busy":2,"degraded":0,"offline":["quantum"]},
 "meetings":{"running":2,"scheduled":1,"open_total":3,
             "items":[{"session_id":"meet_…","title":"…","state":"running",
                       "round_no":1,"max_rounds":3,"current_round":2}]},
 "sessions":{"open":9,"dm":6,"meet":3},
 "workload":{"queued_total":3,"member_max_concurrent":2},
 "unread":{"mode":"client"}}
```
- `agents.online` = `presence.state ∈ {online, busy, queued}`（即"心跳新鲜"），`degraded`/`offline` 分列，不混算。
- `meetings.running` = `state ∈ {scheduled, running}` 的 `meet` 场数（**顶部那个数字就是它**）。
- **`unread` 口径（冻结）**：v1.1 默认 **`mode:"client"`** —— 服务端**只给** 各会话的 `last_seq`（§B.2），未读由前端用本地已读游标算，**`total`/`by_session` 字段在 client 模式下不出现**（不是 `null`，避免前端误用占位）。
  - 预留 `mode:"server"`：实现 `PUT /api/sessions/{sid}/read`（`{"read_seq":41}`，按用户落库）后，`overview.unread` 改发 `{mode:"server", total:4, by_session:[{session_id, unread}]}`，前端可跨设备一致。**未实现前该端点返 `501`。**
- 兜底：`GET /api/overview?since=<version>` → 无变化 `304`。

#### B.5.2 新 SSE 事件 `overview`

| `event:` | `data` | 触发 |
|---|---|---|
| `overview` | `{version, server_ts, ...同 §B.5.1 的计数块…}` | 上线/离线、忙闲、开/散会、会话增删；**去抖 ≤1s**；`hello` 首帧必带一次 |

### B.6 右栏：上下文 / 黑板引用面板

**两级引用（字段预留，响应里必须出现；端点可 501）**：

1. **会话级** `session.context`：
```json
{"source":"blackboard","updated_at":"…",
 "refs":[{"type":"entry","value":812,"title":"…","excerpt":"…前 200 字…",
          "entry_id":812,"board":"decision","updated_at":"…"}],
 "pinned":[{"type":"session","value":"rt-20260920-…","title":"…"}]}
```
2. **消息级** `message.meeting.refs` / dm 消息的 `refs`：`[{"type":"entry","value":812,"entry_id":812,"title":"…","excerpt":"…"}]`
3. **会议级** `meeting.context_refs`：本场会**共用的**上下文（沿用 roundtable `shared_content` 口径，不新造）。

- `type` 取值 = 真源 `REF_TYPES = entry | file | measurement | session | turn`（**冻结，不许扩**；要扩走 v1.2）。
- `entry` 型带 `entry_id` ⟹ 前端可直接深链到看板黑板条目页（看板已有该路由）。
- 端点 `GET /api/sessions/{sid}/refs`：**501 预留**。但 **`context` / `refs` / `context_refs` 三个字段必须出现在响应里（空数组 `[]`）**——右栏先能占位渲染，不留"字段缺失 → 前端崩"。
- SSE `context` 事件：**预留不发**（与 `message.delta` 同待遇）。

---

## C. 冻结数字与枚举（v1.1 部分）

| 项 | 口径 |
|---|---|
| 心跳间隔（真源） | `teamd` `hb_interval = 30s`（`ping_interval = 60s`） |
| `presence` 阈值 | `online/busy/queued` ≤ 75s；`degraded` 75–180s；`offline` > 180s |
| `roster` 去抖 | ≤ 1000ms，整表推送 |
| `overview` 去抖 | ≤ 1000ms |
| 会议推流延迟 | 落库 → 上屏 **≤ 2s**（沿用 v1 §6.2） |
| 会议 `version` | 单调递增整数，变化即 `+1` |
| 前端连接数 | 汇总流 **1 条**（多 Tab/多窗格共用）；总连接 ≤ 6 |
| 兜底轮询最小间隔 | ≥ 15s，且必须带 `since`/`version`（命中 `304` 不计费不看日志） |
| `turns` 分页 | `limit` 夹 `1..200`，缺省 50；游标 `turn_id` |
| 旁听名单可见性 | 全量 → `owner`；`count` + 自己那条 → `viewer`/`member` |
| `unread.mode` 缺省 | `client`（服务端只给 `last_seq`） |

### C.1 上游 9920 并发口径（OpenClaw 侧实测回执，2026-09-20，v1 §7 遗留项收口）

| 维度 | 实测结论 | 证据 / 局限 |
|---|---|---|
| **会话级并发** | **支持，且不串行、不串台** | 对 `http://<node-ip>:9920/a2a/openclaw-nb/messages` 并发发 3 个**不同 `session_id`**（`conc-a` / `conc-b` / `conc-c`），**全部 HTTP 200**，各自 **~5s** 返回，响应互不混串 |
| **串行边界** | 并发单位是 **`session_id`**：不同会话并行；同一会话仍按上游自身队列串行 | 本次只验不同会话并行，未单独压同会话同刻多投 |
| **规模上限** | **未定**——本次规模仅 3 | 单会话 5s 偏慢；**10+ 并发待压**，未压前不得宣称上限 |

- 本回执**只收口"有没有会话级并发"**（有）。§7「成员级（跨会话）默认 2」的**数字暂不动**：实测 ≥3 会话可并行不串台，但保守起见仍按 2 作为服务端保护阈值，待 10+ 压测出真上限后再提 v1.2 收口。
- 5s/条属上游 A2A 单次耗时，**不计入** §6.2 的"落库 → 上屏 ≤ 2s"推流延迟承诺（推流延迟是服务端投影时延，与上游执行时延分开计）。

---

## D. 端点总表增量

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/sessions/{sid}/meeting` | 会议实时态快照（一次渲染整屏） | v1.1 新增 |
| GET | `/api/sessions/{sid}/turns` | 圆桌发言流历史（游标 `turn_id`） | v1.1 新增 |
| GET | `/api/sessions/{sid}/participants` | 参会人 + 发言权快照（兜底轮询用） | v1.1 新增 |
| GET | `/api/overview` | 顶部全局态 | v1.1 新增 |
| GET | `/api/agents?since=` | 花名册兜底（`304`） | v1.1 扩展 |
| GET | `/api/sessions` | 增 `groups` + 每会话 `last_seq` 等 | v1.1 扩展 |
| GET | `/api/sessions/{sid}/observers` | 增 `visibility`/`capabilities`/`speak` | v1.1 扩展 |
| GET | `/api/sessions/{sid}/refs` | 右栏引用（**501**） | 预留 |
| PUT | `/api/sessions/{sid}/read` | 已读游标（**501**） | 预留 |
| POST | `/api/sessions/{sid}/speak-requests` | 旁听者申请发言（**501**） | 预留（v1 已有） |

## E. 视图 ↔ 数据 对照表（这就是 UI 骨架的接线图）

| 视图区域 | 首屏（一次调用） | 实时（SSE） | 兜底（断线才用） |
|---|---|---|---|
| 顶部全局态 | `hello.overview` / `GET /api/overview` | `overview` | `GET /api/overview?since=` → 304 |
| 左栏花名册 | `hello.roster` / `GET /api/agents` | `roster`、`status` | `GET /api/agents?since=` → 304 |
| 中部 Tab 区 | `GET /api/sessions`（`groups`） | `session.created/updated/closed`、`message*` | 重连 + `since_seq` |
| 会话窗格内容 | `GET …/messages?before_seq=` | `message`/`message.done`/`message.failed` | 同上 |
| 会议视图 | `GET …/meeting` + `GET …/turns` | `meeting.state`/`speaker`/`turn.spoke`/`convergence`/`agenda`/`meeting.ended` | `GET …/meeting`（带 `version`） |
| 旁听视图 | `GET …/meeting` + `GET …/observers` | `turn.spoke`/`speaker`/`observers` | `GET …/participants` |
| 右栏引用 | `session.context` / `message.refs` | `context`（预留不发） | 随首屏 |

---

## F. 变更记录（v1.0 → v1.1）

**新增**
1. §A 前端定位硬约束（界面必须超越 Open WebUI；四件一等表达；三栏 + 顶部 + 两个独立视图；"三不 + 一快照"实时纪律）。
2. 端点：`GET /meeting`、`GET /turns`、`GET /participants`、`GET /overview`（+ 预留 `GET /refs`、`PUT /read`）。
3. SSE 事件：`roster`、`overview`、`meeting.state`、`speaker`、`turn.spoke`、`convergence`、`agenda`、`meeting.ended`（+ `context` 预留不发）。
4. `hello` 首帧增 `roster`/`overview`/`meeting` 三个快照。
5. 字段：`presence`、`heartbeat`、`workload`、`sessions.current`、`caps`；`groups`、`last_seq`、`last_author`、`last_activity_at`、`group_key`；`progress`/`speaker`/`speak_queue`/`participants`/`convergence`；`visibility`/`capabilities`/`observers[].speak`；`session.context`/`message.refs`/`context_refs`；各 `*.version`。
6. 兜底 `304`（`agents`/`overview`）。

**修正（与真源对齐，v1.0 相应写法作废）**

| # | v1.0 写法 | v1.1 更正 | 原因 |
|---|---|---|---|
| 1 | `meeting.state: scheduled → running → concluded \| incomplete \| deferred` | `scheduled`（对话台前置）→ 真源 `running`/`converged`/`timeout`/`aborted`/`incomplete` | v1.0 自造了两个真源没有的终态，投影对不上 `roundtable_sessions.state` |
| 2 | `online` = 最近 60s 有 `team.hb.<成员>` 心跳 | 75s/180s 两档，且由服务端下发 `presence.state` | 真源 `hb_interval=30s`，60s 过敏感 |
| 3 | `round_no` 语义未写明 | `round_no` = **已完成轮次**（真源语义），进行中轮次用 `current_round` | 防止前端把"正在讨论第 2 轮"读成"已完成 2 轮" |

**不变**：v1.0 §1–§3（通用规则/命名/鉴权）、§5 消息骨架、§6.1 建会门禁、§6.3 既有事件、§7 并发口径、§8 错误码、§9 落点、§10 建表、§11 不做清单、§12 待回报 —— **原样有效**。

---

## G. 待 OpenClaw 回执（回执即冻结，然后我按 §12.4 顺序实现）

1. ~~**9920 并发口径**（v1 遗留，仍缺）：单成员并行上限、超限的响应码与字段、有无队列。§7 成员级上限暂按 **2**，按你实测收口。~~ → **已收口（2026-09-20）**：见 **§C.1**。会话级并发=支持（3 会话并行全 200、不串台）；上限未定（10+ 待压），§7 成员级阈值**仍暂按 2**，待压测后提 v1.2。
2. **在线态口径**：确认接受 §B.1 的 75s/180s 判定与"服务端算好下发"；若你侧已有更权威的在线源（18789 网关/Open WebUI 侧），报口径，我并进来。
3. **前端产物落点**：`agent-team/ui/` 同源托管（建议）还是你自建静态服务 + 反代 `/api`。 → **已收口（2026-09-21）**：**同源托管**，见 **§H**。
4. **深链形态**：除 `/?session=<sid>`、`/?session=<sid>&pane=<n>` 是否还要 `#/session/<sid>`。
5. **布局与实时纪律**：§A.3 三栏 + 顶部 + 会议/旁听两独立视图、§A.4「三不 + 一快照」**是否照此冻结**（刚哥已定，默认照此，你只需确认）。
6. **会议 `version` 由 chat 服务递增、`roundtable.py` 零改动**（只读真源 + 投影）—— 是否接受。
7. **部署通道**：9980 同源托管、公网默认不开（需要时同隧道加 `chat.example.com`）—— 确认无异议。

**回执方式**：在 `<repo>\agent-team\` 下留一份 `API契约-v1.1-OpenClaw回执.md`（或直接 A2A 回我），我按回执把 v1.1 并入冻结。契约字段/路径/事件名不要单方改，要改走 v1.2 双方确认。

---

**你现在可以开搞**：§E 就是 UI 骨架的接线图，§A.3 是三栏布局，§B.3/§B.4 是会议与旁听两个独立视图的字段全集。我按「读端点 → 发消息 + SSE → 建会话/改名 → 建会 + 会议推流 → 旁听」顺序实现，**未实现的端点/事件统一返 `501 not_implemented`（不是 404）**，你据此开关按钮、先用 mock 数据把界面做出来。

---

## H. 落点收口（2026-09-21 双方确认，无 object）

**依据**：会议 `meet-team_chat-handoff-20260921`（hermes 召集 / openclaw-nb 与会）+ 回执 `API契约-v1.1-OpenClaw回执.md` §2。
**结论**：§G.3 收口 —— **前端产物落点 = 同源托管**（`agent-team-chat` 服务在 9980 同时出 API 与静态页）。

### H.1 决定（冻结）

| 项 | 口径 |
|---|---|
| 静态根 | `agent-team/ui/`，交付物 = `index.html` + `api.js`/`app.js`/`meeting.js`/`observers.js`/`sidebar.js` + `styles.css`/`views.css`/`extras.css` |
| 排除件 | `serve.py`、`mock.js`（开发辅助件，**不纳入托管**） |
| 服务形态 | 单服务同源：`/api/*` + 静态 + `/login`/`/account`；**前端零构建**，相对路径，不写 host/端口 |
| 退路 | 「前端独立服务 + 反代 `/api`」（需 CORS + cookie `SameSite`/`Domain`）—— **不启用** |
| 责任田 | 后端（`chat_svc.py` + 部署）= hermes；前端（`ui/` 全部产物）= openclaw-nb；**互不碰对方代码** |

### H.2 后端配合清单（8 条，**作为实现依据冻结**）

1. **静态根** = `agent-team/ui/`，**排除** `serve.py` / `mock.js`。
2. **路由优先级**：`/api/*`、`/healthz`、`/login`、`/account` 优先于静态；**静态不得覆盖 `/api/` 前缀**（否则 API 被吞）。
3. **SPA fallback**：非 API 命中、非静态文件的 `GET` → 回 `index.html`（**200，非 404**），支持深链 `/?session=<sid>`。
4. **`/login`、`/account` 复用 `view_auth`** 既有口径，前端**不自建登录页**。
5. **缓存头**：`index.html` → `no-cache`（或 `no-store`）；**v1 阶段** `*.js`/`*.css` 统一 `no-cache`（零构建、文件名不变，避免旧缓存）。
6. **SSE 同源**：`Content-Type: text/event-stream` + `X-Accel-Buffering: no` + **禁用压缩缓冲**，否则事件被攒批。
7. **MIME**：`.js` 必须 `application/javascript`，`.css` = `text/css`（防旧 Windows 注册表把 `.js` 报成 `text/plain`）。
8. **不代管前端构建/产物生成** —— 交付物就是纯静态文件，前端改完直接落 `ui/`。

### H.3 验收纪律

清单第 2、6 条属**安全与推流正确性**要求：实现时**逐条自测**（`/api/does-not-exist` 必须仍是 `501` 信封而非 200 HTML；SSE 事件必须逐条到达），**不接受"看着能用"**。

### H.4 交付与排期

| # | 事项 | 责任人 | 截止 | 状态 |
|---|---|---|---|---|
| 1 | 只读面对接口径确认 | openclaw-nb | 本会 | ✅ 已确认（tailnet 实测：`/healthz` ok、`/api/agents` 返回 lobster、未知端点 501 非 404） |
| 2 | 落点决定（同源托管 + 8 条清单） | openclaw-nb | 本会 | ✅ 已决定 |
| 3 | 落点决定收口进契约增补 | hermes | 2026-09-21 | ✅ 本节（§H） |
| 4 | 写 `docs/frontend-integration.md` | hermes | 2026-09-21 | ✅ 已落盘 `agent-team/docs/frontend-integration.md` |
| 5 | 实现同源静态托管（按 §H.2 八条） | hermes | 2026-09-22 | ⏳ |
| 6 | 后端第二档：`POST messages` + SSE | hermes | 按契约 §12.4 | ⏳ |

### H.5 §G 状态更新

| §G | 事项 | 状态 |
|---|---|---|
| 1 | 9920 并发口径 | ✅ 已收口（见 §C.1，2026-09-20） |
| 2 | 在线态口径（75s/180s、服务端算好下发） | ⏳ 未逐条回执：默认无异议，**状态不变**（要留痕走 v1.2 补记） |
| 3 | **前端产物落点** | ✅ **本节点收口：同源托管**（§H.1） |
| 4 | 深链形态 | ⏳ 未逐条回执：按 `/?session=<sid>`、`/?session=<sid>&pane=<n>` 执行（`#/session/<sid>` 由前端自行解析，不改端点）；**状态不变** |
| 5 | 布局与实时纪律（§A.3 / §A.4） | ⏳ 未逐条回执：刚哥已定，默认照此冻结；**状态不变** |
| 6 | 会议 `version` 由 chat 服务递增、`roundtable.py` 零改动 | ⏳ 未逐条回执：后端按此实现，**状态不变** |
| 7 | 部署通道（9980 同源、公网默认不开） | ⏳ 未逐条回执：默认无异议，**状态不变** |

> **口径说明**：本会「三项全部达成、无 object」只针对**议程 1（只读面口径）、议程 2（前端落点）、议程 3（对接说明交付）**；§G 的 2/4/5/6/7 是**另一批问题**，对方未逐条书面回执，故不在此处标 ✅（见 §I 同名结论）。
>
> **遗留跟进（不让静默）**：§12.4 第二档的 ①–⑤ 分档实现尚未与前端逐项对齐字段与事件名 —— **实现到那一档时再开一次短会**确认。契约 v1.0 文件**不动**（冻结留痕），一切增量以本增补为准。

---

## I. 2026-09-21 会议收口（NATS 平面 · `session_id = meet-team_chat-handoff-20260921`）

> 本节 = 会议原始记账（另有一份同内容的会议纪要：`reports/meet-team_chat-handoff-20260921.md`）。
> 与 §H 重复的 8 条后端配合清单以 **§H.2** 为准；§G 的 2/4/5/6/7 状态以 **§H.5** 为准（未逐条回执，状态不变）。

召集 hermes / 与会 openclaw-nb。三项议程**当场达成，无 object**。纪要：`reports/meet-team_chat-handoff-20260921.md`。

1. **§G-3 前端产物落点 → 收口：同源托管。** 9980 同时出 API 与静态页。openclaw-nb 给出 8 条后端配合清单（**逐条为实现依据**）：
   - 静态根 `agent-team/ui/`（排除 `serve.py` / `mock.js`）
   - `/api/*` + `/healthz` + `/login` + `/account` 优先于静态；静态不得覆盖 `/api/` 前缀
   - SPA fallback：非 API 命中的 GET 回 `index.html` 200
   - `/login` `/account` 复用 `view_auth`，前端不自建登录页
   - `index.html` 缓存 `no-cache`；v1 阶段 js/css 亦 `no-cache`
   - SSE 同源：`text/event-stream` + `X-Accel-Buffering: no` + 禁压缩缓冲
   - `.js` MIME 必须 `application/javascript`
   - 不代管前端构建（产物由前端 build 好放进 `ui/`）
2. **只读面对接口径 = agree**（openclaw-nb 自行 tailnet 实测对齐：`/healthz` ok、`/api/agents` 返 `lobster`、未知端点确为 501 非 404；其 `api.js` 已按契约实现 501 分支）。
3. **对接说明交付形式 = agree**：`docs/frontend-integration.md`（hermes 只写接口，不碰前端代码）。

**本会产生的新增后端待办**：
- **`/login` `/account` 的服务端实现**（复用 `view_auth`）—— 原契约未列，但"前端不自建登录页"⇒ 后端必须出。排第二档，与静态托管同上。
- `GET /api/agents` 的 `online/busy/last_hb_at` 本版恒 `null` = **"未知"**（未接 NATS 心跳），前端须显示"未知"而非"离线"（已写入对接说明，防误导）。

**落实事项**：hermes = 对接说明（2026-09-21 已完成）/ 同源静态托管（2026-09-22）/ 收口本增补（2026-09-21，本条）/ 第二档 POST messages + SSE（按 §12.4）；openclaw-nb = 无待办。

§G 其余项（2 在线态口径 / 4 深链 / 5 布局与实时纪律 / 6 会议 version / 7 部署通道）**仍未回执，状态不变**。

---

## I. 临时群 · 群发言协议（刚哥 2026-09-21 定，六条）

> 群 = `chat_sessions(kind='meet')`，一个群就是一个 session。**人与 Agent 同群**，同一份消息数据。

### I.1 六条规则（原话口径，不许走样）

1. **刚哥本人是群参与者**（且是发起人）——"我也需要参与这个交流"。
2. **刚哥发言不带 @** ⇒ 发给**群里所有人**，且**所有人都要处理和回应**（不是通知，是派活）。
3. **刚哥发言带 @ 某人** ⇒ **只发给那个人**，由他处理并回应。
4. **群由刚哥发起 ⇒ 也必须由刚哥宣布结束**（他人无权关群）。
5. **所有与该群有关的内容一律在群内交流**（不得私聊、不得跑到别处）。
6. **Agent 发言必须带 @**（至少一个目标），三种写法：
   - 回复某人 → 至少 @ 该人（"回复我的那些消息，就需要至少艾特我"）
   - 需多人知悉 → @ 多个人
   - 需全员知悉 → **`@所有人`**
   - **不带 @ 直接发 → 服务端拒收**

### I.2 数据结构

| 字段 | 说明 |
|---|---|
| `mentions` | `["@all"]` 或 `["<human-id>","openclaw-nb",…]`（成员 id 数组）。`@所有人` 落库为 `["@all"]` |
| `audience` | 展开后的接收者（`@all` ⇒ 群全部成员）。**始终含发起人**除非明确 @ 单个他人 |
| `needs_response` | 需要处理并回应的人/Agent（规则 2/3/6 算出来的待办对象） |
| `from` | 发送者；`type` ∈ `human` / `agent` |

**发送方差异（规则 2/6）**：
- **人（刚哥）**：`mentions` 可省略 ⇒ 默认 `["@all"]`（规则 2）
- **Agent**：`mentions` **必填且非空**，否则 422（规则 6）

### I.3 服务端硬门禁

| 情形 | 结果 |
|---|---|
| Agent 发言无 mentions | **422 `mention_required`** |
| mentions 里有非群成员 | **422 `unknown_mention`** |
| 非发起人尝试关群 | **403 `only_owner_can_close`** |
| 群已归档（`archived`）仍发言 | **409 `session_closed`** |
| 非群成员读群 | 404（沿用"无权即不可见"） |

### I.3.1 与 v1.0 §10 表结构的对齐（两处澄清，必须照此）

- **群的开合状态用 `chat_sessions.state`，枚举是 `open` / `archived`** —— 表里**没有** `closed`。
  "关群" = 置 `archived`；关群后仍发言 ⇒ `409 session_closed`。
- **群的正文自持**：群消息**写进 `chat_messages.text`**（`author_type` ∈ `human`/`agent`）。
  v1.0 §10 里"meet 场正文为 NULL、从 `ref_id` 指着圆桌真源投影"说的是**圆桌会议投影**（`ref_id` 非空）。
  两者共存不冲突：**`text` 有值就用自己的，`ref_id` 有值就投影真源**。我们的临时群走 `text` 自持。

### I.3.2 Agent 身份口径（群内发言）

| 来源 | 身份判定 |
|---|---|
| 浏览器（刚哥） | cookie 会话 ⇒ `author_type='human'`，`author_id=<username>` |
| 成员机 Agent（`team.py`） | **tailnet 内直连 + `X-Agent-Id: <member_id>` 头** ⇒ `author_type='agent'` |
| tailnet 但无 `X-Agent-Id` | 视为 owner 访客，但**不能**以某个 Agent 名义发言（拒 `400 agent_id_required`） |
| 非 tailnet 且无会话 | 401 |

- `X-Agent-Id` **只在 tailnet 来源被接受**；非 tailnet 给这个头一律忽略（防冒名）。
- 该 id 必须在群成员里，否则 422 `unknown_mention` 同理拒。

### I.4 群生命周期

- **建群**：发起人建 ⇒ `state='open'`，成员 = 人 + 指定 Agent
- **群内发言**：落 `chat_messages`（append-only，seq 单调）
- **关群**：**只有发起人**能 `PATCH /api/sessions/{sid}` → `state='archived'`（表内枚举只有 `open|archived`，关群即归档）
- **关群后**：只读；内容归档
- **兜底**：发起人忘记关 ⇒ 超时自动归档（既有 session 超时机制）

### I.5 全员可见 · 反私聊

- 群消息对**全群成员可见**（人 + 所有 Agent）
- `team.py group pull` 拉的是**全群增量**——**谁说的都看得到**，不是只拉发给自己的
- 成员间协作**必须走群**；点对点内容**不算本群留痕**（防规则 5 被绕过）

### I.6 待回应义务

- 成员每轮开始先 `pull --since` 拉未读
- 返回里带 `needs_my_response` ⇒ 该成员**必须回应并落群**
- 待回应清单对全群可见（谁还没回，大家看得见）

> **实现顺序（在 §12.4 之中）**：②发消息/增量拉（含上面 I.2/I.3 语义）→ ④建群 + 关群 → ⑤旁听 → `team.py group say/pull`。

---

### I.7 实现落点（2026-09-21 已上线，端到端验收 PASS）

**后端**：`chat_svc.py` **VERSION 2.0** → 龙虾 `<deploy-dir>/`，systemd `agent-team-chat` @ 9980，
`/healthz` 的 `phase` = `group`。

| 端点 | 状态 |
|---|---|
| `POST /api/sessions` | ✅ 建群（**必须由登录的人**发起；否则 `403 creator_required`） |
| `POST /api/sessions/{sid}/messages` | ✅ 群内发言（§I.3 门禁全部生效） |
| `PATCH /api/sessions/{sid}` | ✅ 关群（`{"state":"archived"}`；仅发起人，否则 `403 only_owner_can_close`） |
| `GET /api/sessions/{sid}/messages` | ✅ 新增 `me` 与 `my_pending` 两个字段 |

**两处口径细化（以此为准）**：

1. **`needs_response = audience − {发送者自己}`** —— 自己不用回应自己。
   `audience` = `mentions` 展开结果（`@all` ⇒ 全群成员），供前端显示"这条发给了谁"。
   例：刚哥不带 @ 发言 ⇒ `mentions=["@all"]`、`audience=全群`、`needs_response=[hermes, openclaw-nb]`。
2. **成员可见性**：具名访客必须在该会话 `members` 里，否则 **404**（与"不存在"同码，不泄露存在性）；
   `role=owner`（管理员）与匿名 tailnet 放行。

**身份优先级（`_resolve_visitor` 已改）**：**会话 cookie 优先于 tailnet 免登录** ——
否则刚哥在 tailnet 内打开网页时，系统只认得"来了个 owner"、认不出"你是 <human-id>"，
群协议里"人"这一侧就不成立。`127.0.0.1` 仍不豁免。

**Agent 侧入口**（成员机仍只有 `team.py` + `nats_client.py` 两个文件）：

```bash
team.py group show --group <sid>                          # 看群（不需要身份）
team.py group pull --group <sid> [--since N] [--as 我]     # 拉全群增量 + 我还欠谁
team.py group say  --group <sid> --to @all|a,b "话" [--as 我] [--reply-to <mid>]
```

- 群消息走 **HTTP 打 chat_svc（9980）**，**不是 NATS**：库是唯一真源，NATS 只管唤醒通知。
- `--to` 缺省 `@all`，但**只有"没写 `--to`"才默认全员**；写空串**直接报错**（防"以为 @ 了其实没 @"）。
- `--as` 缺省自动推断；读操作推断失败降级为匿名（照样能看全群，只是算不出"我欠谁"）。

**群消息自持**：群（`kind='meet'` 且 `text` 非空）正文**直接落 `chat_messages.text`** ——
与 v1.0 §10"meet 正文在真源（圆桌投影）"不冲突：**投影走 `ref_*` 且 `text` 空，自持群走 `text`**，两者互斥。

**seq 分号**：`SELECT pg_advisory_xact_lock(hashtext(session_id))` + `COALESCE(MAX(seq),0)+1` 同事务落库；
真库并发 12 条实测 seq 连续无重复（`verify_chat_e2e.py` §10）。
