#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T 工具面：**中枢侧凭据镜像通道**（阶段5 第六刀 · 刀30）。

它解决什么
----------
刀27 已经机械证明了这个式子成立::

    L0 名册档案（谁是成员）  +  各成员自己那台机上的凭据文件   ⇒   中枢 nats.conf 的用户区

但「让生成器按档案自动写配置」一直卡在一个**已量出的缺口**上：8 位成员的凭据散在 4 台机
（thinkbook 5 / lobster / top001 / quantum 各 1），而 `gen_nats_conf.py` 是**在中枢机上跑的** ——
它拿不到全员口令，所以只能从旧 conf 里把口令**继承**下来（配置自证配置）。

本件就是那条缺口上的桥：把「档案 + 各机凭据真值」**单向镜像**到两台中枢的::

    <node_profile.nats_conf_dir_posix()>/creds/<member>.pass      目录 0700 / 文件 0600 / owner=root

镜像建成后，生成器（刀30 同步改造）的口令来源就从「旧 conf 继承」切到「镜像文件」——
档案成为成员集合与顺序的唯一真源，口令真值仍然只来自各成员自己那台机。

代价（**明写，不藏**）
----------------------
两台中枢从此**持有全员口令的明文副本**。这是用户 2026-09-27 明确批准的取舍：
换来的是「成员集合可被档案单向决定」。缓解手段：0700/0600/只 root、不入 git、不进日志、
可一键 `--rollback` 删除（镜像随时可从各机真值重建 —— 它是**派生物**，不是真源）。

口令永不落盘、永不进日志
------------------------
  · 采集：**复用** `tools/roster_derive_conf.py` 的既有实现（`collect_live()` / `read_file()` /
    `run_remote()` / `cred_path()`）—— 不另写一套 ssh 取凭据逻辑（两处实现 = 两份口径）。
  · 写入：`ssh <host> 'umask 077; mkdir -p …; cat > …'` —— 口令走 **stdin**，
    **绝不进 argv**（进了 argv 就进了远端 `ps`），**绝不落本机临时文件**。
  · 出口：stdout / stderr / `--json` 里只允许出现 `成员名 + sha256[:12] + mode`。
    `analyze()` 是**纯函数**，入参里根本没有明文 —— 只有 `{成员: sha256[:12]}`，
    所以报告里不可能有口令（不是「记得别打印」，是**结构上拿不到**）。

子命令
------
    python tools/roster_creds_mirror.py [--check]     只读盘点（默认）
    python tools/roster_creds_mirror.py --apply       写入镜像，随后自跑一次 --check 定退出码
    python tools/roster_creds_mirror.py --rollback    删除两中枢的 creds/ 目录（唯一的回滚动作）

退出码
------
    0  两中枢镜像全部在位、内容与源侧一致、mode 合规
    1  有缺失 / 内容漂移 / mode 或 owner 不符
    2  采集失败（ssh 不通 / 算不出路径 / 名册为空 / 探针输出读不懂）

采集失败**绝不退化成 0 或 1** —— 「没量到」和「量到了、一致/不一致」是三件事。

红线（本件自我约束）
--------------------
  ① 不碰 `nats.conf`（那是生成器的活）  ② 不重启任何单元  ③ 出口只有 sha256[:12]
  ④ 幂等（重复 `--apply` 结果不变、不报错）  ⑤ 只写 root 路径下的 `creds/`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import roster_derive_conf as rdc                       # noqa: E402 —— 采集面的唯一出处（不另写一套）

import member_roster                                   # noqa: E402 —— L0 名册档案（成员真源；rdc 已把 agent-team 注入 path）

EXIT_OK, EXIT_DRIFT, EXIT_COLLECT = 0, 1, 2

#: 输出契约版本（**顶层键名冻结**；改形状必须进版本号）
SCHEMA = "l5-creds-mirror/1"

#: 镜像目录名（挂在 `<conf_dir>/` 下）
MIRROR_SUBDIR = "creds"
#: 期望的目录 / 文件 mode 与属主（**不许放宽**）
DIR_MODE = "700"
FILE_MODE = "600"
OWNER = "root"

#: 探针输出的行标记（远端 shell 只发只读命令：stat / sha256sum / cut）
DIR_MARK = "DIR "
FILE_MARK = "M "
#: 探针里「这一项没有」的占位（不用空串 —— 空串会被 split 吃掉）
NONE_MARK = "-"

#: 逐项判定的状态名（**冻结**：装置按这些名字断言）
ST_OK = "ok"
ST_MISSING = "missing"
ST_DRIFT = "drift"
ST_MODE = "mode"
ST_OWNER = "owner"


# ============================================================ 路径（真值全部来自 L0 档案 / 部署表）

def mirror_dir_of(node, target):
    """该中枢上镜像目录的绝对路径；算不出返回空串（**不猜**）。"""
    base = rdc.conf_dir_of(node, target)
    return (base.rstrip("/\\") + "/" + MIRROR_SUBDIR) if base else ""


def mirror_path_of(node, target, member):
    """该中枢上某成员镜像文件的绝对路径；算不出返回空串。"""
    base = mirror_dir_of(node, target)
    return (base + "/%s.pass" % member) if base else ""


# ============================================================ 远端命令（**口令一个字节都不在这里**）

def write_cmd(dirpath, member):
    """写入一份镜像的远端命令 —— **命令串里没有口令**，口令从 stdin 灌。

    `umask 077` 保证新建的目录/文件天生就是 0700/0600；后面两条 `chmod` 是为了**幂等**
    （文件已存在时 umask 管不着它的旧 mode）。全程不落本机临时文件、不进 argv。
    """
    return ("umask 077; mkdir -p %(d)s && chmod %(dm)s %(d)s && cat > %(d)s/%(m)s.pass "
            "&& chmod %(fm)s %(d)s/%(m)s.pass"
            % {"d": dirpath, "m": member, "dm": DIR_MODE, "fm": FILE_MODE})


def probe_cmd(dirpath, members):
    """只读盘点的远端命令：逐行吐 `DIR <mode> <owner>` 与 `M <名> <mode> <owner> <sha12>`。

    只发 `stat` / `sha256sum` / `cut` —— 一个写操作都没有，**口令不出远端**（只出摘要）。
    """
    names = " ".join(members)
    return ("d=%(d)s; if [ -d $d ]; then echo %(DM)s$(stat -c %%a $d) $(stat -c %%U $d); "
            "else echo %(DM)s%(N)s %(N)s; fi; "
            "for m in %(names)s; do f=$d/$m.pass; if [ -f $f ]; then "
            "echo %(FM)s$m $(stat -c %%a $f) $(stat -c %%U $f) $(sha256sum $f | cut -c1-12); "
            "else echo %(FM)s$m %(N)s %(N)s %(N)s; fi; done"
            % {"d": dirpath, "names": names, "DM": DIR_MARK, "FM": FILE_MARK, "N": NONE_MARK})


def rollback_cmd(dirpath):
    """回滚：删掉整个镜像目录（镜像是派生物，删了可重建）。"""
    return "rm -rf %s && echo GONE" % dirpath


def parse_probe(out, members):
    """探针 stdout → `{"dir": {...}, "files": {成员: {...}}}`；读不懂返回 `(None, 为什么)`。

    读不懂**不许**退化成「没有」—— 那会把采集失败伪装成「镜像缺件」。
    """
    dir_row, files = None, {}
    for line in (out or "").split("\n"):
        line = line.strip()
        if line.startswith(DIR_MARK):
            parts = line[len(DIR_MARK):].split()
            if len(parts) < 2:
                return None, "探针的 DIR 行读不懂"
            dir_row = {"present": parts[0] != NONE_MARK,
                       "mode": parts[0], "owner": parts[1]}
        elif line.startswith(FILE_MARK):
            parts = line[len(FILE_MARK):].split()
            if len(parts) < 4:
                return None, "探针的 M 行读不懂"
            name, mode, owner, sha12 = parts[0], parts[1], parts[2], parts[3]
            files[name] = {"present": sha12 != NONE_MARK,
                           "mode": mode, "owner": owner,
                           "sha12": "" if sha12 == NONE_MARK else sha12}
    if dir_row is None:
        return None, "探针没有回传 DIR 行"
    missing_rows = [m for m in members if m not in files]
    if missing_rows:
        return None, "探针漏了 %d 个成员的行" % len(missing_rows)
    return {"dir": dir_row, "files": files}, ""


# ============================================================ analyze —— 纯函数（入参里没有明文）

def analyze(expected, observed, hubs, errors=None):
    """`{成员: sha256[:12]}` + 各中枢观测 → 报告（纯函数：无 IO、无 time、无随机）。

    `expected` = `{成员名: sha12}`（**顺序由 `members` 参数之外的调用方负责**，这里只比集合与值）
    `observed` = `{中枢名: {"dir": {...}, "files": {成员: {...}}} | None}`（`None` = 这台没量到）
    `errors`   = `[{"kind","node","what","why"}]`

    **口令出口闸**：本函数的入参里根本没有明文 —— 只有 sha256[:12]。
    """
    errors = list(errors or [])
    members = sorted(expected.keys())
    per_hub = []
    for hub in sorted(hubs):
        obs = (observed or {}).get(hub)
        row = {"hub": hub, "dir_present": False, "dir_mode": "", "dir_owner": "",
               "dir_ok": False, "members": [], "ok": False, "why": ""}
        if obs is None:
            row["why"] = "没有量到这台中枢的镜像目录"
            per_hub.append(row)
            continue
        d = obs.get("dir") or {}
        row["dir_present"] = bool(d.get("present"))
        row["dir_mode"] = d.get("mode") or ""
        row["dir_owner"] = d.get("owner") or ""
        row["dir_ok"] = (row["dir_present"] and row["dir_mode"] == DIR_MODE
                         and row["dir_owner"] == OWNER)
        files = obs.get("files") or {}
        for member in members:
            f = files.get(member) or {}
            want = expected[member]
            got = f.get("sha12") or ""
            item = {"member": member, "present": bool(f.get("present")),
                    "expected_sha12": want, "actual_sha12": got,
                    "mode": f.get("mode") or "", "owner": f.get("owner") or "",
                    "status": ST_OK}
            if not item["present"]:
                item["status"] = ST_MISSING
            elif got != want:
                item["status"] = ST_DRIFT
            elif item["mode"] != FILE_MODE:
                item["status"] = ST_MODE
            elif item["owner"] != OWNER:
                item["status"] = ST_OWNER
            row["members"].append(item)
        bad = [it for it in row["members"] if it["status"] != ST_OK]
        row["ok"] = row["dir_ok"] and not bad
        if not row["ok"]:
            bits = []
            if not row["dir_ok"]:
                bits.append("目录不合规（present=%s mode=%s owner=%s）"
                            % (row["dir_present"], row["dir_mode"] or "—",
                               row["dir_owner"] or "—"))
            if bad:
                bits.append("%d 个成员不合格" % len(bad))
            row["why"] = "；".join(bits)
        per_hub.append(row)

    collect_errors = sorted(errors,
                            key=lambda it: json.dumps(it, sort_keys=True, ensure_ascii=False))
    return {
        "schema": SCHEMA,
        "hub_list": sorted(hubs),
        "roster": members,
        "per_hub": per_hub,
        "collect_errors": collect_errors,
        "ok": bool(hubs) and bool(members) and (not collect_errors)
              and all(r["ok"] for r in per_hub),
    }


def exit_code(report):
    """报告 → 退出码。采集失败优先（**绝不退化**为 0/1）。"""
    if report.get("collect_errors"):
        return EXIT_COLLECT
    return EXIT_OK if report.get("ok") else EXIT_DRIFT


# ============================================================ 采集面（真机；`--check` 只读）

def collect_source(roster_rows, targets, timeout=40.0):
    """各机上的凭据真值 → `({成员: sha12}, errors)`。**明文只在这一层的局部变量里走一趟。**

    采集**复用** `roster_derive_conf.collect_live()` —— 不另写一套 ssh 取凭据逻辑。
    它同时会读两台中枢的 `nats.conf`（只读），读不到即视为采集失败：连 conf 都读不到的中枢，
    后面的镜像结论本来也不作数。
    """
    collected = rdc.collect_live(list(rdc.HUBS_DEFAULT), roster_rows, targets, timeout=timeout)
    creds = collected.get("creds") or {}
    errors = list(collected.get("errors") or [])
    #: 缺哪个成员的凭据，`collect_live()` 已经记过一条 collect_error（rc=2 的来源），这里不重复记
    expected = {m: rdc.sha256_12(creds[m]) for m in creds}
    return expected, errors, creds


def probe_hubs(hubs, members, targets, timeout=40.0):
    """逐中枢只读盘点 → `(observed, errors)`。"""
    observed, errors = {}, []

    def err(node, what, why):
        errors.append({"kind": "collect_error", "node": node, "what": what, "why": str(why)[:200]})

    for hub in sorted(hubs):
        observed[hub] = None
        target = targets.get(hub)
        if not target:
            err(hub, "部署表", "内部真值表里没有这个节点，拿不到 ssh 目标")
            continue
        dirpath = mirror_dir_of(hub, target)
        if not dirpath:
            err(hub, "镜像目录", "算不出该机的 NATS 配置目录")
            continue
        rc, body, why = rdc.run_remote(target, probe_cmd(dirpath, members), timeout)
        if rc is None:
            err(hub, "镜像盘点", why or "采集失败")
            continue
        parsed, why = parse_probe(body, members)
        if parsed is None:
            err(hub, "镜像盘点", why)
            continue
        observed[hub] = parsed
    return observed, errors


def check(roster_rows, targets, timeout=40.0):
    """只读盘点一轮 → 报告。`--apply` 跑完也走这条，结论由它定。"""
    expected, errors, _creds = collect_source(roster_rows, targets, timeout=timeout)
    observed, probe_errors = probe_hubs(rdc.HUBS_DEFAULT, sorted(expected.keys()),
                                        targets, timeout=timeout)
    return analyze(expected, observed, rdc.HUBS_DEFAULT, errors + probe_errors)


# ============================================================ 写入面（口令走 stdin）

_ENV_RX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def expand(value):
    """展开 `${VAR:默认值}` —— 与 `deploy.py` / `roster_derive_conf.py` 同一口径。

    为什么不直接借 `roster_derive_conf` 那个同名函数：它是**私有面**（前缀下划线），
    跨件去碰别人的私有名是分层门禁检查5 认的违规。这五行是**口径复制**，不是第二套采集逻辑
    （契约禁的是「另写一套 ssh 取凭据逻辑」—— 采集仍然全走 `rdc.collect_live()`）。
    """
    if not isinstance(value, str):
        return value

    def sub(m):
        return os.environ.get(m.group(1)) or (m.group(2) or "")
    return _ENV_RX.sub(sub, value)


def _ssh_argv(target):
    """ssh 的固定 argv 前缀（与 `roster_derive_conf.run_remote` 同一口径）。"""
    key = expand(target.get("ssh_key") or "")
    argv = ["ssh"]
    if key:
        argv += ["-i", key]
    argv += ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=accept-new",
             expand(target["ssh_target"])]
    return argv


def push_one(target, dirpath, member, password, timeout=40.0):
    """把一份口令灌到远端 `<dir>/<member>.pass` → `(ok, 说明)`。

    口令**只经 stdin**：不进 argv（远端 `ps` 看不到）、不落本机临时文件、不进日志。
    `transport=local` 的节点走本机 `os.open(..., 0o600)`（本机是 Windows，posix 的 `cat` 不存在）。
    """
    if (target.get("transport") or "") == "local":
        try:
            os.makedirs(dirpath, exist_ok=True)
            path = dirpath + "/%s.pass" % member
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write((password or "").encode("utf-8"))
            return True, ""
        except OSError as exc:
            return False, "写不了：%s" % exc
    cmd = rdc.REMOTE_PATH + write_cmd(dirpath, member)
    try:
        proc = subprocess.run(_ssh_argv(target) + [cmd],
                              input=(password or "").encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "超时 %ds" % int(timeout)
    except OSError as exc:
        return False, "ssh 起不来：%s" % exc
    if proc.returncode != 0:
        return False, ((proc.stderr or b"").decode("utf-8", "replace").strip() or
                       "远端退出码 %d" % proc.returncode)[-200:]
    return True, ""


def apply_mirror(roster_rows, targets, timeout=40.0):
    """把镜像推到两台中枢 → `(写入结果行, errors)`。**幂等**：重复跑结果不变、不报错。"""
    expected, errors, creds = collect_source(roster_rows, targets, timeout=timeout)
    rows = []
    if errors:
        return rows, errors                              # 源侧没采全 —— 不许写半套镜像
    for hub in sorted(rdc.HUBS_DEFAULT):
        target = targets.get(hub)
        if not target:
            errors.append({"kind": "collect_error", "node": hub, "what": "部署表",
                           "why": "内部真值表里没有这个节点，拿不到 ssh 目标"})
            continue
        dirpath = mirror_dir_of(hub, target)
        if not dirpath:
            errors.append({"kind": "collect_error", "node": hub, "what": "镜像目录",
                           "why": "算不出该机的 NATS 配置目录"})
            continue
        for member, _role, _node, _kind in roster_rows:
            if member not in creds:
                continue
            ok, why = push_one(target, dirpath, member, creds[member], timeout)
            rows.append({"hub": hub, "member": member, "sha12": expected[member],
                         "mode": FILE_MODE, "ok": ok, "why": why})
            if not ok:
                errors.append({"kind": "collect_error", "node": hub,
                               "what": "写镜像 %s" % member, "why": why})
    return rows, errors


def rollback(targets, timeout=40.0):
    """删除两中枢的 `creds/` → `(被删路径行, errors)`。**唯一的回滚动作**。"""
    rows, errors = [], []
    for hub in sorted(rdc.HUBS_DEFAULT):
        target = targets.get(hub)
        if not target:
            errors.append({"kind": "collect_error", "node": hub, "what": "部署表",
                           "why": "内部真值表里没有这个节点，拿不到 ssh 目标"})
            continue
        dirpath = mirror_dir_of(hub, target)
        if not dirpath:
            errors.append({"kind": "collect_error", "node": hub, "what": "镜像目录",
                           "why": "算不出该机的 NATS 配置目录"})
            continue
        if (target.get("transport") or "") == "local":
            shutil.rmtree(dirpath, ignore_errors=True)
            rows.append({"hub": hub, "path": dirpath, "ok": True})
            continue
        rc, body, why = rdc.run_remote(target, rollback_cmd(dirpath), timeout)
        ok = (rc == 0 and "GONE" in (body or ""))
        rows.append({"hub": hub, "path": dirpath, "ok": ok})
        if not ok:
            errors.append({"kind": "collect_error", "node": hub, "what": "删镜像目录",
                           "why": why or "远端 rm 没有回报成功"})
    return rows, errors


# ============================================================ 人读输出（只许出现名字 / sha12 / mode）

def render(report):
    """每中枢一段；逐成员一行 `名字 sha12 mode`。**不可能带明文** —— 报告里根本没有。"""
    out = ["=" * 78,
           "中枢侧凭据镜像通道 · %s" % report["schema"],
           "中枢 %s ｜ 档案成员 %d 人" % ("、".join(report["hub_list"]) or "—",
                                          len(report["roster"])),
           "=" * 78]
    for row in report["per_hub"]:
        head = "%s %s/  mode=%s owner=%s" % (row["hub"], MIRROR_SUBDIR,
                                             row["dir_mode"] or "—", row["dir_owner"] or "—")
        out.append("%s %s" % ("✓" if row["ok"] else "✗", head))
        for it in row["members"]:
            flag = "✓" if it["status"] == ST_OK else "✗"
            out.append("    %s %-12s %s %s%s"
                       % (flag, it["member"],
                          it["actual_sha12"] or "—", it["mode"] or "—",
                          "" if it["status"] == ST_OK
                          else "   ← %s（期望 %s %s）" % (it["status"], it["expected_sha12"],
                                                          FILE_MODE)))
        if row["why"]:
            out.append("    %s" % row["why"])
    if report["collect_errors"]:
        out.append("")
        out.append("采集失败 %d 条（退出码 2）：" % len(report["collect_errors"]))
        for item in report["collect_errors"]:
            out.append("    ✗ %s / %s：%s" % (item.get("node") or "—",
                                              item.get("what"), item.get("why")))
    out.append("")
    out.append("结论：%s" % ("全部在位且一致" if report["ok"] else "有缺失/漂移/不合规"))
    return "\n".join(out)


# ============================================================ main

def main(argv=None):
    ap = argparse.ArgumentParser(description="中枢侧凭据镜像通道（阶段5 第六刀 · 刀30）")
    ap.add_argument("--check", action="store_true", help="只读盘点（默认）")
    ap.add_argument("--apply", action="store_true", help="写入镜像，随后自跑一次 --check 定退出码")
    ap.add_argument("--rollback", action="store_true", help="删除两中枢的 creds/（唯一的回滚动作）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--timeout", type=float, default=40.0, help="单条远端命令超时（秒）")
    args = ap.parse_args(argv)

    picked = [n for n in ("check", "apply", "rollback") if getattr(args, n)]
    if len(picked) > 1:
        ap.error("--check / --apply / --rollback 只能挑一个")

    targets = rdc.load_targets()
    roster_rows = [tuple(r) for r in member_roster.ROSTER]
    if not roster_rows:
        sys.stderr.write("名册为空，拒绝动作（退出码 2）\n")
        return EXIT_COLLECT

    if args.rollback:
        rows, errors = rollback(targets, timeout=args.timeout)
        for row in rows:
            print("已删 %s：%s" % (row["hub"], row["path"]))
        for item in errors:
            sys.stderr.write("✗ %s / %s：%s\n" % (item.get("node"), item.get("what"),
                                                  item.get("why")))
        return EXIT_COLLECT if errors else EXIT_OK

    if args.apply:
        rows, errors = apply_mirror(roster_rows, targets, timeout=args.timeout)
        for row in rows:
            print("%s %-12s %s %s %s" % (row["hub"], row["member"], row["sha12"],
                                         row["mode"], "ok" if row["ok"] else "FAIL"))
        if errors:
            for item in errors:
                sys.stderr.write("✗ %s / %s：%s\n" % (item.get("node"), item.get("what"),
                                                      item.get("why")))
            return EXIT_COLLECT

    report = check(roster_rows, targets, timeout=args.timeout)
    if args.json:
        sys.stdout.write(rdc.canonical(report))
    else:
        print(render(report))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
