# `sys.path` 老债记账表

> **口径**：本表登记三类**「在源码树里跑不起来、而门禁 / 编译 / 单测一律绿灯」**的债 —— 它们的共同点是
> **不会被现有门禁抓到**，只能靠专门的探针 / 装置 / 实跑才能发现。
>
> - **① 裸名 import 债**（靠 `sys.path` 目录注入才能解析的 `import x` / `from x import y`，`x` 是本仓的件却不在包路径上）。
>   特征：**在源码树里跑不起来，只有在把各件平铺到同一个部署目录时才「碰巧」能跑**；分层归位（件搬进
>   `hub/` / `node/` 的包结构）会把它们暴露成 `ModuleNotFoundError`。
> - **② 陈旧路径常量债**：工具 / 装置里写死的旧目录路径（`AGENT_TEAM_DIR = ROOT/"agent-team"`）。
> - **③ 脱敏占位符债**：源码树里是 `<tailnet-cidr>` / `<deploy-dir>` / `<host>` 这类占位符（真值在部署面），
>   凡**模块级就被解析**（`ipaddress.ip_network()`）或被**当路径拼**（`"<deploy-dir>/view-session.key"`）的，
>   会让 import / 启动整个失败。
>
> 不在本表范围：`tools/equivalence/_old/**` 冻结快照（按裁定**不改行**，快照里的旧 import 不算债）；
> 第三方包（`psycopg2`、`fastembed` 等）与标准库。
>
> **验收口径**（改完自证）：两向都要扫，只扫一个方向必漏
> ```bash
> cd /d/Work/HACS
> git grep -nE "^[[:space:]]*(import|from)[[:space:]]+<件名>\b" -- "*.py" | grep -v "_old/"
> ```
> ① **出向**：谁 import 了这个件；② **入向**：这个件自己 import 了哪些**还没搬**的件。
> 批⑤f 就栽在只扫了①：`agent_model` / `blackboard_view` / `chat_svc` 三件里的裸名 `import view_auth`
> 在搬家后解析不到，**而门禁仍然 rc=0、边数仍然守恒**（悬空 import 只让门禁看不见那条边，不是报错），
> 只有**真 import 探针**能抓出来。

## 一、裸名 import 债 —— 已收口（留档，供回溯）

| 件 | 原裸名 | 目标件 | 现状 | 收口于 |
|---|---|---|---|---|
| `node/c/a2a_svc.py` | `import agent_usage` · `import a2a_edge`（该件原**没有 sys.path 段**，源码树里 `--help` 直接报 `ModuleNotFoundError`） | `agent_usage`（今 `node/l2_adapter/`）· `a2a_edge`（今 `node/l1_transport/a2a/`） | **已改绝对包名**：`:35 from node.l1_transport.a2a import a2a_edge` · `:37 from node.l2_adapter import agent_usage` | 批⑤c（补路径段）+ 批⑤e（引用面） |
| `hub/l1_relay/gen_nats_conf.py:41` | `import member_roster as _roster` | `member_roster`（今 `node/l0_host/`） | **已改**：`from node.l0_host import member_roster as _roster` | 批⑤g |
| `tools/nats_membership_audit.py` · `tools/roster_derive_conf.py` | 靠 `AGENT_TEAM = ROOT/agent-team` 注入路径后裸名 import | 同上 | **已改**：注入项指向 `node/l0_host` + 绝对包名 | 批⑤g |
| `tools/roster_creds_mirror.py` · `tools/equivalence/gen_nats_conf_equiv.py` | 裸名 `import member_roster` | 同上 | **已改**：`from node.l0_host import member_roster` | 批⑤g |
| `hub/l3_domain/chat_svc.py:51` · `hub/l3_domain/blackboard_view.py:111` · `hub/l2_adapter/agent_model.py:68` | 裸名 `import view_auth`（三件搬进 `hub/` 后解析不到，**import 即崩**） | `view_auth`（今 `hub/x/`） | **已改**：`from hub.x import view_auth` | 批⑤f 发现、批⑤g 收口（⑤f 先落 `from backend import view_auth` 过渡） |
| `tools/equivalence/todo_enrich_equiv.py:76` | 1 处真裸名残留 | `node_paths`（今 `node/l0_host/`） | **已改**：`from node.l0_host import node_paths` | 暗债批 |
| `tools/scan_secrets.py` 用法示例注释 | 注释里写 `frontend/ui/` | `hub/l4_experience/` | **已改**（纯注释，零行为变化） | 批⑥ |

## 二、陈旧路径常量债 —— 已收口（暗债批 `479ee03`）

| 件 / 范围 | 债 | 现状 | 收口 |
|---|---|---|---|
| `tools/equivalence/` 18 件装置的旧目录路径常量 | 装置自带的 `AGENT_TEAM_DIR = ROOT/agent-team`、`NEW = ROOT/agent-team/<件>.py` 等 —— 装置**当前跑不通**，而门禁 / 编译 / 单测**三路全绿**（装置不在门禁扫描面内），**证据链静默断掉** | **已逐件改到新分层路径** | 暗债批 |
| `tools/A` 之外的路径常量（`gen_arch_doc.py` 输出常量等） | 原指 `agent-team/docs/…` | **已改真**为 `docs/architecture.md`。本仓无模板 ⇒ 它 `rc=2` 死在模板缺失、**一个字节未写**；模板补进来后会覆写该文件，**归属待确认** | 暗债批 |
| `tools/gen_ledger_doc.py` | `--help` **会写盘**（改 `docs/HACS-分层台账.html`），执行器误触一次、靠 `git checkout -- docs/` 还原 | **已加 `--dry-run`**（写盘前的全部校验一个不放宽，只跳过 `io.open(...).write`）；实测 `--dry-run` 前后 `md5sum` 相同、`git status docs/` 为 0 | 本批 |
| `tools/equivalence/nodelet_equiv.py:568` 的 import 抽取正则 | 只取首个标识符 ⇒ `from node.l0_host import node_profile` 只抽到 `node`：**「L0 只 import 同层件」这条守卫恒真空转（假绿）**，同时「确实 import 了节点档案」恒假红；同文件 `:711` 的孪生正则却带 `.`，口径不一致 | **已统一为 `_import_names()`**（抽模块路径 + 首段 + 末段 + 被导入名，兼容 `as` 别名），并**新增一条「抽取器非空转」元判据**（合成夹具自证）。该装置红点 **3 → 1**（剩的那条是部署表缺失，见 §五） | 本批 |

> **残留 `agent-team` 引用：口径必须写明（两个数都对，打架的是口径）**：`git grep` 的 pathspec `*` **会跨 `/` 命中
> `_old/**`** ⇒「**排 `_old`**」与「**含 `_old`**」两个数能差一倍（实测 **47 vs 75 行**）。
> 逐条抽验确认残留**全属不该改的四类**：① **真值**（NATS 集群名 `CLUSTER_NAME = "agent-team"`、服务标识
> `agent-team-chat`、systemd 单元名 `agent-team-nodelet`）② **故意的历史取法**（`git show <ref>:agent-team/chat_svc.py`
> —— 从旧树取旧侧，装置的**正控**就靠它）③ **远端部署路径**（`/data/nbhome/agent-team/fixtures`）
> ④ `_old/**` 冻结快照（按裁定不改行）。

## 三、脱敏占位符债 —— 已按**方案 a** 收口（占位符 = 未配置 ⇒ 退本地开发默认值）

**统一修法**：`X = os.environ.get("HACS_X") or "<占位符>"`，然后 `if X.startswith("<"): X = <开发默认值>`。
**占位符语义 = 「未配置」；线上副本走真值分支 ⇒ 行为零变化。** 开发默认值三条硬要求：
① **必须已存在** ② 不得是已被占用 / 敏感的真实路径 ③ 可被 env 显式覆盖。

| 件 | 现象 | 影响面 | 现状 |
|---|---|---|---|
| `hub/x/view_auth.py:116` | `TAILNET_CIDR = "<tailnet-cidr>"` 是**脱敏占位符**，模块级 `ipaddress.ip_network()` 解析即抛 `ValueError: '<tailnet-cidr>' does not appear to be an IPv4 or IPv6 network` | 凡 import 它的件在**源码树里**跑不过：`hub/l3_domain/chat_svc.py` · `hub/l3_domain/blackboard_view.py` · `hub/l2_adapter/agent_model.py` · `tests/test_view_auth.py` · `tests/test_chat_svc.py`。**线上部署副本不受影响** | **已收口**：退到 **`100.64.0.0/10`**（Tailscale/CGNAT 默认段 —— 与 `test_view_auth.py:404` 的期望值、`nodelet_equiv` 的 `100.127.255.254` 上界用例**逐字吻合**）；可用 `HACS_TAILNET_CIDR` 覆盖 |
| `node/l0_host/node_paths.py:67–69` | `WIN_RUNTIME_DIR` / `POSIX_RUNTIME_DIR` / `POSIX_ETC_DIR` 被统一替换成 `"<deploy-dir>"`（**违反本件 docstring 自己写的 Windows 惯例** `<home>\agent-team`）；拼出 `'<deploy-dir>/view-session.key'` ⇒ Windows 下 `[Errno 22] Invalid argument` | `blackboard_view` 在 **tailnet 模式**（不传 `--session-key-file`）**启动即退** ⇒ `test_view_auth` 4 个 `setUpClass` error | **已收口**：退到 **`tempfile.gettempdir()`**（本件约束「只读、**不建目录**」⇒ 默认值必须是**已存在**的目录；写成 `<tmp>/agent-team` 会因父目录不存在而 `No such file or directory`）。真值仍由 `AGENT_TEAM_RUNTIME_DIR` / `AGENT_TEAM_ETC_DIR` 注入 |
| `tests/test_view_auth.py:57` | 夹具常量 `FIX_NODE_CLOUD_IP = os.environ.get(...) or "<host>"` | `test_tailnet_addresses` 失败（`<host> 应该算 tailnet`） | **已收口**：退到 **`100.64.0.7`**（段内地址；本文件是 `hacs-test-fixtures`、允许假值，真值可用 `AGENT_TEAM_TEST_NODE_CLOUD_IP` 注入） |

**未改的同类占位符**（**不阻断** import / 启动，故不属本债；真值仍由部署面给）：
`hub/l3_domain/chat_svc.py:113` `DEFAULT_HUMAN_ID` · `hub/l3_domain/group_watch_pending.py:33` `HUMAN_IDS` ·
`node/l0_host/node_profile.py:237/584/585/637`（`NT_TARGET_HOME` / `NODE_ADDR_PRIMARY` / `NODE_ADDR_SECONDARY` /
`WIN_CODEX_WORKDIR`）—— 都是**普通字符串**，不参与模块级解析、也不被当路径拼。
其中 `node_profile.py:584` 的 `NODE_ADDR_PRIMARY = "<host>"` 会让**探活类装置**失真
（`node_profile_equiv` 剩 1 处红即与此相关），留待**部署件批**一并处理。

**明确「不收口」的一类（判据，防后来人误改）**：`tests/e2e_blackboard_crossmachine.py:14/18` 与
`tests/e2e_semantic_search.py:19/22` 里的 `"<host>"` / `r"<home>\agent-team\client-token"` —— 这两个是
**跨机 e2e**（要真节点 + 真 token），占位符站的是**真值前置条件**，不是「开发兜底缺口」。
**不许**按方案 a 给它们塞默认值：塞了就把「前置缺失」伪装成「跑通了」，e2e 直接失去意义。
本机跑不起来属**预期**（无 token 文件、无可达节点）；要跑就在节点机上按部署面真值跑。

> ⚠️ **脱敏纪律**：本文档与仓内任何文件**都不许写出真实的 tailnet 网段 / 主机名 / IP / 凭据**，
> 一律用 `<tailnet-cidr>` `<host>` `<node-ip>` 这类占位符。本条债是**既有状态**（批⑤e 已用
> `git stash` 对照证明：撤掉本刀全部改动后同一条命令崩在同一行），非任何一刀引入。

## 四、为什么这类债值得单独记账

分层整改把件从「平铺目录」搬进「包结构（`hub/` / `node/`）」后，**裸名 import 的解析前提就消失了**：
以前靠「部署时把所有件平铺到同一目录、再把该目录塞进 `sys.path`」，搬进包结构后同一段代码
要么改成绝对包名（`from hub.x import y`），要么明确保留注入（并在本表登记）。

门禁（`tools/check_layers.py`）**抓不到**这类债：悬空 import 不会让依赖图多一条边、也不会报错，
只会让那条真实的依赖**从图里消失**（边数看着守恒，其实是「看不见」）。因此对本类债的**唯一有效验收
是真 import 探针**：
```bash
cd /d/Work/HACS
python -c "import sys;sys.path.insert(0,'.');import <可疑件>;print('OK')"
```

## 五、等价装置面现状（暗债批收口后实测）

装置是「零行为变化」的**证据面**。它们跑不通时**门禁 / 编译 / 单测三路一律绿灯** ⇒ 证据链静默断掉。

| 栏 | 台数 | 说明 |
|---|---|---|
| **已转绿** | **5**（主控逐台自跑 `rc=0` 证实） | `gen_nats_conf_equiv`（66 格，改前 41 处不合格 → 0）· `blackboard_svc_http_equiv`（155/155）· `cli_backend_http_equiv`（29/29）· `pg_literals_equiv` · `group_watch_equiv` |
| 路径债已清、但仍红 1–3 处 | 2 | `node_profile_equiv` 剩 **1** 处（与 `node_profile.py:584` 的 `<host>` 占位符相关）· `nodelet_equiv` 258 格剩 **1** 处（部署表缺失） |
| 跑不通 · **ref 不在本仓历史** | 4 | 装置用 `git show <旧 ref>:agent-team/…` 取旧侧，而 `git cat-file -t 1264482` → `fatal: Not a valid object name`（`049a418` / `4de88cf` 同；**已独立核实**）。这些装置是在**源码仓**写的、旧 ref 没跟过来 ⇒ 改路径没用，**改 ref = 重定基线**，须单独拍板。**推荐：先把能跑的 5 台当基线封存，重定基线单列一批。** |
| 跑不通 · **冻结快照未入仓** | 2 | 缺 `_old/chat_svc_old_8a.py` · `_old/membership_audit_old_k29.py` |
| 跑不通 · **脱敏占位符** | 6 + `tools/post_incident_entry.py` | 见 §三（`<tailnet-cidr>` 既已收口 ⇒ 其中若干台应已可跑，待复跑确认）；另 `_old/group_old_k13.html` 含 `<human-id>`（79784B vs 源码仓 79780B） |
| **部署表缺失** | 1 处判据 | `nodelet_equiv` 的「部署表读得到，且四台机都在」：`tools/roster_derive_conf.py:75` 期望 `tools/deploy_targets.json`，**本仓无此件**（部署面件未入仓）⇒ 归**部署件批** |

## 六、部署件批（仓内定位已做；部署同步单列）

`comm/a2a/restart_a2a.py` → **已归位 `node/l1_transport/a2a/`**（内容零改动：仍 `HERE = dirname(__file__)` + `HERE/a2a.env`）。
`node/l1_transport/a2a/README.md` 早已按「`restart_a2a.py` + **同目录** `a2a.env`」写 ⇒ 搬件是**补齐已定案的终态**。
搬前已核：**本机计划任务零指向该件、部署表无该件** ⇒ 无活调用方、零运维风险。
至此**四个旧顶层目录（`backend/` · `comm/` · `frontend/` · `agent-team/`）在 git 里全部退役**（各目录跟踪件为 0）。

> **遗留（部署同步，须在节点机实跑）**：真值 `a2a.env`（含解释器 / 脚本 / 日志路径）部署在**节点机本机、不在仓内**，
> 件搬家后该文件须落到新目录并在节点机实跑一次。同批还有 `tools/deploy_targets.json` 未入仓（§五）。

## 七、本批新增的两条引用面教训（已固化进技能）

- **引用面第三类：以「字符串拼件路径」把件当子进程跑**。批⑤j 只扫了 import 面，漏掉
  `tests/test_view_auth.py:172` 的 `os.path.join(ROOT, "blackboard_view.py")` 与 `tests/test_e2e_local.py` 的
  `os.path.join(ROOT, "a2a_svc.py")` / `"a2a_call.py"` ⇒ 件搬家后**子进程启动即退且零输出**、测试报 `errors`（非 `failures`）。
  扫描模式：`os\.path\.join\((ROOT|HERE|REPO|_HACS_ROOT)[^)]*"[a-z_0-9]+\.py"`。
- **`--help` 不等于只读**：生成器类脚本要显式加 `--dry-run`；收口时必须核 `git status --short docs/` 为空。

## 八、装置面现状（批⑧ 后 · 全盘 22 台）

**绿 13 台**：`a2a_edge_equiv` · `admin_port_equiv` · `blackboard_svc_http_equiv` ·
`blackboard_view_http_equiv` · `chat_svc_static_filter_equiv` · `cli_backend_http_equiv` ·
`gen_nats_conf_equiv` · `group_watch_equiv` · `group_watch_round_equiv` ·
`membership_audit_equiv` · `pg_literals_equiv` · `roster_creds_mirror_equiv` · `roster_derive_equiv`

**红 9 台（四类）**：

| 类 | 台（括号 = 实测红量） | 根因 / 处置方向 |
|---|---|---|
| 方案 a 连带 | `host_truth_equiv`(45/190) · `l0_host_relocate_equiv`(13) · `node_profile_equiv` · `todo_enrich_equiv`(6 项配置覆盖) | 装置拿改造前源码树的**占位符默认值**比新侧的**开发兜底值** ⇒ 两侧应在**同一套部署面真值 env** 下比（装置本意是比行为，不是比占位符状态）。**不许改判据迁就** |
| 待细查 | `chat_svc_http_equiv`(34/311) · `target_profile_equiv`(19/21) | `/login` 302 一类；`<user>@<node>` 占位符 + 目标档案口径 |
| 两仓分叉 · 待拍板 | `group_frontend_equiv` · `team_cli_equiv` | 冻结快照在两仓字节不同（k13：`b4da609f…` vs `58497402…`；team_old_k15：`2c487e78…` vs `8d584d71…`）⇒ 装置**自保护停手**，以谁为准需拍板 |
| 部署面缺件 | `nodelet_equiv`(1/258) | `tools/deploy_targets.json` 未入仓（部署件批） |

**旧侧来源口径（批⑧ 起）**：装置的旧侧基线取自**源码仓**（`agent-team/` 时期的提交在
`../多Agent相互协作`，本仓是整改后的新仓、不含那段历史）。`SRC_REPO` 解析顺序：
env `HACS_SOURCE_REPO` → 同级目录兜底 → 退回本仓（不静默）。凡比 `agent-team/…` 旧侧、
或比 `node/l0_host` 默认值的格，都必须走这条。

**两条硬纪律（批⑧ 换来的）**：
1. **从源码仓补冻结快照前必须先扫真值** —— 源码仓的件带真值 IP，而本仓**要对外发布**；
   快照的契约是「一字不改」⇒ 不可能脱敏 ⇒ 带真值的**一律不收**。
2. **装置有写副作用** —— `materialize_old()` 会**覆盖式写入** `_old/*.py`：跑一次装置就把
   仓内冻结快照改了。**跑完装置必须 `git status` 核一遍**，被改的 `_old/` 一律还原。
