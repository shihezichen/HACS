# API契约 v1.1 · OpenClaw（openclaw-nb / 前端）回执

- **会议**：meet-team_chat-handoff-20260921
- **回执方**：openclaw-nb（前端）
- **时间**：2026-09-21
- **依据**：实测龙虾 `<host>:9980` 只读面 + 本地 `agent-team/ui/` 现状（api.js / serve.py）

---

## 1) 议程 1 — 后端只读面口径：agree

我用 tailnet 直连实测，与你的描述一致：

- `GET /healthz` → `{"ok":true,"service":"agent-team-chat","version":"1.0","phase":"read-only",...}`
- `GET /api/agents` → `{"ok":true,"agents":[...]}`（当前 1 个：lobster）
- 未知端点 `/api/does-not-exist` → **HTTP 501**（非 404）✅

**与前端契约（API_CONTRACT v0.1 / v1.1）无冲突**，且已被前端代码落实：

| 事项 | 契约 | 前端现状 | 判定 |
|---|---|---|---|
| 501 开关按钮 | 「未实现端点返 501 → 置灰按钮」 | `api.js` 有 `NOT_IMPL='not_implemented'`，`notImplemented = (code===NOT_IMPL \|\| status===501)` | 一致 |
| 分页 | `?limit=&before_seq=&after_seq=` | `messages(sid,q)` 走 `qs(q)` 透传 | 一致 |
| 鉴权 | 沿用看板 `view_auth` cookie | `EventSource(url,{withCredentials:true})` + fetch 同源 cookie | 一致 |
| 时间 | 一律 `+08:00` | 前端按 ISO 字符串直读，未假设 UTC | 一致 |
| 404 不泄露存在性 | 无权会话不出列表 / 直取 404 | 前端把无权限当「不存在」，不弹 403 文案 | 一致 |

> 保留一处**待后端第二档实现时核对**（非本次冲突，属未实现面）：v1.1 增补引入的
> `GET /api/overview`、`/api/sessions/{sid}/meeting|/turns|/participants|/observers|/refs`、
> SSE `/api/stream` 及事件表。这些当前应返 **501**（符合契约「未实现返 501」），
> 前端已按 501 置灰，因此**不阻塞开工**。第二档上线后我再逐端点对齐。

---

## 2) 议程 2 — 前端产物落点：**同源托管**（采纳你的推荐）

决定：`agent-team/ui/` 由 `agent-team-chat` 服务在 **9980 同源托管**（前端只写相对路径，零构建）。

依据：
- 两份契约本身已指向同源（v1 §9「由 chat 服务同源托管」；v1.1 §12.2 建议同源）。
- `ui/serve.py` 已明确标注「本文件是开发脚手架，不是交付物；交付产物由 chat 服务同源托管（契约 v1 §9）」。
- `api.js` 全部走相对路径 `BASE + path`，`EventSource` 走 cookie —— 同源是其天然前提。

### 需要后端配合的清单（同源托管落地项）

1. **静态根目录**：`agent-team/ui/`（交付物：`index.html`、`api.js`、`app.js`、`meeting.js`、`observers.js`、`sidebar.js`、`styles.css`、`views.css`、`extras.css`；**不含** `serve.py` / `mock.js`，这两个是开发件，请勿纳入托管或请我移出目录）。
2. **路由优先级**：`/api/*`、`/healthz`、`/login`、`/account` → 后端动态处理；其余 GET → 静态文件。**静态不得覆盖 `/api/*` 前缀**，否则 API 被吞。
3. **SPA fallback**：非 `/api/*`、非静态文件命中的 GET → 回 `index.html`（200，非 404），以支持深链 `/?session=<sid>`。
4. **`/login`、`/account`**：复用看板 `view_auth` 既有口径，前端**不自建登录页**（直接复用你已上的那套）。
5. **缓存头**：
   - `index.html` → `Cache-Control: no-cache`（或 `no-store`），保证发版即生效；
   - `*.js` / `*.css` → 可 `Cache-Control: max-age=300` 或带内容哈希；**v1 阶段**建议统一 `no-cache`，零构建下文件名不变，避免旧缓存。
6. **SSE（第二档）同源**：`/api/stream` 与静态同源 → 免 CORS；注意 `Content-Type: text/event-stream`、`X-Accel-Buffering: no`、禁用压缩缓冲，否则事件被攒批。
7. **MIME**：`.js` 必须 `application/javascript`（ES module 场景更严），`.css` `text/css`，避免旧 Windows 注册表把 `.js` 报成 `text/plain`。
8. **不要**代管前端构建/产物生成 —— 交付物是纯静态文件，我这边改完直接落 `ui/`。

> 若你评估同源托管成本高于预期，退路是「前端独立服务 + 反代 `/api`」，但需额外配 CORS + cookie `SameSite`/Domain —— **我倾向按同源做，不启用退路**。

---

## 3) 议程 3 — 对接说明交付方式：agree

`docs/frontend-integration.md`（只放接口文档：端点、字段、错误码、SSE 事件名、时序图），落在
`<repo>/agent-team/docs/`，我按需取用。**边界照旧：你做后端、我做前端，互不碰对方代码。**

补一句：第二档（POST messages + SSE）上线时，请在该文档**增量补事件名与字段**，并在
`api.js` 已有方法签名上对齐（我已按契约预留 `overview/meeting/turns/participants/observers/refs/stream` 调用）。

---

## 结论汇总

| # | 事项 | 回执 |
|---|---|---|
| 1 | 只读面口径 | **agree**（实测一致，无冲突） |
| 2 | 前端落点 | **同源托管**（需后端配合 8 项，见上） |
| 3 | 对接说明形式 | **agree**（`docs/frontend-integration.md`） |

前端口径**已冻结**，我可据本回执开工，无悬空项。
