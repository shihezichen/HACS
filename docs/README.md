# docs/ — 架构与说明

## 一张图看全貌

```mermaid
flowchart TB
  subgraph H["人（刚哥）"]
    UI["浏览器 / 手机<br/>example.com 反代"]
  end

  subgraph A["Agent 侧"]
    HM["Hermes<br/>（后端 · 总控）"]
    OC["OpenClaw-nb<br/>（前端 ui/）"]
    CL["Claude / 其它 Agent"]
  end

  subgraph S["对话台（backend/）"]
    CS["chat_svc.py :9980<br/>群聊 · @提及 · reply_to · /members"]
    PG[("PG<br/>chat_sessions / chat_messages")]
  end

  subgraph C["通信层（comm/）"]
    NATS[("NATS<br/>拓扑 · KV · 事件")]
    WD["守望器 watcher<br/>扫未回应账"]
    A2A["A2A 桥"]
  end

  subgraph D["部署面（deploy/）"]
    L["龙虾"]
    Q["量子"]
    T["Top001"]
  end

  UI -->|HTTP+SSE| CS
  CS --- PG
  CS <--> NATS
  NATS <--> WD
  NATS <--> A2A
  A2A <--> HM
  A2A <--> OC
  A2A <--> CL
  WD -->|@点名投递| HM & OC & CL
  CS --- L
  NATS --- Q
  A2A --- T
```

## 数据流（一句话版）

1. 人在对话台发言 → `chat_svc` 落 PG（`chat_sessions` / `chat_messages`，会话内单调 `seq`）。
2. 正文里的 `@成员` 被解析成 `mentions`，写进结构化字段，同时落一条「待回应」账。
3. 守望器从 PG/NATS 扫「待回应」账，按 `@` 目标把消息投给对应 Agent。
4. Agent 回话 → 走同一 `chat_svc` 写回；「待回应」账清账，前端标签翻成「已回应」。
5. 前端只走同源 `/api/...`，响应体里不出现绝对地址，所以本机直连与 `example.com` 反代共用一套前端。

## 目录说明

见仓根 `README.md` 的目录表。三个放代码的目录（`backend/` `comm/` `deploy/`）各自带一份 `README.md`，写明「本目录放什么、来自哪台机器、入库门禁是什么」。

## 入库门禁

- `tools/scan_secrets.py`：正则 + 常见凭证指纹，作为提交前的硬门禁。
- 配置零明文：`.env.example` 是唯一被接受的样板，其余一律 `${ENV}`。
- 逐步入：动着的目录等交付冻结后单开一次提交，不固化中间态。
