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

## 5.5 凭据轮换（`route` 集群口令 · 刀29 实测流程）

真源：**两中枢各一份 `/root/.nats-route.pass`**（32B / 0600），**必须两机同值** ——
生成器只读它，把它写进 `cluster.authorization.password`，并在**对端拨号那条**里内联进
`routes = [ "nats-route://route:<口令>@<对端>:9931" ]`。

```bash
# 1) 备份（两机）：口令文件 + conf 各留 .bak-k<刀号>-<ts>
ssh <hub> 'cp -a /root/.nats-route.pass{,.bak-k29-<ts>} && cp -a /etc/nats/nats.conf{,.bak-k29-<ts>}'
# 2) 生新值并**经管道**写两机（不打印、不落本机文件；保持 32 字节、无换行）
printf '%s' "<新口令>" | ssh <hub> 'umask 077; cat > /root/.nats-route.pass; chmod 600 /root/.nats-route.pass'
# 3) 生成器目录必须**带上依赖模块**：node_profile.py + node_paths.py（只放 gen_nats_conf.py 会 ImportError）
scp agent-team/{gen_nats_conf.py,node_profile.py,node_paths.py} <hub>:/tmp/k29/
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
这串报错同时是「旧口令确实被拒」的**实证**。若重启 1 分钟后仍持续刷认证失败 → 口令未同步，**回滚**。

**回滚**：`cp -a /root/.nats-route.pass.bak-k29-<ts> /root/.nats-route.pass` + `cp -a /etc/nats/nats.conf.bak-k29-<ts> /etc/nats/nats.conf` + 两机重启。

**泄漏清理（口令一旦进过日志/会话记录）**：本机有**两处**要扫 ——
`~/.claude/**/*.jsonl`（Claude 会话记录）与 `~/AppData/Local/hermes/state.db`
（Hermes 会话库的 `messages.content/reasoning/reasoning_content` + `messages_fts`，改完必须
`INSERT INTO messages_fts(messages_fts) VALUES('rebuild')` 再 `PRAGMA integrity_check`，
改前先整库备份）。轮换后旧值即**废值**，替换为 `[REDACTED-ROTATED]` 占位符即可。

**红线**：打印 conf 做取证时，遮蔽规则**必须同时覆盖** `password: "…"` **与内联 URL**
`nats-route://user:<口令>@…` 两种形态 —— 只遮前者会把口令原样打出来（刀29 踩过）。

## 5. 红线

- 退场**只许动被点名的成员**；同一台机的其他成员与凭据文件不碰。
- 停单元**只许停已证"无人用"的**（活连接 0 + 全网零引用）。
- 基线数字随真实收敛下调时，**必须单独 commit 留痕**，不许当"放水"顺手改。
