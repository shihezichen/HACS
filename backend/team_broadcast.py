#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""team_broadcast.py —— 新平面(9930)群发：向一批成员发同一条消息并收回应

与 broadcast_ask.py 的区别（重要，别混用）：
  * broadcast_ask.py 走**旧平面**(quantum:4222 + JetStream，已按令退役)。
  * 本脚本走**新平面**：中枢 `lobster:9930`，核心 NATS request/reply；
    成员端由常驻 `teamd` 兜住 `a2a.v1.req.<成员>`(见 teamd.py `REQ_SUBJECT_FMT`)。

「群发」在本平面怎么实现：
  成员 teamd **只订自己的点名主题**、没有一个公共广播主题，所以群发 = 对名单
  **并行扇出** 同一份 payload，各自 request/reply，再汇总成表。这天然带回应。

用法：
  python team_broadcast.py --all --text "全体注意：今晚 23:00 停机窗口" \
      --nats-host <host> --user hermes --password-file <deploy-dir>/nats-hermes.pass
  python team_broadcast.py --members lobster,top001 --text-file msg.md ... --timeout 180

选项：
  --members a,b      指定多个成员（逗号分隔）
  --all              面向**全部在线成员**（先扫 team.hb.> 心跳取名单，排除自己）
  --text / --text-file  正文（中文务必用 --text-file，避免 bash argv GBK 乱码）
  --session-id       同一会话 id → 各成员按该 id 保留上下文
  --no-wait          只投递、不等回应（通知类；仍逐成员投递，不是黑洞广播）
  --timeout N        单个成员等回应上限秒（默认 120，最大 300）
  --concurrency N    并行度（默认 3，避免同时唤醒太多成员造成风暴）
  --roster-seconds N --all 时扫心跳窗口秒数（默认 40）
  --json / --out F   JSON 结果打印 / 落文件
退出码：0 全员回应；1 有成员未回应/报错；2 参数或连接错误；3 名单为空
"""
import argparse
import json
import os
import sys
import threading
import time

# 批① 起被搬件按绝对包名 import，故把仓根一并放进搜索路径（本目录仍在最前）。
for _hacs_p in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                os.path.dirname(os.path.abspath(__file__))):
    sys.path.insert(0, _hacs_p)

from node.l1_transport.nats_client import NatsClient, read_secret_file  # noqa: E402
import node_profile as _npf  # noqa: E402  L0 节点档案：宿主事实唯一真源（help 文本里的地址形态）

MAX_TIMEOUT = 300.0
DEFAULT_TIMEOUT = 120.0
HB_PREFIX = "team.hb."
REQ_FMT = "a2a.v1.req.%s"


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


def call_one_nowait(host, port, user, password, member, payload):
    # type: (str, int, str, str, str, bytes) -> dict
    """只投递不等回应（--no-wait）。"""
    out = {"member": member, "ok": False, "reply": None, "error": None, "elapsed": 0.0}
    t0 = time.time()
    client = None
    try:
        client = NatsClient(host=host, port=port, user=user, password=password)
        client.connect()
        client.publish(REQ_FMT % member, payload)
        client.flush(timeout=5.0)
        out["ok"] = True
        out["reply"] = "(已投递，未等回应)"
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        out["elapsed"] = round(time.time() - t0, 1)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    return out


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


def main(argv=None):
    # type: (object) -> int
    ap = argparse.ArgumentParser(description="新平面(9930)群发：向一批成员发同一消息并收回应")
    ap.add_argument("--members", default=None, help="成员名单，逗号分隔（如 lobster,top001）")
    ap.add_argument("--all", action="store_true", help="面向全部在线成员（扫心跳取名单）")
    ap.add_argument("--text", default=None, help="正文（中文建议改用 --text-file）")
    ap.add_argument("--text-file", default=None, help="正文文件（UTF-8）")
    ap.add_argument("--subject", default="群发", help="主题标签（仅记录/展示用）")
    ap.add_argument("--session-id", default=None, help="会话 id（同一 id 各成员有上下文）")
    ap.add_argument("--nats-host", required=True, help="NATS 中枢地址（如 %s）" % _npf.nats_host_shape())
    ap.add_argument("--nats-port", type=int, default=9930, help="NATS 端口（默认 9930）")
    ap.add_argument("--user", required=True, help="自己的 NATS 用户名（通常 hermes）")
    ap.add_argument("--password-file", required=True, help="NATS 口令文件")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单成员等回应秒（默认 120，最大 300）")
    ap.add_argument("--concurrency", type=int, default=3, help="并行度（默认 3）")
    ap.add_argument("--roster-seconds", type=float, default=40.0, help="--all 扫心跳窗口秒（默认 40）")
    ap.add_argument("--no-wait", action="store_true", help="只投递不等回应（通知类）")
    ap.add_argument("--dry-run", action="store_true", help="只列出目标名单、不发送（先看清发给谁）")
    ap.add_argument("--exclude", default="", help="额外排除的成员，逗号分隔")
    ap.add_argument("--json", action="store_true", help="打印 JSON 结果")
    ap.add_argument("--out", default=None, help="结果落文件（JSON）")
    args = ap.parse_args(argv)

    if not args.text and not args.text_file:
        sys.stderr.write("必须给 --text 或 --text-file\n")
        return 2
    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as fh:
            body = fh.read()
    else:
        body = args.text

    timeout = max(1.0, min(float(args.timeout), MAX_TIMEOUT))
    try:
        password = read_secret_file(args.password_file)
    except Exception as exc:
        sys.stderr.write("读口令文件失败：%s\n" % exc)
        return 2

    excluded = {m.strip() for m in args.exclude.split(",") if m.strip()}
    excluded.add(args.user)  # 不给自己发

    if args.all:
        try:
            client = NatsClient(host=args.nats_host, port=args.nats_port,
                                user=args.user, password=password)
            client.connect()
            members = discover_online(client, args.roster_seconds)
            client.close()
        except Exception as exc:
            sys.stderr.write("连接中枢/取名单失败：%s\n" % exc)
            return 2
        members = [m for m in members if m not in excluded]
        if not members:
            sys.stderr.write("窗口 %.0fs 内没有扫到任何在线成员\n" % args.roster_seconds)
            return 3
    else:
        members = [m.strip() for m in (args.members or "").split(",") if m.strip()]
        members = [m for m in members if m not in excluded]
        if not members:
            sys.stderr.write("必须给 --members 或 --all\n")
            return 2

    envelope = {"text": body, "timeout_s": timeout, "subject": args.subject}
    if args.session_id:
        envelope["session_id"] = args.session_id
    payload = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    if args.dry_run:
        print("【dry-run】不发任何消息。主题：%s　目标 %d 个：%s" % (
            args.subject, len(members), ",".join(members)))
        print("正文预览（前 200 字）：%s" % body[:200].replace("\n", " "))
        return 0

    print("群发主题：%s　目标 %d 个：%s%s" % (
        args.subject, len(members), ",".join(members),
        "　（只投递不等回应）" if args.no_wait else "　（并行度 %d，单成员超时 %.0fs）" % (args.concurrency, timeout)))

    t0 = time.time()
    if args.no_wait:
        args_list = [(args.nats_host, args.nats_port, args.user, password, m, payload) for m in members]
        results = run_pool(call_one_nowait, args_list, args.concurrency)
    else:
        args_list = [(args.nats_host, args.nats_port, args.user, password, m, payload, timeout) for m in members]
        results = run_pool(call_one, args_list, args.concurrency)
    total = round(time.time() - t0, 1)

    ok = [r for r in results if r and r.get("ok")]
    bad = [r for r in results if not (r and r.get("ok"))]
    for r in results:
        mark = "✅" if r.get("ok") else "❌"
        rep = (r.get("reply") or r.get("error") or "")
        rep = rep.replace("\n", " ")
        if len(rep) > 160:
            rep = rep[:160] + "…"
        print("  %s %-12s %6.1fs  %s" % (mark, r.get("member"), r.get("elapsed", 0.0), rep))
    print("汇总：%d/%d 回应，用时 %.1fs" % (len(ok), len(members), total))

    payload_out = {
        "subject": args.subject, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(members), "ok": len(ok), "no_wait": bool(args.no_wait),
        "elapsed": total, "results": results,
    }
    if args.json:
        print(json.dumps(payload_out, ensure_ascii=False, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload_out, fh, ensure_ascii=False, indent=2)
        print("结果已落：%s" % args.out)

    if args.no_wait:
        return 0 if not bad else 1
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
