# `sys.path` 老债记账表

> **口径**：本表只登记一类债 —— **靠 `sys.path` 目录注入才能解析的裸名 import**（`import x` / `from x import y`
> 中 `x` 是本仓的件、却不在包路径上）。这类债的特征是：**在源码树里跑不起来，只有在把各件平铺到同一个
> 部署目录时才「碰巧」能跑**；分层归位（件搬进 `hub/` / `node/` 的包结构）会把它们暴露成
> `ModuleNotFoundError`。
>
> 不在本表范围：① `tools/equivalence/_old/**` 冻结快照（按裁定**不改行**，快照里的旧 import 不算债）；
> ② 等价装置自身携带的陈旧常量（如 `AGENT_TEAM_DIR = ROOT/agent-team`）——归**暗债批**；
> ③ 第三方包（`psycopg2`、`fastembed` 等）与标准库。
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

## 一、已收口（留档，供回溯）

| 件 | 原裸名 | 目标件 | 现状 | 收口于 |
|---|---|---|---|---|
| `node/c/a2a_svc.py` | `import agent_usage` · `import a2a_edge`（该件原**没有 sys.path 段**，源码树里 `--help` 直接报 `ModuleNotFoundError`） | `agent_usage`（今 `node/l2_adapter/`）· `a2a_edge`（今 `node/l1_transport/a2a/`） | **已改绝对包名**：`:35 from node.l1_transport.a2a import a2a_edge` · `:37 from node.l2_adapter import agent_usage` | 批⑤c（补路径段）+ 批⑤e（引用面） |
| `hub/l1_relay/gen_nats_conf.py:41` | `import member_roster as _roster` | `member_roster`（今 `node/l0_host/`） | **已改**：`from node.l0_host import member_roster as _roster` | 批⑤g |
| `tools/nats_membership_audit.py` · `tools/roster_derive_conf.py` | 靠 `AGENT_TEAM = ROOT/agent-team` 注入路径后裸名 import | 同上 | **已改**：注入项指向 `node/l0_host` + 绝对包名 | 批⑤g |
| `tools/roster_creds_mirror.py` · `tools/equivalence/gen_nats_conf_equiv.py` | 裸名 `import member_roster` | 同上 | **已改**：`from node.l0_host import member_roster` | 批⑤g |
| `hub/l3_domain/chat_svc.py:51` · `hub/l3_domain/blackboard_view.py:111` · `hub/l2_adapter/agent_model.py:68` | 裸名 `import view_auth`（三件搬进 `hub/` 后解析不到，**import 即崩**） | `view_auth`（今 `hub/x/`） | **已改**：`from hub.x import view_auth` | 批⑤f 发现、批⑤g 收口（⑤f 先落 `from backend import view_auth` 过渡） |
| `tools/scan_secrets.py` 用法示例注释 | 注释里写 `frontend/ui/` | `hub/l4_experience/` | **已改**（纯注释，零行为变化） | 批⑥ |

## 二、待收口（暗债批）

| 件 / 范围 | 债 | 现状 | 备注 |
|---|---|---|---|
| `tools/equivalence/` 下 **18 件 / 109 处** `agent-team` 引用 | 装置自带的陈旧常量（`AGENT_TEAM_DIR = ROOT/agent-team`、`NEW = ROOT/agent-team/<件>.py` 等） | **未改** | 涉及 `host_truth_equiv.py` · `team_cli_equiv.py` · `node_profile_equiv.py` · `nodelet_equiv.py` · `target_profile_equiv.py` · `cli_backend_http_equiv.py` · `group_frontend_equiv.py` · `gen_nats_conf_equiv.py` 等。这些装置**当前跑不通**（源件已不在 `agent-team/`），要逐件把常量改到**新路径**并重跑正控 / 负控 |
| `tools/equivalence/gen_nats_conf_equiv.py` 的 `NEW` 常量 | 仍指 `agent-team/gen_nats_conf.py`（该件早已搬 `hub/l1_relay/`） | **未改** | 既是上一条的实例，也单列：它是「装置陈旧常量」这一类债的**样例** |
| `tools/equivalence/todo_enrich_equiv.py:76` | 1 处真裸名残留 | **未改** | 归暗债批 |

## 三、待裁定（不是「债」，是**缺省值**问题）

| 件 | 现象 | 影响面 | 选项 |
|---|---|---|---|
| `hub/x/view_auth.py` | `TAILNET_CIDR = "<tailnet-cidr>"` 是**脱敏占位符**，模块级 `ipaddress.ip_network()` 解析即抛 `ValueError: '<tailnet-cidr>' does not appear to be an IPv4 or IPv6 network` | 凡 import 它的件在**源码树里**跑不过：`hub/l3_domain/chat_svc.py` · `hub/l3_domain/blackboard_view.py` · `hub/l2_adapter/agent_model.py` · `tests/test_view_auth.py` · `tests/test_chat_svc.py`。**线上部署副本不受影响**（部署面有真值） | ① 占位符 + **本地开发默认值**（如 `127.0.0.0/8`），真值仍由部署面给；② 从环境变量读（缺省时空值 + 显式报错）；③ 维持占位符（＝接受「仓内跑不通这些件」） |

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
