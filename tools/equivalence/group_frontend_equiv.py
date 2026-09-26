# -*- coding: utf-8 -*-
"""刀11/刀12/刀13/刀18/刀19 · 群聊前端「拆三件套」等价装置（无头浏览器 + CDP，纯 stdlib）。

刀19 改动概要（**只换基、只加严，一类判据没删、一个期望值没下调**）：

* 刀18 下线面板子树之后，``group.css`` 里只服务该面板的规则成了纯死样式
  （``#taskbtn``/``#taskpanel``/``#tp-head``/``#tp-err``/``#tp-list``/``.titem*``/
  ``#tp-add*``，以及窄屏 ``@media`` 里的 ``#taskpanel{width:94vw}``）——
  容器不在 DOM 里，选择器在活 DOM 上一个都匹配不到。刀19 整段删除，31078 → 27996 B。
* 于是第 0 类 CSS 文本级锚**换基**：``css_text_level()`` 从「整块相等」→
  ``CSS_DROPPED`` 之外的保留块纯拼接 + 尾部残余 0 B + 反向断言 + 字节账 +
  交错重组（两段账都平）+ 残留扫描（``#taskpanel``/``#tp-`` 一个字节不许剩）。
  与刀18 的 ``js_view_text_level()`` 同一套路、同强度。改基前的装置原件另存
  ``_old/group_frontend_equiv_old_k19.py``。
* 计算样式 / CSSOM / 外链反向证据**一条没动**：两侧本来就同发 ``agent-team/ui/group.css``
  这一份，删的又是活 DOM 匹配不到的规则 —— 全绿即是「这些规则确实是死的」的浏览器级取证。

刀18 改动概要（**只换基、只加严，一类判据没删、一个期望值没下调**）：

* 刀18 把「任务清单面板子树」（``#taskpanel`` 相关的 ``tErrShow``/``tErrHide``/
  ``tLoad``/``tRow``/``tRender``/``tAdd``/``tOpen``/``TSTATUS``/``TPOLL``/
  ``pinTaskpanelBottom`` 与两处守卫绑定）整棵下线 —— 它运行期不可达（``#taskpanel``
  不在 ``group.html`` 的 DOM 里），并顺带删掉随之零引用的 ``TSVC`` / ``tApi``。
  ``TGROUP_DEFAULT`` **保留**：``tgid()`` 仍被活的 ``tqLoad()``/``tqClear()`` 调用。
* 于是两处文本级锚**换基**：``BLOCKS`` 5 段 → 3 段 + ``BLOCKS_DROPPED`` 反向断言；
  ``js_view_text_level()`` 从「整块相等」→ ``VIEW_BLOCKS`` 保留块纯拼接 + 尾部残余 0 B
  + ``VIEW_DROPPED`` 反向断言 + 字节账 + 源码残留扫描。两处都保住「逐字节对冻结快照、
  拼接必须恰好铺满整件」这个强度。改基前的装置原件另存
  ``_old/group_frontend_equiv_old_k18.py``。
* ``IDENT_PROBES`` 摘走 ``tApi``/``TSVC``/``window.tApi``，改进 ``IDENT_GONE``：
  由「两侧相等」升级为「两侧**各自**被钉死到字面量」；同时加严 —— 新增
  ``tgid``/``window.tgid`` 两条两侧相等的正面探针。
* ``FROZEN`` 里两条 ``tApi`` 用例一条没删、顺序没动，比法改 ``"gone"``：
  OLD 侧仍逐字节比结果 + 线级条数 + 请求行，NEW 侧钉死 ReferenceError + 线级 0 条。
  5 条 ``api`` 用例的两侧逐字节比较、计算样式、CSSOM、外链反向证据全部原封不动。


比什么
------
旧侧 OLD = ``--old <path>``（当刀开工前快照），默认 ``_old/group_old_k13.html``
新侧 NEW = ``agent-team/ui/group.html`` + 它引用的同目录兄弟件
           （``group_transport.js`` 刀11 抽出、``group.css`` 刀12 抽出、
            ``group_view.js`` 刀13 抽出）

两侧**来自两个不同的物理文件**，装置开头打印各自 md5 自证不是自比。

刀13 追加（只加不减；原有各类判据一条没删、期望值一个没下调）：

* **第 0b 类 · JS 文本级（业务视图）**：与下面第 0 类 CSS 文本级**同构** ——
  旧快照里那个**唯一的内联 ``<script>`` 块**的内容（``<script>\\n`` 之后 至
  ``</script>`` 之前）**==** ``agent-team/ui/group_view.js`` 的全部字节；外加新
  ``group.html`` 的字节账（内联 ``<script>`` 计 0 / 外链行计 1 / 总字节）与
  **重组互补证据**（把新件那 39 B 外链行换回原 74877 B 整块 → 与旧快照逐字节相等）。
  注意 ``</script>`` 计数**不减**（6 → 6）：外链 script **不是空元素**，
  ``<script src=...></script>`` 自带闭标签 —— 与刀12 的 ``<link>``（``</style>`` 1 → 0）不同。
* **反向证据**加一条：NEW 侧必须真的 ``GET /group_view.js``、OLD 侧一次都不许请求。
* **旧侧自比防护**：``--old`` 落在 k13 快照时硬校验它的 md5 == ``K13_MD5``。
* 第 0 类 CSS 文本级**换锚**（见 ``css_text_level()`` 的 docstring）：从「``--old`` vs 活件」
  改成钉死的 ``k12 快照 ↔ k13 快照``（两端都是冻结字节，期望数字一个没变），
  并**追加一条全链重组** —— 从**活的** ``group.html`` 两步走回 k12 快照，活件不脱钩。

刀12 追加了三类判据（只加不减，原有各类一条没动）：

* **第 0 类 · CSS 文本级**：旧快照内联 ``<style>``…``</style>`` 之间的字节
  **==** ``agent-team/ui/group.css`` 的字节（刀19 起换基为 **``CSS_DROPPED`` 之外的
  保留块纯拼接 + 尾部残余 0 B**，另加 ``CSS_DROPPED`` 反向断言、字节账、交错重组与残留扫描，
  同强度，见 ``CSS_DROPPED`` 处的注释）；外加新 ``group.html`` 的字节账
  （``</style>`` 计 0 / ``<link`` 计 1 / 总字节）与**重组互补证据**
  （把新件那 42 B 的 ``<link>`` 行换回原 31095 B 的 style 段 → 与旧快照逐字节相等，
  证明「只换了这一处」）。
* **计算样式**：两侧各等 ``readyState === 'complete'`` 且 ``document.styleSheets``
  连续两次采样稳定后，按文档序枚举 ``body *``，对每个元素取一张**固定属性表**的
  ``getComputedStyle`` 字符串值，两侧逐行 diff 必须 0 行、元素数必须相等。
* **反向证据**（防「外链根本没加载，两个空页面也算一致」）：NEW 侧
  ``document.styleSheets`` 里必须有一张 ``href`` 以 ``/group.css`` 结尾、
  且 ``cssRules.length > 0`` 的表；两侧把所有样式表的 ``cssRules`` 逐条
  ``cssText`` 拼起来后，**总长度与 md5 都必须相等**（CSSOM 归一后的同一口径）；
  线级还要求 NEW 侧真的请求过 ``/group.css``、OLD 侧一次都没请求过。

夹具
----
同一个 Python 进程里起一台桩后端（`ThreadingHTTPServer`），两侧共用**同一份冻结夹具**：

* ``/old/<f>`` → `_old/` 目录（只有那一个 html）；``/new/<f>`` → `agent-team/ui/`
* 两个页面里的 ``<script src="/md.js">`` 之类走的是**绝对路径**，所以根路径
  ``/md.js`` ``/tex.js`` ``/hl.js`` ``/codeblock.js`` ``/group_transport.js``
  ``/group.css`` ``/group_view.js`` 一律从 `agent-team/ui/` 发（两侧同一份；
  旧页面不引用 group_view.js，所以它在旧侧根本不会被请求 —— 这一点与线上形态一致）
* ``/api/*`` 全部返回冻结 JSON；``/login`` 返 **204**（见下「已知偏差」）

TSVC（``http://<host>:9981``）是**硬编码常量**，改端口就等于改被测代码。
本装置的做法是 **CDP `Fetch` 域请求拦截 + `Fetch.fulfillRequest`**：

    原理：`Fetch.enable` 登记 urlPattern `*<host>*`；浏览器在**发包之前**
    把请求挂起并推 `Fetch.requestPaused`，装置在 Python 侧按同一份夹具直接
    合成应答（含 `Access-Control-Allow-Origin: *`，顺带把 CORS 预检 OPTIONS
    一并合成）回灌给渲染进程。页面看到的 URL 始终是原始的 <host>:9981，
    被测代码一个字节没动，也不碰系统 hosts / 不需要管理员权限。
    选它而不是 `--host-resolver-rules`：后者还要真在本机 9981 起监听，
    多一个可能被别的进程占用的外部条件，不如拦截可自证。

判据
----
文本级一致 ∧ 标识符探针一致 ∧ 启动轨迹一致 ∧ 轮询轨迹一致 ∧ 动作序轨迹一致
∧ 函数级线级（请求行 + 请求头 + 请求体 + Error.message）逐字节一致
∧ 两侧 JS 错误列表一致 ∧ CSS 文本级一致 ∧ 业务视图文本级一致
∧ 计算样式逐元素一致 ∧ 外链确已加载
→ 打印 EQUIV_SENTINEL_OK，退出 0；否则逐条打 diff，退出 1。

用法
----
``python tools/equivalence/group_frontend_equiv.py``            刀13 默认旧侧（k13 快照）
``python tools/equivalence/group_frontend_equiv.py --old tools/equivalence/_old/group_old_k12.html``
``... -v``        打全量轨迹
``... --runs 3``  连跑 3 次自证确定性
``... --old-md5 58497402``  可选：额外硬校验旧侧 md5 前缀
"""

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
UI_DIR = os.path.join(ROOT, "agent-team", "ui")
OLD_DIR = os.path.join(ROOT, "tools", "equivalence", "_old")
K11_HTML = os.path.join(OLD_DIR, "group_old_k11.html")   # 刀11 基线（JS 文本级判据的固定锚）
K12_HTML = os.path.join(OLD_DIR, "group_old_k12.html")   # 刀12 基线（CSS 文本级判据的固定锚；供跨刀对照）
K13_HTML = os.path.join(OLD_DIR, "group_old_k13.html")   # 刀13 基线（--old 默认值）
NEW_HTML = os.path.join(UI_DIR, "group.html")
NEW_JS = os.path.join(UI_DIR, "group_transport.js")
NEW_CSS = os.path.join(UI_DIR, "group.css")
NEW_VIEW = os.path.join(UI_DIR, "group_view.js")

# 刀12 基线快照的 md5：**只**用来判定「刀12 的增量互补证据（重组回填）适不适用」。
# --old 传别的快照（例如 k11）时，旧侧与新侧的差异不止 style 段这一处，重组自然不等，
# 那条子判据据此显式声明「不适用」而不是悄悄放过 —— 见 css_text_level()。
K12_MD5 = "e5c4676943d70073fd881d936226a2ad"
# 刀13 基线快照的 md5（= 刀12 收口后、刀13 开工前的 group.html）。同样只用来判定
# 「刀13 的增量互补证据（把 39 B 外链行换回 74877 B 内联块）适不适用」—— 见 js_view_text_level()。
K13_MD5 = "58497402129900f8dfa0e7b513bd3afe"

TSVC_HOST = "<host>"
TSVC_ORIGIN = "http://" + TSVC_HOST + ":9981"

EDGE_CANDIDATES = [
    r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]

# ---------------------------------------------------------------- 客户端身份钉死
# 为什么要钉：浏览器构建号**不是被测行为**。Edge 在本机会静默自动升级（实测 153→154），
# 装置两次运行、甚至同一次运行的 OLD / NEW 两侧（两侧各 launch 一次 exe），可能抓到
# 不同构建 → User-Agent 与 sec-ch-ua 天然不等（154 连 brand 列表顺序和
# "Not A(Brand" 的写法都跟 153 不一样）→ 函数级线级逐字节 diff 非空，是**假红**。
# 做法：两侧各自在「任何页面导航 / 任何网络活动之前」把 UA + UA-CH 元数据钉成同一份
# 常量，让 sec-ch-ua / sec-ch-ua-platform / sec-ch-ua-mobile 全部据此**同源生成**，
# Accept-Language 一并钉死（它来自 profile locale，同样不是被测行为）。
# 钉的是**常量**而不是「当前 Edge 版本」：将来 Edge 再升级，装置输出仍逐字节不变。
# 注意这只钉客户端身份头，请求行 / 其余请求头 / 请求体 / Error.message 仍是强比较。
CLIENT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) HeadlessChrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0")
CLIENT_UA_BRANDS = [{"brand": "Chromium", "version": "154"},
                    {"brand": "Microsoft Edge", "version": "154"},
                    {"brand": "Not A(Brand", "version": "99"}]
CLIENT_UA_METADATA = {
    "brands": CLIENT_UA_BRANDS,
    "fullVersionList": [dict(b, version="154.0.0.0") for b in CLIENT_UA_BRANDS],
    "fullVersion": "154.0.0.0",
    "platform": "Windows",
    "platformVersion": "15.0.0",
    "architecture": "x86",
    "model": "",
    "mobile": False,
    "bitness": "64",
    "wow64": False,
}
# 传**语言表**（不带 q）：CDP 的 acceptLanguage 会自己补 q 值，带 q 传进去会补成
# "zh;q=0.9;q=0.9" 这种畸形串。这里的表与本机 Edge 默认一致，落到线上就是
# "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6"。
CLIENT_ACCEPT_LANGUAGE = "zh-CN,zh,en,en-GB,en-US"

SETTLE_BOOT = 2.0          # 启动 settle：主轮询 3000ms，2s 窗口内不会触发
SETTLE_POLL = 7.0          # 轮询窗口：至少覆盖 2 个 3s 周期
SETTLE_STEP = 0.45         # 动作序每步的静置


# ====================================================================== 冻结夹具

FX_SID = "meet_fixture_0001"
FX_SID2 = "meet_fixture_0002"

FX_SESSION = {
    "session_id": FX_SID,
    "title": "夹具群",
    "kind": "meet",
    "state": "open",
    "created_by": "nb001",
    "members": [{"id": "nb001", "display": "机主", "type": "human"},
                {"id": "hermes", "display": "Hermes", "type": "agent"}],
}
FX_SESSION2 = dict(FX_SESSION, session_id=FX_SID2, title="夹具新群")

FX_MESSAGES = [{
    "message_id": "fx-m-0001",
    "seq": 1,
    "author": {"id": "nb001", "display": "机主", "type": "human"},
    "created_at": "2026-09-21T10:00:00+08:00",
    "text": "夹具消息一条",
    "content": "夹具消息一条",
    "body": "夹具消息一条",
    "meta": {"audience": ["hermes"], "needs_response": []},
}]

FX_TODOS = [{"id": "td-1", "seq": 1, "status": "open", "title": "夹具待办",
             "owner": "hermes", "created_at": "2026-09-21T10:01:00+08:00"}]

FX_TASKS = [{"id": "tk-1", "group_id": FX_SID, "title": "夹具任务", "status": "pending",
             "priority": "normal", "msg_seq": 1,
             "created_at": "2026-09-21T10:02:00+08:00"}]

# 夹具里的落盘路径一律走 /data/nbhome/…（不许出现 Unix 家目录 /home/，对外仓扫描器会判 blocking）
FX_WORKDIR = "/data/nbhome/agent-team/fixtures"


def _sess_obj(sid):
    if sid == FX_SID2:
        return FX_SESSION2
    return dict(FX_SESSION, session_id=sid)


def fixture(method, path, body):
    """(status, content_type, bytes) —— 两侧共用，无任何随机/时钟输入。"""
    p = path.split("?", 1)[0]

    if p == "/login":
        # 已知偏差（见模块末「已知偏差」注释）：204 而非 200
        return 204, "text/plain; charset=utf-8", b""

    if p == "/api/me":
        return 200, "application/json", _j({"ok": True, "id": "nb001", "workdir": FX_WORKDIR})
    if p == "/api/whoami":
        return 200, "application/json", _j({"ok": True, "user": {
            "id": "nb001", "name": "nb001", "display": "机主",
            "role": "owner", "role_cn": "机主", "source": "tailnet"}})
    if p == "/api/agents":
        return 200, "application/json", _j({"ok": True, "agents": [
            {"id": "hermes", "display": "Hermes", "type": "agent"},
            {"id": "openclaw-nb", "display": "OpenClaw", "type": "agent"}]})
    if p == "/api/requires_login":
        return 401, "application/json", _j({"ok": False, "error": {"code": "unauthorized"}})
    if p == "/api/boom":
        return 500, "text/plain; charset=utf-8", "内部错误：这不是 JSON".encode("utf-8")

    if p == "/api/sessions":
        if method == "POST":
            return 200, "application/json", _j({"ok": True, "session": FX_SESSION2,
                                                "session_id": FX_SID2})
        # 两个群都在表里 → createGroup() 建完能 find 到并真的 openGroup（多走一段路），
        # 同时列表本身是冻结的（POST 不改变它），轮询签名恒定
        return 200, "application/json", _j({"ok": True, "sessions": [FX_SESSION, FX_SESSION2]})

    m = re.match(r"^/api/sessions/([^/]+)$", p)
    if m:
        return 200, "application/json", _j({"ok": True, "session": _sess_obj(m.group(1))})
    m = re.match(r"^/api/sessions/([^/]+)/messages$", p)
    if m:
        if method == "POST":
            return 200, "application/json", _j({"ok": True, "seq": 1, "message_id": "fx-m-0001"})
        # 冻结：永远同一批（POST 不改变它）→ CURSOR 恒为 1 → 轮询签名恒定
        return 200, "application/json", _j({"ok": True, "me": "nb001", "messages": FX_MESSAGES})
    m = re.match(r"^/api/sessions/([^/]+)/messages/(\d+)$", p)
    if m:
        return 200, "application/json", _j({"ok": True, "message": FX_MESSAGES[0]})
    m = re.match(r"^/api/sessions/([^/]+)/members$", p)
    if m:
        return 200, "application/json", _j({"ok": True, "members": FX_SESSION["members"]})
    m = re.match(r"^/api/sessions/([^/]+)/todos$", p)
    if m:
        return 200, "application/json", _j({"ok": True, "todos": FX_TODOS})
    m = re.match(r"^/api/sessions/([^/]+)/todos/purge$", p)
    if m:
        return 200, "application/json", _j({"ok": True, "purged": 0})

    # ---- 任务服务（TSVC :9981）面 ----
    if p == "/tasks" or p == "/api/tasks":
        if method == "POST":
            return 200, "application/json", _j({"ok": True, "task": FX_TASKS[0]})
        return 200, "application/json", _j({"ok": True, "tasks": FX_TASKS})
    if re.match(r"^/tasks/[^/]+$", p):
        return 200, "application/json", _j({"ok": True, "task": FX_TASKS[0]})

    return 404, "application/json", _j({"ok": False, "error": {
        "code": "not_found", "message": "夹具里没有这个端点"}})


def _j(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")


# ====================================================================== 桩后端

WIRE = []                 # 线级记录：本机桩收到的每个请求
WIRE_LOCK = threading.Lock()


def wire_add(rec):
    with WIRE_LOCK:
        WIRE.append(rec)
        return len(WIRE)


def wire_len():
    with WIRE_LOCK:
        return len(WIRE)


def wire_slice(a, b):
    with WIRE_LOCK:
        return list(WIRE[a:b])


STATIC_ROOTS = {"old": OLD_DIR, "new": UI_DIR}
ROOT_STATIC = {"/md.js", "/tex.js", "/hl.js", "/codeblock.js", "/group_transport.js",
               "/group.css", "/group_view.js"}
MIME = {".js": "text/javascript; charset=utf-8", ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8"}


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "k11stub"
    sys_version = ""

    def log_message(self, *a):        # 别把桩的访问日志喷进装置输出
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _record(self, body):
        hdrs = ["%s: %s" % (k, v) for k, v in self.headers.items()]
        wire_add({
            "where": "stub",
            "requestline": self.requestline,
            "path": self.path,
            "method": self.command,
            "headers": hdrs,
            "body": body.decode("utf-8", "replace"),
        })

    def _serve(self):
        body = self._body()
        self._record(body)
        p = self.path.split("?", 1)[0]

        # 1) 静态：/old/<f>、/new/<f>、以及根路径上的那几个兄弟脚本
        fs = None
        m = re.match(r"^/(old|new)/([A-Za-z0-9_.-]+)$", p)
        if m:
            fs = os.path.join(STATIC_ROOTS[m.group(1)], m.group(2))
        elif p in ROOT_STATIC:
            fs = os.path.join(UI_DIR, p.lstrip("/"))
        if fs is not None:
            if os.path.isfile(fs):
                data = open(fs, "rb").read()
                ct = MIME.get(os.path.splitext(fs)[1].lower(), "application/octet-stream")
                return self._out(200, ct, data)
            return self._out(404, "text/plain; charset=utf-8", b"no such static")

        # 2) 夹具
        st, ct, data = fixture(self.command, self.path, body)
        return self._out(st, ct, data)

    def _out(self, status, ctype, data):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if data:
            self.wfile.write(data)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = do_HEAD = _serve

    def do_OPTIONS(self):
        self._record(b"")
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()


class QuietServer(ThreadingHTTPServer):
    """浏览器关连接是常态（keep-alive 到期 / 页面卸载），别把 ConnectionReset 喷成噪音。"""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ====================================================================== 极简 WebSocket（RFC6455 客户端）

class WS(object):
    """够用就行的 WS 客户端：只发文本帧（掩码），收帧支持 126/127 长度与分片、回 pong。"""

    def __init__(self, url):
        m = re.match(r"^ws://([^/:]+):(\d+)(/.*)$", url)
        if not m:
            raise RuntimeError("认不出的 ws url: " + url)
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=60)
        self.sock.settimeout(60)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key))
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WS 握手时连接被关")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("WS 握手失败: " + head.split(b"\r\n")[0].decode("latin1"))
        self.buf = rest

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WS 连接被对端关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        payload = text.encode("utf-8")
        n = len(payload)
        head = bytearray([0x81])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + masked)

    def recv(self):
        """返回一条完整文本消息（自动拼分片、吞掉 ping/pong）。"""
        chunks = []
        while True:
            b0, b1 = self._read(2)
            fin = b0 & 0x80
            op = b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            data = self._read(ln) if ln else b""
            if op == 0x9:                       # ping → pong
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if op == 0xA:
                continue
            if op == 0x8:
                raise RuntimeError("WS 收到 close 帧")
            chunks.append(data)
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ====================================================================== CDP

class CDP(object):
    def __init__(self, ws_url):
        self.ws = WS(ws_url)
        self._id = 0
        self.on_event = {}

    def send(self, method, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        return self._id

    def call(self, method, **params):
        want = self.send(method, **params)
        while True:
            msg = json.loads(self.ws.recv())
            if "id" in msg:
                if msg["id"] != want:
                    continue
                if "error" in msg:
                    raise RuntimeError("CDP %s 报错: %s" % (method, msg["error"]))
                return msg.get("result", {})
            h = self.on_event.get(msg.get("method"))
            if h:
                h(msg.get("params") or {})

    def pump(self, seconds):
        """在 seconds 内只处理事件（拦截、弹窗…），不发命令。"""
        end = time.time() + seconds
        while True:
            left = end - time.time()
            if left <= 0:
                return
            self.ws.sock.settimeout(left)
            try:
                msg = json.loads(self.ws.recv())
            except socket.timeout:
                return
            finally:
                self.ws.sock.settimeout(60)
            if "id" in msg:
                continue
            h = self.on_event.get(msg.get("method"))
            if h:
                h(msg.get("params") or {})

    def evaluate(self, expr, await_promise=True):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=await_promise, userGesture=True)
        if r.get("exceptionDetails"):
            ed = r["exceptionDetails"]
            raise RuntimeError("页面里抛异常: " + json.dumps(ed, ensure_ascii=False)[:600])
        return r.get("result", {}).get("value")

    def close(self):
        self.ws.close()


# ====================================================================== 探针脚本（两侧完全一致）

PROBE_JS = r"""
(function () {
  window.__tr = [];
  window.__errs = [];
  var of = window.fetch;
  window.fetch = function (input, init) {
    try {
      var i = init || {};
      var url = (typeof input === "string") ? input
              : (input && input.url) ? input.url : String(input);
      var method = i.method || (input && input.method) || "GET";
      var body = (i.body === undefined) ? null : String(i.body);
      var cred = (i.credentials === undefined) ? null : i.credentials;
      var hs = null;
      if (i.headers) { hs = []; for (var k in i.headers) { hs.push(k + ": " + i.headers[k]); } hs.sort(); }
      window.__tr.push({ method: method, url: url, body: body, credentials: cred, headers: hs });
    } catch (e) { /* 探针自己绝不改变页面行为 */ }
    return of.apply(this, arguments);
  };
  window.addEventListener("error", function (ev) {
    window.__errs.push("error: " + (ev && ev.message));
  });
  window.addEventListener("unhandledrejection", function (ev) {
    var r = ev && ev.reason;
    window.__errs.push("unhandledrejection: " + ((r && r.message) || String(r)));
  });
})();
"""

# 两侧必须**相等**的标识符探针。刀18 从这里摘走 tApi / TSVC / window.tApi ——
# 不是放宽：它们改进 IDENT_GONE，由「两侧相等」升级为「**两侧各自被钉死到字面量**」
# （OLD 侧仍必须是 function / 原端点串，NEW 侧必须是 undefined / ReferenceError）。
# 同时**加严**：新增 tgid / window.tgid 两条 —— 任务面板子树下线后它是唯一留活的成员，
# 两侧都必须仍是 function（防「连活的那条也一起删了」这种过头删）。
IDENT_PROBES = ["typeof api", "typeof pull", "typeof boot", "typeof tgid",
                "API", "TGROUP_DEFAULT", "typeof window.api", "typeof window.tgid"]

# 刀18 反向断言（一侧一个字面量，比原来的「两侧相等」更紧）：
#   (表达式, OLD 侧期望值, NEW 侧期望值)
# OLD = k13 快照页面，NEW = 活件。两侧期望值为什么**不总是**一边有一边没有：
#   · `tApi` / `TSVC` 定义在 **group_transport.js** —— 刀11 起它就是**两侧共用的同一份
#     活件**（夹具把根路径 /group_transport.js 对两侧都从 agent-team/ui/ 发，见模块头
#     「夹具」一节）。刀18 把这两段从活件删掉，两侧就一起没有了 → 两边都钉 undefined，
#     等于**多钉了一条「TSVC 端点在前端已彻底不可达」**；
#   · `TSTATUS`/`TPOLL`/`tLoad`/`tOpen` 等定义在 **内联脚本**里，k13 快照自带 → OLD 仍有、
#     NEW 已无，这才是一边一个值。
# 不管哪种，两侧的值都被钉到字面量，比原先「只要求两侧相等」严。
IDENT_GONE = [
    ("typeof tApi",        "undefined", "undefined"),
    ("typeof window.tApi", "undefined", "undefined"),
    ("TSVC",               "<<throw>> TSVC is not defined",
                           "<<throw>> TSVC is not defined"),
    ("typeof TSTATUS",     "object",   "undefined"),
    ("typeof TPOLL",       "object",   "undefined"),
    ("typeof tErrShow",    "function", "undefined"),
    ("typeof tErrHide",    "function", "undefined"),
    ("typeof tLoad",       "function", "undefined"),
    ("typeof tRow",        "function", "undefined"),
    ("typeof tRender",     "function", "undefined"),
    ("typeof tAdd",        "function", "undefined"),
    ("typeof tOpen",       "function", "undefined"),
    ("document.getElementById('taskpanel') === null", True, True),
]

# ---------------------------------------------------------------- 刀12 · 计算样式
# 固定属性表：口径必须「与时间无关」—— 这里一条 transition / animation / transform
# 都不收（它们随时钟变化，会把装置变成掷骰子）。其余全是布局/盒模型/排版/配色的
# **最终计算值**（getComputedStyle 的字符串，颜色一律 rgb(...) 形式）。
STYLE_PROPS = [
    "display", "position", "z-index", "box-sizing", "float", "clear", "vertical-align",
    "width", "height", "min-width", "min-height", "max-width", "max-height",
    "top", "right", "bottom", "left",
    "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
    "border-top-style", "border-right-style", "border-bottom-style", "border-left-style",
    "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
    "border-top-left-radius", "border-top-right-radius",
    "border-bottom-left-radius", "border-bottom-right-radius",
    "overflow", "overflow-x", "overflow-y", "text-overflow",
    "color", "background-color", "background-image", "background-size",
    "background-position", "background-repeat", "box-shadow", "outline-width",
    "font-family", "font-size", "font-weight", "font-style", "line-height",
    "letter-spacing", "text-align", "text-decoration-line", "text-transform",
    "white-space", "word-break", "overflow-wrap", "list-style-type",
    "flex-direction", "flex-wrap", "flex-grow", "flex-shrink", "flex-basis",
    "align-items", "align-self", "align-content", "justify-content",
    "gap", "row-gap", "column-gap",
    "grid-template-columns", "grid-template-rows", "grid-auto-flow",
    "grid-auto-columns", "grid-auto-rows", "grid-column-start", "grid-column-end",
    "grid-row-start", "grid-row-end",
    "opacity", "visibility", "cursor", "pointer-events", "user-select",
]

# 等样式稳定：readyState === 'complete' 且 styleSheets 的表数与各表 cssRules 条数
# **连续两次采样不变**（100ms 一拍，最多 50 拍 ≈ 5s）。不稳就等，等不到就如实回报。
STYLE_WAIT_JS = r"""
(function () {
  function snap() {
    var a = [document.styleSheets.length];
    for (var i = 0; i < document.styleSheets.length; i++) {
      var n;
      try { n = document.styleSheets[i].cssRules.length; } catch (e) { n = "ERR:" + e.name; }
      a.push(n);
    }
    return a.join(",");
  }
  return new Promise(function (resolve) {
    var prev = null, tries = 0, stable = false;
    (function tick() {
      var cur = snap();
      tries++;
      if (document.readyState === "complete" && prev !== null && cur === prev) { stable = true; }
      prev = cur;
      if (stable || tries >= 50) {
        resolve(JSON.stringify({ready: document.readyState, snap: cur,
                                ticks: tries, stable: stable}));
        return;
      }
      setTimeout(tick, 100);
    })();
  });
})()
"""

# 样式表清点 + 反向证据的原料：每张表的来源（href / 挂载节点）、规则条数，
# 以及把所有表的 cssRules 逐条 cssText 拼起来的那一大串（Python 侧再取长度与 md5）。
SHEETS_JS = r"""
(function () {
  var out = {sheets: [], joined: ""};
  var parts = [];
  for (var i = 0; i < document.styleSheets.length; i++) {
    var s = document.styleSheets[i];
    var rec = {href: s.href || null,
               owner: s.ownerNode ? s.ownerNode.nodeName.toLowerCase() : null,
               media: String((s.media && s.media.mediaText) || ""),
               disabled: !!s.disabled, rules: -1, err: null};
    try {
      var r = s.cssRules;
      rec.rules = r.length;
      var t = "";
      for (var k = 0; k < r.length; k++) { t += r[k].cssText; }
      parts.push(t);
    } catch (e) { rec.err = String(e && e.name); }
    out.sheets.push(rec);
  }
  out.joined = parts.join("");
  return JSON.stringify(out);
})()
"""

COMPUTED_JS_TMPL = r"""
(function () {
  var PROPS = %s;
  var els = document.querySelectorAll("body *");
  var out = [];
  for (var i = 0; i < els.length; i++) {
    var e = els[i], cs = getComputedStyle(e), vals = [];
    for (var j = 0; j < PROPS.length; j++) {
      vals.push(PROPS[j] + "=" + cs.getPropertyValue(PROPS[j]));
    }
    var cls = e.getAttribute("class");
    var sigp = e.tagName.toLowerCase()
             + (e.id ? "#" + e.id : "")
             + (cls ? "." + cls.trim().split(/\s+/).join(".") : "");
    out.push(String(i).padStart(4, "0") + " | " + sigp + " | " + vals.join("; "));
  }
  return JSON.stringify(out);
})()
"""

# 函数级冻结输入（401 那例放最后：它会写 location.href）
# 第 5 列是**比法**，刀18 新增（用例一条没删、执行顺序一字没动）：
#   "both" —— 两侧逐字节 diff（请求行 + 请求头 + 请求体 + Error.message），口径同刀11；
#   "gone" —— `tApi` 随刀18 从 group_transport.js 下线；该件是**两侧共用的同一份活件**
#             （见 IDENT_GONE 处的说明），于是两侧都已无此函数 → 不再是「两侧比 wire」，
#             改成**两侧各自对字面量**：结果 JSON 必须逐字节是 ReferenceError、
#             且该用例窗口内的线级记录必须是 **0 条**（= TSVC 端点一次都没被打）。
#             这是「装置跟随实现」，不是放宽：原来只要求两侧一样（两边都对也算过），
#             现在两侧各自被钉到字面量，且多钉了一条「零出网」。
#             （5 条 api 用例的两侧逐字节线级比较一条没动。）
FROZEN = [
    ("api",  "GET",   "/api/me", None, "both"),
    ("api",  "GET",   "/api/zzz_notexist", None, "both"),
    ("api",  "POST",  "/api/sessions", {"name": "x"}, "both"),
    ("api",  "GET",   "/api/boom", None, "both"),
    ("tApi", "GET",   "/api/tasks", None, "gone"),
    ("tApi", "GET",   "/api/zzz_notexist", None, "gone"),
    ("api",  "GET",   "/api/requires_login", None, "both"),
]

# 刀18：下线函数的两侧字面量。键 = 用例的 call 串（与 res["frozen"][i]["call"] 同构）。
FROZEN_GONE_EXPECT = {
    "tApi('GET', '/api/tasks')": {
        "outcome": {"ok": False, "message": "tApi is not defined"},
        "wire": 0,
    },
    "tApi('GET', '/api/zzz_notexist')": {
        "outcome": {"ok": False, "message": "tApi is not defined"},
        "wire": 0,
    },
}

ACTIONS = [
    ("建群·打开对话框", '$("newbtn").click(); return "ok";'),
    ("建群·填标题并创建", '$("gt").value = "夹具新群"; $("gok").click(); return "ok";'),
    ("开群·点第一个群条目",
     'var g = document.querySelector("#groups .gitem"); if (g) { g.click(); }'
     ' return (g ? "ok" : "no-gitem");'),
    ("发一条消息",
     '$("text").value = "夹具发言"; $("text").dispatchEvent(new Event("input", {bubbles:true}));'
     ' $("send").click(); return "ok";'),
    ("加 Agent·打开对话框", '$("addagent").click(); return "ok";'),
    # 可选项判据抄 addAgentsToGroup() 自己的口径：置灰的那些 style.cursor = "not-allowed"
    ("加 Agent·选一个并加入",
     'var cs = Array.prototype.slice.call(document.querySelectorAll("#addsel .chip"));'
     ' var c = cs.filter(function (x) { return x.style.cursor !== "not-allowed"; })[0];'
     ' if (c) { c.click(); } $("addok").click();'
     ' return (c ? "ok:" + c.dataset.m : "no-candidate");'),
    ("切到「任务队列」TAB", '$("tab-queue").click(); return "ok";'),
    ("刷新", '$("refresh").click(); return "ok";'),
]

# 动作序前后都掐掉主轮询：轮询「发什么」已在 A2 轮询轨迹里单独逐行比过，
# 动作序要的是「动作发不发 / 发什么 / 顺序」，掐掉定时器让它不受调度抖动影响。
KILL_TIMER = 'try { if (typeof TIMER !== "undefined" && TIMER) { clearInterval(TIMER); TIMER = null; } } catch (e) {} "ok"'


# ====================================================================== 归一化

NORM_RULES = [
    # (名字, 正则, 替换) —— 两侧同一套代码、同一顺序
    ("next=<PAGE>（401 跳转带回的本页路径；两侧页面文件名天然不同）",
     re.compile(r"next=[^\s&\"]*"), "next=<PAGE>"),
    ("Referer: <referer>（两侧页面 URL 天然不同）",
     re.compile(r"^(Referer|referer):\s.*$", re.M), r"\1: <referer>"),
    ("<page-url>（正文/URL 里出现的本页绝对地址；只匹配 /old/ /new/ 下的 .html 本体）",
     re.compile(r"http://127\.0\.0\.1:\d+/(?:old|new)/[A-Za-z0-9_.-]+\.html"), "<page-url>"),
    ("<port>（桩后端每次运行端口不同；仅用于两次运行间可比，同一次运行两侧端口相同）",
     re.compile(r"127\.0\.0\.1:\d+"), "127.0.0.1:<port>"),
    ("<ts13>（13 位毫秒时戳，防御性规则）", re.compile(r"\b\d{13}\b"), "<ts13>"),
]
NORM_HITS = {}


def norm(text):
    for name, rx, rep in NORM_RULES:
        text, n = rx.subn(rep, text)
        if n:
            NORM_HITS[name] = NORM_HITS.get(name, 0) + n
    return text


def sig(entry):
    """轨迹签名：只看「发什么」—— 方法 / URL / 正文 / credentials / 头。"""
    return norm("%s %s | body=%s | cred=%s | hdr=%s" % (
        entry.get("method"), entry.get("url"), entry.get("body"),
        entry.get("credentials"), entry.get("headers")))


def collapse(entries):
    """连续重复签名压缩成一条（吸收轮询次数差异，保住「发不发 / 发什么 / 顺序」）。"""
    out = []
    for e in entries:
        s = sig(e)
        if out and out[-1] == s:
            continue
        out.append(s)
    return out


def wire_text(rec):
    if rec["where"] == "stub":
        return norm("\n".join([rec["requestline"]] + rec["headers"] + ["", rec["body"]]))
    hdrs = ["%s: %s" % (k, rec["headers"][k]) for k in sorted(rec["headers"])]
    return norm("\n".join([rec["requestline"]] + hdrs + ["", rec["body"]]))


# ====================================================================== 浏览器

def find_browser():
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise RuntimeError("找不到 Edge / Chrome，改不了就别硬跑")


def launch(browser, port, profile):
    args = [browser, "--headless=new", "--remote-debugging-port=%d" % port,
            "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
            "--disable-gpu", "--disable-extensions", "--disable-background-networking",
            "--disable-sync", "--disable-component-update", "--no-sandbox",
            "--window-size=1280,900", "about:blank"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            c.request("GET", "/json/list")
            data = json.loads(c.getresponse().read().decode("utf-8"))
            c.close()
            pages = [t for t in data if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if pages:
                return proc, pages[0]["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("浏览器 DevTools 端口 %d 起不来" % port)


# ====================================================================== 一侧的完整采集

CLIENT_IDENT_RX = re.compile(
    r"^(User-Agent|sec-ch-ua|sec-ch-ua-platform|sec-ch-ua-mobile|Accept-Language):", re.I)


def client_ident(recs):
    """从线级记录里抽出客户端身份头（去重排序）—— 用来自证 UA override 真的生效了。"""
    seen = set()
    for r in recs:
        hdrs = r["headers"] if isinstance(r["headers"], list) else \
            ["%s: %s" % (k, v) for k, v in r["headers"].items()]
        for h in hdrs:
            if CLIENT_IDENT_RX.match(h):
                seen.add(h)
    return sorted(seen)


DOC_REQ_RX = re.compile(r"^/(old|new)/")


def doc_reqs(recs):
    """本侧发生过的「文档本体 + 静态子资源」请求（去重计数排序）。

    用途是刀12 的线级反向证据：NEW 侧必须真的去取过 ``/group.css``，
    OLD 侧必须一次都没取过（它的样式是内联的）。两侧天然不同，故**不做逐行 diff**，
    只对这两条事实各下一个断言。
    """
    cnt = {}
    for r in recs:
        p = r["path"].split("?", 1)[0]
        if p in ROOT_STATIC or DOC_REQ_RX.match(p):
            k = "%s %s" % (r["method"], p)
            cnt[k] = cnt.get(k, 0) + 1
    return ["%s ×%d" % (k, cnt[k]) for k in sorted(cnt)]


def run_side(name, page_url, browser, stub_port, verbose):
    port = free_port()
    profile = tempfile.mkdtemp(prefix="k11_%s_" % name)      # 两侧各自独立 profile，localStorage 天然隔离
    w0 = wire_len()                                          # 本侧线级记录的起点（两侧串行，区间干净）
    proc, ws_url = launch(browser, port, profile)
    res = {"name": name, "url": page_url}
    try:
        cdp = CDP(ws_url)

        # --- 客户端身份钉死：必须排在 Page.navigate / Fetch.enable 之前，
        #     即这条 CDP 会话上发生任何网络活动之前（见 CLIENT_UA 处的注释）---
        cdp.call("Emulation.setUserAgentOverride",
                 userAgent=CLIENT_UA,
                 acceptLanguage=CLIENT_ACCEPT_LANGUAGE,
                 platform="Win32",
                 userAgentMetadata=CLIENT_UA_METADATA)

        # --- Fetch 域拦截：只挂 TSVC，其余请求原样放行走桩后端 ---
        def on_paused(p):
            rid = p["requestId"]
            req = p["request"]
            url = req["url"]
            path = re.sub(r"^https?://[^/]+", "", url) or "/"
            hdrs = {k: v for k, v in (req.get("headers") or {}).items()}
            post = req.get("postData") or ""
            wire_add({"where": "cdp", "requestline": "%s %s HTTP/1.1" % (req["method"], path),
                      "path": path, "method": req["method"], "headers": hdrs, "body": post})
            if req["method"] == "OPTIONS":
                st, body = 204, b""
                rh = [{"name": "Access-Control-Allow-Origin", "value": "*"},
                      {"name": "Access-Control-Allow-Methods", "value": "GET,POST,PATCH,DELETE,OPTIONS"},
                      {"name": "Access-Control-Allow-Headers", "value": "Content-Type"},
                      {"name": "Content-Length", "value": "0"}]
            else:
                st, ct, body = fixture(req["method"], path, post.encode("utf-8"))
                rh = [{"name": "Content-Type", "value": ct},
                      {"name": "Content-Length", "value": str(len(body))},
                      {"name": "Access-Control-Allow-Origin", "value": "*"},
                      {"name": "Cache-Control", "value": "no-store"}]
            cdp.send("Fetch.fulfillRequest", requestId=rid, responseCode=st,
                     responseHeaders=rh, body=base64.b64encode(body).decode())

        def on_dialog(p):
            # createGroup / refresh 的失败分支会 alert()，headless 下不处理会把页面卡住
            cdp.send("Page.handleJavaScriptDialog", accept=True)

        cdp.on_event["Fetch.requestPaused"] = on_paused
        cdp.on_event["Page.javascriptDialogOpening"] = on_dialog

        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Fetch.enable", patterns=[{"urlPattern": "*" + TSVC_HOST + "*"}])
        cdp.call("Page.addScriptToEvaluateOnNewDocument", source=PROBE_JS)

        # ---------- A 启动 ----------
        cdp.call("Page.navigate", url=page_url)
        cdp.pump(SETTLE_BOOT)
        res["boot_trace"] = cdp.evaluate("JSON.stringify(window.__tr)")
        def ident(expr):
            return json.loads(cdp.evaluate(
                "JSON.stringify((function(){try{return (%s);}"
                "catch(e){return '<<throw>> '+e.message;}})())" % expr))

        res["idents"] = [ident(e) for e in IDENT_PROBES]
        # 刀18：下线标识符的一侧一钉（两侧值天然不同，不进两侧 diff）
        res["idents_gone"] = [ident(e) for e, _o, _n in IDENT_GONE]

        # ---------- A3 样式（刀12 新增；取值点在启动 settle 之后、任何动作之前）----------
        res["style_wait"] = json.loads(cdp.evaluate(STYLE_WAIT_JS))
        sh = json.loads(cdp.evaluate(SHEETS_JS, await_promise=False))
        joined = sh.pop("joined")
        sh["total"] = len(joined)
        sh["md5"] = hashlib.md5(joined.encode("utf-8")).hexdigest()
        res["sheets"] = sh
        res["computed"] = json.loads(cdp.evaluate(
            COMPUTED_JS_TMPL % json.dumps(STYLE_PROPS), await_promise=False))

        # ---------- A2 轮询窗口 ----------
        cdp.evaluate("window.__tr.length = 0; 1")
        cdp.pump(SETTLE_POLL)
        res["poll_trace"] = cdp.evaluate("JSON.stringify(window.__tr)")

        # ---------- B 函数级冻结输入 ----------
        # 刀18：解析函数名这一步也放进 try —— NEW 侧 tApi 已下线，裸取会在 try 之外
        # 抛 ReferenceError 让整个 promise reject（装置读不到结果）。包进来之后，
        # 「函数不存在」这件事本身变成一条可比的 outcome：{ok:false,"...is not defined"}。
        cdp.evaluate(
            "window.__probe = async function (kind, method, path, body) {"
            "  var f;"
            "  try { f = (kind === 'api') ? api : tApi; }"
            "  catch (e) { return {ok: false, message: String(e && e.message)}; }"
            "  try { var v = (body === null) ? await f(method, path) : await f(method, path, body);"
            "        return {ok: true, value: JSON.stringify(v)}; }"
            "  catch (e) { return {ok: false, message: String(e && e.message)}; } }; 1")
        frozen = []
        for kind, method, path, body, _mode in FROZEN:
            a = wire_len()
            # Runtime.evaluate 不是模块，顶层 await 是语法错 → 交给 awaitPromise 去 await
            out = json.loads(cdp.evaluate(
                "window.__probe(%s, %s, %s, %s).then(function (r) { return JSON.stringify(r); })"
                % (json.dumps(kind), json.dumps(method), json.dumps(path),
                   "null" if body is None else json.dumps(body))))
            cdp.pump(0.25)                      # 让 401 分支的 /login 导航请求落到桩上
            b = wire_len()
            # 本探针窗口里，路径正好等于被测路径的那些线级记录（轮询恰好插进来也不会被误收）
            recs = [r for r in wire_slice(a, b)
                    if r["path"].split("?", 1)[0] in (path, "/login")]
            frozen.append({"call": "%s(%r, %r%s)" % (kind, method, path,
                                                     "" if body is None else ", " + json.dumps(body)),
                           "outcome": out,
                           "wire": [wire_text(r) for r in recs]})
        res["frozen"] = frozen

        # ---------- C 动作序 ----------
        cdp.evaluate("window.__tr.length = 0; window.__errs.length = 0; 1")
        cdp.evaluate(KILL_TIMER)
        steps = []
        for label, js in ACTIONS:
            cdp.evaluate(KILL_TIMER)
            r = cdp.evaluate("(function(){ try { return (function(){ %s })(); }"
                             " catch (e) { return '<<throw>> ' + e.message; } })()" % js,
                             await_promise=False)
            cdp.pump(SETTLE_STEP)
            cdp.evaluate(KILL_TIMER)
            steps.append("%s -> %s" % (label, r))
        res["action_steps"] = steps
        res["action_trace"] = cdp.evaluate("JSON.stringify(window.__tr)")
        res["errs"] = json.loads(cdp.evaluate("JSON.stringify(window.__errs)"))
        mine = wire_slice(w0, wire_len())
        res["client_ident"] = client_ident(mine)
        res["doc_reqs"] = doc_reqs(mine)
        # 刀18 追加的反向证据：整侧全程（启动 + 轮询 + 函数级 + 动作序）打到 TSVC
        # (:9981) 的请求一条都不许有。where=="cdp" 的记录只可能来自 Fetch 域拦截，
        # 而拦截只登记了 urlPattern=*TSVC_HOST* —— 所以这个列表非空就等于「还在出网」。
        res["tsvc_hits"] = ["%s %s" % (r["method"], r["path"])
                            for r in mine if r["where"] == "cdp"]
        cdp.close()
    finally:
        proc.kill()
        proc.wait(timeout=20)
        shutil.rmtree(profile, ignore_errors=True)
    return res


# ====================================================================== 文本级

# 刀11 立、刀18 **换基**（不是放宽）：`group_transport.js` 至今仍逐字节等于 k11 快照
# 的那几段。刀18 把任务面板子树下线后，B3（`const TSVC`）与 B5（`async function tApi`）
# 在前端已零引用 → 两段随之从传输件删除，锚从「5 段纯拼接」换成「**剩余 3 段**纯拼接
# 且尾部残余 0 B」——**同强度**：每段仍逐字节对 k11 快照，拼接仍必须**恰好**铺满整件。
# 换基同时**加严**：下线的两段另立 BLOCKS_DROPPED，逐段做**反向断言**（那些字节在
# 新件里一个都不许出现）。判据总数只增不减：3 段正向 + 2 段反向 + 尾部残余 + 残留扫描。
BLOCKS = [("B1", 555, 555), ("B2", 560, 586), ("B4", 1469, 1469)]

# 刀18 下线（NEW 侧必须一个字节都不剩）—— 行号仍是 k11 快照里的原位置。
BLOCKS_DROPPED = [("B3", 1468, 1468), ("B5", 1475, 1486)]


STYLE_OPEN = b"<style>\n"
STYLE_CLOSE = b"</style>\n"
LINK_LINE = b'<link rel="stylesheet" href="/group.css">\n'
SCRIPT_OPEN = b"<script>\n"
SCRIPT_CLOSE = b"</script>\n"
VIEW_LINE = b'<script src="/group_view.js"></script>\n'

# 刀13 立、刀18 **换基**（不是放宽）：`group_view.js` 原本 == k13 快照那个唯一内联
# <script> 块的**全部**字节（1568 行）。刀18 证明「任务清单面板子树」运行期不可达
# （`#taskpanel` 不在 DOM 里、`tOpen`/`tLoad` 无调用方）并整棵删除后，锚从「整块相等」
# 换成「**剩余分块纯拼接** 且尾部残余 0 B」——与 3.1 JS 文本级同一套路、**同强度**：
# 每块仍逐字节对 k13 冻结快照，拼接仍必须**恰好**铺满整件，一个字节的私货都塞不进来。
# 下线的两段另立 VIEW_DROPPED 做**反向断言**（字节不许出现），另加字节账与源码残留扫描。
# 行号 = k13 内联块内的 1-based 行号（该块逐字节 == 刀18 之前的 group_view.js）。
VIEW_BLOCKS = [
    ("V1", 1, 902),        # 渲染壳 + 业务视图主体，直到 boot() 那行之后的空行
    ("V2", 907, 908),      # #432-3 注释 + function tgid()  —— 仍有活调用方（tqLoad/tqClear）
    ("V3", 1030, 1568),    # 左栏 TAB（#432-3/#448 任务队列，走 9980 同源台账）起，到文件末尾
]
VIEW_DROPPED = [
    ("D1", 903, 906),      # 面板段头注释 + const TSTATUS + let TPOLL
    ("D2", 909, 1029),     # tErrShow/tErrHide/tLoad/tRow/tRender/tAdd/tOpen
                           #   + tp-addbtn/tp-title 守卫绑定 + pinTaskpanelBottom IIFE
]
# 刀18 反向断言用：删干净之后，这些字面量在 group_view.js 里一处都不许再出现。
# 注：不收 "tp-" —— V3 里 #452-1 那段注释仍如实提到 HTML 上写过 #tp-title/#tp-owner，
# 那是**历史说明**不是活引用；反向断言只钉「活引用写法」（调用式 / 元素 id / 常量名）。
VIEW_RESIDUE = [b"tApi(", b"TSVC", b"taskpanel", b"tOpen(", b"tLoad(",
                b"TSTATUS", b"TPOLL", b'$("tp-', b'#taskpanel']


def js_view_text_level(old_html):
    """刀13 第 0b 类判据：业务视图字节同源 + 新 group.html 的字节账 + 重组互补证据。

    与刀12 的第 0 类 CSS 文本级**同构**，只是把「内联 ``<style>`` → 外链 css」
    换成「内联 ``<script>`` → 外链 js」。三件事：

    1. 旧快照里那个**唯一的内联 script 块**的内容（``<script>\\n`` 之后 至
       ``</script>`` 之前）与 ``agent-team/ui/group_view.js`` 逐字节同源 ——
       刀18 起换基为 **``VIEW_BLOCKS`` 保留块纯拼接 + 尾部残余 0 B**，另加
       ``VIEW_DROPPED`` 的反向断言、字节账与源码残留扫描（同强度，见常量处注释）；
    2. 新 ``group.html`` 的字节账：总字节 = 旧 − 整块 + 39、内联 ``<script>`` 计 0、
       ``VIEW_LINE`` 计 1 且落在**原内联块同一 offset**（文档序不变 = 执行序不变）；
    3. **重组互补证据**：把新件那 39 B 的外链行换回原整块 → 与旧快照逐字节相等，
       证明「只换了这一处」。（2/3 两条与刀18 无关：``group.html`` 本刀一字未动。）
    """
    print("---- 3.0b JS 文本级·业务视图（刀13 立 / 刀18 换基）：外链件 == 旧快照内联 script 块的保留分块，逐字节 ----")
    old = open(old_html, "rb").read()
    new = open(NEW_HTML, "rb").read()
    view = open(NEW_VIEW, "rb").read()

    a = old.rfind(SCRIPT_OPEN)          # 裸 <script>（不带 src）—— 文档里只该有这一个
    if a < 0:
        print("  ✗ 旧快照里找不到内联 <script>\\n 块")
        return False
    b = old.find(b"</script>", a)
    if b < 0:
        print("  ✗ 旧快照里 <script>@%d 之后找不到 </script>" % a)
        return False
    ia, ib = a + len(SCRIPT_OPEN), b
    inner = old[ia:ib]
    block = old[a:b + len(SCRIPT_CLOSE)]
    ok = True

    print("  旧快照 %s" % old_html)
    print("    内联 <script> 块数 %d（应 1）｜整块 offset [%d, %d)  共 %d B"
          % (old.count(SCRIPT_OPEN), a, a + len(block), len(block)))
    print("    脚本内容       offset [%d, %d)  %6d B  md5 %s  换行 %d 个"
          % (ia, ib, len(inner), hashlib.md5(inner).hexdigest(), inner.count(b"\n")))
    ok &= old.count(SCRIPT_OPEN) == 1
    ok &= old[b:b + len(SCRIPT_CLOSE)] == SCRIPT_CLOSE
    print("  新件   %s" % NEW_VIEW)
    print("    文件内容                        %6d B  md5 %s  换行 %d 个"
          % (len(view), hashlib.md5(view).hexdigest(), view.count(b"\n")))

    # --- 刀18 换基：整块相等 → 剩余分块纯拼接（同强度，见 VIEW_BLOCKS 处的注释）---
    ilines = inner.split(b"\n")
    if ilines[-1] != b"":
        print("  ✗ k13 内联块不是以换行收尾，分块锚无法成立")
        return False
    ilines = [x + b"\n" for x in ilines[:-1]]
    nline = len(ilines)

    def vseg(x, y):                      # 注意别用 a/b 当循环变量：外层 a/b 是 <script> 的 offset
        return b"".join(ilines[x - 1:y])

    print("    分块锚：k13 内联块共 %d 行 ｜ 保留 %d 块 / 下线 %d 块"
          % (nline, len(VIEW_BLOCKS), len(VIEW_DROPPED)))
    span_ok = (VIEW_BLOCKS[-1][2] == nline)
    off = 0
    for nm, la, lb in VIEW_BLOCKS:
        seg = vseg(la, lb)
        got = view[off:off + len(seg)]
        eq1 = got == seg
        ok &= eq1
        print("      %s k13 内联块行 %4d-%-4d %6d B → 新件偏移 %6d  字节相等: %s"
              % (nm, la, lb, len(seg), off, eq1))
        off += len(seg)
    tail = view[off:]
    print("      新件尾部残余 %d B（应为 0，证明新件 = 保留块纯拼接、无任何私货）: %r"
          % (len(tail), tail[:40]))
    ok &= len(tail) == 0 and span_ok
    print("      末块右端 %d == 内联块行数 %d（保留块铺到文件末尾）: %s"
          % (VIEW_BLOCKS[-1][2], nline, span_ok))

    # 反向断言：下线的两块在新件里一个字节都不剩
    dropped_bytes = 0
    for nm, la, lb in VIEW_DROPPED:
        seg = vseg(la, lb)
        dropped_bytes += len(seg)
        gone = seg not in view
        ok &= gone
        print("      %s k13 内联块行 %4d-%-4d %6d B → 新件里已无此段: %s ｜ 首个非空行 %r"
              % (nm, la, lb, len(seg), gone,
                 next((x for x in seg.split(b"\n") if x.strip()), b"")[:48]))
    acct = len(view) + dropped_bytes == len(inner)
    ok &= acct
    print("      字节账：新件 %d B + 下线 %d B = %d B ｜ == k13 内联块 %d B: %s"
          % (len(view), dropped_bytes, len(view) + dropped_bytes, len(inner), acct))
    # 反向断言（刀18 契约要求的那条）：源码里不许再出现任务面板的活引用写法
    vres = [p for p in VIEW_RESIDUE if p in view]
    ok &= not vres
    print("      源码残留扫描 %s（应为空）: %s"
          % ([p.decode() for p in VIEW_RESIDUE], [p.decode() for p in vres]))

    # 忠实性守则（刀11 立、刀12/13 沿用）：原样搬运、不包 IIFE、不加 "use strict"、不加 BOM
    bom = view[:3] == b"\xef\xbb\xbf"
    head_ok = view[:1] == b"\n"
    print("    首 40 B: %r" % view[:40])
    print("    末 40 B: %r" % view[-40:])
    print("    忠实性：无 BOM %s ｜ 首字节仍是换行 %s ｜ 末字节仍是换行 %s"
          % (not bom, head_ok, view[-1:] == b"\n"))
    ok &= (not bom) and head_ok and view[-1:] == b"\n"

    # --- 新 group.html 的字节账 ---
    n_inline = new.count(SCRIPT_OPEN)
    n_view = new.count(VIEW_LINE)
    n_close = new.count(b"</script>")
    n_src = new.count(b"<script src=")
    at = new.find(VIEW_LINE)
    print("  新件   %s" % NEW_HTML)
    print("    字节数 %d  md5 %s" % (len(new), hashlib.md5(new).hexdigest()))
    print("    内联 <script> 计数 %d（应 0）｜ 外链行 %d B @ offset %d（旧内联块也在 %d）｜ 该行计数 %d（应 1）"
          % (n_inline, len(VIEW_LINE), at, a, n_view))
    # 注：</script> 计数**不减**（6 → 6）。外链 script 不是空元素，`<script src=...></script>`
    # 自带一个闭标签 —— 这点与刀12 的 <link>（空元素，</style> 1 → 0）不同，不要照搬那个算法。
    print("    </script> 计数 %d（应 6：5 个原外链 + 1 个新外链，外链 script 非空元素，闭标签不减）"
          % n_close)
    print("    <script src= 计数 %d（应 6）" % n_src)
    ok &= (n_inline == 0 and n_view == 1 and at == a and n_close == 6 and n_src == 6)
    # 位置不变 = 执行序不变：仍紧跟 /group_transport.js、仍在 </body> 之前
    prev = b'<script src="/group_transport.js"></script>\n'
    prev_ok = new[at - len(prev):at] == prev
    body_at = new.find(b"</body>")
    tail_ok = at < body_at            # 外链行整体落在 </body> 之前（行尾恰好紧贴它）
    print("    外链行紧接 /group_transport.js 之后: %s ｜ 落在 </body>(@%d) 之前: %s（行尾 @%d）"
          % (prev_ok, body_at, tail_ok, at + len(VIEW_LINE)))
    ok &= prev_ok and tail_ok
    print("    字节账 %d − %d + %d = %d ｜ 与实测 %d 相符: %s"
          % (len(old), len(block), len(VIEW_LINE),
             len(old) - len(block) + len(VIEW_LINE), len(new),
             len(old) - len(block) + len(VIEW_LINE) == len(new)))

    # --- 重组互补证据：把那 39 B 换回 74877 B 的整块 → 应与旧快照逐字节相等 ---
    old_md5 = hashlib.md5(old).hexdigest()
    recon = new[:at] + block + new[at + len(VIEW_LINE):]
    same = recon == old
    print("    重组回填（新件的外链行 ← 原内联块）→ %d B  md5 %s ｜ 与旧快照逐字节相等: %s"
          % (len(recon), hashlib.md5(recon).hexdigest(), same))
    if old_md5 == K13_MD5:
        ok &= same and len(old) - len(block) + len(VIEW_LINE) == len(new)
        print("      （旧侧是刀13 基线快照 md5 %s → 这条是**硬判据**：两侧唯一差异就是这一处）"
              % K13_MD5[:8])
    else:
        print("      （旧侧 md5 %s 不是刀13 基线 %s → 两侧差的不止内联脚本这一处，"
              % (old_md5[:8], K13_MD5[:8]))
        print("        重组互补证据**对该旧侧不适用**，不计入判定；JS 字节同源那条照样是硬判据）")
    print("  JS 文本级·业务视图结论: %s" % ("一致" if ok else "不一致"))
    return ok


# 刀12 立、刀19 **换基**（不是放宽）：`group.css` 原本 == k12 快照那个唯一内联
# <style> 块的**全部**字节（31078 B）。刀18 已把「任务清单面板」子树从 group_view.js
# 整棵下线（`#taskpanel` 不在 DOM 里、group.html 里无该容器、JS 零引用），本刀据此
# 证明那段样式是**死规则**（选择器在活 DOM 上一个都匹配不到）并整段删除；锚从
# 「整块相等」换成「**剩余分块纯拼接** 且尾部残余 0 B」——与 3.0b JS 文本级同一
# 套路、**同强度**：保留块仍逐字节对 k12 冻结快照，拼接仍必须**恰好**铺满整件，
# 一个字节的私货都塞不进来。下线的两段另立 CSS_DROPPED 做**反向断言**（字节不许
# 出现）、字节账、交错重组（两段账都平）与残留扫描。
# 行号 = k12 内联 style 块内的 1-based 行号（该块逐字节 == 刀19 之前的 group.css）。
# 第 4 项为行内子串：那行 @media 里只有 `#taskpanel{width:94vw}` 是死的，同一行的
# `header button{padding:6px 9px}` 仍作用于活元素，**不动**——所以按字节切片而非整行。
CSS_DROPPED = [
    ("S1", 328, 369, None),                      # 面板段头注释 + #taskbtn/#taskpanel/#tp-*/.titem*
    ("S2", 370, 370, b"#taskpanel{width:94vw}"),  # 窄屏 @media 里的面板宽度（行内子串）
]
# 刀19 反向断言用：删干净之后，这些字面量在 group.css 里一处都不许再出现。
CSS_RESIDUE = [b"#taskpanel", b"#tp-", b".titem", b"#taskbtn"]


def css_text_level():
    """刀12 第 0 类判据：CSS 字节同源 + 字节账 + 重组互补证据（刀13 起改成**常驻回归**）。

    为什么换锚：这条判据问的是「``group.css`` 至今仍逐字节等于刀12 抽走的那段内联
    ``<style>``，且当时那一换**只**换了这一处」。刀12 当时 ``--old`` 就是 k12 快照、
    新侧就是刚改完的活件，两者相邻，所以直接拿 ``--old`` 和活件比。刀13 又动了
    ``group.html``（抽内联脚本），活件与 k12 之间隔了**两步**，再拿 ``--old``
    （现在默认 k13 快照）去找 ``<style>`` 段只会找不到 —— 那不是判据失效，是锚错位。
    所以把它钉死成 **K12_HTML ↔ K13_HTML** 这对冻结快照（与 text_level() 钉 K11_HTML
    同一套路），**期望数字一个没下调**：31078 B / 31095 B / 42 B / offset 173 /
    ``</style>``=0 / ``<link>``=1 / 110833 B，全部照旧。
    活件也不脱钩：末尾多加一条**全链重组** —— 从活的 ``group.html`` 两步走回 k12 快照。

    刀19 **换基**：``group.css`` 不再整块等于那段内联 ``<style>``（任务面板死样式已
    删除），改成 **``CSS_DROPPED`` 之外的保留块纯拼接 + 尾部残余 0 B**，另加反向断言、
    字节账、交错重组与残留扫描（同强度，见 ``CSS_DROPPED`` 处的注释）。
    字节账那几条**期望数字一个没下调**：31078 B / 31095 B / 42 B / offset 173 /
    ``</style>``=0 / ``<link>``=1 / 110833 B 全部照旧 —— 它们问的是 ``group.html``
    那一换，本刀一个字节都没碰 ``group.html``。
    """
    print("---- 3.0 CSS 文本级（刀12 立 / 刀19 换基，锚定 %s ↔ %s）：外链件 == 内联 style 段的保留分块，逐字节 ----"
          % (os.path.basename(K12_HTML), os.path.basename(K13_HTML)))
    old = open(K12_HTML, "rb").read()
    new = open(K13_HTML, "rb").read()
    css = open(NEW_CSS, "rb").read()
    old_html = K12_HTML

    a = old.find(STYLE_OPEN)
    b = old.find(b"</style>")
    if a < 0 or b < 0:
        print("  ✗ 旧快照里找不到 <style>\\n … </style> 段（找到 %d / %d）" % (a, b))
        return False
    ia, ib = a + len(STYLE_OPEN), b
    inner = old[ia:ib]
    block = old[a:b + len(STYLE_CLOSE)]
    ok = True

    print("  旧快照 %s" % old_html)
    print("    <style> 段     offset [%d, %d)  共 %d B" % (a, a + len(block), len(block)))
    print("    样式内容       offset [%d, %d)  %6d B  md5 %s"
          % (ia, ib, len(inner), hashlib.md5(inner).hexdigest()))
    print("  新件   %s" % NEW_CSS)
    print("    文件内容                        %6d B  md5 %s  换行 %d 个"
          % (len(css), hashlib.md5(css).hexdigest(), css.count(b"\n")))

    # --- 刀19 换基：整块相等 → 剩余分块纯拼接（同强度，见 CSS_DROPPED 处的注释）---
    ilines = inner.split(b"\n")
    if ilines[-1] != b"":
        print("  ✗ k12 内联 style 块不是以换行收尾，分块锚无法成立")
        return False
    ilines = [x + b"\n" for x in ilines[:-1]]
    nline = len(ilines)
    coff = [0]
    for line in ilines:
        coff.append(coff[-1] + len(line))

    # 把「行区间 / 行内子串」折算成 k12 内联块里的字节区间 [s, e)，并校验严格递增不重叠
    spans, prev = [], 0
    for nm, la, lb, sub in CSS_DROPPED:
        s, e = coff[la - 1], coff[lb]
        if sub is not None:
            one = inner[s:e]
            if one.count(sub) != 1:
                print("  ✗ %s：k12 内联块行 %d 里 %r 出现 %d 次（应 1）"
                      % (nm, la, sub, one.count(sub)))
                return False
            s, e = s + one.index(sub), s + one.index(sub) + len(sub)
        if not prev <= s < e:
            print("  ✗ %s：下线段不是严格递增 / 不重叠（prev %d ｜ [%d, %d)）" % (nm, prev, s, e))
            return False
        spans.append((nm, la, lb, s, e))
        prev = e

    print("    分块锚：k12 内联块共 %d 行 %d B ｜ 下线 %d 段 → 保留 %d 块"
          % (nline, len(inner), len(spans), len(spans) + 1))
    off, cur = 0, 0                      # off = 新件写头 ｜ cur = 旧块读头
    for i, (_nm, _la, _lb, s, e) in enumerate(
            spans + [(None, 0, 0, len(inner), len(inner))]):
        seg = inner[cur:s]
        got = css[off:off + len(seg)]
        eq1 = got == seg
        ok &= eq1
        print("      K%d k12 内联块字节 [%6d,%6d) %6d B → 新件偏移 %6d  字节相等: %s"
              % (i + 1, cur, s, len(seg), off, eq1))
        off += len(seg)
        cur = e
    tail = css[off:]
    print("      新件尾部残余 %d B（应为 0，证明新件 = 保留块纯拼接、无任何私货）: %r"
          % (len(tail), tail[:40]))
    ok &= len(tail) == 0

    # 反向断言：下线的两段在新件里一个字节都不剩
    dropped_bytes = 0
    for nm, la, lb, s, e in spans:
        seg = inner[s:e]
        dropped_bytes += len(seg)
        gone = seg not in css
        ok &= gone
        print("      %s k12 内联块行 %3d-%-3d 字节 [%6d,%6d) %5d B → 新件里已无此段: %s ｜ 首个非空行 %r"
              % (nm, la, lb, s, e, len(seg), gone,
                 next((x for x in seg.split(b"\n") if x.strip()), b"")[:48]))
    acct = len(css) + dropped_bytes == len(inner)
    ok &= acct
    print("      字节账：新件 %d B + 下线 %d B = %d B ｜ == k12 内联块 %d B: %s"
          % (len(css), dropped_bytes, len(css) + dropped_bytes, len(inner), acct))

    # 交错重组：保留块与下线段按文档序纯拼接 → 逐字节还原 k12 内联块（两段账都平）
    recon, cur = [], 0
    for _nm, _la, _lb, s, e in spans:
        recon.append(inner[cur:s])
        recon.append(inner[s:e])
        cur = e
    recon.append(inner[cur:])
    inter = b"".join(recon)
    inter_ok = inter == inner
    ok &= inter_ok
    print("      交错重组（保留块 + 下线段，文档序）→ %d B ｜ == k12 内联块逐字节: %s"
          % (len(inter), inter_ok))

    # 反向断言（刀19 契约要求的那条）：新件里不许再出现任务面板的选择器字面量
    cres = [p for p in CSS_RESIDUE if p in css]
    ok &= not cres
    print("      源码残留扫描 %s（应为空）: %s"
          % ([p.decode() for p in CSS_RESIDUE], [p.decode() for p in cres]))
    # 正向配对：下线段确实是面板段（防「删错段也能拼平」）——每段都得命中面板字面量
    for nm, _la, _lb, s, e in spans:
        hit = [p.decode() for p in CSS_RESIDUE if p in inner[s:e]]
        ok &= bool(hit)
        print("      %s 段内命中面板字面量 %s（非空 = 删掉的确实是任务面板那段）" % (nm, hit))

    print("    首 40 B: %r" % css[:40])
    print("    末 40 B: %r" % css[-40:])

    # --- 刀12 收口后那一版 group.html（= k13 快照）的字节账 ---
    n_close = new.count(b"</style>")
    n_link = new.count(b"<link")
    at = new.find(LINK_LINE)
    print("  刀12 收口件 %s（= 刀13 开工前的活件）" % K13_HTML)
    print("    字节数 %d  md5 %s" % (len(new), hashlib.md5(new).hexdigest()))
    print("    </style> 计数 %d（应 0）｜ <link 计数 %d（应 1）｜ <link> 行 %d B @ offset %d（旧 <style> 也在 %d）"
          % (n_close, n_link, len(LINK_LINE), at, a))
    ok &= (n_close == 0 and n_link == 1 and at == a)
    # 位置不变 = 层叠顺序不变：<link> 仍夹在 </title> 与 </head> 之间
    pos_ok = new.find(b"</title>") < at < new.find(b"</head>")
    print("    <link> 仍在 </title>(@%d) 之后、</head>(@%d) 之前 → 层叠顺序同序: %s"
          % (new.find(b"</title>"), new.find(b"</head>"), pos_ok))
    ok &= pos_ok
    print("    字节账 %d − %d + %d = %d ｜ 与实测 %d 相符: %s"
          % (len(old), len(block), len(LINK_LINE),
             len(old) - len(block) + len(LINK_LINE), len(new),
             len(old) - len(block) + len(LINK_LINE) == len(new)))

    # --- 重组互补证据：把那 42 B 换回 31095 B 的 style 段 → 应与旧快照逐字节相等 ---
    old_md5 = hashlib.md5(old).hexdigest()
    recon = new[:at] + block + new[at + len(LINK_LINE):]
    same = recon == old
    print("    重组回填（新件的 <link> 行 ← 原 style 段）→ %d B ｜ 与旧快照逐字节相等: %s"
          % (len(recon), same))
    if old_md5 == K12_MD5:
        ok &= same and len(old) - len(block) + len(LINK_LINE) == len(new)
        print("      （旧侧是刀12 基线快照 md5 %s → 这条是**硬判据**：两侧唯一差异就是这一处）"
              % K12_MD5[:8])
    else:
        print("      （旧侧 md5 %s 不是刀12 基线 %s → 两侧差的不止 style 段这一处，"
              % (old_md5[:8], K12_MD5[:8]))
        print("        重组互补证据**对该旧侧不适用**，不计入判定；CSS 字节同源那条照样是硬判据）")

    # --- 全链重组（刀13 追加）：活件不因为换锚而脱钩 ---
    # 活 group.html --[39 B 外链行 ← 74877 B 内联块]--> k13 快照 --[42 B <link> 行 ← 31095 B style 段]--> k12 快照
    live = open(NEW_HTML, "rb").read()
    k13 = new
    j = k13.rfind(SCRIPT_OPEN)
    k = k13.find(b"</script>", j)
    vat = live.find(VIEW_LINE)
    chain_ok = False
    if j >= 0 and k >= 0 and vat >= 0:
        step1 = live[:vat] + k13[j:k + len(SCRIPT_CLOSE)] + live[vat + len(VIEW_LINE):]
        step2 = step1[:at] + block + step1[at + len(LINK_LINE):]
        chain_ok = step1 == k13 and step2 == old
        print("  全链重组：活件 %d B --[脚本块回填]--> %d B（== k13 快照: %s）--[style 段回填]--> %d B（== k12 快照: %s）"
              % (len(live), len(step1), step1 == k13, len(step2), step2 == old))
        print("            md5 链 %s → %s → %s"
              % (hashlib.md5(live).hexdigest()[:8], hashlib.md5(step1).hexdigest()[:8],
                 hashlib.md5(step2).hexdigest()[:8]))
    else:
        print("  ✗ 全链重组取不到锚点（脚本块 @%d/%d ｜ 外链行 @%d）" % (j, k, vat))
    print("    → 从**活件**两步走回刀12 基线，逐字节相等: %s（证明这两刀合起来只换了这两处）"
          % chain_ok)
    ok &= chain_ok
    print("  CSS 文本级结论: %s" % ("一致" if ok else "不一致"))
    return ok


def text_level():
    """证明「零逻辑改动」：新件里的每一段 == 刀11 快照同段的那几行，逐字节。

    这条判据钉死在 ``group_old_k11.html``（而不是 ``--old`` 给的那个旧侧）——
    它问的是「刀11 抽出的 group_transport.js 至今仍逐字节等于当初那几段」，
    是一条**常驻回归**，与本次比的是哪个旧侧无关。

    刀18 换基（见 ``BLOCKS`` 处的注释）：正向段 5 → 3，下线的 B3/B5 转为**反向断言**，
    再加一条「字节账」——剩余 3 段 + 下线 2 段 == k11 快照里那 5 段的原始总字节。
    """
    old = open(K11_HTML, "rb").read().split(b"\n")
    newjs = open(NEW_JS, "rb").read()

    def k11seg(a, b):
        return b"".join(old[i - 1] + b"\n" for i in range(a, b + 1))

    print("---- 3.1 JS 文本级（刀11 常驻回归，锚定 %s）：%d 段逐段字节相等 ----"
          % (os.path.basename(K11_HTML), len(BLOCKS)))
    off, ok = 0, True
    for nm, a, b in BLOCKS:
        seg = k11seg(a, b)
        got = newjs[off:off + len(seg)]
        eq = got == seg
        ok &= eq
        print("  %s 旧快照行 %4d-%-4d %5d B → 新件偏移 %5d  字节相等: %s" % (nm, a, b, len(seg), off, eq))
        off += len(seg)
    tail = newjs[off:]
    print("  新件尾部残余 %d B（应为 0，证明新件 = %d 段纯拼接、无任何包装）: %s"
          % (len(tail), len(BLOCKS), tail[:40]))
    ok &= len(tail) == 0

    # --- 刀18 反向断言：下线的两段在新件里一个字节都不许出现 ---
    print("  —— 刀18 下线段的反向断言（任务面板子树零引用后，传输面这两段随之下线）——")
    dropped_bytes = 0
    for nm, a, b in BLOCKS_DROPPED:
        seg = k11seg(a, b)
        dropped_bytes += len(seg)
        gone = seg not in newjs
        ok &= gone
        print("    %s 旧快照行 %4d-%-4d %5d B → 新件里已无此段: %s ｜ 首行 %r"
              % (nm, a, b, len(seg), gone, seg.split(b"\n")[0][:52]))
    total = len(newjs) + dropped_bytes
    orig = sum(len(k11seg(a, b)) for _, a, b in BLOCKS) + dropped_bytes
    print("    字节账：新件 %d B + 下线 %d B = %d B ｜ 与刀11 原 5 段总字节 %d 相符: %s"
          % (len(newjs), dropped_bytes, total, orig, total == orig))
    ok &= total == orig

    cur = open(NEW_HTML, "rb").read()
    residue = [p for p in (b"fetch(", b"const API = ", b"const TSVC = ",
                           b"const TGROUP_DEFAULT = ", b"async function api(",
                           b"async function tApi(") if p in cur]
    print("  group.html 残留（应为空）: %s" % residue)
    ok &= not residue
    # 刀18 追加：传输件自身也不许再出现被下线的两个标识符（含调用点写法）
    tres = [p for p in (b"TSVC", b"tApi") if p in newjs]
    print("  group_transport.js 残留 TSVC/tApi（应为空）: %s" % tres)
    ok &= not tres
    print("  JS 文本级结论: %s" % ("一致" if ok else "不一致"))
    return ok


# ====================================================================== 比对

def diff_lines(tag, a, b, verbose):
    if a == b:
        print("  ✓ %-16s 两侧逐行一致（%d 条）" % (tag, len(a)))
        if verbose:
            for i, x in enumerate(a):
                print("        [%02d] %s" % (i, x))
        return 0
    n = 0
    print("  ✗ %-16s 不一致（旧 %d 条 / 新 %d 条）" % (tag, len(a), len(b)))
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else "<缺>"
        y = b[i] if i < len(b) else "<缺>"
        if x != y:
            n += 1
            print("      [%02d] OLD: %s" % (i, x))
            print("      [%02d] NEW: %s" % (i, y))
    print("      差异行数: %d" % n)
    return n


def run_once(args):
    old_html = os.path.abspath(args.old)

    print("=" * 78)
    print("刀11/刀12/刀13/刀18 · 群聊前端拆三件套 —— 等价装置")
    print("=" * 78)

    for p in (old_html, NEW_HTML, NEW_JS, NEW_CSS, NEW_VIEW, K11_HTML, K12_HTML, K13_HTML):
        if not os.path.isfile(p):
            print("缺件: " + p)
            return 1

    def md5(p):
        return hashlib.md5(open(p, "rb").read()).hexdigest()

    old_md5, new_md5 = md5(old_html), md5(NEW_HTML)
    js_md5, css_md5, view_md5 = md5(NEW_JS), md5(NEW_CSS), md5(NEW_VIEW)
    old_stat = (os.path.getsize(old_html), old_md5, os.path.getmtime(old_html))
    print("---- 装置自证：两侧来自两个不同的物理文件 ----")
    print("  OLD html  %s  %8d B  md5 %s" % (old_html, os.path.getsize(old_html), old_md5))
    print("  NEW html  %s  %8d B  md5 %s" % (NEW_HTML, os.path.getsize(NEW_HTML), new_md5))
    print("  NEW js    %s  %8d B  md5 %s" % (NEW_JS, os.path.getsize(NEW_JS), js_md5))
    print("  NEW css   %s  %8d B  md5 %s" % (NEW_CSS, os.path.getsize(NEW_CSS), css_md5))
    print("  NEW view  %s  %8d B  md5 %s" % (NEW_VIEW, os.path.getsize(NEW_VIEW), view_md5))
    print("  刀11 锚   %s  %8d B  md5 %s" % (K11_HTML, os.path.getsize(K11_HTML), md5(K11_HTML)))
    print("  刀12 锚   %s  %8d B  md5 %s" % (K12_HTML, os.path.getsize(K12_HTML), md5(K12_HTML)))
    pin_ok = (not args.old_md5) or old_md5.startswith(args.old_md5)
    self_ok = (new_md5 != old_md5) and pin_ok
    print("  新侧 md5 != 旧侧: %s ｜ --old-md5 前缀校验: %s ｜ 不是自比: %s"
          % (new_md5 != old_md5,
             ("%s（要求前缀 %s）" % (pin_ok, args.old_md5)) if args.old_md5 else "未指定，不校验具体值",
             self_ok))
    if not self_ok:
        print("装置自证不过（旧侧不是开工前快照，或两侧同一个文件）→ 停")
        return 1

    # 旧侧走的是 k13 快照这条默认路 → 额外硬校验它就是那份冻结字节（防有人拿改过的件当旧侧）。
    # 传 k12/k11 快照时这条不适用、不报错（与 css/js 文本级里 K12_MD5/K13_MD5 分支同构）。
    is_k13 = (os.path.abspath(old_html) == os.path.abspath(K13_HTML))
    if is_k13 or old_md5 == K13_MD5:
        k13_pin = old_md5 == K13_MD5
        print("  旧侧是刀13 基线快照 → 硬校验 md5 == %s: %s" % (K13_MD5, k13_pin))
        if not k13_pin:
            print("    ✗ 旧侧字节被动过（实测 %s）→ 停" % old_md5)
            return 1
    else:
        print("  旧侧不是刀13 基线快照（md5 %s ≠ %s）→ 该项硬校验不适用，不报错"
              % (old_md5[:8], K13_MD5[:8]))

    view_ok = js_view_text_level(old_html)
    css_ok = css_text_level()
    txt_ok = text_level()

    browser = find_browser()
    STATIC_ROOTS["old"] = os.path.dirname(old_html)      # 旧侧页面从它自己所在目录发
    stub_port = free_port()
    srv = QuietServer(("127.0.0.1", stub_port), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % stub_port
    old_url = base + "/old/" + os.path.basename(old_html)
    new_url = base + "/new/group.html"
    print("---- 桩后端 ----")
    print("  浏览器      : %s" % browser)
    print("  桩监听      : %s" % base)
    print("  OLD 页面 URL: %s" % old_url)
    print("  NEW 页面 URL: %s" % new_url)
    print("  TSVC 拦截   : CDP Fetch 域 urlPattern=*%s* → Fetch.fulfillRequest 合成应答"
          % TSVC_HOST)
    print("  UA 钉死     : %s" % CLIENT_UA)
    print("  UA-CH 钉死  : brands=%s ｜ platform=%s ｜ mobile=%s ｜ 语言表=%s"
          % (json.dumps(CLIENT_UA_BRANDS, ensure_ascii=False, sort_keys=True),
             CLIENT_UA_METADATA["platform"], CLIENT_UA_METADATA["mobile"],
             CLIENT_ACCEPT_LANGUAGE))
    print("  （客户端构建号不是被测行为：两侧各 launch 一次浏览器，Edge 自动升级会让两侧")
    print("    抓到不同构建。钉死后 User-Agent / sec-ch-ua / sec-ch-ua-platform /")
    print("    sec-ch-ua-mobile 由同一份常量同源生成；其余线级字段仍是逐字节强比较。）")

    try:
        print("---- 采集 OLD ----")
        o = run_side("old", old_url, browser, stub_port, args.verbose)
        print("---- 采集 NEW ----")
        n = run_side("new", new_url, browser, stub_port, args.verbose)
    finally:
        srv.shutdown()

    print("---- 动作序执行回执（两侧应完全一致）----")
    for i, (a, b) in enumerate(zip(o["action_steps"], n["action_steps"])):
        print("  [%d] OLD %-40s ｜ NEW %s" % (i, a, b))

    print("---- 逐项比对 ----")
    bad = 0
    ob, nb = collapse(json.loads(o["boot_trace"])), collapse(json.loads(n["boot_trace"]))
    op, np_ = collapse(json.loads(o["poll_trace"])), collapse(json.loads(n["poll_trace"]))
    oa, na = collapse(json.loads(o["action_trace"])), collapse(json.loads(n["action_trace"]))
    oi = ["%s = %s" % (k, json.dumps(v, ensure_ascii=False)) for k, v in zip(IDENT_PROBES, o["idents"])]
    ni = ["%s = %s" % (k, json.dumps(v, ensure_ascii=False)) for k, v in zip(IDENT_PROBES, n["idents"])]

    bad += diff_lines("标识符探针", oi, ni, True)

    # ---------- 刀18 · 下线标识符的反向断言（两侧各钉各的字面量）----------
    print("  —— 刀18 反向断言：任务面板子树在 NEW 侧已整棵下线（OLD/NEW 各钉字面量，"
          "比「两侧相等」更紧）——")
    for (expr, exp_o, exp_n), got_o, got_n in zip(IDENT_GONE, o["idents_gone"], n["idents_gone"]):
        hit_o, hit_n = got_o == exp_o, got_n == exp_n
        mark = "✓" if (hit_o and hit_n) else "✗"
        print("    %s %-48s OLD %-34s（期望 %s）｜ NEW %-34s（期望 %s）"
              % (mark, expr,
                 json.dumps(got_o, ensure_ascii=False), json.dumps(exp_o, ensure_ascii=False),
                 json.dumps(got_n, ensure_ascii=False), json.dumps(exp_n, ensure_ascii=False)))
        if not hit_o:
            bad += 1
        if not hit_n:
            bad += 1
    # 整侧全程零出网：TSVC(:9981) 在两侧都一次都没被打（拦截器仍挂着，所以是「真没打」
    # 而不是「没拦到」—— 刀18 之前这里 OLD/NEW 各有 2 条 tApi 探针请求）
    for side, r in (("OLD", o), ("NEW", n)):
        print("    %s| 全程打到 TSVC(%s) 的请求: %d 条 %s（应 0）"
              % (side, TSVC_HOST, len(r["tsvc_hits"]), r["tsvc_hits"][:4]))
        if r["tsvc_hits"]:
            bad += 1

    bad += diff_lines("启动轨迹", ob, nb, args.verbose)
    bad += diff_lines("轮询轨迹", op, np_, args.verbose)
    bad += diff_lines("动作序轨迹", oa, na, args.verbose)

    # ---------- 刀12 · 计算样式（逐元素逐属性）----------
    print("  —— 计算样式（%d 个属性 × 文档序枚举 body * ；取值点：readyState=complete"
          " 且 styleSheets 连续两次采样稳定之后）——" % len(STYLE_PROPS))
    for side, r in (("OLD", o), ("NEW", n)):
        w = r["style_wait"]
        print("    %s| 稳定等待: ready=%s ｜ 采样稳定=%s ｜ 轮询 %d 拍 ｜ styleSheets 快照 [%s]"
              % (side, w["ready"], w["stable"], w["ticks"], w["snap"]))
        if not w["stable"]:
            print("    %s| ✗ 5s 内没等到样式表稳定 —— 判红（不许拿不稳定的时刻取值）" % side)
            bad += 1
    oc, nc = o["computed"], n["computed"]
    print("    元素数: OLD %d / NEW %d ｜ 相等: %s" % (len(oc), len(nc), len(oc) == len(nc)))
    if len(oc) != len(nc):
        bad += 1
        # 多出来的是哪几个元素 —— 直接把签名打出来，别让读者靠猜。
        # （这一段只加诊断，不改判定：元素数不等照样 +1 判红。）
        longer, tag = (oc, "OLD") if len(oc) > len(nc) else (nc, "NEW")
        for i in range(min(len(oc), len(nc)), len(longer)):
            print("    ✗ %s 侧多出 [%s]" % (tag, " | ".join(longer[i].split(" | ")[:2])))
    cdiff = 0
    for i in range(min(len(oc), len(nc))):
        if oc[i] != nc[i]:
            cdiff += 1
            if cdiff == 1:
                print("    ✗ 首个分歧元素：")
                print("      OLD| " + oc[i])
                print("      NEW| " + nc[i])
                a_kv = dict(kv.split("=", 1) for kv in oc[i].split(" | ")[2].split("; "))
                b_kv = dict(kv.split("=", 1) for kv in nc[i].split(" | ")[2].split("; "))
                for k in STYLE_PROPS:
                    if a_kv.get(k) != b_kv.get(k):
                        print("        · %s: OLD %r ｜ NEW %r" % (k, a_kv.get(k), b_kv.get(k)))
    if cdiff == 0 and len(oc) == len(nc):
        print("    ✓ 计算样式       两侧逐元素逐属性一致（%d 个元素 × %d 属性 = %d 个值）"
              % (len(oc), len(STYLE_PROPS), len(oc) * len(STYLE_PROPS)))
    else:
        print("    计算样式分歧元素数: %d / %d" % (cdiff, min(len(oc), len(nc))))
    bad += cdiff

    # ---------- 刀12 · 反向证据：外链确已加载 ----------
    print("  —— 反向证据：外链 /group.css 与 /group_view.js 真的加载了（防「两个空页面也算一致」）——")
    for side, r in (("OLD", o), ("NEW", n)):
        for s in r["sheets"]["sheets"]:
            print("    %s| 样式表 href=%s owner=<%s> media=%r disabled=%s 规则数=%d err=%s"
                  % (side, s["href"], s["owner"], s["media"], s["disabled"], s["rules"], s["err"]))
        print("    %s| 全部样式表 cssRules 的 cssText 拼接：%d 字符  md5 %s"
              % (side, r["sheets"]["total"], r["sheets"]["md5"]))
        if any(s["err"] for s in r["sheets"]["sheets"]):
            print("    %s| ✗ 有样式表读 cssRules 报错（SecurityError 之类）" % side)
            bad += 1
    ext = [s for s in n["sheets"]["sheets"]
           if s["href"] and s["href"].endswith("/group.css")]
    ext_ok = len(ext) == 1 and ext[0]["rules"] > 0 and not ext[0]["err"]
    print("    NEW 侧存在 href 以 /group.css 结尾且 cssRules.length>0 的样式表: %s（命中 %d 张，规则 %s）"
          % (ext_ok, len(ext), ext[0]["rules"] if ext else "-"))
    if not ext_ok:
        bad += 1
    # OLD 侧那条按快照形态分派（刀13 起）：k11/k12 快照样式是**内联 <style>**，
    # k13 快照已是刀12 收口后的形态、样式**也是外链 /group.css** —— 两种形态各有一条
    # **同强度**的正面断言（都要求「有且仅有一张、规则数 > 0」），没有一侧被放过。
    if is_k13:
        oext = [s for s in o["sheets"]["sheets"]
                if s["href"] and s["href"].endswith("/group.css")]
        inl_ok = len(oext) == 1 and oext[0]["rules"] > 0 and not oext[0]["err"]
        print("    OLD 侧（k13 快照，样式已外链）存在 href 以 /group.css 结尾且 cssRules.length>0 的样式表: %s"
              "（命中 %d 张，规则 %s）" % (inl_ok, len(oext), oext[0]["rules"] if oext else "-"))
    else:
        inl = [s for s in o["sheets"]["sheets"] if s["href"] is None and s["owner"] == "style"]
        inl_ok = len(inl) == 1 and inl[0]["rules"] > 0
        print("    OLD 侧存在内联 <style> 样式表且 cssRules.length>0: %s（命中 %d 张，规则 %s）"
              % (inl_ok, len(inl), inl[0]["rules"] if inl else "-"))
    if not inl_ok:
        bad += 1
    len_ok = o["sheets"]["total"] == n["sheets"]["total"]
    md5_ok = o["sheets"]["md5"] == n["sheets"]["md5"]
    print("    CSSOM 归一后总长度 OLD %d == NEW %d: %s ｜ 拼接文本 md5 相同: %s"
          % (o["sheets"]["total"], n["sheets"]["total"], len_ok, md5_ok))
    if not len_ok:
        bad += 1
    if not md5_ok:
        bad += 1
    print("  —— 线级：静态件请求（两侧天然不同，只断言下列正面事实，不逐行 diff）——")
    for side, r in (("OLD", o), ("NEW", n)):
        for line in r["doc_reqs"]:
            print("    %s| %s" % (side, line))
    new_got = [x for x in n["doc_reqs"] if x.startswith("GET /group.css ")]
    old_got = [x for x in o["doc_reqs"] if x.startswith("GET /group.css ")]
    if is_k13:
        # k13 快照已是刀12 收口后的形态 → 样式两侧都外链，正面断言改成「两侧都必须取过」。
        print("    NEW 侧确实请求过 /group.css: %s ｜ OLD 侧（k13 快照，样式已外链）也必须取过: %s"
              % (bool(new_got), bool(old_got)))
        if not new_got or not old_got:
            bad += 1
    else:
        print("    NEW 侧确实请求过 /group.css: %s ｜ OLD 侧一次都没请求（样式是内联的）: %s"
              % (bool(new_got), not old_got))
        if not new_got or old_got:
            bad += 1
    # 刀13 反向证据：业务视图外链真的被取了（防「外链根本没加载，两个空页面也算一致」）。
    # 三份旧快照（k11/k12/k13）里脚本都还是内联的 → OLD 侧一次都不该请求它，无条件成立。
    new_v = [x for x in n["doc_reqs"] if x.startswith("GET /group_view.js ")]
    old_v = [x for x in o["doc_reqs"] if x.startswith("GET /group_view.js ")]
    print("    NEW 侧确实请求过 /group_view.js: %s（%s）｜ OLD 侧一次都没请求（脚本是内联的）: %s"
          % (bool(new_v), new_v[0] if new_v else "-", not old_v))
    if not new_v or old_v:
        bad += 1

    print("  —— 函数级冻结输入（请求行 + 请求头 + 请求体 + Error.message 逐字节）——")
    fz = 0
    n_both = n_gone = 0
    for (mode, a, b) in zip([c[4] for c in FROZEN], o["frozen"], n["frozen"]):
        if mode == "both":
            n_both += 1
            la = [a["call"], json.dumps(a["outcome"], ensure_ascii=False, sort_keys=True)] + a["wire"]
            lb = [b["call"], json.dumps(b["outcome"], ensure_ascii=False, sort_keys=True)] + b["wire"]
            if la == lb:
                print("    ✓ %-46s 线级 %d 条逐字节一致 ｜ 结果 %s"
                      % (a["call"], len(a["wire"]),
                         json.dumps(a["outcome"], ensure_ascii=False)[:100]))
            else:
                fz += 1
                print("    ✗ %s" % a["call"])
                for i in range(max(len(la), len(lb))):
                    x = la[i] if i < len(la) else "<缺>"
                    y = lb[i] if i < len(lb) else "<缺>"
                    if x != y:
                        print("        OLD| " + x.replace("\n", "\n        OLD| "))
                        print("        NEW| " + y.replace("\n", "\n        NEW| "))
            continue
        # mode == "gone"：刀18 —— NEW 侧函数已下线，两侧各自对字面量
        n_gone += 1
        exp = FROZEN_GONE_EXPECT.get(a["call"])
        if exp is None:
            fz += 1
            print("    ✗ %-46s 装置漏登记 FROZEN_GONE_EXPECT" % a["call"])
            continue
        checks = [
            ("OLD outcome", a["outcome"], exp["outcome"]),
            ("OLD 线级条数", len(a["wire"]), exp["wire"]),
            ("NEW outcome", b["outcome"], exp["outcome"]),
            ("NEW 线级条数", len(b["wire"]), exp["wire"]),
        ]
        miss = [(t, g, e) for t, g, e in checks if g != e]
        if not miss:
            print("    ✓ %-46s [gone] 两侧均已无此函数且零出网（%s ｜ 线级 OLD %d / NEW %d 条）"
                  % (a["call"], json.dumps(b["outcome"], ensure_ascii=False),
                     len(a["wire"]), len(b["wire"])))
        else:
            fz += len(miss)
            print("    ✗ %-46s [gone]" % a["call"])
            for t, g, e in miss:
                print("        %s: 实测 %s ｜ 期望 %s"
                      % (t, json.dumps(g, ensure_ascii=False), json.dumps(e, ensure_ascii=False)))
    bad += fz
    print("  函数级不一致例数: %d / %d（两侧逐字节 %d 例 + 刀18 下线反向断言 %d 例）"
          % (fz, len(FROZEN), n_both, n_gone))

    eo = diff_lines("JS 错误列表", o["errs"], n["errs"], True)
    bad += eo
    print("  两侧错误列表条数: OLD %d / NEW %d" % (len(o["errs"]), len(n["errs"])))

    print("  —— 客户端身份头自证（钉死是否真的生效）——")
    ua_line = "User-Agent: " + CLIENT_UA
    for side, r in (("OLD", o), ("NEW", n)):
        for h in r["client_ident"]:
            print("    %s| %s" % (side, h))
        hit = ua_line in r["client_ident"]
        ua_only = [h for h in r["client_ident"] if h.lower().startswith("user-agent:")]
        print("    %s 侧线上只出现钉死的那一条 User-Agent: %s（实际出现 %d 种）"
              % (side, hit and len(ua_only) == 1, len(ua_only)))
        if not (hit and len(ua_only) == 1):
            bad += 1
    print("    两侧客户端身份头集合相同: %s" % (o["client_ident"] == n["client_ident"]))

    print("---- 装置自证汇总 ----")
    print("  轨迹条数（压缩后）: 启动 OLD %d / NEW %d ｜ 轮询 OLD %d / NEW %d ｜ 动作序 OLD %d / NEW %d"
          % (len(ob), len(nb), len(op), len(np_), len(oa), len(na)))
    print("  轨迹条数（压缩前）: 启动 OLD %d / NEW %d ｜ 轮询 OLD %d / NEW %d ｜ 动作序 OLD %d / NEW %d"
          % (len(json.loads(o["boot_trace"])), len(json.loads(n["boot_trace"])),
             len(json.loads(o["poll_trace"])), len(json.loads(n["poll_trace"])),
             len(json.loads(o["action_trace"])), len(json.loads(n["action_trace"]))))
    print("  归一化实际命中的字段（没命中的规则不列）:")
    if not NORM_HITS:
        print("    （无 —— 一条归一化规则都没触发）")
    for k, v in sorted(NORM_HITS.items()):
        print("    · %s  命中 %d 次" % (k, v))
    # 旧侧是「等价旧侧」，装置全程只读它；跑完再验一次没被谁顺手改掉
    now_stat = (os.path.getsize(old_html), md5(old_html), os.path.getmtime(old_html))
    old_intact = now_stat == old_stat
    print("  旧侧快照跑完仍未被改写: %s（%d B / md5 %s / mtime %.6f）"
          % (old_intact, now_stat[0], now_stat[1], now_stat[2]))
    if not old_intact:
        print("    ✗ 开工时 %d B / md5 %s / mtime %.6f" % old_stat)
        bad += 1
    print("  CSS 文本级一致: %s ｜ JS 文本级一致: %s ｜ 业务视图文本级一致: %s ｜ 逐条 diff 行数合计: %d"
          % (css_ok, txt_ok, view_ok, bad))

    if css_ok and txt_ok and view_ok and bad == 0:
        print("EQUIV_SENTINEL_OK")
        return 0
    print("EQUIV_SENTINEL_FAIL")
    return 1


# ====================================================================== 确定性自证（--runs N）

PORT_RX = re.compile(r"127\.0\.0\.1:\d+")


def repeat(args):
    """连跑 N 次子进程；每次都必须 OK，且**把随机端口归一后 N 份输出逐字节相同**。"""
    n = args.runs
    print("=" * 78)
    print("确定性自证：同一份树连跑 %d 次（每次一个干净子进程）" % n)
    print("=" * 78)
    outs, paths, codes = [], [], []
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    cmd = [sys.executable, os.path.abspath(__file__), "--old", os.path.abspath(args.old)]
    if args.old_md5:
        cmd += ["--old-md5", args.old_md5]
    if args.verbose:
        cmd += ["-v"]
    print("  子进程命令行: %s" % " ".join(cmd[1:]))
    for i in range(1, n + 1):
        t0 = time.time()
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        txt = p.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
        path = os.path.join(tempfile.gettempdir(), "k13_run%d.txt" % i)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(txt)
        outs.append(txt)
        paths.append(path)
        codes.append(p.returncode)
        print("  第 %d/%d 次 ｜ 退出码 %d ｜ %s ｜ %5.1fs ｜ %s"
              % (i, n, p.returncode,
                 "EQUIV_SENTINEL_OK" if "EQUIV_SENTINEL_OK" in txt else "EQUIV_SENTINEL_FAIL",
                 time.time() - t0, path))
        sys.stdout.flush()

    all_ok = all(c == 0 for c in codes) and all("EQUIV_SENTINEL_OK" in t for t in outs)

    # 随机端口是装置自己的外部条件（free_port），不是被测行为 → 归一后再逐字节比
    normed = [PORT_RX.sub("127.0.0.1:<PORT>", t) for t in outs]
    digests = [hashlib.md5(t.encode("utf-8")).hexdigest() for t in normed]
    same = len(set(digests)) == 1
    print("---- 归一端口后的逐次输出摘要 ----")
    for i, d in enumerate(digests, 1):
        print("  第 %d 次 md5(归一后输出) = %s  长度 %d B" % (i, d, len(normed[i - 1])))
    print("  %d 份输出归一端口后逐字节相同: %s" % (n, same))
    if not same:
        base = normed[0].split("\n")
        for i in range(1, n):
            cur = normed[i].split("\n")
            for k in range(max(len(base), len(cur))):
                x = base[k] if k < len(base) else "<缺>"
                y = cur[k] if k < len(cur) else "<缺>"
                if x != y:
                    print("    第1次 vs 第%d次 首个差异行 %d:" % (i + 1, k))
                    print("      #1 | " + x)
                    print("      #%d | %s" % (i + 1, y))
                    break
    print("  每次都 EQUIV_SENTINEL_OK 且退出码 0: %s" % all_ok)
    if all_ok and same:
        print("EQUIV_DETERMINISM_OK")
        return 0
    print("EQUIV_DETERMINISM_FAIL")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true", help="打印全量轨迹")
    ap.add_argument("--runs", type=int, default=1,
                    help="连跑 N 次自证确定性（默认 1；>1 时每次起一个干净子进程，"
                         "要求次次 OK 且归一随机端口后 N 份输出逐字节相同）")
    ap.add_argument("--old", default=K13_HTML,
                    help="旧侧 HTML 路径（默认 _old/group_old_k13.html；传 k12 / k11 快照也能跑）")
    ap.add_argument("--old-md5", default="",
                    help="可选：额外硬校验旧侧 md5 前缀。不给就只校验「新旧 md5 不同」")
    args = ap.parse_args()
    if args.runs > 1:
        return repeat(args)
    return run_once(args)


# ---------------------------------------------------------------- 已知偏差
# 1) /login 返 204 而不是 200：200 会让浏览器真的导航走、把页面连同 window.__tr 一起
#    卸掉，后面的采集就没了。204 让浏览器发出那次 GET 但不换文档 —— 401 分支
#    「发了什么、抛了什么」照样被完整记录。两侧同一份夹具，不影响可比性。
# 2) 动作序前后掐 TIMER：轮询「发什么」已在 A2 轮询轨迹里单独逐行比过；动作序要比的是
#    动作本身，掐掉定时器是为了不让调度抖动伪造出 diff。两侧同一套代码、同一时机。
# 3) 根路径 /group_transport.js 对两侧都发得出来，但旧页面压根不引用它 ——
#    它只会出现在新侧的文档子资源请求里，而子资源请求不进 window.__tr（探针只包 fetch）。
# 4) /group.css 同理：桩对两侧都发得出来，刀12 时只有新侧会去取。这不是「偏差」而是刀12 要证的事，
#    所以它被抬成**正面断言**（doc_reqs：NEW 必须取过、OLD 必须一次没取过），
#    而不是靠「两侧都没差」蒙混过去。刀13 起默认旧侧换成 k13 快照（已是刀12 收口后的形态、
#    样式也外链），这条断言按快照形态**分派**成「两侧都必须取过」—— 同为正面断言、同等强度；
#    传 k12/k11 快照时仍走原来那条「OLD 一次都没取过」，一字未改。
# 6) /group_view.js 是刀13 的同款正面断言：三份旧快照（k11/k12/k13）里脚本都还是内联的，
#    所以「NEW 必须取过、OLD 必须一次没取过」是**无条件**成立的强断言，不需要分派。
# 5) 计算样式只在启动 settle 之后、动作序之前取一次。动作序会改 DOM（开对话框、加消息），
#    那之后的样式属于「动作后的状态」，已由动作序轨迹与 JS 错误列表覆盖；
#    这里刻意不取第二次，免得把 headless 的布局抖动当成被测差异。

if __name__ == "__main__":
    sys.exit(main())
