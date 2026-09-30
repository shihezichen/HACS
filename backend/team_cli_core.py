#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""team_cli_core.py —— 成员侧 CLI 的**通信层管道**（L1）

刀15（阶段3）从 team.py 摘出：team.py 改归组合根 C 之后，「C 不含业务与传输实现」
这一条要求它手里不能再攥着 NATS 调用与身份/去重这类通信层实现。于是把下面这些
**逐字节原样**搬到本件，team.py 与 L3 域件 team_group.py 共同 import 它：

  · 连接与扇出：discover_online（扫心跳取在线名单）/ call_one（单成员一条独立连接
    发一次 request）/ run_pool（限并发跑一批）/ collect_heartbeats（扫心跳收原始包）
  · 身份与口令：infer_member / resolve_password_file / resolve_member_and_password
  · 输入与限幅：read_text / clamp_timeout
  · 节流防轰炸：check_and_mark_dedup / rollback_dedup（含锁与原子落盘）
  · 上述实现依赖的常量（超时上限 / 默认中枢地址 / 心跳前缀 / 请求 subject 模板 /
    去重窗口与去重文件默认路径）

本件不含任何「群聊 / 会议」语义 —— 那些在 L3 team_group.py。
"""

import glob
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import node_paths as _np  # L0 平台层：宿主默认值唯一出处（env 优先）
import node_profile as _npf  # L0 节点档案：宿主事实唯一真源（NATS 集群串；env 优先）

# 批① 起被搬件按绝对包名 import，故把仓根一并放进搜索路径（本目录仍在最前）。
for _hacs_p in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                os.path.dirname(os.path.abspath(__file__))):
    sys.path.insert(0, _hacs_p)

from node.l1_transport.nats_client import NatsClient, read_secret_file  # noqa: E402
import random
import string

MAX_TIMEOUT = 300.0
DEFAULT_TIMEOUT = 120.0
# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_RUNTIME_DIR = os.environ.get("AGENT_TEAM_RUNTIME_DIR") or _np.runtime_dir()
# 兜底默认值集中到 L0 node_profile（env 优先）
AGENT_TEAM_NATS_HOST = (os.environ.get("AGENT_TEAM_NATS_HOST")
                        or _npf.nats_hosts_default())  # <NODE_IP_1> 主 / <NODE_IP_2> 备
DEFAULT_NATS_HOSTS = AGENT_TEAM_NATS_HOST
DEFAULT_PORT = 9930
HB_PREFIX = "team.hb."
REQ_FMT = "a2a.v1.req.%s"
DEDUP_WINDOW_SECONDS = 30 * 60
DEFAULT_DEDUP_FILE = os.path.join(os.path.expanduser("~"), ".team_broadcast_dedup.json")


# ---------------------------------------------------------------- 内联自总控侧群发工具
# 成员侧只下发 team.py 本文件（+ nats_client.py），云机上没有那份总控侧工具，
# 故把用得到的几个 helper 原样内联进来，保持实现与其同名函数一致。

def discover_online(client, seconds):
    # type: (NatsClient, float) -> list
    """扫 `team.hb.>` 心跳，返回窗口内报到的成员名（去重）。"""
    seen = {}
    lock = threading.Lock()

    def _on_hb(payload, reply=""):
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        names = data.get("members") if isinstance(data, dict) else None
        if not isinstance(names, list):
            names = [data.get("member")] if isinstance(data, dict) else []
        with lock:
            for n in names:
                if n:
                    seen[str(n)] = time.time()

    client.subscribe(HB_PREFIX + ">", _on_hb, sid="_bc_roster")
    deadline = time.time() + max(1.0, seconds)
    while time.time() < deadline:
        time.sleep(0.2)
    client.unsubscribe("_bc_roster")
    return sorted(seen)


def call_one(host, port, user, password, member, payload, timeout):
    # type: (str, int, str, str, str, bytes, float) -> dict
    """单独喂一个成员：一条独立连接 + 一条 request，避免共享连接相互阻塞。"""
    t0 = time.time()
    out = {"member": member, "ok": False, "reply": None, "error": None, "elapsed": 0.0}
    client = None
    try:
        client = NatsClient(host=host, port=port, user=user, password=password)
        client.connect()
        raw = client.request(REQ_FMT % member, payload, timeout=timeout)
        out["elapsed"] = round(time.time() - t0, 1)
        if raw is None:
            out["error"] = "超时未回应"
            return out
        try:
            data = json.loads(raw.decode("utf-8"))
            out["reply"] = data.get("reply") if isinstance(data, dict) else raw.decode("utf-8", "replace")
            if isinstance(data, dict) and data.get("ok") is False:
                out["error"] = "对方返回 ok=false"
                return out
        except Exception:
            out["reply"] = raw.decode("utf-8", "replace")
        out["ok"] = True
        return out
    except Exception as exc:
        out["elapsed"] = round(time.time() - t0, 1)
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        return out
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def run_pool(fn, args_list, concurrency):
    # type: (object, list, int) -> list
    """限并发跑一批任务，返回与入参同序的结果。"""
    results = [None] * len(args_list)
    sem = threading.Semaphore(max(1, concurrency))
    threads = []

    def _worker(idx, args):
        with sem:
            results[idx] = fn(*args)

    for i, a in enumerate(args_list):
        t = threading.Thread(target=_worker, args=(i, a), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return results


class TeamCliError(Exception):
    """参数/身份推断层面的错误，统一在 main() 里落成退出码 2。"""
    pass


# ---------------------------------------------------------------- 身份与口令

def _infer_member_linux():
    # type: () -> str
    d = _np.nats_dir()  # 兜底默认值集中到 L0 node_paths（env 优先）
    names = []
    if os.path.isdir(d):
        for f in glob.glob(os.path.join(d, "*.pass")):
            names.append(os.path.basename(f)[:-len(".pass")])
    if len(names) == 1:
        return names[0]
    if not names:
        raise TeamCliError("自动推断身份失败：%s 下没有任何 *.pass 文件，请显式加 --as <成员名>" % d)
    raise TeamCliError("自动推断身份失败：%s 下有多个口令文件（%s），无法确定你是谁，请显式加 --as <成员名>"
                       % (d, ", ".join(sorted(names))))


def _infer_member_windows():
    # type: () -> str
    # 兜底默认值集中到 L0 node_paths（env 优先）（无凭据时的兜底身份）
    default = os.environ.get("AGENT_TEAM_MEMBER_ID") or "openclaw-nb"
    d = os.environ.get("AGENT_TEAM_NATS_DIR") or AGENT_TEAM_RUNTIME_DIR
    if os.path.isdir(d):
        names = [os.path.basename(f)[len("nats-"):-len(".pass")]
                for f in glob.glob(os.path.join(d, "nats-*.pass"))]
        if len(names) > 1:
            raise TeamCliError("自动推断身份失败：%s 下有多个口令文件（%s），无法确定你是谁，请显式加 --as <成员名>"
                               % (d, ", ".join(sorted(names))))
    return default


def infer_member():
    # type: () -> str
    if sys.platform == "win32":
        return _infer_member_windows()
    return _infer_member_linux()


def resolve_password_file(args, member):
    # type: (argparse.Namespace, str) -> str
    if args.password_file:
        return args.password_file
    if sys.platform == "win32":
        return "%s\\nats-%s.pass" % (os.environ.get("AGENT_TEAM_NATS_DIR") or AGENT_TEAM_RUNTIME_DIR, member)
    return "%s/%s.pass" % (_np.nats_dir(), member)


def resolve_member_and_password(args):
    # type: (argparse.Namespace) -> object
    """返回 (member, password)；失败时打印原因并返回 (None, None)，调用方据此 return 2。"""
    try:
        member = args.as_member or infer_member()
    except TeamCliError as exc:
        sys.stderr.write(str(exc) + "\n")
        return None, None
    try:
        password = read_secret_file(resolve_password_file(args, member))
    except Exception as exc:
        sys.stderr.write("读口令文件失败：%s\n" % exc)
        return None, None
    return member, password


def clamp_timeout(raw):
    # type: (object) -> float
    return max(1.0, min(float(raw), MAX_TIMEOUT))


def read_text(args):
    # type: (argparse.Namespace) -> object
    """正文来源：--text-file 优先，其次位置参数 text。都没有返回 None。"""
    text_file = getattr(args, "text_file", None)
    if text_file:
        with open(text_file, "r", encoding="utf-8") as f:
            return f.read()
    return getattr(args, "text", None)


# ---------------------------------------------------------------- 去重节流（防轰炸）

def _dedup_key(subject_norm, body):
    # type: (str, str) -> str
    h = hashlib.sha1()
    h.update(subject_norm.encode("utf-8"))
    h.update(b"\x00")
    h.update((body or "").encode("utf-8"))
    return h.hexdigest()


def _acquire_lock(lock_path, timeout=5.0):
    # type: (str, float) -> bool
    """独占创建锁文件；拿不到就在超时后放弃（降级为无锁，靠"写临时文件再 rename"兜底一致性）。"""
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.close(fd)
            return True
        except FileExistsError:
            if time.time() > deadline:
                return False
            time.sleep(0.05)
        except Exception:
            return False


def _release_lock(lock_path):
    # type: (str) -> None
    try:
        os.remove(lock_path)
    except Exception:
        pass


def _load_dedup(path):
    # type: (str) -> dict
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_dedup(path, data):
    # type: (str, dict) -> None
    """写临时文件再 rename，避免并发写导致文件损坏（Windows/Linux 都适用）。"""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".team_dedup_", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def check_and_mark_dedup(dedup_file, subject_norm, body, force):
    # type: (str, str, str, bool) -> object
    """命中窗口内已发过 → (False, key)；否则先标记为已发送再 (True, key)。

    标记先于实际发送，防止同一时刻并发调用重复发送；调用方发送失败要用
    rollback_dedup(dedup_file, key) 撤销标记，别把没发出去的记成已发。
    """
    key = _dedup_key(subject_norm, body)
    dedup_file = os.path.expanduser(dedup_file)
    lock_path = dedup_file + ".lock"
    got_lock = _acquire_lock(lock_path)
    try:
        data = _load_dedup(dedup_file)
        now = time.time()
        data = dict((k, v) for k, v in data.items() if now - v < DEDUP_WINDOW_SECONDS)
        if not force:
            ts = data.get(key)
            if ts is not None and now - ts < DEDUP_WINDOW_SECONDS:
                return False, key
        data[key] = now
        _save_dedup(dedup_file, data)
        return True, key
    finally:
        if got_lock:
            _release_lock(lock_path)


def rollback_dedup(dedup_file, key):
    # type: (str, str) -> None
    dedup_file = os.path.expanduser(dedup_file)
    lock_path = dedup_file + ".lock"
    got_lock = _acquire_lock(lock_path)
    try:
        data = _load_dedup(dedup_file)
        data.pop(key, None)
        _save_dedup(dedup_file, data)
    finally:
        if got_lock:
            _release_lock(lock_path)


# ---------------------------------------------------------------- 心跳采集（who 的数据面）

def collect_heartbeats(client, seconds):
    # type: (NatsClient, float) -> dict
    seen = {}
    lock = threading.Lock()

    def on_hb(payload, reply=""):
        try:
            obj = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        member = obj.get("member") or "?"
        with lock:
            seen[member] = obj

    client.subscribe(HB_PREFIX + ">", on_hb, sid="_team_who")
    deadline = time.time() + max(1.0, seconds)
    while time.time() < deadline:
        time.sleep(0.2)
    client.unsubscribe("_team_who")
    return seen


# ------------------------------------------------------------
# 刀15（阶段3）：以下块自 team.py 逐字节下沉（cmd_who / say 三件套辅助 / cmd_say / cmd_broadcast）
# ------------------------------------------------------------

def cmd_who(args):
    # type: (argparse.Namespace) -> int
    member, password = resolve_member_and_password(args)
    if member is None:
        return 2
    try:
        client = NatsClient(host=args.nats_host, port=args.nats_port,
                            user=member, password=password, name="team-who-" + member)
        client.connect()
    except Exception as exc:
        sys.stderr.write("连接 NATS 中枢失败：%s\n" % exc)
        return 2
    try:
        seen = collect_heartbeats(client, args.seconds)
    finally:
        client.close()

    if args.json:
        for m in sorted(seen):
            print(json.dumps(seen[m], ensure_ascii=False))
        return 0 if seen else 1

    if not seen:
        print("本窗口 %.0fs 内没收到任何心跳（检查中枢是否在跑、口令是否正确）" % args.seconds)
        return 1
    print("在线成员 %d 个（窗口 %.0fs）：" % (len(seen), args.seconds))
    print("成员|主机名|心跳距今|版本")
    now = time.time()
    for m in sorted(seen):
        o = seen[m]
        age = int(now - int(o.get("ts") or 0))
        print("%s|%s|%ss|%s" % (m, o.get("host"), age, o.get("version")))
    return 0


# ---------------------------------------------------------------- say：点对点呼叫

def derive_default_session(from_member, target, now=None):
    # type: (str, str, float) -> str
    """不带 --session-id/--new-session 时的默认会话键：按**小时**分桶的稳定派生。

    同一小时内、同一 (发送方, 目标) 的连续对话落在同一会话 —— 多轮上下文才成立；
    跨小时自动滚新桶，避免后端上下文无限堆积（<MEMBER_ID> 实测已用到 38k/120k）。

    为什么按小时分桶而不是固定键：没有「对话边界」信号时，固定键会把一整天里
    互不相关的对话全塞进同一段上下文 —— token 顶到模型窗口上限，且互相污染。
    """
    ts = time.strftime("%Y%m%d%H", time.localtime(now if now is not None else time.time()))
    return "say-%s-%s-%s" % (from_member, target, ts)


def make_say_session_id(from_member, target, now=None):
    # type: (str, str, float) -> str
    """--new-session：强制开一段新会话（不接上文）。"""
    ts = time.strftime("%Y%m%d%H%M%S", time.localtime(now if now is not None else time.time()))
    rand = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    return "say-%s-%s-%s-%s" % (from_member, target, ts, rand)


def resolve_say_session_id(args, member):
    # type: (argparse.Namespace, str) -> str
    """会话 id 优先级：显式 --session-id > --new-session > 默认派生（按小时）。"""
    if getattr(args, "session_id", None):
        return args.session_id
    if getattr(args, "new_session", False):
        return make_say_session_id(member, args.target)
    return derive_default_session(member, args.target)


def build_say_envelope(text, member, target, timeout, session_id):
    # type: (str, str, str, float, str) -> dict
    """构造 say 请求信封（纯函数，便于单测）。

    from 用的是**已解析的真实身份** member，不是调用方传进来的字符串。
    """
    return {"text": text, "timeout_s": timeout, "from": member, "session_id": session_id}


def cmd_say(args):
    # type: (argparse.Namespace) -> int
    # 互斥检查放最前：参数错误要早报，且不被「读口令失败」之类的原因遮掉（SESS-07）
    if getattr(args, "session_id", None) and getattr(args, "new_session", False):
        sys.stderr.write("--session-id 与 --new-session 互斥，只能给一个\n")
        return 2
    member, password = resolve_member_and_password(args)
    if member is None:
        return 2
    text = read_text(args)
    if not text or not str(text).strip():
        sys.stderr.write("必须提供正文（位置参数或 --text-file）\n")
        return 2

    timeout = clamp_timeout(args.timeout)
    subject = REQ_FMT % args.target
    # 会话 id：显式指定 > --new-session > 默认按 (发送方, 目标, 小时) 派生。
    # 原先这里是 `if args.session_id:` —— 不带就完全不传，于是每轮都是全新会话、
    # 零上下文，agent 会忘记自己刚承诺过的事（已实测复现）。
    session_id = resolve_say_session_id(args, member)
    envelope = build_say_envelope(text, member, args.target, timeout, session_id)
    raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    try:
        client = NatsClient(host=args.nats_host, port=args.nats_port,
                            user=member, password=password, name="team-say-" + member)
        client.connect()
    except Exception as exc:
        sys.stderr.write("连不上 NATS 中枢：%s\n" % exc)
        return 2

    try:
        reply = client.request(subject, raw, timeout=timeout + 15.0)
    except Exception as exc:
        sys.stderr.write("请求异常：%s\n" % exc)
        return 2
    finally:
        try:
            client.close()
        except Exception:
            pass

    if reply is None:
        sys.stderr.write("点名超时（%ss 内无应答）—— 确认 <%s> 的 teamd 在跑、subject 正确：%s\n"
                         % (timeout, args.target, subject))
        return 1

    text_reply = reply.decode("utf-8", "replace")
    try:
        obj = json.loads(text_reply)
    except ValueError:
        sys.stdout.write(text_reply + "\n")
        return 0

    if args.json:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    else:
        if obj.get("ok") and obj.get("reply") is not None:
            sys.stdout.write(str(obj.get("reply")) + "\n")
        else:
            sys.stderr.write("对方回了错误：%s\n" % json.dumps(obj, ensure_ascii=False))
    return 0 if obj.get("ok") else 3


# ---------------------------------------------------------------- broadcast：群发

def cmd_broadcast(args):
    # type: (argparse.Namespace) -> int
    member, password = resolve_member_and_password(args)
    if member is None:
        return 2
    text = read_text(args)
    if not text or not str(text).strip():
        sys.stderr.write("必须提供正文（位置参数或 --text-file）\n")
        return 2
    if bool(args.all) == bool(args.to):
        sys.stderr.write("必须**且只能**给 --all 或 --to 其中一个\n")
        return 2

    timeout = clamp_timeout(args.timeout)

    if args.all:
        target_desc = "all"
        target_list = None
    else:
        target_list = sorted(set(m.strip() for m in args.to.split(",") if m.strip()))
        if not target_list:
            sys.stderr.write("--to 不能为空\n")
            return 2
        target_desc = ",".join(target_list)

    subject_norm = "broadcast:%s" % target_desc
    should_send, key = check_and_mark_dedup(args.dedup_file, subject_norm, text, args.force)
    if not should_send:
        print("本窗口内已发过同一内容，跳过（用 --force 强制重发）")
        return 0

    try:
        if args.all:
            client = NatsClient(host=args.nats_host, port=args.nats_port,
                                user=member, password=password, name="team-bc-" + member)
            client.connect()
            members = discover_online(client, args.roster_seconds)
            client.close()
            members = [m for m in members if m != member]
        else:
            members = [m for m in target_list if m != member]
    except Exception as exc:
        rollback_dedup(args.dedup_file, key)
        sys.stderr.write("连接中枢/取名单失败：%s\n" % exc)
        return 2

    if not members:
        rollback_dedup(args.dedup_file, key)
        sys.stderr.write("目标名单为空（%s）\n"
                         % ("窗口 %.0fs 内没扫到在线成员" % args.roster_seconds if args.all
                            else "排除自己后 --to 没有其他成员"))
        return 3

    envelope = {"text": text, "timeout_s": timeout, "subject": "群发", "from": member}
    if args.session_id:
        envelope["session_id"] = args.session_id
    payload = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    print("群发目标 %d 个：%s　（并行度 %d，单成员超时 %.0fs）"
          % (len(members), ",".join(members), args.concurrency, timeout))
    try:
        args_list = [(args.nats_host, args.nats_port, member, password, m, payload, timeout)
                    for m in members]
        results = run_pool(call_one, args_list, args.concurrency)
    except Exception as exc:
        rollback_dedup(args.dedup_file, key)
        sys.stderr.write("发送失败，已回滚去重标记：%s\n" % exc)
        return 2

    ok = [r for r in results if r and r.get("ok")]
    bad = [r for r in results if not (r and r.get("ok"))]
    for r in results:
        mark = "OK" if r.get("ok") else "X "
        rep = (r.get("reply") or r.get("error") or "").replace("\n", " ")
        if len(rep) > 160:
            rep = rep[:160] + "…"
        print("  [%s] %-14s %6.1fs  %s" % (mark, r.get("member"), r.get("elapsed", 0.0), rep))
    print("汇总：%d/%d 回应" % (len(ok), len(members)))

    if args.json:
        print(json.dumps({"ok": len(ok), "total": len(members), "results": results},
                         ensure_ascii=False, indent=2))
    return 0 if not bad else 1
