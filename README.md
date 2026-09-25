# HACS

Human-Agent Communication System — 人机群聊前端 + 多 Agent 通信后端 / 网络 / 部署件。

本仓收纳「多 Agent 协作系统」的全部自研件：**对话台（群聊前后端）**、**团队通信层**、**三台云机的部署件**。

- 可见性：**private**（`shihezichen/HACS`）。脱敏达标后可一句话改公开，不用重建。
- 分支：`main`。
- 目标形态：一台新机器拿到本仓 + 一份 `.env`，照着 `docs/` 与 `deploy/` 就能把整套跑起来。

## 目录

| 目录 | 放什么 |
|---|---|
| `docs/` | 架构图、目录说明、数据流、术语表 |
| `backend/` | 对话台后端 `chat_svc.py`、群成员表 schema、`/members`、正文 `@` 解析、`reply_to` |
| `comm/` | 通信层：NATS 拓扑、守望器、A2A 桥、各 Agent daemon 与启动件 |
| `deploy/` | 量子 / 龙虾 / Top001 三台要装的东西，部署脚本 + 计划任务 |
| `frontend/ui/` | OpenClaw 产出的聊天前端渲染层（**本次骨架不入**，见下） |
| `tools/` | 密钥扫描脚本（提交硬门禁，`ui/` 一视同仁扫） |

## 入库纪律（硬规矩）

1. **私钥密钥零入库**。`.env`、`secrets/`、`*.pem`、`*.key`、`*.pass`、各类 token 一律进 `.gitignore`；配置里的口令全部写成 `${ENV}` 占位，配一份 `.env.example`。
2. **提交前跑扫描**：`python tools/scan_secrets.py .`，扫不干净不推。扫描范围一视同仁，`frontend/ui/` 不享豁免。
3. **大文件与模型权重不进 git**（见 `.gitignore`）。
4. **逐步入**：`frontend/ui/` 等 OpenClaw v0.1 交付 → 贴 `ui/` 清单 + 时间戳 → 扫描过 → 再单开一次全量提交。本次骨架不含 `ui/`，不把还在动的目录固化进历史。
5. 责任田：每个 Agent 的系统（文件/配置/DB）owner 是自己，他人可提要求、不直改。

## 当前状态

- 本次提交 = **仓库骨架**：目录结构 + `.gitignore` + `.env.example` + `docs/` + `tools/scan_secrets.py`。
- 真实代码（`backend/` `comm/` `deploy/`）在骨架核过后分批入，每批附扫描回显。
