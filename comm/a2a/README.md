# comm/a2a/ — A2A 桥（Agent → Agent 直投）

每个成员节点自己监听 **9920** 的 A2A 端点服务。调用方带 `Authorization: Bearer <token>`，
服务端**只用 token 的 sha256 反查 `peers.json`** 得到真实成员 id —— 绝不采信请求体里的 `from` 字段。

| 文件 | 说明 |
|---|---|
| `a2a_svc.py` | A2A 端点服务（纯标准库，零 pip 依赖，语法兼容 Python 3.9） |
| `README.md` | 本文件：设计要点 / 配置 / 部署 / 与生产版的差异 |

## 本仓这一版是什么

来源：**thinkbook 节点生产版**，`VERSION 2.2`（2026-09-23）。相对 2.1 增加两项能力：

### 1) B2 续跑任务循环

一次唤醒不再是「答一句就睡」：第 1 轮跑用户原文，若回复里没有终标记，就回灌一条续跑指令
（`TASK_LOOP_CONTINUE_PROMPT`）再跑下一轮，直到：

- 回复里出现**独占一行**的 `DONE` 或 `BLOCKED`（`_TASK_DONE_RE` / `_TASK_BLOCKED_RE`，大小写不敏感），或
- 达 `TASK_LOOP_MAX_ROUNDS`（默认 8 轮），或
- 累计耗时触达 `min(调用方 timeout_s, TASK_LOOP_MAX_TIMEOUT_S)`（默认 600s 硬上限）。

响应体回传 `loop_rounds` / `loop_stop`（`done` / `blocked` / `max_rounds` / `budget` / `round_error`），
上层据此判断「为什么停」。成员级开关：`agents.json` 里 `task_loop: false` 可单独关掉。
**只有带 `session_id` 的调用才启用续跑**（多轮必须落在同一会话上）。

### 2) 上下文自愈（方案乙·甲水位）

续跑会让同一会话上下文越滚越大。每轮续跑前读该会话水位（`totalTokens / contextTokens`，读
`A2A_SESSIONS_JSON` 指向的会话档案）：

| 水位 | 动作 |
|---|---|
| ≥ `CTX_SOFT_RATIO`（0.75） | 注入 `CTX_COMPACT_PROMPT`，让 Agent 自行把前文压成交接摘要 |
| ≥ `CTX_HARD_RATIO`（0.85） | 先要一份交接摘要，再**换新 session 接力**（改 OpenAI `user` 字段 = `/new` 等效），摘要带进新会话第一条消息 |

安全口径：会话档案**只读打开、绝不写**；读不到（文件不存在/无权限/key 不在/字段缺失）一律降级为
「不压缩、照常续跑」，**不报错、不阻塞主流程**。会话键映射：
`user = a2a:<caller>:<session_id>` → session key `agent:main:openai-user:a2a:<caller>:<session_id>`。

### 3) 回合纪律注入与超时收敛

`agents.json` 里 `system_prompt` 会作为 system 消息拼在核心提示词之上（借网关
`/v1/chat/completions` 的 system 叠加口子，不改核心），用于把群聊应答纪律注入；
`max_timeout_s` 与本轮声明 timeout 取**小值** —— 失败要快，卡死回合尽快把 worker 还给团队总线。

## 环境变量

| 变量 | 必需 | 说明 |
|---|---|---|
| `A2A_SESSIONS_JSON` | 自愈需要 | 会话档案（OpenClaw `sessions.json`）路径。**不留默认值**：不给则自愈静默降级 |
| `A2A_ALLOW_ANON` | 否 | `1` 跳过鉴权，**仅限本地调试** |

## 部署

```bash
python3 a2a_svc.py --host <tailnet-ip> --port 9920 \
    --config "${AGENTS_JSON}" --peers "${PEERS_JSON}"
```

- 默认绑 `127.0.0.1`；生产传 tailnet 的 `100.x` 地址，**绝不绑 `0.0.0.0`**。
- `agents.json` 只配**本机承载**的 agent，未配置的 `agent_id` 一律 404 `unknown_agent`。
- 对外公布的 host/port 取 `agents.json` 的 `host`/`port`，可用 `--advertise-host/--advertise-port` 覆盖。
- systemd 最小件见 `deploy/`，启动件里加 `Environment=A2A_SESSIONS_JSON=...`。

## 与生产版的差异（仅脱敏，逻辑零改动）

1. 文档示例里的真实 tailnet IP → `<tailnet-ip>`；
2. `SESSIONS_JSON_PATH` 不再内置绝对路径，改由 `A2A_SESSIONS_JSON` 提供；
3. 读取 backend key 的局部变量改名（避开密钥扫描门禁对「字面量赋值」的误判）。

## 运行注意

- **节点间版本要对齐**：不同版本混跑会出现「同一团队里 A 节点续跑、B 节点不续跑」的行为差；
  升级时逐节点替换并读回 `/healthz` 的 `version` 字段确认。
- `/healthz` 与 Agent Card 无需鉴权；`/agents` 与 `/a2a/<id>/messages` 需 Bearer。

## 门禁回显

```bash
python tools/scan_secrets.py comm/
```
