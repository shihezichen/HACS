# -*- coding: utf-8 -*-
"""刀25 的「旧侧」**独立第二实现** —— 成员入网只读对账的另一条算法路径。

它为什么存在
------------
正件 `tools/nats_membership_audit.py` 说「现网就是这样」。**光它自己说不算数** ——
一个解析器可以既算错又自洽。所以本件从零再写一遍同一套判定，走**另一条路**：

  · conf 解析：正件是「正则定位 + 引号感知的括号配平切片」；本件是**逐行状态机 +
    顶层逗号切词**（先按行认出 `users = [` 区段，再把每条 entry 按 depth-0 的逗号切成
    `key: value` 词，最后取词）。
  · 时长解析：正件是一条正则 `finditer` 逐段累加；本件是**手写扫描器**（数字段 / 字母段
    交替切开，查表累加），一个正则都不用。
  · 角色模板：**本件不 import `gen_nats_conf`**，自己按 L5 契约文档的口径拼那串期望权限。
    于是「现网权限串 == 期望权限串」这件事被两份互不相识的模板同时确认 ——
    任何一份写歪，装置立刻不一致。
    （附带一条：`gen_nats_conf.HB_DELEGATION` 现为空表，两份才对得上。将来若又出现
    「一个凭据代表多成员」的拓扑，两侧会立刻分家 —— 那正是该被看见的信号，不是装置坏了。）
  · 采集：只实现**夹具**一路（装置离线跑）。真机采集不属于判定逻辑，不需要第二实现。

本件**不 import 正件**、不 import 任何运行期件，只用标准库。
输出形状（键名、顺序、排序规则）与正件逐字节相同 —— 这就是等价装置逐格比的东西。
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os

#: 与正件同一份冻结契约（形状常量，不是实现）
SCHEMA = "l5-membership-audit/1"
V2_PORTS = (9930, 9931, 9932)
STALE_IDLE_SECONDS = 3600
CHECKS = (
    ("M1", "hub_user_matrix"),
    ("M2", "perm_template"),
    ("M3", "cred_landing"),
    ("M4", "roster_coverage"),
    ("M5", "live_subset"),
    ("M6", "legacy_and_routes"),
)
CHECK_KEYS = tuple(k for k, _ in CHECKS)
CRED_GLOBS = ("nats-*.pass", "*.pass")
LISTEN_KEYS = ("listen", "port", "http", "https")

_UNITS = {"ns": 1e-9, "µs": 1e-6, "us": 1e-6, "ms": 1e-3,
          "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


# ============================================================ 角色模板（自己拼，不 import L1）

def perm_template(user):
    """期望权限串 —— 按 L5 契约文档 §「角色模板」的口径**自己拼**。

    形状：`publish` = 通用四条 + 「只许发自己的」心跳/探测两条；`subscribe` = 通用四条。
    """
    publish = ['"a2a.>"',
               '"team.hb.' + user + '"',
               '"team.probe.' + user + '"',
               '"bb.>"',
               '"$JS.API.>"',
               '"_INBOX.>"']
    subscribe = ['"a2a.>"', '"team.>"', '"bb.>"', '"_INBOX.>"']
    return "publish: [" + ", ".join(publish) + "], subscribe: [" + ", ".join(subscribe) + "]"


# ============================================================ 手写扫描器（不用正则）

def parse_duration(text):
    """人读时长串 → 秒。切成「数字段 / 单位段」交替扫描；任何一段不认识就返回 `None`。"""
    s = str(text or "").strip()
    if not s:
        return None
    total, i, n, seen = 0.0, 0, len(s), False
    while i < n:
        start = i
        while i < n and (s[i].isdigit() or s[i] == "."):
            i += 1
        if i == start:
            return None
        try:
            value = float(s[start:i])
        except ValueError:
            return None
        start = i
        while i < n and not (s[i].isdigit() or s[i] == "."):
            i += 1
        unit = s[start:i]
        if unit not in _UNITS:
            return None
        total += value * _UNITS[unit]
        seen = True
    return total if seen else None


def _split_top_commas(text):
    """按 **depth-0 的逗号**切词（`[]` / `{}` 内的逗号不算，引号内的括号不算）。"""
    parts, buf, depth, i, n = [], [], 0, 0, len(text)
    quote = ""
    while i < n:
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _kv(word):
    """`key: value` → `(key, value)`；切不出来返回 `(None, None)`。"""
    if ":" not in word:
        return None, None
    key, _sep, value = word.partition(":")
    return key.strip(), value.strip()


def _unquote(value):
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_conf(text):
    """`nats.conf` → 结构化事实。**逐行状态机**：先认出 `users = [` 区段，再逐条切词。"""
    users, ports, cluster_name = [], [], ""
    has_permissions = False
    has_users_block = False
    in_users, in_cluster, cluster_depth = False, False, 0

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if in_users:
            if line.startswith("]"):
                in_users = False
                continue
            entry = line
            if entry.endswith(","):
                entry = entry[:-1].rstrip()
            if not (entry.startswith("{") and entry.endswith("}")):
                continue
            fields = {}
            for word in _split_top_commas(entry[1:-1]):
                key, value = _kv(word)
                if key is not None:
                    fields[key] = value
            if "user" not in fields:
                continue
            perm = None
            if "permissions" in fields:
                body = fields["permissions"].strip()
                if body.startswith("{") and body.endswith("}"):
                    body = body[1:-1]
                perm = body.strip()
                has_permissions = True
            users.append((_unquote(fields["user"]),
                          _unquote(fields.get("password", "")), perm))
            continue

        key, value = _kv(line)
        if key == "users" or (key is None and line.startswith("users")):
            pass
        if line.startswith("users") and "[" in line:
            in_users, has_users_block = True, True
            continue
        if "permissions" in line:
            has_permissions = True

        if line.startswith("cluster") and "{" in line:
            in_cluster, cluster_depth = True, line.count("{") - line.count("}")
            continue
        if in_cluster:
            cluster_depth += line.count("{") - line.count("}")
            if key == "name" and not cluster_name:
                cluster_name = _unquote(value)
            if cluster_depth <= 0:
                in_cluster = False

        if key in LISTEN_KEYS and value:
            token = _unquote(value)
            if token.endswith(","):
                token = token[:-1]
            tail = token.split(":")[-1].strip()
            if tail.isdigit():
                ports.append(int(tail))

    return {"users": users,
            "listen_ports": sorted(set(ports)),
            "cluster_name": cluster_name,
            "has_permissions": has_permissions,
            "has_users_block": has_users_block}


# ============================================================ 夹具采集（本件只实现离线这一路）

def _sha(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _member_of(filename):
    if filename.startswith("nats-") and filename.endswith(".pass"):
        return filename[len("nats-"):-len(".pass")]
    if filename.endswith(".pass"):
        return filename[:-len(".pass")]
    return None


def collect_fixtures(root):
    art = {"hubs": [], "nodes": [], "conf": {}, "connz": {}, "routez": {},
           "creds": {}, "errors": []}

    def err(node, what, why):
        art["errors"].append({"kind": "collect_error", "node": node,
                              "what": what, "why": str(why)[:200]})

    if not os.path.isdir(root):
        err("", "夹具目录", "夹具目录不存在：%s" % os.path.basename(root.rstrip("/\\")))
        return art

    nodes, hubs = [], []
    case_path = os.path.join(root, "case.json")
    if os.path.isfile(case_path):
        try:
            case = json.loads(io.open(case_path, encoding="utf-8").read()) or {}
            nodes = list(case.get("nodes") or [])
            hubs = list(case.get("hubs") or [])
        except ValueError as exc:
            err("", "case.json", "夹具清单不是合法 JSON：%s" % exc)
    if not nodes:
        nodes = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    if not hubs:
        hubs = [n for n in nodes if os.path.isfile(os.path.join(root, n, "connz.json"))]

    art["nodes"], art["hubs"] = sorted(nodes), sorted(hubs)
    for name in art["nodes"]:
        base = os.path.join(root, name)
        art["conf"][name] = None
        art["creds"][name] = {}
        conf_path = os.path.join(base, "nats.conf")
        if os.path.isfile(conf_path):
            text = io.open(conf_path, encoding="utf-8", errors="replace").read()
            if not text.strip():
                err(name, "nats.conf", "配置文件存在但内容为空")
            else:
                art["conf"][name] = text
        cred_dir = os.path.join(base, "creds")
        if os.path.isdir(cred_dir):
            for filename in sorted(os.listdir(cred_dir)):
                if not any(fnmatch.fnmatch(filename, g) for g in CRED_GLOBS):
                    continue
                body = io.open(os.path.join(cred_dir, filename),
                               encoding="utf-8", errors="replace").read()
                art["creds"][name][filename] = _sha(body.strip())
        for what in ("connz", "routez"):
            art[what][name] = None
            if name not in art["hubs"]:
                continue
            path = os.path.join(base, what + ".json")
            if not os.path.isfile(path):
                err(name, what, "中枢夹具缺 %s.json" % what)
                continue
            try:
                art[what][name] = json.loads(
                    io.open(path, encoding="utf-8", errors="replace").read())
            except ValueError as exc:
                err(name, what, "监控口夹具不是 JSON：%s" % exc)

    for hub in art["hubs"]:
        if art["conf"].get(hub) is None \
                and not any(e["node"] == hub and e["what"] == "nats.conf" for e in art["errors"]):
            err(hub, "nats.conf", "中枢上没有 nats.conf")
        if art["conf"].get(hub) and not parse_conf(art["conf"][hub])["users"]:
            err(hub, "nats.conf", "中枢配置里解析不出任何用户")
    return art


# ============================================================ 六项检查（第二条实现路径）

def _live(connz):
    got = {"authorized_user": [], "name_prefix": []}
    for conn in ((connz or {}).get("connections") or []):
        if not isinstance(conn, dict):
            continue
        who = str(conn.get("authorized_user") or "").strip()
        if who and who not in got["authorized_user"]:
            got["authorized_user"].append(who)
        nm = str(conn.get("name") or "").strip()
        if nm.startswith("teamd-") and len(nm) > len("teamd-"):
            member = nm[len("teamd-"):]
            if member not in got["name_prefix"]:
                got["name_prefix"].append(member)
    return {k: sorted(v) for k, v in got.items()}


def _sort(items):
    return sorted(items, key=lambda it: json.dumps(it, sort_keys=True, ensure_ascii=False))


def analyze(artifacts, perm_fn=None):
    """与正件同形状的报告。`perm_fn` 缺省用本件自己的模板（装置也可反向注入做交叉验证）。"""
    perm_fn = perm_fn or perm_template
    hubs = list(artifacts.get("hubs") or [])
    nodes = list(artifacts.get("nodes") or [])
    confs = artifacts.get("conf") or {}
    connz = artifacts.get("connz") or {}
    routez = artifacts.get("routez") or {}
    creds = artifacts.get("creds") or {}

    parsed = {}
    for n in nodes:
        if confs.get(n) is not None:
            parsed[n] = parse_conf(confs[n])

    hub_users, ref_hub = {}, ""
    for h in hubs:
        if h in parsed:
            table = {}
            for user, password, _perm in parsed[h]["users"]:
                table[user] = password
            hub_users[h] = table
            if not ref_hub:
                ref_hub = h
    roster_pw = dict(hub_users.get(ref_hub) or {})
    names = set()
    for table in hub_users.values():
        names |= set(table)
    roster = sorted(names)

    found = {k: [] for k in CHECK_KEYS}

    # M1
    if ref_hub:
        ref = set(hub_users[ref_hub].items())
        for h in hubs:
            if h == ref_hub or h not in hub_users:
                continue
            other = set(hub_users[h].items())
            for user, _pw in sorted(ref - other):
                found["M1"].append({
                    "kind": "hub_matrix_missing", "member": user, "hub": h, "ref_hub": ref_hub,
                    "why": "口令与基准中枢不同" if user in hub_users[h]
                           else "该成员不在此中枢名册里"})
            for user, _pw in sorted(other - ref):
                found["M1"].append({
                    "kind": "hub_matrix_extra", "member": user, "hub": h, "ref_hub": ref_hub,
                    "why": "口令与基准中枢不同" if user in hub_users[ref_hub]
                           else "该成员不在基准中枢名册里"})

    # M2
    for h in hubs:
        if h not in parsed:
            continue
        for user, _pw, perm in parsed[h]["users"]:
            want = perm_fn(user)
            if perm != want:
                found["M2"].append({"kind": "perm_drift", "hub": h, "member": user,
                                    "expected": want, "actual": perm})

    # M3
    for node in nodes:
        table = creds.get(node) or {}
        for filename in sorted(table):
            member = _member_of(filename)
            if member is None:
                continue
            digest = table[filename]
            row = {"member": member, "node": node,
                   "location": node + ":" + filename, "sha256_8": digest[:8]}
            if member not in roster_pw:
                row["kind"] = "cred_orphan"
                row["why"] = "该机上有凭据文件，名册里没有这个成员"
                found["M3"].append(row)
            elif _sha(roster_pw[member]) != digest:
                row["kind"] = "cred_mismatch"
                row["why"] = "凭据文件内容与名册口令不一致"
                found["M3"].append(row)

    # M4
    with_file = set()
    for node in nodes:
        for filename in (creds.get(node) or {}):
            member = _member_of(filename)
            if member is not None:
                with_file.add(member)
    sources = {"authorized_user": set(), "name_prefix": set()}
    for h in hubs:
        got = _live(connz.get(h))
        for key in sources:
            sources[key] |= set(got[key])
    with_live = sources["authorized_user"] | sources["name_prefix"]
    for member in roster:
        if member not in with_file and member not in with_live:
            found["M4"].append({"kind": "zombie_credential", "member": member,
                                "why": "名册里有，但全网既无凭据文件也无活连接"})

    # M5
    for h in hubs:
        got = _live(connz.get(h))
        for key in sorted(got):
            for member in got[key]:
                if member not in roster_pw and member not in names:
                    found["M5"].append({"kind": "live_outside_roster", "hub": h,
                                        "member": member, "source": key})

    # M6a
    for node in nodes:
        info = parsed.get(node)
        if info is None:
            continue
        if not info["users"] and not info["listen_ports"]:
            continue
        bad = [p for p in info["listen_ports"] if p not in V2_PORTS]
        if info["has_permissions"] and not bad:
            continue
        why = []
        if not info["has_permissions"]:
            why.append("conf 里没有 permissions 块（= 全权）")
        if bad:
            why.append("监听端口 %s 不属于 v2 端口体系 %s"
                       % (",".join([str(p) for p in bad]),
                          "/".join([str(p) for p in V2_PORTS])))
        found["M6"].append({"kind": "legacy_hub", "node": node, "bad_ports": bad,
                            "has_permissions": info["has_permissions"],
                            "user_count": len(info["users"]),
                            "why": "；".join(why)})

    # M6b
    clusters = {}
    for h in hubs:
        rz = routez.get(h)
        if not isinstance(rz, dict):
            continue
        routes = [r for r in (rz.get("routes") or []) if isinstance(r, dict)]
        stale = 0
        for route in routes:
            secs = parse_duration(route.get("idle"))
            if secs is not None and secs > STALE_IDLE_SECONDS:
                stale += 1
        num = rz.get("num_routes")
        if not isinstance(num, int):
            num = len(routes)
        name = (parsed.get(h) or {}).get("cluster_name") or ""
        clusters.setdefault(name, []).append(
            {"hub": h, "num_routes": num, "stale_routes": stale})
    for name in sorted(clusters):
        rows = sorted(clusters[name], key=lambda r: r["hub"])
        flagged = False
        for row in rows:
            if row["num_routes"] > 2 or row["stale_routes"] > 0:
                flagged = True
        if not flagged:
            continue
        found["M6"].append({
            "kind": "duplicate_route", "cluster": name, "hubs": rows,
            "stale_threshold_seconds": STALE_IDLE_SECONDS,
            "why": "两台中枢之间应当只有 1 条活路由；条目数 >2 或存在 idle 超阈值的陈旧条目"})

    found = {k: _sort(v) for k, v in found.items()}
    counts = {}
    for k in CHECK_KEYS:
        counts[k] = len(found[k])
    errors = _sort(list(artifacts.get("errors") or []))
    return {
        "schema": SCHEMA,
        "node_list": sorted(nodes),
        "hub_list": sorted(hubs),
        "roster": roster,
        "check_names": dict(CHECKS),
        "live_sources": {k: sorted(v) for k, v in sources.items()},
        "collect_errors": errors,
        "findings": found,
        "counts": counts,
        "ok": (not errors) and (sum(counts.values()) == 0),
    }


def report_of(root, perm_fn=None):
    """夹具目录 → 报告（装置的旧侧入口）。"""
    return analyze(collect_fixtures(root), perm_fn)
