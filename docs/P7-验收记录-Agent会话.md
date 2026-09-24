# P7 验收记录 —— Agent 会话（`/sess`，`agent_sessions` / `agent_session_turns`）

- 日期：2026-09-20
- 唯一依据：`Agent会话-session-设计与契约-v1.md`（契约冻结：路径名/字段名/语义不许改，D1..D8 一并执行）
- 服务：`agent-team-blackboard`（龙虾 <host>:9940），**version 自报 1.7**（1.6 → 1.7）
- 本次范围（严格限定）：契约 §4 表设计 + §5 接口 + §4.3 归档 + 空闲超时 sweep，**全部落在 9940 的 `blackboard_svc`**
- 明确不在本次范围（一行未改）：`a2a_svc.py`（§6 服务端回放）、`cli_backend.py`（§8 codex 内存字典）、`team.py`（§7 默认会话）、`blackboard_view.py`（看板）
- 结果：**本批可做的判据 5/5 PASS**（判 2、3、4、5、8 全真跑）；判 1、6、7 依赖成员机侧改造，**本批未覆盖**（见 §9）

## 0. 改动文件清单 + 本机/龙虾 md5 对照

| 文件（绝对路径） | 本机 md5 | 龙虾 md5 | 说明 |
|---|---|---|---|
| `<repo>\agent-team\blackboard_svc.py` | `eafebca0412a3502d578c100f0929ca1` | `eafebca0412a3502d578c100f0929ca1` | VERSION 1.6 → 1.7；新增会话业务层 + 5 个 `/sess` 路由 + sweep 扩一块 |
| `<repo>\agent-team\sql\agent_session_schema.sql` | `1914cb8427b1edfba0667e2faa62997a` | `1914cb8427b1edfba0667e2faa62997a` | **新建**：两张表 + 5 个守卫触发器 + 3+2 索引 |
| `<repo>\agent-team\tests\test_blackboard_svc.py` | `9567ded2bca3db3453b4b41ad586fa9c` | （未部署，单测只在本机跑） | 新增 `SessFakeDB` + `SessTest` 12 条；`test_25b` 版本号钉到 1.7 |
| `<repo>\agent-team\P7-验收记录-Agent会话.md` | 本文件 | —— | 验收记录 |

```
$ md5sum blackboard_svc.py sql/agent_session_schema.sql tests/test_blackboard_svc.py      # 本机
eafebca0412a3502d578c100f0929ca1 *blackboard_svc.py
1914cb8427b1edfba0667e2faa62997a *sql/agent_session_schema.sql
9567ded2bca3db3453b4b41ad586fa9c *tests/test_blackboard_svc.py
$ ssh lobster "md5sum <deploy-dir>/blackboard_svc.py <deploy-dir>/sql/agent_session_schema.sql"
eafebca0412a3502d578c100f0929ca1  <deploy-dir>/blackboard_svc.py
1914cb8427b1edfba0667e2faa62997a  <deploy-dir>/sql/agent_session_schema.sql
```

- 部署前备份：`<deploy-dir>/blackboard_svc.py.bak-20260920-195752`（131814 字节 = v1.6 现网那份，md5 `da51bd76...`）
- 回滚：`ssh lobster "cp -p <deploy-dir>/blackboard_svc.py.bak-20260920-195752 <deploy-dir>/blackboard_svc.py && systemctl restart agent-team-blackboard"`（表可以留着，v1.6 代码不认识它们，互不影响）

```
$ ssh lobster "systemctl restart agent-team-blackboard; sleep 3; systemctl is-active agent-team-blackboard"
active
$ curl -s http://<host>:9940/healthz | python -c "...print(d['version'],d['sess'])"
1.7 {'swept_at': '2026-09-20T12:00:52Z', 'sweeps': 1, 'archived': 1, 'sweep_interval_s': 300, 'last_error': None}
$ ssh lobster "journalctl -u agent-team-blackboard -n 6 --no-pager | grep -i sweep"
20:00:52 INFO 兜底 sweep 线程已启动（每 300s 一轮：tmp 到期销毁 + session 空闲超时归档；/healthz 的 tmp_swept_at / sess_swept_at 各自自报）
20:00:52 INFO session 兜底 sweep 归档 1 场
```

判据用 `/healthz` 自报的 `version`（1.6 → **1.7**）+ **`sess_swept_at` / `sess` 两个新字段出现**，不看 systemd 状态。

## 1. 建表幂等 + `\d` 结构回读

```
$ ssh lobster 'docker exec -i agent-team-pg psql -U bb -d blackboard -v ON_ERROR_STOP=1 -f - < <deploy-dir>/sql/agent_session_schema.sql; echo FIRST_EXIT=${PIPESTATUS[0]}'
... CREATE TRIGGER / DO
FIRST_EXIT=0
$ （同一条命令第 2 次）
SECOND_EXIT=0
```

连跑两次 exit=0（`CREATE TABLE/FUNCTION/INDEX IF NOT EXISTS` + `DROP TRIGGER IF EXISTS` + 补挂约束的 DO 块）。

```
$ docker exec -i agent-team-pg psql -U bb -d blackboard -c '\d agent_sessions' -c '\d agent_session_turns'
 agent_sessions：session_id text PK / topic text NOT NULL / title / owner text NOT NULL /
   members jsonb NOT NULL DEFAULT '[]' / state text NOT NULL DEFAULT 'open' / created_at timestamptz NOT NULL now() /
   last_turn_at timestamptz / idle_ttl_s int NOT NULL DEFAULT 1800 / turn_count int NOT NULL DEFAULT 0 /
   closed_at / close_reason / archive_ref bigint / note
 Indexes: agent_sessions_pkey(session_id)
          agent_sessions_topic_created(topic, created_at DESC)     ← 契约 §4.1 点名要的
          agent_sessions_state_last_turn(state, last_turn_at)
          agent_sessions_owner_created(owner, created_at DESC)
 Check: state_ck(open|closed|expired)、close_reason_ck(manual|idle|owner)、idle_ttl_ck(60..86400)、
        closed_ck(终态必须带 closed_at 与 close_reason)、turn_count_ck(>=0)、members_ck(jsonb array)
 Triggers: agent_sessions_immutable_cols BEFORE DELETE OR UPDATE FOR EACH ROW
           agent_sessions_no_truncate      BEFORE TRUNCATE FOR EACH STATEMENT

 agent_session_turns：turn_id bigserial PK / session_id text NOT NULL REFERENCES agent_sessions /
   seq int NOT NULL / from_member / to_member / role text NOT NULL / content / content_ref / bytes int /
   ts timestamptz NOT NULL now()
 Indexes: agent_session_turns_pkey(turn_id)、agent_session_turns_seq_uk UNIQUE(session_id, seq)
          agent_session_turns_session_seq_desc(session_id, seq DESC)、agent_session_turns_session_ts(session_id, ts DESC)
 Check: role_ck(user|assistant|system)、bytes_ck(>=0)
 Triggers: agent_session_turns_append_only BEFORE INSERT OR DELETE OR UPDATE FOR EACH ROW
           agent_session_turns_no_truncate BEFORE TRUNCATE FOR EACH STATEMENT
           agent_session_turns_touch_bi    AFTER INSERT FOR EACH ROW   ← last_turn_at / turn_count 由它维护（D1）
```

列名/型/索引与契约 §4.1、§4.2 **逐条对齐**，`topic NOT NULL`（D8）在列上钉死。

**PASS**

## 2. 判 2 —— 表纪律（真跑 SQL，看的是 DB 自己的报错）

| 尝试（`psql -f`，`ON_ERROR_STOP=1`） | 真实输出 | exit |
|---|---|---|
| `UPDATE agent_session_turns SET content='篡改后的正文' WHERE seq=1` | `ERROR: agent_session_turns.content 不允许改写（只允许归档时单向清空一次）` + `HINT: 要说新的话请追加一条新 turn` | 3 |
| `DELETE FROM agent_session_turns WHERE seq=1` | `ERROR: agent_session_turns 不允许 DELETE（对话流水不可改不可删）` | 3 |
| `TRUNCATE agent_session_turns` | `ERROR: agent_session_turns 不允许 TRUNCATE（对话流水不可改不可删）` | 3 |
| `UPDATE agent_sessions SET topic='p7-renamed'`（冻结列） | `ERROR: agent_sessions.topic 不可变（D8：话题决定这次会话归到哪儿）` | 3 |
| `UPDATE agent_sessions SET state='open'`（已 closed 回退） | `ERROR: agent_sessions.state 只允许 open -> closed|expired（closed -> open）` | 3 |
| `INSERT INTO agent_session_turns`（父会话已 closed） | `ERROR: 会话 as-20260920-115818-jl3o 已是 closed 态，不能再追加轮次（契约 §4.3：归档后只读）` | 3 |
| **对照**：正常 `INSERT`（open 会话） | `INSERT 0 1`，`seq` 触发器给 1/2/3、`bytes` 触发器给 68/81/70，会话侧 `turn_count` 自动 0→3、`last_turn_at` 非空 | 0 |

最后一条对照很关键：护栏没有误伤正常写入，且 `seq`/`bytes`/`last_turn_at`/`turn_count` 都是**库自己算的**，不靠调用方自觉（D1）。

```
--- 会话侧：触发器自动维护 ---
       session_id        | state | turn_count | has_last_turn_at
 as-20260920-120036-3fnc | open  |          3 | t
```

**PASS**

## 3. 判 3 —— `topic` 检索 + 与黑板/会议同一值域

```
$ curl -s -X POST http://<host>:9940/sess -H "Authorization: Bearer <hermes token>" \
  -H 'Content-Type: application/json' \
  -d '{"topic":"p7-sess-accept","title":"P7 final close path","members":["codex","claudecode"],"idle_ttl_s":900}'
{"ok": true, "session_id": "as-20260920-120036-3fnc", "topic": "p7-sess-accept", "state": "open",
 "title": "P7 final close path", "owner": "hermes", "members": ["codex","claudecode"],
 "idle_ttl_s": 900, "turn_count": 0, "created_at": "2026-09-20T12:00:36Z"}

$ curl -s -H "Authorization: Bearer <token>" 'http://<host>:9940/sess?topic=p7-sess-accept'
{"ok": true, "count": 1, "sessions": [{"session_id":"as-20260920-115818-jl3o", ..., "topic":"p7-sess-accept",
  "owner":"hermes","state":"open","idle_ttl_s":900,"turn_count":0,"archive_ref":null,"open":true}], ...}

$ curl -s -H "Authorization: Bearer <token>" 'http://<host>:9940/sess?state=closed&topic=p7-sess-accept'
count 2 [('as-20260920-120036-3fnc','closed'), ('as-20260920-115818-jl3o','closed')]

$ curl -s -H "Authorization: Bearer <token>" 'http://<host>:9940/sess?state=bogus'
{"ok": false, "error": "bad_request", "detail": "state 只能是 open/closed/expired 之一"}  [HTTP 400]
$ curl -s ... '/sess?limit=abc'   -> {"ok": false, "error": "bad_request", "detail": "limit 必须是整数"} [400]
$ curl -s ... '/sess'（无 token） -> [HTTP 401]
$ curl -s ... '/sess/as-bad-id'（形状不对） / '/sess/as-99999999-000000-zzzz'（不存在）
      -> {"ok": false, "error": "not_found", "detail": "没有这个会话"} [HTTP 404]
$ curl -s -H "Authorization: Bearer <openclaw-nb token>" '/sess/as-20260920-120036-3fnc'   # 非成员
      -> [HTTP 403]
```

值域同源（事实查询，三处都是自由文本 + 缺省 `general`，没有任何白名单把 session 关在门外）：

```
                 src                 |          topic           | count
 agent_sessions                      | p7-sess-accept           |     2
 agent_sessions                      | p7-sess-idle             |     2
 blackboard_entries                  | ops/smoke                |     6
 blackboard_entries                  | regression               |     3
 roundtable_sessions                 | ops/acceptance-4elements |     2
```

**PASS**

## 4. 判 5 —— 手动 close → 归档 → 之后追加被拒

```
$ curl -s -X POST -H "Authorization: Bearer <token>" -H 'Content-Type: application/json' \
   -d '{"note":"P7 final close"}' http://<host>:9940/sess/as-20260920-120036-3fnc/close
{"ok": true, "session_id": "as-20260920-120036-3fnc", "state": "closed", "close_reason": "manual",
 "closed_at": "2026-09-20T12:00:42Z", "archive_ref": 6, "archived": true, "turn_count": 3,
 "already_closed": false, "turns_cleared": 3}

$ 重复 close（幂等，不重复归档）
{"ok": true, ..., "archive_ref": 6, "archived": true, "already_closed": true}     # shared_content 没有多出第二条

$ 非成员 close -> [HTTP 403]
$ DELETE /sess/<id> -> 501（契约 §5：不提供）   PUT /sess/<id> -> 501   POST /sess/<id>/turns -> 404
$ 取流水（未关闭时）GET /sess/<id>/turns?limit=2 -> count 2 [(2,'codex','assistant',63), (3,'hermes','user',70)]
       （只给最近 2 轮，且按 seq 升序回，读起来是顺着的时间线）
```

归档真源回读（**D6：归档≠删除**）：

```
--- 会话行 ---
 as-20260920-120036-3fnc | p7-sess-accept | closed | manual | has_closed_at=t | archive_ref=6 | 3 | P7 final close
--- 流水行：清正文留行 ---
 seq |   role    | content_cleared | bytes
   1 | user      | t               |   113
   2 | assistant | t               |    63
   3 | user      | t               |    70
--- 归档文档（shared_content）---
 content_id=6 scope=task task_id=sess-archive-as-20260920-120036-3fnc kind=note audience=["all"]
 sensitivity=internal status=active updated_by=blackboard_svc
 source_ref={"kind":"agent_session","topic":"p7-sess-accept","session_id":"as-20260920-120036-3fnc",
             "turn_count":3,"close_reason":"manual"}
 body:
   # Agent 会话归档 as-20260920-120036-3fnc
   - topic: `p7-sess-accept`   - 标题: P7 final close path
   - 发起方: hermes            - 参与成员: codex, claudecode
   - 状态: closed（关闭原因 manual）   - 轮次: 3
   - 创建/最近一轮/关闭/归档 四个时间
   - 收尾说明: P7 final close
   ## 对话流水
   ### #1 ... hermes → codex [user]       刚哥问的那个事我接了：今天 18:00 前把 P7 的前端字段名对齐给你，落成文档发群里。
   ### #2 ... codex → hermes [assistant]  收到。我按你说的字段名同步改，18:00 前对完。
   ### #3 ... hermes → codex [user]       补一条：字段名以契约 §4.1/§4.2 为准，别自己起名。
```

**关闭后追加**：HTTP 面没有追加口（`POST /sess/<id>/turns` → 404，契约 §5 没这个接口，落流水是批 2 的 `a2a_svc` 的事），所以"再追加被拒"由 DB 触发器兜底 —— 判 2 第 6 行即为该证据。

**PASS**

## 5. 判 4 —— 空闲超时归档（走的是 9940 进程里那个真 sweep 线程）

D1 判据是 `now() - last_turn_at > idle_ttl_s`，所以造场景的方式是：真实落一轮流水（触发器把 `last_turn_at` 刷成 now），再把 `last_turn_at` 拨回 1 小时前，会话 `idle_ttl_s=60`。

```
$ curl -s -X POST .../sess -d '{"topic":"p7-sess-idle","title":"P7 final idle path","members":["codex"],"idle_ttl_s":60}'
{"session_id": "as-20260920-120048-ikyv", "state": "open", "idle_ttl_s": 60, ...}
$ docker exec -i ... psql -f - < 14_idle.sql
INSERT 0 1          # 一轮流水
UPDATE 1            # last_turn_at 拨回 1 小时前
--- sweep 前：open + last_turn_at 已被拨回 1 小时前 ---
 as-20260920-120048-ikyv | open | 1 | 60 | 01:00:00

$ ssh lobster "systemctl restart agent-team-blackboard; sleep 4; journalctl -u agent-team-blackboard -n 6 | grep -i sweep"
20:00:52 INFO 兜底 sweep 线程已启动（每 300s 一轮：tmp 到期销毁 + session 空闲超时归档；...）
20:00:52 INFO session 兜底 sweep 归档 1 场

$ curl -s http://<host>:9940/healthz | python -c "...d['sess']"
1.7 {'swept_at': '2026-09-20T12:00:52Z', 'sweeps': 1, 'archived': 1, 'sweep_interval_s': 300, 'last_error': None}
```

回读真源：

```
--- 会话：expired + close_reason=idle + archive_ref 已回写 ---
 as-20260920-120048-ikyv | p7-sess-idle | expired | idle | has_closed_at=t | archive_ref=7 | 1
--- turns：清正文留行 ---
 seq | content_cleared | bytes
   1 | t               |    73
--- 归档文档：正文留住原话（content_id=7, scope=task, audience=["all"]）---
   - 状态: expired（关闭原因 idle）
   ### #1 ... hermes → codex [user]   空闲超时前说的话：别忘了我答应过 18:00 前给字段名。
```

### 5b. 周期轮（不是只有启动那一轮）

上面那次是"启动即刻跑一轮"；为了不把"周期轮到底会不会自己跑"混过去，另造一场空闲会话 `as-20260920-120128-6l2i`（`idle_ttl_s=60`，`last_turn_at` 拨回 1 小时），**不动服务、不重启**，等本轮 300s 周期到点（boot 轮 12:00:52 → 周期轮应落在 12:05:52 前后）：

```
（12:01:30 UTC 造好场景，此后全程没有任何 restart）
$ date -u +'now=%H:%M:%S UTC'
now=12:06:59 UTC
$ ssh lobster "journalctl -u agent-team-blackboard --since '12:05:00' --no-pager | tail -8"
20:05:37 INFO GET /healthz -> 200 from=- ip=<host>
20:05:52 INFO 会话空闲超时归档 session_id=as-20260920-120128-6l2i     ← 周期轮自己跑了
20:05:52 INFO session 兜底 sweep 归档 1 场
$ curl -s http://<host>:9940/healthz | ...
1.7 {'swept_at': '2026-09-20T12:05:52Z', 'sweeps': 2, 'archived': 2, 'sweep_interval_s': 300, 'last_error': None}
      （sweeps 1→2、archived 1→2、swept_at 自己走到 12:05:52）
$ 会话现状： as-20260920-120128-6l2i | expired | idle | archive_ref=8
$ 归档回读：content_id=8 scope=task task_id=sess-archive-as-20260920-120128-6l2i kind=note
            状态: expired（关闭原因 idle）；$1 正文： "等 300s 周期轮看着我被归档：别忘了我答应过 18:00 前给字段名。"
            turns seq=1 content_cleared=t bytes=85
```

**PASS**（D1 的"空闲超时"是活的：不用重启，300s 周期轮自己扫到并归档）

## 6. 判 8 —— 回归（`/entries`、`/search`、`/sessions`（会议）、`/tmp`、看板 9941/9942）

```
$ for p in "/entries?limit=2" "/search?q=blackboard&limit=3" "/sessions?limit=3" "/sess?limit=3" "/topic"; do curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer <token>" "http://<host>:9940$p"; done
/entries?limit=2             -> 200
/search?q=blackboard&limit=3 -> 200
/sessions?limit=3            -> 200
/sess?limit=3                -> 200（新面）
/topic                       -> 404（本来就没有这个路径，对照组）

$ POST /tmp（写）-> 200 tmp_id=t-IFHR32KC；GET /tmp/t-IFHR32KC -> 200 body="p7 final regression"；
  GET /tmp?mine=1 -> 200 mode=mine count 20        # 临时共享区写读全通
$ GET /search?q=回归校验 -> 200 mode=semantic count 10

$ ssh lobster "curl -s -o /dev/null -w ... http://<host>:9941/healthz ... /    http://127.0.0.1:9942/healthz ..."
9941 /healthz=200  9941 /=200（90103 字节，tailnet 免登录）
9942 /healthz=200  9942 /=302（公网实例要求先登录，符合 E1 设计）
```

看板读共享内容的几个面（归档写了 `shared_content`，确认没把看板搞崩、且归档真的上板）：

```
$ ssh lobster "curl -s 'http://<host>:9941/api/shared?limit=10'" | ...
count 7
7 task sess-archive-as-20260920-120048-ikyv ['all'] 会话归档 as-20260920-120048-ikyv（P7 final idle path）
6 task sess-archive-as-20260920-120036-3fnc ['all'] 会话归档 as-20260920-120036-3fnc（P7 final close path）
5 task sess-archive-as-20260920-115906-ista ['all'] 会话归档 as-20260920-115906-ista（P7 idle sweep）
4 task sess-archive-as-20260920-115818-jl3o ['all'] 会话归档 as-20260920-115818-jl3o（P7 acceptance session）
3 task p5-acceptance   ['all'] P5 验收证据与待办          ← 既有内容不受影响
2 permanent            ['all'] 集群节点清单
1 permanent            ['all'] 团队会议与共享内容系统使用约定（P5）
$ /api/shared/search?q=session -> 200 count 5
$ /api/sessions?limit=3 -> 200   /api/entries?limit=2 -> 200   /api/tracking?limit=3 -> 200   /api/search?q=blackboard -> 200
```

**PASS**

## 7. 本机单测

```
$ python -m unittest tests.test_blackboard_svc
Ran 149 tests in 6.9s
OK
```

改动前 137 条 → 改后 **149 条全绿**（新增 `SessFakeDB` + `SessTest` 12 条：开会话口径/id 形状、列表四种筛选与三种 400、详情与流水的 403/404、close 归档与幂等、DELETE 不提供、sweep 归档、healthz 新字段、版本号 1.7）。既有 137 条**一条没动语义**（只把钉版本号的 `test_25b` 从 1.6 改成 1.7）。

## 8. 终态真源（部署最终代码那一轮跑出来的）

```
--- agent_sessions 全量 ---
 as-20260920-115818-jl3o | p7-sess-accept | closed  | hermes | ["codex","claudecode"] |  900 | 2 | t | manual | 4
 as-20260920-115906-ista | p7-sess-idle   | expired | hermes | ["codex"]              |   60 | 1 | t | idle   | 5
 as-20260920-120036-3fnc | p7-sess-accept | closed  | hermes | ["codex","claudecode"] |  900 | 3 | t | manual | 6
 as-20260920-120048-ikyv | p7-sess-idle   | expired | hermes | ["codex"]              |   60 | 1 | t | idle   | 7
 as-20260920-120128-6l2i | p7-sess-idle   | expired | hermes | ["codex"]              |   60 | 1 | t | idle   | 8
--- agent_session_turns 全量（8 行，content 全清、bytes 留着、content_ref 未用）---
--- 触发器清点：5 个（sessions 2 + turns 3）---
--- 归档软引用不悬空 ---
 4/5/6/7/8 全部 target_exists=t, scope=task, audience=["all"]
```

## 9. 本批未覆盖（**真没做，不是"应该没问题"**）

| 判据 | 状态 | 原因 |
|---|---|---|
| 判 1 复现刚哥故障（A 承诺 → B 确认 → 问 A「你刚才承诺了什么」必须答出） | **本批未覆盖** | 要同时有"§7 默认会话（`team.py` 自动派生 session）+ §6 服务端回放（`a2a_svc` 把最近 N 轮拼成 `messages`）"，两条都在别的批；本批只到"中枢侧记下来 + 归档查得到" |
| 判 6 回放生效（绕开 CLI resume 仍能接上文） | **本批未覆盖** | 回放执行方是成员机的 `a2a_svc`（契约 §6），本次一行未改 `a2a_svc.py` |
| 判 7 长正文 >8 KiB 落 `/tmp`、turn 存 `tmp_id` | **本批未覆盖** | 同样落在成员机侧落流水那一步（§6）。表结构已备好（`content_ref` 列 + `content` 可空 + "清正文留行"纪律），但没有任何代码写 `content_ref` |
| D2 的服务端回放机制 | 本批未实现 | 同上，核心机制在 §6，属成员机批 |

结论：**本批交付的是"中枢侧的记与档"**（表 + `/sess` + 归档 + 空闲 sweep）。"不忘记"这件事要等 §6/§7/§8 那几批合上才谈得上判 1/6/7。

## 10. 妥协与自行判定的口径（契约没写死、我拍板的地方，逐条留痕）

1. **归档受众 = `["all"]`（不是"发起方+成员"）**。契约 §4.3 只写"任务级 scope"。第一次实现按成员名单写，实测发现**看板看不见**：只读侧每条查询都带 `audience @> '["all"]' OR audience @> '["某人"]'`（schema 注释与 `audience_clause()` 都在），成员名单等于归档完没人看得到；且契约 §10 批 2 明确要"归档文档的看板展示"，库里既有 3 条 team 内容也全是 `["all"]`。改成全员可见；"谁参与过"仍逐条记在 `source_refs` 与正文抬头（不丢信息）。已把先前两条成员名单的归档行就地改成 `["all"]`（`UPDATE shared_content SET audience=... WHERE content_id IN (4,5)`，成功）。
2. **`GET /sess` 列表不做"只列我参与的"过滤**，与会议 `GET /sessions` 一致（契约 §5 原文就是"口径与会议对齐"）。`GET /sess/<id>` / `/turns` / `/close` 三个按 id 的面**做成员校验**（非 owner 且不在 `members` → 403）。
3. **空闲基线用 `COALESCE(last_turn_at, created_at)`**。契约 D1 只写了 `last_turn_at`；但"开完没说过话"的会话 `last_turn_at` 是 NULL，若只按它判会**永远不过期**。故开完就没动静的会话按其 `created_at` 计时。
4. **归档 `expires_in_days` 固定 30 天**（`SESS_ARCHIVE_EXPIRES_DAYS`），并给每场会话建一个**任务容器** `shared_tasks.task_id = 'sess-archive-<session_id>'`（`scope='task'` 的 CHECK 要求 `task_id` 非空且外键指向 `shared_tasks`，不建容器写不进去；容器 `ttl_at` 与文档 `expires_at` 同 30 天）。这是契约没提但**必须**的一步。
5. **`kind='note'`**（不是新造 kind）：契约只说"一份 markdown 归档文档"，`shared_content.kind` 无白名单；用既有 `note` + `tags=["session","archive","topic:<x>",...]` + `source_refs[0].kind="agent_session"` 做区分，避免看板遇到未知 kind 不渲染。
6. **sweep 顺序：先原子认领（`UPDATE ... WHERE state='open' ... RETURNING`）再归档**，认领成功才写 `shared_content`，防并发重复归档；若归档那一步失败，会话已是终态但 `archive_ref` 仍空，**每轮 sweep 会补扫"终态但 `archive_ref IS NULL`"的会话**（不会静默丢痕）。单线程 sweep，且这一圈与 `tmp` 销毁共用同一个 daemon 线程（契约要求"复用已有的 tmp sweep 线程"，未新增定时单元/service）。
7. **close 幂等**：对已终态会话再 close 返回 200 + `already_closed=true` + 现状（含 `archive_ref`），**不重复归档**；不是 409。
8. **`seq` 与 `bytes` 由触发器给**：`seq = max(seq)+1`（同会话并发插入用 `pg_advisory_xact_lock` 串起来，另有 `UNIQUE(session_id, seq)` 兜底），`bytes` 缺省取 `octet_length(content)`。契约 §4.2 只说 `seq` 由"触发器/序列给"，`bytes` 是顺手补的。
9. **`agent_sessions` 也拒 DELETE / TRUNCATE**（契约只点名了 turns 的 UPDATE/DELETE/TRUNCATE）。理由同 D6 与 `tmp_shared` 先例：会话行是留痕的锚，删了 `turns` 的外键也就没了根。
10. **`members` 不校验"必须是龙虾 peers.json 里的登记成员"**。`/tmp` 是校验的（它是投递目标），但 session 的 `members` 是"谁参与过"的声明，成员机上可能还有没进 peers.json 的名字；校验了会把合法会话挡在 400。只做去重、类型与长度上限。
11. **`turn_count` 只增、终态必须带 `closed_at` + `close_reason`**（两处额外守卫），把"关没关过"钉成库里的事实，防手滑把终态写成半截。
12. **没有 `POST /sess/<id>/turns` 这种接口**：契约 §5 没列，不自行发明（`404`）。落流水是批 2 `a2a_svc` 的活；本批验收用直接落库模拟那一侧。

## 11. 运维要点（已补进技能 `pg-blackboard-ops`）

- 表：`agent_sessions` / `agent_session_turns`；路径 `/sess`（**别再和 `/sessions`（会议）搞混**，D5）。
- 改 `blackboard_svc.py` 后：`scp` + `systemctl restart agent-team-blackboard` + 核对 `/healthz` 的 `version` 与 `sess.swept_at`。
- 落库/查库：龙虾宿主**没有 `psql` 二进制**，一律 `ssh lobster "docker exec -i agent-team-pg psql -U bb -d blackboard ..."`；中文 SQL 走 `-f -` + stdin（别用 `psql -c "中文"`，也**别用 bash argv 传中文**，会被 mangle 成 `ä¼è¯`）。
- 只有登记过的 token 能进（`bbgw/claudecode/codex/hermes/lobster/openclaw-nb/quantum/top001`），`piagent`/`client` 一律 401，**那不是权限 bug**。
- 想让空闲超时立刻生效：造场景后 `systemctl restart`（启动即跑一轮），或等 300s 周期轮。
