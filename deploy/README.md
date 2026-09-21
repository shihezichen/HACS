# deploy/ — 三台机器的部署件

覆盖 **量子 / 龙虾 / Top001** 三台：要装什么、怎么起、怎么开机自启、怎么升级回滚。

| 子目录 | 目标机 | 放什么 |
|---|---|---|
| `quantum/` | 量子 | Agent 运行时 + NATS 客户端 + daemon + 计划任务 |
| `lobster/` | 龙虾 | 对话台 `chat_svc` + A2A 桥 + 团队管理 + 计划任务 |
| `top001/` | Top001 | 后台服务 + 反代（`example.com` 入口） |

## 硬规矩

1. **部署脚本里不得出现明文口令**：一律 `os.environ` / `${ENV}`，脚本只做「读环境变量 → 落地配置 → 起服务」。
2. **SSH 私钥不入库**：脚本里的密钥只写**路径**（`${DEPLOY_SSH_KEY}`），内容零入库。
3. **阿里云 AK / 飞书 secret / Top001 口令**：只从 `.env` 读，`.env` 进 `.gitignore`。
4. 每台机的启动件（计划任务 / systemd / 启动壳）随该机的目录一起交，写明「谁是 owner」。

## 门禁回显

```bash
python tools/scan_secrets.py deploy/
```
