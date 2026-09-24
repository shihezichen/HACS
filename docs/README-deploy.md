# a2a_svc.py 部署说明

A2A 平面统一端口 **9920/TCP**，每个成员节点各自跑一份 `a2a_svc.py`，
只绑 tailscale0 的 100.x 地址。替代已退役的 `a2a_bridge.py`（9910）。

依赖：**只需要 Python 3.9+ 标准库**，无任何 pip 包。

---

## 一、各节点绑定地址

- **lobster** → `--host <host>`
- **top001** → `--host <node-ip>`
- **thinkbook（笔记本）** → `--host <node-ip>`

启动命令统一形态：

```bash
python3 <deploy-dir>/a2a_svc.py \
  --host <host> \
  --port 9920 \
  --config <deploy-dir>/agents.json \
  --peers  <deploy-dir>/peers.json
```

**绝不要传 `--host 0.0.0.0`。** 不传 `--host` 时默认 `127.0.0.1`（安全默认，只能本机访问）。

可选参数：

- `--advertise-host` / `--advertise-port`：只影响 Agent Card 里 `url` 字段对外公布的地址。
  默认取 `agents.json` 里的 `host`/`port`，缺失时回落到实际监听地址。
- 环境变量 `A2A_ALLOW_ANON=1`：跳过鉴权，**仅限本地调试**，生产严禁开启。

---

## 二、配置文件

### `agents.json`（每个节点只放本机承载的 agent）

```json
{
  "node_id": "lobster",
  "host": "lobster",
  "port": 9920,
  "agents": {
    "lobster": {
      "backend": "http://127.0.0.1:19805/v1",
      "model": "openclaw/default",
      "api_key": "",
      "description": "龙虾助手",
      "skills": ["openclaw"]
    }
  }
}
```

分配关系：

- lobster 节点 → 只放 `lobster`
- top001 节点 → 只放 `top001`
- thinkbook 节点 → 放 `hermes` / `openclaw-nb` / `codex` 三条

请求打到本节点**未配置**的 agent_id → `404 {"ok": false, "error": "unknown_agent"}`。

### `peers.json`（鉴权表，只存 sha256，不存明文 token）

```json
{
  "hermes":  "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "lobster": "…64位十六进制…",
  "top001":  "…64位十六进制…"
}
```

生成 token 和哈希：

```bash
# Linux / macOS
TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
echo "token(发给该成员，只发一次): $TOKEN"
python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$TOKEN"
```

```powershell
# Windows PowerShell
$py = "<home>\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe"
& $py -c "import secrets,hashlib;t=secrets.token_urlsafe(32);print('token:',t);print('sha256:',hashlib.sha256(t.encode()).hexdigest())"
```

**每成员一枚 token**，明文只保存在调用方；服务端 `peers.json` 里只有 sha256。
文件权限建议 `chmod 600 <deploy-dir>/peers.json`。

调用方带头：`Authorization: Bearer <明文token>`。

---

## 三、Linux（lobster / top001）：systemd 单元模板

`/etc/systemd/system/a2a-svc.service`：

```ini
[Unit]
Description=A2A service endpoint (port 9920)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=<deploy-dir>
ExecStart=/usr/bin/python3 <deploy-dir>/a2a_svc.py \
  --host <host> \
  --port 9920 \
  --config <deploy-dir>/agents.json \
  --peers <deploy-dir>/peers.json
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

> `--host` 换成本机的 100.x 地址；tailscale 起得慢时靠 `Restart=always` 重试绑定。

```bash
systemctl daemon-reload
systemctl enable --now a2a-svc
systemctl status a2a-svc
journalctl -u a2a-svc -f      # 日志一行一请求
```

---

## 四、Windows（thinkbook）：计划任务启动

用 PowerShell 注册一个开机自启的计划任务：

```powershell
$py  = "<home>\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe"
$svc = "<repo>\agent-team\a2a_svc.py"
$cfg = "<repo>\agent-team\agents.json"
$prs = "<repo>\agent-team\peers.json"

$action  = New-ScheduledTaskAction -Execute $py `
  -Argument "`"$svc`" --host <node-ip> --port 9920 --config `"$cfg`" --peers `"$prs`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "a2a-svc" -Action $action -Trigger $trigger `
  -Settings $set -RunLevel Highest -User "SYSTEM"

Start-ScheduledTask -TaskName "a2a-svc"
Get-ScheduledTask   -TaskName "a2a-svc" | Get-ScheduledTaskInfo
```

前台手动跑（调试用）：

```powershell
& $py $svc --host <node-ip> --port 9920 --config $cfg --peers $prs
```

Windows 防火墙放行（只放行 tailscale 网段）：

```powershell
New-NetFirewallRule -DisplayName "a2a-svc 9920" -Direction Inbound `
  -Protocol TCP -LocalPort 9920 -RemoteAddress <tailnet-cidr> -Action Allow
```

---

## 五、冒烟验证

```bash
# 1. 健康检查（不需要鉴权）
curl -s http://<host>:9920/healthz
# {"ok":true,"node":"lobster","agents":["lobster"],"ts":1789800000}

# 2. Agent Card（不需要鉴权）
curl -s http://<host>:9920/a2a/lobster/.well-known/agent-card.json

# 3. 成员清单（需要鉴权）
curl -s -H "Authorization: Bearer $TOKEN" http://<host>:9920/agents

# 4. 发消息
curl -s -X POST http://<host>:9920/a2a/lobster/messages \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"报一下你的状态","from":"hermes","timeout_s":60}'
```

状态码对照：`401` 鉴权失败 / `404` agent 不存在 / `400` 请求体不合法或 text 为空 /
`504` backend 超时 / `502` backend 报错或连不上（`detail` 里是原始响应前 200 字）。

---

## 六、排错

**`ss -lntp | grep 9920` 看不到监听**
→ 绑错地址了。确认 `--host` 是本机 tailscale0 的 100.x 地址：`ip -4 addr show tailscale0`。
tailscaled 还没起来时绑定会失败，看 `journalctl -u a2a-svc` 里的 `Cannot assign requested address`。

**`ss` 显示 `127.0.0.1:9920`**
→ 忘了传 `--host`，走了默认值，外部访问不到。

**`curl: (7) Connection refused` / 连接被拒**
→ 进程没起（先 `systemctl status a2a-svc`），或者防火墙拦了：

```bash
firewall-cmd --permanent --add-port=9920/tcp
firewall-cmd --reload
firewall-cmd --list-ports
# 更严格的做法：只放行 tailscale 网段
firewall-cmd --permanent --zone=trusted --add-interface=tailscale0
firewall-cmd --reload
```

Windows 侧见上面的 `New-NetFirewallRule`。

**一直 401**
→ 三种可能：`peers.json` 里存的不是 sha256 而是明文；请求头写成了
`Bearer sha256:xxx`（**不支持前缀**，就传明文 token）；`peers.json` 没加载成功
（启动日志会打 `peers 表为空` 的警告）。

**总是 404 unknown_agent**
→ 该 agent 没配在**这个节点**的 `agents.json` 里。用 `/agents` 看本节点实际承载了哪些。

**502 backend_error**
→ 本机后端没起或路径不对。`detail` 里是后端原始响应前 200 字，直接 curl
`<backend>/chat/completions` 复现；若响应里没有 `choices[0].message.content`，
服务端不会编造回复，一律 502。

**504 backend_timeout**
→ 后端太慢，调大请求体里的 `timeout_s`（默认 120 秒）。
