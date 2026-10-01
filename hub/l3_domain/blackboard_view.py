#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
blackboard_view.py —— 团队黑板·只读看板（默认端口 9941）

一句话：把 9940 的黑板 API 包一层**只读**的中文网页，让人直接看见"多 Agent 往
共享记忆里写了什么"，而不用自己 curl 带 token。

设计要点（与 blackboard_svc.py 同一套口径）：
  * 纯标准库：http.server + urllib，**零第三方依赖**，离线可跑。
  * **只读**：只接 GET / HEAD；POST/PUT/PATCH/DELETE 一律 405。本文件里没有任何
    往黑板写数据的路径 —— 不碰数据库、不发写请求，黑板的 append-only 由上游保证，
    这里连"能写"的代码都不存在。
  * **token 不出浏览器**：凭据只在本进程内存里，只在发往上游的 Authorization 头上
    出现一次。页面走本服务的 /api/*，浏览器永远拿不到 token；所有响应体和日志
    都过 _scrub()，token 绝不外泄。
  * **正文不可信**：条目内容是各 Agent 写的，可能含 <script> / 引号 / &。
    两道防线：① 服务端 JSON 把 < > & 转成 \\uXXXX（JSON.parse 原样解回，不失真，
    但字节流里不会出现裸标签）；② 页面一律用 textContent 渲染，绝不拼 innerHTML。
  * **上游挂了不崩**：连不上 / 超时 / 401 / 500 → API 返 502，页面顶部亮黄条。
    单次上游超时 5s，不跟随 3xx 跳转（否则 Authorization 头可能被带去别的 host）。
    唯一例外是 404：/api/sessions/<id> 的"这场会议不存在"要原样透给前端，
    否则前端分不清"没这场会"和"上游挂了"。
  * v1.1 加了**会议**页签：圆桌会议的场次与逐条发言同样是只读透传。
    发言正文与 refs 一样是各 Agent 写的**不可信内容**，走同一套转义与 textContent。
  * v1.2 把"会议四要素"摆上台面，页签变成**黑板 / 会议 / 跟踪**三个：
      - 会议页签：会议目标 → 议程逐项（含该项结论与状态徽标）→ 总体结论 + 当场确认
        （agree/object/abstain/absent 名单与计数，passed 与否一眼可见）→ 落实事项
        （责任人 / 截止 / 状态，逾期标红）→ 遗留问题。
      - 跟踪页签：跨会的落实事项与遗留问题总清单，顶部给"未闭环 / 逾期"计数。
    **逾期与否一律用上游算好的 overdue 字段**，页面不自己比时间；时间统一显示成
    北京时间（UTC+8，页面里写明时区），不看浏览器本地时区的脸色。
    老会议（没有 goal/agenda 这些新字段）照常渲染，显示"（未记录目标/议程）"。
  * v1.3 按产品口径重构成**三个模块**：会议（日历）/ 共享内容 / 跟踪。
      - 会议 = 月历（有会议的日期打角标，角标是当天场次）→ 当天会议列表 → 详情。
        详情按 ①目标 ②议程 ③结论与当场确认 ④落实 ⑤遗留 的顺序排，附出席率，
        原来那个"黑板流水"页签收进会议模块，当"完整留痕"看。
      - 共享内容 = 任务级（按任务分组，带 TTL / 销毁条件）+ 永久（按 kind 分组），
        过期 / 待复审 / 7 天内到期 / 已撤下各有高亮；**sensitive 且未授权的条目
        在服务端就把标题与正文抹掉**（mask_shared），浏览器侧连字节都拿不到。
        上游还没有 /shared 端点时 → 502 shared_unavailable，页面降级显示原因。
      - 页面给 Agent 用的「查询手册」：端点清单 + 可直接复制的 curl（只读，写 405）。
      - 响应式做到三档断点（≥1024 / 768~1023 / <768）外加 <480 微调：月历在手机上
        换成日期卡片列表，表格行转单列卡片，触摸目标 ≥44px，长内容走 overflow-x
        的代码块，字号用 clamp() —— 页面不许出现整页横向滚动。
  * v1.4 给看板加**登录口令 + 角色权限 + 审计**，为的是这条看板要开到公网上。
      - 双层门槛：外层 TLS 由 Cloudflare 隧道终结（cloudflared 在本机以 127.0.0.1
        回连本服务），内层就是这里的账号登录。认证的实现全在 view_auth.py，
        本文件只负责"把门"与"按角色遮内容"。
      - **--mode public（部署默认）：一律要求登录**，含 /api/*。免登录只有三个口子：
        /healthz、GET /login、POST /login。免登录白名单**只认 tailnet 段
        <tailnet-cidr>**（内部 Agent 走 tailnet IP 直接 curl，不受影响）。
        ⚠️ **127.0.0.1 / ::1 在 public 模式下不免登录** —— cloudflared 就在本机以
        127.0.0.1 回连，放行本机等于给公网开后门。--mode tailnet 只用于本机调试，
        那时候（且仅那时候）回环地址才免登录。
        判据**只看对端 IP**；X-Forwarded-For / CF-Connecting-IP 不进鉴权。
      - **两套 IP 并用**（挂 Cloudflare 隧道后的必需品，见 --trust-proxy）：
        cloudflared 从 127.0.0.1 回连 → 所有公网访客的对端 IP 都一样，拿它当锁定键
        等于"一人连错五次、全场锁 15 分钟"，审计里也人人都是 127.0.0.1。
        于是**鉴权（免登录白名单）走 _peer_ip()，锁定键与审计 IP 走 _client_ip()**；
        后者只在对端 IP 命中 --trust-proxy 时才采信外层的头。默认不配 = 谁都不信，
        行为与加这个参数之前完全一致。为什么敢在一处信头、绝不在另一处信头，
        见 _client_ip() 上面那段注释。
      - 角色三级 viewer / member / owner 对应密级 public / internal / sensitive，
        看不了的内容**在服务端就抹成"存在但无权查看"**：标题、正文、标签、责任人
        一个字节都不下发（不是靠前端不渲染）。
      - 只读纪律不变：**唯一的写入口是 POST /login**（登录 / 改密 / 退出三个动作由
        表单里的 action 区分，都带 CSRF）。其余任何 POST/PUT/PATCH/DELETE 一律 405。
      - 安全头补齐：HSTS、CSP、nosniff、X-Frame-Options: DENY、Referrer-Policy、
        X-Robots-Tag: noindex（这页不该被搜索引擎收录）。

用法：
    # 内网调试（免登录，口径同 v1.3）
    python blackboard_view.py --host <NODE_IP> --port 9941 --mode tailnet \\
        --upstream <BB_BASE> \\
        --token-file <PASS_FILE>

    # 公网部署（默认口径：一律要登录）
    python blackboard_view.py --host 127.0.0.1 --port 9941 --mode public \\
        --upstream <BB_BASE> \\
        --token-file <PASS_FILE> \\
        --session-key-file <PASS_FILE> \\
        --dsn-file <ETC_DIR>/pg.env \\
        --log-file /var/log/<SERVICE>.log
"""

import argparse
import http.cookies
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

# 批① 起被搬件按绝对包名 import，故把仓根一并放进搜索路径（本目录仍在最前）。
for _hacs_p in (os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                os.path.dirname(os.path.abspath(__file__))):
    sys.path.insert(0, _hacs_p)

from node.l0_host import node_paths as _np  # noqa: E402  L0 平台层：宿主默认值唯一出处（env 优先）
from node.l0_host import node_profile as _npf  # noqa: E402  L0 节点档案：宿主事实唯一真源（协作域基址；env 优先）

from backend import view_auth  # 账号 / 会话 / 角色 / 审计，纯标准库；批⑤f 过渡（⑤i 搬 hub/x/ 后再改）
from node.l1_transport import web_gateway                      # L1 通信层：HTTP 应答出口与路由分发

VERSION = "1.5"

# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_RUNTIME_DIR = os.environ.get("AGENT_TEAM_RUNTIME_DIR") or _np.runtime_dir()
# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_ETC_DIR = os.environ.get("AGENT_TEAM_ETC_DIR") or _np.etc_dir()
# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_DOMAIN = os.environ.get("AGENT_TEAM_DOMAIN") or "example.com"
# 兜底默认值集中到 L0 node_profile（env 优先）
AGENT_TEAM_BB_BASE = os.environ.get("AGENT_TEAM_BB_BASE") or _npf.blackboard_base_default()

DEFAULT_UPSTREAM = AGENT_TEAM_BB_BASE
DEFAULT_TOKEN_FILE = os.environ.get("AGENT_TEAM_TOKEN_FILE") or AGENT_TEAM_ETC_DIR + "/client-token"
DEFAULT_PORT = 9941
DEFAULT_REFRESH_S = 30

UPSTREAM_TIMEOUT_S = 5.0        # 单次上游请求超时，硬性 5s
UPSTREAM_MAX_BYTES = 4 * 1024 * 1024
DRAIN_MAX = 1024 * 1024         # 405 时最多排空这么多请求体
DEFAULT_LIMIT = 20
MAX_LIMIT = 50                  # 与上游 /search 的上限对齐，一律夹在 1..50
QUERY_MAX = 2048                # q 的长度上限
DETAIL_MAX = 200                # 错误 detail 截断长度
MIN_REFRESH_S = 5               # 自动刷新最快 5s，0 表示关闭

LOGIN_PATH = "/login"
ACCOUNT_PATH = "/account"
# v1.5：根路径让给 webPortal 卡片墙（/portal 同指），白板本尊挪到 /board
#（旧链接 302 过去，老书签不断）
BOARD_PATH = "/board"
PORTAL_PATH = "/"
FORM_MAX_BYTES = 8192           # 登录表单最多这么大，多的直接拒（这是唯一的写入口）
MODES = ("public", "tailnet")
TRUST_HEADER_MAX = 64           # 外层声称的 IP 超过这么长就当垃圾，直接退回对端 IP
LOGIN_ACTIONS = ("login", "passwd", "logout")
CSRF_FIELD = "csrf"             # 表单里 CSRF 隐藏域的名字（cookie 那半边在 view_auth）
NEXT_MAX = 512                  # ?next= 的长度上限
SESSION_MAX_AGE_S = view_auth.SESSION_TTL_S

DEFAULT_SESSION_KEY_FILE = os.environ.get("AGENT_TEAM_SESSION_KEY_FILE") or AGENT_TEAM_ETC_DIR + "/view-session.key"
DEFAULT_DSN_FILE = os.environ.get("AGENT_TEAM_PG_ENV_FILE") or AGENT_TEAM_ETC_DIR + "/pg.env"

LOG = logging.getLogger("blackboard_view")

_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")


class Runtime(object):
    """进程级运行时配置（由 main 填充，handler 只读）。"""

    upstream = DEFAULT_UPSTREAM
    token = ""
    token_loaded = False
    token_file = DEFAULT_TOKEN_FILE
    refresh_seconds = DEFAULT_REFRESH_S

    # v1.4：认证。**默认 public（要登录）** —— 这个默认值是故意往严了定的：
    # 忘了传 --mode 的后果应该是"多输一次口令"，不该是"把主人画像挂到公网上"。
    mode = "public"
    auth = None                 # type: Any  —— view_auth.Auth 实例；None = 认证未配置
    # cookie 要不要打 Secure。默认自动判断（X-Forwarded-Proto: https），
    # --secure-cookie 则不管前面写什么头都强制打上（证书由 cloudflared 终结时用这个）。
    secure_cookie = False
    # 受信反代网段（--trust-proxy 解析结果）。**默认空元组 = 谁都不信**，
    # 这时 _client_ip() 逐字节等价于 _peer_ip()，行为与没有这个参数时完全一致。
    trust_proxy = ()            # type: Tuple[Any, ...]


class Visitor(object):
    """一次请求背后的访问者。

    与 view_auth.Principal 的差别只有一处，但很要命：**这里的 role 允许是 None**。
    None 表示"不做密级遮蔽"（tailnet 内网直连的 Agent）；Principal 会把 None 归一成
    viewer，那样内网 Agent 会突然看不见 internal 内容 —— 所以内网这条路不复用它。
    """

    __slots__ = ("username", "role", "source")

    def __init__(self, username: str, role: Optional[str], source: str) -> None:
        self.username = username
        self.role = role
        self.source = source            # session | tailnet


# --- 脱敏与序列化 ---------------------------------------------------

def _scrub(text: Any) -> str:
    """把任何可能带出 token 的文本洗干净，再截断。

    两层：整串替换掉当前 token，再兜底把 "Bearer xxx" 整体打码 —— 即便将来
    换了取凭据的方式，这行也还挡着。
    """
    out = str(text)
    token = Runtime.token
    if token and token in out:
        out = out.replace(token, "***")
    out = _BEARER_RE.sub("Bearer ***", out)
    if len(out) > DETAIL_MAX:
        out = out[:DETAIL_MAX] + "..."
    return out


def json_bytes(payload: Dict[str, Any]) -> bytes:
    """JSON 序列化，并把 < > & 转成 \\uXXXX 转义。

    浏览器 JSON.parse 会原样解回来（正文一个字符都不丢），但响应字节流里
    永远不会出现裸 <script> —— 就算有人把这段响应直接塞进 HTML 也炸不了。
    """
    text = json.dumps(payload, ensure_ascii=False)
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return text.encode("utf-8")


# --- 共享内容：sensitive 遮罩 + 角色遮罩 ------------------------------
#
# 口径一（v1.3，上游口径）：sensitive=true 且 authorized!=true 的条目，**在看板这一层
# 就把标题、正文、标签、变更历史抹掉**，只留"存在但无权查看"所必需的骨架（谁维护、
# 什么时候更新、归哪个 kind）。不是靠前端不渲染 —— 那样正文仍然会躺在浏览器的网络
# 面板里。
#
# 口径二（v1.4，角色口径）：登录进来的人有角色，内容有密级。
#   viewer(只读访客) 只看 public；member 看到 internal；owner 全都能看。
#   角色不够的条目遮得**更狠**：连 owner（责任人）和 task_title 都不给 ——
#   给外人看的那一版，连"这条是谁在维护"都不该漏。
SHARED_SCOPES = ("task", "permanent")
SHARED_SAFE_KEYS = (
    "shared_id", "scope", "kind", "owner", "task_id", "task_title",
    "updated_at", "expires_at", "review_at", "version", "revoked",
    "sensitive", "authorized", "expired", "review_due", "expiring_soon",
)
# 角色不够时能留下的字段：只够说明"这里存在一条内容"，别的一律不给
SHARED_PUBLIC_KEYS = (
    "shared_id", "scope", "kind", "task_id", "sensitivity",
    "updated_at", "expires_at", "review_at", "version", "revoked",
    "expired", "review_due", "expiring_soon",
)


def item_sensitivity(item: Dict[str, Any], default: Optional[str]) -> Optional[str]:
    """一条内容的密级。

    优先看上游给的 sensitivity 字段；没有就看老的 sensitive 布尔；两个都没有时，
    default=None 表示"这条东西压根没有密级概念"（会议、落实、遗留这些公开字段），
    default='internal' 表示"共享内容没标密级，按内部算"（默认收紧）。
    """
    raw = str(item.get("sensitivity") or "").strip().lower()
    if raw in view_auth.SENSITIVITY_RANK:
        return raw
    if item.get("sensitive") is True:
        return "sensitive"
    return default


def _mask_item(item: Any, role: Optional[str] = None,
               default_level: Optional[str] = view_auth.DEFAULT_SENSITIVITY) -> Any:
    """一条内容：不该看的就只剩骨架 + masked=True。

    role=None 表示"不按角色判"（tailnet 免登录、或者本来就没有密级的内容），
    这时只走 v1.3 的 sensitive/authorized 口径。
    """
    if not isinstance(item, dict):
        return item

    level = item_sensitivity(item, default_level)
    if role is not None and level is not None and not view_auth.role_can_see(role, level):
        masked = {k: v for k, v in item.items() if k in SHARED_PUBLIC_KEYS}
        masked["masked"] = True
        masked["mask_reason"] = "role"
        masked["sensitivity"] = level
        masked["title"] = ""
        masked["body"] = ""
        masked["tags"] = []
        return masked

    if not item.get("sensitive") or item.get("authorized") is True:
        return item
    masked = {k: v for k, v in item.items() if k in SHARED_SAFE_KEYS}
    masked["masked"] = True
    masked["mask_reason"] = "sensitive"
    masked["title"] = ""
    masked["body"] = ""
    masked["tags"] = []
    return masked


# 响应里可能装着条目的列表键。共享内容在 items/permanent/results/tasks[].items，
# 会议与跟踪的条目也一起扫一遍 —— 上游哪天给它们也标上 sensitivity，这里立刻生效。
LIST_KEYS = ("items", "permanent", "results", "entries", "actions", "issues",
             "turns", "agenda_items")


def _walk(payload: Dict[str, Any], fn: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    for key in LIST_KEYS:
        rows = payload.get(key)
        if isinstance(rows, list):
            payload[key] = [fn(r) for r in rows]
    tasks = payload.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict) and isinstance(task.get("items"), list):
                task["items"] = [fn(r) for r in task["items"]]
    session = payload.get("session")
    if isinstance(session, dict):
        _walk(session, fn)
    return payload


def mask_shared(payload: Dict[str, Any], role: Optional[str] = None) -> Dict[str, Any]:
    """共享内容的遮罩：没标密级的按 internal 算（默认收紧）。"""
    return _walk(payload, lambda item: _mask_item(item, role,
                                                  view_auth.DEFAULT_SENSITIVITY))


def apply_role(payload: Dict[str, Any], role: Optional[str]) -> Dict[str, Any]:
    """所有 API 响应的统一收口：按角色再过一遍。

    与 mask_shared 的差别只有一个 —— 这里 default_level=None：**没带密级字段的条目
    原样放行**。会议、落实事项、遗留问题的公开字段本来就是给所有登录用户看的，
    不能因为"没标 public"就把整个看板对 viewer 变成空白。
    """
    if role is None or role == "owner":
        return payload                  # owner 看全量（sensitive 仍受上游 authorized 约束）
    return _walk(payload, lambda item: _mask_item(item, role, None))


# --- 登录相关的小工具 -------------------------------------------------

def _esc(value: Any) -> str:
    """HTML 转义。自己写是因为 stdlib 的 html 模块不在本文件的依赖白名单里，
    而这几行的语义一望即知，没必要为它多进一个 import。"""
    return (str("" if value is None else value)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def safe_next(raw: Any) -> str:
    """把 ?next= 收敛成**站内相对路径**，别的一律退回 "/"。

    这是防开放跳转的那一道：`//evil.com` / `https://evil.com` / `/\\evil.com`
    在浏览器眼里都是"跳去别人家"，而登录页上的跳转链接最招人钓鱼。
    """
    text = str(raw or "").strip()
    if not text or len(text) > NEXT_MAX:
        return "/"
    if not text.startswith("/") and not text.lower().startswith("https://"):
        return "/"
    if text.startswith("//") or text[:2] == "/\\":
        return "/"                      # 协议相对 URL
    for ch in text:
        if ord(ch) < 0x20 or ch == "\x7f" or ch == "\\":
            return "/"                  # 控制字符（含 CR/LF 头注入）与反斜杠一律不收
    split = urllib.parse.urlsplit(text)
    if split.scheme or split.netloc:
        host = (split.hostname or "").lower()
        allow = bool(host and COOKIE_DOMAIN_SCOPE and split.scheme == "https"
                     and (host == COOKIE_DOMAIN_SCOPE
                          or host.endswith("." + COOKIE_DOMAIN_SCOPE)))
        if not allow or split.path == LOGIN_PATH:
            return "/"
        return text
    if split.path == LOGIN_PATH:
        return "/"                      # 登录成功再跳回登录页，等于原地打转
    return text


#: Cookie 归属域（可由环境变量覆盖）。让 <DOMAIN> 的各子域
#: （门户 / 看板 / 对话台）共享同一份登录态 —— 缺了它，在门户登录过的人
#: 打开 chat.<DOMAIN> 仍会被当未登录（host-only cookie 带不过去）。
COOKIE_DOMAIN_SCOPE = (os.environ.get("BBVIEW_COOKIE_DOMAIN")
                       or AGENT_TEAM_DOMAIN).strip().lower()


def cookie_domain(host: Optional[str]) -> Optional[str]:
    """按请求 Host 推导 Cookie 域。

    只对自有域名生效；tailnet IP 与 localhost 一律不带 Domain
    （IP 主机上带 Domain 的 cookie 会被浏览器直接丢掉）。
    """
    h = (host or "").split(":")[0].strip().lower()
    if not h or not COOKIE_DOMAIN_SCOPE:
        return None
    if h == COOKIE_DOMAIN_SCOPE or h.endswith("." + COOKIE_DOMAIN_SCOPE):
        return "." + COOKIE_DOMAIN_SCOPE
    return None


def cookie_header(name: str, value: str, secure: bool,
                  max_age: Optional[int] = None, delete: bool = False,
                  domain: Optional[str] = None) -> Tuple[str, str]:
    """拼一条 Set-Cookie。

    HttpOnly：JS 拿不到会话 token，页面被 XSS 也偷不走 cookie。
    SameSite=Lax：外站表单 POST 过来不带 cookie，CSRF 的第一道（第二道是 token）。
    Secure：只在 HTTPS 下发，明文 HTTP 里这个 cookie 直接不出现。
    """
    parts = ["%s=%s" % (name, value), "Path=/"]
    if domain:
        parts.append("Domain=" + domain)
    parts += ["HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    if delete:
        parts.append("Max-Age=0")
        parts.append("Expires=Thu, 01 Jan 1970 00:00:00 GMT")
    elif max_age is not None:
        parts.append("Max-Age=%d" % int(max_age))
    return ("Set-Cookie", "; ".join(parts))


def load_token(path: Optional[str]) -> str:
    """从文件读 token（只取第一行、去空白）。读不到不抛异常，交给调用方降级。"""
    if not path:
        return ""
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


# --- 受信反代：为什么要并用两套 IP ------------------------------------
#
# 背景：挂上 Cloudflare 隧道之后，cloudflared 在本机以 127.0.0.1 回连本服务，于是
# **所有公网访客的对端 IP 都是 127.0.0.1**。如果继续拿对端 IP 当锁定键，会出两件事：
#   ① 任何一个人连错 5 次，就把全体公网用户一起锁 15 分钟（白送的拒绝服务）；
#   ② 审计表里人人都是 127.0.0.1，事后追不了责。
#
# 所以这里**并用两套 IP，各管各的，千万别合并成一个**：
#   * `_peer_ip()` —— 对端 IP，内核给的，谁都伪造不了。**只**用来判免登录白名单
#     （tailnet <tailnet-cidr>）。白名单永远不看任何请求头：否则随便谁发一个
#     `CF-Connecting-IP: <tailnet-ip>` 就白拿内网免登录待遇，整套认证当场作废。
#   * `_client_ip()` —— 真实访客 IP，用来当**锁定键**和**审计 IP**。只有对端 IP
#     落在 --trust-proxy 列表里时才采信 CF-Connecting-IP / X-Forwarded-For。
#
# 敢在第二处信头、绝不在第一处信头，是因为两边信错的后果差着量级：锁定键被搅浑
# 最坏是"某个 IP 的失败计数记歪了、审计里多一条假 IP"，换不来任何权限；白名单信错
# 就是直接免登录。--trust-proxy 默认空 = 谁都不信，此时行为与加参数之前完全一致。


def parse_ip(raw: Any) -> Optional[Any]:
    """字符串 -> ipaddress 地址对象；认不出来返回 None（不抛异常）。

    顺手归一三种写法：`[::1]` 的方括号、`fe80::1%eth0` 的 scope id、
    `::ffff:8.8.8.8` 的 v4 映射 —— 同一个访客换个写法就换一把锁，那锁等于没上。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if "%" in text:
        text = text.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr


def parse_trust_proxy(raw: Any) -> Tuple[Any, ...]:
    """--trust-proxy 的逗号分隔串 -> 网段元组。看不懂的段**丢掉并告警**。

    单个 IP（`127.0.0.1`）当 /32、/128 处理，CIDR（`127.0.0.0/8`）原样收。
    整串为空 -> 空元组 = 谁都不信（默认值，也是出错时最安全的那一头）。
    """
    nets = []          # type: List[Any]
    for piece in str(raw or "").split(","):
        text = piece.strip()
        if not text:
            continue
        try:
            nets.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            LOG.warning("--trust-proxy 里这一段看不懂，已忽略：%r", text[:TRUST_HEADER_MAX])
    return tuple(nets)


def ip_in_nets(ip: Any, nets: Any) -> bool:
    """这个 IP 在不在受信网段里。网段为空 / IP 认不出来 -> 一律 False（不信）。"""
    if not nets:
        return False
    addr = parse_ip(ip)
    if addr is None:
        return False
    for net in nets:
        if addr.version == net.version and addr in net:
            return True
    return False


# --- 入参整形 -------------------------------------------------------

def clamp_limit(raw: Optional[str], default: int = DEFAULT_LIMIT) -> int:
    """limit 一律夹在 1..MAX_LIMIT；非数字（含空）退回默认值。"""
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_LIMIT, value))


def clamp_refresh(value: int) -> int:
    """自动刷新秒数：0 = 关闭，其余至少 MIN_REFRESH_S。"""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_S
    if seconds <= 0:
        return 0
    return max(MIN_REFRESH_S, seconds)


# --- 上游反代 -------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随 3xx：urllib 默认会把 Authorization 头一起带到跳转目标去。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def upstream_get(path: str, params: Dict[str, Any],
                 allow_status: Tuple[int, ...] = ()) -> Tuple[int, Dict[str, Any]]:
    """向黑板发一次 GET，返回 (本服务要回的 HTTP 码, 响应体)。

    上游的任何不顺（连不上 / 超时 / 401 / 500 / 返回不是 JSON）统统收敛成 502：
    对看板的使用者来说"上游不好使"是同一件事，细节放 detail 里（已脱敏）。

    allow_status 里的状态码是例外，原样透传 —— 目前只用于 404（"没这场会议"
    是业务事实，不是故障，前端要能分开显示）。
    """
    query = {k: v for k, v in params.items() if v not in (None, "")}
    url = Runtime.upstream.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)

    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    if Runtime.token:
        # token 在整个进程里只在这一行离开内存，且只发给 --upstream 指定的地址
        req.add_header("Authorization", "Bearer " + Runtime.token)

    started = time.time()
    try:
        resp = OPENER.open(req, timeout=UPSTREAM_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        code = exc.code
        try:
            body = exc.read(UPSTREAM_MAX_BYTES).decode("utf-8", "replace")
        except Exception:
            body = ""
        exc.close()
        if code in allow_status:
            try:
                passthrough = json.loads(body)
            except Exception:
                passthrough = None
            if not isinstance(passthrough, dict):
                passthrough = {"ok": False, "error": "upstream_status_%d" % code}
            passthrough.setdefault("ok", False)
            return code, passthrough
        if code == 401 or code == 403:
            detail = "上游拒绝了看板的凭据（%d）：检查 --token-file 指向的 token" % code
        else:
            detail = "上游返回 %d：%s" % (code, body[:DETAIL_MAX])
        return 502, {"ok": False, "error": "upstream_error",
                     "upstream_status": code, "detail": _scrub(detail)}
    except socket.timeout:
        return 502, {"ok": False, "error": "upstream_timeout",
                     "detail": "上游 %.0fs 内没有响应" % UPSTREAM_TIMEOUT_S}
    except Exception as exc:
        return 502, {"ok": False, "error": "upstream_unreachable",
                     "detail": _scrub("连不上黑板：%s" % exc)}

    try:
        raw = resp.read(UPSTREAM_MAX_BYTES + 1)
        code = resp.getcode() or 200
    except Exception as exc:
        return 502, {"ok": False, "error": "upstream_unreachable",
                     "detail": _scrub("读取上游响应失败：%s" % exc)}
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if len(raw) > UPSTREAM_MAX_BYTES:
        return 502, {"ok": False, "error": "upstream_too_large",
                     "detail": "上游响应超过 %d 字节" % UPSTREAM_MAX_BYTES}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return 502, {"ok": False, "error": "upstream_bad_json",
                     "detail": _scrub("上游返回的不是 JSON：%s" % exc)}
    if not isinstance(payload, dict):
        return 502, {"ok": False, "error": "upstream_bad_json",
                     "detail": "上游返回的 JSON 不是对象"}

    payload["upstream_ms"] = int((time.time() - started) * 1000)
    return 200 if code == 200 else 502, payload


# --- HTTP 处理 ------------------------------------------------------
#
# 「应答出口 + 路由分发」这层**传输职责**交给 L1 `web_gateway`（阶段2 刀6）：本件
# 只留「路径 → 处理函数」的登记表，处理函数一律 `return (码, 响应体字节, 本次特有
# 的头)`，一行 socket 都不碰。L1 给不了的三样东西收在 `ViewHandler._reply()` 这
# **唯一一个出口**里（迁移前的 `_finish()`）：
#   · 公网面的**固定安全头集合**（SECURITY_HEADERS，顺序即字节顺序）；
#   · `head_only` 语义（HEAD 照发 Content-Length，但一个字节的体都不写）；
#   · 每个响应一行 `<方法> <路径> -> <码> <耗时>ms ip=<ip>` 的日志。

# v1.4 起这页开在公网上，下面这组头**每个响应都带**。顺序就是发出去的字节顺序，
# 一个字节都不许动（迁移前写死在 _finish() 里，现在登记成一处常量）。
SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
    ("X-Frame-Options", "DENY"),            # 不许被套进 iframe 钓鱼
    ("X-Robots-Tag", "noindex, nofollow"),  # 不许被搜索引擎收录
    # 页面完全自包含：不许加载任何外部脚本/样式/图片，fetch 与表单只许打自己。
    # style-src / script-src 留 'unsafe-inline' 是因为整页样式与脚本都内联在
    # HTML 里（自包含、离线可跑）；**外域一律不许**，这才是这条头的重点。
    ("Content-Security-Policy",
     "default-src 'self'; style-src 'self' 'unsafe-inline'; "
     "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
     "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
     "form-action 'self'; frame-ancestors 'none'"),
)

JSON_CT = "application/json; charset=utf-8"
HTML_CT = "text/html; charset=utf-8"
TEXT_CT = "text/plain; charset=utf-8"

GW = web_gateway.Gateway("blackboard_view", version=VERSION, quiet=True)

READ_METHODS = ("GET", "HEAD")                      # 只读看板：两者走同一套处理函数
WRITE_METHODS = ("PUT", "PATCH", "DELETE", "OPTIONS")


# --- 路由登记表：路径 → 处理函数（语义全在 ViewHandler 的同名方法里）-----

@GW.auth
def _gw_gate(req: "web_gateway.Request") -> Any:
    """门禁钩子：L1 在路由前调一次，返回回复即短路（迁移前 _route() 开头那一段）。"""
    return req.view._auth_gate()


@GW.not_found
def _gw_not_found(req: "web_gateway.Request") -> Any:
    """没登记的路径：读方法落 404 文案，写方法照旧落 405（只读纪律）。"""
    if req.method in READ_METHODS:
        return req.view._route_404()
    return req.view._reject_write()


@GW.route(PORTAL_PATH, methods=READ_METHODS)
@GW.route("/portal", methods=READ_METHODS)
def _gw_portal(req):
    return req.view._portal()


@GW.route(BOARD_PATH, methods=READ_METHODS)
def _gw_board(req):
    return req.view._board()


@GW.route("/healthz", methods=READ_METHODS)
def _gw_healthz(req):
    return req.view._healthz()


@GW.route("/favicon.ico", methods=READ_METHODS)
def _gw_favicon(req):
    return req.view._favicon()


@GW.route("/api/health", methods=READ_METHODS)
def _gw_api_health(req):
    return req.view._api_health()


@GW.route("/api/entries", methods=READ_METHODS)
def _gw_api_entries(req):
    return req.view._api_entries()


@GW.route("/api/search", methods=READ_METHODS)
def _gw_api_search(req):
    return req.view._api_search()


@GW.route("/api/sessions", methods=READ_METHODS)
def _gw_api_sessions(req):
    return req.view._api_sessions()


@GW.route("/api/sessions", methods=READ_METHODS, prefix=True)
def _gw_api_session_detail(req):
    """/api/sessions/<id>。迁移前只认**恰好三段**，更深的路径落 404 —— 前缀登记
    会把 /api/sessions/a/b 也收进来，所以这里把段数判断补回去。"""
    if len(req.parts) != 3:
        return req.view._route_404()
    return req.view._api_session_detail(req.parts[2])


@GW.route("/api/tracking", methods=READ_METHODS)
def _gw_api_tracking(req):
    return req.view._api_tracking()


@GW.route("/api/actions", methods=READ_METHODS)
def _gw_api_actions(req):
    return req.view._api_tracking_list("actions")


@GW.route("/api/issues", methods=READ_METHODS)
def _gw_api_issues(req):
    return req.view._api_tracking_list("issues")


@GW.route("/api/shared", methods=READ_METHODS)
def _gw_api_shared(req):
    return req.view._api_shared()


@GW.route("/api/shared/search", methods=READ_METHODS)
def _gw_api_shared_search(req):
    return req.view._api_shared_search()


@GW.route(LOGIN_PATH, methods=("POST",))
def _gw_login_post(req):
    """唯一开了口子的写方法，而且只开在 /login 上（登录 / 改密 / 退出）。

    认证没装（--mode tailnet）时连这个口子都没有 —— 直接 405，口径同 v1.3。
    路径在这里**再比一次原始 path**：迁移前用的是 `urlsplit(path).path == LOGIN_PATH`
    的严格相等，`/login/` 落的是 405 而不是登录页，登记表的尾斜杠归一化不能把它放宽。
    """
    view = req.view
    if view._auth_on() and urllib.parse.urlsplit(view.path).path == LOGIN_PATH:
        return view._login_post()
    return view._reject_write()


# 写方法登记在根路径上，为的是让 L1 的 handler_class() **保留** do_PUT / do_PATCH /
# do_DELETE / do_OPTIONS（连同上面那条 POST 带来的 do_POST）：这几个方法一旦一条路由
# 都没有，基类就会回 501，而迁移前这里是带 Allow 头的 405。别的路径上的写方法由
# _gw_not_found 兜到同一个 _reject_write —— 应答出口始终只有一个。
@GW.route("/", methods=WRITE_METHODS)
def _gw_write_blocked(req):
    return req.view._reject_write()


# 必须在全部 @GW.route 之后取：handler_class() 会按「已登记的方法」删掉多余的
# do_<METHOD>。上面七个方法全登记了，所以一个都不会被删。
_GatewayHandler = GW.handler_class()


class ViewHandler(_GatewayHandler):
    """L1 网关处理器 + 本件特有的三处差异；路由分发与信封全部沿用 L1。

    1. **响应形态**：`sys_version=""` + HTTP/1.1，保住 `Server: blackboard_view/<VERSION>`
       与 keep-alive 语义（L1 默认 HTTP/1.0 且不清 sys_version）。
    2. **应答出口**：重写 `_reply()` —— 公网面的固定安全头集合、`head_only`、一行
       日志，外加「同名头允许发多条」（改密成功那一下要一次下发两个 Set-Cookie，
       L1 的 dict 头会把它们并成一条，字节就不一样了）。
    3. **路径归一**：沿用迁移前的 parts 口径（剥 #fragment、吃掉空段与尾斜杠），
       比 L1 的 `partition("?")` + `rstrip("/")` 宽，`/api//entries` 照样认。
    """

    server_version = "blackboard_view/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    head_only = False
    visitor = None          # type: Optional[Visitor]  —— 每个请求在 _auth_gate 里填

    # --- 基础工具 ---------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        """屏蔽默认 stderr 日志，统一由 _reply() 打一行。"""
        return

    def _elapsed_ms(self) -> float:
        return (time.time() - getattr(self, "_started", time.time())) * 1000.0

    def _handle(self) -> None:
        """L1 的统一入口：请求交给 GW.dispatch，回来的信封由 _reply() 发出去。"""
        self._started = time.time()
        self.head_only = self.command == "HEAD"
        req = web_gateway.Request(self)
        req.view = self
        # 路径归一化对齐迁移前的 `[p for p in path.split("/") if p]`：剥 #fragment、
        # 吃掉空段与尾斜杠。要原始路径的地方一律读 self.path（见 _gw_login_post）。
        req.parts = [p for p in urllib.parse.urlsplit(self.path).path.split("/") if p]
        req.path = "/" + "/".join(req.parts)
        status, body, headers = GW.dispatch(req)
        self._reply(status, body, headers)

    def _reply(self, status: int, body: bytes, headers: Dict[str, Any]) -> None:
        """**本件唯一的应答出口**（迁移前的 _finish()）。

        头的顺序就是发出去的字节顺序：Content-Type → Content-Length → 固定安全头
        集合 → 本次特有的头。同名头（改密成功时的两个 Set-Cookie）在 headers 里以
        list 的形式带进来，这里逐条发出去。
        """
        headers = dict(headers)
        self.send_response(status)
        self.send_header("Content-Type", headers.pop("Content-Type"))
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        for name, value in headers.items():
            for one in (value if isinstance(value, list) else [value]):
                self.send_header(name, one)
        self.end_headers()
        if not self.head_only:
            try:
                self.wfile.write(body)
            except Exception:
                pass
        # 日志一行一条：方法/路径/状态/耗时/来源 IP —— 不记 token、不记响应体
        LOG.info("%s %s -> %d %.1fms ip=%s", self.command,
                 urllib.parse.urlsplit(self.path).path, status, self._elapsed_ms(),
                 self.client_address[0] if self.client_address else "-")

    # --- 应答信封：处理函数只组装，不发送 ----------------------------
    def _out(self, code: int, body: bytes, content_type: str,
             extra_headers: Optional[List[Tuple[str, str]]] = None) -> Tuple[int, bytes, Dict[str, Any]]:
        """装一个 L1 认的信封：公共安全头由 _reply() 补，这里只给本次特有的头。"""
        headers = {"Content-Type": content_type}    # type: Dict[str, Any]
        for name, value in (extra_headers or []):
            if name in headers:                     # 同名头（两个 Set-Cookie）留成 list
                current = headers[name]
                headers[name] = (current if isinstance(current, list)
                                 else [current]) + [value]
            else:
                headers[name] = value
        return code, body, headers

    def _json(self, code: int, payload: Dict[str, Any]) -> Tuple[int, bytes, Dict[str, Any]]:
        """所有 JSON 响应的**唯一**出口：出门前统一按角色再遮一遍。

        放在这里而不是放在每个 _api_* 里，是因为"漏了一个端点"这种事只要发生一次
        就是泄密；收口在出口上，将来加端点也自动被管住。
        role=None（tailnet 直连 / 未装认证）时 apply_role 原样返回，一个字节不动。
        """
        return self._out(code, json_bytes(apply_role(payload, self.role)), JSON_CT)

    def _html(self, code: int, text: str,
              extra_headers: Optional[List[Tuple[str, str]]] = None) -> Tuple[int, bytes, Dict[str, Any]]:
        return self._out(code, text.encode("utf-8"), HTML_CT, extra_headers)

    def _redirect(self, location: str,
                  extra_headers: Optional[List[Tuple[str, str]]] = None) -> Tuple[int, bytes, Dict[str, Any]]:
        """302 + 空体。Location 只会是本服务里的相对路径（见 safe_next）。"""
        headers = [("Location", location)] + list(extra_headers or [])
        return self._out(302, b"", TEXT_CT, headers)

    # --- 认证：取请求上下文 -----------------------------------------
    @property
    def role(self) -> Optional[str]:
        visitor = getattr(self, "visitor", None)
        return visitor.role if visitor is not None else None

    def _auth_on(self) -> bool:
        """这个请求要不要过门禁。

        两个条件都成立才算：mode=public **且**认证真的装起来了。
        public 模式下 main() 装不上认证是直接 return 2 不启动的，所以线上不存在
        "public 但 auth=None"；留这个 and 只是为了让进程内直接用 make_server 的
        调用方（单元测试、别的脚本）保持 v1.3 的行为。
        """
        return Runtime.mode == "public" and Runtime.auth is not None

    def _peer_ip(self) -> str:
        """**对端 IP** —— TCP 那头的地址，内核给的，谁都伪造不了。

        免登录白名单（tailnet）只认它，一个字节的请求头都不看。X-Forwarded-For /
        CF-Connecting-IP 是外层写的，伪造一个 `CF-Connecting-IP: <tailnet-ip>` 就白拿
        内网待遇 —— 所以**鉴权这一路永远只走这个函数**。见上面「受信反代」那段。
        """
        return self.client_address[0] if self.client_address else ""

    def _ua(self) -> str:
        return (self.headers.get("User-Agent") or "")[:300]

    def _claimed_ip(self) -> str:
        """外层反代**声称**的访客 IP，原样（不截断、不校验）。信不信见 _client_ip()。"""
        raw = (self.headers.get("CF-Connecting-IP")
               or (self.headers.get("X-Forwarded-For") or "").split(",")[0])
        return raw.strip()

    def _fwd_ip(self) -> str:
        """给审计看的"外层说他是谁"，截到 64 字符，仅供人工对照，不参与任何判断。"""
        return self._claimed_ip()[:TRUST_HEADER_MAX]

    def _client_ip(self) -> str:
        """**真实访客 IP** —— 锁定键与审计 IP 都用它（不是 _peer_ip，也不是拼接串）。

        对端 IP 落在 --trust-proxy 里才采信外层的头，而且先当 IP 字面量校验一遍：
        空 / 非法 / 超 64 字符一律退回对端 IP，绝不把垃圾字符串当锁定键。
        --trust-proxy 默认空，这时这个函数逐字节等价于 _peer_ip()。
        """
        peer = self._peer_ip()
        if not ip_in_nets(peer, Runtime.trust_proxy):
            return peer
        claimed = self._claimed_ip()
        if not claimed or len(claimed) > TRUST_HEADER_MAX:
            return peer
        addr = parse_ip(claimed)
        return str(addr) if addr is not None else peer

    def _audit_ip(self) -> str:
        """「原始对端(via 外层声称)」的对照串 —— **只给人看，只进 detail**。

        这个拼接串**绝不能当锁定键**：同一个访客改一下头就换一把锁，等于没锁。
        键一律用 _client_ip()。
        """
        peer, fwd = self._peer_ip(), self._fwd_ip()
        return "%s(via %s)" % (peer, fwd) if fwd and fwd != peer else peer

    def _ip_note(self) -> str:
        """审计 detail 里附的「原始对端 + 外层声称」对照；两者一致时不啰嗦。

        不管信不信这个头都记 —— 对端明明不在 --trust-proxy 里却一直有人发
        CF-Connecting-IP，本身就是值得看见的信号。
        """
        peer, shown = self._peer_ip(), self._audit_ip()
        return "" if shown == peer else "来源 " + shown

    def _cookie_domain(self) -> Optional[str]:
        """按请求 Host 推导 cookie 域（见模块级 cookie_domain）。"""
        return cookie_domain(self.headers.get("Host"))

    def _secure(self) -> bool:
        """这条连接算不算 HTTPS：配置强制打开，或者外层反代说 proto=https。"""
        if Runtime.secure_cookie:
            return True
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https"

    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie")
        if not raw:
            return ""
        try:
            jar = http.cookies.SimpleCookie()
            jar.load(raw)
        except http.cookies.CookieError:
            return ""                   # 畸形 cookie 当没有，不能因此 500
        morsel = jar.get(name)
        return morsel.value if morsel else ""

    def _audit(self, event: str, username: Optional[str], detail: str) -> None:
        """审计一律带 IP 与 UA；认证没装就什么都不做。

        入库的 ip 列是 **_client_ip()**（干净的 IP 字面量，能直接 GROUP BY、能跟
        锁定键对上）；"对端 vs 外层声称"的对照挂在 detail 末尾，供人工核对。
        """
        if Runtime.auth is None:
            return
        note = self._ip_note()
        if note:
            detail = (detail + " ｜ " + note) if detail else note
        try:
            Runtime.auth.audit(event, username, self._client_ip(), self._ua(), detail)
        except Exception as exc:        # 审计失败绝不能把请求带崩
            LOG.error("审计失败 event=%s: %s", event, _scrub(exc))

    def _resolve_visitor(self) -> Optional[Visitor]:
        """这个请求背后是谁：tailnet 直连 / 有效会话 / 都不是（None）。

        ⚠️ **127.0.0.1 不在免登录名单里** —— cloudflared 正是从本机 127.0.0.1 回连
        本服务，放行回环地址等于给整个公网开一道后门。
        ⚠️ 这里用的是 **_peer_ip()，不是 _client_ip()**，而且就算配了 --trust-proxy
        也不改：免登录白名单一旦看头，伪造一个 `CF-Connecting-IP: <tailnet-ip>` 就能
        白拿内网待遇。头只配决定"这条记录记在谁头上"，不配决定"放不放他进来"。
        """
        ip = self._peer_ip()
        if view_auth.in_tailnet(ip):
            return Visitor("tailnet", None, "tailnet")
        cookie = self._cookie(view_auth.SESSION_COOKIE)
        if not cookie:
            return None
        principal, _reason = Runtime.auth.check_cookie(cookie, self._client_ip(), self._ua())
        if principal is None:
            return None
        return Visitor(principal.username, principal.role, "session")

    def _drain_body(self) -> None:
        """把请求体排空，别让 keep-alive 连接错位；太大就直接断。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return
        remaining = min(length, DRAIN_MAX)
        if remaining < length:
            self.close_connection = True
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _query(self) -> Dict[str, List[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def _first(self, query: Dict[str, List[str]], name: str) -> str:
        values = query.get(name) or []
        return (values[0] or "").strip() if values else ""

    # --- 门禁与路由目标（登记表见文件上方的 @GW.route）----------------
    def _auth_gate(self) -> Any:
        """门禁。这几行的顺序就是安全边界，改之前先想清楚。

        **只对读方法生效**：迁移前这段写在 _route() 开头，而 _route() 只从
        do_GET / do_HEAD 进来；写方法（含 POST /login）从来不过这道门。
        """
        if self.command not in READ_METHODS:
            return None
        path = urllib.parse.urlsplit(self.path).path
        parts = [p for p in path.split("/") if p]
        self.visitor = None

        if not self._auth_on():
            return None
        if parts == ["healthz"]:
            return None                     # 探活免登录：不然 systemd/隧道判不了死活
        if path == LOGIN_PATH:
            return self._login_get()
        self.visitor = self._resolve_visitor()
        if self.visitor is None:
            if parts != ["favicon.ico"]:    # 图标被挡不值得记一条审计
                self._audit("access_denied", None,
                            "未登录访问 %s %s，已跳登录页" % (self.command, path[:120]))
            return self._redirect(LOGIN_PATH + "?next="
                                  + urllib.parse.quote(safe_next(self.path), safe="/?=&"))
        if path == ACCOUNT_PATH:
            return self._account_get()
        return None

    def _reject_write(self) -> Any:
        """只读看板：一切写方法都到这里。

        v1.4 起唯一的例外是 POST /login（见 _gw_login_post）—— 那是登录表单，
        不碰黑板。这条 405 **不看登录状态**：登录了也一样写不进去，这是只读纪律，
        不是权限。
        """
        self._drain_body()
        self._audit("write_blocked", None,
                    "拦下写请求 %s %s" % (self.command,
                                          urllib.parse.urlsplit(self.path).path[:120]))
        return self._out(405, json_bytes({
            "ok": False,
            "error": "method_not_allowed",
            "detail": "看板只读：只接受 GET / HEAD，不提供任何写入黑板的入口",
        }), JSON_CT, [("Allow", "GET, HEAD")])

    # --- 首页 = webPortal 卡片墙（v1.5）------------------------------
    # 只做入口、不展示任何黑板数据；未登录的人仍然被 _auth_gate 挡在 /login。
    def _portal(self) -> Any:
        """老书签兼容：白板时代的带参链接（/?tab=... ）302 到 /board；/portal 不带这条。"""
        parts = [p for p in urllib.parse.urlsplit(self.path).path.split("/") if p]
        if parts != ["portal"] and urllib.parse.urlsplit(self.path).query:
            return self._redirect(BOARD_PATH)
        bar, headers = self._userbar()
        return self._html(200, render_portal(bar), headers)

    def _board(self) -> Any:
        bar, headers = self._userbar()
        return self._html(200, render_page(bar), headers)

    def _healthz(self) -> Any:
        return self._json(200, {
            "ok": True, "version": VERSION, "service": "blackboard_view",
            "upstream": Runtime.upstream, "token_loaded": Runtime.token_loaded,
            "refresh_seconds": Runtime.refresh_seconds,
        })

    def _favicon(self) -> Any:
        return self._out(204, b"", "image/x-icon")

    def _api_health(self) -> Any:
        code, payload = upstream_get("/healthz", {})
        return self._json(code, payload)

    def _route_404(self) -> Any:
        return self._json(404, {"ok": False, "error": "not_found",
                                "detail": "看板只有 / 、/api/health、/api/entries、"
                                          "/api/search、/api/sessions、/api/tracking、"
                                          "/api/actions、/api/issues、/api/shared、"
                                          "/api/shared/search"})

    def _api_entries(self) -> Any:
        """GET /api/entries?topic=&kind=&author=&since=&limit= → 透传上游 /entries。"""
        query = self._query()
        params = {
            "topic": self._first(query, "topic"),
            "kind": self._first(query, "kind"),
            "author": self._first(query, "author"),
            "since": self._first(query, "since"),
            "limit": clamp_limit(self._first(query, "limit") or None),
        }
        code, payload = upstream_get("/entries", params)
        if code == 200:
            payload["mode"] = "entries"
        return self._json(code, payload)

    def _api_search(self) -> Any:
        """GET /api/search?q=&topic=&limit= → 透传上游 /search。

        q 为空不去打 /search（上游会回 400，对看板没意义）：直接退回 /entries，
        页面上"清空搜索框"就等于"看最新条目"。
        """
        query = self._query()
        q = self._first(query, "q")[:QUERY_MAX]
        limit = clamp_limit(self._first(query, "limit") or None)
        topic = self._first(query, "topic")

        if not q:
            code, payload = upstream_get("/entries", {"topic": topic, "limit": limit})
            if code == 200:
                payload["mode"] = "entries"
            return self._json(code, payload)

        code, payload = upstream_get("/search", {"q": q, "topic": topic, "limit": limit})
        if code == 200:
            payload.setdefault("mode", "search")
        return self._json(code, payload)

    def _api_sessions(self) -> Any:
        """GET /api/sessions?state=&topic=&limit=&since= → 透传上游 /sessions。"""
        query = self._query()
        params = {
            "state": self._first(query, "state"),
            "topic": self._first(query, "topic"),
            "since": self._first(query, "since"),
            "limit": clamp_limit(self._first(query, "limit") or None),
        }
        code, payload = upstream_get("/sessions", params)
        return self._json(code, payload)

    def _api_session_detail(self, session_id: str) -> Any:
        """GET /api/sessions/<id> → 透传上游 /sessions/<id>。上游 404 → 本服务 404。"""
        sid = urllib.parse.unquote(session_id).strip()
        if not sid:
            return self._json(404, {"ok": False, "error": "not_found",
                                    "detail": "没有这场会议"})
        params = {"limit": clamp_limit(self._first(self._query(), "limit") or None,
                                       MAX_LIMIT)}
        code, payload = upstream_get("/sessions/" + urllib.parse.quote(sid, safe=""),
                                     params, allow_status=(404,))
        return self._json(code, payload)

    def _track_params(self) -> Dict[str, Any]:
        """跟踪类端点共用的入参整形：只放行 status/owner/overdue/limit/kind，别的一概不转发。

        overdue 在这里就归一成 "1" / ""（空的会被 upstream_get 丢掉）—— 上游只认 0/1，
        浏览器传个 on 也不该让上游回 400。
        """
        query = self._query()
        overdue = self._first(query, "overdue").lower()
        return {
            "status": self._first(query, "status"),
            "owner": self._first(query, "owner"),
            "kind": self._first(query, "kind"),
            "overdue": "1" if overdue in ("1", "true", "yes", "on") else "",
            "limit": clamp_limit(self._first(query, "limit") or None, MAX_LIMIT),
        }

    def _api_tracking(self) -> Any:
        """GET /api/tracking?overdue=&limit= → 透传上游 /tracking（两张清单 + 计数）。"""
        params = self._track_params()
        params.pop("status", None)          # /tracking 不收 status，两张表的枚举不一样
        params.pop("kind", None)            # kind 只有 /issues 收
        code, payload = upstream_get("/tracking", params)
        return self._json(code, payload)

    def _api_tracking_list(self, kind: str) -> Any:
        """GET /api/actions|/api/issues?status=&owner=&overdue=&kind=&limit= → 透传上游同名端点。"""
        params = self._track_params()
        if kind != "issues":
            params.pop("kind", None)        # 落实事项没有"性质"这一说
        code, payload = upstream_get("/" + kind, params)
        return self._json(code, payload)

    # --- 共享内容 --------------------------------------------------
    def _shared_params(self) -> Dict[str, Any]:
        """共享内容端点共用的入参整形：只放行 scope/kind/owner/tag/limit。"""
        query = self._query()
        scope = self._first(query, "scope")
        if scope not in SHARED_SCOPES:
            scope = ""                      # 看不懂的 scope 当"全都要"，别让上游 400
        return {
            "scope": scope,
            "kind": self._first(query, "kind"),
            "owner": self._first(query, "owner"),
            "tag": self._first(query, "tag"),
            "limit": clamp_limit(self._first(query, "limit") or None, MAX_LIMIT),
        }

    def _shared_out(self, code: int, payload: Dict[str, Any]) -> Any:
        """共享内容的统一出口：上游没有这个端点 -> 502 shared_unavailable（页面降级）。

        其余情况照常，但**一定先过一遍 mask_shared** —— sensitive 且未授权的条目
        在这一行之后就没有标题和正文了，浏览器永远拿不到那几个字节。
        """
        if code == 404:
            return self._json(502, {
                "ok": False, "error": "shared_unavailable",
                "detail": "上游还没有 /shared 端点：共享内容服务尚未上线",
            })
        if code == 200:
            payload = mask_shared(payload, self.role)
        return self._json(code, payload)

    def _api_shared(self) -> Any:
        """GET /api/shared?scope=&kind=&owner=&tag=&limit= → 透传上游 /shared。"""
        code, payload = upstream_get("/shared", self._shared_params(),
                                     allow_status=(404,))
        return self._shared_out(code, payload)

    def _api_shared_search(self) -> Any:
        """GET /api/shared/search?q=&semantic=&... → 透传上游 /shared/search。

        q 为空不去打 /search（上游会回 400）：直接退回 /shared，跟黑板搜索同一口径。
        semantic 在这里归一成 "1" / ""，上游只认 0/1。
        """
        query = self._query()
        q = self._first(query, "q")[:QUERY_MAX]
        params = self._shared_params()
        if not q:
            code, payload = upstream_get("/shared", params, allow_status=(404,))
            return self._shared_out(code, payload)
        semantic = self._first(query, "semantic").lower()
        params["q"] = q
        params["semantic"] = "1" if semantic in ("1", "true", "yes", "on") else ""
        code, payload = upstream_get("/shared/search", params, allow_status=(404,))
        return self._shared_out(code, payload)

    # --- 登录 / 账号（本文件里唯一会"写"的一小块，写的只有账号库，不碰黑板）---
    def _csrf(self) -> Tuple[str, List[Tuple[str, str]]]:
        """拿一个可用的 CSRF token：手上那个还有效就接着用，否则现发一个。

        复用是有意的 —— 每渲染一次页面就换一次 token，会让"先开首页、再开账号页、
        回头点首页那个退出按钮"这种正常操作莫名其妙地 403。
        """
        current = self._cookie(view_auth.CSRF_COOKIE)
        if view_auth.unsign_token(current, Runtime.auth.key, purpose=b"csrf"):
            return current, []
        token = view_auth.issue_csrf(Runtime.auth.key)
        return token, [cookie_header(view_auth.CSRF_COOKIE, token, self._secure(),
                                                domain=self._cookie_domain())]

    def _userbar(self) -> Tuple[str, List[Tuple[str, str]]]:
        """看板右上角那条「谁在看 · 改密 · 退出」。没装认证 / 没登录就是空串。"""
        visitor = self.visitor
        if not self._auth_on() or visitor is None:
            return "", []
        if visitor.source == "tailnet":
            return render_userbar("内网直连", "tailnet 免登录", ""), []
        token, headers = self._csrf()
        who = view_auth.ROLE_CN.get(visitor.role or "", visitor.role or "-")
        return render_userbar(visitor.username, who, token), headers

    def _login_page(self, code: int, error: str = "", notice: str = "",
                    next_url: str = "/", username: str = "") -> Any:
        token, headers = self._csrf()
        return self._html(code, render_login(token, next_url, error, notice, username),
                          headers)

    def _login_get(self) -> Any:
        """GET /login —— 免登录三个口子之一。已登录的人来了也照给表单（顺带给账号入口）。"""
        self.visitor = self._resolve_visitor()
        next_url = safe_next(self._first(self._query(), "next"))
        if self.visitor is not None and self.visitor.source == "session":
            return self._login_page(200,
                                    notice="当前已登录为 %s，可直接进看板，或在下面换个账号登录"
                                           % self.visitor.username, next_url=next_url,
                                    username=self.visitor.username)
        return self._login_page(200, next_url=next_url)

    def _account_get(self) -> Any:
        """GET /account —— 改密与退出。只有登录会话能进（tailnet 直连没有账号可改）。"""
        visitor = self.visitor
        if visitor is None or visitor.source != "session":
            return self._redirect(LOGIN_PATH + "?next=" + urllib.parse.quote(ACCOUNT_PATH))
        token, headers = self._csrf()
        return self._html(200, render_account(visitor, token,
                                              self._first(self._query(), "msg")), headers)

    def _read_form(self) -> Optional[Dict[str, str]]:
        """读登录表单。**这是全服务唯一读请求体的地方**，所以卡得格外死：
        只收 x-www-form-urlencoded，长度上限 FORM_MAX_BYTES，超了直接当没有。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if length <= 0 or length > FORM_MAX_BYTES or ctype != "application/x-www-form-urlencoded":
            self._drain_body()
            return None
        raw = self.rfile.read(length)
        parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        return {k: (v[0] if v else "") for k, v in parsed.items()}

    def _login_post(self) -> Any:
        """POST /login —— 看板上唯一的写入口，三个动作靠 action 区分，都要过 CSRF。"""
        form = self._read_form()
        if form is None:
            self._audit("login_fail", None, "表单被拒：类型不对或超过 %d 字节" % FORM_MAX_BYTES)
            return self._login_page(400, error="表单格式不对，请从登录页重新提交")

        action = (form.get("action") or "login").strip().lower()
        if action not in LOGIN_ACTIONS:
            action = "login"
        username = (form.get("user") or "").strip()[:64]
        next_url = safe_next(form.get("next"))
        # 登录 / 改密 / 退出全走 _client_ip()：这个值同时是 LockTracker 的**锁定键**，
        # 必须是干净的 IP 字面量。拿 "127.0.0.1(via 8.8.8.8)" 当键的话，坏人改一下
        # 头就换一把锁，5 次失败锁定形同虚设。
        ip, ua = self._client_ip(), self._ua()

        if not view_auth.check_csrf(self._cookie(view_auth.CSRF_COOKIE),
                                    form.get(CSRF_FIELD), Runtime.auth.key):
            self._audit("csrf_reject", username or None, "CSRF 校验不通过：action=%s" % action)
            return self._login_page(403, error="表单已过期或来源不对，请重新提交",
                                    next_url=next_url, username=username)

        if action == "logout":
            Runtime.auth.logout(self._cookie(view_auth.SESSION_COOKIE), ip, ua)
            return self._redirect(LOGIN_PATH, [cookie_header(view_auth.SESSION_COOKIE, "",
                                          self._secure(), delete=True,
                                          domain=self._cookie_domain())])

        if action == "passwd":
            self.visitor = self._resolve_visitor()
            if self.visitor is None or self.visitor.source != "session":
                return self._redirect(LOGIN_PATH + "?next="
                                      + urllib.parse.quote(ACCOUNT_PATH))
            ok, message = Runtime.auth.change_password(
                self.visitor.username, form.get("old") or "", form.get("new") or "", ip, ua)
            if not ok:
                token, headers = self._csrf()
                return self._html(200, render_account(self.visitor, token, "", message),
                                  headers)
            # 改密成功 = 所有会话（含自己手上这张）作废，所以把 cookie 一起清掉
            return self._finish_passwd(message)

        ok, cookie_value, reason = Runtime.auth.login(
            username, form.get("password") or "", ip, ua)
        if not ok:
            return self._login_page(200, error=reason or "账号或口令不正确",
                                    next_url=next_url, username=username)
        return self._redirect(next_url, [cookie_header(view_auth.SESSION_COOKIE, cookie_value,
                                                self._secure(),
                                                max_age=SESSION_MAX_AGE_S,
                                                domain=self._cookie_domain())])

    def _finish_passwd(self, message: str) -> Any:
        """改密成功后的收尾：清会话 cookie + 回登录页，让人用新口令重来一次。"""
        token, headers = self._csrf()
        headers = list(headers) + [cookie_header(view_auth.SESSION_COOKIE, "",
                                          self._secure(), delete=True,
                                          domain=self._cookie_domain())]
        return self._html(200, render_login(token, "/", "", message, ""), headers)


# --- 页面 -----------------------------------------------------------

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>团队黑板 · 共享内容看板</title>
<style>
:root{
  --bg:#0f1116; --panel:#161a22; --panel2:#1c212b; --line:#2a3140;
  --fg:#e7eaf0; --dim:#98a2b3; --accent:#5b9cff; --ok:#3fb950;
  --warn:#d29922; --err:#f85149; --chip:#232a36;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--fg);
  font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC","Source Han Sans SC",
    system-ui,-apple-system,"Segoe UI",sans-serif;
  /* 流体字号：手机上不至于小到看不清，桌面上也不会糊一屏大字 */
  font-size:clamp(13px,2.4vw,14px); line-height:1.7;
  overflow-x:hidden;                     /* 兜底：整页永远不横向滚 */
}
.wrap{max-width:1180px;margin:0 auto;padding:clamp(10px,2.2vw,16px) clamp(10px,2.2vw,16px) 48px}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:10px}
h1{font-size:clamp(16px,3.8vw,19px);margin:0;font-weight:600;letter-spacing:.5px}
.sub{color:var(--dim);font-size:12px}
.lights{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto}
.light{display:inline-flex;align-items:center;gap:6px;background:var(--panel2);
  border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:12px;color:var(--dim)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim);flex:none}
.dot.ok{background:var(--ok)} .dot.bad{background:var(--err)} .dot.warn{background:var(--warn)}
.light b{color:var(--fg);font-weight:600}

#banner{display:none;margin:10px 0;padding:8px 12px;border-radius:6px;
  background:rgba(210,153,34,.14);border:1px solid var(--warn);color:#f0d68c;font-size:13px}
#banner.on{display:block}

.bar{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:10px 12px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.bar .grow{flex:1 1 260px;display:flex;gap:8px;align-items:center;min-width:240px}
input[type=text],select{
  background:var(--panel2);color:var(--fg);border:1px solid var(--line);
  border-radius:6px;padding:6px 10px;font:inherit;font-size:13px;outline:none}
input[type=text]{width:100%}
input[type=text]:focus,select:focus{border-color:var(--accent)}
button{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
  border-radius:6px;padding:6px 12px;font:inherit;font-size:13px;cursor:pointer;
  flex:none;white-space:nowrap}
button:hover{border-color:var(--accent);color:#fff}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button.primary:hover{filter:brightness(1.1)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden;flex:none}
.seg button{border:0;border-radius:0;padding:6px 12px;background:transparent;color:var(--dim)}
.seg button.on{background:var(--accent);color:#fff;font-weight:600}
label.chk{display:inline-flex;align-items:center;gap:6px;color:var(--dim);font-size:13px;cursor:pointer}
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:8px}
.filters .lb{color:var(--dim);font-size:12px}
.count{color:var(--dim);font-size:13px;margin-left:auto}
.count b{color:var(--fg)}

#list{margin-top:14px;display:flex;flex-direction:column;gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--line);
  border-radius:8px;padding:10px 12px;cursor:pointer}
.card:hover{border-color:#3a4152;border-left-color:var(--accent)}
.card.open{border-left-color:var(--accent)}
.meta{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--dim)}
.id{font-family:Consolas,"Courier New",monospace;color:var(--accent);font-weight:700}
.chip{background:var(--chip);border:1px solid var(--line);border-radius:4px;padding:1px 7px;color:var(--fg)}
.chip.author{color:#7ee787} .chip.topic{color:#79c0ff} .chip.kind{color:#d2a8ff}
.chip.score{color:#ffb86b}
.time{margin-left:auto;white-space:nowrap}
.content{margin-top:7px;white-space:pre-wrap;word-break:break-word;font-size:13.5px;color:#dfe4ec}
.content.clip{max-height:4.8em;overflow:hidden;
  -webkit-mask-image:linear-gradient(#000 60%,transparent);mask-image:linear-gradient(#000 60%,transparent)}
.more{margin-top:4px;color:var(--accent);font-size:12px}
.refs{margin-top:7px;display:flex;gap:6px;flex-wrap:wrap}
.ref{background:var(--chip);border:1px dashed var(--line);border-radius:4px;
  padding:0 6px;font-size:11.5px;color:var(--dim)}
.empty{text-align:center;color:var(--dim);padding:44px 12px;border:1px dashed var(--line);border-radius:8px}
footer{margin-top:20px;color:var(--dim);font-size:12px;text-align:center}

/* --- 一级导航：会议 / 共享内容 / 跟踪（手机上平分一行，触摸目标 ≥44px）--- */
.tabs-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:10px 0 2px}
.tabs{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.tabs button{border:0;border-radius:0;padding:8px 20px;background:transparent;color:var(--dim);
  font-size:clamp(13px,2.8vw,14px);min-height:40px}
.tabs button.on{background:var(--accent);color:#fff;font-weight:600}
.tabs.sub button{padding:6px 14px;font-size:clamp(12px,2.6vw,13px);min-height:36px}
.pane{display:none} .pane.on{display:block}
.bar .lb{color:var(--dim);font-size:12px;flex:none}

/* --- 会议视图 --- */
.meetgrid{display:grid;grid-template-columns:330px 1fr;gap:12px;margin-top:14px;align-items:start}
.mlist{display:flex;flex-direction:column;gap:8px;max-height:74vh;overflow:auto}
.scard{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--line);
  border-radius:8px;padding:8px 10px;cursor:pointer}
.scard:hover{border-color:#3a4152}
.scard.on{border-left-color:var(--accent);background:var(--panel2)}
.scard .q{margin:5px 0;color:var(--dim);font-size:12.5px;max-height:3.4em;overflow:hidden;
  word-break:break-word}
.badge{border-radius:4px;padding:1px 7px;font-size:11.5px;border:1px solid var(--line);color:var(--dim)}
.badge.running{color:#79c0ff;border-color:#2f5f96}
.badge.converged{color:#7ee787;border-color:#2c6b3f}
.badge.timeout{color:#ffb86b;border-color:#8a6520}
.badge.aborted{color:#ff8a80;border-color:#8a2f2f}
.badge.ok{color:#7ee787;border-color:#2c6b3f}
.badge.absent{color:#98a2b3}
.badge.error{color:#ff8a80;border-color:#8a2f2f}
.mdetail{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:12px 14px;min-height:220px}
.qfull{margin-top:6px;font-size:14.5px;color:#eef2f8;white-space:pre-wrap;word-break:break-word}
.concl{margin-top:10px;padding:10px 12px;border-radius:8px;border:1px solid #2c6b3f;
  background:rgba(63,185,80,.10);white-space:pre-wrap;word-break:break-word;color:#dff5e3}
.concl.none{border-color:var(--line);background:var(--panel2);color:var(--dim)}
.roundhead{margin:16px 0 8px;color:var(--accent);font-size:13px;font-weight:600;
  border-bottom:1px solid var(--line);padding-bottom:4px}
.turn{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:8px;
  background:var(--panel2)}
.turn .why{margin-top:6px;color:#f0b6b0;font-size:12.5px;white-space:pre-wrap;word-break:break-word}
.linkid{background:var(--chip);border:1px solid var(--accent);border-radius:4px;padding:0 6px;
  font-size:11.5px;color:var(--accent);cursor:pointer}
.card.hit{border-color:var(--accent)}

/* --- 会议四要素：目标 / 议程 / 当场确认 / 落实·遗留 --- */
.goal{margin-top:8px;padding:8px 12px;border-radius:8px;border:1px solid #2f5f96;
  background:rgba(91,156,255,.10);color:#dce9ff;font-size:15px;font-weight:600;
  white-space:pre-wrap;word-break:break-word}
.goal.none{border-color:var(--line);background:var(--panel2);color:var(--dim);font-weight:400}
.sect{margin:14px 0 6px;color:var(--fg);font-size:13px;font-weight:600;
  border-bottom:1px solid var(--line);padding-bottom:4px;display:flex;gap:8px;align-items:center}
.sect .n{color:var(--dim);font-weight:400;font-size:12px}
.item{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:8px;
  background:var(--panel2)}
.item .t{font-size:13.5px;color:#eef2f8;word-break:break-word}
.item .k{color:var(--dim);font-size:12.5px;margin-top:4px;white-space:pre-wrap;word-break:break-word}
.item .k b{color:#cfd6e2;font-weight:600}
.badge.pending{color:#98a2b3}
.badge.concluded{color:#7ee787;border-color:#2c6b3f}
.badge.deferred{color:#ffb86b;border-color:#8a6520}
.badge.agree{color:#7ee787;border-color:#2c6b3f}
.badge.object{color:#ff8a80;border-color:#8a2f2f}
.badge.abstain{color:#98a2b3}
.badge.late{color:#ff8a80;border-color:#8a2f2f}
.badge.open{color:#79c0ff;border-color:#2f5f96}
.badge.doing{color:#ffb86b;border-color:#8a6520}
.badge.done{color:#7ee787;border-color:#2c6b3f}
.badge.dropped{color:#98a2b3}
.badge.watching{color:#ffb86b;border-color:#8a6520}
.badge.closed{color:#7ee787;border-color:#2c6b3f}
.vote{margin-top:8px;padding:8px 12px;border-radius:8px;border:1px solid var(--line);
  background:var(--panel2);color:var(--dim);font-size:13px}
.vote.pass{border-color:#2c6b3f;background:rgba(63,185,80,.10);color:#dff5e3}
.vote.fail{border-color:var(--warn);background:rgba(210,153,34,.12);color:#f0d68c}
.vote .names{margin-top:4px;color:var(--fg);font-size:12.5px;word-break:break-word}
.warnline{margin-top:8px;color:#f0d68c;font-size:12.5px}

/* --- 出席率：满员=绿，缺一个人就黄；缺席成员的名字一律红标 --- */
.att{margin-top:8px;padding:7px 12px;border-radius:8px;border:1px solid #2c6b3f;
  background:rgba(63,185,80,.08);color:#dff5e3;font-size:13px}
.att.bad{border-color:var(--warn);background:rgba(210,153,34,.12);color:#f0d68c}
.att.none{border-color:var(--line);background:var(--panel2);color:var(--dim)}
.miss{color:#ff8a80;font-weight:600;word-break:break-word;font-size:12.5px;margin-top:4px}
.badge.att{border-color:#2c6b3f;color:#7ee787;background:none;padding:1px 7px;margin:0}
.badge.attbad{color:#f0d68c;border-color:#8a6520}
.badge.defect{color:#ff8a80;border-color:#8a2f2f}
.badge.issue{color:#98a2b3}

/* --- 跟踪页签：跨会总清单 --- */
.tsec{margin-top:16px}
.trow{display:grid;grid-template-columns:96px 1fr 132px 72px 128px;gap:8px;
  padding:7px 10px;border:1px solid var(--line);border-radius:8px;margin-bottom:6px;
  background:var(--panel);align-items:start;font-size:13px;word-break:break-word}
.trow.head{background:var(--panel2);color:var(--dim);font-size:12px;font-weight:600;
  position:sticky;top:0}
.trow.late{border-color:#8a2f2f;background:rgba(248,81,73,.09)}
.trow .who{color:#7ee787}
.trow .due{color:var(--dim)}
.trow.late .due{color:#ff8a80;font-weight:600}
.trow .src{color:var(--accent);cursor:pointer;font-size:12px}
.sum{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.sum .pill{background:var(--panel2);border:1px solid var(--line);border-radius:999px;
  padding:2px 12px;font-size:12.5px;color:var(--dim)}
.sum .pill b{color:var(--fg)}
.sum .pill.late b{color:#ff8a80}

/* --- 会议模块：月历（仿日历 App：有会议的日子打点 + 场次角标）--- */
.calbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:12px}
.calbar .mon{font-size:clamp(14px,3.4vw,17px);font-weight:600;letter-spacing:.5px}
.calbar button{min-width:44px;min-height:40px}
.cal{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin-top:10px}
.cal .wd{text-align:center;color:var(--dim);font-size:12px;padding:2px 0}
.day{position:relative;min-height:62px;border:1px solid var(--line);border-radius:8px;
  background:var(--panel);padding:4px 6px;cursor:pointer}
.day:hover{border-color:#3a4152}
.day.pad{background:transparent;border-color:transparent;cursor:default}
.day.has{border-color:#2f5f96}
.day.on{border-color:var(--accent);background:var(--panel2)}
.day .dn{font-size:13px;color:var(--fg)}
.day.pad .dn{color:#3a4152}
.day .mark{position:absolute;left:6px;bottom:8px;width:7px;height:7px;border-radius:50%;
  background:var(--accent)}
.day .cnt{position:absolute;right:4px;bottom:4px;background:var(--accent);color:#fff;
  border-radius:999px;font-size:11px;line-height:18px;height:18px;min-width:18px;
  text-align:center;padding:0 5px;font-weight:600}
.day.late .cnt{background:var(--err)}
/* 手机上的月历替身：只列"当月有会议的日期"，一行一张卡，点得到 */
.callist{display:none;flex-direction:column;gap:8px;margin-top:10px}
.dcard{border:1px solid var(--line);border-radius:10px;background:var(--panel);
  padding:10px 12px;min-height:44px;cursor:pointer;display:flex;gap:10px;
  align-items:center;flex-wrap:wrap}
.dcard.on{border-color:var(--accent);background:var(--panel2)}
.dcard .dd{font-size:15px;font-weight:600}
.dayhead{margin:14px 0 6px;color:var(--accent);font-size:13px;font-weight:600;
  border-bottom:1px solid var(--line);padding-bottom:4px}

/* --- 共享内容模块 --- */
.grp{border:1px solid var(--line);border-radius:10px;background:var(--panel);
  padding:10px 12px;margin-bottom:12px}
.grp>.gh{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13.5px;
  font-weight:600;margin-bottom:6px}
.grp>.gh .n{color:var(--dim);font-weight:400;font-size:12px}
.sitem{border:1px solid var(--line);border-left:3px solid var(--line);border-radius:8px;
  background:var(--panel2);padding:9px 11px;margin-bottom:8px;word-break:break-word}
.sitem.expired{border-left-color:var(--err);background:rgba(248,81,73,.08)}
.sitem.soon{border-left-color:var(--warn);background:rgba(210,153,34,.08)}
.sitem.review{border-left-color:var(--warn)}
.sitem.revoked{opacity:.55;border-left-color:var(--dim)}
.sitem.locked{border-style:dashed;border-left-color:var(--warn)}
.sitem .st{font-size:14px;color:#eef2f8;font-weight:600}
.kvs{display:flex;gap:4px 14px;flex-wrap:wrap;font-size:12.5px;color:var(--dim);margin-top:6px}
.kvs b{color:#cfd6e2;font-weight:600}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.badge.expired{color:#ff8a80;border-color:#8a2f2f}
.badge.soon{color:#ffb86b;border-color:#8a6520}
.badge.review{color:#ffb86b;border-color:#8a6520}
.badge.revoked{color:#98a2b3}
.badge.locked{color:#ffb86b;border-color:#8a6520}
.badge.ttl{color:#79c0ff;border-color:#2f5f96}
.hist{margin-top:6px;font-size:12.5px;color:var(--dim)}
.hist .hrow{border-top:1px dashed var(--line);padding-top:4px;margin-top:4px;word-break:break-word}

/* --- Agent 查询手册：长 curl 用可横滚的代码块，绝不撑破页面 --- */
details.man{border:1px solid var(--line);border-radius:10px;background:var(--panel);
  padding:6px 12px;margin-top:14px}
details.man>summary{cursor:pointer;min-height:44px;display:flex;align-items:center;
  gap:8px;font-size:13.5px;font-weight:600;color:var(--accent)}
details.man .p{color:var(--dim);font-size:12.5px;margin:4px 0;word-break:break-word}
.code{background:#0b0d12;border:1px solid var(--line);border-radius:6px;
  padding:8px 10px;margin:6px 0;font-family:Consolas,"Courier New",monospace;
  font-size:12.5px;white-space:pre;overflow-x:auto;max-width:100%;color:#cfe3ff}
.ep{font-family:Consolas,"Courier New",monospace;color:var(--accent);
  word-break:break-all;overflow-wrap:anywhere}
.trunc{display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;vertical-align:bottom}

/* --- 响应式三档：≥1024 桌面多列 / 768~1023 平板 / <768 手机单列，另有 <480 微调 --- */
@media (min-width:1024px){
  .meetgrid{grid-template-columns:340px 1fr}
}
@media (max-width:1023px){
  .meetgrid{grid-template-columns:1fr}
  .mlist{max-height:none}
  .trow{grid-template-columns:1fr 1fr}
  .trow.head{display:none}
  .day{min-height:54px}
  .wrap{max-width:100%}
}
@media (max-width:767px){
  /* 表格/多列行一律转卡片式单列；桌面专用列直接隐藏 */
  .trow{grid-template-columns:1fr;padding:10px}
  .d-none-mobile{display:none !important}
  .cal{display:none}                     /* 7 列小方块在手机上看不清，换列表 */
  .callist{display:flex}
  .meetgrid{gap:10px}
  button,select,input[type=text]{min-height:44px}   /* 触摸目标 */
  .tabs{display:flex;width:100%}
  .tabs button{flex:1 1 0;padding:8px 4px;min-height:44px}
  .bar{padding:10px;gap:8px}
  .bar .grow{min-width:0;flex:1 1 100%}
  .count{margin-left:0}
  .lights{margin-left:0}
  .filters{gap:8px}
  .vote .names{display:flex;flex-direction:column;gap:2px}
  .sum{width:100%}
  .time{margin-left:0}
}
@media (max-width:479px){
  .wrap{padding:8px 8px 40px}
  .chip,.badge,.ref{font-size:11px;padding:1px 5px}
  .meta{gap:6px}
  .tabs button{font-size:12.5px}
  .grp,.sitem,.item,.turn,.mdetail{padding:8px 9px}
  .code{font-size:11.5px}
  .dcard .dd{font-size:14px}
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>团队黑板 · 共享内容看板</h1>
    <span class="sub">只读视图</span>
    <div class="lights">
      <span class="light"><i class="dot" id="d-up"></i>上游 <b id="t-up">连接中</b></span>
      <span class="light"><i class="dot" id="d-cnt"></i>条目 <b id="t-cnt">-</b></span>
      <span class="light"><i class="dot" id="d-emb"></i>语义检索 <b id="t-emb">-</b></span>
    </div>
  </header>

  <div id="banner"></div>

  <div class="tabs-row">
    <span class="tabs" id="tabs">
      <button data-mod="meet" class="on">会议</button>
      <button data-mod="shared">共享内容</button>
      <button data-mod="track">跟踪</button>
    </span>
    <button id="refresh">刷新</button>
    <label class="chk"><input type="checkbox" id="auto"> 自动刷新<span id="tick"></span></label>
    <button id="man-top">本页数据接口</button>
  </div>

  <div class="pane on" id="pane-meet">
    <div class="tabs-row">
      <span class="tabs sub" id="subtabs">
        <button data-sub="cal" class="on">日历</button>
        <button data-sub="board">完整留痕</button>
      </span>
      <span class="lb d-none-mobile">会议 = 谁在什么时候定了什么；「完整留痕」是这些会的原始发言流水</span>
    </div>

    <div class="pane on" id="sub-cal">
      <div class="bar">
        <div class="grow">
          <input type="text" id="m-q" placeholder="按议题 / 目标 / 会议号搜索" autocomplete="off">
          <button id="m-clear">清空</button>
        </div>
        <span class="lb">时间范围</span>
        <select id="m-range">
          <option value="">全部</option>
          <option value="7">近 7 天</option>
          <option value="30">近 30 天</option>
          <option value="90">近 90 天</option>
        </select>
        <span class="lb">成员</span>
        <select id="m-member"><option value="">全部</option></select>
        <span class="lb">状态</span>
        <select id="m-state">
          <option value="">全部</option>
          <option value="running">进行中</option>
          <option value="converged">已收敛</option>
          <option value="timeout">超时</option>
          <option value="aborted">已中止</option>
        </select>
        <label class="chk"><input type="checkbox" id="m-unconf"> 只看未当场确认</label>
        <label class="chk"><input type="checkbox" id="m-risk"> 只看有遗留/有逾期</label>
        <span class="count"><b id="m-count">0</b> 场会议</span>
      </div>

      <div class="calbar">
        <button id="cal-prev">◀ 上月</button>
        <span class="mon" id="cal-mon">—</span>
        <button id="cal-next">下月 ▶</button>
        <button id="cal-recent">回到最近有会议的月份</button>
        <span class="lb d-none-mobile">点日期看当天会议 · 角标 = 当天场次</span>
      </div>
      <div class="cal" id="cal"></div>
      <div class="callist" id="cal-list"></div>

      <div class="dayhead" id="m-dayhead">当天会议</div>
      <div class="meetgrid">
        <div class="mlist" id="m-list"></div>
        <div class="mdetail" id="m-detail"></div>
      </div>
    </div>

  <div class="pane" id="sub-board">
    <div class="bar">
      <div class="grow">
        <input type="text" id="q" placeholder="搜索：用一句话描述你想找的内容（语义检索）" autocomplete="off">
        <button class="primary" id="go">搜索</button>
        <button id="clear">清空</button>
      </div>
      <span class="seg" id="modeseg">
        <button data-mode="semantic" class="on" title="调用上游语义检索">语义</button>
        <button data-mode="keyword" title="在已加载的条目里做本地包含匹配，不调用语义检索">关键词</button>
      </span>
    </div>

    <div class="filters">
      <span class="lb">作者</span><select id="f-author"><option value="">全部</option></select>
      <span class="lb">主题</span><select id="f-topic"><option value="">全部</option></select>
      <span class="lb">类型</span><select id="f-kind"><option value="">全部</option></select>
      <span class="lb">条数</span><select id="f-limit">
        <option value="10">10</option><option value="20" selected>20</option>
        <option value="50">50</option></select>
      <span class="count"><b id="count">0</b> 条<span id="scope"></span></span>
    </div>

    <div id="list"></div>
  </div>
  </div>

  <div class="pane" id="pane-shared">
    <div class="bar">
      <div class="grow">
        <input type="text" id="s-q" placeholder="搜索共享内容：标题 / 正文 / 标签" autocomplete="off">
        <button class="primary" id="s-go">搜索</button>
        <button id="s-clear">清空</button>
      </div>
      <label class="chk" title="走上游 /shared/search?semantic=1"><input type="checkbox" id="s-sem"> 语义检索</label>
      <span class="lb">类型</span><select id="s-kind"><option value="">全部</option></select>
      <span class="lb">维护责任人</span><select id="s-owner"><option value="">全部</option></select>
      <span class="lb">标签</span><select id="s-tag"><option value="">全部</option></select>
    </div>

    <div class="tabs-row">
      <span class="tabs sub" id="s-scope">
        <button data-scope="task" class="on">任务级</button>
        <button data-scope="permanent">永久</button>
      </span>
      <span class="lb d-none-mobile">任务级 = 跟着任务生灭，到点按销毁条件清掉；永久 = 长期档案，靠复审保鲜</span>
      <span class="count"><b id="s-count">0</b> 条内容</span>
    </div>

    <div id="s-body"></div>
  </div>

  <div class="pane" id="pane-track">
    <div class="bar">
      <span class="sum">
        <span class="pill">落实未闭环 <b id="k-aopen">-</b></span>
        <span class="pill late">落实逾期 <b id="k-alate">-</b></span>
        <span class="pill">遗留未闭环 <b id="k-iopen">-</b></span>
        <span class="pill late">遗留逾期 <b id="k-ilate">-</b></span>
        <span class="pill late">缺陷未闭环 <b id="k-dopen">-</b></span>
        <span class="pill late">缺陷逾期 <b id="k-dlate">-</b></span>
      </span>
      <span class="lb">责任人</span>
      <select id="k-owner"><option value="">全部</option></select>
      <span class="lb">性质</span>
      <select id="k-kind">
        <option value="">全部</option>
        <option value="defect">疑似缺陷 defect</option>
        <option value="issue">普通遗留 issue</option>
      </select>
      <span class="lb">状态</span>
      <select id="k-status">
        <option value="">全部</option>
        <option value="open">待办 open</option>
        <option value="doing">进行中 doing</option>
        <option value="watching">观察中 watching</option>
        <option value="done">已完成 done</option>
        <option value="closed">已关闭 closed</option>
        <option value="dropped">已放弃 dropped</option>
      </select>
      <label class="chk"><input type="checkbox" id="k-overdue"> 只看逾期</label>
    </div>

    <div class="tsec">
      <div class="sect">落实事项（谁 · 做什么 · 什么时候之前做完）
        <span class="n" id="k-acount">0 条</span></div>
      <div id="k-actions"></div>
    </div>

    <div class="tsec">
      <div class="sect">遗留问题 / 疑似缺陷（跟踪系统：没闭环的都在这儿）
        <span class="n" id="k-icount">0 条</span></div>
      <div id="k-issues"></div>
    </div>
  </div>

  <details class="man" id="man">
    <summary>Agent 查询手册 · 本页数据接口（只读，写请求一律 405）</summary>
    <div id="man-body"></div>
  </details>

  <footer id="foot">—</footer>
</div>

<script>
"use strict";
var CFG = __CFG_JSON__;

var state = {
  raw: [],                 // 最近一次从服务端拿到的条目
  mode: "semantic",
  expanded: {},            // entry_id -> true，刷新后保持展开状态
  facets: {author: {}, topic: {}, kind: {}},   // 选项从见过的数据里归纳，只增不减
  timer: null, left: 0, busy: false,
  mod: "meet",             // 一级模块：meet / shared / track，自动刷新时保持不变
  sub: "cal",              // 会议模块内的二级页：cal（日历）/ board（完整留痕）
  month: null,             // 月历当前显示的月份 {y, m}（m 是 0..11）
  day: null,               // 选中的日期 "YYYY-MM-DD"（北京时间口径）
  sessions: [], sid: null, detail: null,
  shared: null,            // 共享内容：整份上游响应
  sscope: "task",          // 共享内容分区：task / permanent
  sopen: {},               // shared_id -> true，展开正文
  shist: {},               // shared_id -> true，展开变更历史
  sfacets: {kind: {}, owner: {}, tag: {}},
  topen: {},               // turn_id -> true，刷新后保持展开的发言
  highlight: null,         // 从会议跳过来要高亮的 entry_id（只亮一次）
  track: null,             // 跟踪页签的数据：{actions, issues, counts, degraded}
  owners: {}               // 见过的责任人，用来填筛选下拉；只增不减
};

// 服务端给的是英文枚举，页面上一律显示中文
var STATE_CN = {running: "进行中", converged: "已收敛", timeout: "超时", aborted: "已中止"};
var STATUS_CN = {ok: "已发言", absent: "缺席", error: "异常", skipped: "跳过"};
var STANCE_CN = {support: "支持", oppose: "反对", neutral: "中立", need_info: "要资料"};
var VOTE_CN = {"continue": "再谈一轮", "converge": "可收敛"};
var STATE_KINDS = ["running", "converged", "timeout", "aborted"];
var STATUS_KINDS = ["ok", "absent", "error", "skipped"];

// 会议四要素用到的枚举（同样是"英文进来、中文出去"）
var AGENDA_CN = {pending: "待议", concluded: "已结论", deferred: "顺延"};
var AGENDA_KINDS = ["pending", "concluded", "deferred"];
var VERDICT_CN = {agree: "同意", object: "反对", abstain: "弃权", absent: "缺席"};
var VERDICT_KINDS = ["agree", "object", "abstain", "absent"];
var TRACK_CN = {open: "待办", doing: "进行中", done: "已完成", dropped: "已放弃",
                watching: "观察中", closed: "已关闭"};
var TRACK_KINDS = ["open", "doing", "done", "dropped", "watching", "closed"];
// 遗留问题的性质：defect = 缺席自动开的疑似缺陷单，必须一眼认出来
var ISSUE_KIND_CN = {issue: "普通遗留", defect: "疑似缺陷"};
var ISSUE_KINDS = ["issue", "defect"];
// 上游 degraded[] 里的技术名 -> 人话（缺表/缺列时告诉用户缺的是哪一块）
var DEGRADED_CN = {session_goal: "目标/议程/确认摘要", agenda_items: "议程条目",
                   confirmations: "当场确认", actions: "落实事项", issues: "遗留问题",
                   session_attendance: "出席率（应到/实到/缺席）",
                   issue_kind: "遗留问题的性质与证据（issue/defect）"};

// 共享内容：永久区按 kind 分组，英文枚举照例翻成中文（不认识的原样显示）
var SHARED_KIND_CN = {node: "节点", agent: "Agent", owner_profile: "主人画像",
                      owner_key_info: "主人关键信息", config: "配置", other: "其它"};
var SHARED_KIND_ORDER = ["node", "agent", "owner_profile", "owner_key_info", "config", "other"];
var TASK_STATE_CN = {active: "进行中", running: "进行中", done: "已结束",
                     closed: "已结束", aborted: "已中止", destroyed: "已销毁"};
var WEEKDAY_CN = ["日", "一", "二", "三", "四", "五", "六"];

function degradedText(list){
  if (!list || !list.length) return "";
  var names = [];
  for (var i = 0; i < list.length; i++) names.push(DEGRADED_CN[list[i]] || list[i]);
  return "库里还没有这几块的记录表：" + names.join("、") + "（等建表迁移跑完就有了）";
}

function $(id){ return document.getElementById(id); }

function el(tag, cls, text){
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  // 一律 textContent：条目正文是各 Agent 写的，永远不进 innerHTML
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}

function banner(msg){
  var b = $("banner");
  if (msg) { b.textContent = msg; b.className = "on"; }
  else { b.textContent = ""; b.className = ""; }
}

function pad(n){ return n < 10 ? "0" + n : "" + n; }

// 时间一律显示北京时间（UTC+8）：不看浏览器本地时区的脸色，
// 大家截图对时间时说的是同一个数。页脚会写明时区。
function fmtTime(ts){
  if (!ts) return "-";
  var d = new Date(ts);
  if (isNaN(d.getTime())) return String(ts);
  var b = new Date(d.getTime() + 8 * 3600 * 1000);
  return b.getUTCFullYear() + "-" + pad(b.getUTCMonth()+1) + "-" + pad(b.getUTCDate()) + " " +
         pad(b.getUTCHours()) + ":" + pad(b.getUTCMinutes()) + ":" + pad(b.getUTCSeconds());
}

// 日历一律按北京时间切日：先把时刻整体 +8h，再全程用 UTC 取值
function bj(ts){
  if (!ts) return null;
  var d = new Date(ts);
  if (isNaN(d.getTime())) return null;
  return new Date(d.getTime() + 8 * 3600 * 1000);
}

function dayKey(ts){
  var b = bj(ts);
  if (!b) return "";
  return b.getUTCFullYear() + "-" + pad(b.getUTCMonth() + 1) + "-" + pad(b.getUTCDate());
}

function hhmm(ts){
  var b = bj(ts);
  if (!b) return "--:--";
  return pad(b.getUTCHours()) + ":" + pad(b.getUTCMinutes());
}

function keyOf(y, m, d){ return y + "-" + pad(m + 1) + "-" + pad(d); }

function dayLabel(key){
  var parts = String(key || "").split("-");
  if (parts.length !== 3) return key || "-";
  var dt = new Date(Date.UTC(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2])));
  return Number(parts[1]) + " 月 " + Number(parts[2]) + " 日（周" +
         WEEKDAY_CN[dt.getUTCDay()] + "）";
}

// 截止时间只到分钟，表格里省一列宽度
function fmtDue(ts){
  if (!ts) return "未定截止";
  var text = fmtTime(ts);
  return text.length > 16 ? text.slice(0, 16) : text;
}

function relTime(ts){
  var d = new Date(ts);
  if (isNaN(d.getTime())) return "";
  var s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (s < 0) return "";
  if (s < 60) return s + " 秒前";
  if (s < 3600) return Math.floor(s/60) + " 分钟前";
  if (s < 86400) return Math.floor(s/3600) + " 小时前";
  return Math.floor(s/86400) + " 天前";
}

function api(path){
  return fetch(path, {headers: {"Accept": "application/json"}}).then(function(r){
    return r.json().catch(function(){ return {ok:false, error:"bad_json"}; })
            .then(function(j){ return {status: r.status, body: j}; });
  });
}

// --- 状态灯 ---------------------------------------------------------
function loadHealth(){
  return api("/api/health").then(function(res){
    var b = res.body || {};
    if (res.status !== 200 || !b.ok){
      $("d-up").className = "dot bad"; $("t-up").textContent = "不可用";
      $("d-cnt").className = "dot"; $("t-cnt").textContent = "-";
      $("d-emb").className = "dot"; $("t-emb").textContent = "-";
      banner("上游黑板不可用：" + (b.detail || b.error || ("HTTP " + res.status)));
      return;
    }
    banner("");
    $("d-up").className = "dot ok";
    $("t-up").textContent = (b.node || "ok") + " v" + (b.version || "?");
    var total = (b.entries && b.entries.total !== undefined) ? b.entries.total : "-";
    $("d-cnt").className = "dot ok"; $("t-cnt").textContent = total;
    var emb = b.embed || {};
    if (emb.available && emb.loaded !== false){
      $("d-emb").className = "dot ok";
      $("t-emb").textContent = "可用 · " + (emb.model || "?");
    } else if (emb.available){
      $("d-emb").className = "dot warn"; $("t-emb").textContent = "未加载";
    } else {
      $("d-emb").className = "dot warn"; $("t-emb").textContent = "退化为关键词";
    }
  }).catch(function(e){
    $("d-up").className = "dot bad"; $("t-up").textContent = "不可用";
    banner("看板服务本身连不上：" + e);
  });
}

// --- 拉数据 ---------------------------------------------------------
function load(){
  if (state.busy) return Promise.resolve();
  state.busy = true;
  var q = $("q").value.trim();
  var limit = $("f-limit").value;
  var url;
  // 语义模式且有 q 才去打 /api/search；空 q 一律走 /api/entries
  if (q && state.mode === "semantic"){
    url = "/api/search?q=" + encodeURIComponent(q) + "&limit=" + encodeURIComponent(limit);
  } else {
    url = "/api/entries?limit=" + encodeURIComponent(limit);
    var a = $("f-author").value, t = $("f-topic").value, k = $("f-kind").value;
    if (a) url += "&author=" + encodeURIComponent(a);
    if (t) url += "&topic=" + encodeURIComponent(t);
    if (k) url += "&kind=" + encodeURIComponent(k);
  }
  return api(url).then(function(res){
    var b = res.body || {};
    if (res.status !== 200 || !b.ok){
      banner("取条目失败：" + (b.detail || b.error || ("HTTP " + res.status)));
      state.raw = [];
    } else {
      banner("");
      state.raw = b.entries || [];
      collectFacets(state.raw);
    }
    render();
  }).catch(function(e){
    banner("请求失败：" + e); state.raw = []; render();
  }).then(function(){ state.busy = false; });
}

function collectFacets(rows){
  var keys = ["author", "topic", "kind"];
  for (var i = 0; i < rows.length; i++){
    for (var j = 0; j < keys.length; j++){
      var v = rows[i][keys[j]];
      if (v) state.facets[keys[j]][v] = true;
    }
  }
  for (var j2 = 0; j2 < keys.length; j2++) syncSelect("f-" + keys[j2], state.facets[keys[j2]]);
}

function syncSelect(id, set){
  var sel = $(id), keep = sel.value;
  var names = Object.keys(set).sort();
  if (sel.options.length - 1 === names.length) return;   // 没有新选项就别动
  sel.innerHTML = "";
  sel.appendChild(el("option", null, "全部"));
  sel.options[0].value = "";
  for (var i = 0; i < names.length; i++){
    var o = el("option", null, names[i]);
    o.value = names[i];
    sel.appendChild(o);
  }
  sel.value = keep;
}

// --- 渲染 -----------------------------------------------------------
function visible(){
  var a = $("f-author").value, t = $("f-topic").value, k = $("f-kind").value;
  var q = $("q").value.trim().toLowerCase();
  var kw = (state.mode === "keyword") && q;
  return state.raw.filter(function(e){
    if (a && e.author !== a) return false;
    if (t && e.topic !== t) return false;
    if (k && e.kind !== k) return false;
    if (kw){
      var hay = [e.content, e.author, e.topic, e.kind].join(" ").toLowerCase();
      if (hay.indexOf(q) < 0) return false;
    }
    return true;
  });
}

function card(e){
  var box = el("div", "card");
  var open = !!state.expanded[e.entry_id];
  if (open) box.className = "card open";

  var meta = el("div", "meta");
  meta.appendChild(el("span", "id", "#" + (e.entry_id !== undefined ? e.entry_id : "?")));
  if (e.author) meta.appendChild(el("span", "chip author", e.author));
  if (e.topic)  meta.appendChild(el("span", "chip topic", e.topic));
  if (e.kind)   meta.appendChild(el("span", "chip kind", e.kind));
  if (e.status && e.status !== "active") meta.appendChild(el("span", "chip", e.status));
  if (typeof e.score === "number") meta.appendChild(el("span", "chip score", "相似度 " + e.score.toFixed(3)));
  var t = el("span", "time", fmtTime(e.ts));
  var rel = relTime(e.ts);
  if (rel) t.textContent += "（" + rel + "）";
  meta.appendChild(t);
  box.appendChild(meta);

  var body = el("div", "content", e.content === undefined ? "" : e.content);
  if (!open) body.className = "content clip";
  box.appendChild(body);

  var text = String(e.content || "");
  if (text.length > 160 || text.indexOf("\\n") >= 0){
    box.appendChild(el("div", "more", open ? "收起 ▲" : "展开全文 ▼"));
  } else if (!open){
    body.className = "content";        // 短正文不折叠
  }

  if (e.refs && e.refs.length) box.appendChild(refsRow(e.refs));

  box.onclick = function(){
    if (state.expanded[e.entry_id]) delete state.expanded[e.entry_id];
    else state.expanded[e.entry_id] = true;
    render();
  };
  return box;
}

// refs 同样是各 Agent 写的不可信内容：JSON.stringify 之后仍旧走 textContent
function refsRow(refs){
  var row = el("div", "refs");
  for (var i = 0; i < refs.length; i++){
    var r = refs[i];
    row.appendChild(el("span", "ref", "ref " + (typeof r === "object" ? JSON.stringify(r) : r)));
  }
  return row;
}

function stamp(){
  $("foot").textContent = "最后更新 " + fmtTime(new Date().toISOString()) +
    "　·　页面所有时间均为北京时间（UTC+8）" +
    "　·　本页只读，不向黑板写入任何内容";
}

function render(){
  var rows = visible();
  var hl = state.highlight;
  state.highlight = null;                      // 高亮只亮这一次
  var list = $("list");
  list.innerHTML = "";
  $("count").textContent = rows.length;
  var q = $("q").value.trim();
  $("scope").textContent = q
    ? ("　" + (state.mode === "semantic" ? "语义检索" : "关键词匹配") + "：" + q)
    : "　最新条目";
  if (!rows.length){
    var hint = state.raw.length
      ? "当前筛选条件下没有条目 —— 试试把作者/主题/类型改回「全部」。"
      : "没有取到任何条目 —— 上游可能为空，或连接异常（见顶部提示）。";
    list.appendChild(el("div", "empty", hint));
  } else {
    var seen = false;
    for (var i = 0; i < rows.length; i++){
      var box = card(rows[i]);
      if (hl !== null && rows[i].entry_id === hl){
        box.className = box.className + " hit";
        seen = true;
      }
      list.appendChild(box);
    }
    if (hl !== null && !seen){
      banner("条目 #" + hl + " 不在当前列表里 —— 把「条数」调大或清掉筛选再找。");
    }
  }
  stamp();
}

// --- 会议：场次列表 -------------------------------------------------
function badge(kind, allowed, text){
  // class 名只从白名单里取，绝不把服务端字符串直接当样式类
  var cls = "badge";
  for (var i = 0; i < allowed.length; i++) if (allowed[i] === kind) cls = "badge " + kind;
  return el("span", cls, text);
}

// --- 出席率：分母是**应到人数**（members 全集），不是在场人数 ----------
// 成员都是云主机 / 常驻 Agent，设计上不该掉线 —— 所以满员才算正常（绿），
// 只要缺一个就是异常（黄），缺席成员的名字一律红标。
function attInfo(s){
  var exp = s.expected_count, pre = s.present_count;
  var miss = s.absent_members || [];
  if (exp === null || exp === undefined || pre === null || pre === undefined){
    return {known: false, expected: (s.members || []).length, present: null,
            absent: null, rate: null, missing: miss, text: "出席率（未记录）"};
  }
  var rate = s.attendance_rate;
  if (rate === null || rate === undefined) rate = exp ? (pre / exp) : null;
  var text = "出席率 " + pre + "/" + exp +
             (rate === null ? "" : (" = " + (rate * 100).toFixed(1) + "%"));
  return {known: true, expected: exp, present: pre,
          absent: (s.absent_count === undefined ? null : s.absent_count),
          rate: rate, missing: miss, text: text};
}

function attFull(a){ return a.known && a.rate !== null && a.rate >= 1; }

function attBadge(s){
  var a = attInfo(s);
  if (!a.known) return badge("", [], a.text);
  return badge(attFull(a) ? "att" : "attbad", ["att", "attbad"], a.text);
}

// 缺席名单：红标，且明说"已自动开缺陷单"，别让人以为只是个统计数字
function attBlock(s){
  var a = attInfo(s);
  var box = el("div", !a.known ? "att none" : (attFull(a) ? "att" : "att bad"));
  box.appendChild(el("div", null, a.known
    ? ("应到 " + a.expected + " 人 / 实到 " + a.present + " 人 / 缺席 " +
       (a.absent === null ? (a.expected - a.present) : a.absent) + " 人 —— " + a.text)
    : "出席率：（未记录 —— 这场会是老版本跑的）"));
  if (a.missing.length){
    box.appendChild(el("div", "miss",
      "缺席：" + a.missing.join("、") + "（点名无应答 = 疑似程序缺陷，已自动开单跟踪）"));
  } else if (a.known && attFull(a)){
    box.appendChild(el("div", null, "全员到齐"));
  }
  return box;
}

// 缺陷单的证据是 jsonb 数组（键名是中文），这里压成一行给人看
function evidenceText(list){
  if (!list || !list.length) return "";
  var parts = [];
  for (var i = 0; i < list.length && i < 3; i++){
    var e = list[i] || {}, bits = [];
    if (e["点名次数"] !== undefined && e["点名次数"] !== null){
      bits.push("点名 " + e["点名次数"] + " 次无应答");
    }
    if (e["失败原因"]) bits.push(String(e["失败原因"]));
    if (e["环节"] && e["环节"].length) bits.push(String(e["环节"].join("、")));
    if (e.session_id) bits.push("来源 " + e.session_id);
    if (e.ts) bits.push(fmtTime(e.ts));
    parts.push(bits.length ? bits.join("｜") : JSON.stringify(e));
  }
  return parts.join("；");
}

// --- 三个模块：会议（日历）/ 共享内容 / 跟踪 --------------------------
// 信息边界：会议 = 一场会本身（目标/议程/结论/确认/落实/遗留/留痕）；
// 共享内容 = 与会议无关、供人和 Agent 长期或按任务取用的资料；
// 跟踪 = 跨会汇总的未闭环清单。三者受众与管控不同，页面上不混。
function paintMod(){
  var btns = $("tabs").getElementsByTagName("button");
  for (var i = 0; i < btns.length; i++){
    btns[i].className = (btns[i].getAttribute("data-mod") === state.mod) ? "on" : "";
  }
  $("pane-meet").className = (state.mod === "meet") ? "pane on" : "pane";
  $("pane-shared").className = (state.mod === "shared") ? "pane on" : "pane";
  $("pane-track").className = (state.mod === "track") ? "pane on" : "pane";
}

function paintSub(){
  var btns = $("subtabs").getElementsByTagName("button");
  for (var i = 0; i < btns.length; i++){
    btns[i].className = (btns[i].getAttribute("data-sub") === state.sub) ? "on" : "";
  }
  $("sub-cal").className = (state.sub === "cal") ? "pane on" : "pane";
  $("sub-board").className = (state.sub === "board") ? "pane on" : "pane";
}

function switchMod(name){
  state.mod = name;
  paintMod();
  reloadCurrent();
}

function switchSub(name){
  state.sub = name;
  paintSub();
  reloadCurrent();
}

function loadSessions(){
  var url = "/api/sessions?limit=50";
  var st = $("m-state").value;
  if (st) url += "&state=" + encodeURIComponent(st);
  return api(url).then(function(res){
    var b = res.body || {};
    if (res.status !== 200 || !b.ok){
      banner("取会议列表失败：" + (b.detail || b.error || ("HTTP " + res.status)));
      state.sessions = [];
    } else {
      banner("");
      state.sessions = b.sessions || [];
      collectMembers(state.sessions);
    }
    renderSessions();
  }).catch(function(e){
    banner("请求失败：" + e); state.sessions = []; renderSessions();
  });
}

function loadDetail(sid){
  if (!sid) return Promise.resolve();
  return api("/api/sessions/" + encodeURIComponent(sid)).then(function(res){
    var b = res.body || {};
    if (res.status === 404){
      state.detail = {gone: true};
    } else if (res.status !== 200 || !b.ok){
      banner("取会议详情失败：" + (b.detail || b.error || ("HTTP " + res.status)));
      state.detail = null;
    } else {
      state.detail = b;
    }
    renderDetail();
  }).catch(function(e){
    banner("请求失败：" + e); state.detail = null; renderDetail();
  });
}

// 顶栏筛选：时间范围 / 只看未当场确认 / 只看有遗留或逾期 / 按成员
// "有遗留或逾期"要靠跟踪清单来判断（会议列表本身不带这些），所以会议模块也拉一份 /tracking。
function riskMap(){
  var map = {}, d = state.track || {};
  var bump = function(rows, isIssue){
    for (var i = 0; i < rows.length; i++){
      var r = rows[i], sid = r.session_id;
      if (!sid) continue;
      if (r.status === "done" || r.status === "closed" || r.status === "dropped") continue;
      if (!map[sid]) map[sid] = {issues: 0, overdue: 0};
      if (isIssue) map[sid].issues += 1;
      if (r.overdue) map[sid].overdue += 1;
    }
  };
  bump(d.actions || [], false);
  bump(d.issues || [], true);
  return map;
}

// 只有 confirm_summary.passed === true 才算"当场确认过"，其余一律算没确认
function sessionPassed(s){
  return !!(s.confirm_summary && s.confirm_summary.passed === true);
}

function visibleSessions(){
  var q = $("m-q").value.trim().toLowerCase();
  var range = Number($("m-range").value || 0);
  var member = $("m-member").value;
  var unconf = $("m-unconf").checked;
  var risky = $("m-risk").checked;
  var risks = riskMap();
  var floor = range ? (Date.now() - range * 86400000) : null;
  return state.sessions.filter(function(s){
    if (q){
      var hay = [s.topic, s.question, s.goal, s.session_id].join(" ").toLowerCase();
      if (hay.indexOf(q) < 0) return false;
    }
    if (floor !== null){
      var t = new Date(s.started_at).getTime();
      if (isNaN(t) || t < floor) return false;
    }
    if (member){
      var mem = (s.members || []).concat([s.moderator || ""]);
      if (mem.indexOf(member) < 0) return false;
    }
    if (unconf && sessionPassed(s)) return false;
    if (risky){
      var r = risks[s.session_id];
      if (!r || (r.issues <= 0 && r.overdue <= 0)) return false;
    }
    return true;
  });
}

function collectMembers(rows){
  if (!state.mems) state.mems = {};
  for (var i = 0; i < rows.length; i++){
    var mem = (rows[i].members || []).concat([rows[i].moderator || ""]);
    for (var j = 0; j < mem.length; j++) if (mem[j]) state.mems[mem[j]] = true;
  }
  syncSelect("m-member", state.mems);
}

// --- 会议：月历（一格一天，有会议就打点 + 场次角标）---------------------
function byDay(rows){
  var map = {}, k;
  for (var i = 0; i < rows.length; i++){
    var key = dayKey(rows[i].started_at);
    if (!key) continue;
    if (!map[key]) map[key] = [];
    map[key].push(rows[i]);
  }
  for (k in map) if (map.hasOwnProperty(k)){
    map[k].sort(function(a, b){            // 同一天多场会按时间排
      return String(a.started_at || "").localeCompare(String(b.started_at || ""));
    });
  }
  return map;
}

function monthKey(){ return state.month.y + "-" + pad(state.month.m + 1); }

function dayHasLate(rows, risks){
  for (var i = 0; i < rows.length; i++){
    var r = risks[rows[i].session_id];
    if (r && r.overdue > 0) return true;
  }
  return false;
}

function selectDay(key){
  state.day = key;
  var rows = byDay(visibleSessions())[key] || [];
  var found = false;
  for (var i = 0; i < rows.length; i++){
    if (rows[i].session_id === state.sid) found = true;
  }
  if (rows.length && !found){
    state.sid = rows[0].session_id;       // 当天第一场先摆出来，手机上少点一次
    state.detail = null;
    state.topen = {};
    loadDetail(state.sid);
  }
  renderSessions();
  renderDetail();
}

function renderCalendar(){
  var rows = visibleSessions();
  var map = byDay(rows);
  var keys = Object.keys(map).sort();
  var risks = riskMap();
  $("m-count").textContent = rows.length;

  if (!state.month){                       // 默认定位到最近有会议的月份
    var anchor = keys.length ? keys[keys.length - 1] : dayKey(new Date().toISOString());
    state.month = {y: Number(anchor.slice(0, 4)), m: Number(anchor.slice(5, 7)) - 1};
  }
  if (state.day && !map[state.day]) state.day = null;
  if (!state.day){
    var inMonth = [];
    for (var i = 0; i < keys.length; i++){
      if (keys[i].slice(0, 7) === monthKey()) inMonth.push(keys[i]);
    }
    if (inMonth.length) state.day = inMonth[inMonth.length - 1];
  }
  // 首次进来就把当天第一场摊开，别让详情面板空着等人点
  if (state.day && !state.sid){
    var todays = map[state.day] || [];
    if (todays.length){
      state.sid = todays[0].session_id;
      loadDetail(state.sid);
    }
  }

  $("cal-mon").textContent = state.month.y + " 年 " + (state.month.m + 1) + " 月";

  var cal = $("cal");
  cal.innerHTML = "";
  for (var w = 0; w < 7; w++) cal.appendChild(el("div", "wd", WEEKDAY_CN[w]));
  var lead = new Date(Date.UTC(state.month.y, state.month.m, 1)).getUTCDay();
  var total = new Date(Date.UTC(state.month.y, state.month.m + 1, 0)).getUTCDate();
  for (var p = 0; p < lead; p++) cal.appendChild(el("div", "day pad"));
  for (var d = 1; d <= total; d++){
    var key = keyOf(state.month.y, state.month.m, d);
    var list = map[key] || [];
    var cls = "day";
    if (list.length) cls += " has";
    if (list.length && dayHasLate(list, risks)) cls += " late";
    if (state.day === key) cls += " on";
    var cell = el("div", cls);
    cell.appendChild(el("div", "dn", d));
    if (list.length){
      cell.appendChild(el("div", "mark"));               // 标记点
      cell.appendChild(el("div", "cnt", list.length));   // 角标 = 当天场次
      cell.onclick = (function(k){ return function(){ selectDay(k); }; })(key);
    }
    cal.appendChild(cell);
  }

  // 手机上不挤 7 列小方块：只列"本月有会议的日期"，一天一张大卡片
  var cl = $("cal-list");
  cl.innerHTML = "";
  var mine = [];
  for (var j = 0; j < keys.length; j++){
    if (keys[j].slice(0, 7) === monthKey()) mine.push(keys[j]);
  }
  if (!mine.length){
    cl.appendChild(el("div", "empty", "本月没有会议 —— 左右切换月份，或点「回到最近有会议的月份」。"));
  } else {
    for (var n = 0; n < mine.length; n++){
      var k2 = mine[n], day = map[k2];
      var card2 = el("div", state.day === k2 ? "dcard on" : "dcard");
      card2.appendChild(el("span", "dd", dayLabel(k2)));
      card2.appendChild(el("span", "chip", day.length + " 场"));
      card2.appendChild(el("span", "chip topic", hhmm(day[0].started_at) + " 起"));
      if (dayHasLate(day, risks)) card2.appendChild(badge("late", ["late"], "有逾期"));
      card2.onclick = (function(k3){ return function(){ selectDay(k3); }; })(k2);
      cl.appendChild(card2);
    }
  }
}

function shiftMonth(delta){
  if (!state.month) return;
  var m = state.month.m + delta, y = state.month.y;
  while (m < 0){ m += 12; y -= 1; }
  while (m > 11){ m -= 12; y += 1; }
  state.month = {y: y, m: m};
  state.day = null;
  renderSessions();
}

function jumpRecentMonth(){
  var keys = Object.keys(byDay(visibleSessions())).sort();
  if (!keys.length) return;
  var last = keys[keys.length - 1];
  state.month = {y: Number(last.slice(0, 4)), m: Number(last.slice(5, 7)) - 1};
  state.day = last;
  renderSessions();
}

function sessionCard(s){
  var box = el("div", state.sid === s.session_id ? "scard on" : "scard");
  var head = el("div", "meta");
  head.appendChild(el("span", "id", hhmm(s.started_at)));
  head.appendChild(el("span", "chip topic", s.topic || "-"));
  head.appendChild(badge(s.state, STATE_KINDS, STATE_CN[s.state] || s.state || "?"));
  head.appendChild(el("span", "chip", "第 " + (s.round_no || 0) + "/" + (s.max_rounds || 0) + " 轮"));
  if (!sessionPassed(s)) head.appendChild(badge("attbad", ["attbad"], "未当场确认"));
  box.appendChild(head);
  box.appendChild(el("div", "q", s.goal ? ("目标：" + s.goal) : (s.question || "（无问题描述）")));
  var foot = el("div", "meta");
  foot.appendChild(el("span", null, fmtTime(s.started_at)));
  var a = attInfo(s);
  foot.appendChild(el("span", "chip", "应到 " + a.expected + " 人"));
  if (a.known) foot.appendChild(el("span", "chip", "实到 " + a.present + " 人"));
  foot.appendChild(attBadge(s));
  foot.appendChild(el("span", "chip", (s.turns_total || 0) + " 条发言"));
  if (s.conclusion_entry) foot.appendChild(badge("converged", STATE_KINDS, "有结论"));
  box.appendChild(foot);
  // 缺席名单直接摆在卡片上：谁没来是最该被看见的那件事
  if (a.missing.length) box.appendChild(el("div", "miss", "缺席：" + a.missing.join("、")));
  box.onclick = function(){
    state.sid = s.session_id;
    state.detail = null;
    state.topen = {};
    renderSessions(); renderDetail(); loadDetail(s.session_id);
  };
  return box;
}

// 当天会议列表：点月历里的某一天 → 这儿列出那天的每一场（多场按时间排）
function renderDayList(){
  var map = byDay(visibleSessions());
  var rows = state.day ? (map[state.day] || []) : [];
  var list = $("m-list");
  list.innerHTML = "";
  $("m-dayhead").textContent = state.day
    ? (dayLabel(state.day) + " · 共 " + rows.length + " 场")
    : "当天会议（先在上面挑一天）";
  if (!rows.length){
    list.appendChild(el("div", "empty", state.sessions.length
      ? "这一天没有会议 —— 换个日期，或把上面的筛选放宽。"
      : "暂无会议记录"));
  } else {
    for (var i = 0; i < rows.length; i++) list.appendChild(sessionCard(rows[i]));
  }
}

function renderSessions(){
  renderCalendar();
  renderDayList();
  stamp();
}

// --- 会议：逐条发言 -------------------------------------------------
function jumpToEntry(entryId){
  state.expanded[entryId] = true;
  state.highlight = entryId;
  $("q").value = "";
  state.sub = "board";                 // 黑板流水现在是会议模块里的「完整留痕」
  paintSub();
  switchMod("meet");
}

// 从跟踪模块点"来源会议"跳回会议模块，并自动展开那一场（连带定位到那一天）
function jumpToSession(sid){
  state.sid = sid;
  state.detail = null;
  state.topen = {};
  for (var i = 0; i < state.sessions.length; i++){
    if (state.sessions[i].session_id === sid){
      var key = dayKey(state.sessions[i].started_at);
      if (key){
        state.day = key;
        state.month = {y: Number(key.slice(0, 4)), m: Number(key.slice(5, 7)) - 1};
      }
      break;
    }
  }
  state.sub = "cal";
  paintSub();
  switchMod("meet");
}

function turnCard(t){
  var box = el("div", "turn");
  var meta = el("div", "meta");
  meta.appendChild(el("span", "chip author", t.speaker || "?"));
  meta.appendChild(badge(t.status, STATUS_KINDS, STATUS_CN[t.status] || t.status || "?"));
  if (t.stance) meta.appendChild(el("span", "chip kind", STANCE_CN[t.stance] || t.stance));
  if (t.vote) meta.appendChild(el("span", "chip", VOTE_CN[t.vote] || t.vote));
  if (typeof t.confidence === "number"){
    meta.appendChild(el("span", "chip score", "置信 " + t.confidence.toFixed(2)));
  }
  meta.appendChild(el("span", "time", fmtTime(t.ts)));
  box.appendChild(meta);

  var text = (t.content === null || t.content === undefined) ? "" : String(t.content);
  if (text){
    var open = !!state.topen[t.turn_id];
    var body = el("div", open ? "content" : "content clip", text);
    box.appendChild(body);
    if (text.length > 160 || text.indexOf("\\n") >= 0){
      var more = el("div", "more", open ? "收起 ▲" : "展开全文 ▼");
      more.onclick = function(){
        if (state.topen[t.turn_id]) delete state.topen[t.turn_id];
        else state.topen[t.turn_id] = true;
        renderDetail();
      };
      box.appendChild(more);
    } else if (!open){
      body.className = "content";
    }
  } else if (t.status === "absent" || t.status === "error"){
    // 没有正文的位置就显示原因，别留一个空壳
    box.appendChild(el("div", "why", "原因：" + (t.error || "未记录原因")));
  }
  if (text && t.error) box.appendChild(el("div", "why", "备注：" + t.error));

  if (t.refs && t.refs.length) box.appendChild(refsRow(t.refs));
  if (t.entry_id){
    var row = el("div", "refs");
    var link = el("span", "linkid", "黑板 #" + t.entry_id);
    link.onclick = (function(id){ return function(){ jumpToEntry(id); }; })(t.entry_id);
    row.appendChild(link);
    box.appendChild(row);
  }
  return box;
}

// --- 会议四要素：目标 / 议程 / 当场确认 / 落实·遗留 -------------------
function section(box, title, count){
  var head = el("div", "sect", title);
  head.appendChild(el("span", "n", count + " 条"));
  box.appendChild(head);
  return head;
}

function kv(parent, label, value){
  var line = el("div", "k");
  line.appendChild(el("b", null, label + "："));
  line.appendChild(el("span", null, (value === null || value === undefined || value === "")
    ? "（未记录）" : String(value)));
  parent.appendChild(line);
}

// 议程：逐项 title / expected / 该项结论 / 状态徽标
function renderAgenda(box, items, agenda){
  section(box, "议程", items.length || (agenda || []).length);
  if (items.length){
    for (var i = 0; i < items.length; i++){
      var it = items[i];
      var row = el("div", "item");
      var head = el("div", "meta");
      head.appendChild(el("span", "id", "#" + (it.seq === undefined ? "?" : it.seq)));
      head.appendChild(el("span", "chip topic", it.title || "（无题）"));
      head.appendChild(badge(it.status, AGENDA_KINDS, AGENDA_CN[it.status] || it.status || "?"));
      row.appendChild(head);
      kv(row, "预期产出", it.expected);
      kv(row, "该项结论", it.conclusion);
      if (it.note) kv(row, "备注", it.note);
      if (it.conclusion_entry){
        var links = el("div", "refs");
        var link = el("span", "linkid", "黑板 #" + it.conclusion_entry);
        link.onclick = (function(id){ return function(){ jumpToEntry(id); }; })(it.conclusion_entry);
        links.appendChild(link);
        row.appendChild(links);
      }
      box.appendChild(row);
    }
    return;
  }
  // 没有结构化议程条目时，退而求其次显示会话上那份 agenda（纯文本数组）
  var raw = agenda || [];
  if (raw.length){
    for (var j = 0; j < raw.length; j++){
      var plain = el("div", "item");
      plain.appendChild(el("div", "t", (j + 1) + ". " +
        (typeof raw[j] === "object" ? JSON.stringify(raw[j]) : raw[j])));
      box.appendChild(plain);
    }
  } else {
    box.appendChild(el("div", "empty", "（未记录目标/议程）"));
  }
}

// 总体结论 + 当场确认：passed 与否必须一眼看得见，不许把"没确认"显示成"通过"
function renderConfirm(box, s, confirms){
  section(box, "总体结论与当场确认", confirms.length);
  if (s.conclusion_text){
    box.appendChild(el("div", "concl", "结论：" + s.conclusion_text));
  } else {
    box.appendChild(el("div", "concl none", "尚未产出结论"));
  }

  var sm = s.confirm_summary;
  var counts = {agree: [], object: [], abstain: [], absent: []};
  var k;
  if (sm){
    for (k in counts) if (counts.hasOwnProperty(k)) counts[k] = sm[k] || [];
  } else {
    // 没有汇总就按逐条确认自己数（只数总体口径 target=overall 的）
    for (var i = 0; i < confirms.length; i++){
      var c = confirms[i];
      if (c.target && c.target !== "overall") continue;
      if (counts.hasOwnProperty(c.verdict)) counts[c.verdict].push(c.member);
    }
  }
  var passed = sm ? sm.passed : null;
  var line = el("div", passed === true ? "vote pass" : "vote fail");
  var required = sm && (sm.required !== null && sm.required !== undefined) ? sm.required : null;
  var head = passed === true
    ? "已当场确认通过"
    : (passed === false ? "未通过（当场确认没过）" : "未确认（没有当场确认记录）");
  if (required !== null) head += "　需 " + required + " 票，同意 " + counts.agree.length + " 票";
  else head += "　同意 " + counts.agree.length + " 票";
  line.appendChild(el("div", null, head));
  line.appendChild(el("div", null, "确认时间：" +
    (s.confirmed_at ? fmtTime(s.confirmed_at) : "（未确认）")));
  var order = VERDICT_KINDS;
  for (var v = 0; v < order.length; v++){
    var names = counts[order[v]] || [];
    var row = el("div", "names");
    row.appendChild(el("span", null, VERDICT_CN[order[v]] + "（" + names.length + "）："));
    row.appendChild(el("span", null, names.length ? names.join("、") : "无"));
    line.appendChild(row);
  }
  box.appendChild(line);

  if (confirms.length){
    for (var m = 0; m < confirms.length; m++){
      var cc = confirms[m];
      var item = el("div", "item");
      var meta = el("div", "meta");
      meta.appendChild(el("span", "chip author", cc.member || "?"));
      meta.appendChild(badge(cc.verdict, VERDICT_KINDS, VERDICT_CN[cc.verdict] || cc.verdict || "?"));
      meta.appendChild(el("span", "chip", cc.target === "overall" || !cc.target
        ? "总体" : ("议程 " + cc.target)));
      meta.appendChild(el("span", "time", fmtTime(cc.ts)));
      item.appendChild(meta);
      if (cc.note) item.appendChild(el("div", "k", cc.note));
      box.appendChild(item);
    }
  }
}

// 落实事项：责任人 / 截止 / 状态；overdue 由上游算好，这里只负责标红
function actionRow(a, withSource){
  var row = el("div", a.overdue ? "trow late" : "trow");
  row.appendChild(el("div", "who", a.owner || "（未指定责任人）"));
  row.appendChild(el("div", "t", a.what || "（未记录事项）"));
  var due = el("div", "due", "截止 " + fmtDue(a.due_at));
  if (a.overdue) due.textContent += "　已逾期";
  row.appendChild(due);
  var st = el("div");
  st.appendChild(badge(a.status, TRACK_KINDS, TRACK_CN[a.status] || a.status || "?"));
  if (a.overdue) st.appendChild(badge("late", ["late"], "已逾期"));
  row.appendChild(st);
  var src = el("div", "src", withSource
    ? ("来源 " + (a.session_topic || a.session_id || "-")) : ("议程 " +
        (a.agenda_seq === null || a.agenda_seq === undefined ? "-" : a.agenda_seq)));
  if (withSource && a.session_id){
    src.onclick = (function(id){ return function(){ jumpToSession(id); }; })(a.session_id);
  } else {
    src.className = "due";
  }
  row.appendChild(src);
  return row;
}

function issueRow(it, withSource){
  var row = el("div", it.overdue ? "trow late" : "trow");
  row.appendChild(el("div", "who", it.owner || "（未指定跟踪人）"));
  var title = el("div", "t", it.title || "（无题）");
  if (it.detail) title.appendChild(el("div", "k", it.detail));
  var ev = evidenceText(it.evidence);
  if (ev) title.appendChild(el("div", "k", "证据：" + ev));
  row.appendChild(title);
  var due = el("div", "due", "截止 " + fmtDue(it.due_at));
  if (it.next_check_at) due.textContent += "　下次跟进 " + fmtDue(it.next_check_at);
  if (it.overdue) due.textContent += "　已逾期";
  row.appendChild(due);
  var st = el("div");
  // 疑似缺陷（缺席自动开的单）红标在最前面：它跟普通遗留问题不是一回事
  if (it.kind === "defect"){
    st.appendChild(badge("defect", ISSUE_KINDS, ISSUE_KIND_CN.defect));
  }
  st.appendChild(badge(it.status, TRACK_KINDS, TRACK_CN[it.status] || it.status || "?"));
  if (it.overdue) st.appendChild(badge("late", ["late"], "已逾期"));
  row.appendChild(st);
  var src = el("div", withSource ? "src" : "due", withSource
    ? ("来源 " + (it.session_topic || it.session_id || "-")) : "");
  if (withSource && it.session_id){
    src.onclick = (function(id){ return function(){ jumpToSession(id); }; })(it.session_id);
  }
  row.appendChild(src);
  return row;
}

function renderDetail(){
  var box = $("m-detail");
  box.innerHTML = "";
  if (!state.sid){
    box.appendChild(el("div", "empty", "从左边挑一场会议，这里显示它的逐条发言。"));
    return;
  }
  var d = state.detail;
  if (!d){
    box.appendChild(el("div", "empty", "加载中…"));
    return;
  }
  if (d.gone){
    box.appendChild(el("div", "empty", "这场会议不存在（可能已被换库或 id 打错）。"));
    return;
  }

  var s = d.session || {};
  var head = el("div", "meta");
  head.appendChild(el("span", "id", s.session_id || "?"));
  head.appendChild(el("span", "chip topic", s.topic || "-"));
  head.appendChild(badge(s.state, STATE_KINDS, STATE_CN[s.state] || s.state || "?"));
  head.appendChild(el("span", "chip", "第 " + (s.round_no || 0) + "/" + (s.max_rounds || 0) + " 轮"));
  if (s.moderator) head.appendChild(el("span", "chip author", "主持 " + s.moderator));
  head.appendChild(el("span", "time", fmtTime(s.started_at)));
  box.appendChild(head);

  // ① 会议目标：最显眼的一块；老会议没这字段就明说"未记录"
  box.appendChild(el("div", s.goal ? "goal" : "goal none",
    s.goal ? ("会议目标：" + s.goal) : "会议目标：（未记录目标/议程）"));
  box.appendChild(el("div", "qfull", s.question || "（无问题描述）"));

  var hint = degradedText(d.degraded);
  if (hint) box.appendChild(el("div", "warnline", "提示：" + hint));

  // ② 议程　③ 总体结论 + 当场确认　④ 落实事项　⑤ 遗留问题
  renderAgenda(box, d.agenda_items || [], s.agenda || []);
  renderConfirm(box, s, d.confirmations || []);

  var actions = d.actions || [];
  section(box, "落实事项", actions.length);
  if (!actions.length){
    box.appendChild(el("div", "empty", "暂无落实事项"));
  } else {
    for (var ai = 0; ai < actions.length; ai++) box.appendChild(actionRow(actions[ai], false));
  }

  var issues = d.issues || [];
  section(box, "遗留问题", issues.length);
  if (!issues.length){
    box.appendChild(el("div", "empty", "暂无遗留问题"));
  } else {
    for (var ii = 0; ii < issues.length; ii++) box.appendChild(issueRow(issues[ii], false));
  }

  // 附：出席率（应到 / 实到 / 缺席名单，缺一个人就标黄；参会人是人还是 Agent 不作区分）
  section(box, "附：出席情况", (s.members || []).length);
  box.appendChild(attBlock(s));

  // 折叠的「完整留痕」：这场会的全部发言流水（含 refs 出处）
  var turns = d.turns || [];
  var det = el("details", "man");
  det.appendChild(el("summary", null, "完整留痕 · 本场全部发言（" + turns.length + " 条）"));
  if (!turns.length){
    det.appendChild(el("div", "empty", "本场暂无发言记录"));
  } else {
    var current = null;
    for (var i = 0; i < turns.length; i++){
      var t = turns[i];
      if (t.round_no !== current){
        current = t.round_no;
        det.appendChild(el("div", "roundhead", "第 " + current + " 轮"));
      }
      det.appendChild(turnCard(t));
    }
  }
  box.appendChild(det);
}

// --- 跟踪页签：跨会总清单 -------------------------------------------
function loadTracking(){
  var url = "/api/tracking?limit=50";
  if ($("k-overdue").checked) url += "&overdue=1";
  return api(url).then(function(res){
    var b = res.body || {};
    if (res.status !== 200 || !b.ok){
      banner("取跟踪清单失败：" + (b.detail || b.error || ("HTTP " + res.status)));
      state.track = null;
    } else {
      banner("");
      state.track = b;
      collectOwners(b.actions || []);
      collectOwners(b.issues || []);
    }
    renderTracking();
  }).catch(function(e){
    banner("请求失败：" + e); state.track = null; renderTracking();
  });
}

function collectOwners(rows){
  for (var i = 0; i < rows.length; i++){
    if (rows[i].owner) state.owners[rows[i].owner] = true;
  }
  syncSelect("k-owner", state.owners);
}

// kindAware=true 时才按"性质"筛（只有遗留问题有 issue/defect 之分，落实事项没有）
function trackVisible(rows, kindAware){
  var owner = $("k-owner").value, status = $("k-status").value;
  var kind = $("k-kind").value;
  var lateOnly = $("k-overdue").checked;
  return rows.filter(function(r){
    if (owner && r.owner !== owner) return false;
    if (status && r.status !== status) return false;
    if (kindAware && kind && (r.kind || "issue") !== kind) return false;
    if (lateOnly && !r.overdue) return false;
    return true;
  });
}

function trackHead(cols){
  var row = el("div", "trow head");
  for (var i = 0; i < cols.length; i++) row.appendChild(el("div", null, cols[i]));
  return row;
}

function num(id, value){
  $(id).textContent = (value === null || value === undefined) ? "-" : String(value);
}

function renderTracking(){
  var aBox = $("k-actions"), iBox = $("k-issues");
  aBox.innerHTML = "";
  iBox.innerHTML = "";
  var d = state.track;
  if (!d){
    num("k-aopen", null); num("k-alate", null);
    num("k-iopen", null); num("k-ilate", null);
    num("k-dopen", null); num("k-dlate", null);
    $("k-acount").textContent = "0 条";
    $("k-icount").textContent = "0 条";
    aBox.appendChild(el("div", "empty", "没有取到跟踪数据（见顶部提示）"));
    iBox.appendChild(el("div", "empty", "没有取到跟踪数据（见顶部提示）"));
    stamp();
    return;
  }
  var counts = d.counts || {};
  num("k-aopen", counts.actions_open);
  num("k-alate", counts.actions_overdue);
  num("k-iopen", counts.issues_open);
  num("k-ilate", counts.issues_overdue);
  // 缺陷未闭环数摆在顶部：缺席 = 疑似 Bug，这个数字不该藏在遗留问题的总数里
  num("k-dopen", counts.defects_open);
  num("k-dlate", counts.defects_overdue);

  var hint = degradedText(d.degraded);
  var actions = trackVisible(d.actions || [], false);
  var issues = trackVisible(d.issues || [], true);
  $("k-acount").textContent = actions.length + " 条";
  $("k-icount").textContent = issues.length + " 条";

  if (!actions.length){
    aBox.appendChild(el("div", "empty", hint || "暂无落实事项"));
  } else {
    aBox.appendChild(trackHead(["责任人", "事项", "截止（北京时间）", "状态", "来源会议"]));
    for (var i = 0; i < actions.length; i++) aBox.appendChild(actionRow(actions[i], true));
  }
  if (!issues.length){
    iBox.appendChild(el("div", "empty", hint || "暂无遗留问题"));
  } else {
    iBox.appendChild(trackHead(["跟踪人", "题目 / 证据", "截止（北京时间）",
                                "性质 · 状态", "来源会议"]));
    for (var j = 0; j < issues.length; j++) iBox.appendChild(issueRow(issues[j], true));
  }
  stamp();
}

// --- 共享内容模块：任务级（按任务分组）/ 永久（按 kind 分组）-------------
//
// 共享内容和会议是两回事：会议存"谁在什么时候定了什么"，共享内容存"大家（含 Agent）
// 平时要取用的资料"。受众、存的东西、管控要求都不同，所以这里是独立的一块。
function sharedUrl(){
  var q = $("s-q").value.trim();
  var url, sep = "?";
  if (q){
    url = "/api/shared/search?q=" + encodeURIComponent(q);
    if ($("s-sem").checked) url += "&semantic=1";
    sep = "&";
  } else {
    url = "/api/shared";
  }
  url += sep + "scope=" + encodeURIComponent(state.sscope) + "&limit=50";
  var k = $("s-kind").value, o = $("s-owner").value, t = $("s-tag").value;
  if (k) url += "&kind=" + encodeURIComponent(k);
  if (o) url += "&owner=" + encodeURIComponent(o);
  if (t) url += "&tag=" + encodeURIComponent(t);
  return url;
}

function loadShared(){
  return api(sharedUrl()).then(function(res){
    var b = res.body || {};
    if (res.status !== 200 || !b.ok){
      // 上游还没上线 /shared 就是这条路：降级显示原因，不白屏
      state.shared = {down: true,
                      why: b.detail || b.error || ("HTTP " + res.status)};
    } else {
      banner("");
      state.shared = b;
      collectSharedFacets(b);
    }
    renderShared();
  }).catch(function(e){
    state.shared = {down: true, why: String(e)};
    renderShared();
  });
}

function sharedItems(payload){
  var rows = (payload.items || []).slice();
  var tasks = payload.tasks || [];
  for (var i = 0; i < tasks.length; i++){
    var sub = tasks[i].items || [];
    for (var j = 0; j < sub.length; j++) rows.push(sub[j]);
  }
  return rows;
}

function collectSharedFacets(payload){
  var rows = sharedItems(payload);
  for (var i = 0; i < rows.length; i++){
    var r = rows[i];
    if (r.kind) state.sfacets.kind[r.kind] = true;
    if (r.owner) state.sfacets.owner[r.owner] = true;
    var tags = r.tags || [];
    for (var j = 0; j < tags.length; j++) if (tags[j]) state.sfacets.tag[tags[j]] = true;
  }
  syncSelect("s-kind", state.sfacets.kind);
  syncSelect("s-owner", state.sfacets.owner);
  syncSelect("s-tag", state.sfacets.tag);
}

// 高亮口径：已过期 = 红，待复审 / 7 天内将过期 = 黄，已撤下 = 灰。
// 上游给了 expired/review_due/expiring_soon 就用上游的；没给才拿 now 现算。
function sharedFlags(it, now){
  var f = {expired: false, soon: false, review: false,
           revoked: !!it.revoked, locked: !!(it.sensitive && it.authorized !== true)};
  var ms = function(ts){ var d = new Date(ts); return isNaN(d.getTime()) ? null : d.getTime(); };
  var nowMs = ms(now) === null ? Date.now() : ms(now);
  f.expired = (it.expired === true) ||
    (it.expired === undefined && it.expires_at && ms(it.expires_at) !== null && ms(it.expires_at) <= nowMs);
  if (!f.expired && it.expires_at){
    var left = ms(it.expires_at);
    f.soon = (it.expiring_soon === true) ||
      (it.expiring_soon === undefined && left !== null && left - nowMs <= 7 * 86400000);
  }
  if (it.review_at){
    var rv = ms(it.review_at);
    f.review = (it.review_due === true) ||
      (it.review_due === undefined && rv !== null && rv <= nowMs);
  }
  return f;
}

function sharedCard(it, now){
  var f = sharedFlags(it, now);
  var cls = "sitem";
  if (f.revoked) cls += " revoked";
  else if (f.expired) cls += " expired";
  else if (f.soon || f.review) cls += " soon";
  if (f.locked) cls += " locked";
  var box = el("div", cls);

  var head = el("div", "meta");
  head.appendChild(el("span", "id", "#" + (it.shared_id === undefined ? "?" : it.shared_id)));
  head.appendChild(el("span", "chip kind", SHARED_KIND_CN[it.kind] || it.kind || "其它"));
  if (f.locked) head.appendChild(badge("locked", ["locked"], "存在但无权查看"));
  if (f.revoked) head.appendChild(badge("revoked", ["revoked"], "已撤下"));
  if (f.expired) head.appendChild(badge("expired", ["expired"], "已过期"));
  else if (f.soon) head.appendChild(badge("soon", ["soon"], "7 天内到期"));
  if (f.review) head.appendChild(badge("review", ["review"], "待复审"));
  head.appendChild(el("span", "time", "v" + (it.version === undefined ? "?" : it.version)));
  box.appendChild(head);

  if (f.locked){
    // 标题与正文在服务端就被抹掉了（mask_shared），这里连能显示的字节都没有
    box.appendChild(el("div", "st", "🔒 存在但无权查看"));
    box.appendChild(el("div", "k", "这条内容标了 sensitive，当前身份没有授权：标题与正文都不下发。"));
  } else {
    box.appendChild(el("div", "st", it.title || "（无题）"));
    var text = String(it.body === undefined || it.body === null ? "" : it.body);
    if (text){
      var open = !!state.sopen[it.shared_id];
      var body = el("div", open ? "content" : "content clip", text);
      box.appendChild(body);
      if (text.length > 160 || text.indexOf("\\n") >= 0){
        var more = el("div", "more", open ? "收起 ▲" : "展开正文 ▼");
        more.onclick = (function(id){
          return function(){
            if (state.sopen[id]) delete state.sopen[id];
            else state.sopen[id] = true;
            renderShared();
          };
        })(it.shared_id);
        box.appendChild(more);
      } else if (!open){
        body.className = "content";
      }
    }
    var tags = it.tags || [];
    if (tags.length){
      var row = el("div", "tags");
      for (var t = 0; t < tags.length; t++) row.appendChild(el("span", "chip", tags[t]));
      box.appendChild(row);
    }
  }

  var kvs = el("div", "kvs");
  var put = function(label, value){
    var span = el("span");
    span.appendChild(el("b", null, label + "："));
    span.appendChild(el("span", null, value));
    kvs.appendChild(span);
  };
  put("维护责任人", it.owner || "（未指定）");
  put("最后更新", it.updated_at ? fmtTime(it.updated_at) : "（未记录）");
  put("过期时间", it.expires_at ? fmtDue(it.expires_at) : "不过期");
  put("下次复审", it.review_at ? fmtDue(it.review_at) : "（未排复审）");
  box.appendChild(kvs);

  var hist = it.history || [];
  if (hist.length){
    var link = el("div", "more", state.shist[it.shared_id]
      ? ("收起变更历史（" + hist.length + " 条）▲")
      : ("变更历史（" + hist.length + " 条）▼"));
    link.onclick = (function(id){
      return function(){
        if (state.shist[id]) delete state.shist[id];
        else state.shist[id] = true;
        renderShared();
      };
    })(it.shared_id);
    box.appendChild(link);
    if (state.shist[it.shared_id]){
      var hbox = el("div", "hist");
      for (var h = 0; h < hist.length; h++){
        var e = hist[h] || {};
        var fields = e.fields || e.changed || [];
        hbox.appendChild(el("div", "hrow", "v" + (e.version === undefined ? "?" : e.version) +
          "　" + (e.by || "（不详）") + "　" + (e.at ? fmtTime(e.at) : "（无时间）") +
          "　改了：" + (fields.length ? fields.join("、") : "（未记录字段）")));
      }
      box.appendChild(hbox);
    }
  }
  return box;
}

function taskGroup(task, now){
  var box = el("div", "grp");
  var head = el("div", "gh");
  head.appendChild(el("span", null, task.title || task.task_id || "（未命名任务）"));
  head.appendChild(el("span", "chip author", "负责人 " + (task.owner || "（未指定）")));
  var st = task.status || task.state || "";
  head.appendChild(el("span", "chip", TASK_STATE_CN[st] || st || "状态未记录"));
  if (task.destroyed || st === "destroyed"){
    head.appendChild(badge("revoked", ["revoked"], "已销毁"));
  } else if (task.ttl_expire_at){
    var leftMs = new Date(task.ttl_expire_at).getTime() -
                 (new Date(now).getTime() || Date.now());
    var leftTxt = isNaN(leftMs) ? "（TTL 未知）"
      : (leftMs <= 0 ? "TTL 已到期" : ("TTL 剩 " + Math.floor(leftMs / 3600000) + " 小时"));
    head.appendChild(badge(leftMs <= 0 ? "expired" : "ttl", ["expired", "ttl"], leftTxt));
  }
  head.appendChild(el("span", "n", (task.items || []).length + " 条"));
  box.appendChild(head);
  var line = el("div", "kvs");
  var dw = el("span");
  dw.appendChild(el("b", null, "销毁条件："));
  dw.appendChild(el("span", null, task.destroy_when || "（未写明销毁条件）"));
  line.appendChild(dw);
  box.appendChild(line);

  var items = task.items || [];
  if (!items.length){
    box.appendChild(el("div", "empty", "这个任务下暂无共享内容"));
  } else {
    for (var i = 0; i < items.length; i++) box.appendChild(sharedCard(items[i], now));
  }
  return box;
}

function renderShared(){
  var host = $("s-body");
  host.innerHTML = "";
  var d = state.shared;
  if (!d){
    host.appendChild(el("div", "empty", "加载中…"));
    stamp();
    return;
  }
  if (d.down){
    host.appendChild(el("div", "empty",
      "数据暂时不可用：" + d.why + "　——　共享内容走上游 /shared，等它上线这里就有了。"));
    $("s-count").textContent = "0";
    stamp();
    return;
  }
  var now = d.now || new Date().toISOString();
  var rows = sharedItems(d);
  $("s-count").textContent = rows.length;
  var hint = degradedText(d.degraded);
  if (hint) host.appendChild(el("div", "warnline", "提示：" + hint));

  if (state.sscope === "task"){
    var tasks = (d.tasks || []).slice();
    if (!tasks.length){
      // 上游只给了扁平 items 时，按 task_id 自己分组
      var byTask = {}, order = [];
      for (var i = 0; i < rows.length; i++){
        var r = rows[i];
        if (r.scope && r.scope !== "task") continue;
        var key = r.task_id || "（未归属任务）";
        if (!byTask[key]){ byTask[key] = {task_id: key, title: r.task_title || key, items: []};
                           order.push(key); }
        byTask[key].items.push(r);
      }
      for (var o = 0; o < order.length; o++) tasks.push(byTask[order[o]]);
    }
    if (!tasks.length){
      host.appendChild(el("div", "empty", "暂无任务级共享内容"));
    } else {
      for (var t = 0; t < tasks.length; t++) host.appendChild(taskGroup(tasks[t], now));
    }
  } else {
    var groups = {}, names = [];
    for (var j = 0; j < rows.length; j++){
      var it = rows[j];
      if (it.scope && it.scope !== "permanent") continue;
      var kind = it.kind || "other";
      if (!groups[kind]){ groups[kind] = []; names.push(kind); }
      groups[kind].push(it);
    }
    names.sort(function(a, b){
      var ia = SHARED_KIND_ORDER.indexOf(a), ib = SHARED_KIND_ORDER.indexOf(b);
      if (ia < 0) ia = 99;
      if (ib < 0) ib = 99;
      return ia - ib;
    });
    if (!names.length){
      host.appendChild(el("div", "empty", "暂无永久共享内容"));
    } else {
      for (var n = 0; n < names.length; n++){
        var grp = el("div", "grp");
        var gh = el("div", "gh");
        gh.appendChild(el("span", null, SHARED_KIND_CN[names[n]] || names[n]));
        gh.appendChild(el("span", "n", groups[names[n]].length + " 条"));
        grp.appendChild(gh);
        for (var m = 0; m < groups[names[n]].length; m++){
          grp.appendChild(sharedCard(groups[names[n]][m], now));
        }
        host.appendChild(grp);
      }
    }
  }
  stamp();
}

// --- Agent 查询手册：端点清单 + 可直接复制的 curl（示例用当前实际数据）---
function curlBlock(host, title, cmd){
  host.appendChild(el("div", "p", title));
  host.appendChild(el("div", "code", cmd));
}

function renderManual(){
  var host = $("man-body");
  host.innerHTML = "";
  host.appendChild(el("div", "p",
    "这三个模块都能被 Agent 直接检索：页面看到的每一块数据，背后都是下面这些**只读**端点。" +
    "看板自己带 token 转发到黑板；直接打黑板要自己带 Authorization: Bearer <token>（token → 成员名，" +
    "sensitive 内容需要授权才下发，audience 不匹配的条目上游不会返回）。写请求（POST/PUT/PATCH/DELETE）一律 405。"));

  var eps = [
    ["会议", "/api/sessions?state=&topic=&since=&limit=", "会议场次列表（日历就是按 started_at 按天聚合的）"],
    ["会议", "/api/sessions/<session_id>", "单场详情：目标 / 议程 / 确认 / 落实 / 遗留 / 逐条发言"],
    ["会议", "/api/entries?topic=&kind=&author=&limit=", "完整留痕：黑板原始流水"],
    ["会议", "/api/search?q=&limit=", "黑板语义检索"],
    ["共享内容", "/api/shared?scope=task|permanent&kind=&owner=&tag=&limit=", "共享内容清单（任务级 / 永久）"],
    ["共享内容", "/api/shared/search?q=&semantic=1&kind=&owner=&tag=", "共享内容检索，semantic=1 走语义"],
    ["跟踪", "/api/tracking?overdue=&limit=", "跨会未闭环总清单 + 计数"],
    ["跟踪", "/api/actions?status=&owner=&overdue=", "落实事项"],
    ["跟踪", "/api/issues?status=&owner=&kind=defect", "遗留问题 / 疑似缺陷"]
  ];
  for (var i = 0; i < eps.length; i++){
    var row = el("div", "p");
    row.appendChild(el("span", "chip topic", eps[i][0]));
    row.appendChild(el("span", "ep", "　" + eps[i][1] + "　"));
    row.appendChild(el("span", null, eps[i][2]));
    host.appendChild(row);
  }

  // 示例里的 id / 关键词用当前页面上真有的那一条，复制就能跑
  var sid = state.sid || (state.sessions.length ? state.sessions[0].session_id : "<session_id>");
  var owner = "";
  var track = state.track || {};
  var acts = track.actions || [];
  if (acts.length && acts[0].owner) owner = acts[0].owner;
  var base = "127.0.0.1:9941";
  curlBlock(host, "① 会议：某一天有哪些会（列表里带 started_at，按北京时间切日即可）",
    "curl -s " + base + "/api/sessions?limit=50");
  curlBlock(host, "② 会议：这一场的四要素 + 全部发言",
    "curl -s " + base + "/api/sessions/" + sid);
  curlBlock(host, "③ 共享内容：永久区 + 语义检索（sensitive 未授权只回骨架，没有标题正文）",
    "curl -s '" + base + "/api/shared?scope=permanent&limit=50'\\n" +
    "curl -s '" + base + "/api/shared/search?q=备份策略&semantic=1'");
  curlBlock(host, "④ 跟踪：还没闭环的落实与遗留（只看逾期加 overdue=1" +
    (owner ? "；只看某人加 owner=" + owner : "") + "）",
    "curl -s '" + base + "/api/tracking?overdue=1&limit=50'" +
    (owner ? "\\ncurl -s '" + base + "/api/actions?owner=" + owner + "&status=open'" : ""));
  curlBlock(host, "⑤ 直接打黑板（9940）要自带 token；看板这一层已经替你带过了",
    "curl -s -H 'Authorization: Bearer <token>' <NODE_IP>:9940/tracking");
  host.appendChild(el("div", "p",
    "只读约定：以上端点只接 GET / HEAD；任何写请求都会拿到 405 method_not_allowed。"));
}

// --- 自动刷新 -------------------------------------------------------
function stopAuto(){
  if (state.timer){ clearInterval(state.timer); state.timer = null; }
  $("tick").textContent = "";
}

function startAuto(){
  stopAuto();
  if (!CFG.refresh) return;
  state.left = CFG.refresh;
  state.timer = setInterval(function(){
    state.left -= 1;
    if (state.left <= 0){ state.left = CFG.refresh; reloadCurrent(); }
    $("tick").textContent = "（" + state.left + "s）";
  }, 1000);
}

// 刷新只动当前模块的数据：模块、二级页、已展开的条目/发言、选中的日期与场次全部不变
function reloadCurrent(){
  loadHealth();
  if (state.mod === "meet"){
    if (state.sub === "board"){
      load();                       // 完整留痕 = 原来的黑板流水
    } else {
      loadSessions();
      loadTracking();               // 日历上的"有遗留 / 有逾期"要靠跟踪清单来判断
      if (state.sid) loadDetail(state.sid);
    }
  } else if (state.mod === "shared"){
    loadShared();
  } else {
    loadTracking();
  }
  renderManual();
}

function refreshNow(){ reloadCurrent(); if ($("auto").checked) startAuto(); }

// --- 绑定 -----------------------------------------------------------
$("go").onclick = refreshNow;
$("clear").onclick = function(){ $("q").value = ""; refreshNow(); };
$("refresh").onclick = refreshNow;
$("q").addEventListener("keydown", function(ev){ if (ev.key === "Enter") refreshNow(); });
$("f-limit").onchange = refreshNow;
$("f-author").onchange = refreshNow;
$("f-topic").onchange = refreshNow;
$("f-kind").onchange = refreshNow;

var segBtns = $("modeseg").getElementsByTagName("button");
for (var i = 0; i < segBtns.length; i++){
  segBtns[i].onclick = (function(btn){
    return function(){
      for (var j = 0; j < segBtns.length; j++) segBtns[j].className = "";
      btn.className = "on";
      state.mode = btn.getAttribute("data-mode");
      $("q").placeholder = state.mode === "semantic"
        ? "搜索：用一句话描述你想找的内容（语义检索）"
        : "搜索：在已加载条目里做关键词匹配";
      refreshNow();
    };
  })(segBtns[i]);
}

var tabBtns = $("tabs").getElementsByTagName("button");
for (var ti = 0; ti < tabBtns.length; ti++){
  tabBtns[ti].onclick = (function(btn){
    return function(){ switchMod(btn.getAttribute("data-mod")); };
  })(tabBtns[ti]);
}

var subBtns = $("subtabs").getElementsByTagName("button");
for (var si = 0; si < subBtns.length; si++){
  subBtns[si].onclick = (function(btn){
    return function(){ switchSub(btn.getAttribute("data-sub")); };
  })(subBtns[si]);
}

$("cal-prev").onclick = function(){ shiftMonth(-1); };
$("cal-next").onclick = function(){ shiftMonth(1); };
$("cal-recent").onclick = jumpRecentMonth;

$("m-state").onchange = function(){ state.sid = null; state.detail = null; refreshNow(); };
$("m-clear").onclick = function(){ $("m-q").value = ""; renderSessions(); };
$("m-q").addEventListener("keydown", function(ev){ if (ev.key === "Enter") renderSessions(); });
$("m-q").addEventListener("input", renderSessions);
$("m-range").onchange = renderSessions;
$("m-member").onchange = renderSessions;
$("m-unconf").onchange = renderSessions;
$("m-risk").onchange = renderSessions;

$("man-top").onclick = function(){
  var man = $("man");
  man.open = true;
  man.scrollIntoView(true);
};

// 责任人 / 状态是本地筛选（数据都在手里），只有"只看逾期"要回上游重拉一遍
$("k-owner").onchange = renderTracking;
$("k-status").onchange = renderTracking;
$("k-kind").onchange = renderTracking;
$("k-overdue").onchange = function(){ loadTracking(); };

var scopeBtns = $("s-scope").getElementsByTagName("button");
for (var qi = 0; qi < scopeBtns.length; qi++){
  scopeBtns[qi].onclick = (function(btn){
    return function(){
      state.sscope = btn.getAttribute("data-scope");
      for (var j = 0; j < scopeBtns.length; j++){
        scopeBtns[j].className = (scopeBtns[j] === btn) ? "on" : "";
      }
      loadShared();
    };
  })(scopeBtns[qi]);
}

$("s-go").onclick = loadShared;
$("s-clear").onclick = function(){ $("s-q").value = ""; loadShared(); };
$("s-q").addEventListener("keydown", function(ev){ if (ev.key === "Enter") loadShared(); });
$("s-sem").onchange = loadShared;
$("s-kind").onchange = loadShared;
$("s-owner").onchange = loadShared;
$("s-tag").onchange = loadShared;

$("auto").checked = !!CFG.refresh;
$("auto").disabled = !CFG.refresh;
$("auto").onchange = function(){ if (this.checked) startAuto(); else stopAuto(); };

paintMod();
paintSub();
renderDetail();
refreshNow();
if (CFG.refresh) startAuto();
</script>
</body>
</html>
"""


def render_page(userbar: str = "") -> str:
    """产出自包含页面。

    页面里唯一的动态内容是刷新秒数（整数），用 JSON 注入；条目正文一个字节都不在
    服务端拼进 HTML —— 全部由浏览器 fetch /api/* 拿到后用 textContent 渲染。
    userbar 是登录后右上角那条「谁在看 · 改密 · 退出」，未登录 / 未装认证时是空串。
    """
    cfg = {"refresh": int(Runtime.refresh_seconds), "version": VERSION}
    page = PAGE_HTML.replace("__CFG_JSON__", json.dumps(cfg))
    if userbar:
        page = page.replace("<body>", "<body>\n" + userbar, 1)
    return page


# --- webPortal 首页卡片墙（v1.5）-----------------------------------------
#
# 根路径现在是「入口墙」：一个字节的黑板数据都不在这里渲染，卡片就是几个静态
# 链接 + 一段说明。登录态由 _route() 的门禁负责 —— 未登录的人第一屏仍然是 /login，
# 这张墙对陌生人不出现。要加卡片，改 PORTAL_CARDS 一处即可。
#
# ready=False 的卡在目标系统公网入口开通前**只展示不开跳**：点了给一句解释，
# 不给出一个打不开的死链。开通后把它改成 True 就点亮，不用动别的。
#
#
# 公网落点（2026-09-22 上线，同一朵 Cloudflare 命名隧道 ed201266，见 <HOST>
# <cloudflared-dir>/config.yml）：apex `<DOMAIN>` 与 `board.<DOMAIN>`
# 都指本实例（127.0.0.1:9942，强制登录），所以桌面/手机直接敲域名进的就是这张卡
# 片墙，白板本尊在 /board；`chat.<DOMAIN>` 由 chat 公网实例（127.0.0.1:9980）
# 承载，卡片 2 指向它 —— 两张卡今天都是 ready=True。
# 卡数口径（<USER_ID> #187 批示，2026-09-22）：**就两张**，不设第三张。原三张卡方案里
# 那张「圆桌会议」卡删掉——会议本来就在「团队看板」的会议页签里，单列一张卡等于
# 一个入口两个名字。要再加卡必须先在群里拿到点头，不许只改这一处就上线。

CHAT_URL = os.environ.get("AGENT_TEAM_CHAT_URL") or "https://chat.%s/" % AGENT_TEAM_DOMAIN

PORTAL_CARDS = (
    {
        "icon": "🗂",
        "title": "团队看板",
        "desc": "白板 · 会议 · 共享内容 · 跟踪。只读，看的是黑板上的留痕与结论。",
        "href": BOARD_PATH,
        "cta": "进入看板",
        "badge": "已开通",
        "ready": True,
    },
    {
        "icon": "💬",
        "title": "人机对话台",
        "desc": "群聊：和人、和各路 Agent 在同一个会话里说话，会议也在里面开。",
        "href": CHAT_URL,
        "cta": "进入对话台",
        "badge": "已开通",
        "ready": True,
    },
)

PORTAL_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<title>团队门户 · example.com</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:#0f1116;color:#e7eaf0;
     font:16px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
     -webkit-font-smoothing:antialiased}
.wrap{max-width:980px;margin:0 auto;padding:clamp(28px,7vw,64px) clamp(16px,5vw,28px) 48px}
h1{margin:0 0 8px;font-size:clamp(24px,6vw,34px);letter-spacing:.5px}
.sub{margin:0 0 clamp(22px,5vw,34px);color:#98a2b3;font-size:15px}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.card{display:flex;flex-direction:column;gap:10px;padding:clamp(18px,4vw,24px);
      background:#161a22;border:1px solid #2a3140;border-radius:16px;
      color:inherit;text-decoration:none;min-height:180px;
      transition:border-color .15s,transform .15s,background .15s}
a.card:hover{border-color:#5b9cff;background:#1a2029;transform:translateY(-2px)}
a.card:focus-visible{outline:2px solid #5b9cff;outline-offset:2px}
.card.locked{cursor:default;opacity:.72;border-style:dashed}
.ico{font-size:28px;line-height:1}
.ttl{display:flex;align-items:center;gap:10px;font-size:19px;font-weight:600;flex-wrap:wrap}
.badge{font-size:12px;font-weight:400;padding:2px 9px;border-radius:999px;
       border:1px solid #2c5c38;background:#12251a;color:#9ae6b4;white-space:nowrap}
.badge.wait{border-color:#5c4a2b;background:#2a2214;color:#ffd79a}
.desc{margin:0;color:#98a2b3;font-size:14px;flex:1}
.cta{margin-top:auto;color:#5b9cff;font-size:14px;font-weight:600}
.card.locked .cta{color:#98a2b3}
.foot{margin:clamp(26px,6vw,40px) 0 0;color:#697586;font-size:13px}
.foot code{background:#1c212b;padding:1px 5px;border-radius:5px}
#ptip{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);max-width:88vw;
      background:#1c212b;border:1px solid #2a3140;border-radius:12px;padding:10px 16px;
      font-size:14px;color:#e7eaf0;box-shadow:0 8px 30px rgba(0,0,0,.5);display:none}
</style>
</head>
<body>
__USERBAR__
<main class="wrap">
  <h1>团队门户</h1>
  <p class="sub">一个入口，进各自的系统。这块只管带路，不展示数据。</p>
  <div class="grid">
__CARDS__
  </div>
  <p class="foot">登录一次即可在各系统之间通行；<code>board.example.com/board</code> 是老白板地址，旧书签会自动跳过去。</p>
</main>
<div id="ptip"></div>
<script>
// 未开通的卡：点了不跳死链，给一句人话。
(function () {
  var tip = document.getElementById('ptip'), timer = null;
  function say(text) {
    tip.textContent = text;
    tip.style.display = 'block';
    clearTimeout(timer);
    timer = setTimeout(function () { tip.style.display = 'none'; }, 4000);
  }
  Array.prototype.forEach.call(document.querySelectorAll('.card.locked'), function (el) {
    el.addEventListener('click', function (ev) {
      ev.preventDefault();
      say('「' + el.querySelector('.ttl').firstChild.nodeValue.trim() + '」的公网入口还在开通中，开通后这张卡自动点亮。');
    });
  });
})();
</script>
</body>
</html>
"""


def render_portal(userbar: str = "") -> str:
    """webPortal 首页。卡片全部由 PORTAL_CARDS 生成，样式/脚本内联，自包含。"""
    cards = []
    for card in PORTAL_CARDS:
        ready = bool(card.get("ready"))
        tag = "a" if ready else "div"
        if ready:
            attrs = ' href="%s"' % _esc(card["href"])
        else:
            attrs = ' role="link" aria-disabled="true" tabindex="0" data-locked="1"'
        cards.append(
            '<%s class="card%s"%s>\n'
            '  <div class="ico" aria-hidden="true">%s</div>\n'
            '  <div class="ttl">%s<span class="badge%s">%s</span></div>\n'
            '  <p class="desc">%s</p>\n'
            '  <div class="cta">%s</div>\n'
            '</%s>' % (tag, "" if ready else " locked", attrs,
                       card["icon"], _esc(card["title"]),
                       "" if ready else " wait", _esc(card["badge"]),
                       _esc(card["desc"]), _esc(card["cta"]), tag))
    page = PORTAL_HTML.replace("__CARDS__", "\n".join(cards))
    page = page.replace("__USERBAR__", userbar or "", 1)
    # 页面里的域名走 AGENT_TEAM_DOMAIN（默认与原字面量等价，行为零变更）
    return page.replace("example.com", AGENT_TEAM_DOMAIN)


# --- 登录页 / 账号页（自包含、内联样式、无任何外部资源）------------------
#
# 单独一套壳，不复用看板那张大页面：登录页是**未认证的人唯一能看到的东西**，
# 它越小越好读越好审，不该把几千行看板 JS 也送到陌生人浏览器里。

SHELL_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>__TITLE__</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
     padding:24px 16px;background:#0f1116;color:#e7eaf0;
     font:16px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.card{width:100%;max-width:420px;background:#161a22;border:1px solid #2a3140;
      border-radius:14px;padding:clamp(20px,5vw,32px)}
h1{margin:0 0 6px;font-size:clamp(20px,5vw,24px)}
.sub{margin:0 0 20px;color:#98a2b3;font-size:14px}
label{display:block;margin:14px 0 6px;font-size:14px;color:#98a2b3}
input[type=text],input[type=password]{width:100%;min-height:44px;padding:10px 12px;
  border-radius:10px;border:1px solid #2a3140;background:#1c212b;color:#e7eaf0;font-size:16px}
input:focus{outline:2px solid #5b9cff;outline-offset:1px}
button{width:100%;min-height:44px;margin-top:20px;border:0;border-radius:10px;
  background:#5b9cff;color:#0f1116;font-size:16px;font-weight:600;cursor:pointer}
button.ghost{background:transparent;border:1px solid #2a3140;color:#e7eaf0;font-weight:400}
.banner{margin:0 0 16px;padding:10px 12px;border-radius:10px;font-size:14px;
  border:1px solid #2a3140;word-break:break-word}
.bad{background:#2a1417;border-color:#5c2b30;color:#ffb4b4}
.good{background:#12251a;border-color:#2c5c38;color:#9ae6b4}
.foot{margin:22px 0 0;color:#98a2b3;font-size:13px}
.foot a{color:#5b9cff}
code{background:#1c212b;padding:1px 5px;border-radius:5px;font-size:13px}
hr{border:0;border-top:1px solid #2a3140;margin:26px 0}
@media (max-width:480px){.card{border:0;border-radius:0;padding:20px 16px;background:#0f1116}}
</style>
</head>
<body>
<main class="card">
__BODY__
</main>
</body>
</html>
"""


def _shell(title: str, body: str) -> str:
    return SHELL_HTML.replace("__TITLE__", _esc(title)).replace("__BODY__", body)


def _banner(text: str, good: bool = False) -> str:
    if not text:
        return ""
    return '<p class="banner %s">%s</p>' % ("good" if good else "bad", _esc(text))


def render_login(csrf_token: str, next_url: str = "/", error: str = "",
                 notice: str = "", username: str = "") -> str:
    """登录页。错误提示一律是上游给的那一句，**不区分"没这个账号"和"口令错"**。"""
    body = (
        "<h1>团队门户 · 登录</h1>\n"
        '<p class="sub">HACS 平台 · 一个入口，进各自的系统</p>\n'
        + _banner(notice, good=True) + _banner(error) +
        '<form method="post" action="%s" accept-charset="utf-8">\n' % LOGIN_PATH +
        '<input type="hidden" name="action" value="login">\n'
        '<input type="hidden" name="next" value="%s">\n' % _esc(safe_next(next_url)) +
        '<input type="hidden" name="%s" value="%s">\n' % (CSRF_FIELD, _esc(csrf_token)) +
        '<label for="user">账号</label>\n'
        '<input id="user" name="user" type="text" value="%s" required autofocus\n'
        '       autocapitalize="off" autocorrect="off" spellcheck="false"\n'
        '       autocomplete="username" maxlength="64">\n' % _esc(username) +
        '<label for="password">口令</label>\n'
        '<input id="password" name="password" type="password" required\n'
        '       autocomplete="current-password" maxlength="256">\n'
        "<button type=\"submit\">登录</button>\n"
        "</form>\n"
        '<p class="foot">门户只做入口、只读：登录后进的是看板与人机对话台，'
        "写黑板的入口这里一个都没有。"
        "忘了口令请找主人用 <code>view_auth.py passwd</code> 重置。</p>"
    )
    return _shell("团队门户 · 登录", body)


def render_account(visitor: Visitor, csrf_token: str, notice: str = "",
                   error: str = "") -> str:
    """账号页：改密 + 退出。两个动作都 POST 回 /login，共用同一个 CSRF。"""
    role_cn = view_auth.ROLE_CN.get(visitor.role or "", visitor.role or "-")
    hidden = '<input type="hidden" name="%s" value="%s">\n' % (CSRF_FIELD, _esc(csrf_token))
    body = (
        "<h1>账号</h1>\n"
        '<p class="sub">当前登录：%s（%s）</p>\n' % (_esc(visitor.username), _esc(role_cn)) +
        _banner(notice, good=True) + _banner(error) +
        '<form method="post" action="%s" accept-charset="utf-8">\n' % LOGIN_PATH +
        '<input type="hidden" name="action" value="passwd">\n' + hidden +
        '<label for="old">原口令</label>\n'
        '<input id="old" name="old" type="password" required autocomplete="current-password">\n'
        '<label for="new">新口令（至少 %d 位）</label>\n' % view_auth.PASSWORD_MIN +
        '<input id="new" name="new" type="password" required autocomplete="new-password"\n'
        '       minlength="%d" maxlength="%d">\n' % (view_auth.PASSWORD_MIN,
                                                     view_auth.PASSWORD_MAX) +
        "<button type=\"submit\">修改口令</button>\n"
        "</form>\n<hr>\n"
        '<form method="post" action="%s" accept-charset="utf-8">\n' % LOGIN_PATH +
        '<input type="hidden" name="action" value="logout">\n' + hidden +
        '<button type="submit" class="ghost">退出登录</button>\n'
        "</form>\n"
        '<p class="foot"><a href="' + BOARD_PATH + '">← 回看板</a>　改密会立刻作废你所有的登录'
        "（包括手上这台设备），改完要用新口令重新登录。</p>"
    )
    return _shell("团队黑板 · 账号", body)


_BAR_STYLE = ("position:sticky;top:0;z-index:50;display:flex;gap:12px;align-items:center;"
              "justify-content:flex-end;flex-wrap:wrap;padding:6px 14px;background:#1c212b;"
              "border-bottom:1px solid #2a3140;font-size:13px;color:#98a2b3")
_BAR_LINK = "color:#5b9cff;text-decoration:none;min-height:24px;line-height:24px"
_BAR_BTN = ("background:transparent;border:1px solid #2a3140;border-radius:8px;"
            "color:#e7eaf0;padding:2px 12px;min-height:28px;font-size:13px;cursor:pointer")


def render_userbar(name: str, role_cn: str, csrf_token: str) -> str:
    """看板顶上那条细横幅。样式走行内 style（CSP 已放行 inline），
    这样一个字符都不用动看板那张大页面的 <style>。"""
    who = '<span>%s · %s</span>' % (_esc(name), _esc(role_cn))
    if not csrf_token:                  # tailnet 直连：没账号可改，也没会话可退
        return '<div style="%s">%s</div>' % (_BAR_STYLE, who)
    return (
        '<form method="post" action="%s" accept-charset="utf-8" style="%s">\n'
        % (LOGIN_PATH, _BAR_STYLE) + who +
        '<a href="%s" style="%s">门户</a>\n' % (PORTAL_PATH, _BAR_LINK) +
        '<a href="%s" style="%s">改密</a>\n' % (ACCOUNT_PATH, _BAR_LINK) +
        '<input type="hidden" name="action" value="logout">\n'
        '<input type="hidden" name="%s" value="%s">\n' % (CSRF_FIELD, _esc(csrf_token)) +
        '<button type="submit" style="%s">退出</button>\n</form>' % _BAR_BTN
    )


# --- 启动 -----------------------------------------------------------

def setup_logging(log_file: Optional[str]) -> None:
    """单行日志：stdout（systemd 收得到）+ 可选文件。日志里永远不出现 token。"""
    handlers = [logging.StreamHandler(sys.stdout)]  # type: List[logging.Handler]
    if log_file:
        try:
            directory = os.path.dirname(log_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as exc:
            sys.stdout.write("日志文件不可写(%s)，仅输出到 stdout\n" % exc)
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")


def make_server(host: str, port: int, handler: Any = ViewHandler) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="团队黑板·只读看板（纯标准库）")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址，默认 127.0.0.1；部署时传 tailnet 的 100.x 地址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="监听端口，默认 %d" % DEFAULT_PORT)
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM,
                        help="黑板服务地址，默认 " + DEFAULT_UPSTREAM)
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE,
                        help="Bearer token 文件路径，默认 " + DEFAULT_TOKEN_FILE)
    parser.add_argument("--log-file", default=None, help="单行日志文件路径（可选）")
    parser.add_argument("--refresh-seconds", type=int, default=DEFAULT_REFRESH_S,
                        help="页面自动刷新秒数，默认 %d；0 表示关闭" % DEFAULT_REFRESH_S)

    # --- v1.4 认证 ---
    # **默认 public**：忘了传 --mode 的后果应该是"多输一次口令"，
    # 绝不该是"把主人画像挂到公网上"。
    parser.add_argument("--mode", choices=MODES, default="public",
                        help="public=一律要登录（默认，公网部署用）；"
                             "tailnet=完全不装认证（只给内网/本机调试用）")
    parser.add_argument("--session-key-file", default=DEFAULT_SESSION_KEY_FILE,
                        help="会话签名密钥文件，默认 " + DEFAULT_SESSION_KEY_FILE
                             + "（用 view_auth.py init-key 生成）")
    parser.add_argument("--dsn-file", default=DEFAULT_DSN_FILE,
                        help="账号库的数据库连接串文件，默认 " + DEFAULT_DSN_FILE)
    parser.add_argument("--sqlite", default=None,
                        help="改用本地文件账号库（本地演示 / 单测用），与 --dsn-file 二选一")
    parser.add_argument("--secure-cookie", action="store_true",
                        help="强制给会话 cookie 打 Secure；不加则看 X-Forwarded-Proto 自动判断")
    parser.add_argument("--trust-proxy", default="",
                        help="受信反代的 IP 或 CIDR，逗号分隔（挂 Cloudflare 隧道时传 "
                             "127.0.0.1）。只有对端 IP 命中这个列表，锁定键与审计 IP 才采信 "
                             "CF-Connecting-IP / X-Forwarded-For；**免登录白名单永远不看头**。"
                             "默认空 = 谁都不信")
    return parser


def setup_auth(args: argparse.Namespace) -> bool:
    """按 --mode 装配认证。装不上就返回 False，让 main 直接不启动。

    **public 模式装不上认证时宁可不启动** —— 一个"起来了但谁都能看"的看板，
    比一个起不来的看板危险得多：前者没人会发现，后者五秒钟就有人来问。
    """
    Runtime.mode = args.mode
    Runtime.secure_cookie = bool(getattr(args, "secure_cookie", False))
    if args.mode != "public":
        Runtime.auth = None
        LOG.warning("--mode tailnet：不装认证，任何能连上这个端口的人都能看全量内容。"
                    "只有确认端口只在内网/本机可达时才该这么跑。")
        return True
    try:
        store = view_auth.open_store(args)
        Runtime.auth = view_auth.build_auth(store, args.session_key_file, logger=LOG)
    except Exception as exc:
        LOG.error("public 模式装配认证失败，拒绝启动：%s", view_auth.safe_err(exc))
        LOG.error("先跑 view_auth.py init-key / adduser 把密钥和账号建好，"
                  "或者确认 --dsn-file / --sqlite 指对了地方")
        return False
    LOG.info("认证已装配：mode=public 密钥=%s 账号库=%s",
             args.session_key_file, args.sqlite or args.dsn_file)
    return True


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_file)

    scheme = urllib.parse.urlsplit(args.upstream).scheme
    if scheme not in ("http", "https"):
        LOG.error("--upstream 必须是 http/https 地址，拿到的是 %r", args.upstream)
        return 2
    Runtime.upstream = args.upstream.rstrip("/")
    Runtime.refresh_seconds = clamp_refresh(args.refresh_seconds)
    Runtime.token_file = args.token_file

    Runtime.trust_proxy = parse_trust_proxy(getattr(args, "trust_proxy", ""))
    if Runtime.trust_proxy:
        LOG.info("受信反代：%s —— 来自这些地址的请求，锁定键与审计 IP 采信 "
                 "CF-Connecting-IP / X-Forwarded-For（免登录白名单仍只看对端 IP）",
                 ", ".join(str(n) for n in Runtime.trust_proxy))

    # token 读不到不退出：页面照样开得起来，只是每个请求都会亮"上游拒绝凭据"的黄条，
    # 运维一眼就能看出是 token 的问题，而不是看见一个死掉的服务。
    try:
        Runtime.token = load_token(args.token_file)
        Runtime.token_loaded = bool(Runtime.token)
    except OSError as exc:
        Runtime.token = ""
        Runtime.token_loaded = False
        LOG.error("读取 token 文件失败(%s)：%s", args.token_file, exc.strerror or "unknown")
    if not Runtime.token_loaded:
        LOG.warning("token 为空，上游会一律 401，本服务对应返回 502")

    if not setup_auth(args):
        return 2

    try:
        httpd = make_server(args.host, args.port)
    except (OSError, socket.error) as exc:
        LOG.error("监听失败 %s:%d: %s", args.host, args.port, _scrub(exc))
        return 2

    LOG.info("blackboard_view %s listening on %s:%d upstream=%s token=%s refresh=%ds mode=%s",
             VERSION, httpd.server_address[0], httpd.server_address[1],
             Runtime.upstream, "loaded" if Runtime.token_loaded else "MISSING",
             Runtime.refresh_seconds, Runtime.mode)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("收到中断，退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
