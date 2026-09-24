# Agent 会话（session）设计与契约 v1 —— 治「多轮对话忘记承诺」

> 触发：刚哥 2026-09-20 报告真实故障 —— 任务执行中两个 Agent 多轮对话，某 Agent 忘记自己**刚刚承诺过**的事。
> 本文件是实现的唯一依据（契约冻结）。实现方不许自行改路径名、字段名、语义。

---

## 1. 确诊（已用代码链证实，不是推测）

「忘记」不是偶发，是**三层设计性缺口**叠加：

**根因 1 —— 普通对话默认没有会话（直接原因）**
- `team.py` 第 750/758 行：`--session-id` **默认 None**；第 433/534 行 `if args.session_id:` 才写进 envelope → 默认**不带**。
- `a2a_svc.py` 第 112 行：`if session_id:` 才设 `payload["user"] = "a2a:<caller>:<session_id>"` → 不带就**没有 user 字段**。
- `cli_backend.py` 第 587-589 行：`session_key=None` → codex 加 `--ephemeral`、pi 加 `--no-session` → **一次性会话、不落盘、答完即弃**。
- 即：**每一轮都是全新会话，零上下文**。
- 唯一例外：`team.py meet`（第 612 行 `session_id = args.session_id or make_session_id()`）—— **会议自动生成 session_id**，所以只有会议有多轮上下文。

**根因 2 —— 服务端不回放历史（根本原因）**
- `a2a_svc.py` 第 109 行：`messages` **永远只有一条** `{"role":"user","content":text}`。
- `cli_backend.build_prompt()` 本来会把「最后一条 user 之前」的消息拼成 `[上下文]` 前缀保证多轮不丢 —— 但**前面永远是空的**。
- 所以上下文 100% 依赖后端 CLI 自己的 resume；一旦 resume 失效，**没有任何降级，静默重开**，Agent 不会报错、只会从头说。

**根因 3 —— codex 路径的会话 id 是内存字典（放大）**
- `cli_backend.py` 第 137 行 `codex_sessions = {}`（纯内存）→ 进程重启即清空。
- pi 用 `safe_session_id()`、claude 用 `uuid5()`，都是**稳定派生**，重启能续；**codex 无稳定派生 → 重启后同一 session 也续不上，静默开新会话**。

**加重项 —— 普通对话零留痕**
- 库里只有 `roundtable_turns`（会议）有对话流水；**普通 A2A 对话没有任何表**。
- 即：不但会忘，**事后连"它当时承诺过什么"都查不到**。

## 2. 目标（刚哥定的）

1. Agent 之间对话（含多 Agent 团队讨论）引入 **session**：开启 → 对话 → 关闭。
2. session 结束 → 对话内容归档。
3. session 自带超时 → 超时也归档。
4. **要真正达成的效果**：多轮对话不忘记前面说过的话。

## 3. 设计裁决（与本契约一并执行，不得删改）

- **D1 空闲超时，不是绝对超时。** 判据 `now() - last_turn_at > idle_ttl_s`。绝对 TTL 会在长任务中途把活跃 session 掐死。`last_turn_at` 由**数据库触发器**每轮自动刷新，不靠调用方自觉。
- **D2 服务端回放是核心机制，归档只是附属。** 每轮由被调方把该 session 最近 N 轮拼回 `messages` 发给后端 —— 让历史**从中枢出发**，不再托管给 CLI 黑盒。CLI resume 保留作增强，两者并存；**历史重复是可接受的代价，失忆的代价不可接受**。
- **D3 中枢统一记，Agent 侧无状态。** 成员机上只准放 `team.py` + `nats_client.py` 两个文件（硬约束），本来也没地方存历史。
- **D4 抄会议的表纪律，不复用会议的表。** `roundtable_*` 是「主持人/最大轮数/出席率」语义，掺一起互相污染。
- **D5 路径用 `/sess`。** 黑板现有 `/sessions` 是**会议**（roundtable 场次），绝不能混用。
- **D6 归档 ≠ 删除。** 沿用既有纪律：清正文留行、落时间与原因。
- **D7 长正文走 tmp box。** 某轮正文超 8 KiB 自动落 `/tmp`，turn 里只存 `tmp_id`（与「Agent 间只传简短信息 + id」一致）。
- **D8 每个 session 带 topic（刚哥 2026-09-20 追加），值域与黑板条目 / 会议 session 的 `topic` 同一套。** 团队已有统一的 topic 概念（`blackboard_entries.topic`、`roundtable_sessions.topic`，后者注释即「与黑板条目的 topic 同一个值」）。session 沿用同一值域，才能「按主题」横跨黑板/会议/会话三处检索 —— 这正是他说的「好管理」。topic **NOT NULL**，缺省填 `general`（不编造业务主题）。

---

## 4. 表设计（新表，不动任何既有表）

### 4.1 `agent_sessions` —— 会话注册表

| 列 | 型 | 说明 |
|---|---|---|
| `session_id` | text PK | `as-YYYYMMDD-HHMMSS-xxxx` |
| `topic` | text NOT NULL | **D8**：与黑板条目/会议 session 同一值域；缺省 `general` |
| `title` | text | 会话标题（人读用） |
| `owner` | text NOT NULL | 发起方成员名 |
| `members` | jsonb NOT NULL | 参与成员数组 |
| `state` | text NOT NULL DEFAULT `open` | **`open` \| `closed` \| `expired`** |
| `created_at` | timestamptz NOT NULL DEFAULT now() | |
| `last_turn_at` | timestamptz | **每轮由触发器刷新**（D1 空闲判据的依据） |
| `idle_ttl_s` | int NOT NULL DEFAULT 1800 | 空闲超时秒数，范围 60..86400 |
| `turn_count` | int NOT NULL DEFAULT 0 | 轮次计数（触发器维护） |
| `closed_at` | timestamptz | |
| `close_reason` | text | `manual` \| `idle` \| `owner` |
| `archive_ref` | bigint | 归档后指向 `shared_content.content_id`（软引用） |
| `note` | text | 收尾说明 |

索引：`(topic, created_at DESC)`、`(state, last_turn_at)`、`(owner, created_at DESC)`。
冻结：`session_id`/`topic`/`owner`/`created_at`/`members` 写入后不可变（触发器拒）；`state` 只许 `open→closed`、`open→expired`，**不许回退**。

### 4.2 `agent_session_turns` —— 对话流水（append-only）

| 列 | 型 | 说明 |
|---|---|---|
| `turn_id` | bigserial PK | |
| `session_id` | text NOT NULL REFERENCES agent_sessions | |
| `seq` | int NOT NULL | 会话内单调递增（触发器/序列给） |
| `from_member` | text | 谁说的 |
| `to_member` | text | 说给谁 |
| `role` | text NOT NULL | `user`（对方说的）\| `assistant`（本机回的）\| `system` |
| `content` | text | 正文；外置时置空 |
| `content_ref` | text | **D7**：正文落 `/tmp` 时存 `tmp_id` |
| `bytes` | int | 正文字节数 |
| `ts` | timestamptz NOT NULL DEFAULT now() | |

唯一约束 `(session_id, seq)`。索引 `(session_id, seq DESC)`、`(session_id, ts DESC)`。
**纪律（照抄会议）**：`UPDATE` / `DELETE` / `TRUNCATE` 一律被触发器拒 —— 流水不可改、不可删。

### 4.3 归档

session 关（manual / idle 超时）→ 把该 session 的 turns 拼成一份 markdown 归档文档 → 写入 `shared_content`（**任务级 scope**，带 `expires_in_days`）→ `agent_sessions.archive_ref` 指向它、`state` 置终态。
归档后 **turn 正文可清空留行**（D6：清正文 + 留痕，不删行）。归档后 session 转为**只读**：还能查、不能再追加 turn（触发器拒）。

---

## 5. 接口（路径 `/sess`，落在 `blackboard_svc`/9940 同一进程，复用 `_auth_peer`）

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/sess` | 开会话。body `{topic, title?, members:[...], idle_ttl_s?}` → `{session_id, topic, state:'open'}` |
| `GET` | `/sess?topic=&state=&limit=&since=` | 列表（**口径与会议 `GET /sessions` 对齐**） |
| `GET` | `/sess/<id>` | 详情 + 最近若干轮 |
| `POST` | `/sess/<id>/close` | 关闭 → **同时归档**。body `{note?}` |
| `GET` | `/sess/<id>/turns?limit=` | 取流水 |
| `DELETE` | `/sess/<id>` | **不提供**（D6：关闭走 close，流水不可删） |

鉴权同 `/tmp`（Bearer token）。非成员读写 → 403。

## 6. 服务端回放（D2，本契约的核心）

- **执行方**：**被调方**的 `a2a_svc`（每轮调用都在被调方留下痕迹，天然不重复）。
- **触发**：请求带 `session_id`，且该 session 存在、`state='open'`。
- **拼装**：取该 session **最近 N 轮**（N 默认 20；累计字节 ≤ 32 KiB，超了**从最旧丢**）→ 转成 **OpenAI `messages` 数组（多条）**，随请求发给 backend。
  - `build_prompt()` 会自动生成 `[上下文]` 前缀 —— **复用现有机制，不改 `cli_backend` 的拼装逻辑**（最小改动）。
  - `user` 字段（CLI resume 增强）**照旧保留**：两路并存，重复可接受（D2）。
- **落流水**：同一轮里记 **两条**：`role='user'`（caller→自己）+ `role='assistant'`（自己→caller）。正文 >8 KiB 时落 `/tmp`、`content_ref` 存 `tmp_id`、`content` 置空（D7）。
- **降级铁律**：session 不存在 / 已关闭 → **不回放、照常转发**（不得因此让调用失败）；但 `session_id` 已关闭时要在响应里带 `session_state` 让调用方知道。

## 7. 默认会话（治「默认裸奔」，根因 1）

`team.py say` / `broadcast` 不带 `--session-id` 时，**自动派生** `chat-<caller>-<收件人>-<YYYYMMDD>`，并在输出**打印出来**（"已进入会话 X，续用请带 --session-id X"），`topic` 缺省 `general`，`idle_ttl_s` 取短值 4 小时（防不相关对话串味）。
**判据**：改造后「不传 --session-id 的两轮对话」也必须有上下文。

## 8. 修根因 3（codex 内存字典）

`cli_backend.Runtime.codex_sessions`（内存 dict）改为**落盘**（同目录 JSON，原子写）或落库，重启不丢。
**判据**：重启 `cli_backend` 后，同一 session 的 codex 对话仍能续上（修前必失败、修后必成功 —— 这个前后对照要真做一次）。

## 9. 验收判据（**必须真跑**，逐条给命令与输出）

1. **复现刚哥的故障场景**：A 承诺某件事 → B 确认 → **下一轮 A 被问「你刚才承诺了什么」→ 必须答出**。（这是本契约存在的唯一理由，必须真跑）
2. 表纪律：`UPDATE`/`DELETE`/`TRUNCATE` `agent_session_turns` **一律被拒**；`agent_sessions` 的冻结列改不动、`state` 不可回退。
3. `topic`：按 `GET /sess?topic=<x>` 能查到；与黑板/会议同一值域。
4. 空闲超时：把 `last_turn_at` 改到过去 → sweep 归档，`state='expired'`、`archive_ref` 非空、正文字段已清。
5. 手动 close：`state='closed'` + 归档 + 之后再追加 turn **被拒**。
6. 回放生效：**绕开 CLI resume**（把后端会话清掉，或换一个后端）后，同一 session 仍能接上文 —— 证明历史确实从中枢回放。
7. 长正文：单轮 >8 KiB → 正文落 `/tmp`、turn 里是 `tmp_id`。
8. 回归：`/entries`、`/search`、`/sessions`（会议）、`/tmp`、看板 9941/9942 **全部不受影响**。

## 10. 分期

- **批 1（本契约范围）**：两张表 + `/sess` 接口 + 服务端回放 + 空闲超时归档 + `team.py` 默认会话 + 修根因 3。
- **批 2（另出清单走流程）**：重新下发 `team.py` 到各成员机 + `sess open/close` 子命令 + 归档文档的看板展示。


