# L0 轻量节点守护 · 探针口契约（刀 23，阶段 4 第三刀）

> 件：`agent-team/nodelet.py`（L0）· 生成器：`tools/nodelet_gen.py`（T）
> 等价证据：`tools/equivalence/nodelet_equiv.py`
> 本文是**契约**：下面的键名、键序、取值口径改一个字，都要连着等价装置一起改，并在台账里留痕。

## 它回答什么

节点档案 `node_profile.py` 回答「这台机**该**长什么样」；
本守护回答「这台机**现在实际**长什么样」，而且四台机口径一字不差：

- 运行目录在不在、写不写得进去；
- 档案声明的宿主真源（Hermes `state.db` / OpenClaw `sessions.json`）文件真在吗；
- 档案声明要跑的那几个监管单元，本机此刻 active 吗；
- 这台机上跑的这一份件，指纹对不对（「已落盘但没重启」不再是静默态）。

## 端点

只有一条对外路径。

```
GET /probe    → 200 application/json
GET /其它     → 404 {"ok": false, "why": "no such path"}
POST/PUT/...  → 405 {"ok": false, "why": "read only"}
```

绑定默认 `127.0.0.1:9971`，env `AGENT_TEAM_NODELET_BIND` 可覆盖 ——
但 `parse_bind()` **只放行环回与尾网**（`<tailnet-cidr>`），写别的地址直接拒启（退出码 2）。

## 返回体（键名与**顺序**冻结）

```json
{"node": "lobster",
 "profile": {"file": "node_profile.py", "ok": true, "missing": []},
 "facts": {
   "runtime_dir":       {"exists": true, "writable": true},
   "log_dir":           {"exists": true, "writable": true},
   "hermes_state_db":   {"configured": false, "exists": false},
   "openclaw_sessions": {"configured": false, "exists": false},
   "a2a_probe_token":   {"present": true, "len": 43},
   "services": [{"name": "a2a-svc", "known": true, "active": true}]
 },
 "version": "8035c1e7",
 "uptime_s": 12.5,
 "ts": "2026-09-27T04:34:56Z"}
```

顶层六键的顺序由 `nodelet.PROBE_KEYS` 冻结，`facts` 六键由 `nodelet.FACT_KEYS` 冻结，
两处在 `build_probe()` 出口都有 `assert` 把关 —— 键序被人改了，进程当场炸，不会悄悄漂。

| 键 | 口径 |
| --- | --- |
| `node` | 本节点名，取自档案 `node_profile.node_name()`（env `AGENT_TEAM_NODE` > 主机名归一）。档案给不出名字 → 空串，**不猜** |
| `profile.file` | 档案模块的**文件名**（basename），不是路径 |
| `profile.ok` | `missing` 为空即 true |
| `profile.missing` | 按 `nodelet.REQUIRED_FACES` 的**固定顺序**列出「不在 / 调用炸了」的档案面；本机在档案表里查不到时另加固定串 `target_profile` |
| `facts.*.exists` / `writable` | `writable` 用 `os.access(W_OK)` 判，**不试写**（只读铁律优先于判定精度；Windows 目录只读属性不反映在 `os.access` 里，该格偏乐观，是已知代价） |
| `facts.*.configured` | 档案给出了非空路径即 true（≠ 文件存在） |
| `facts.a2a_probe_token` | **凭据面**：只有 `present` / `len` 两个键，见下方红线 |
| `facts.services[]` | `{"name", "known", "active"}`。`known=false` = 这台机不认识这个单元（没登记 / 查询跑不起来）；查不出来一律 `active=false`，**不编造 true** |
| `version` | **磁盘上本件**的 md5 前 8 位，每次探针现算 |
| `uptime_s` | 秒，三位小数。**易变键** |
| `ts` | UTC，`%Y-%m-%dT%H:%M:%SZ`。**易变键** |

> 拿探针体做逐字节比对（等价装置、跨机 diff）之前，`uptime_s` 与 `ts` **必须先归一**。
> `tools/equivalence/nodelet_equiv.py` 里的 `normalize()` 是参考实现，且它会断言归一真的生效了。

## 三条红线

1. **只读**。不写文件、不建目录、不启停任何单元。查单元状态只跑 `systemctl is-active` /
   `schtasks /Query`，都是查询命令。
2. **不越层**。只 import 同层 L0 `node_profile`。HTTP 面用纯标准库 `http.server` 自己搭 ——
   L1 的 `web_gateway` 对 L0 是上层，不许 import。这是本件作为 L0 的代价，不是疏漏。
3. **不泄密**。绝不输出凭据值，**也绝不输出路径全文**（探针口能被读到的地方比你想的多）。
   凡名字含 `token` / `secret` / 口令 / `credential` / `key` 词根的面，唯一合法形态是
   `{"present": bool, "len": int}`；出口前由 `enforce_credential_redline()` 机械复核，
   违反直接抛 `AssertionError` —— 宁可探针口炸掉，也不许把一个凭据值悄悄送出门。

## 谁可以读

- **本机**：`curl http://127.0.0.1:9971/probe`，或不起服务直接 `python nodelet.py --once`。
- **部署巡检**：`tools/deploy_targets.json` 里每个节点的 `nodelet.py` 条目带一条 health，
  `tools/deploy.py --health-only` 会连着跑。远端机只绑环回，故该条 health 写 `via: "ssh"`，
  在机内 `curl`。
- **协作域内的别的节点**：默认够不着（只绑环回）。真要开，把 `AGENT_TEAM_NODELET_BIND`
  改成本机 100.x 地址 —— `parse_bind()` 放行尾网，但**只放行尾网**。

## 单元名：各机统一

| flavour | 监管方式 | 单元名 |
| --- | --- | --- |
| posix | systemd | `agent-team-nodelet` |
| nt | 计划任务 | `AgentTeamNodelet` |

唯一出处 = `nodelet.UNIT_NAME_POSIX` / `UNIT_NAME_NT`。
单元内容**不许手抄**：模板只有一份，在 `tools/nodelet_gen.py` 里，差异（运行目录 / python /
该机声明的单元清单）由**部署表 + L0 档案**推导。

```
python tools/nodelet_gen.py --gen            # 打印四台机的单元（只打印，不落盘）
python tools/nodelet_gen.py --check          # 核对各机：规范单元在不在、有没有异名同类单元
```

存量单元（`a2a-svc` / `AgentTeamA2ASvc` / `agent-team-a2a` 这类历史异名）**刀 23 一个不动** ——
本刀只保证**新增**的这一个从第一天起就只有一个名字。

## 配置从哪来（为什么守护自己不读部署表）

`tools/deploy_targets.json` 是 T 工具面的**内部真值表**（含内网地址与密钥路径，禁止入公开仓），
它不随件下发到节点。所以链条是：

```
部署表 + L0 档案  →  nodelet_gen 生成单元（Environment= / --services）  →  守护读 env
```

守护的输入通道**只有 env** 一条。`--node` / `--bind` / `--services` 三个开关只是把值写回
本进程 env 后照常走 —— 它们存在是因为 Windows 计划任务的 `/TR` 有 261 字符上限、
靠 `cmd /c set X=Y&&…` 串 env 太脆。
