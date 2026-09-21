# comm/ — 团队通信层

## 本目录放什么

| 文件 | 说明 | 来源 |
|---|---|---|
| `nats/` | NATS 拓扑与配置模板（账号、用户、路由） | 龙虾 `.hermes-comm/` + 各机 nats 配置 |
| `watcher/` | 守望器：扫「待回应」账、按 `@` 投递、链式上限与补投 | Hermes 本机 |
| `a2a/` | A2A 桥（Agent→Agent 直投） | 龙虾 `<deploy-dir>/a2a_svc.py` |
| `daemon/` | 各 Agent 的 daemon 与启动件（含计划任务体） | 各 Agent 本机 |

## 硬规矩

1. **NATS 证书 / 密码文件零入库**。配置模板里用户名可以留，`*.pass` 与证书一律进 `.gitignore`，改 `${NATS_PASS_FILE}` 指向运行时路径。
2. **拓扑图可以进仓，真实 IP 不行**。`docs/` 里画拓扑用占位（`<tailnet-ip>`），不写真值。
3. 每个 Agent 的 daemon / 启动件归各自 owner，入库前由 owner 自查一遍再交。

## 门禁回显

```bash
python tools/scan_secrets.py comm/
```
