# L1 通信层 · 公共面契约

本文档登记 **L1 通信层对外承诺的公共面**：哪些符号是跨件可依赖的、各自承诺什么、
谁在用、变动时的兼容规则。对应判据 6「契约先于实现」。

范围：`agent-team/web_gateway.py`（阶段2 抽出的 HTTP 入口件）、
`agent-team/pg_literals.py`（刀16 抽出的 PG 类型口径件，层归属 L2 —— 因为它与
`web_gateway` 同属「被多件共用的通信/适配面」，一并在此登记其对外承诺）。

**纪律**：本文只写**可核对的事实** —— 每一项都给出「符号名 + 文件:行号 + 至少一个
真实使用方」。推测一律标「待定」，不写成承诺。行号对应写作时的 `HEAD`（刀16 之后、
刀17 未改动任何 `.py` 逻辑），符号名才是锚，行号只是便于定位。

---

## 一、`agent-team/web_gateway.py`

件本身的自述见文件头 docstring（`web_gateway.py:3-37`），四条设计约束（传输不含语义 /
零行为改变 / 鉴权可插拔 / 不持业务状态）是本件的总纲，下面各项都在它之下。

**在用的件（6 个，均 `import web_gateway`）**：
`a2a_edge.py:40`、`blackboard_svc.py:108`、`blackboard_view.py:105`、
`chat_svc.py:52`、`cli_backend.py:68`、`teamd.py:72`。

### 1. `envelope(status, obj, headers=None)` — `web_gateway.py:103`

**承诺什么**（逐条对应实现 `web_gateway.py:108-115`）：

- 签名 `(status, obj, headers=None)`，返回 **三元组** `(status, body_bytes, headers)`。
- `status` **原样透传**，本函数不改写状态码、不做范围校验。
- `headers` 先 `dict(headers or {})` 拷贝一份 —— **不修改调用方传入的字典**，
  `None` 合法。
- `obj` 的三种形态：
  - `bytes` → **原样作为 body**，**不设** `Content-Type`（调用方自带）；
  - `None` → body 为 `b""`，同样不设 `Content-Type`；
  - 其它 → `json.dumps(obj, ensure_ascii=False).encode("utf-8")`，
    并 `setdefault("Content-Type", "application/json; charset=utf-8")`
    （**setdefault**：调用方已给 `Content-Type` 时以调用方为准）。
- **错误体形状不由本函数决定**：`envelope()` 只负责编码，不造错误体。
  L1 自己产出的错误体只有三处，形状均为 `{"ok": False, "error": "<code>"}`：
  `_default_not_found` 额外带 `path` 字段（`web_gateway.py:170`）、
  `json_body` 的 `payload_too_large`（413，`web_gateway.py:94`）与
  `bad_json`（400，`web_gateway.py:100`）。业务错误体由各域件自己给。
- `Content-Length` **不在本函数设**，由 `handler_class()` 造出的 handler 在写回时
  兜底补齐（`web_gateway.py:214-215`）。

**谁在用**：`a2a_edge.py:791`（`code, body, out = web_gateway.envelope(code, payload)`）、
`blackboard_svc.py:3130`（同一句形态）；件内 `dispatch()` 的四个返回点
（`web_gateway.py:181/183/193/195`）与 handler 兜底（`web_gateway.py:229`）也都走它。

**变动的兼容规则**：三元组元数与元素语义、`ensure_ascii=False`、UTF-8 编码、
默认 `Content-Type` 字面量、`bytes`/`None` 两条旁路 —— 任一改动都会改到 6 个件的
线上响应字节，属于**破坏性变更**，须先改契约再改实现，并由
`tools/equivalence/gateway_envelope_equiv.py` 复跑（该装置有 41 条逐例对照）。

### 2. `_envelope` — `web_gateway.py:118`

**承诺什么**：`_envelope = envelope`，即**同一个函数对象**（不是包装、不是拷贝）。
它是刀7c 把私有名提升为公开名时留下的**兼容别名**，供改名前的外部调用点继续工作。

**谁在用**：生产件里**当前没有调用点**（`agent-team/*.py` 全域 grep
`web_gateway\._envelope` 零命中）；活引用只在等价装置
`tools/equivalence/gateway_envelope_equiv.py:118/123/128-133`，其中
`:128-129` 断言 `new._envelope is new.envelope`（别名同一对象）。

**变动的兼容规则**：别名**保留**，除非确认外部（含仓外调用方）无引用；
删除属破坏性变更。**不许**把它改成独立实现或包装函数 —— 装置的「同一对象」断言
会红，且会让两个名字的行为有分叉的可能。

### 3. `Gateway.route(path, fn=None, methods=("GET",), prefix=False)` — `web_gateway.py:137`

**承诺什么**：

- **既可当装饰器用，也可直接传 `fn` 调用**（`web_gateway.py:152`），
  两种形态都**原样返回 handler**，所以同一个函数上可以叠多条 `@GW.route`。
- 路径归一化：登记键 `path.rstrip("/") or "/"`（`web_gateway.py:143`），与分发侧
  `Request.path` 的归一化（`web_gateway.py:68`）**同一套** —— 这是为了杜绝
  「登记写了 `/api/` 却永远匹配不上」的静默死路由（注释 `web_gateway.py:140-142`）。
- `methods` 里每个方法名 `.upper()` 后各登记一条。
- `prefix=True` 走前缀表，命中规则是 `path == prefix or path.startswith(prefix + "/")`
  （`web_gateway.py:187`），且前缀表**按长度降序**排列 —— **更长者优先**
  （`web_gateway.py:148`）。
- 精确路由优先于前缀路由（`dispatch` 先查 `_exact`，`web_gateway.py:184-189`）。

**谁在用**：`a2a_edge.py:527`（直接调用形态，登记 GET 前缀路由）、
`a2a_edge.py:547/557/577/615`（装饰器形态，含 `prefix=True`）、
`blackboard_view.py:670-671`（**同一函数叠两条 route**，`/portal` 与 `PORTAL_PATH`）、
`blackboard_view.py:676..763`（整张只读路由表 + 根路径登记写方法）、
`teamd.py:395/399`。

**变动的兼容规则**：归一化口径、精确优先、最长前缀优先、返回 handler 原对象 ——
这四条是各件路由表的隐含前提，改动须先改契约。新增**可选**关键字参数（带默认值、
不改既有匹配结果）属兼容变更。

### 4. `Gateway.pre(fn)` — `web_gateway.py:154`

**承诺什么**：

- 登记**路由前钩子**并原样返回 `fn`（可当装饰器）；每个 `Gateway` 只有一个 `_pre`，
  **后登记的覆盖先登记的**（`web_gateway.py:156` 直接赋值）。
- 执行时机：在 `dispatch()` 的最前面，**先 `_pre` 再 `_auth`**，两者都在**查路由之前**
  （`web_gateway.py:175-183`）—— 因此它对**未命中路径也会跑**。
- 短路口径：返回 `None` → 继续；返回**非 `None`** → 该返回值直接 `envelope(*verdict)`，
  即必须是 `(status, obj)` 或 `(status, obj, headers)`；抛 `HttpError` → 同样短路
  （`web_gateway.py:180-181`）。
- 钩子的副作用（如提前读干请求体）是允许的 —— L1 不限制，但也不代管。

**谁在用**：`teamd.py:389`（`@gw.pre` 的 `_not_ready`：`daemon is None` 时任何路径
回 `503 {"ok": False, "error": "not_ready"}`）、`a2a_edge.py:530`（`@GW.pre`，把迁移前
两个 `do_*` 开头各一句 `_read_body()` 收成一次，见 `a2a_edge.py:468/641`）、
`blackboard_svc.py:2766`、`chat_svc.py:2450`。

**变动的兼容规则**：「先 `pre` 后 `auth`、二者皆在路由之前、对未命中路径也跑」
是 4 个使用件的行为前提（尤其 `teamd` 的全路径 503 与 `a2a_edge` 的读体时机），
改执行顺序或改「非 `None` 即短路」的判定属破坏性变更。

### 5. `Gateway.handler_class()` — `web_gateway.py:197`

**承诺什么**：

- 返回一个**新造的** `BaseHTTPRequestHandler` 子类（闭包捕获当前 `Gateway` 实例）。
- `server_version = "<name>/<version>"`（无 version 时就是 `name`，
  `web_gateway.py:134/203`）；`protocol_version = "HTTP/1.0"`（`web_gateway.py:204`）；
  `quiet=True` 时不刷访问日志（`web_gateway.py:206-209`）。
- **未登记的 HTTP 方法保持基类的 501**：类里先定义 7 个 `do_<METHOD>`，再把**没有任何
  路由登记过**的那些 `delattr` 掉（`web_gateway.py:255-258`）—— 基类只认
  `do_<METHOD>`，没有就给 501。**不自行发明 405 信封**（注释 `web_gateway.py:253-254`，
  对应件头设计约束 2）。
- **调用时机是契约的一部分**：必须在**全部路由登记之后**取，否则方法集合会算错
  （`a2a_edge.py:710` 的注释明写了这一点）。
- 写回时若 `headers` 里没有 `Content-Length`，按 `len(body)` 补齐
  （`web_gateway.py:214-215`）；写 socket 期间的异常被吞掉，不把服务带崩
  （`web_gateway.py:221-222`）。

**谁在用**：`a2a_edge.py:715`、`blackboard_svc.py:3074`、`blackboard_svc.py:4421`
（管理口 `ADMIN_GW`）、`blackboard_view.py:770`、`chat_svc.py:2817`、
`cli_backend.py:730`；`serve()` 内部也调它（`web_gateway.py:264`，`teamd.py` 经由
`gw.serve()` 走这条路，见 `a2a_edge.py:940` 的说明）。
`blackboard_view.py:759-763` 把写方法登记在根路径上，**目的就是**让本函数
**保留** `do_PUT` / `do_PATCH` 等 —— 这是对本契约「按登记删方法」的直接依赖。

**变动的兼容规则**：`server_version` 形状、`HTTP/1.0`、未登记方法回 501（而非 405）、
`Content-Length` 兜底 —— 都被各件的等价装置逐字节钉住
（`tools/equivalence/a2a_edge_equiv.py:183` 明写「方法集合」这条判据）。改动属破坏性。

### 6. 其余公共面（同属 L1 对外面，本刀只登记不展开）

以下符号也被跨件使用，形态见实现，本文**不对其细节作承诺**（标「待定」，
留给后续刀补齐）：`HttpError`（`web_gateway.py:48`）、`Request` 及其
`json_body()`（`web_gateway.py:59/90`，使用方 `a2a_edge.py:772`）、
`Gateway.auth()`（`web_gateway.py:159`，使用方 `blackboard_view.py:656`、
`cli_backend.py:609`）、`Gateway.not_found()`（`web_gateway.py:164`，使用方
`a2a_edge.py:692`、`blackboard_svc.py:2788/4414`、`blackboard_view.py:662`、
`chat_svc.py:2474`、`cli_backend.py:616`）、`Gateway.dispatch()`（`web_gateway.py:173`）、
`Gateway.serve()` / `serve_in_thread()`（`web_gateway.py:262/268`）。

---

## 二、`agent-team/pg_literals.py`（L2 适配层，刀16 新件）

件的自述与层归属理由见文件头 docstring（`pg_literals.py:3-23`）。
公开面由 `__all__` 显式钉住：`["UTC", "vector_literal", "to_utc"]`（`pg_literals.py:62`）。

**总纪律（件头 `pg_literals.py:19-22`，三条都是对外承诺）**：纯标准库零第三方依赖
（连 `psycopg2` 都不 import）；只读纯函数，无状态无 I/O 无日志；
**认不出来的输入一律返回 `None`，不抛、不猜、不编造**。

### 1. `UTC` — `pg_literals.py:28`

**承诺什么**：`UTC = datetime.timezone.utc`，即标准库那个单例对象本身
（不是自造的 `tzinfo`）。

**谁在用**：件内 `to_utc()`（`pg_literals.py:58-59`）。
注意：`blackboard_svc.py:826` 有**自己的** `UTC = datetime.timezone.utc`
（同一个标准库单例，值相同），刀16 **没有**把它改成 import —— 这是事实，不是分叉。

**变动的兼容规则**：必须始终是 `datetime.timezone.utc` 本体。

### 2. `vector_literal(embedding)` — `pg_literals.py:31`

**承诺什么**：

- 入参 `Optional[Sequence[float]]`，返回 `Optional[str]`。
- `None` → 返回 `None`（`pg_literals.py:33-34`）。
- 否则返回 pgvector 的**文本字面量** `"[" + ",".join(repr(float(x)) for x in embedding) + "]"`
  （`pg_literals.py:35`）—— 逐元素 `float()` 再 `repr()`，逗号分隔，方括号包裹，
  **无空格**。用 `repr` 而非 `str`/格式化，是为了不丢精度。
- 只造字面量，**不碰连接、不执行 SQL**。

**谁在用**：`shared_content.py:859/936/1081`（直接用公开名）、
`blackboard_svc.py:459/538/1369/2328`（经同名别名 `_vector_literal`，
别名定义在 `blackboard_svc.py:453`）、`backfill_embeddings.py:180`
（经别名 `_vector_literal`，定义在 `backfill_embeddings.py:86`）。

**变动的兼容规则**：输出字符串形状会直接进库（写进 pgvector 列），
改格式化方式 = 改「写进库的是不是同一个数」，属破坏性变更。
三个 L3 件的**同名别名**（`blackboard_svc._vector_literal` 等）是刀16 承诺保留的
模块属性面，删别名会打断它们的调用点与装置断言
（`tools/equivalence/pg_literals_equiv.py:17-18` 断言别名与本函数**是同一对象**）。

### 3. `to_utc(value)` — `pg_literals.py:38`

**承诺什么**：

- 入参 `Any`，返回 `Optional[datetime.datetime]`（带 UTC 时区）。
- `datetime` 入参 → 直接取用；`str` 入参 → `strip()` 后为空返回 `None`；
  结尾是 `Z`/`z` 的先换成 `+00:00` 再 `datetime.fromisoformat()`；
  解析失败返回 `None`；**其它类型一律返回 `None`**（`pg_literals.py:43-56`）。
- **裸时间（无时区）一律按 UTC 解释**（`pg_literals.py:57-58`），然后
  `astimezone(UTC)` 归一（`pg_literals.py:59`）。件头与 docstring 都写明理由：
  宁可口径单一，也不要「看你在哪台机器上跑」。
- 全程**不抛异常**（`ValueError` 已在内部吞成 `None`）。

**谁在用**：`shared_content.py:137/149/160/758`（直接用公开名）、
`blackboard_svc.py:850/1240/1583`（经同名别名 `_to_utc`，定义在
`blackboard_svc.py:838`）。

**变动的兼容规则**：「裸时间按 UTC」「认不出来返回 `None` 而不抛」这两条是上层
「逾期没逾期」（`blackboard_svc` 的 `overdue` 字段，见 `blackboard_svc.py:28`）
与「过期没过期」判断的地基，改动属破坏性变更。同名别名的保留规则同 `vector_literal`。

---

## 附：本文档的核对方式

每条契约都能用一条 `grep` 就地复核。例如：

```
grep -n "^def envelope" agent-team/web_gateway.py
grep -rn "web_gateway.envelope" agent-team/*.py
grep -n "__all__" agent-team/pg_literals.py
```

发现本文与实现对不上时：**以实现为准修本文**，同时判断该处是不是一次
未经契约的破坏性变更。
