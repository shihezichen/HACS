#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
team.py —— 成员侧统一通信 CLI（"成员平等"的核心）

背景：过去只有总控（<MEMBER_ID>）配齐了 team_call.py / team_broadcast.py / team_roster.py 这套
工具，其他成员只能"被点名"，自己发不出呼叫。本工具让**任意一个成员**在 <MEMBER_ID> 不在线、
主中枢（NATS 主中枢）也不在线（走 <MEMBER_ID> 热备）的情况下，仍能主动：
  * 点对点呼叫队友（say）
  * 群发消息（broadcast）
  * 召集临时会议（meet）并留痕回放（history）
  * 查看谁在线（who）

全程只走 NATS 中枢（<NODE_IP_1>:9930 主 / <NODE_IP_2>:9930 备，逐个尝试到连上），
不依赖总控、不依赖黑板/PG——这是"正式会议才写黑板"之外的降级通道。

复用现有实现，不重造轮子：
  * 多地址故障转移能力来自改造过的 nats_client.NatsClient（host 支持逗号分隔/list）。
  * 群发的并行扇出/在线名单发现（call_one / discover_online / run_pool）内联自总控侧
    同名工具的实现，成员侧只下发本文件 + nats_client.py，不依赖那份工具本身。
  * 点对点请求/回包解析的写法与 team_call.py 保持一致（一致的错误提示与退出码）。

身份与连接参数（尽量少输）：
  --as <成员>       显式指定自己是谁；省略时自动推断——
                    Linux：扫 <PASS_DIR>/*.pass，要求**恰好一个**；
                    Windows：默认 <MEMBER_ID>，但若 <PASS_DIR>\\nats-*.pass 存在多个，
                    视为身份不明确，报错要求显式 --as。
  --password-file   默认按平台 + 上面解出的成员名拼路径；可覆盖。
  --nats-host       默认多地址 "<NODE_IP_1>:9930,<NODE_IP_2>:9930"；可逗号分隔多个覆盖。
  --nats-port       地址未显式带端口时的兜底端口，默认 9930。
  --timeout         等回应上限秒，默认 120，最大 300。
  --json            打印原始 JSON。

节流防轰炸（broadcast / meet 共用）：
  发送前用 sha1(主题归一化 + 正文) 查本地去重文件（默认 ~/.team_broadcast_dedup.json），
  30 分钟窗口内命中就跳过（退出码 0），--force 可强制重发；发送失败会回滚去重标记。

退出码约定（与 team_call.py / team_broadcast.py 一致）：
  0 成功 / 1 无应答或超时（或有成员缺席） / 2 参数或连接错误 / 3 对方回了错误结构（或名单为空）

用法示例：
  python team.py who
  python team.py say <MEMBER_ID> "你在吗？"
  python team.py broadcast --all "全体注意：..."
  python team.py broadcast --to <MEMBER_ID_A>,<MEMBER_ID_B> --text-file msg.md
  python team.py meet --goal "对齐明天的发布计划" --agenda "确认范围" --agenda "定风险点" \\
      --members <MEMBER_ID_A>,<MEMBER_ID_B>,<MEMBER_ID_C>
  python team.py history --session-id <SESSION_ID>
"""

import argparse
import glob
import hashlib
import json
import os
import random
import string
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import node_paths as _np  # L0 平台层：宿主默认值唯一出处（env 优先）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nats_client import NatsClient, read_secret_file  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass  # 老版本 Python / 非标准 stream 兜底：拿不到就算了，别把整个命令搞挂

MAX_TIMEOUT = 300.0
DEFAULT_TIMEOUT = 120.0
# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_RUNTIME_DIR = os.environ.get("AGENT_TEAM_RUNTIME_DIR") or _np.runtime_dir()
# 兜底默认值集中到 L0 node_paths（env 优先）
AGENT_TEAM_NATS_HOST = (os.environ.get("AGENT_TEAM_NATS_HOST")
                        or "<host>:9930,<node-ip>:9930")  # <NODE_IP_1> 主 / <NODE_IP_2> 备
DEFAULT_NATS_HOSTS = AGENT_TEAM_NATS_HOST
DEFAULT_PORT = 9930
HB_PREFIX = "team.hb."
REQ_FMT = "a2a.v1.req.%s"
DEDUP_WINDOW_SECONDS = 30 * 60
DEFAULT_DEDUP_FILE = os.path.join(os.path.expanduser("~"), ".team_broadcast_dedup.json")
DEFAULT_MEETINGS_FILE = os.path.join(os.path.expanduser("~"), ".team_meetings.jsonl")
#: 人机对话台（群的家）—— 群消息走 HTTP 落库，NATS 只管唤醒通知（契约 §I）
# 兜底默认值集中到 L0 node_paths（env 优先）
DEFAULT_CHAT_BASE = os.environ.get("AGENT_TEAM_CHAT_BASE") or os.environ.get("TEAM_CHAT_BASE") or "http://<host>:9984"


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


# ---------------------------------------------------------------- who：谁在线

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


# ---------------------------------------------------------------- meet：临时会议

def make_session_id():
    # type: () -> str
    ts = time.strftime("%Y%m%d-%H%M")
    rand = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    return "meet-%s-%s" % (ts, rand)


def build_meeting_body(goal, agenda, members, session_id):
    # type: (str, list, list, str) -> str
    lines = ["【临时会议召集】session_id=%s" % session_id, "目标：%s" % goal, "议程："]
    for i, item in enumerate(agenda, 1):
        lines.append("  %d. %s" % (i, item))
    lines.append("参会成员：%s" % ", ".join(members))
    lines.append("请每位参会成员按以下格式回报（一行，不要额外寒暄）：")
    lines.append("<你的成员名>|<真实主机名>|<一句话观点/结论>")
    return "\n".join(lines)


def append_jsonl(path, obj):
    # type: (str, dict) -> None
    full = os.path.expanduser(path)
    d = os.path.dirname(full)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(full, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def cmd_meet(args):
    # type: (argparse.Namespace) -> int
    member, password = resolve_member_and_password(args)
    if member is None:
        return 2
    if not args.goal or not args.goal.strip():
        sys.stderr.write("必须给 --goal\n")
        return 2
    agenda = args.agenda or []
    if not agenda:
        sys.stderr.write("必须至少给一条 --agenda\n")
        return 2
    members = sorted(set(m.strip() for m in (args.members or "").split(",") if m.strip()))
    if not members:
        sys.stderr.write("必须给 --members\n")
        return 2

    session_id = args.session_id or make_session_id()
    timeout = clamp_timeout(args.timeout)
    body = build_meeting_body(args.goal, agenda, members, session_id)

    subject_norm = "meet:%s" % ",".join(members)
    should_send, key = check_and_mark_dedup(args.dedup_file, subject_norm, body, args.force)
    if not should_send:
        print("本窗口内已发过同一内容，跳过（用 --force 强制重发）")
        return 0

    envelope = {"text": body, "session_id": session_id, "timeout_s": timeout, "from": member}
    payload = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    print("召集会议 session_id=%s　参会 %d 人：%s" % (session_id, len(members), ",".join(members)))
    try:
        args_list = [(args.nats_host, args.nats_port, member, password, m, payload, timeout)
                    for m in members]
        results = run_pool(call_one, args_list, args.concurrency)
    except Exception as exc:
        rollback_dedup(args.dedup_file, key)
        sys.stderr.write("召集失败，已回滚去重标记：%s\n" % exc)
        return 2

    present = [r for r in results if r and r.get("ok")]
    absent = [r for r in results if not (r and r.get("ok"))]

    print("目标：%s" % args.goal)
    for i, item in enumerate(agenda, 1):
        print("  议程 %d：%s" % (i, item))
    print("-" * 60)
    for r in results:
        rep = (r.get("reply") or r.get("error") or "").replace("\n", " ")
        if r.get("ok"):
            print("  [到会] %-14s %6.1fs  %s" % (r.get("member"), r.get("elapsed", 0.0), rep))
        else:
            print("  [缺席] %-14s %6.1fs  %s（不折算进通过率）"
                  % (r.get("member"), r.get("elapsed", 0.0), rep))
    print("汇总：到会 %d / 应到 %d" % (len(present), len(members)))

    record = {
        "session_id": session_id, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "convener": member, "goal": args.goal, "agenda": agenda, "members": members,
        "body": body, "results": results,
        "present": [r.get("member") for r in present],
        "absent": [r.get("member") for r in absent],
    }
    try:
        append_jsonl(args.out, record)
    except Exception as exc:
        sys.stderr.write("警告：本地留痕写入失败（不影响本次会议结果）：%s\n" % exc)

    if args.json:
        print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if not absent else 1


# ---------------------------------------------------------------- history：回放

def cmd_history(args):
    # type: (argparse.Namespace) -> int
    path = os.path.expanduser(args.file)
    if not os.path.isfile(path):
        sys.stderr.write("留痕文件不存在：%s\n" % path)
        return 2

    found = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("session_id") == args.session_id:
                found = obj  # 同一 session_id 可能重发过，取文件里最后一条

    if found is None:
        sys.stderr.write("没找到 session_id=%s 的会议记录（文件：%s）\n" % (args.session_id, path))
        return 1

    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 0

    print("会议 session_id=%s　召集人=%s　时间=%s"
          % (found.get("session_id"), found.get("convener"), found.get("ts")))
    print("目标：%s" % found.get("goal"))
    for i, item in enumerate(found.get("agenda") or [], 1):
        print("  议程 %d：%s" % (i, item))
    print("-" * 60)
    for r in found.get("results") or []:
        tag = "到会" if r.get("ok") else "缺席"
        rep = (r.get("reply") or r.get("error") or "").replace("\n", " ")
        print("  [%s] %-14s  %s" % (tag, r.get("member"), rep))
    print("汇总：到会 %d / 应到 %d" % (len(found.get("present") or []), len(found.get("members") or [])))
    return 0


# ---------------------------------------------------------------- 参数解析

def build_common_parser():
    # type: () -> argparse.ArgumentParser
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--as", dest="as_member", default=None,
                   help="显式指定自己是谁；省略时自动推断（Linux 扫 <PASS_DIR>/*.pass 取唯一一个，"
                        "Windows 默认 <MEMBER_ID>，若口令文件不唯一则报错要求显式指定）")
    p.add_argument("--password-file", default=None,
                   help="自己的 NATS 口令文件；默认按平台 + --as 推断出的成员名拼路径")
    p.add_argument("--nats-host", default=DEFAULT_NATS_HOSTS,
                   help="NATS 中枢地址，逗号分隔可给多个做故障转移（默认：<NODE_IP_1> 主/<NODE_IP_2> 备）")
    p.add_argument("--nats-port", type=int, default=DEFAULT_PORT,
                   help="兜底端口，仅用于地址里没显式带端口的项（默认 9930）")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   help="等回应上限秒（默认 120，最大 300）")
    p.add_argument("--json", action="store_true", help="打印原始 JSON")
    return p


# ---------------------------------------------------------------- 临时群（契约 §I）
#
# 群 = 一个 meet 场会话，消息落库在对话台（不落在 NATS 里）。
# 铁律：**Agent 发言必须 @ 至少一个目标**（规则 6），服务端会硬拒无 @ 的发言。
# 视角：pull 拉的是**全群增量**（谁说的都看得见），不是只给我的。

def _chat_call(path, method="GET", body=None, member=None, base=None, timeout=15.0):
    # type: (str, str, object, object, object, float) -> tuple
    """打对话台 HTTP 接口。Agent 身份用 X-Agent-Id（只在 tailnet 来源被服务端接受）。"""
    url = (base or DEFAULT_CHAT_BASE).rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if member:
        req.add_header("X-Agent-Id", member)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def _chat_member(args, required=False):
    # type: (argparse.Namespace, bool) -> object
    """群里的"我是谁"：显式 --as 优先，否则自动推断。

    读操作（show/pull）不该因为推断失败就用不了 —— 推断不出来就回 None，
    服务端按"匿名 tailnet"处理：照样能看全群，只是算不出"我欠谁"。
    """
    who = getattr(args, "as_member", None)
    if who:
        return who
    try:
        return infer_member()
    except TeamCliError:
        if required:
            raise
        return None


def _chat_error(status, body):
    # type: (int, object) -> str
    err = (body or {}).get("error") or {}
    return "HTTP %s %s：%s" % (status, err.get("code") or "?", err.get("message") or "")


def cmd_group_say(args):
    # type: (argparse.Namespace) -> int
    member = _chat_member(args, required=True)
    text = read_text(args)
    if not text or not str(text).strip():
        raise TeamCliError("发言不能为空：给位置参数 text 或 --text-file")
    raw_to = getattr(args, "to", None)
    if raw_to is None:
        raw_to = "@all"                       # 只有"没写 --to"才默认全员
    raw_to = raw_to.strip()
    if not raw_to:
        raise TeamCliError("--to 不能为空：写 @all 或成员 id 逗号分隔"
                           "（Agent 发言必须 @，服务端会硬拒无 @ 的发言）")
    mentions = (["@all"] if raw_to == "@all"
                else [t.strip() for t in raw_to.split(",") if t.strip()])
    status, body = _chat_call(
        "/api/sessions/%s/messages" % args.group, "POST",
        {"text": text, "mentions": mentions,
         "reply_to": getattr(args, "reply_to", None),
         "client_msg_id": getattr(args, "client_msg_id", None)},
        member=member, base=args.chat_base)
    if status != 201:
        print("发言失败：%s" % _chat_error(status, body))
        if getattr(args, "json", False):
            print(json.dumps(body, ensure_ascii=False, indent=2))
        return 1
    msg = body["message"]
    if getattr(args, "json", False):
        print(json.dumps(msg, ensure_ascii=False, indent=2))
        return 0
    meta = msg.get("meta") or {}
    print("[%s] %s → %s" % (msg["seq"], member, "、".join(meta.get("mentions") or [])))
    print("已落群：seq=%s id=%s" % (msg["seq"], msg["message_id"]))
    return 0


def cmd_group_pull(args):
    # type: (argparse.Namespace) -> int
    member = _chat_member(args)
    if member is None:
        print("（没认出我是谁：加 --as <成员名> 才能看到\u201c我欠谁回应\u201d）")
    query = "?limit=%d" % max(1, int(getattr(args, "limit", 50) or 50))
    if getattr(args, "since", None) is not None:
        query += "&after_seq=%d" % int(args.since)
    status, body = _chat_call("/api/sessions/%s/messages%s" % (args.group, query),
                              member=member, base=args.chat_base)
    if status != 200:
        print("拉取失败：%s" % _chat_error(status, body))
        return 1
    if getattr(args, "json", False):
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0
    messages = body.get("messages") or []
    print("群 %s 共 %d 条（我是 %s）" % (args.group, len(messages), member))
    for msg in messages:
        meta = msg.get("meta") or {}
        who = (msg.get("author") or {}).get("display") or (msg.get("author") or {}).get("id")
        to = "、".join(meta.get("mentions") or []) or "-"
        mark = " *" if member in (meta.get("needs_response") or []) else ""
        print("  [%s]%s %s → %s：%s" % (msg.get("seq"), mark, who, to,
                                       (msg.get("text") or "").replace("\n", " ")))
    pending = body.get("my_pending") or []
    if pending:
        print("\n我还欠 %d 条回应（@我 且我没回过）：" % len(pending))
        for item in pending:
            print("  seq %s 来自 %s：%s" % (item.get("seq"), item.get("from"), item.get("text")))
        print("（回的时候记得 @ 对方，规则 6）")
    else:
        print("\n没有欠回应。")
    return 0


def cmd_group_show(args):
    # type: (argparse.Namespace) -> int
    status, body = _chat_call("/api/sessions/%s" % args.group,
                              member=_chat_member(args), base=args.chat_base)
    if status != 200:
        print("查群失败：%s" % _chat_error(status, body))
        return 1
    if getattr(args, "json", False):
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0
    sess = body["session"]
    print("群：%s" % sess.get("title"))
    print("  id     ：%s" % sess.get("session_id"))
    print("  状态   ：%s%s" % (sess.get("state"),
                            "（已结束，只读）" if sess.get("state") != "open" else ""))
    print("  发起人 ：%s" % sess.get("created_by"))
    print("  成员   ：%s" % "、".join(m.get("id") for m in (sess.get("members") or [])))
    return 0


def build_parser():
    # type: () -> argparse.ArgumentParser
    common = build_common_parser()
    ap = argparse.ArgumentParser(
        prog="team.py",
        description="成员侧统一通信 CLI（成员平等）：点对点呼叫 / 群发 / 召集临时会议，"
                     "全程只走 NATS，不依赖总控/黑板")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_who = sub.add_parser("who", parents=[common], help="谁在线：扫心跳打印在线成员")
    p_who.add_argument("--seconds", type=float, default=35.0,
                       help="扫描窗口秒（默认 35，心跳周期 30s，给太短会漏人）")
    p_who.set_defaults(func=cmd_who)

    p_say = sub.add_parser("say", parents=[common], help="点对点呼叫某个成员，等其回应")
    p_say.add_argument("target", help="被叫成员 id，如 <MEMBER_ID>")
    p_say.add_argument("text", nargs="?", default=None, help="正文（中文长文本建议改用 --text-file）")
    p_say.add_argument("--text-file", default=None, help="正文文件（UTF-8）")
    p_say.add_argument("--session-id", default=None,
                       help="会话 id（同一 id 对方有上下文）；不给则按 发送方-目标-小时 自动派生")
    p_say.add_argument("--new-session", action="store_true",
                       help="强制开一段新会话（不接上文）；与 --session-id 互斥")
    p_say.set_defaults(func=cmd_say)

    p_bc = sub.add_parser("broadcast", parents=[common], help="群发一批成员并收回应")
    p_bc.add_argument("text", nargs="?", default=None, help="正文（中文长文本建议改用 --text-file）")
    p_bc.add_argument("--text-file", default=None, help="正文文件（UTF-8）")
    p_bc.add_argument("--all", action="store_true", help="面向全部在线成员（先扫心跳取名单）")
    p_bc.add_argument("--to", default=None, help="指定成员，逗号分隔（与 --all 二选一）")
    p_bc.add_argument("--session-id", default=None, help="会话 id（同一 id 各成员有上下文）")
    p_bc.add_argument("--concurrency", type=int, default=3, help="并行度（默认 3）")
    p_bc.add_argument("--roster-seconds", type=float, default=40.0, help="--all 扫心跳窗口秒（默认 40）")
    p_bc.add_argument("--dedup-file", default=DEFAULT_DEDUP_FILE,
                      help="去重记录文件（默认 ~/.team_broadcast_dedup.json）")
    p_bc.add_argument("--force", action="store_true", help="跳过 30 分钟去重检查，强制重发")
    p_bc.set_defaults(func=cmd_broadcast)

    p_meet = sub.add_parser("meet", parents=[common], help="召集临时会议：广播议程 + 收发言 + 出结论")
    p_meet.add_argument("--goal", required=True, help="会议目标")
    p_meet.add_argument("--agenda", action="append", default=None,
                        help="议程条目（标题+预期产出），可重复给出多条")
    p_meet.add_argument("--members", required=True, help="参会成员，逗号分隔")
    p_meet.add_argument("--session-id", default=None,
                        help="会话 id（默认自动生成 meet-<YYYYMMDD-HHMM>-<4位随机>）")
    p_meet.add_argument("--concurrency", type=int, default=3, help="并行度（默认 3）")
    p_meet.add_argument("--dedup-file", default=DEFAULT_DEDUP_FILE,
                        help="去重记录文件（默认 ~/.team_broadcast_dedup.json）")
    p_meet.add_argument("--force", action="store_true", help="跳过 30 分钟去重检查，强制重发")
    p_meet.add_argument("--out", default=DEFAULT_MEETINGS_FILE,
                        help="本地留痕文件（默认 ~/.team_meetings.jsonl），一行一场会议，只本机可见")
    p_meet.set_defaults(func=cmd_meet)

    p_hist = sub.add_parser("history", parents=[common], help="回放某场会议的召集与各成员发言")
    p_hist.add_argument("--session-id", required=True, help="要回放的会议 session_id")
    p_hist.add_argument("--file", default=DEFAULT_MEETINGS_FILE,
                        help="留痕文件（默认 ~/.team_meetings.jsonl）")
    p_hist.set_defaults(func=cmd_history)

    # -- 临时群（契约 §I）--------------------------------------------
    p_grp = sub.add_parser("group",
                           help="临时群：群里发言 / 拉全群增量（走对话台 HTTP，不是 NATS）")
    grp = p_grp.add_subparsers(dest="group_cmd", required=True)

    g_say = grp.add_parser("say", parents=[common],
                           help="在群里发言（Agent 必须 @；规则 6 由服务端硬门禁）")
    g_say.add_argument("--group", required=True, help="群 id（session_id，如 <SESSION_ID>）")
    g_say.add_argument("text", nargs="?", default=None, help="正文（长文建议 --text-file）")
    g_say.add_argument("--text-file", default=None, help="正文文件（UTF-8）")
    g_say.add_argument("--to", default="@all",
                       help="@all（默认，全员）或成员 id 逗号分隔，如 <MEMBER_ID>,<USER_ID>")
    g_say.add_argument("--reply-to", default=None, help="引用某条的 message_id（登记这是回应）")
    g_say.add_argument("--client-msg-id", default=None, help="幂等键，防重复提交")
    g_say.add_argument("--chat-base", default=DEFAULT_CHAT_BASE, help="对话台地址")
    g_say.set_defaults(func=cmd_group_say)

    g_pull = grp.add_parser("pull", parents=[common],
                            help="拉全群增量：谁说的都看得见＋我欠谁的回应")
    g_pull.add_argument("--group", required=True, help="群 id")
    g_pull.add_argument("--since", type=int, default=None, help="只取 seq 大于它的（增量拉）")
    g_pull.add_argument("--limit", type=int, default=50, help="最多几条（默认 50）")
    g_pull.add_argument("--chat-base", default=DEFAULT_CHAT_BASE, help="对话台地址")
    g_pull.set_defaults(func=cmd_group_pull)

    g_show = grp.add_parser("show", parents=[common],
                            help="看群详情：标题 / 状态 / 发起人 / 成员")
    g_show.add_argument("--group", required=True, help="群 id")
    g_show.add_argument("--chat-base", default=DEFAULT_CHAT_BASE, help="对话台地址")
    g_show.set_defaults(func=cmd_group_show)

    return ap


def main(argv=None):
    # type: (object) -> int
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TeamCliError as exc:
        # 给 Agent 看的话，一行说清比整个堆栈有用
        print("错误：%s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
