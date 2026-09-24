# backend/ — 对话台后端

## 本目录放什么

| 文件 | 说明 | 来源 |
|---|---|---|
| `chat_svc.py` | 群聊后端主服务（HTTP + SSE，`/api/sessions/...`） | 龙虾 `<deploy-dir>/chat_svc.py` |
| `sql/` | 群成员表 / 消息表 schema（`chat_sessions`、`chat_messages`） | 龙虾 `<deploy-dir>/sql/` |

覆盖面（对应契约 §I）：会话枚举 / 建群 / 发消息 / 取历史 / 成员清单 `/members` / 正文手打 `@` 解析 / `reply_to` / 群成员表 schema。

## 入库记录

| 批次 | 内容 |
|---|---|
| 2026-09-24 | `chat_svc.py`（生产版）、`chat.env.example`、`sql/`（14 份 schema：chat / view_auth / blackboard / shared_content / roundtable / msg_log / agent_session / tmp_shared / p4 迁移…） |

**已处理清单**（照上表门禁逐项过，`tools/scan_secrets.py backend/` 零命中）：

1. **DSN 明文** → docstring 示例改 `${CHAT_PG_DSN}`；代码只读 `CHAT_PG_DSN` / `BB_PG_DSN` / `CHAT_DSN_FILE`
2. **tailnet IP / 主机名** → 零出现；监听地址走 `CHAT_HOST`
3. **身份常量** → `TEAM_DEFAULT_HUMAN_ID`；注释里的人名/成员 id 一律 `<human-id>`
4. **`.bak*` 旧版** → 不入仓（`.gitignore` 已拦 `*.bak-*`）
5. **绝对路径** → 新增 `CHAT_ENV_DIR`：`pg.env` / `agents.json` / `view-session.key` / `ssh/*` 全部 `os.path.join(APP_ENV_DIR, …)`；`admin-ui` 缺省 = 本文件同级；富化（SSH 到便宜执行器）的目标/密钥/远端命令全部走 env，缺省关

⚠️ 本批 `chat_svc.py` 含一处行为改动：**未登录访客的 `/login`、`/logout` 302 到门户登录页**（带 `next=`），
本地不再把群页面当登录页发回去。**配套的前端改动**（`api()` 遇 401 跳 `/login`）在 `frontend/ui/group.html`，
按「`ui/` 归 OpenClaw、交付冻结后单开一次提交」的纪律**本批不入**。

## 入库前必须处理的（门禁）

代码是**直接从生产机取的**，里面天然带着不许出仓的东西，逐项过：

1. **DSN 明文** → 改 `${CHAT_PG_DSN}` / `${BB_PG_DSN}`。
2. **tailnet IP / 主机名** → 改 `${NATS_HOST}` 等占位，不留真实拓扑。
3. **`DEFAULT_HUMAN_ID` 等身份常量** → 改 `${TEAM_DEFAULT_HUMAN_ID}`。
4. **`.bak*` 旧版** → 不进仓（旧版里的口令最容易被人忽略着带进来）。
5. **绝对路径**（`<deploy-dir>/...`）→ 改相对路径或 `${APP_DIR}`。

## 门禁回显

```bash
python tools/scan_secrets.py backend/
```

扫不干净，本目录不提交。
