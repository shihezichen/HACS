# 成员名册档案 + 「档案 + 凭据探针 ⇒ 用户区」自证契约（阶段5 第三刀 · 刀27）

> 面向：名册的真源在哪、条目长什么样、凭什么说「档案 + 凭据」够决定现网配置、下一刀卡在哪。
> 前置契约：`l5-membership-audit.md`（刀25 只读对账器 M1–M6 是**判据**）、
> `l5-nats-ops.md`（刀26 成员退场 / 遗留收敛是**动作**）。
> 本刀**不动 NATS、不改任何运行期件、不部署、不 commit**。

---

## 0. 这一刀在阶段5 里的位置

规划原文：「阶段5 = 入网动态化 …… 目标＝把『成员入网』从人工改 NATS 配置变成**按档案 + 探针
自动生成**；顺序不可颠倒 —— **先量 → 再冻结 → 最后才生成**。收口刀：期望矩阵改由『档案 + 探针』
推导，生成器写配置。」

- 刀25 = **量**（只读对账 + 基线冻结）
- 刀26 = **收**（消 M4 僵尸凭据 + 退役 M6 遗留中枢）
- **刀27 = 收口刀的第一步**：把名册从「中枢配置文件的规范形」提升为**独立 L0 档案**，
  并**机械证明**这份档案 + 各机凭据文件（探针）**足以唯一决定**现网中枢的用户区。

证不成立，后面两刀就不该动手；证成立了，切换才是「已知等价的替换」而不是「换个写法试试」。

---

## 1. 名册真源（本刀定义，别再翻案）

**名册 = L0 档案件 `agent-team/member_roster.py`。**

刀25 把名册真源定义为「两台 v2 中枢 `nats.conf` 里 `authorization.users` 的规范形」，
并写明「名册提升为独立档案件是阶段5 后续刀的事」。那句话的落点就是本件 —— 从本刀起，
**`member_roster.py` 是名册的唯一真源**，`nats.conf` 降级为它的**产物**。

### 1.1 条目形态（冻结）

有序表，**顺序 == 中枢 conf 里 users 的规范序（字典序）** —— 这是逐字节对照能成立的前提。

每条 = `(member, role, cred_node, cred_kind)` 四元组：

- `member` —— 成员名。
- `role` —— 角色名。**本刀只有 `"member"` 一个值**，为将来多角色预留。
- `cred_node` ∈ `{"thinkbook", "lobster", "quantum", "top001"}` —— 口令真值落在**哪台机**
  （节点名，不是 IP → 可入仓）。
- `cred_kind` ∈ `{"runtime", "conf"}` —— 落在该机的**哪一类位置**：
  - `runtime` → `<runtime_dir>/nats-<member>.pass`
  - `conf` → `<conf_dir>/<member>.pass`

路径由 L0 `node_profile`（`runtime_dir()` / `nats_conf_dir_posix()`）与 `tools/deploy_targets.json`
算出 —— **档案里一个路径字面量、一个 IP 字面量都没有**。

### 1.2 三条铁律

1. **口令绝不进档案**。档案只记「口令的真值在哪台机的哪个文件」（= 探针面）。
2. **权限串绝不进档案**。角色模板的唯一出处仍是 L1 `gen_nats_conf.perm_list(user)`；
   档案只记角色**名**。因此档案件**不许 import `gen_nats_conf`** —— L0 → L1 是分层门禁的硬违规。
3. **档案是纯数据 + 纯查询**：零 import、零 IO、零环境依赖。

### 1.3 导出 API（名字冻结）

`SCHEMA` · `ROSTER` · `members()` · `member_names()`（= `members()` 的别名）·
`entry(name)` · `role_of(name)` · `cred_spec(name) -> (node, kind)`。
查不到一律返回 `None` —— **不猜**。

### 1.4 本刀的名册内容（2026-09-27 真机实测）

| member | role | cred_node | cred_kind |
| --- | --- | --- | --- |
| claudecode | member | thinkbook | runtime |
| codex | member | thinkbook | runtime |
| hermes | member | thinkbook | runtime |
| lobster | member | lobster | conf |
| openclaw-nb | member | thinkbook | runtime |
| piagent | member | thinkbook | runtime |
| quantum | member | quantum | conf |
| top001 | member | top001 | conf |

`quantum` 已无 `/etc/nats/nats.conf`（刀26 退役改名），但 `quantum.pass` **仍在**，
是 `quantum` 成员的合法凭据 —— 「那台机不再是中枢」与「那台机上住着一个成员」是两件事。

---

## 2. 唯一判据：「档案 + 探针 ⇒ 用户区」逐字节自证

```
推导侧 = 档案的有序成员表  +  各成员凭据文件的内容（探针读）
        → 生成「用户区」文本（行格式与 gen_nats_conf.py 逐字符同形）
现网侧 = 两中枢 <conf_dir>/nats.conf 里的「用户区」区块（原样抽出）
判据   = 两侧归一化后 逐字节相等（两侧都必须 rc=0、差异 0 行）
```

### 2.1 「用户区」的定义（冻结）

从**含 `users = [` 的那个 `authorization {` 行**起，到与之配对的 `}` 行止（**含首尾两行**）。
抽出时**原样保留** —— 不重排、不 strip 行内空白。

`cluster { … authorization { user: "route" … } }` 那一行里没有 `users = [`，
所以**集群路由凭据天然落在区外**，一个字节都不会进入对照面。

### 2.2 期望用户区的行格式（与 `gen_nats_conf.py:188-196` 逐字符同形）

```
authorization {
  timeout: 3
  users = [
    { user: "<m>", password: "<pw>", permissions: { <perm_list(m)> } }<,>
  ]
}
```

末条**无逗号**，其余带逗号；用户行缩进 4 空格；`password:` 后**一个空格**。

### 2.3 归一化（**只许两处**，其余一个字都不许归一）

1. `password: "<明文>"` → `password: "sha256:<前 12 位十六进制>"`
2. `# 由 gen_nats_conf.py 生成于 <时间戳>（改配置请改脚本再重跑，别手改本文件）`
   → `# 由 gen_nats_conf.py 生成于 <ts>`（时间戳是天然漂移）

**如实记一笔**：第 2 条在「用户区」上**永远不会命中** —— 那行时间戳在文件第 4 行，不在用户区内。
它仍然写进归一化器并冻结在契约里，理由是归一化器是**对文本**的、不是对某个区块的：
将来若把对照面扩到整份 conf，口径不必再议一次。

正因为它在用户区上永不命中，**只靠夹具它就是零覆盖**（实现了但从没被验过）。
装置 B 段因此单列 **7 格归一化交叉核对**直接对文本比两侧的 `normalize()`，把这个缺口补上 ——
其中「形似时间戳但不是」那一格（缺后缀那句提醒）**不被归一**，确认这条规则是紧的、不会误伤。

### 2.4 口令纪律

明文**只存在于进程内存**。stdout / `--json` / `--out` 文件 / 报告 / 夹具里**一律不得出现明文**，
出口只允许 `sha256:<前12>`。`analyze()` 在把任何文本放进报告之前先过归一化器 ——
所以报告里**不可能**出现明文。异常信息与断言消息同样不许带明文。

夹具口令一律 **2 字符合成串**（`p1` / `p2` …），夹具主机名一律 `*.example.invalid`。

---

## 3. 工具（`tools/roster_derive_conf.py`，层 T）

```bash
python tools/roster_derive_conf.py --live [--json] [--out FILE]
python tools/roster_derive_conf.py --fixtures <DIR> [--json]
```

| 退出码 | 含义 |
| --- | --- |
| `0` | 两个中枢的用户区与推导侧**逐字节一致**（归一化后） |
| `1` | **有差异**（逐 hub 打印差异行数与逐行差异） |
| `2` | **采集失败**（ssh 不通 / conf 读不到 / 凭据文件缺失或读不到 / 夹具缺件） |

采集失败**绝不退化成 0 或 1** —— 「没量到」和「量到了、一致 / 不一致」是三件事。

`--json` 顶层键（冻结）：`schema`（`"l5-roster-archive/1"`）· `hub_list` · `roster` ·
`per_hub` · `collect_errors` · `ok`。

`per_hub` 每条：`hub` · `expected_md5_8` · `expected_lines` · `actual_md5_8` · `actual_lines` ·
`diff_count` · `diffs`（逐行：`line` / `expected` / `actual`，两侧内容**已归一化**）· `ok` · `why`。

**采集面**：中枢清单硬编码为**节点名** `("lobster", "top001")`；ssh 目标经
`tools/deploy_targets.json` 解析（`transport == "local"` 的节点用本地 `io.open` 读，其余走 ssh）。
远端**只发 `cat` 一条形态**（`<_guarded>` 包装 + `K27RC=` 回传），不写远端任何文件、
不重启任何单元、不改任何 `nats.conf`。

**依赖注入**：`main()` 里 `import gen_nats_conf` 后把 `perm_list` 作为参数传进纯函数；
`analyze()` / `derive_users_block()` 内**不许**直接 import 它。装置侧传**自己那份独立实现** ——
所以「现网权限串 == 期望权限串」是被两份互不相识的模板同时确认的，不是自证。

### 3.1 夹具目录结构（`--fixtures`）

```
<DIR>/roster.json               可选 {"hub_list": [...], "roster": [[m, role, node, kind], …]}
                                缺省 = L0 档案本体 + 默认中枢清单
<DIR>/<node>/nats.conf          该中枢的配置
<DIR>/<node>/creds/<文件名>      该机上落地的凭据（runtime → nats-<m>.pass；conf → <m>.pass）
```

---

## 4. 本刀实测数字（2026-09-27）

**真机（`--live`，两中枢 lobster / top001）**

- 档案成员 **8** 人，顺序 == 规范序。
- `lobster` 用户区 **✓ 一致**，归一化 md5 `970bcf1c`，**13 行**，差异 **0** 行。
- `top001` 用户区 **✓ 一致**，归一化 md5 `970bcf1c`，**13 行**，差异 **0** 行。
- 推导侧归一化 md5 同为 `970bcf1c`（两 hub 共用一份推导侧）。
- `collect_errors` **空**；`ok = true`；**退出码 0**。
- `--json` 全文里 `password` 字样出现 **0** 次、`sha256:` 出现 **0** 次（零差异 ⇒ 无 `diffs` 行，
  出口面上连摘要都不需要出现）。

**装置（`tools/equivalence/roster_derive_equiv.py`）**

- **50 格**（A 段模板 12 + B 段抽块 7 + B 段归一化 7 + C 段 6 例夹具 × 4），**不一致 0 处**。
- `--runs 2` 两轮矩阵指纹逐字节相同：`37dbd959e3e7a13d5126a1ac3dddf4ce`。
- `--negative` 注毒 3 处（改一个凭据内容 / 换档案顺序 / 删 conf 里一个用户）**全红**，
  还原**转绿**。

**冻结夹具 6 例的期望**

| 例 | 含义 | hub-a 差异 | hub-b 差异 | 退出码 |
| --- | --- | --- | --- | --- |
| `clean` | 档案 / 凭据 / 两中枢完全一致 | 0 | 0 | 0 |
| `order_swap` | 档案顺序与 conf 不一致（换两条） | 2 | 2 | 1 |
| `cred_mismatch` | 某成员凭据文件内容变了 | 1 | 1 | 1 |
| `missing_cred` | 某成员凭据文件缺失 | 0 | 0 | **2**（`collect_errors` 非空） |
| `extra_in_conf` | 现网 conf 多一个名册外用户 | 4 | 4 | 1 |
| `perm_drift` | 只在 hub-a 改一个字的权限串 | 1 | 0 | 1 |

---

## 5. 本刀**不做**的：下一刀的前置缺口（已量出）

本刀只证明「档案 + 凭据 ⇒ 用户区」成立。**把生成器切到档案驱动**（`gen_nats_conf --from-roster`）
与**把对账器期望侧换成推导侧**，都卡在同一个**前置件**上：

> **中枢机上的生成器拿不到全体成员的凭据。**
> 现有凭据是**分布在各成员自己那台机**上的 —— 5 份在 `thinkbook` 的运行目录，
> 另 3 份各在 `lobster` / `quantum` / `top001` 的配置目录。中枢机上的 `gen_nats_conf.py`
> 只能从**旧 conf 原样继承口令**（它根本不认识「凭据文件」这回事，见 `l5-nats-ops.md` §2）。
> 所以「按档案生成」目前只能生成**成员名单与权限串**，生成不出口令那一列。

**推荐默认（尚未实现，本刀不许动手）**

- 在中枢侧建 `<conf_dir>/creds/<member>.pass`，作为**中枢本地的凭据镜像**。
- 由**部署件**从各节点同步过去（单向：节点 → 中枢；中枢侧只读）。
- 口令**不入仓、不入日志、不进任何输出**；镜像文件权限 `0600`，目录 `0700`。
- 同步完成后 `tools/roster_derive_conf.py --live` 必须仍然 rc=0 —— **它就是这条通道的验收门禁**。
- 迁移期两路并存的判据：镜像内容与节点侧原件的 sha256 前 12 必须逐个相等（对账器 M3 已有这把尺子）。

**顺序不可颠倒**：先建凭据通道 → 再切生成器 → 最后才切对账器期望侧。
每一步都拿本刀这把尺子（`--live` rc=0）当验收门禁。

---

## 6. 放置点 / 触发时机 / 回滚

**放置点**

- `agent-team/member_roster.py` —— L0，随 `agent-team/` 一起部署（部署表新增受管件，本刀**未**加，
  因为本刀不部署；切生成器那一刀再加）。
- `tools/roster_derive_conf.py` · `tools/equivalence/roster_derive_equiv.py` ·
  `tools/equivalence/_old/roster_derive_old_k27.py` —— 层 T，**只在源码树里跑**，不上任何节点。

**触发时机**

1. **每次改名册之后**（加人 / 退场 / 换凭据机）：先跑 `--live`，rc 必须是 0 或 1；
   是 1 就说明档案与现网已分叉，**先对齐再做别的**。
2. **每次动 `gen_nats_conf.py` 的用户区渲染之后**：跑装置 43 格，任何一格红都说明行格式改了形。
3. **下一刀切生成器之前与之后各一次**：切之前证「现状一致」，切之后证「新生成器产物仍一致」。
4. 与 `nats_membership_audit.py --check` 是**互补**不是替代：对账器比的是「现网 vs 现网 + 模板」，
   本件比的是「**档案** vs 现网」。前者查漂移，后者查**档案有没有资格当真源**。

**回滚**

本刀**只新增文件**，不改任何运行期件、不动任何节点 —— **回滚 = 删掉这 5 个新件 + 撤销四处登记**，
现网零影响。真正需要回滚方案的是下一刀（凭据通道），届时另立契约。

---

## 7. 红线

- 不许为了「让它一致」而放宽归一化口径 —— **只许 §2.3 那两处**。
- 不许把权限串搬进档案、不许在档案里写路径 / IP / 口令。
- 不许把口令明文写进任何交付物 / 输出 / 夹具 / 异常信息；出口只允许 `sha256:<前12>`。
- 不许动 `tools/nats_membership_audit.py` 及其基线与装置 —— 那是刀25 冻结的另一把尺子。
- 档案件**不许 import `gen_nats_conf`**（L0 → L1 反向依赖）。
