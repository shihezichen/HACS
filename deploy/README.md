# deploy/ — 四节点部署说明

覆盖 **thinkbook（本机）/ 龙虾 / 量子 / Top001** 四台节点：谁跑什么、怎么起、怎么自启、怎么升级回滚。

> 本文件是**现状（as-built）**，不是规划。上一版把三台机写成三套完全不同的件，是一个误导——
> 其实**通信层四台是对等的**，真正的不对等只有一层：**业务侧进程只放在中枢（龙虾）**。
> 部署件目录（`quantum/` `lobster/` `top001/`）目前**尚未落件**，systemd 模板在 `deploy/systemd/`；
> 现状部署是 `deploy_p1.py` 等一次性脚本 + 手工 scp 完成，落件工作待办。

---

## 一、一句话模型：两层结构

任何节点上的部署件，都归入两类之一：

| 层 | 内容 | 谁需要 |
|---|---|---|
| **通信层（对等）** | `a2a_svc.py` + `teamd.py` + `nats_client.py` + NATS 服务端 + 进程监管 | **每个节点都装**（thinkbook 是 Windows 计划任务，云机是 systemd） |
| **业务层（中枢独有）** | `chat_svc.py` / `blackboard_svc.py` / `blackboard_view.py` / 公网隧道 / 计算器 / 任务清单 | **只装在中枢节点（龙虾）** |

**判断法则**：一个新件要往哪台机放，只看它属于哪一层——
- 属于通信层 → 四台都放，形态对等；
- 属于业务层 → 只放中枢，其它节点零份。

>

---

## 二、四节点总览

| 节点 | tailnet | 运行目录 | 通信层 | 业务层 | 监管 |
|---|---|---|---|---|---|
| **thinkbook**（本机） | <host> | `<home>\agent-team` | A2A:9920 / teamd / group_watch | — | Windows 计划任务 |
| **lobster**（龙虾·中枢） | <host> | `<deploy-dir>` | a2a-svc / teamd / NATS | blackboard / view / chat-9984 / chat-pub / tunnel / calc / task_svc | systemd |
| **quantum**（量子） | <host> | `<deploy-dir>` | a2a-svc / teamd / NATS | — | systemd |
| **top001** | <host> | `<deploy-dir>` | a2a-svc / teamd / NATS | — | systemd |

**真实差异只在两处**，且都不是"规划"造成的：

1. **承载的 Agent 数不同**——thinkbook 一个节点挂了 5 个 Agent（hermes / openclaw-nb / codex / piagent / claudecode），
   量子、Top001、龙虾各只有 1 个自己的 Agent（quantum / top001 / lobster）。同一个 teamd 进程可带多成员。
2. **业务侧进程只在龙虾**——chat_svc / blackboard / 隧道等是**业务进程不是通信层**，全局只需一份，
   落在中枢是因为公网入口与对话台都在龙虾。这是架构决策，不是"龙虾特殊"。

**NATS 也不是龙虾独有**：三台云机都跑 NATS，组成集群（主干 9930 / 集群 9931）。
`--nats-host <host>:9930,<host>:9930` 是**故障转移双地址**（主龙虾、备 top001），
量子自己那份既是成员也参与集群。**"NATS 通信层对等"这一点，上一版 README 完全没说出来。**

---

## 三、通信层（四节点对等）

### 3.1 组件与端口

| 组件 | 端口 | 说明 |
|---|---|---|
| `a2a_svc.py` | 9920/TCP | A2A 平面，每节点一份，只绑本机 tailnet `100.x`，**绝不绑 0.0.0.0** |
| NATS 服务端 | 9930（客户端）/ 9931（集群）/ 9222（监控） | 三台云机组成集群 |
| `teamd.py` | 9960（管理口，只绑 127.0.0.1） | NATS 客户端守护：接点名 → 转本机 A2A → 转 Agent backend |
| `nats_client.py` | — | 极简 NATS 客户端（只用标准库，无 pip 依赖） |

依赖：**只需 Python 3.9+ 标准库**，无任何 pip 包。

### 3.2 平台差异（只有"怎么自启"不同）

- **云机（龙虾 / 量子 / Top001）**：systemd，单位名 `a2a-svc.service` / `agent-team-teamd.service`（Top001 的 A2A 单位名为 `a2a-svc.service`，量子为 `agent-team-a2a.service`——历史命名遗留，功能一致）。
- **thinkbook（Windows）**：计划任务 `AgentTeamA2ASvc` / `AgentTeamTeamd`（触发器=开机/登录 + 看门狗，`MultipleInstances IgnoreNew`）。

### 3.3 启动形态（三台云机完全一致）

```bash
python3 <deploy-dir>/teamd.py --member <member> \
  --nats-host <host>:9930,<host>:9930 --user <member> \
  --password-file /etc/nats/<member>.pass \
  --a2a-base http://<node-ip>:9920 --token-file <deploy-dir>/client-token \
  --admin-port 9960
```

A2A：

```bash
python3 <deploy-dir>/a2a_svc.py --host <node-ip> --port 9920 \
  --config <deploy-dir>/agents.json --peers <deploy-dir>/peers.json
```

可选 `--advertise-host/--advertise-port`（只影响 Agent Card 里对外公布的 url）。
环境变量 `A2A_ALLOW_ANON=1` 跳过鉴权，**仅限本地调试，生产严禁**。

---

## 四、业务层（仅中枢 = 龙虾）

只有**中枢节点**放这些，其它节点零份：

| 件 | systemd 单位 | 端口 / 入口 | 作用 |
|---|---|---|---|
| `chat_svc.py` | `agent-team-chat-9984.service` | <host>:9984（默认文档 group.html） | 对话台 · tailnet 实例 |
| `chat_svc.py` | `agent-team-chat-pub.service` | 127.0.0.1:9980 | 对话台 · 公网入口实例（`chat.example.com`） |
| `blackboard_svc.py` | `agent-team-blackboard.service` | tailnet 9940 | 黑板服务（P3） |
| `blackboard_view.py` | `agent-team-blackboard-view.service` | 9941 | 黑板只读视图 |
| `blackboard_view.py` | `agent-team-blackboard-view-pub.service` | 127.0.0.1:9942（mode=public） | 看板公网入口 |
| `tunnel.py` | `agent-team-tunnel.service` | `board.example.com` → 127.0.0.1:9942 | Cloudflare 命名隧道 |
| `calc/` | `calc-svc.service` | 127.0.0.1:9985 | 团队计算器后端 |
| `task_svc.py` | `task_svc.service` | 9981 | 群任务清单 |

> `agent-team-chat.service`（旧单位，disabled）已被 `chat-9984` + `chat-pub` 取代，保留仅为回滚。

---

## 五、Agent 承载与调用链

### 5.1 每节点 `agents.json` 只配**本机承载**的 Agent

| 节点 | `agents.json` 里的成员 | backend |
|---|---|---|
| thinkbook | hermes / openclaw-nb / codex / piagent / claudecode | 8642 / 18789 / 9970（同后端按 model 分） |
| lobster | lobster | `127.0.0.1:19805/v1` |
| quantum | quantum | `127.0.0.1:18789/v1` |
| top001 | top001 | `127.0.0.1:18789/v1` |

请求打到**本节点未配置**的 agent_id → `404 {"ok": false, "error": "unknown_agent"}`。

### 5.2 鉴权：`peers.json` 只存 sha256

```
{ "hermes": "<sha256>", "lobster": "<sha256>", "quantum": "<sha256>", ... }
```

- 调用方持**明文 token**，服务端只存 **sha256**，`chmod 600`。
- 请求头 `Authorization: Bearer <明文token>`，**不支持 `sha256:` 前缀**。
- 身份**只由 token 反查得出，绝不采信请求体里的 `from` 字段**。

### 5.3 消息投递链（每个 Agent 方式相同）

```
点名者 ──PUB a2a.v1.req.<member>──▶ NATS 集群
   │  （subject 按成员切分；每成员只订阅自己的一个 subject）
   ▼
该 member 的 teamd 收到 → 有界队列 → worker 池 → 同步 HTTP：
   POST http://<node>:9920/a2a/<member>/messages
   ▼
本机 a2a_svc 鉴权（token sha256 反查 peers.json）
   ▼
转本机 Agent backend（如 openclaw-nb → 127.0.0.1:18789/v1）
```

关键性质：

- **推模式，非拉模式**：NATS 推给 teamd → 队列 → worker → 同步 HTTP。
- **同机多成员共用一个 teamd 进程，但每成员一条独立 NATS 连接、各自一套凭据**
  （`--user hermes=hermes --password-file ...`）；一个成员掉线只影响它自己那条循环。
- **队列满或成员在途超限 → 立刻回 busy 拒收**（不排队、不静默丢）。
- **NATS 账号权限按成员隔离**：每用户只能 `publish` 自己的 `team.hb.<member>` / `team.probe.<member>`，
  `subscribe` 则放开到 `a2a.>` / `team.>` / `bb.>`。即**发心跳限定身份，收点名不限**。

**结论：所有 Agent 走完全相同的投递机制，差异只在"哪台机承载了几个成员"和"backend 指向谁"。**

### 5.4 发现与注册：静态配置 + 心跳，**没有自动注册**

- **注册不存在**：新加一个 Agent = 改对应节点 `agents.json` + 发 token 并写入各机 `peers.json` + 重启该机 teamd。手工，非自动。
- **发现靠心跳**：每个成员的 teamd 每 30s 发 `team.hb.<member>`；`team_roster.py` 订阅 `team.hb.>` 收集在线成员。
- **补拉**：teamd 上线后向中枢 blackboard 领回离线期间欠它的消息（`--blackboard-base` 给了才做）。

---

## 六、硬规矩

1. **部署脚本里不得出现明文口令**：一律 `os.environ` / `${ENV}`，脚本只做「读环境变量 → 落地配置 → 起服务」。
2. **SSH 私钥不入库**：脚本里的密钥只写**路径**（`${DEPLOY_SSH_KEY}`），内容零入库。
3. **NATS 证书 / 口令文件零入库**：配置模板里用户名可留，`*.pass` 与证书一律 `.gitignore`。
4. **阿里云 AK / 飞书 secret / Top001 口令**：只从 `.env` 读，`.env` 进 `.gitignore`。
5. **拓扑可画，真实 IP 不可写**：文档里用占位（`<tailnet-ip>`）。
6. **零硬编码**：路径 / IP / 端口 / 主机名 / 身份一律走环境变量或相对推导，任何节点不得出现硬编码绝对路径（含注释示例）。
7. 每台机的启动件（计划任务 / systemd）随该机目录一起交，写明「谁是 owner」。

## 七、门禁回显

```bash
python tools/scan_secrets.py deploy/
python tools/scan_secrets.py comm/
```

---

## 八、排错速查

**`ss -lntp | grep 9920` 看不到监听**
→ 绑错地址。确认 `--host` 是本机 tailscale0 的 100.x：`ip -4 addr show tailscale0`。
tailscaled 未起时绑定失败，看 `journalctl -u a2a-svc` 的 `Cannot assign requested address`。

**`ss` 显示 `127.0.0.1:9920`**
→ 忘传 `--host`，走了默认值，外部访问不到。

**`curl: (7) Connection refused`**
→ 进程没起（`systemctl status`），或防火墙拦了。只放行 tailnet：`zone=trusted --add-interface=tailscale0`。

**一直 401**
→ 三种可能：`peers.json` 里存的不是 sha256 而是明文；请求头写了 `Bearer sha256:xxx`（不支持前缀）；
`peers.json` 没加载成功（启动日志会打 `peers 表为空` 警告）。

**总是 404 unknown_agent**
→ 该 agent 没配在**这个节点**的 `agents.json`。用 `/agents` 看本节点实际承载了什么。

**502 backend_error**
→ 本机后端没起或路径不对。`detail` 里是后端原始响应前 200 字，直接 curl `<backend>/chat/completions` 复现。

**504 backend_timeout**
→ 后端太慢，调大请求体里的 `timeout_s`（默认 120 秒）。

---

## 附：本次重写说明

- 旧版把它写成"量子=运行时+客户端+daemon / 龙虾=chat+桥+团队管理 / top001=后台+反代"，三台各自一摊、看不出共性。
- 新版明确：**通信层四台对等，业务层只在中枢**；并补上实测的端口、单位名、账号权限、投递链与鉴权细节。
- 待办：`deploy/quantum/` `deploy/lobster/` `deploy/top001/` 三个子目录落件（把现状的 scp 脚本收编为 `tools/deploy.py`）。
