# 成员入网「退场 / 收敛」运维契约（阶段5 第二刀 · 刀26）

> 面向：谁有权改中枢成员、怎么改、改完拿什么证、坏了怎么退回去。
> 前置契约：`l5-membership-audit.md`（只读对账器 M1–M6 是**判据**，本文是**动作**）。

## 1. 两条铁律

1. **不准手改 `/etc/nats/nats.conf`** —— 该文件首行自带此提醒。成员增删一律走生成器
   `gen_nats_conf.py`，它自带 `.bak-<时间戳>` 备份。
2. **退场前必须先有对账证据**：`python tools/nats_membership_audit.py --live` 里
   M4（僵尸凭据 = 名册里有、但全网既无凭据文件也无活连接）或 M6a（非 v2 形状的遗留中枢）
   点名到它，才准动。**没有证据的成员，一律视为在用，禁止退场。**

## 2. 成员退场（`--drop-user`）

```bash
# 0) 只读取证（在哪台机、是不是僵尸）
python tools/nats_membership_audit.py --live            # 看 M4 点名
# 1) 把生成器送上中枢机（它不在部署表里：是"在机器本机跑"的离线生成器）
ssh <hub> 'mkdir -p /tmp/k26' && ssh <hub> 'cat > /tmp/k26/gen_nats_conf.py' < agent-team/gen_nats_conf.py
# 2) 先 dry-run，逐行看它到底要改什么（dry-run 不写盘、不备份）
ssh <hub> 'cd /tmp/k26 && PYTHONPATH=<deploy-dir> python3 gen_nats_conf.py <server_name> <self_ip> <peer_ip> --drop-user <成员名> --dry-run'
# 3) dry-run 与线上 conf 的差异**只允许**出现「被退场用户那一行 + 生成时间戳」；否则停手
# 4) 真跑（自动备份 .bak-<时间戳>）后重启中枢生效
ssh <hub> 'cd /tmp/k26 && PYTHONPATH=<deploy-dir> python3 gen_nats_conf.py ... --drop-user <成员名> && systemctl restart agent-team-nats'
# 5) 复核：M4 该成员点数归零；其余成员口令/权限块一字未动（用 dry-run 稿逐行比对）
```

**为什么必须走生成器**：它是**从旧 conf 原样继承用户**的（不认识"凭据文件"这回事），
所以只会照抄出僵尸条目。`--drop-user` 是给它的**唯一**合法删人通道；副作用为零 ——
不带该开关时产物与旧版**逐字节相同**（由 `tools/equivalence/gen_nats_conf_equiv.py` 的
56 格矩阵证明，含负控）。

位置参数老契约不变：`<server_name> <self_ip> <peer_ip> [--drop-user <名>]... [--dry-run]`；
未知开关一律拒绝（不做静默忽略）。

## 3. 遗留中枢退役（M6a）

判据：conf 里**没有 `permissions` 块**（= 全权）或**监听端口不属于 v2 端口体系**
（9930 客户端 / 9931 集群 / 9932 监控）。

```bash
ssh <node> 'systemctl is-enabled <unit>'                     # 现状
ssh <node> 'ss -tn | grep -c ":<port> "'                     # 活连接数（必须为 0）
grep -rln "<port>" <deploy-dir> <deploy-dir>           # 全网引用（必须为空）
ssh <node> 'cp -a /etc/systemd/system/<unit>.service /etc/systemd/system/<unit>.service.bak-k26'
ssh <node> 'systemctl disable --now <unit>'
```

**数据不删**：JetStream 目录等原样留着（退役 ≠ 销毁）。回滚 = `systemctl enable --now <unit>`。

## 4. 集群路由条目（M6b）

判据：任一中枢 `num_routes > 2` 或存在 `idle > 3600s` 的条目。
**已知固有项（不消红）**：nats-server 2.14.5 在尾网上开机 0.25 秒内会对**同一条** routes
条目并发建立多条连接（实测 4 条、全 idle、无 duplicate 告警），重启可复现 → 属上游竞态，
配置侧无解，**如实记账**。下一步候选：网关(gateway)替代 routes / 升 nats 版本。

## 5. 红线

- 退场**只许动被点名的成员**；同一台机的其他成员与凭据文件不碰。
- 停单元**只许停已证"无人用"的**（活连接 0 + 全网零引用）。
- 基线数字随真实收敛下调时，**必须单独 commit 留痕**，不许当"放水"顺手改。

## 5.5 凭据轮换（`route` 集群口令 · 刀29 实测流程）

真源：**两中枢各一份 `/root/.nats-route.pass`**（32B / 0600），**必须两机同值** ——
生成器只读它，把它写进 `cluster.authorization.password`，并在**对端拨号那条**里内联进
`routes = [ "nats-route://route:<口令>@<对端>:9931" ]`。

```bash
# 1) 备份（两机）：口令文件 + conf 各留 .bak-k<刀号>-<ts>
ssh <hub> 'cp -a /root/.nats-route.pass{,.bak-k29-<ts>} && cp -a /etc/nats/nats.conf{,.bak-k29-<ts>}'
# 2) 生新值并**经管道**写两机（不打印、不落本机文件；保持 32 字节、无换行）
printf '%s' "<新口令>" | ssh <hub> 'umask 077; cat > /root/.nats-route.pass; chmod 600 /root/.nats-route.pass'
# 3) 生成器目录必须**带上依赖模块**：node_profile.py + node_paths.py + member_roster.py（刀30 起，少一个都 ImportError）
scp agent-team/{gen_nats_conf.py,node_profile.py,node_paths.py,member_roster.py} <hub>:/tmp/k29/
# 4) 先 dry-run 对差：遮掉口令（含内联 URL 形态）后与现网 conf 逐行比，**只许差时间戳行**
ssh <hub> 'cd /tmp/k29 && python3 gen_nats_conf.py <server_name> <self_ip> <peer_ip> --dry-run'
# 5) 真跑 → 依次重启（不并发）→ 复查
ssh <hub> 'cd /tmp/k29 && python3 gen_nats_conf.py <server_name> <self_ip> <peer_ip>'
ssh <hub> 'systemctl restart agent-team-nats'
ssh <hub> 'curl -s http://127.0.0.1:9932/routez'
```

**验收判据**：① 两机 `route.pass` sha256 相同且 ≠ 旧值；② dry-run 遮罩后与现网**逐行 0 差异**
（含用户块、权限块原样）；③ 新 conf「含新口令、不含旧口令」，与改前备份遮罩逐行 0 差异；
④ 重启后两机 `num_routes ≥ 1`、对端名正确、**1 分钟后无新的认证失败**。

**预期现象（不是故障）**：先重启的一台会立刻对**尚未重启**的对端报
`authentication error - User "route"`（对端还是旧口令内存态），持续约 20 秒，对端重启即停。

## 6. 凭据镜像通道（成员口令 · 刀30 新增）

### 6.1 它解决什么

刀27 已机械证明「**L0 名册档案 + 各机凭据真值 ⇒ 中枢用户区**」成立，但生成器是**在中枢机上跑的**，
而 8 位成员的凭据散在 4 台机（thinkbook 5 / lobster / top001 / quantum 各 1）——
中枢拿不到全员口令，所以刀30 之前生成器只能从**旧 conf 里正则继承**口令（配置自证配置：
中枢上手改一行，仓里没有痕迹，对账器也看不见）。

镜像通道就是那条缺口上的桥：

```
tools/roster_creds_mirror.py --apply
  → 两中枢各落一份  <conf_dir>/creds/<成员>.pass   （目录 0700 / 文件 0600 / owner=root）
agent-team/gen_nats_conf.py
  → 成员集合与顺序取自 member_roster.ROSTER，口令取自上面那套镜像（**不再继承旧 conf**）
```

**方向是单向的**：节点 → 中枢。镜像是**派生物**不是真源 —— 删了随时可从各机真值重建。

### 6.2 代价（明写，不藏）

两台中枢从此**持有全员口令的明文副本**。这是 2026-09-27 用户明确批准的取舍：
换来「成员集合可被档案单向决定」。缓解手段就四条，缺一不可：

- 目录 0700 / 文件 0600 / owner=root（只有 root 读得到）；
- **不入 git、不进日志**：工具的出口只有 `成员名 + sha256[:12] + mode`，
  写入时口令走 **ssh stdin**（不进 argv → 远端 `ps` 看不到；不落本机临时文件）；
- 随时可 `--rollback` 删除（见 6.4）；
- 中枢一旦失陷，处置等级按「**全员口令泄露**」办：走 5.5 的轮换流程逐个换，不是只换 route。

### 6.3 日常操作

```bash
# 只读盘点（默认动作；逐中枢逐成员报 存在?/sha256[:12]/mode）
python tools/roster_creds_mirror.py --check
# 建/刷新镜像（幂等；写完自跑一次 --check，用它的结论定退出码）
python tools/roster_creds_mirror.py --apply
```

退出码：`0` 全在位且与源侧一致 · `1` 有缺失/漂移/mode 或 owner 不符 · `2` 采集失败
（ssh 不通 / 算不出路径 / 名册为空 / 探针输出读不懂）。**采集失败绝不退化成 0 或 1。**

**顺序铁律**：动生成器之前**先** `--check` 到 rc=0。镜像缺任一份，生成器会
**rc=2 点名该成员并拒绝改写** —— 它**不会**回落旧 conf 继承（静默回落等于把本刀的意义抹掉）。

改名册（加人/减人）时的完整链条：

```
改 agent-team/member_roster.py :: ROSTER
  → roster_creds_mirror.py --apply          （把新人的凭据镜像上去）
  → roster_creds_mirror.py --check  rc=0
  → gen_nats_conf.py --dry-run  遮罩归一后与现网 conf 对差（只许差预期那几行）
  → 真跑 → 依次重启两中枢（不并发）
  → nats_membership_audit.py --live / --check 复核
```

### 6.4 回滚

```bash
# 唯一的回滚动作：删两中枢的 creds/（镜像可重建，删它不丢真值）
python tools/roster_creds_mirror.py --rollback
```

完整回滚（连生成器一起退回刀30 之前）：

1. `python tools/roster_creds_mirror.py --rollback`；
2. `git revert` 生成器那笔改动；
3. 用 `nats.conf.bak-<ts>` 覆盖回去，**依次**重启两中枢。

回滚件一律**按 sha 与 `git rev-parse HEAD:<件>` 比对**认定，**不许按时间戳取最新**。

### 6.5 红线

- 镜像工具**不碰 `nats.conf`**（那是生成器的活）、**不重启任何单元**。
- stdout / stderr / `--json` 里**只许**出现 `成员名 + sha256[:12] + mode`；见到明文即事故。
- 远端**只发只读命令**（`stat` / `sha256sum` / `cut`）盘点；写入只有 `umask 077; mkdir -p; cat >` 一条形态。
- 镜像**只是口令的搬运**：加人减人一律改 `member_roster.py`，**不许**靠手塞一份 `.pass` 进 `creds/` 让人"上线"。
这串报错同时是「旧口令确实被拒」的**实证**。若重启 1 分钟后仍持续刷认证失败 → 口令未同步，**回滚**。

**回滚**：`cp -a /root/.nats-route.pass.bak-k29-<ts> /root/.nats-route.pass` + `cp -a /etc/nats/nats.conf.bak-k29-<ts> /etc/nats/nats.conf` + 两机重启。

**泄漏清理（口令一旦进过日志/会话记录）**：本机有**两处**要扫 ——
`~/.claude/**/*.jsonl`（Claude 会话记录）与 `~/AppData/Local/hermes/state.db`
（Hermes 会话库的 `messages.content/reasoning/reasoning_content` + `messages_fts`，改完必须
`INSERT INTO messages_fts(messages_fts) VALUES('rebuild')` 再 `PRAGMA integrity_check`，
改前先整库备份）。轮换后旧值即**废值**，替换为 `[REDACTED-ROTATED]` 占位符即可。

**红线**：打印 conf 做取证时，遮蔽规则**必须同时覆盖** `password: "…"` **与内联 URL**
`nats-route://user:<口令>@…` 两种形态 —— 只遮前者会把口令原样打出来（刀29 踩过）。
