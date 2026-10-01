# HACS 终态架构文档

> **两份同源**：本文件 = 内部完整版（`agent-team/docs/architecture-final.md`，由 `tools/gen_arch_doc.py` 生成）；
> 对外仓副本 = 同一份字节经 `tools/sanitizers/generic_deploy.py` 脱敏后的产物（真实主机地址 / 部署目录 / 真人 id → 占位符），
> **除这些占位符外两份逐字相同、数字同源**；文中 SHA、md5、计数一律以本文实测为准。

## 1 一句话结论 + 架构口诀

**一句话结论**：这套多 Agent 协作系统已经从「一文件承载全部职责、跨主机真值互相冒充」收敛成
**五层 + 三横切 + 组合根**的分层结构 —— 每条事实只有一个出处，其余位置一律是薄委托或可再生产物；
每一次搬迁都有一台**等价装置**把「零行为改变」钉成可复跑的字节级证据；每个数字都能用一条命令当场复算。

**架构口诀（八句）**

1. **上层依赖下层**，同层可互依，反向依赖是硬门禁，零容忍。
2. **组合根在最外**：`C` 可 import 任意层，任何层不得 import `C`。
3. **横切随处可引**：`X` 不参与方向判定。
4. **工具面不入运行路径**：`T` 不部署、不被运行件依赖。
5. **传输不含语义**：`L1` 只管投递，不认识群聊 / 会议 / 待办。
6. **适配层是 Agent 差异的唯一落脚点**：`L2` 之外不许出现「这个 Agent 特殊」。
7. **宿主事实收口 L0**：路径 / 凭据位置 / 会话真源 / 探针，一律向下取，不在上层拼。
8. **真值单向流**：档案 → 生成器 → 产物；产物永不回头当真值，**配置不许自证配置**。

---

## 2 分层模型

| 层 | 一句话职责 | 件数 |
| --- | --- | --- |
| C | 组合根（进程入口装配：只做装配与注入，不含业务与传输实现） | 2 |
| L0 | 平台层 Host（进程监管 / 路径 / 凭据 / 日志 / 探针 / 部署件） | 10 |
| L1 | 通信层 Transport（投信封 / 保投到 / 不重复 / 可观测 / 成员入网控制面） | 10 |
| L2 | 适配层 Adapter（Agent 差异唯一落脚点：调用 / 能力 / 上下文用量） | 5 |
| L3 | 协作域 Domain（群聊 / 待回应账 / 任务 / 会议 / 黑板 / 共享内容） | 14 |
| L4 | 呈现层 Experience（静态壳 / 业务视图 / 网关：同源托管 · 反代 · 鉴权跳转 · SSE 聚合） | 5 |
| X | 横切（身份与授权 / 契约与事件总线 / 可观测与门禁） | 1 |
| T | 工具面（巡检 / 驱动 / 验收 / 一次性脚本；不属于运行路径） | 31 |

**依赖方向（硬规则）**

- 只允许**上层依赖下层**：`L4 → L3 → L2 → L1 → L0`；**同层可互依**。
- **组合根 `C`**：进程入口装配件，只做装配与注入，不含业务与传输实现。
  `C` 可 import 任意层，**任何层不得 import `C`**。
  实现上不需要额外判据 —— `C` 排在 `layer_order` 最前、rank 最小，
  判据 `rank[src] <= rank[dst]` 天然让 `C → 任意层`成立、`任意层 → C` 违规。
- **横切 `X`**（身份与授权 / 契约与事件总线 / 可观测与门禁）：**可被任意层引用**，不参与方向判定。
- **工具面 `T`**（巡检 / 驱动 / 验收 / 一次性脚本）：**不属于运行路径**，
  在 `allowed_import.exempt_src_layers` 里豁免，也不进部署表。

**机器可读真源**：`tools/hacs_layers.json`（件 → 层 / 端口 / 一句话职责 + 层定义 + 端口登记 + 门禁规则）。
改层归属只改这份 JSON，门禁与台账都从它读。

**已声明的层门禁例外**：`rules.allowed_import.exceptions` 与 `rules.storage_handle_exceptions` **现均为空**
—— 唯一那条（`L2 agent_usage.py` 直接持 SQLite 句柄）在刀21 到期兑现后**撤销**，
不是改判据放宽，是真把句柄与文件格式下沉到了 `L0 node_profile.py`。

---

## 3 当前基线数字

| 量 | 值 | 可复跑命令 |
| --- | --- | --- |
| 登记件数 | **78** | `python tools/check_layers.py`（读表头行） |
| 层间 import 边 | **108** | `python tools/check_layers.py`（读表头行） |
| 登记端口 | **17** 个 | `python -c "import io,json;print(len([k for k in json.load(io.open('tools/hacs_layers.json',encoding='utf-8'))['ports'] if k!='_readme']))"` |
| 生成自 HEAD | `7fb3645`（2026-09-28） | `git rev-parse --short HEAD` / `git log -1 --date=short --format=%ad` |

口径说明：

- **件数**＝层归属表里登记的全部件（运行件 + 工具件 + T 面测试件 + 装置与冻结快照），**不等于**部署表受管件数。
- **层间 import 边**＝登记件之间的 import 关系条数。解耦会让这个数**上升**——
  底座件被提为唯一出处后，每个调用点各 import 一次，这是解耦的账单，不是耦合增加。
- 这两个数由 `check_layers.py` 现算；台账 HTML 里的同名常量需人工同步，
  以 `check_layers.py` 的实时输出为准。

---

## 4 端口表

**三种绑定面**

- **尾网**：绑 Tailscale 尾网地址（`<tailnet-cidr>`），只有在网节点可达；跨机协作走这一面。
- **回环**：只绑 `127.0.0.1`，仅本机可达；管理口与「由隧道代理的实例」都走这一面。
- **公网入口**：进程本身只绑回环，由命名隧道（cloudflared）对外暴露 —— 公网从不直连进程端口。

| 端口 | 用途（层归属表原文） | 绑定面 |
| --- | --- | --- |
| 9920 | A2A 边缘端点（每节点一份，只绑尾网地址） | 尾网 |
| 9930 | NATS 客户端口（明文 + 用户口令） | 尾网 |
| 9931 | NATS 集群路由 | 尾网（两台中枢之间） |
| 9932 | NATS 管理口（只绑回环） | 回环 |
| 9940 | 黑板服务（尾网） | 尾网 |
| 9941 | 看板只读视图（尾网） | 尾网 |
| 9942 | 看板公网入口实例（只绑回环，由隧道暴露） | 公网入口 |
| 9950 | PostgreSQL（黑板块库） | 尾网 |
| 9960 | 成员守护管理口（只绑回环） | 回环 |
| 9970 | CLI 类 Agent 统一后端 | 回环（件内默认 `--host 127.0.0.1`；登记表未另声明） |
| 9971 | 轻量节点守护的只读探针口（每节点一份，默认只绑回环） | 回环（`parse_bind()` 只放行环回与尾网，写别的地址拒启） |
| 9980 | 对话台后端 · 公网入口实例（只绑回环） | 公网入口 |
| 9981 | 群任务清单 | 登记表未声明绑定面 |
| 9984 | 对话台后端 · 尾网实例（默认文档 = 群聊页） | 尾网 |
| 9985 | 团队计算器后端（只绑回环） | 回环 |
| 8787 | HACS 数据层骨架（本地实验，未接真源） | 本机 |
| 8080 | 前端开发服务器（仅本机开发用） | 本机 |

> 端口总数须与第 3 节的「登记端口」对上；两处同源于 `tools/hacs_layers.json` 的 `ports` 表。

---

## 5 四道硬门禁 + 提示级检查

### ① 分层门禁 `python tools/check_layers.py`

**退出码只由检查 1 决定**；检查 2 / 3 / 4 / 5 是**提示级**，照实打印命中数，不影响 rc。

| 检查 | 口径 | 级别 | 为什么是这个级别 |
| --- | --- | --- | --- |
| 1 · 依赖方向 | 登记件之间的 import 图必须与 `layer_order` 一致（同层放行、`X` 放行、`T` 豁免） | **硬门禁** | 方向违规是结构性错误，机械可判、无灰区，一条都不许留；当前 rc=0、无反向依赖 |
| 2 · L1 通信层里的业务词 | `L1` 件里出现 `transport_business_words`（群聊 / 会话 / 待办 / 会议 / 黑板 / 共享区 / 圆桌 / 任务队列） | 提示级 | 机械判定分不出「注释/`help` 文案里提到」与「代码里真依赖语义」。写成硬门禁只会逼人改注释而不是改结构 —— 那是消红，不是解耦。判定留给人，机器只负责**不让它消失在视野里** |
| 3 · 宿主硬编码 | `L1–L4` 件里的 IP 字面量与绝对路径常量（`/etc\|/opt\|/home\|/root\|/var` 或盘符开头） | 提示级 | 刀22 收口后**非环回命中已为 0**；余下全是 `127.0.0.1` 这类本地环回默认（合法，不收）与正则串被 `PATH_RX` 误判（如 `Z:\-+.`、`d:\s*(...)`）。刀33 已把 3 处误判**逐条实名登记**为 `rules.path_known_false_positives`（键＝件＋`PATH_RX` 命中的**原串**，严格全等才生效；不是按件/行号豁免）；检查**保留**、口径不变，路径指标由「恒卡在 3」变为 **要收 0 + known-FP 3**，真硬编码不再被噪声淹没。仍为**提示级**（要收数照实打印，不影响 rc）——误判源未清前写成硬门禁＝制造假红 |
| 4 · 越层持存储句柄 | 直接 import `sqlite3` / `psycopg2` 的件，落在 `storage_handle_forbidden_layers`（`L1/L2/L4`）即打 ⚠ | 提示级 | 阶段4 之后 `L0` 是句柄的**所有者**（探针职责），已从禁止层名单移出；刀33 已裁定 **认现状、不登记例外**（`L0` 探针职责本就包含只读宿主真源；给它登记例外等于承认它不该持），`storage_handle_exceptions` 保持 **空** |
| 5 · 跨件私有面访问 | 一个件访问另一个件的 `_` 前缀私有名 | 提示级 | 刀14 新增、当前 0 命中。判据新、样本少，先提示级观察一段再考虑收硬 |

判据对照：检查1↔判据2 · 检查2↔判据5 · 检查3↔判据4 · 检查4↔判据1 · 检查5↔判据1。

### ② 等价装置 `tools/equivalence/*.py`

**为什么要有它**：每一刀都声称「零行为改变」。装置把这句话变成**可复跑的逐字节证据** ——
同一批输入分别喂「改前」与「改后」两侧，逐格比对；任何一侧写歪立刻不一致。
「改前」一侧有两种合法形态：**独立第二实现**（另一条算法路径，用于新增件）与
**正件的逐字节冻结快照**（`tools/equivalence/_old/*`，用于原地改造件，先冻后改）。
多数装置另带 `--runs N` 自证确定性、`--negative` / `--neg` 强制负控（注毒必须报红、还原必须转绿）。

现有 20 个装置（刀34 新增 1 个**等价-增严**型：除「改前改后逐字节相同」外，另有一批格子断言
**定向**由「命中」变「拦」，且内置反方向护栏 —— 任一格放宽即判负）：

| 装置 | 口径（比什么） | 格数 |
| --- | --- | --- |
| `admin_port_equiv.py` | teamd 管理口迁 L1 后，状态行 + 响应头（去 Date）+ 正文逐字节 | 刀1 报告实测 11/11 |
| `a2a_edge_equiv.py` | `a2a_svc`（C 组合根）+ `a2a_edge`（L1）双实现同起两台 HTTP 服务，逐字节比响应 | 刀2 报告实测 23/23 |
| `todo_enrich_equiv.py` | 模型富化执行器 + 展示名下沉 L2 后的**函数级**对照（`ast` 抽旧函数原文 `exec` 进自带 globals） | 刀3 报告实测 137/137 + 接线复验 13/13 |
| `target_profile_equiv.py` | 「目标主机档案」冻结矩阵：`target_claude_exec` 在「主机 × env 覆盖」全组合下的返回值 | 本件自述 7 × 3 = 21 格 + 2 条 `ssh_argv` 平台断言 |
| `cli_backend_http_equiv.py` | cli_backend HTTP 面收进 L1 后，同进程双实例跑同一张请求矩阵 | 本件自述 22 例线上基线 + 504/502/503/成功 = 26 例，另加 3 例自行加固 |
| `blackboard_view_http_equiv.py` | 看板新旧两实例挂同一个假上游 + 同一把会话密钥，`--mode tailnet` / `--mode public` 两套矩阵逐字节比状态码 / 响应头 / 正文 / 日志行 | 运行时打印（按两套矩阵逐例） |
| `blackboard_svc_http_equiv.py` | 黑板业务口 + 本机健康口各起新旧两份，同一张矩阵逐例比；数据库一个字节都不真连（注入假驱动） | 运行时打印（含 keep-alive 连发用例） |
| `gateway_envelope_equiv.py` | `_envelope()` 与公开名 `envelope()` 的三元组 `(status, body_bytes, headers)` 逐例比，**不归一不剔字段** | 台账实测 41/41（含 `_envelope is envelope` 同一对象断言） |
| `chat_svc_http_equiv.py` | chat_svc 的应答出口 + 非 GET 分派（刀8a）与 GET 路由表 + 前端页服务（刀8b）；三份实例同进程对跑 | 刀8c 实测 311 例（刀8a 及以前 155 + 刀8b 追加 156） |
| `chat_svc_static_filter_equiv.py` | **等价-增严**（刀34）：静态面「备份/临时件一律拦」。同一张用例表喂改前（冻结基线 `_old/chat_svc_old_k34.py`）与改后两侧，每格预标 `same`（逐字节相同）或 `tighten`（200 → 404 `not_found`）；`_serve_static` 与 `_serve_admin` 两处判据各跑一遍 | 本件实测 106 格（HTTP 61 = 等价 40 + 增严 21；判据逐名 37；冻结方向 2；未动面 6） |
| `group_frontend_equiv.py` | 前端拆三件套：无头浏览器 + CDP，钉客户端身份（UA / UA-CH / Accept-Language）防浏览器自动升级假红；刀20 起 CSS 冻结基线 `_old/group_css_old_k19.css` 三重身份硬校验 | 判据分段计（3 段正向 + 2 段反向 + 尾部残余 + 残留扫描），**只增不减** |
| `team_cli_equiv.py` | 群聊/会议业务摘出后的 **CLI 级**冻结矩阵：`argv → (stdout, stderr, 退出码, 出站请求序列)` + 落盘产物 | 层表登记 56 例 |
| `pg_literals_equiv.py` | pgvector 字面量与时间归一收口 L2 后，旧 3 份实现 vs 新 3 条别名 + 出处，逐字节比返回值 / 类型 / 异常类型 / 异常消息 | 四段（A/B 单元级 + C/D 调用面） |
| `host_truth_equiv.py` | 非 L0 件的宿主事实收口 L0 后，同一条语句在新旧两棵树上的取值 | 本件自述 19 项 × 10 组 env 场景 = 190 格（契约要求 ≥ 30 格） |
| `node_profile_equiv.py` | L0 节点档案正式化 + 宿主会话真源下沉：真 import 两侧模块、真调函数、真开临时 SQLite 库 | 本件自述 A 段 13 主机 × 4 env = 52 格 × 3 路；B 段 13 函数 × 3 flavour × 3 env = 117 格 × 3 路 |
| `nodelet_equiv.py` | 轻量节点守护：旧侧是照契约文字写的**独立第二实现**（不 import `nodelet`），新侧真调 `build_probe()` 并真起 HTTP 服务 | 层表登记 285 格（本件自述 A 段 20 场景 × 8 项 = 160 格） |
| `gen_nats_conf_equiv.py` | 生成器：同一份输入喂新旧两侧子进程，只比「渲染出的 conf 文本 + 退出码 + 是否写盘」 | 层表登记 66 格（含刀30 F 段 9 格） |
| `membership_audit_equiv.py` | 成员入网对账器：A 角色模板互证 / B 时长解析互证 / C 10 例冻结夹具 / D `--check` 身份口径 / E 期望侧不再自证 / F 口令真值换源 | 本件自述合计 154 格（12+14+80+6+20+22） |
| `roster_derive_equiv.py` | 「档案 + 凭据探针 ⇒ 中枢用户区」：抽块器与归一化器各走两条互不相识的算法路径 | 本件自述合计 50 格（12+7+7+24） |
| `roster_creds_mirror_equiv.py` | 凭据镜像通道：正件切成「采集/写入（有 IO）」与「`analyze()` / `parse_probe()` / 命令串构造（纯函数）」两半，只打后半截 —— 七种结论全在那半截里 | 层表登记 31 格（本件 docstring 写「约 22 格」是设计期估值） |

装置的红线（各件自证，不许放宽）：口令明文绝不进报告与 `artifacts`；写入命令串里不得有任何口令字面量；
探针命令串里不得有任何写操作；采集失败必须**优先于**一切漂移结论并判 rc=2。

### ③ 回归红点**集合**不变

判据不是「测试全绿」，而是**红点集合逐行一致** —— `chat_svc` 套件在改动开始前就带着既有红
（**27F + 8E**），要求的是改前改后**集合**相同，不是绝对值相同（绝对值会随夹具与用例增减漂移）。
取证方式：`git archive <旧 commit> agent-team | tar -x -C <临时目录>` **独立解包**起旧版对照，
不是同一副本自比。

### ④ 对外脱敏 `tools/push_hacs.py --manifest` + `tools/scan_secrets.py`

- `push_hacs.py --manifest` 按 `tools/hacs_manifest.json` 逐件脱敏 → 扫描 → 提交 → 推送。
  `--dry-run` 把脱敏结果写临时文件**再扫一遍**，链路验证完整。
- 两道扫描**口径不同，必须同时过**：
  ① 源仓 `tools/scan_secrets.py`：`LITERALISH` 判「整串都是引号字面量」——**拼接 `"a"+"b"` 也算命中**；
  ② `push_hacs.py` 内置 `secret-literal`：判「常量名含 token/password/… + `=` 后紧跟引号字面量」，**与拼不拼接无关**。
  稳妥写法：常量名避开 `token/password/secret` 词根，值以运行期变量开头。
- `push_hacs.py` 的 block 级规则：尾网地址 / 内网地址 / 团队云机公网地址 / Windows 用户目录 /
  Unix 家目录 / 服务部署目录 / 连接串 / 口令令牌字面量 / Bearer / 飞书 id / 真实人名。
- 护栏 `dirty_mapped_sources()`：**清单源件未提交即中止** —— 对外仓自己的扫描与 `py_compile` 拦不住半成品。
- **不许走 `--add` 窄路**：刀30 实证它绕过脱敏器。对外发布一律 `--manifest`。

---

## 6 六阶段与刀序历史

本表的「阶段」列是**内容口径**（按这一刀干的是哪件事归阶段）；**时间口径见规划文档**
（长跑规划按提交时间分阶段，两者在刀10–刀20 一段上不一致，详见表下说明）。

| 阶段（内容口径；时间口径见规划文档） | 刀 | SHA | 一句话 |
| --- | --- | --- | --- |
| 阶段0 台账 | （无刀号） | `1ed5cc5` | `push_hacs` 入库流水线落地（脱敏器 + 清单 + 门禁），层归属表成为唯一可编辑真源 |
| 阶段1 适配层与平台默认值 | 刀1 | `1264482` | `agent_usage`(L2) 水位单一实现、`node_paths`(L0) 宿主默认值、`web_gateway`(L1) 落地 + teamd 管理口迁移 |
| 阶段2 HTTP 面收进 L1 + 组合根 C | 刀2 | `4de88cf` | 消解 `a2a_svc`[L1]→`agent_usage`[L2] 反向依赖 —— 新增组合根层 `C`，实现迁 L1 `a2a_edge.py`（收尾 `57930b3`） |
| 阶段1 适配层与平台默认值 | 刀3 | `29ad0b0` | 模型富化执行器 + Agent 展示名下沉 L2 `agent_model.py`，`chat_svc` 只留同名薄委托 |
| 阶段1（阶段4 前哨） | 刀4 | `bfc8889` | 模型通道「宿主真值跨主机误用」纠正 + L0 目标主机档案雏形（收尾 `12f1e14`） |
| 阶段2 | 刀5 | `b288b64` | `cli_backend`(L2) 的 HTTP 面收进 L1 —— 自建 handler 177 行 → 子类 43 行 + 路由登记 |
| 阶段2 | 刀6 | `5b089b8` | `blackboard_view`(L3/L4) 的 HTTP 面收进 L1（收尾 `795021d`：推仓护栏 + 台账/规划 HTML 入仓） |
| 阶段2 | 刀7a | `daad20b` | `blackboard_svc`(L3) 的 HTTP 面收进 L1（`do_GET` 的路由留给 7b） |
| 阶段2 | 刀7b | `422f899` | `blackboard_svc` 的 `do_GET` 路由分派收进 L1 |
| 阶段2 | 刀7c | `da28cd0` | L1 的 JSON 信封提为公开面 `envelope()`，`_envelope` 保留为**同一对象**的别名 |
| 阶段2 | 刀8a | `9841382` | `chat_svc`(9980/9984) 的应答出口 + 非 GET 方法分派收进 L1 |
| 阶段2 | 刀8b | `aeefd5a` | `chat_svc` 的 `do_GET` 路由表 + 前端页服务收进 L1 |
| 阶段2 | 刀8c | `701f9ed` | 等价装置 `FakeDB` 形状按生产契约真实化 —— 不再有用例走 500 兜底；归因更正（收尾 `4e73ca8`） |
| 阶段2 | 刀9 | `f88f7ac` | `a2a_edge` 的 HTTP 面收进 L1 —— 阶段2 L1 迁移收官 |
| 阶段3 前端拆三件套 + L1 公共面 | 刀10 | `dc15d3a` | 前端运行真值收口：源码仓 `group.html` 组 4 件对齐线上（线上是严格超集） |
| 阶段3 | 刀11 | `3ece3da` | 抽出「传输客户端」`group_transport.js`（装置加固 `7fab76a` / 清单 `4267757` / 定稿 `9c8684a`） |
| 阶段3 | 刀12 | `974b6d9` | 抽出「渲染壳·样式表」`group.css` |
| 阶段3 | 刀13 | `2761c1d` | 抽出「业务视图」`group_view.js` —— 前端三件套收官 |
| 阶段3 | 刀14 | `049a418` | L2 公开面 `DISPLAY_NAMES_FALLBACK`，消除唯一一处跨件私有面访问；门禁新增检查5 |
| 阶段3 | 刀15 | `55cee72` | 群聊/会议业务从 L1 摘出：`team_group.py`(L3) + `team_cli_core.py`(L1)，`team.py` 改归组合根 `C` |
| 阶段3 | 刀16 | `aa532e9` | pgvector 字面量/时间归一收口到 L2 `pg_literals.py`（三处重复实现 → 单一出处） |
| 阶段3 | 刀17 | `cc5cf0a` | L1 公共面契约文档化 `agent-team/docs/l1-contract.md`（9 条契约 + 出处行号 + 使用方 + 兼容规则） |
| 阶段3 | 刀18 | `0828dac` | 任务面板子树下线（运行期不可达，先证零引用再删） |
| 阶段3 | 刀19 | `84d753f` | `group.css` 任务面板死样式下线；装置 CSS 锚换基 + 反向断言 |
| 阶段3 | 刀20 | `487c04c` | 装置 CSS 冻结基线钉死 + 负控证明敏感度；离线失效工具取证零调用方后下线（收尾 `f06f3c5`） |
| 阶段4 L0 节点档案与宿主事实收口 | 刀21 | `50755dd` | L0 节点档案正式化 + 宿主会话真源下沉，**撤掉层门禁唯一已声明例外**（收尾 `c529b32`） |
| 阶段4 | 刀22 | `a6ae189` | 非 L0 件的宿主事实收口到 L0 节点档案（收尾 `29012b7`：`L0` 移出「不该持句柄」名单） |
| 阶段4 | 刀23 | `0ebf9da` | 轻量节点守护 = 档案驱动的只读探针口 + 单元生成（收尾 `b296dff`） |
| 阶段4 | 刀24 | `f8d2fce` | 阶段4 收尾：台账 HTML 全量重算同步（收尾 `3fb3bc7`：台账补脱敏器） |
| 阶段5 成员入网控制面 | 刀25 | `bb1b801` | 成员入网**只读对账**（六项检查 M1–M6，不动 NATS、不改运行件；报告补全 `e91f3d3`） |
| 阶段5 | 刀26 | `b4ebb38` | 生成器加成员退场通道 `--drop-user` / `--dry-run` + 等价装置 + NATS 运维契约（基线过账 `9fe5565`） |
| 阶段5 | 刀27 | `e01789c` | **名册档案化**：新增 L0 `member_roster.py` + 「档案 + 凭据探针 ⇒ 用户区」逐字节自证 |
| 阶段5 | 刀28 | `530f54b` | 对账器「身份 / 观测量」分离 —— `--check` 改比**判据身份**，修掉基线时间脆性假红（补记 `e4b1025`） |
| 阶段5 | 刀29·A | `a4e5560` | 集群路由凭据轮换（两中枢 32B，only-cred-diff 验证）+ 轮换 runbook（文档顺序 `cb25a05`） |
| 阶段5 | 刀29·B | `44b5aed` | 对账器**期望侧**从「两中枢 conf 用户并集」切到 L0 名册档案 —— 期望侧不再是被检对象自己 |
| 阶段5 | 刀30 | `f592498` | 中枢侧**凭据镜像通道** + 生成器改由「名册 + 镜像」驱动（缺件 rc=2，绝不回退继承旧 conf 口令） |
| 阶段5 | 刀31 | `18e72c0` | 对账器 **M3 口令真值换源到成员本机凭据**；中枢 conf 与镜像降级为被核对的产物 —— 阶段5 收口 |
| 阶段6 收尾件 | 刀32 | `c5e6971` | 终态架构文档：模板 + 生成器 + 清单映射 ⇒ 内部完整版与对外脱敏版**同源同数字**（收尾 `44d3755`/`e24b17f`/`555aceb`） |
| 阶段6 后收尾 | 刀33 | `3cc8b20` | 判据收口：`host_path_owner` 单值 → **列表（只 2 件）**、`PATH_RX` 3 处误判**实名登记 known-FP（检查保留）**、`L0` 持句柄**认现状不登记例外**、阶段归并（内容口径）与 29 撞号（A/B 并列）口径落定；随后清理两处旧版生成器裸副本 |
| 阶段6 后收尾 | 刀34 | `d9cdaea` | **运行面纳管（解耦最后一公里）**：`session_ctx_archiver.py`（thinkbook 计划任务）+ `task_svc.py`（lobster systemd 9981）写进部署表（69 → **71** 件）；9984 静态面加「备份 / 临时件一律拦」判据（**只收紧**，106 格等价-增严矩阵自证）；`task_sys/task_svc.py` 裁定为无消费者的历史第一版、**物理件不动**；余 `ledger_patrol.py` / `cli_backend.py` 两件如实挂账、不擅自补条目 |
| 阶段6 后收尾 | 刀35 | `4a97a7a` | **运行面收敛**：删 7 件无用孤儿（5 件阶段1 一次性部署脚本 + 2 件调试残留）；删前后层门禁**边数 108 一件未变** —— 真孤儿的铁证（不属于任何依赖关系）；登记表 83 → **78** 件；对外仓同步 `git rm` 5 件残留（台账回写 `d783b58`） |
| 阶段6 后收尾 | 刀36 | `7fb3645` | **运行面纳管收尾**：thinkbook 纳管 5 件（薄壳 `team.py` + 依赖闭包 `team_cli_core.py` / `team_group.py` + 真在跑的 `ledger_patrol.py` + 其 import 依赖 `post_group.py`）⇒ 部署状态回到 **76 一致 / 漂移 0 / 缺失 0 / 多余 0**；`ledger_patrol.py` 运行期文案占位符**回正 2 处**，新增「仅文本改动」自证装置（AST 归一全等 + 串数不变 + 表外差异 0） |

> **阶段归并的既有分歧（如实记账）**：长跑规划里把**刀13 前端收官记作阶段2 全闭、阶段3 自刀14 起**；
> 而刀14–刀20 的提交说明自称「阶段3」。本表按「前端拆三件套 + L1 公共面」这一**内容口径**
> 把刀10–刀20 统归阶段3，与规划文档的**时间口径**并存，编号怎么归并由主控裁。
> 另：刀29 编号撞车（`a4e5560` 的集群口令轮换与 `44b5aed` 的期望侧切档案），本表按 A / B 并列，不改写历史。

---

## 7 关键架构决定

### ① 组合根 `C` 消解「下层读上层」

- **问题**：`a2a_svc`（L1）要读 `agent_usage`（L2）的上下文水位 —— 下层 import 上层，方向违规；
  而把 `agent_usage` 下沉到 L1 又会让「Agent 差异」漏出适配层。
- **决定**：新增**组合根层 `C`**（rank 最小）。进程入口件原地改组合根，只做装配与注入；
  实现迁到同层的 L1 实现件；L1 只**声明能力端口**，未注入时一律降级为「不知道」，**绝不编造数字**。
  同一手法后来复用在 `team.py`：搬迁后它必须调用 L3，于是改归 `C`。
- **证据件**：`agent-team/a2a_svc.py`(C) / `agent-team/a2a_edge.py`(L1) / `agent-team/team.py`(C)；
  装置 `tools/equivalence/a2a_edge_equiv.py`、`tools/equivalence/team_cli_equiv.py`；刀2 `4de88cf`、刀15 `55cee72`。

### ② HTTP 面统一收进 L1 `web_gateway`

- **问题**：五个服务各自手写 `BaseHTTPRequestHandler` —— 路由、JSON 信封、鉴权钩子、
  请求体读取、405/501 兜底各抄一份，行为细节互不相同，改一处修不了其余四处。
- **决定**：L1 `web_gateway` 只管**传输**（路由表 / JSON 信封 / 前置与鉴权钩子），
  **不出现任何业务词**；各服务改为继承 `GW.handler_class()` + `@GW.route` 登记，
  响应形态差异（`server_version` / `sys_version` / 协议版本）在子类覆盖。**组合根不动**。
- **证据件**：`agent-team/web_gateway.py`、契约 `agent-team/docs/l1-contract.md`；
  装置 `cli_backend_http_equiv.py` / `blackboard_svc_http_equiv.py` / `blackboard_view_http_equiv.py` /
  `chat_svc_http_equiv.py` / `gateway_envelope_equiv.py`；刀5 `b288b64` → 刀9 `f88f7ac`。

### ③ 真值单向链「L0 名册档案 → 生成器 → `nats.conf`」

- **问题**：谁是成员名册的真源？原先是「中枢 `nats.conf` 的规范形」——
  而 conf 正是要被校验的对象，于是「两台中枢一起被手改加人/删人」在对账里**完全看不见**。
- **决定**：把名册提为独立的 L0 档案 `member_roster.py`（有序表，顺序 == conf 用户区的规范序），
  `nats.conf` 的用户区**降级为它的产物**；生成器改由「档案 + 中枢凭据镜像」驱动，
  **缺件即 rc=2 点名并拒绝写盘**，绝不回退到「从旧 conf 继承口令」。
  档案三条铁律：口令绝不进档案（只记真值在哪台机的哪个文件）／不写路径不写 IP／不存权限串。
- **证据件**：`agent-team/member_roster.py`(L0)、`agent-team/gen_nats_conf.py`(L1)、
  `tools/roster_derive_conf.py`、`tools/roster_creds_mirror.py`；
  契约 `agent-team/docs/l5-roster-archive.md`、`agent-team/docs/l5-nats-ops.md`；刀27 `e01789c`、刀30 `f592498`。

### ④ 口令真值归属**成员本机**，对账器不再「配置自证配置」

- **问题**：对账器的 M3「各机 `*.pass` 内容 == 名册口令」里，「名册口令」取自**基准中枢的 conf**——
  被检对象同时当标准，中枢 conf 与刀30 落的镜像一起漂也发现不了。
- **决定**：口令真值改取**成员自己那台机上的凭据文件**（由名册的 `cred_node` / `cred_kind` 指定，
  逐字节、不 strip 不归一）；中枢 conf 与镜像**降级为被核对的产物**，各自新增纯增量发现项；
  声明的凭据文件采不到 → `collect_error` + **rc=2**，该成员真值视为**未知**，不猜、不顶替、**绝不回落 conf**。
  `analyze()` 签名与整份 JSON `SCHEMA` **一字未改**，观测面只增不删。
- **证据件**：`tools/nats_membership_audit.py`、冻结锚 `tools/equivalence/_old/nats_membership_audit_old_k31.py`、
  装置 F 段、契约 `agent-team/docs/l5-membership-audit.md`；刀31 `18e72c0`。

### ⑤ L0 节点档案收口宿主事实，**撤掉层门禁唯一已声明例外**

- **问题**：`L2 agent_usage.py` 直接持 SQLite 句柄读别的 Agent 的会话真源 —— 当时登记了一条例外，
  带到期条件（「阶段1 刀3 / 阶段4 L0：宿主路径与文件格式下沉后不再直接持句柄」）。
  同时 13 个非 L0 件里散着宿主事实（协作域节点地址、绝对路径常量）。
- **决定**：新增 L0 `node_profile.py` 作为**节点档案**，接手 SQLite 只读句柄、SQL、
  真源路径与行格式，并把散落的宿主事实收进去；例外**到期撤销**（是兑现，不是放宽）。
  两条铁律：**只读**（`mode=ro` URI、用完即 close、不建目录不写状态）与
  **不编造**（没配置/文件不在 → `None` 或空；真出错 → **原样抛**，由调用方按自己口径降级）。
  动态那一半是 `nodelet.py` 的只读探针口 `/probe`，**不泄密**：凡名字含 token/secret/key 词根的面，
  唯一合法形态是 `{present, len}`，出口前机械复核，违反直接抛。
- **证据件**：`agent-team/node_profile.py`、`agent-team/node_paths.py`、`agent-team/nodelet.py`、
  契约 `agent-team/docs/l0-nodelet.md`；装置 `node_profile_equiv.py` / `host_truth_equiv.py` / `nodelet_equiv.py`；
  刀21 `50755dd`、刀22 `a6ae189`、刀23 `0ebf9da`。

### ⑥ 前端拆三件套（渲染壳 / 业务视图 / 传输客户端）

- **问题**：`group.html` 一个文件里塞着 HTML 骨架、内联 `<style>`、唯一一个巨大的内联 `<script>`——
  样式、业务视图、传输三件事混在一处，改一处要通读全文，且运行真值长期只在线上、对源码仓不可见。
- **决定**：先做**运行真值收口**（以线上字节为准对齐源码仓），再按三件套逐字节抽出：
  传输客户端 `group_transport.js` → 渲染壳样式表 `group.css` → 业务视图 `group_view.js`。
  忠实性守则：原样搬运、不包 IIFE、不加 `"use strict"`、不加 BOM；外链脚本落在原内联块**同一 offset**，
  经典脚本仍 parser-blocking，文档序即执行序不变。静态件每请求现读磁盘 ⇒ 免重启、无陈旧缓存。
- **证据件**：`agent-team/ui/group.html` / `group.css` / `group_view.js` / `group_transport.js`；
  装置 `tools/equivalence/group_frontend_equiv.py`（无头浏览器 + CDP，客户端身份钉死防假红）；
  刀10 `dc15d3a`、刀11 `3ece3da`、刀12 `974b6d9`、刀13 `2761c1d`。

---

## 8 失败模式与回滚

**四台节点与运行目录**（真源 `tools/deploy_targets.json`，`deploy.py` 本体零硬编码）

| 节点 | 通道 | 监管方式 | 运行目录 |
| --- | --- | --- | --- |
| thinkbook | local | Windows 计划任务 | `<deploy-dir>` |
| lobster | ssh | systemd | `<deploy-dir>` |
| top001 | ssh | systemd | `<deploy-dir>` |
| quantum | ssh | systemd | `<deploy-dir>` |

`code_root` = **唯一代码编辑入口**（单向数据流起点）；四台的运行目录都是它的下游副本。
NATS 两台中枢 = lobster + top001；SSH 目标与私钥路径一律经部署表的 `${VAR:默认值}` 解析，
**真值不写进任何工具件**。

**部署前自动留档**：`tools/deploy.py` 每次推送前在目标机留 `.bak-<时间戳>`。

| 失败模式 | 现象 | 处置 |
| --- | --- | --- |
| 源码仓改错 | 门禁/装置报红 | 按 commit 回退：`git checkout <commit>^ -- <件>`；只读刀无部署面 ⇒ 无需重启任何服务 |
| 线上推错版本 | 行为与预期不符 | **逐台**重推旧版 + 重启：`python tools/deploy.py --node <节点> --file <件>` |
| 多节点 `--two-phase` 卡住 | phase2「远端写临时件失败」，单次调用可能 >420s 被工具截断 | **逐台部署**。若 phase1 已落盘且 md5 已验，phase2 失败只需 `ssh <节点> "systemctl restart <单元>"` + 健康检查 |
| 单元名不统一 | `a2a-svc.service` vs `agent-team-a2a.service`；thinkbook 是 Windows 计划任务 | 先 `systemctl list-units \| grep`（Windows 侧 `schtasks /Query`）确认真名，别照抄 |
| `LastTaskResult=0x40010004` | Windows 计划任务退出码 | 含义＝**已被终止**（不是崩溃），照终止处理 |
| 文件落盘但进程没重启 | 路由响应仍与旧版一致，误判「部署没生效」 | `systemctl show <单元> -p ExecMainStartTimestamp` 判「到底重启没有」 |
| 远端服务绑尾网 IP | 本机与远端 `curl 127.0.0.1` **都不通** | 探活一律用**节点 IP**，不要用回环 |
| 部署集依赖未闭合 | 依赖件不在同节点 `files` 里 → 重启即 `ModuleNotFoundError` | 加件前先问「它 import 谁、那些件在不在部署表里」；底座库件必须排在依赖者之前 |
| 推仓被脱敏门禁拦 | 尾网 CIDR / Windows 家目录等字面量命中 block 规则 | 把字面量改运行期拼装，或给该件登记脱敏器；**不许为过门禁而弱化规则** |

**回滚口诀**：源码仓按 commit 回退 → 线上逐台重推旧版 + 重启 → 健康检查全绿 → 再记账。

---

## 9 已知项（如实记账，不消红、不改口径）

1. **M6 `duplicate_route` 竞态**：两台中枢的 `/routez` 各报 `num_routes=4`（3 条陈旧），
   受制于 NATS 上游竞态。刀26 已把 M6 从 2 条收敛到 1 条，**这 1 条留红不消** —— 属上游观察项。
2. **`test_message_id_shape` 既有 flaky**：根因是 `gen_message_id()` 的随机后缀长度不定，
   20 万样本里 **1.55%** 不合形状；**改前树也命中**，不是本轮工程引入。
3. **发布面缺口 —— 刀34 起纳管 2 件，余 2 件如实挂账**：
   - ✅ **已纳管（刀34 写进 `tools/deploy_targets.json`，69 件 → 71 件）**：
     - `agent-team/session_ctx_archiver.py`（thinkbook）—— 计划任务 `AgentTeamCtxArchiver` 每 5 分钟拉起运行目录副本，
       `services` 故意留空（周期任务，无长驻进程可重启，下次触发自然取新件）；
     - `agent-team/task_svc.py`（lobster）—— systemd `task_svc` active+enabled，9981 在跑，
       健康口 `via: ssh` + `curl http://127.0.0.1:9981/health`（实测 200 / `{"ok": true, "service": "task_svc"}`）。
     两件**纳管当时线上仍是旧口径**（仍硬编码 `CHAT_BASE_DEFAULT` / DB / 日志路径，刀22 的 L0 收口没下去），
     故 `deploy.py --status` 会报这 2 件漂移 —— 属**待主控部署**，不是门禁失败。
   - ✅ **已纳管（刀36 补登，本条原记「仍未纳管 + 运行副本 md5 ≠ 仓内」，批⑪ 实测已不成立，按实改写）**：
     - `agent-team/ledger_patrol.py`（thinkbook）—— 计划任务 `AgentTeamLedgerPatrol` 拉起
       `<deploy-dir>\ledger_patrol.py --once`。刀34 当时运行副本与仓内 md5 不一致（原文记 `c62478b11691e60b`
       ≠ `89e9198191d3e234`），那是**刀34 时点的快照**；刀36 纳管并主控部署后两者已对齐
       （批⑪ 实测运行副本与旧仓副本 md5 前16 同为 `5DC63FD56F8F8295`，依赖件 `post_group.py` 同批登记、
       同为 `647BCDD8ED5794E6`）。
       **批⑪ ① 已把本件归位入本仓** `tools/ledger_patrol.py`（T 工具面）与 `node/l1_transport/post_group.py`（L1）；
       归位时做了真值收口（import 改绝对包名、两处基址收进 L0、真人 id 与昵称 env 化），故仓内副本
       **与运行副本不再逐字节相同**，这是预期内的 —— 部署表 `path` 重指（② ）与运行面切换（③ ）由架构侧另做，
       在此之前 `deploy.py --status` 对这两件报漂移属**待部署**，不是门禁失败。
   - ⏳ **仍未纳管（刀34 逐件取证扫描所得，如实列出、未擅自补条目）**：
     - `agent-team/cli_backend.py`（thinkbook）—— 计划任务 `AgentTeamCliBackend` 的实参是
       **源码仓路径** `D:\Work\…\agent-team\cli_backend.py --port 9970`，即运行目录副本
       （md5 `b9bd5452e85ffc5d` ≠ 仓内 `7f8371437d69924b`）**根本不被执行**，属陈旧裸副本 ——
       与「活件未纳管」不同类，处置（删副本 / 改回运行目录 / 纳管）待拍板。
   - 取证方法（可复跑，见 §9 末「复跑命令清单」）：thinkbook 取 `Get-ScheduledTask` 里 `AgentTeam*` 的
     `Actions.Arguments` 中的 `.py`；三台 Linux 取 `/etc/systemd/system/*.service` 的 `ExecStart`
     里的 `<deploy-dir>/*.py`；两边各与部署表比对。**top001 / quantum 扫描结果：零缺口**。
   （原在列的 `agent-team/gen_nats_conf.py` 两处**裸副本**已核实零引用后删除，见下条；**真源始终是源码仓**。）
4. **裸旧版生成器 —— 已清理（刀33 · 用户裁定「无用即删」）**：两份副本（top001 `<home>/gen_nats_conf.py`、
   本机运行目录 `/c/agent-team/gen_nats_conf.py`）的 md5 均为 `34a7b738bc9c` ≠ 仓内正件 `4bb1b6244e57`，
   且**零自动化引用**（`/etc/cron*`、`/etc/systemd/system`、`<home>/*.sh` 全无命中；本机无计划任务引用），
   手工误跑会退回刀30 前「从旧 conf 继承口令」口径 —— 正是刀30 要堵的洞。⇒ 判**无用即删**，两处均已删除并复验不存在；
   `lobster` 无副本；`quantum` 当时 SSH 不通（`<node-ip>:22` 超时），其扫描列为待补项。
5. **9984 把 `*.bak-*` 当静态件 serve —— 已落地（刀34）**：
   根因不是手抖，是**系统性**的：`tools/deploy.py` 部署第 1 步会把被覆盖的件留一份 `<path>.bak-<ts>`
   **就地**备份，而 `chat_svc.py` 的静态根 `ui/` 正是被部署的目录之一 ⇒ 历史版本原样躺在静态根里。
   既有两道判据都拦不住：`STATIC_EXCLUDE` 是逐件名单（只有 `serve.py` / `mock.js`）、隐藏件判定只认 `.` 开头。
   实测 `lobster:<deploy-dir>/ui/` 48 件里有 `group.html.bak-*` / `group.css.bak-*` /
   `codeblock.js.bak-inline-` / `calc.html.bak-v1-*` 四类。
   ⇒ 刀34 在 `_serve_static()` 与 `_serve_admin()` 的 `blocked` 判据里**各加一条**：任一路径段命中
   `STATIC_BACKUP_RE = re.compile(r"(\.bak($|[-._])|~$|\.orig$|\.rej$|\.tmp$|\.swp$|\.pre-)")` 即拦（404，走既有 `not_found` 分支）。
   **只收紧不放宽**，SPA fallback / MIME 钉死 / no-cache / 既有排除表与隐藏件判定**一字未动**。
   证据：`tools/equivalence/chat_svc_static_filter_equiv.py`（冻结基线 `_old/chat_svc_old_k34.py`）
   **106 格**矩阵 —— 40 格改前改后逐字节相同、21 格由「命中(200)」定向变「拦(404 `not_found`)」、
   **0 格放宽**（装置内置反方向护栏：任一格「改前拦 / 改后命中」直接判负），另含 37 格判据逐名断言
   （含 `backup.html` / `bak.html` / `a.bakery.js` / `a.pre.js` 等 8 个**形近但不该拦**的正控）。
   ⚠️ **静态根里那些 `.bak-*` 物理件本身刀34 未删**（红线：不碰生产机文件）—— 判据已拦住外部可见性，
   清理由主控执行。
6. **`task_svc` 双实现 —— 已裁定口径（刀34，物理件不动）**：
   **真源唯一 = `agent-team/task_svc.py`（lobster 9981 在跑）；`agent-team/task_sys/task_svc.py`
   是历史第一版，无消费者、任何主机未安装、勿 `enable`；物理件不动。**
   取证：`lobster` 上 `<deploy-dir>/task_sys` **不存在**；仓内无任何件 import 它。
   为什么「物理件不动」而不是删：`tools/equivalence/host_truth_equiv.py:93-94` 以**行锚点**
   （`old_line=35/36`，`DB_DEFAULT` / `LOG_DEFAULT`）引用该件，`tools/gen_ledger_doc.py:122` 登记其层归属 ——
   动它会破坏行锚点、使该装置转红。刀34 起 `agent-team/task_svc.py` 已纳管进部署表（见第 3 条）。
7. **`host_path_owner` 单值化与 `PATH_RX` 误判 —— 已落地（刀33）**，两条各有裁定：
   - ① **`host_path_owner` 改成列表，只含 2 件**：`agent-team/node_paths.py` + `agent-team/node_profile.py`
     （两件同属 L0、语义上共同构成「宿主字面量的唯一合法去处」；旧形态是单值字符串、按 `endswith` 只能写一个，
     另一件靠「检查3 只扫 L1–L4、L0 不在扫描面内」侥幸不报 —— 那是巧合不是判据）。`check_layers.py`
     读取处**兼容 `str` 与 `list`**（`str` 视为单元素列表）。**判据一条未放宽**：放宽只到这张 2 件的表为止，
     任何**非**表内件持宿主路径字面量，检查3 照旧报「要收」。负控实证（同一扫描面，只换 `host_path_owner`
     的形态）：退回单值 ⇒ `node_paths.py` 报 **9 处要收**；改回列表 ⇒ **0 处**。
   - ② **3 处正则串标 known-FP，检查本身保留**（删检查即属放宽判据）：`blackboard_svc.py:2681` 的
     `Z:\-+.`、`cli_backend.py:109` 的 `d:\s*(...)`、`session_ctx_archiver.py:565` 的 `n:\n((?:[` ——
     都是 `PATH_RX` 的盘符分支把源码里的**正则语法**当成了绝对路径。逐条实名登记在
     `rules.path_known_false_positives`，键 =（件, `PATH_RX` 命中的**原串**）**严格全等**才生效：
     不是按件豁免、不是按行号豁免、更不是改正则。命中后仍**逐条打印**文件:行号 + 原文 + 理由，
     只是不计进「要收」的路径指标 ⇒ 路径指标从「恒卡在 3」变为 **要收 0 + known-FP 3**，
     真出现新硬编码不再被噪声淹没。红线：不许往这张表里塞真的宿主路径；原串变一个字节本条即失效、
     该处立刻重新计进「要收」（负控已实证）。
8. **`L0` 与检查4 的口子 —— 已裁定：认现状，不登记例外（刀33，代码不动）**：
   `node_profile.py` 是存储句柄的**合法**所有者。检查4（提示级）当前对 `L0` **不打 ⚠**、
   已声明例外数为 **0**，这就是正确状态 —— 例外是给「本不该持」的件开的口子，而 `L0` 本就是
   宿主真源的唯一合法宿主层（刀22 已把 `L0` 移出 `storage_handle_forbidden_layers`）。
   给它补登一条例外反而是自相矛盾：等于承认它不该持。本刀**只把这条口径写进文档，判据与代码一行未动**。
9. **阶段6 候选**：`/probe` 二次佐证（刀31 登记）——它与刀31 红线「`SCHEMA` 不改 + 改前改后
   `--json` 逐字节相同」直接冲突，故没在收口刀里偷加，留作后续独立一刀。

### 复跑命令清单

| 命令 | 期望 |
| --- | --- |
| `python tools/check_layers.py` | 退出码 0；表头行的件数/边数与第 3 节一致；检查 1「无反向依赖」；检查 5「0 处」 |
| `python tools/gen_arch_doc.py` | 退出码 0；同一 HEAD 连跑两次，`agent-team/docs/architecture-final.md` 的 md5 相同 |
| `python tools/gen_ledger_doc.py` | 退出码 0；台账 HTML 的登记件数与 `check_layers.py` 实时输出同源同数 |
| `python tools/scan_secrets.py .` | 退出码 0，末行「结果：干净，可提交。」 |
| `python tools/push_hacs.py --manifest --dry-run` | 退出码 0；逐件「✓ 脱敏 N 项，dry-run 写入临时文件」，无 block 级命中 |
| `python tools/nats_membership_audit.py --check` | 退出码 0、`✓ 逐项一致`；基线文件一字未改 |
| `python tools/nats_membership_audit.py --live` | 采集失败 0 条；非零项仅 M6 的 `duplicate_route`（已知项 1） |
| `python tools/roster_derive_conf.py --live` | 退出码 0；两台中枢用户区归一化后逐字节一致、差异 0 行 |
| `python tools/roster_creds_mirror.py --check` | 退出码 0；两中枢 `creds/` 全员在位、内容一致、mode/owner 相符 |
| `python tools/equivalence/membership_audit_equiv.py --runs 2` | 不一致 0；两轮同 md5；`EQUIV_SENTINEL_OK` |
| `python tools/equivalence/membership_audit_equiv.py --negative` | 退出码 0（注毒处处报红、还原全部转绿） |
| `python tools/equivalence/chat_svc_static_filter_equiv.py` | 退出码 0；106 格全按期望：40 格逐字节相同、21 格「命中(200) → 拦(404 `not_found`)」、**0 格放宽** |
| `python tools/deploy.py --status` | 四台漂移 0 —— **刀34 起例外**：新纳管的 `session_ctx_archiver.py`(thinkbook) / `task_svc.py`(lobster) 在主控部署前会报漂移，其余 69 件仍应全一致 |
| `python tools/deploy.py --health-only` | 健康检查全绿 |
| `Get-ScheduledTask \| ? { $_.TaskName -like "AgentTeam*" } \| % { $t=$_; $_.Actions \| % { "$($t.TaskName)`t$($_.Arguments)" } }` | thinkbook 活件取证：逐条 `.py` 实参与 `tools/deploy_targets.json` 的 thinkbook 件表比对，缺口只应是 §9.3 挂账的那 2 件 |
| `ssh <节点> "grep -H '^ExecStart' /etc/systemd/system/*.service \| grep agent-team"` | lobster / top001 / quantum 活件取证：`ExecStart` 里的 `<deploy-dir>/*.py` 与各自件表比对；刀34 实测 top001 / quantum **零缺口** |

> 本文件由 `tools/gen_arch_doc.py` 从 `tools/arch_doc_template.md` 生成，请勿手改；改文案改模板，改层归属改 `tools/hacs_layers.json`。
> 数字会随代码漂移，落地前用上文命令复核。
