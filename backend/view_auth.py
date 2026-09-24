#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
view_auth.py —— 只读看板（blackboard_view）的账号 / 会话 / 审计（纯标准库）

一句话：这条看板要开到公网上，里面有节点公网 IP、各机装了什么 Agent、主人画像与
关键信息 —— 对外之前先给它上一道门槛。本文件就是那道门槛的**内层**：账号、口令、
会话、角色、暴力破解防护、审计。外层 TLS 由 Cloudflare 隧道终结，不归这里管。

设计要点（与 blackboard_svc / shared_content 同一套口径）：
  * 纯标准库：hashlib / hmac / secrets / sqlite3 / argparse，**零第三方依赖**。
    只有真连 PostgreSQL 时才延迟导入 psycopg2（跑单测、跑替身都不需要）。
  * **口令只存哈希**：hashlib.scrypt，每个账号一条独立随机 salt，落库的是
    `scrypt$n$r$p$<salt_b64>$<dk_b64>`。明文在进程里活不过一次校验，
    **不进库、不进日志、不进命令行**（压根没有 --password 这个参数，
    只收 --password-stdin 或交互输入）。
  * **会话只存指纹**：库里是 sha256(原始 token)；cookie 里是"原始 token + HMAC 签名"，
    签名密钥从 --session-key-file / 环境变量读，**绝不硬编码**。
    → 拿到整张表反推不出 cookie；拿到 cookie 没密钥过不了签名。
  * **角色三级**：viewer(0) < member(1) < owner(2)，对应内容密级
    public(0) < internal(1) < sensitive(2)；看不了的东西**在服务端就抹掉**，
    只留"存在但无权查看"，标题 / 正文 / 标签 / 责任人一个字节都不下发。
  * **审计 append-only**：登录成功 / 失败 / 锁定 / 退出 / 改密 / 越权尝试全落
    view_audit，DELETE 与 TRUNCATE 由数据库触发器拒（见 sql/view_auth_schema.sql）。
  * **暴力破解**：同一 IP 5 分钟内失败 5 次 → 锁 15 分钟。内存 + 表双记：
    内存快，表让"重启进程"不等于"解锁"。

用法（服务器上）：
    # 会话签名密钥，只生成一次，权限 600
    python3 view_auth.py init-key --key-file <deploy-dir>/view-session.key

    # 建账号：口令走 stdin，不进 shell 历史
    printf '%s' "$PASS" | python3 view_auth.py adduser --user gangge --role owner \\
        --password-stdin --dsn-file <deploy-dir>/pg.env

    python3 view_auth.py list --dsn-file <deploy-dir>/pg.env
    python3 view_auth.py sessions --revoke-all --user guest1 --dsn-file <deploy-dir>/pg.env
    python3 view_auth.py audit --limit 20 --dsn-file <deploy-dir>/pg.env

--dsn-file 指向的是 **systemd 环境文件**（<deploy-dir>/pg.env），取值等价于
`sed -n 's/^BB_PG_DSN=//p' <file> | head -1`。**绝不能 bash source 它** —— 值里带
空格，shell 会把 DSN 截断成错值。
"""

import argparse
import base64
import datetime
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import threading
import time
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

VERSION = "1.0"

DEFAULT_DSN_FILE = "<deploy-dir>/pg.env"
DEFAULT_KEY_FILE = "<deploy-dir>/view-session.key"
KEY_ENV = "BB_VIEW_SESSION_KEY"          # 备选：直接给 base64 的密钥（systemd 环境变量）

# --- 角色与密级 -----------------------------------------------------
ROLES = ("viewer", "member", "owner")
ROLE_RANK = {"viewer": 0, "member": 1, "owner": 2}
ROLE_CN = {"viewer": "只读访客", "member": "成员", "owner": "主人"}

SENSITIVITY = ("public", "internal", "sensitive")
SENSITIVITY_RANK = {"public": 0, "internal": 1, "sensitive": 2}
SENSITIVITY_CN = {"public": "公开", "internal": "内部", "sensitive": "敏感"}
# 没标密级的共享内容按 internal 算 —— 默认收紧，不是默认放开
DEFAULT_SENSITIVITY = "internal"

# --- 口令哈希（scrypt）---------------------------------------------
SCRYPT_N = 16384                 # 2**14，约 16MB 内存 / 次，登录这种低频操作完全扛得住
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
SALT_BYTES = 16
PASSWORD_MIN = 8
PASSWORD_MAX = 256

# --- 会话 -----------------------------------------------------------
SESSION_COOKIE = "bbview_session"
CSRF_COOKIE = "bbview_csrf"
SESSION_TTL_S = 12 * 3600        # 默认 12 小时
SESSION_RENEW_S = 6 * 3600       # 剩余不足 6 小时且有活动 -> 自动续期到满 12 小时
TOKEN_BYTES = 32
KEY_BYTES = 32

# --- 暴力破解 -------------------------------------------------------
FAIL_WINDOW_S = 300              # 5 分钟
FAIL_MAX = 5                     # 5 次
LOCK_S = 900                     # 锁 15 分钟

# --- tailnet 免登录段 -----------------------------------------------
# 只认这一段。**127.0.0.1 不在里面** —— cloudflared 就在本机以 127.0.0.1 回连本服务，
# 放行本机等于给公网开后门。
TAILNET_CIDR = "<tailnet-cidr>"
_TAILNET_NET = ipaddress.ip_network(TAILNET_CIDR)

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")
UTC = datetime.timezone.utc


class AuthError(Exception):
    """CLI 能看懂的错误：main 打一行人话就退出，不给用户看 traceback。"""


CliError = AuthError          # 与 shared_content.py 同名，方便互相抄用法


# ================================================================ 小工具

def utcnow() -> datetime.datetime:
    """统一的"现在"：aware UTC，秒级（毫秒对审计没意义，还徒增两种格式）。"""
    return datetime.datetime.now(UTC).replace(microsecond=0)


def iso(value: Any) -> Optional[str]:
    """datetime -> "2026-09-19T10:00:00Z"；None 原样返回；字符串原样返回。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime.datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def parse_ts(value: Any) -> Optional[datetime.datetime]:
    """反向：ISO 文本 / datetime -> aware UTC datetime。解不出来给 None。"""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def beijing(value: Any) -> str:
    """给人看的时间一律北京时间（UTC+8），和看板页面同一个口径。"""
    dt = parse_ts(value)
    if dt is None:
        return "-"
    return (dt + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


_SECRET_RE = re.compile(r"(password|passfile|sslpassword)\s*=\s*\S+", re.IGNORECASE)


def safe_err(exc: Any) -> str:
    """异常 -> 可以安全打印的一行字：抹掉 DSN 口令再截断（口径同 blackboard_svc）。"""
    text = str(exc).replace("\n", " ").strip() or exc.__class__.__name__
    return _SECRET_RE.sub(r"\1=***", text)[:400]


def read_dsn(path: str) -> str:
    """从 systemd 环境文件里取 BB_PG_DSN（等价于 sed -n 's/^BB_PG_DSN=//p'）。

    **绝不能 source 这个文件**：值里带空格，shell 会截断成错值。
    """
    with open(path, "r", encoding="utf-8-sig") as fh:
        for line in fh:
            if line.startswith("BB_PG_DSN="):
                value = line[len("BB_PG_DSN="):].rstrip("\r\n").strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                return value
    return ""


def _width(text: str) -> int:
    """中文按两格算的显示宽度，否则中英混排的表格永远是歪的。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def render_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """中文对齐的纯文本表格（不依赖任何第三方库）。空表也给一句人话。"""
    if not rows:
        return "（没有匹配的记录）"
    cells = [[("" if c is None else str(c)) for c in row] for row in rows]
    widths = [_width(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], _width(cell))
    lines = ["  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)).rstrip(),
             "  ".join("-" * w for w in widths)]
    for row in cells:
        lines.append("  ".join(_pad(c, widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


# ================================================================ 角色 / 密级

def normalize_role(role: Any) -> str:
    text = str(role or "").strip().lower()
    return text if text in ROLE_RANK else "viewer"


def normalize_sensitivity(value: Any, default: str = DEFAULT_SENSITIVITY) -> str:
    text = str(value or "").strip().lower()
    return text if text in SENSITIVITY_RANK else default


def role_can_see(role: Any, sensitivity: Any) -> bool:
    """角色能不能看这个密级的内容。

    viewer 只看 public；member 看到 internal；owner 全都能看。
    **认不出来的角色按 viewer 算、认不出来的密级按 internal 算** —— 两边都往收紧了兜。
    """
    return ROLE_RANK[normalize_role(role)] >= SENSITIVITY_RANK[normalize_sensitivity(sensitivity)]


def in_tailnet(ip: Any) -> bool:
    """这个对端 IP 是不是 tailnet（<tailnet-cidr>）里的。

    只认对端 IP —— **不看 X-Forwarded-For / CF-Connecting-IP**，那两个头是外层反代
    自己写的，谁都能伪造，只配进审计记录，不配当鉴权依据。
    """
    text = str(ip or "").strip()
    if not text:
        return False
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped          # ::ffff:100.x.x.x 也算 tailnet
    return addr.version == 4 and addr in _TAILNET_NET


def is_loopback(ip: Any) -> bool:
    text = str(ip or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


# ================================================================ 口令哈希

def check_password_policy(password: str) -> None:
    """口令的最低要求。挡不住"弱口令"，但挡得住"空口令 / 一个字符"这种事故。"""
    if password is None:
        raise AuthError("口令为空")
    if len(password) < PASSWORD_MIN:
        raise AuthError("口令至少 %d 位（当前 %d 位）" % (PASSWORD_MIN, len(password)))
    if len(password) > PASSWORD_MAX:
        raise AuthError("口令最多 %d 位" % PASSWORD_MAX)
    if password.strip() == "":
        raise AuthError("口令不能全是空白字符")


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """scrypt 哈希，输出 `scrypt$n$r$p$<salt_b64>$<dk_b64>`。

    每个账号一条独立随机 salt（不复用、不派生自用户名）—— 两个人用同一个口令，
    库里两行也长得完全不一样，撞库表在这里没用。
    """
    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                        p=SCRYPT_P, dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)
    return "scrypt$%d$%d$%d$%s$%s" % (SCRYPT_N, SCRYPT_R, SCRYPT_P, _b64(salt), _b64(dk))


def verify_password(password: str, stored: str) -> bool:
    """校验。参数从哈希串里读回来（将来调 n/r/p 也能验旧口令）。

    比对走 hmac.compare_digest（定时安全）；哈希串坏掉一律 False，不抛异常 ——
    登录路径上抛异常就等于给攻击者一个"这个账号存在"的信号。
    """
    try:
        algo, n, r, p, salt_b64, dk_b64 = str(stored).split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt_b64),
                            n=int(n), r=int(r), p=int(p), dklen=len(_unb64(dk_b64)),
                            maxmem=SCRYPT_MAXMEM)
    except Exception:
        return False
    return hmac.compare_digest(_b64(dk), dk_b64)


def looks_hashed(stored: str) -> bool:
    """给自检用：库里这一列长得像哈希吗（不是明文吧）。"""
    return str(stored or "").startswith("scrypt$") and len(str(stored).split("$")) == 6


# ================================================================ 会话密钥与签名

def init_key(path: str, force: bool = False) -> str:
    """生成会话签名密钥（32 字节随机，base64 落文件，权限 600）。

    已经有了就不覆盖 —— 覆盖等于把所有人的 cookie 一次性作废，要干这事得 --force。
    """
    if os.path.exists(path) and not force:
        raise AuthError("密钥文件已存在：%s（要换新密钥请加 --force，注意会让所有人重新登录）" % path)
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    key = secrets.token_bytes(KEY_BYTES)
    # 先建成 600 再写内容：别让密钥有哪怕一瞬间是 644
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (_b64(key) + "\n").encode("ascii"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_session_key(path: Optional[str] = None, env: str = KEY_ENV) -> bytes:
    """读会话签名密钥：优先文件，其次环境变量。**代码里没有任何默认密钥**。

    文件权限比 600 松会打一行告警（不阻断：宁可服务起来 + 一条告警，
    也别因为一个 chmod 把看板挡在外面）。
    """
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        if not raw:
            raise AuthError("会话密钥文件是空的：%s（用 init-key 生成）" % path)
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode & 0o077:
                sys.stderr.write("[警告] %s 权限是 %o，建议 chmod 600\n" % (path, mode))
        except OSError:
            pass
        return _key_bytes(raw)
    raw = os.environ.get(env) or ""
    if not raw.strip():
        raise AuthError("没有会话密钥：给 --session-key-file，或设置环境变量 %s" % env)
    return _key_bytes(raw.strip())


def _key_bytes(raw: str) -> bytes:
    """密钥文件里可以是 base64，也可以是一串够长的随机字符 —— 两种都收。"""
    try:
        key = _unb64(raw)
        if len(key) >= 16:
            return key
    except Exception:
        pass
    key = raw.encode("utf-8")
    if len(key) < 16:
        raise AuthError("会话密钥太短（至少 16 字节），请用 init-key 重新生成")
    return key


def _sign(value: str, key: bytes, purpose: bytes) -> str:
    """HMAC-SHA256 签名。purpose 做域分隔：会话 token 的签名换不到 CSRF 用。"""
    mac = hmac.new(key, purpose + b"|" + value.encode("utf-8"), hashlib.sha256).digest()
    return _b64(mac)


def sign_token(token: str, key: bytes, purpose: bytes = b"session") -> str:
    """cookie 里放的东西：`v1.<token>.<签名>`。签名不对就当没这个 cookie。"""
    return "v1.%s.%s" % (token, _sign(token, key, purpose))


def unsign_token(value: Any, key: bytes, purpose: bytes = b"session") -> Optional[str]:
    """验签并取回原始 token。**任何一处不对就返回 None**，不解释是哪不对。"""
    text = str(value or "")
    parts = text.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return None
    token, sig = parts[1], parts[2]
    if not token or not sig:
        return None
    if not hmac.compare_digest(_sign(token, key, purpose), sig):
        return None
    return token


def token_fingerprint(token: str) -> str:
    """库里存的就是这个：sha256(原始 token) 的 hex。原始 token 只在 cookie 里。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def issue_csrf(key: bytes) -> str:
    """CSRF token：随机数 + 签名。登录页把它同时放进 cookie 和隐藏字段，
    提交时两边对得上才算数（double submit）。"""
    return sign_token(secrets.token_urlsafe(16), key, purpose=b"csrf")


def check_csrf(cookie_value: Any, form_value: Any, key: bytes) -> bool:
    left = unsign_token(cookie_value, key, purpose=b"csrf")
    right = unsign_token(form_value, key, purpose=b"csrf")
    if not left or not right:
        return False
    return hmac.compare_digest(left, right)


# ================================================================ 存储层
#
# 两个后端、同一套方法名：
#   PgStore     —— 真库（唯一会用到 psycopg2 的地方，延迟导入）
#   SqliteStore —— 替身（单测、本地演示用；表结构与 pg 一一对应，连 append-only
#                  触发器都照着建，这样"审计不许删"在替身上也是真的）

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS view_users (
  username       TEXT PRIMARY KEY,
  role           TEXT NOT NULL DEFAULT 'viewer',
  pwd_hash       TEXT NOT NULL,
  disabled       INTEGER NOT NULL DEFAULT 0,
  note           TEXT,
  created_at     TEXT NOT NULL,
  created_by     TEXT,
  updated_at     TEXT NOT NULL,
  pwd_changed_at TEXT NOT NULL,
  last_login_at  TEXT,
  CHECK (role IN ('viewer','member','owner')),
  CHECK (pwd_hash LIKE 'scrypt$%' AND length(pwd_hash) >= 40)
);

CREATE TABLE IF NOT EXISTS view_sessions (
  token_hash TEXT PRIMARY KEY,
  username   TEXT NOT NULL,
  role       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_seen  TEXT,
  ip         TEXT,
  ua         TEXT,
  revoked    INTEGER NOT NULL DEFAULT 0,
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS view_sessions_user ON view_sessions (username, created_at DESC);

CREATE TABLE IF NOT EXISTS view_audit (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL,
  username TEXT,
  ip       TEXT,
  ua       TEXT,
  event    TEXT NOT NULL,
  detail   TEXT
);
CREATE INDEX IF NOT EXISTS view_audit_ts ON view_audit (ts DESC);

CREATE TABLE IF NOT EXISTS view_login_failures (
  fail_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL,
  ip       TEXT NOT NULL,
  username TEXT,
  outcome  TEXT NOT NULL DEFAULT 'fail',
  CHECK (outcome IN ('fail','ok'))
);
CREATE INDEX IF NOT EXISTS view_login_failures_ip_ts ON view_login_failures (ip, ts DESC);

-- append-only：与 sql/view_auth_schema.sql 里的 pg 触发器同口径
CREATE TRIGGER IF NOT EXISTS view_audit_no_update BEFORE UPDATE ON view_audit
BEGIN SELECT RAISE(ABORT, 'view_audit 是 append-only：不允许 UPDATE'); END;
CREATE TRIGGER IF NOT EXISTS view_audit_no_delete BEFORE DELETE ON view_audit
BEGIN SELECT RAISE(ABORT, 'view_audit 是 append-only：不允许 DELETE'); END;
CREATE TRIGGER IF NOT EXISTS view_fail_no_update BEFORE UPDATE ON view_login_failures
BEGIN SELECT RAISE(ABORT, 'view_login_failures 是 append-only：不允许 UPDATE'); END;
CREATE TRIGGER IF NOT EXISTS view_fail_no_delete BEFORE DELETE ON view_login_failures
BEGIN SELECT RAISE(ABORT, 'view_login_failures 是 append-only：不允许 DELETE'); END;
CREATE TRIGGER IF NOT EXISTS view_users_no_delete BEFORE DELETE ON view_users
BEGIN SELECT RAISE(ABORT, 'view_users 不允许 DELETE（账号只停用不删除）'); END;
CREATE TRIGGER IF NOT EXISTS view_sessions_no_delete BEFORE DELETE ON view_sessions
BEGIN SELECT RAISE(ABORT, 'view_sessions 不允许 DELETE（下线走 revoked=true）'); END;
"""

USER_COLUMNS = ("username", "role", "pwd_hash", "disabled", "note", "created_at",
                "created_by", "updated_at", "pwd_changed_at", "last_login_at")
SESSION_COLUMNS = ("token_hash", "username", "role", "created_at", "expires_at",
                   "last_seen", "ip", "ua", "revoked", "revoked_at")
AUDIT_COLUMNS = ("audit_id", "ts", "username", "ip", "ua", "event", "detail")


def _row_to_user(row: Sequence[Any]) -> Dict[str, Any]:
    return {
        "username": row[0], "role": normalize_role(row[1]), "pwd_hash": row[2],
        "disabled": bool(row[3]), "note": row[4], "created_at": iso(row[5]),
        "created_by": row[6], "updated_at": iso(row[7]),
        "pwd_changed_at": iso(row[8]), "last_login_at": iso(row[9]),
    }


def _row_to_session(row: Sequence[Any]) -> Dict[str, Any]:
    return {
        "token_hash": row[0], "username": row[1], "role": normalize_role(row[2]),
        "created_at": iso(row[3]), "expires_at": iso(row[4]), "last_seen": iso(row[5]),
        "ip": row[6], "ua": row[7], "revoked": bool(row[8]), "revoked_at": iso(row[9]),
    }


def _row_to_audit(row: Sequence[Any]) -> Dict[str, Any]:
    return {"audit_id": row[0], "ts": iso(row[1]), "username": row[2], "ip": row[3],
            "ua": row[4], "event": row[5], "detail": row[6]}


class SqliteStore(object):
    """测试与本地演示用的替身。表结构、约束、append-only 触发器都照着 pg 抄。"""

    kind = "sqlite"

    def __init__(self, path: str) -> None:
        # check_same_thread=False：看板是多线程 HTTP 服务，外面有 Auth 的锁兜着
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._conn.executescript(SQLITE_SCHEMA)
        self._conn.commit()

    # --- 账号 ---
    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT %s FROM view_users WHERE username = ?" % ", ".join(USER_COLUMNS),
            (username,))
        row = cur.fetchone()
        return _row_to_user(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT %s FROM view_users ORDER BY username" % ", ".join(USER_COLUMNS))
        return [_row_to_user(r) for r in cur.fetchall()]

    def add_user(self, username: str, role: str, pwd_hash: str,
                 created_by: Optional[str], note: Optional[str]) -> None:
        now = iso(utcnow())
        self._conn.execute(
            "INSERT INTO view_users (username, role, pwd_hash, disabled, note, "
            "created_at, created_by, updated_at, pwd_changed_at) "
            "VALUES (?,?,?,0,?,?,?,?,?)",
            (username, role, pwd_hash, note, now, created_by, now, now))
        self._conn.commit()

    def set_password(self, username: str, pwd_hash: str) -> None:
        now = iso(utcnow())
        self._conn.execute(
            "UPDATE view_users SET pwd_hash = ?, pwd_changed_at = ?, updated_at = ? "
            "WHERE username = ?", (pwd_hash, now, now, username))
        self._conn.commit()

    def set_disabled(self, username: str, disabled: bool) -> None:
        self._conn.execute(
            "UPDATE view_users SET disabled = ?, updated_at = ? WHERE username = ?",
            (1 if disabled else 0, iso(utcnow()), username))
        self._conn.commit()

    def touch_login(self, username: str, ts: datetime.datetime) -> None:
        self._conn.execute(
            "UPDATE view_users SET last_login_at = ?, updated_at = ? WHERE username = ?",
            (iso(ts), iso(ts), username))
        self._conn.commit()

    # --- 会话 ---
    def create_session(self, token_hash: str, username: str, role: str,
                       created_at: datetime.datetime, expires_at: datetime.datetime,
                       ip: str, ua: str) -> None:
        self._conn.execute(
            "INSERT INTO view_sessions (token_hash, username, role, created_at, "
            "expires_at, last_seen, ip, ua, revoked) VALUES (?,?,?,?,?,?,?,?,0)",
            (token_hash, username, role, iso(created_at), iso(expires_at),
             iso(created_at), ip, ua))
        self._conn.commit()

    def get_session(self, token_hash: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT %s FROM view_sessions WHERE token_hash = ?" % ", ".join(SESSION_COLUMNS),
            (token_hash,))
        row = cur.fetchone()
        return _row_to_session(row) if row else None

    def touch_session(self, token_hash: str, last_seen: datetime.datetime,
                      expires_at: Optional[datetime.datetime] = None) -> None:
        if expires_at is None:
            self._conn.execute("UPDATE view_sessions SET last_seen = ? WHERE token_hash = ?",
                               (iso(last_seen), token_hash))
        else:
            self._conn.execute(
                "UPDATE view_sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?",
                (iso(last_seen), iso(expires_at), token_hash))
        self._conn.commit()

    def revoke_session(self, token_hash: str) -> int:
        cur = self._conn.execute(
            "UPDATE view_sessions SET revoked = 1, revoked_at = ? "
            "WHERE token_hash = ? AND revoked = 0", (iso(utcnow()), token_hash))
        self._conn.commit()
        return cur.rowcount or 0

    def revoke_user_sessions(self, username: str) -> int:
        cur = self._conn.execute(
            "UPDATE view_sessions SET revoked = 1, revoked_at = ? "
            "WHERE username = ? AND revoked = 0", (iso(utcnow()), username))
        self._conn.commit()
        return cur.rowcount or 0

    def list_sessions(self, limit: int = 50, username: Optional[str] = None
                      ) -> List[Dict[str, Any]]:
        sql = "SELECT %s FROM view_sessions" % ", ".join(SESSION_COLUMNS)
        args = []  # type: List[Any]
        if username:
            sql += " WHERE username = ?"
            args.append(username)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        return [_row_to_session(r) for r in self._conn.execute(sql, args).fetchall()]

    # --- 审计 ---
    def audit(self, event: str, username: Optional[str], ip: Optional[str],
              ua: Optional[str], detail: Optional[str],
              ts: Optional[datetime.datetime] = None) -> None:
        self._conn.execute(
            "INSERT INTO view_audit (ts, username, ip, ua, event, detail) VALUES (?,?,?,?,?,?)",
            (iso(ts or utcnow()), username, ip, ua, event, detail))
        self._conn.commit()

    def list_audit(self, limit: int = 50, event: Optional[str] = None,
                   username: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT %s FROM view_audit" % ", ".join(AUDIT_COLUMNS)
        where, args = [], []  # type: List[str], List[Any]
        if event:
            where.append("event = ?")
            args.append(event)
        if username:
            where.append("username = ?")
            args.append(username)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY audit_id DESC LIMIT ?"
        args.append(int(limit))
        return [_row_to_audit(r) for r in self._conn.execute(sql, args).fetchall()]

    # --- 登录失败流水 ---
    def record_login_outcome(self, ip: str, username: Optional[str], outcome: str,
                             ts: Optional[datetime.datetime] = None) -> None:
        self._conn.execute(
            "INSERT INTO view_login_failures (ts, ip, username, outcome) VALUES (?,?,?,?)",
            (iso(ts or utcnow()), ip, username, outcome))
        self._conn.commit()

    def recent_login_rows(self, ip: str, since: datetime.datetime
                          ) -> List[Tuple[datetime.datetime, str]]:
        cur = self._conn.execute(
            "SELECT ts, outcome FROM view_login_failures WHERE ip = ? AND ts >= ? "
            "ORDER BY ts", (ip, iso(since)))
        return [(parse_ts(r[0]), str(r[1])) for r in cur.fetchall() if parse_ts(r[0])]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


class PgStore(object):
    """真库访问层。只在真连库时才导入 psycopg2。

    写的只有 view_users / view_sessions / view_audit / view_login_failures 四张表，
    **一条 DELETE 都没有** —— 停用走 disabled、下线走 revoked、审计只增不改。
    """

    kind = "pg"

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise AuthError("DSN 为空：检查 --dsn-file 指向的文件里有没有 BB_PG_DSN=")
        import psycopg2  # noqa: F401  —— 唯一的第三方依赖，延迟导入
        self._conn = psycopg2.connect(dsn, connect_timeout=5)
        self._conn.autocommit = True

    def _exec(self, sql: str, args: Sequence[Any] = (), fetch: str = "") -> Any:
        cur = self._conn.cursor()
        try:
            cur.execute(sql, tuple(args))
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return cur.rowcount
        finally:
            cur.close()

    # --- 账号 ---
    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        row = self._exec("SELECT %s FROM view_users WHERE username = %%s" %
                         ", ".join(USER_COLUMNS), (username,), fetch="one")
        return _row_to_user(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        rows = self._exec("SELECT %s FROM view_users ORDER BY username" %
                          ", ".join(USER_COLUMNS), fetch="all")
        return [_row_to_user(r) for r in rows or []]

    def add_user(self, username: str, role: str, pwd_hash: str,
                 created_by: Optional[str], note: Optional[str]) -> None:
        self._exec("INSERT INTO view_users (username, role, pwd_hash, note, created_by) "
                   "VALUES (%s, %s, %s, %s, %s)",
                   (username, role, pwd_hash, note, created_by))

    def set_password(self, username: str, pwd_hash: str) -> None:
        self._exec("UPDATE view_users SET pwd_hash = %s, pwd_changed_at = now() "
                   "WHERE username = %s", (pwd_hash, username))

    def set_disabled(self, username: str, disabled: bool) -> None:
        self._exec("UPDATE view_users SET disabled = %s WHERE username = %s",
                   (bool(disabled), username))

    def touch_login(self, username: str, ts: datetime.datetime) -> None:
        self._exec("UPDATE view_users SET last_login_at = %s WHERE username = %s",
                   (ts, username))

    # --- 会话 ---
    def create_session(self, token_hash: str, username: str, role: str,
                       created_at: datetime.datetime, expires_at: datetime.datetime,
                       ip: str, ua: str) -> None:
        self._exec("INSERT INTO view_sessions (token_hash, username, role, created_at, "
                   "expires_at, last_seen, ip, ua) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                   (token_hash, username, role, created_at, expires_at, created_at, ip, ua))

    def get_session(self, token_hash: str) -> Optional[Dict[str, Any]]:
        row = self._exec("SELECT %s FROM view_sessions WHERE token_hash = %%s" %
                         ", ".join(SESSION_COLUMNS), (token_hash,), fetch="one")
        return _row_to_session(row) if row else None

    def touch_session(self, token_hash: str, last_seen: datetime.datetime,
                      expires_at: Optional[datetime.datetime] = None) -> None:
        if expires_at is None:
            self._exec("UPDATE view_sessions SET last_seen = %s WHERE token_hash = %s",
                       (last_seen, token_hash))
        else:
            self._exec("UPDATE view_sessions SET last_seen = %s, expires_at = %s "
                       "WHERE token_hash = %s", (last_seen, expires_at, token_hash))

    def revoke_session(self, token_hash: str) -> int:
        return int(self._exec("UPDATE view_sessions SET revoked = true, revoked_at = now() "
                              "WHERE token_hash = %s AND revoked = false", (token_hash,)) or 0)

    def revoke_user_sessions(self, username: str) -> int:
        return int(self._exec("UPDATE view_sessions SET revoked = true, revoked_at = now() "
                              "WHERE username = %s AND revoked = false", (username,)) or 0)

    def list_sessions(self, limit: int = 50, username: Optional[str] = None
                      ) -> List[Dict[str, Any]]:
        sql = "SELECT %s FROM view_sessions" % ", ".join(SESSION_COLUMNS)
        args = []  # type: List[Any]
        if username:
            sql += " WHERE username = %s"
            args.append(username)
        sql += " ORDER BY created_at DESC LIMIT %s"
        args.append(int(limit))
        return [_row_to_session(r) for r in self._exec(sql, args, fetch="all") or []]

    # --- 审计 ---
    def audit(self, event: str, username: Optional[str], ip: Optional[str],
              ua: Optional[str], detail: Optional[str],
              ts: Optional[datetime.datetime] = None) -> None:
        self._exec("INSERT INTO view_audit (ts, username, ip, ua, event, detail) "
                   "VALUES (%s,%s,%s,%s,%s,%s)",
                   (ts or utcnow(), username, ip, ua, event, detail))

    def list_audit(self, limit: int = 50, event: Optional[str] = None,
                   username: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT %s FROM view_audit" % ", ".join(AUDIT_COLUMNS)
        where, args = [], []  # type: List[str], List[Any]
        if event:
            where.append("event = %s")
            args.append(event)
        if username:
            where.append("username = %s")
            args.append(username)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY audit_id DESC LIMIT %s"
        args.append(int(limit))
        return [_row_to_audit(r) for r in self._exec(sql, args, fetch="all") or []]

    # --- 登录失败流水 ---
    def record_login_outcome(self, ip: str, username: Optional[str], outcome: str,
                             ts: Optional[datetime.datetime] = None) -> None:
        self._exec("INSERT INTO view_login_failures (ts, ip, username, outcome) "
                   "VALUES (%s,%s,%s,%s)", (ts or utcnow(), ip, username, outcome))

    def recent_login_rows(self, ip: str, since: datetime.datetime
                          ) -> List[Tuple[datetime.datetime, str]]:
        rows = self._exec("SELECT ts, outcome FROM view_login_failures "
                          "WHERE ip = %s AND ts >= %s ORDER BY ts", (ip, since), fetch="all")
        out = []
        for row in rows or []:
            ts = parse_ts(row[0])
            if ts:
                out.append((ts, str(row[1])))
        return out

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def open_store(args: argparse.Namespace) -> Any:
    """按参数挑后端：--sqlite 走替身，否则从 --dsn-file 读 DSN 连 PG。"""
    if getattr(args, "sqlite", None):
        return SqliteStore(args.sqlite)
    path = getattr(args, "dsn_file", None) or DEFAULT_DSN_FILE
    try:
        dsn = read_dsn(path)
    except OSError as exc:
        raise AuthError("读不到 DSN 文件 %s：%s" % (path, safe_err(exc)))
    return PgStore(dsn)


# ================================================================ 暴力破解防护

class LockTracker(object):
    """同一 IP 5 分钟内失败 5 次 -> 锁 15 分钟。**内存 + 表双记**。

    内存那份快、不依赖库；表那份让"重启进程"不等于"解锁"。
    两边算出来的解锁时间取**较晚**的那个 —— 往严了兜。
    """

    def __init__(self, store: Any) -> None:
        self._store = store
        self._mem = {}      # type: Dict[str, List[Tuple[datetime.datetime, str]]]
        self._lock = threading.Lock()

    @staticmethod
    def _locked_until(rows: Sequence[Tuple[datetime.datetime, str]],
                      now: datetime.datetime) -> Optional[datetime.datetime]:
        """从"这个 IP 最近的登录流水"里算出解锁时间。

        口径：只看最近一次成功之后的失败；任意连续 5 次失败落在 5 分钟窗口里，
        就从第 5 次那一刻起锁 15 分钟。取所有候选里最晚的。
        """
        fails = []  # type: List[datetime.datetime]
        for ts, outcome in sorted(rows, key=lambda r: r[0]):
            if outcome == "ok":
                fails = []          # 成功一次，前面的失败一笔勾销
            else:
                fails.append(ts)
        until = None
        for i in range(0, max(0, len(fails) - FAIL_MAX + 1)):
            first, fifth = fails[i], fails[i + FAIL_MAX - 1]
            if (fifth - first).total_seconds() <= FAIL_WINDOW_S:
                candidate = fifth + datetime.timedelta(seconds=LOCK_S)
                if until is None or candidate > until:
                    until = candidate
        return until if (until and until > now) else None

    def _mem_rows(self, ip: str, since: datetime.datetime
                  ) -> List[Tuple[datetime.datetime, str]]:
        rows = [r for r in self._mem.get(ip, []) if r[0] >= since]
        self._mem[ip] = rows          # 顺手丢掉过老的，内存不会无限涨
        return rows

    def locked_until(self, ip: str, now: Optional[datetime.datetime] = None
                     ) -> Optional[datetime.datetime]:
        now = now or utcnow()
        since = now - datetime.timedelta(seconds=FAIL_WINDOW_S + LOCK_S)
        with self._lock:
            mem = self._locked_until(self._mem_rows(ip, since), now)
        try:
            table = self._locked_until(self._store.recent_login_rows(ip, since), now)
        except Exception:
            table = None              # 库抖了不能把人都锁在门外，内存那份还在
        if mem and table:
            return max(mem, table)
        return mem or table

    def record(self, ip: str, username: Optional[str], outcome: str,
               now: Optional[datetime.datetime] = None) -> None:
        now = now or utcnow()
        with self._lock:
            self._mem.setdefault(ip, []).append((now, outcome))
        try:
            self._store.record_login_outcome(ip, username, outcome, now)
        except Exception:
            pass                      # 落表失败不影响内存计数，也不影响登录判定


# ================================================================ 认证门面

class Principal(object):
    """一次请求背后的"人"：谁、什么角色、凭什么进来的。"""

    __slots__ = ("username", "role", "source", "token", "token_hash", "expires_at")

    def __init__(self, username: str, role: str, source: str,
                 token: str = "", token_hash: str = "",
                 expires_at: Optional[datetime.datetime] = None) -> None:
        self.username = username
        self.role = normalize_role(role)
        self.source = source              # session | tailnet
        self.token = token
        self.token_hash = token_hash
        self.expires_at = expires_at

    def can_see(self, sensitivity: Any) -> bool:
        return role_can_see(self.role, sensitivity)

    def as_dict(self) -> Dict[str, Any]:
        return {"user": self.username, "role": self.role,
                "role_cn": ROLE_CN.get(self.role, self.role), "source": self.source,
                "expires_at": iso(self.expires_at)}


class Auth(object):
    """看板侧用的认证门面：握着 store + 会话密钥 + 锁定计数 + 审计。

    线程安全：看板是 ThreadingHTTPServer，store 的连接（sqlite / psycopg2）都不适合
    并发使用，统一用一把锁串起来。登录这种低频操作，一把锁完全够。
    """

    def __init__(self, store: Any, session_key: bytes,
                 ttl_seconds: int = SESSION_TTL_S, logger: Any = None) -> None:
        self.store = store
        self.key = session_key
        self.ttl = int(ttl_seconds or SESSION_TTL_S)
        self.log = logger
        self.locks = LockTracker(store)
        self._mutex = threading.Lock()

    # --- 审计 ---
    def audit(self, event: str, username: Optional[str], ip: Optional[str],
              ua: Optional[str], detail: str = "") -> None:
        """落审计表 + 写日志文件。两边都写 —— 库挂了日志还在，日志轮转了库里还有。"""
        detail = (detail or "")[:500]
        try:
            with self._mutex:
                self.store.audit(event, username, ip, (ua or "")[:300], detail)
        except Exception as exc:
            if self.log:
                self.log.error("审计落库失败 event=%s: %s", event, safe_err(exc))
        if self.log:
            self.log.info("audit event=%s user=%s ip=%s detail=%s",
                          event, username or "-", ip or "-", detail)

    # --- 登录 ---
    def login(self, username: str, password: str, ip: str, ua: str
              ) -> Tuple[bool, Optional[str], str]:
        """返回 (成功与否, cookie 值, 给人看的原因)。

        失败提示**一律是同一句** —— "账号不存在"和"口令错"说成两句话，就等于
        免费送人一个用户名枚举器。
        """
        generic = "账号或口令不正确"
        now = utcnow()
        locked = self.locks.locked_until(ip, now)
        if locked:
            wait = int((locked - now).total_seconds() // 60) + 1
            self.audit("login_lock", username or None, ip, ua,
                       "锁定中，拒绝登录；解锁还需约 %d 分钟" % wait)
            return False, None, "尝试过于频繁，请约 %d 分钟后再试" % wait

        user = None
        try:
            with self._mutex:
                user = self.store.get_user(username)
        except Exception as exc:
            if self.log:
                self.log.error("读账号失败：%s", safe_err(exc))
            self.audit("login_fail", username or None, ip, ua, "后端不可用")
            return False, None, "认证后端暂时不可用，请稍后再试"

        ok = bool(user) and not user["disabled"] and verify_password(password, user["pwd_hash"])
        if not ok:
            self.locks.record(ip, username or None, "fail", now)
            reason = "账号不存在" if not user else ("账号已停用" if user["disabled"] else "口令不匹配")
            # 审计里记真实原因（给运维查），回给浏览器的永远是同一句
            self.audit("login_fail", username or None, ip, ua, reason)
            still = self.locks.locked_until(ip, now)
            if still:
                self.audit("login_lock", username or None, ip, ua,
                           "%d 分钟内失败满 %d 次，锁定至 %s（北京时间）"
                           % (FAIL_WINDOW_S // 60, FAIL_MAX, beijing(still)))
            return False, None, generic

        token = new_token()
        fingerprint = token_fingerprint(token)
        expires = now + datetime.timedelta(seconds=self.ttl)
        try:
            with self._mutex:
                self.store.create_session(fingerprint, user["username"], user["role"],
                                          now, expires, ip, (ua or "")[:300])
                self.store.touch_login(user["username"], now)
        except Exception as exc:
            if self.log:
                self.log.error("建会话失败：%s", safe_err(exc))
            return False, None, "认证后端暂时不可用，请稍后再试"

        self.locks.record(ip, user["username"], "ok", now)
        self.audit("login_ok", user["username"], ip, ua,
                   "角色 %s，会话有效至 %s（北京时间）" % (user["role"], beijing(expires)))
        return True, sign_token(token, self.key), ""

    # --- 校验 cookie ---
    def check_cookie(self, cookie_value: Any, ip: str, ua: str
                     ) -> Tuple[Optional[Principal], str]:
        """cookie -> Principal。返回 (principal, 失败原因)。

        依次卡：签名对不对 -> 库里有没有 -> 撤销没有 -> 过期没有 -> 账号还在不在。
        任何一关不过都只回一句"请重新登录"，不告诉浏览器卡在哪一关。
        """
        if not cookie_value:
            return None, "没有会话 cookie"
        token = unsign_token(cookie_value, self.key)
        if not token:
            return None, "cookie 签名不匹配（被篡改或换过密钥）"
        fingerprint = token_fingerprint(token)
        try:
            with self._mutex:
                row = self.store.get_session(fingerprint)
        except Exception as exc:
            if self.log:
                self.log.error("读会话失败：%s", safe_err(exc))
            return None, "认证后端不可用"
        if not row:
            return None, "会话不存在"
        if row["revoked"]:
            return None, "会话已撤销"
        now = utcnow()
        expires = parse_ts(row["expires_at"])
        if expires is None or expires <= now:
            return None, "会话已过期"

        try:
            with self._mutex:
                user = self.store.get_user(row["username"])
        except Exception:
            user = None
        if not user or user["disabled"]:
            return None, "账号已停用"

        # 续期：剩余不足一半就顺延到满 TTL（活跃的人不该在看着看着被踢出去）
        renewed = None
        if (expires - now).total_seconds() < SESSION_RENEW_S:
            renewed = now + datetime.timedelta(seconds=self.ttl)
        try:
            with self._mutex:
                self.store.touch_session(fingerprint, now, renewed)
        except Exception:
            pass
        # 角色以**账号表当前值**为准：改了角色立刻生效，不用等 cookie 过期
        return Principal(user["username"], user["role"], "session", token, fingerprint,
                         renewed or expires), ""

    def logout(self, cookie_value: Any, ip: str, ua: str) -> bool:
        token = unsign_token(cookie_value, self.key)
        if not token:
            return False
        fingerprint = token_fingerprint(token)
        username = None
        try:
            with self._mutex:
                row = self.store.get_session(fingerprint)
                username = row["username"] if row else None
                changed = self.store.revoke_session(fingerprint)
        except Exception as exc:
            if self.log:
                self.log.error("撤销会话失败：%s", safe_err(exc))
            return False
        self.audit("logout", username, ip, ua, "用户主动退出登录")
        return bool(changed)

    def change_password(self, username: str, old: str, new: str, ip: str, ua: str
                        ) -> Tuple[bool, str]:
        """改自己的口令：**必须先验旧口令**；改完把这个人的会话全部撤掉。

        撤会话是有意的 —— 改密的常见原因就是"怀疑被人拿到了"，那就得把所有已经
        发出去的 cookie 一起作废，包括改密的人自己手上这一张。
        """
        try:
            check_password_policy(new)
        except AuthError as exc:
            return False, str(exc)
        if old == new:
            return False, "新口令不能和旧口令一样"
        try:
            with self._mutex:
                user = self.store.get_user(username)
        except Exception:
            return False, "认证后端暂时不可用，请稍后再试"
        if not user or user["disabled"] or not verify_password(old, user["pwd_hash"]):
            self.audit("passwd", username, ip, ua, "改密失败：旧口令不匹配")
            return False, "旧口令不正确"
        try:
            with self._mutex:
                self.store.set_password(username, hash_password(new))
                killed = self.store.revoke_user_sessions(username)
        except Exception as exc:
            if self.log:
                self.log.error("改密失败：%s", safe_err(exc))
            return False, "认证后端暂时不可用，请稍后再试"
        self.audit("passwd", username, ip, ua, "改密成功，同时撤销了 %d 个会话" % killed)
        return True, "口令已修改，所有旧会话已失效，请用新口令重新登录"


def build_auth(store: Any, key_file: Optional[str], ttl_seconds: int = SESSION_TTL_S,
               logger: Any = None) -> Auth:
    """给看板用的组装函数：读密钥 + 包一层 Auth。密钥读不到就抛，**不给默认值**。"""
    return Auth(store, load_session_key(key_file), ttl_seconds, logger)


# ================================================================ CLI

def read_password(args: argparse.Namespace, confirm: bool, prompt: str = "口令") -> str:
    """取口令：--password-stdin 走标准输入，否则交互输入（不回显）。

    **没有"把口令当命令行参数"这条路** —— 命令行会进 shell 历史、会进 ps 输出、
    会进 systemd 日志，那等于把口令贴墙上。
    """
    if getattr(args, "password_stdin", False):
        data = sys.stdin.buffer.read()
        password = data.decode("utf-8", "replace")
        # 只去掉结尾的换行（printf '%s' 不带换行，echo 带；口令里的空格一律保留）
        while password.endswith("\n") or password.endswith("\r"):
            password = password[:-1]
    else:
        if not sys.stdin.isatty():
            raise AuthError("非交互环境请加 --password-stdin，并把口令从标准输入喂进来")
        password = getpass.getpass("%s：" % prompt)
        if confirm and password != getpass.getpass("再输一次确认："):
            raise AuthError("两次输入的口令不一致")
    check_password_policy(password)
    return password


def _need_user(args: argparse.Namespace) -> str:
    name = str(getattr(args, "user", "") or "").strip()
    if not USERNAME_RE.match(name):
        raise AuthError("用户名不合法：只收字母数字与 . _ -，2~32 位（拿到的是 %r）" % name)
    return name


def cmd_init_key(args: argparse.Namespace) -> int:
    path = init_key(args.key_file, force=args.force)
    print("会话签名密钥已生成：%s（权限 600）" % path)
    print("看板启动时用 --session-key-file %s 指向它；换密钥 = 所有人重新登录。" % path)
    return 0


def cmd_adduser(args: argparse.Namespace) -> int:
    username = _need_user(args)
    role = normalize_role(args.role)
    if args.role and role != args.role:
        raise AuthError("角色只能是 viewer / member / owner，拿到的是 %r" % args.role)
    store = open_store(args)
    try:
        if store.get_user(username):
            raise AuthError("账号已存在：%s（改口令用 passwd，换角色请新建并停用旧的）" % username)
        password = read_password(args, confirm=True)
        store.add_user(username, role, hash_password(password), args.by, args.note)
        del password                      # 明文在进程里的生命到此为止
        store.audit("admin", username, "cli", "view_auth.py",
                    "新建账号，角色 %s，操作人 %s" % (role, args.by or "-"))
    finally:
        store.close()
    print("已建账号：%s（角色 %s / %s）。库里只有 scrypt 哈希，没有明文。"
          % (username, role, ROLE_CN[role]))
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    username = _need_user(args)
    store = open_store(args)
    try:
        if not store.get_user(username):
            raise AuthError("没有这个账号：%s" % username)
        password = read_password(args, confirm=True, prompt="新口令")
        store.set_password(username, hash_password(password))
        del password
        killed = store.revoke_user_sessions(username)
        store.audit("admin", username, "cli", "view_auth.py",
                    "管理员改密，顺带撤销 %d 个会话" % killed)
    finally:
        store.close()
    print("已改口令：%s；同时撤销了 %d 个在用会话（旧 cookie 立刻失效）。" % (username, killed))
    return 0


def _set_disabled(args: argparse.Namespace, disabled: bool) -> int:
    username = _need_user(args)
    store = open_store(args)
    try:
        if not store.get_user(username):
            raise AuthError("没有这个账号：%s" % username)
        store.set_disabled(username, disabled)
        killed = store.revoke_user_sessions(username) if disabled else 0
        store.audit("admin", username, "cli", "view_auth.py",
                    ("停用账号，撤销 %d 个会话" % killed) if disabled else "恢复账号")
    finally:
        store.close()
    if disabled:
        print("已停用：%s；撤销了 %d 个在用会话。行还在，审计查得到。" % (username, killed))
    else:
        print("已恢复：%s。" % username)
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    return _set_disabled(args, True)


def cmd_enable(args: argparse.Namespace) -> int:
    return _set_disabled(args, False)


def cmd_list(args: argparse.Namespace) -> int:
    store = open_store(args)
    try:
        users = store.list_users()
    finally:
        store.close()
    rows = []
    for u in users:
        rows.append([u["username"], "%s/%s" % (u["role"], ROLE_CN[u["role"]]),
                     "停用" if u["disabled"] else "正常",
                     # 只给哈希的"长相"，不给全量：够证明"只存哈希"，又不多泄露一个字节
                     (u["pwd_hash"][:14] + "…") if looks_hashed(u["pwd_hash"]) else "⚠ 不是哈希",
                     beijing(u["created_at"]), beijing(u["pwd_changed_at"]),
                     beijing(u["last_login_at"]) if u["last_login_at"] else "-"])
    print(render_table(["账号", "角色", "状态", "口令哈希(前缀)", "创建", "改密", "最后登录"], rows))
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    store = open_store(args)
    try:
        if args.revoke_all:
            if not args.user:
                raise AuthError("--revoke-all 必须配 --user（不提供「全体踢下线」这种大杀器）")
            username = _need_user(args)
            killed = store.revoke_user_sessions(username)
            store.audit("admin", username, "cli", "view_auth.py",
                        "管理员撤销该账号全部会话，共 %d 个" % killed)
            print("已撤销 %s 的 %d 个会话。" % (username, killed))
            return 0
        rows = []
        now = utcnow()
        for s in store.list_sessions(limit=args.limit, username=args.user or None):
            expires = parse_ts(s["expires_at"])
            if s["revoked"]:
                state = "已撤销"
            elif expires and expires <= now:
                state = "已过期"
            else:
                state = "有效"
            rows.append([s["token_hash"][:12] + "…", s["username"], s["role"], state,
                         beijing(s["created_at"]), beijing(s["expires_at"]),
                         s["ip"] or "-", (s["ua"] or "-")[:28]])
        print(render_table(["会话指纹", "账号", "角色", "状态", "建立", "到期", "IP", "UA"], rows))
        print("\n注：库里存的是 sha256(token)，不是 token 本身 —— 这张表拿走也登不进来。")
    finally:
        store.close()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    store = open_store(args)
    try:
        rows = [[beijing(a["ts"]), a["event"], a["username"] or "-", a["ip"] or "-",
                 (a["detail"] or "")[:60]]
                for a in store.list_audit(limit=args.limit, event=args.event or None,
                                          username=args.user or None)]
    finally:
        store.close()
    print(render_table(["时间(北京)", "事件", "账号", "IP", "详情"], rows))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="看板认证：账号 / 会话 / 审计（纯标准库）",
        epilog="口令只走 stdin 或交互输入 —— 本工具没有 --password 参数，别找了。")
    parser.add_argument("--version", action="version", version="view_auth " + VERSION)
    subs = parser.add_subparsers(dest="cmd")

    def add_db(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--dsn-file", default=DEFAULT_DSN_FILE,
                         help="systemd 环境文件，从中取 BB_PG_DSN=（默认 %s）" % DEFAULT_DSN_FILE)
        sub.add_argument("--sqlite", default=None,
                         help="改用 sqlite 文件当后端（本地演示 / 单测替身）")

    p = subs.add_parser("adduser", help="新建账号（口令走 stdin 或交互输入）")
    p.add_argument("--user", required=True)
    p.add_argument("--role", default="viewer", help="viewer | member | owner")
    p.add_argument("--password-stdin", action="store_true", help="从标准输入读口令")
    p.add_argument("--by", default=None, help="操作人（记进审计）")
    p.add_argument("--note", default=None, help="备注（比如「给谁用的」）")
    add_db(p)
    p.set_defaults(func=cmd_adduser)

    p = subs.add_parser("passwd", help="管理员改某人的口令（会撤销其全部会话）")
    p.add_argument("--user", required=True)
    p.add_argument("--password-stdin", action="store_true")
    add_db(p)
    p.set_defaults(func=cmd_passwd)

    p = subs.add_parser("disable", help="停用账号（不删行，撤销其全部会话）")
    p.add_argument("--user", required=True)
    add_db(p)
    p.set_defaults(func=cmd_disable)

    p = subs.add_parser("enable", help="恢复账号")
    p.add_argument("--user", required=True)
    add_db(p)
    p.set_defaults(func=cmd_enable)

    p = subs.add_parser("list", help="列账号（只显示哈希前缀，永远不显示明文）")
    add_db(p)
    p.set_defaults(func=cmd_list)

    p = subs.add_parser("sessions", help="看会话 / 一键踢下线")
    p.add_argument("--user", default=None)
    p.add_argument("--revoke-all", action="store_true", help="撤销该账号的全部会话")
    p.add_argument("--limit", type=int, default=30)
    add_db(p)
    p.set_defaults(func=cmd_sessions)

    p = subs.add_parser("audit", help="看审计（append-only，删不掉）")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--event", default=None, help="只看某类事件：login_ok|login_fail|…")
    p.add_argument("--user", default=None)
    add_db(p)
    p.set_defaults(func=cmd_audit)

    p = subs.add_parser("init-key", help="生成会话签名密钥（权限 600）")
    p.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    p.add_argument("--force", action="store_true", help="覆盖已有密钥（所有人重新登录）")
    p.set_defaults(func=cmd_init_key)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except AuthError as exc:
        sys.stderr.write("错误：%s\n" % exc)
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\n已取消\n")
        return 130
    except Exception as exc:               # 别把 traceback 糊到运维脸上
        sys.stderr.write("失败：%s\n" % safe_err(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
