# HACS

Human-Agent Communication System — 人机群聊前端 + 多 Agent 通信后端 / 网络 / 部署件。

本仓收纳「多 Agent 协作系统」的自研件：**对话台（群聊前后端）**、**团队通信层**、**节点部署件**。

- 可见性：**private**（`shihezichen/HACS`）。脱敏达标后可一句话改公开，不用重建。
- 分支：`main`。
- **架构先读**：**[`docs/architecture.md`](docs/architecture.md)** —— 终态架构文档：分层模型（`L0`–`L4` + `X` 横切 / `T` 工具面 / `C` 组合根）、端口表、四道硬门禁、六阶段刀序历史（刀1–刀36）、关键架构决定、失败模式与回滚、已知项。
- 规划与台账：**[`docs/HACS-分层规划-handoff.html`](docs/HACS-分层规划-handoff.html)**（回答「为什么这么分」）· **[`docs/HACS-分层台账.html`](docs/HACS-分层台账.html)**（回答「现在谁在哪一层、基线数字多少」）。
- 目标形态：一台新机器拿到本仓 + 一份 `.env`，照着 `docs/` 与 `deploy/` 就能把整套跑起来。

## 目录

| 目录 | 放什么 | 文件数 |
|---|---|---|
| `hub/` | **相① 中枢**（只装中枢机）：`l1_relay/` 中转配置生成 · `l2_adapter/` 中枢侧适配（`pg_literals` · `agent_model`）· `l3_domain/` 协作域业务件（`chat_svc` · `blackboard_svc` / `blackboard_view` · `roundtable` · `rt_remind` · `shared_content` · `team_group` · `embedder` · `backfill_embeddings` · `group_watch_pending`）＋ `l3_domain/sql/`（建表脚本 14 份）· `l4_experience/` L4 呈现（静态壳 `group.html` / `group.css` + 视图 / 传输件 + 开发用 `serve.py`）· `x/` 横切（`view_auth`：账号 / 会话 / 角色 / 密级） | 46 |
| `node/` | **相② 节点**（四台对等）：`l0_host/` 宿主事实（`node_paths` · `node_profile` · `nodelet` · `member_roster`）· `l1_transport/` L1 通信层（`nats_client` · `teamd` · `team_broadcast` · `team_call` · `team_cli_core` · `team_roster` · `web_gateway` · `group_watch` 投递侧，＋ `a2a/` 边缘端点与调用客户端）· `l2_adapter/` 节点侧适配（`agent_usage` · `cli_backend` · `ctx_watermark`）· `c/` 组合根入口（`team.py` · `a2a_svc.py` · `group_watch.py`） | 28 |
| `deploy/` | 各节点要装的东西：部署脚本、计划任务 / 单元件、环境变量样例（`chat.env.example`）、跑起来的 README | 19 |
| `tools/` | 门禁与工具面：`check_layers.py`（分层门禁）+ `hacs_layers.json`（层归属真源）、`scan_secrets.py`（凭据扫描）、`gen_arch_doc.py`（架构文档生成器）、`equivalence/`（等价装置 + `_old/` 冻结快照） | 57 |
| `tests/` | 跨件回归测试（含原 `backend/tests/*`） | 22 |
| `docs/` | 契约与规划文档：`architecture.md`（入口）· 分层规划 / 台账 · API 与设计契约 · 运维手册 · `l0-nodelet.md` / `l5-nats-ops.md` / `l5-roster-archive.md` / `l5-membership-audit.md`；**过程件**（`P1`–`P7` 验收记录、工作简报）已下移 **`docs/archive/`** | 19（+ `archive/` 9） |
| `seed/` | 初始数据种子 | 1 |

> 文件数 = `git ls-files <目录> | wc -l` 实测值（改目录结构时请同步本表）；`__init__.py` 包标记计入。
>
> **四个旧顶层目录已退役**：`backend/` · `comm/` · `frontend/` · `agent-team/` —— 其中的件已按分层归入
> `hub/` 与 `node/`（对照关系见 `docs/HACS-分层台账.html`）。唯一例外：`comm/a2a/restart_a2a.py` 属
> **部署件批**（要与 `node/l1_transport/a2a/a2a.env.example` 一起在节点机上实跑验证），暂留原位。

## 关键文档

| 文档 | 内容 |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | 终态架构文档（分层 / 端口 / 门禁 / 刀序 / 决定 / 回滚 / 已知项），**入口** |
| [`docs/HACS-分层规划-handoff.html`](docs/HACS-分层规划-handoff.html) | 分层规划（五层 + 三横切、七条判据、六期落地） |
| [`docs/HACS-分层台账.html`](docs/HACS-分层台账.html) | 分层台账（件 → 层 → 端口 → 契约 → 门禁基线，唯一权威版本） |
| [`docs/l1-contract.md`](docs/l1-contract.md) | L1 通信层公共面契约（9 条 + 出处行号 + 使用方 + 兼容规则） |
| [`docs/l5-roster-archive.md`](docs/l5-roster-archive.md) | L0 名册档案与「档案 + 凭据 ⇒ 用户区」自证契约 |
| [`docs/l5-membership-audit.md`](docs/l5-membership-audit.md) | 成员入网只读对账器契约 |
| [`docs/l5-nats-ops.md`](docs/l5-nats-ops.md) | 成员入网「退场 / 收敛」运维契约（动作侧） |
| [`docs/l0-nodelet.md`](docs/l0-nodelet.md) | L0 轻量节点守护 · 只读探针口契约 |
| [`docs/sys-path-debt.md`](docs/sys-path-debt.md) | `sys.path` 老债记账表（靠注入才能解析的裸名 import 待收口清单） |
| [`docs/公网入口运维手册-认证与隧道.md`](docs/公网入口运维手册-认证与隧道.md) | 公网入口（认证 + 隧道）运维手册 |
| [`docs/README-deploy.md`](docs/README-deploy.md) | 部署说明 |

## 系统功能总览（自底向上）

按层从底往上读：每层先说**这层解决什么问题**，再列这层提供的**能力块**（按能力归并，不是文件清单）。
层的一句话职责取自层归属真源 `tools/hacs_layers.json`，件数见 `docs/architecture.md` 第 2 节。

### `L0` 平台层 Host（进程监管 / 路径 / 凭据 / 日志 / 探针 / 部署件）

解决「这台机器长什么样、东西放在哪、凭据落在谁手里」—— 上层一律不自己猜宿主事实。

- **宿主事实唯一出处**：`node_paths.py`（宿主默认值，env 优先 > 平台惯例）+ `node_profile.py`（节点档案，两条铁律 = 只读 / 不编造；SQLite 一律 `mode=ro` 打开，读不到就返回空或原样抛，不把「打不开」偷偷变成「没有数据」）。
- **成员名册档案**：`member_roster.py` —— 只回答三件事：这个协作域有哪些成员、各自什么角色、它的口令真值落在哪台机的哪个文件。**口令绝不进档案**，不写路径不写 IP，不存权限串。
- **轻量节点守护 / 只读探针**：`nodelet.py`（9971）—— 不写文件、不启停单元、不输出凭据值也不输出路径全文，出口前机械复核。
- **公网入口与隧道**：`tunnel.py`（命名隧道）、`deploy_auth.py`（入口认证部署，9942）、`repair_gateway.py`（网关修复）、`snap_portal.py`（门户快照）。
- **入口验收**：`accept_auth.py`、`verify_portal_public.py`。

### `L1` 通信层 Transport（投信封 / 保投到 / 不重复 / 可观测 / 成员入网控制面）

解决「消息怎么送到对方手里」—— 只管传输，不含任何群聊 / 会议语义。

- **点对点呼叫 / 群发 / 召集名册 / 成员守护**：`team_call.py`（点对点调用封装）+ `team_broadcast.py`（广播投递）+ `team_roster.py`（订阅心跳收名册）+ `teamd.py`（守护：订阅点名 → 转本机 A2A，管理口只绑回环 9960），底座是纯标准库的 `nats_client.py`。
- **成员侧 CLI 的管道**：`team_cli_core.py` —— 连接与扇出（在线发现 / 单呼 / 并发池 / 心跳汇总）＋身份与口令解析＋输入读取与超时限幅＋**节流防轰炸**（去重窗口，带锁与原子落盘）。
- **A2A 通道**：`a2a_edge.py`（边缘端点实现：收发 / 鉴权 / 回合循环，9920）+ `a2a_call.py`（调用客户端）。
- **HTTP 入口件**：`web_gateway.py` —— 路由 / JSON 信封 / 前置与鉴权钩子，传输不含语义；全仓 HTTP 面统一收在这里。
- **成员入网控制面**：`gen_nats_conf.py` —— 服务端权限白名单与**角色模板的唯一出处**，支持成员退场（`--drop-user`）与 `--dry-run`（9930）。

### `L2` 适配层 Adapter（Agent 差异唯一落脚点：调用 / 能力 / 上下文用量）

解决「不同 Agent 长得不一样」—— 所有差异收在这一层，上层只看统一口径。

- **模型调用**：`agent_model.py`（模型富化执行器 + Agent 展示名兜底表；失败一律降级返回原因，绝不编造），`cli_backend.py`（CLI 类 Agent 的统一后端，9970）。
- **上下文水位**：`agent_usage.py` 是**唯一权威口径**（读取 / 窗口分母 / 后端判定 / 会话头），`ctx_watermark.py` 是对外薄壳。
- **存储字面量归一**：`pg_literals.py` —— pgvector 字面量与时间归一的单一出处（纯标准库，不持任何连接）。

### `L3` 协作域 Domain（群聊 / 待回应账 / 任务 / 会议 / 黑板 / 共享内容）

解决「人和 Agent 在一起干活的业务语义」—— 这层才有「群」「任务」「会议」这些词。

- **对话台**：`chat_svc.py`（9984 尾网实例，默认文档 = 群聊页；9980 公网入口实例只绑回环）。
- **群聊与会议**：`team_group.py`（临时群收发 / 临时会议 `meet` / 会议回放 `history`）、`roundtable.py`（圆桌会议经 NATS 推流）、`rt_remind.py`（会议提醒）、`group_watch.py`（群 → 点名投递桥）。
- **任务清单**：`task_svc.py`（群任务清单，9981）。
- **黑板与语义检索**：`blackboard_svc.py`（黑板服务，9940）+ `blackboard_view.py`（看板只读视图 9941，兼公网登录门户）+ `embedder.py`（向量化）+ `backfill_embeddings.py`（向量回填），块库走 PostgreSQL 9950。
- **共享与归档**：`shared_content.py`（临时共享区）、`group_transcript_archiver.py`（群记录归档）、`session_ctx_archiver.py`（会话上下文归档）。

### `L4` 呈现层 Experience（静态壳 / 业务视图 / 网关：同源托管 · 反代 · 鉴权跳转 · SSE 聚合）

解决「人在浏览器里看到什么」—— 前端已拆成三件套，壳里零内联脚本零内联样式。

- **群聊前端三件套**：`group.html`（渲染壳，只剩 HTML 骨架 + 1 个样式外链 + 6 个脚本外链）/ `group.css`（样式表）/ `group_view.js`（业务视图：渲染 · 状态 · 事件）/ `group_transport.js`（传输客户端：同源 `/api` 客户端 + 任务服务客户端 + 端点常量）。
- **开发用静态服务器**：`serve.py`（8080，**生产不用**）。

### `X` 横切（身份与授权 / 契约与事件总线 / 可观测与门禁）

- **身份与密级**：`view_auth.py`（账号 / 会话 / 角色 / 密级），可被任意层引用，不参与依赖方向判定。

### `C` 组合根（进程入口装配：只做装配与注入，不含业务与传输实现）

- **A2A 进程入口**：`a2a_svc.py` —— 注入 L2 能力 + 注册 `/ctx-usage`，实现在 L1 `a2a_edge.py`（9920）。
- **成员侧 CLI 进程入口**：`team.py` —— 只剩四件事：argv 解析、装配注入（L1 管道件 + L3 域件）、分派、打印。
- 规则：`C` 可 import 任意层，**任何层不得 import `C`**。

### `T` 工具面（巡检 / 驱动 / 验收 / 一次性脚本；不属于运行路径）

- **只读对账与自证**：`nats_membership_audit.py`（成员入网六项检查，只读 / 口令一个字节都不出门 / 纯函数 analyze）、`roster_derive_conf.py`（「档案 + 凭据 ⇒ 中枢用户区」逐字节自证）、`roster_creds_mirror.py`（中枢侧凭据镜像通道，只走 ssh stdin，出口只有 sha256 前缀）。
- **台账与文档**：`ledger_patrol.py`（台账巡检）、`gen_arch_doc.py`（终态架构文档生成器）。
- **探针**：`nats_probe.py`（连通性）、`probe_model_channel.py`（模型通道真打一次最小 prompt，把静默失效变成可判死的退出码）、`probe_chat_turn.py`（单轮对话）、`lenserver_probe.py`（远端主机）、`nodelet_gen.py`（节点守护单元生成与核对）。
- **验收与测试**：`verify_chat_e2e.py`、`verify_group_page.py`、`test_web_gateway.py`、`test_a2a_edge_caps.py`、`test_agent_model.py`。
- **等价装置与冻结快照**：`tools/equivalence/`（每刀「零行为改变」的字节级证据 + `_old/` 改前锚）。
- **命令行与实验件**：`taskctl.py` / `seed_tasks.py` / `post_group.py`（任务与群操作）、`calc_svc.py`（团队计算器后端，9985）+ `regression_calc.py` + `curve_evidence.py`、`server.py`（HACS 数据层骨架，8787，内存假数据未接真源）。

### 端口与用途

登记端口共 **17** 个（真源 `tools/hacs_layers.json` 的 `ports` 表；绑定面见 `docs/architecture.md` 第 4 节）。

| 端口 | 用途 |
|---|---|
| 9920 | A2A 边缘端点（每节点一份，只绑尾网地址） |
| 9930 | NATS 客户端口（明文 + 用户口令） |
| 9931 | NATS 集群路由 |
| 9932 | NATS 管理口（只绑回环） |
| 9940 | 黑板服务（尾网） |
| 9941 | 看板只读视图（尾网） |
| 9942 | 看板公网入口实例（只绑回环，由隧道暴露） |
| 9950 | PostgreSQL（黑板块库） |
| 9960 | 成员守护管理口（只绑回环） |
| 9970 | CLI 类 Agent 统一后端 |
| 9971 | 轻量节点守护的只读探针口（每节点一份，默认只绑回环） |
| 9980 | 对话台后端 · 公网入口实例（只绑回环） |
| 9981 | 群任务清单 |
| 9984 | 对话台后端 · 尾网实例（默认文档 = 群聊页） |
| 9985 | 团队计算器后端（只绑回环） |
| 8787 | HACS 数据层骨架（本地实验，未接真源） |
| 8080 | 前端开发服务器（仅本机开发用） |

### 节点与角色

四台节点，真源 `tools/deploy_targets.json`（`deploy.py` 本体零硬编码，SSH 目标与私钥路径一律经 `${VAR:默认值}` 解析）。

| 节点 | 通道 / 监管 | 角色与承载 | 受管件 |
|---|---|---|---|
| `thinkbook` | local / Windows 计划任务 | 业务侧常驻进程落这里：会话上下文归档、台账巡检、CLI 类 Agent 后端；成员侧 CLI 入口 `team.py` 及其依赖闭包也纳管在此 | 20 |
| `lobster` | ssh / systemd | NATS 中枢之一；对话台 `chat_svc.py` 的运行节点；群任务清单 9981 在这里跑 | 28 |
| `top001` | ssh / systemd | NATS 另一台中枢，与 `lobster` 经集群路由 9931 **对等** | 14 |
| `quantum` | ssh / systemd | 尾网协作节点 | 14 |

> 每台节点各有一份 A2A 边缘端点（9920，只绑尾网）与一份轻量节点守护探针口（9971，默认只绑回环）——
> 跨机协作走尾网面，管理口与「由隧道代理的实例」走回环面，公网从不直连进程端口。
> 代码编辑入口唯一（`code_root`），四台的运行目录都是它的下游副本。

### 应用层看得见的东西

- **群聊页**：浏览器打开对话台尾网实例（9984，默认文档就是群聊页），页面由渲染壳 / 业务视图 / 传输客户端三件套拼出。
- **对话台后端**：`chat_svc.py` —— 尾网实例 9984 + 公网入口实例 9980（只绑回环，由命名隧道对外）。
- **群任务清单**：`task_svc.py`（9981），命令行侧对应 `taskctl.py` / `seed_tasks.py`。
- **看板**：只读视图 9941（尾网）+ 公网入口实例 9942（只绑回环 + 登录门户，由隧道暴露）。
- **团队计算器**：`calc_svc.py`（9985，前后台配合的实验件）。
- **账本 / 台账巡检**：`ledger_patrol.py`（thinkbook 计划任务拉起）。
- **成员侧命令行**：`team.py` —— 点对点呼叫、群发、临时群、临时会议与回放，都从这个入口进。

细节（各件一句话职责、端口绑定面、四道硬门禁、六阶段刀序历史、关键架构决定与已知项）见
**[`docs/architecture.md`](docs/architecture.md)**。

## 跑法

均在**仓根**执行，用 Python 3（命令名 `python`）：

```bash
# 1) 分层门禁 —— 依赖方向是硬门禁，rc=0 才能提交（件数 / 层间边数也会一并打印）
python tools/check_layers.py

# 2) 跨件回归测试 —— 各自独立可跑，退出码即结果
python tests/test_cli_backend.py ; python tests/test_a2a_edge_caps.py ; python tests/test_web_gateway.py

# 3) 前端开发用静态服务器（仅本机开发；生产由对话台 chat_svc 同源托管）
python hub/l4_experience/serve.py --help
```

> `docs/archive/` 是**过程件**（阶段性验收记录与工作简报），不是上手必读；上手先读
> `architecture.md` → `docs/HACS-分层规划-handoff.html` → 本 README。

## 入库纪律（硬规矩）

1. **私钥密钥零入库**。`.env`、`secrets/`、`*.pem`、`*.key`、`*.pass`、各类 token 一律进 `.gitignore`；配置里的口令全部写成 `${ENV}` 占位，配一份 `.env.example`。
2. **提交前跑扫描**：`python tools/scan_secrets.py .`，扫不干净不推。扫描范围一视同仁，前端件不享豁免。
3. **大文件与模型权重不进 git**（见 `.gitignore`）。
4. **走脱敏入库管线**：新增对外件先登记进清单（源仓 `tools/hacs_manifest.json`），由 `push_hacs.py` 做「脱敏 → 门禁 → 提交 → 推送」；**不要手工拷文件进本仓**，也不要用绕过脱敏器的窄路（`--add`）。
5. 责任田：每个 Agent 的系统（文件 / 配置 / DB）owner 是自己，他人可提要求、不直改。

## 当前状态

- 分层解耦工程**阶段 0–6 已全闭**，含阶段 6 后收尾（刀1–刀36）。基线（以 `docs/architecture.md` 第 3 节实测为准）：登记件 **78**、层间 import 边 **108**、登记端口 **17**。
- 门禁：**分层门禁** `tools/check_layers.py`（依赖方向为硬门禁，业务词 / 宿主硬编码 / 存储句柄 / 跨件私有面为提示级）；**等价装置** 21 台（每刀「零行为改变」的字节级证据；本仓随发布收录 19 台，另 2 台未随发布 —— 1 台对照源不在发布面、1 台待登记；`_old/` 冻结快照不计作装置）；**回归红点集合不变**；**凭据扫描** + 脱敏入库管线。
- 已知项（不消红、如实记账）：对账器 M6 `duplicate_route`（上游竞态）、`test_message_id_shape` 既有 flaky、若干发布面 / 判据待拍板项 —— 逐条见 `docs/architecture.md` 第 9 节。
