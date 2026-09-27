# HACS

Human-Agent Communication System — 人机群聊前端 + 多 Agent 通信后端 / 网络 / 部署件。

本仓收纳「多 Agent 协作系统」的全部自研件：**对话台（群聊前后端）**、**团队通信层**、**节点部署件**。

- 可见性：**private**（`shihezichen/HACS`）。脱敏达标后可一句话改公开，不用重建。
- 分支：`main`。
- **架构先读**：**[`docs/architecture.md`](docs/architecture.md)** —— 终态架构文档：分层模型（`L0`–`L4` + `X` 横切 / `T` 工具面 / `C` 组合根）、端口表、四道硬门禁、六阶段刀序历史（刀1–刀32）、关键架构决定、失败模式与回滚、已知项。
- 规划与台账：**[`docs/HACS-分层规划-handoff.html`](docs/HACS-分层规划-handoff.html)**（回答「为什么这么分」）· **[`docs/HACS-分层台账.html`](docs/HACS-分层台账.html)**（回答「现在谁在哪一层、基线数字多少」）。
- 目标形态：一台新机器拿到本仓 + 一份 `.env`，照着 `docs/` 与 `deploy/` 就能把整套跑起来。

## 目录

| 目录 | 放什么 | 文件数 |
|---|---|---|
| `docs/` | 架构文档、分层规划 / 台账、API 与设计契约、验收记录、运维手册 | 25 |
| `backend/` | 对话台后端 `chat_svc.py`、L1 网关 `web_gateway.py`、L0 节点档案 `node_profile.py`，以及群聊 / 任务 / 会议 / 黑板 / 共享内容等域件 | 49 |
| `comm/` | 通信层：A2A 组合根 `a2a_svc.py` + 边缘端点 `a2a_edge.py` + 拓扑说明 | 7 |
| `deploy/` | 各节点要装的东西：部署脚本、计划任务 / 单元件、跑起来的 README | 23 |
| `frontend/ui/` | 聊天前端三件套：渲染壳 `group.html` / `group.css`、业务视图 `group_view.js`、传输客户端 `group_transport.js` | 12 |
| `tools/` | 门禁与工具面：`scan_secrets.py`（凭据扫描）、`check_layers.py`（分层门禁）、`gen_arch_doc.py`（架构文档生成器）、`push_hacs.py` + `sanitizers/`（脱敏入库）、`equivalence/`（等价装置与冻结快照） | 51 |
| `tests/` | 回归测试（`chat_svc` 套件改前即有 27F+8E 的**既有红**，只要求红点集合不变） | 19 |
| `agent-team/` | 通信 / MCP 侧补充件与契约文档（如 `docs/l5-roster-archive.md`） | 2 |
| `seed/` | 初始数据种子 | 1 |

> 文件数 = `git ls-files <目录> | wc -l` 实测值（改目录结构时请同步本表）。

## 关键文档

| 文档 | 内容 |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | 终态架构文档（分层 / 端口 / 门禁 / 刀序 / 决定 / 回滚 / 已知项），**入口** |
| [`docs/HACS-分层规划-handoff.html`](docs/HACS-分层规划-handoff.html) | 分层规划（五层 + 三横切、七条判据、六期落地） |
| [`docs/HACS-分层台账.html`](docs/HACS-分层台账.html) | 分层台账（件 → 层 → 端口 → 契约 → 门禁基线，唯一权威版本） |
| [`docs/l1-contract.md`](docs/l1-contract.md) | L1 通行层公共面契约（9 条 + 出处行号 + 使用方 + 兼容规则） |
| [`agent-team/docs/l5-roster-archive.md`](agent-team/docs/l5-roster-archive.md) | L0 名册档案与「档案 + 凭据 ⇒ 用户区」自证契约 |
| [`docs/l5-membership-audit.md`](docs/l5-membership-audit.md) | 成员入网只读对账器契约 |
| [`docs/公网入口运维手册-认证与隧道.md`](docs/公网入口运维手册-认证与隧道.md) | 公网入口（认证 + 隧道）运维手册 |
| [`docs/README-deploy.md`](docs/README-deploy.md) | 部署说明 |

## 入库纪律（硬规矩）

1. **私钥密钥零入库**。`.env`、`secrets/`、`*.pem`、`*.key`、`*.pass`、各类 token 一律进 `.gitignore`；配置里的口令全部写成 `${ENV}` 占位，配一份 `.env.example`。
2. **提交前跑扫描**：`python tools/scan_secrets.py .`，扫不干净不推。扫描范围一视同仁，前端件不享豁免。
3. **大文件与模型权重不进 git**（见 `.gitignore`）。
4. **走脱敏入库管线**：新增对外件先登记进清单（源仓 `tools/hacs_manifest.json`），由 `push_hacs.py` 做「脱敏 → 门禁 → 提交 → 推送」；**不要手工拷文件进本仓**，也不要用绕过脱敏器的窄路（`--add`）。
5. 责任田：每个 Agent 的系统（文件 / 配置 / DB）owner 是自己，他人可提要求、不直改。

## 当前状态

- 分层解耦工程**阶段 0–6 已全闭**（刀1–刀32）。基线（以 `docs/architecture.md` 第 3 节实测为准）：登记件 **83**、层间 import 边 **108**、登记端口 **17**。
- 门禁：**分层门禁** `tools/check_layers.py`（依赖方向为硬门禁，业务词 / 宿主硬编码 / 存储句柄 / 跨件私有面为提示级）；**等价装置** 19 台（每刀「零行为改变」的字节级证据）；**回归红点集合不变**；**凭据扫描** + 脱敏入库管线。
- 已知项（不消红、如实记账）：对账器 M6 `duplicate_route`（上游竞态）、`test_message_id_shape` 既有 flaky、若干发布面 / 判据待拍板项 —— 逐条见 `docs/architecture.md` 第 9 节。
