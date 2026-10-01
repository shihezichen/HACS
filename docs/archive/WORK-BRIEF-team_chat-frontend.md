# 人机对话台（team_chat）前端 · 工作书

> 存档时间：2026-09-21 10:25（北京时间）
> 存档人：陈刚的OpenClaw（本机 main）
> 用途：跨 session 记忆锚点。**重开 session 后先读本文件**。
> 位置：`<repo>\agent-team\WORK-BRIEF-team_chat-frontend.md`
> 同步：`<home>\.openclaw\workspace\memory\team-chat-frontend.md`

---

## 0. 一句话目标

给「人类（刚哥）与团队 Agent 的网页对话台」做**前端**。硬标准（契约 v1.1 §A.1）：
**布局可以学 Open WebUI，模型不能照搬**；四件诉求必须是**原生表达**，不是外挂拼凑：

1. 多 Agent 多会话（一 Agent N 条会话各占 Tab，可并排双窗格）
2. 多会并存（进行中的会议可同时观看，各自独立圆桌发言流）
3. 实时旁听（只读实时流 + 参会人名单 + 发言权状态）
4. 全局态（在线 Agent 数 / 进行中会议数 / 未读，顶部常驻）

---

## 1. 与 Hermes 的合作关系（责任田）

| 角色 | owner | 负责 |
|---|---|---|
| **前端** | **我（陈刚的OpenClaw / 本机 main）** | `agent-team/ui/` 全部产物、界面形态、SSE 消费、交互 |
| **后端 + 服务落点** | **Hermes 总控** | `chat_svc.py`、PG 表、SSE 推流实现、systemd 部署、会议投影 |

- **契约**：`API契约-人机对话台-v1.md`（v1.0 已冻结）+ `API契约-人机对话台-v1.1-前端增强增补.md`（我 9-20 已回执 → 即冻结）
- **回执**：`API契约-v1.1-OpenClaw回执.md`（我写的，§G 七条全确认）
- **纪律**：契约字段/路径/事件名**不得单方改**，要改走 v1.2 双方确认；前端不得依赖未写入契约的字段。
- **配合纪律（A2A）**：Hermes 长回复必超时 → 一次只问一件事、要短回复、要文档只要路径。见 `TOOLS.md`。

### 后端实现顺序（Hermes 按 §12.4）
① 只读端点（whoami/agents/sessions/messages） ← **当前进度**
② POST messages + SSE
③ POST sessions / PATCH 改名
④ POST meetings + 会议推流
⑤ 旁听

**未实现端点统一返 `501 not_implemented`（不是 404）** → 前端据此开关按钮。

---

## 2. 已完成内容（前端，截至 2026-09-21 10:25）

产物目录：`<repo>\agent-team\ui\`（10 个文件，零构建纯 HTML/CSS/JS）

| 文件 | 职责 | 状态 |
|---|---|---|
| `index.html` | 三栏 + 顶部全局态布局 + 演示横幅 + 建会对话框 | ✅ |
| `styles.css` | 深色主题基础样式 | ✅ |
| `views.css` | 会议/旁听/侧栏/引用/对话框样式 | ✅ |
| `api.js` | 契约 v1/v1.1 **全端点客户端** + SSE `Stream` 类（1 条汇总流·`since_seq` 重连·事件分派·501 识别） | ✅ |
| `mock.js` | 完整演示数据（5 成员/3 会话/会议快照/圆桌流/旁听/引用） | ✅ |
| `meeting.js` | 会议视图（快照+历史+SSE 合并·`version` 判重） | ✅ |
| `observers.js` | 旁听视图（可见性/能力/加入退出） | ✅ |
| `sidebar.js` | 会话侧栏（groups 分组）+ 右栏引用面板 + 建会表单（四要素门禁） | ✅ |
| `app.js` | 主程序（花名册/多Tab/双窗格/未读本地记账/SSE 接线/mock 自动降级） | ✅ |
| `serve.py` | 本地预览代理（开发脚手架，**非交付物**） | ✅ |

### 已跑通并截图验证的视图
- ✅ 顶部全局态（在线 3/5、会议 1、未读、连接态）
- ✅ 左栏 Agent 花名册（presence 五态点：online/busy/queued/degraded/offline + 心跳 + 负载 + 当前活动）
- ✅ 左栏会话侧栏（按 `groups` 分组 + 未读徽标 + 最后发言人/时间/摘要）
- ✅ 中部多 Tab + **双窗格并排**（截图：左 DM 聊天 + 右会议旁听同屏）
- ✅ **会议视图**（独立形态：进度轴/议程 3 态/发言人+队列/圆桌发言流按轮次分组/收敛进度/参会人）
- ✅ **旁听视图**（独立形态：只读横幅/参会人发言权/旁听名单按可见性/能力行）
- ✅ 右栏引用面板（`context.refs` + `message.refs` 真实渲染，5 种 REF_TYPE 图标）
- ✅ 建会表单（§6.1 六条门禁前端本地校验：missing_goal/missing_agenda/agenda_item_missing_expected/missing_meetings/moderator_not_in_members/invalid_deadline）
- ✅ **mock 自动降级**：`?mock=1` 或检测到 501 → 自动进演示模式，界面交互全真、数据用 mock，后端就绪自动换真（**不与后端互相等待**）

### 本地预览（现在还活着）
- `cd <repo>\agent-team\ui && python serve.py --port 8080 --backend http://<host>:9980`
- 访问：`http://127.0.0.1:8080/?mock=1`（强制演示）/ `http://127.0.0.1:8080/`（直连后端）
- 深链：`/?session=<sid>`、`/?session=<sid>&pane=<n>`、`#/session/<sid>`

---

## 3. 待完成目标

### 前端侧（不依赖后端，可继续做）
- [ ] 消息流分页加载（向上翻历史 `before_seq` / 补缺口 `after_seq`）
- [ ] 会话改名内联编辑（`PATCH /api/sessions/{sid}`）
- [ ] 新建 DM 会话的成员选择弹层（现在只"点左栏成员即开"）
- [ ] 会话置顶/Tab 拖拽排序（存 localStorage，契约 §A.5）
- [ ] 布局态持久化完善（栏宽、窗格归属）

### 联调侧（等后端）
- [ ] SSE 端到端：`hello` 首帧快照、`since_seq` 断线续推、事件去重
- [ ] 发消息 `202` + `message`/`message.done`/`message.failed` 全链路
- [ ] 会议推流：`turn.spoke`/`speaker`/`meeting.state`/`convergence`/`meeting.ended` 落库→上屏 ≤2s
- [ ] 旁听：`POST /observers` 加入 → 只读事件 → 写操作收 `403 observer_readonly`
- [ ] 真实建会 → roundtable 起的闭环
- [ ] 兜底轮询 `?since=<version>` → `304` 验证

---

## 4. 其他注意事项（坑与约定）

### 契约硬点（别踩）
1. **响应信封**：`{ok:true,...}` / `{ok:false,error:{code,message,detail},req_id}`；新服务统一**对象**信封（旧 9920/9970 是扁平字符串）。
2. **SSE 用 cookie 不用 Authorization**（`EventSource` 不能带自定义头）；前端**只用相对路径**，不写 host/端口。
3. **全前端只用 1 条汇总流** `/api/stream`（HTTP/1.1 上限 6 连接），多 Tab/窗格按事件体 `session_id` 分发。
4. **事件 `id:` = `seq`**；`hello`/`roster`/`overview` 等快照类事件**不写 id**（重连不重放）。
5. **`presence.state` 由服务端算好下发**（75s/180s 两档，`hb_interval=30s`），前端**不得**自己减时间判断。
6. **`round_no` = 已完成轮次**（真源语义）；进行中轮次用 `current_round`。会议视图按**轮次为主轴**，不当聊天流。
7. **`unread` 默认 `mode:"client"`**：服务端只给 `last_seq`，未读前端用本地已读游标算（存 localStorage）。
8. **`last_seq` 是本地算未读的唯一依据**。
9. **REF_TYPES 冻结**：`entry | file | measurement | session | turn`，不许扩（要扩走 v1.2）。
10. **v1 明确不做**（前端别提前做）：token 级流式（`message.delta` 预留不发）、附件上传、旁听者发言（501）、服务端已读游标、中断运行中 Agent、会员管理 UI。

### 环境/工具坑
- **后端落点**：龙虾机 `<node-ip>`（tailnet `<host>`）:9980，systemd 名 `agent-team-chat`；当前 `phase: read-only`，`db:true`。
- **SSH 别名** `quantum`/`lobster`/`top001`，密钥 `~/.openclaw/.ssh/id_ed25519`。
- **Python 用 `uv`**；本机浏览器自动化用 `browser` 工具。
- 当前会话工具集**没有 `message`/`tts` 等**（policy 过滤），用直接回复即可。
- **别碰** `<tools-dir>\output`（Hermes 的）；本机 `<repo>\agent-team\ui\` 是我的。
- 契约 v1.0 文件**不动**（冻结留痕）；v1.1 增补冲突处以 v1.1 为准。

### 团队记忆同步（NATS KV）
- bucket `TEAM_MEMORY`，工具 `<home>\AppData\Local\hermes\scripts\team_memory.py`（需 `$env:AGENT_NAME="openclaw"`、venv `.venv-nats`）。

---

## 5. 关键路径速查

```
契约 v1.0   <repo>\agent-team\API契约-人机对话台-v1.md
契约 v1.1   <repo>\agent-team\API契约-人机对话台-v1.1-前端增强增补.md
我的回执    <repo>\agent-team\API契约-v1.1-OpenClaw回执.md
后端源码    <repo>\agent-team\chat_svc.py
前端产物    <repo>\agent-team\ui\
本工作书    <repo>\agent-team\WORK-BRIEF-team_chat-frontend.md
```

---

_下一步开工点：消息流分页加载（`before_seq` 向上翻页）+ 会话改名内联编辑（`PATCH`）。_
