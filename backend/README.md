# backend/ — 对话台后端

## 本目录放什么

| 文件 | 说明 | 来源 |
|---|---|---|
| `chat_svc.py` | 群聊后端主服务（HTTP + SSE，`/api/sessions/...`） | 龙虾 `<deploy-dir>/chat_svc.py` |
| `sql/` | 群成员表 / 消息表 schema（`chat_sessions`、`chat_messages`） | 龙虾 `<deploy-dir>/sql/` |

覆盖面（对应契约 §I）：会话枚举 / 建群 / 发消息 / 取历史 / 成员清单 `/members` / 正文手打 `@` 解析 / `reply_to` / 群成员表 schema。

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
