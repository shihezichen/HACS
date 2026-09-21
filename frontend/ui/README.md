# frontend/ui/ — 聊天前端渲染层（占位）

**本次骨架不含 `ui/` 代码。** 这里只放一枚占位说明，理由是：该目录归 OpenClaw 产出，此刻还在动，固化进历史等于把中间态钉死。

## 入库流程（三方口径，已冻结）

1. OpenClaw 交付 v0.1，贴 `ui/` 清单 + 时间戳到群里。
2. Hermes 用 `tools/scan_secrets.py` 扫描，**一视同仁**：硬编码后端地址、鉴权 cookie、tailnet 信息，一律打回。
3. 扫过之后，**单开一次全量提交**入 `frontend/ui/`。本次骨架不插手。

## OpenClaw 自查项（交付前）

- `ui/` 内不留硬编码后端地址（走同源相对路径 `/api/...`）。
- 不留鉴权 cookie / token / 任何凭据。
- 不留 tailnet IP 与主机名。
- 第三方依赖（Markdown 解析、KaTeX）引进前把**包名 + 版本**贴群，过供应链关。
