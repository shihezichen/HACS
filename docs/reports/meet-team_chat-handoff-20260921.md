# 会议纪要：人机对话台（team_chat）后端交接 · 2 项待拍板

- **session_id**：`meet-team_chat-handoff-20260921`
- **召集人**：hermes（总控）　**与会**：openclaw-nb
- **时间**：2026-09-21 10:28（北京时间）
- **通道**：团队 NATS 平面（`<host>:9930` → `a2a.v1.req.openclaw-nb` → 本机 teamd）
- **会议目标**：把「后端只读面已上线」的事实与两项交接口径当场定下，让前端可开工。

---

## 一、会前联检（开会状态，实测）

| 项 | 结果 |
|---|---|
| 在线名册（45s 窗口） | **8/8**（claudecode/codex/hermes/lobster/openclaw-nb/piagent/quantum/top001），openclaw-nb 在册 |
| 通配兜底探测 | `--members nobody-xyz-zzz` → **超时**（无通配订阅，群发汇总可信）|
| 服务面（龙虾） | nats / teamd / blackboard / tunnel / chat 五者均 `active` + `enabled` |

## 二、议程结论

### 议程 1 —— 后端只读面口径确认：**agree（无冲突）**
openclaw-nb 自述已 tailnet 实测对齐：`/healthz` ok、`/api/agents` 返回 `lobster`、未知端点
**501（确非 404）**；其前端 `api.js` 已按契约落实 `501/not_implemented` 分支用于开关按钮。

### 议程 2 —— 前端产物落点：**同源托管**（后端 9980 既出 API 也出静态）
openclaw-nb 提出的后端配合清单（**本会已确认，作为实现依据**）：

1. 静态根 = `agent-team/ui/`，**排除** `serve.py` / `mock.js`（其开发辅助件）
2. `/api/*`、`/healthz`、`/login`、`/account` **优先于静态**；静态不得覆盖 `/api/` 前缀
3. **SPA fallback**：非 API 命中的 `GET` → 回 `index.html`（200）
4. `/login`、`/account` **复用 `view_auth`**，前端不自建登录页
5. 缓存：`index.html` `no-cache`；v1 阶段 js/css 亦 `no-cache`
6. SSE 同源：`text/event-stream` + `X-Accel-Buffering: no` + 禁压缩缓冲
7. `.js` MIME 必须 `application/javascript`
8. 后端**不代管前端构建**

### 议程 3 —— 对接说明交付方式：**agree**
`docs/frontend-integration.md` 放 `<repo>/agent-team/docs/`；
**hermes 只写接口文档，不碰前端代码**（分工按刚哥定的：后端 hermes / 前端 openclaw-nb）。

---

## 三、落实事项（责任人 + 截止）

| # | 事项 | 责任人 | 截止 | 状态 |
|---|---|---|---|---|
| 1 | 只读面对接口径确认 | openclaw-nb | 本次会议 | ✅ 已确认 |
| 2 | 前端落点决定（同源托管 + 8 条清单） | openclaw-nb | 本次会议 | ✅ 已决定 |
| 3 | 写 `docs/frontend-integration.md` | hermes | 2026-09-21 | ⏳ |
| 4 | 后端实现**同源静态托管**（按上面 8 条） | hermes | 2026-09-22 | ⏳ |
| 5 | 落点决定收口进契约（走 v1.1 增补，不原地改冻结本） | hermes | 2026-09-21 | ⏳ |
| 6 | 后端第二档：`POST messages` + SSE 推流 | hermes | 按契约 §12.4 | ⏳ |

## 四、遗留与风险（跟进项，不许静默）

1. **本会只覆盖「后端只读面 + 落点 + 交付形式」三件事**；契约 §12.4 的 ②~⑤ 段（写/S‌SE/建会/旁听）
   尚未与前端逐项对齐，需在实现到那一段时**再开一次会**确认字段与事件名。
2. **`/login`、`/account` 复用 `view_auth`** 是 openclaw-nb 新提的需求 —— 现 `chat_svc.py` 尚未实现这两个页/口，
   已并入落实事项 4。
3. 议程 2 的清单里「静态不得覆盖 `/api/` 前缀」与「SSE 禁压缩缓冲」属**安全与推流正确性**要求，
   实现时须逐条自测，不接受"看着能用"。
4. 与**量子机无关**；本会未涉及量子机任何文件或服务。

## 五、附：本场发言查询方式

```bash
# 本场全部历史发言（严格按 session_id 精确匹配，不跨场）
python scripts/meeting_history.py --session-id "meet-team_chat-handoff-20260921"
```
