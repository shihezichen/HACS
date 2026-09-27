#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""roster_creds_mirror_equiv.py —— 刀30「中枢侧凭据镜像通道」的等价装置（T 面）。

为什么这把尺子不是「跑一遍看看」
--------------------------------
镜像工具的写入面要 ssh 到两台中枢，装置**不可能**（也不该）在 CI/本机上真跑它。
所以正件被切成两半：**采集/写入**（有 IO）与 **`analyze()` / `parse_probe()` / 命令串构造**
（纯函数）。装置只打后半截，但那恰好是**全部判据所在的地方** ——
「在位 / 缺件 / 漂移 / mode / owner / 目录 / 采集失败」这七种结论没有一条依赖 ssh。

判据（**不放宽**）
------------------
  ① 全在位 ⇒ `ok=True` 且退出码 0
  ② 缺 1 件 / 内容漂移 / 文件 mode 不符 / owner 不符 / 目录缺失 / 目录 mode 不符 ⇒ 退出码 1，且
     **逐成员状态名**必须精确命中（`missing` / `drift` / `mode` / `owner`）——
     不许用「反正是红的」蒙混：状态名本身就是给运维看的诊断。
  ③ 采集失败 ⇒ 退出码 **2**，且**优先于**一切漂移结论（「没量到」不许退化成「量到了、不一致」）
  ④ `parse_probe()` 读不懂探针输出时返回 `None` —— **不许**退化成「镜像缺件」
  ⑤ 写入命令串里**一个字节的口令都没有**（口令只走 stdin）；探针命令串里**没有任何写操作**
  ⑥ 报告（含 `--json` 的规范 JSON 与人读渲染）里**不可能**出现明文口令
  ⑦ sha256[:12] 与装置**自己那份独立实现**（hashlib 直算）逐字符相同

结构体量：约 22 格。`--neg` 三处注毒必须变红（每处都对准一个不同的判据）。
"""
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import roster_creds_mirror as rcm                       # noqa: E402 —— 被测件
import roster_derive_conf as rdc                        # noqa: E402 —— 只用它的路径口径做交叉验证

#: 装置自造的**假**成员与假口令（不是真凭据）。变量名特意避开 pass / secret / token /
#: credential 这类口令词 —— 否则整串字面量会撞 `scan_secrets.py` 的 `literal-assignment`。
FIXTURE_MEMBERS = ("claudecode", "codex", "hermes", "lobster",
                   "openclaw-nb", "piagent", "quantum", "top001")
FIXTURE_JOIN = {m: "fixture-join-%s-0001" % m for m in FIXTURE_MEMBERS}
HUBS = ("lobster", "top001")


def own_sha12(text):
    """**独立第二实现**：装置自己算 sha256 前 12 位，不借正件的函数。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def healthy_expected():
    return {m: own_sha12(FIXTURE_JOIN[m]) for m in FIXTURE_MEMBERS}


def healthy_hub(expected):
    """一台「全都对」的中枢观测。"""
    return {"dir": {"present": True, "mode": rcm.DIR_MODE, "owner": rcm.OWNER},
            "files": {m: {"present": True, "mode": rcm.FILE_MODE,
                          "owner": rcm.OWNER, "sha12": expected[m]}
                      for m in expected}}


def healthy_observed(expected):
    return {hub: healthy_hub(expected) for hub in HUBS}


CELLS = []


def cell(name, ok, detail=""):
    CELLS.append((name, bool(ok), detail))


def status_of(report, hub, member):
    for row in report["per_hub"]:
        if row["hub"] != hub:
            continue
        for it in row["members"]:
            if it["member"] == member:
                return it["status"]
    return "<没有这一行>"


def main():
    neg = "--neg" in sys.argv[1:]

    expected = healthy_expected()
    if neg:
        # 注毒 ①：期望侧改一个字节 —— 「全在位」格必须红（否则说明 sha 根本没在比）
        bad = dict(expected)
        bad["hermes"] = "0" + expected["hermes"][1:]
        expected = bad

    # ---- A 组：全在位 ----
    ok_report = rcm.analyze(expected, healthy_observed(healthy_expected()), HUBS)
    cell("A1 全在位：ok=True", ok_report["ok"] is True,
         json.dumps(ok_report["per_hub"][0]["why"], ensure_ascii=False))
    cell("A2 全在位：退出码 0", rcm.exit_code(ok_report) == rcm.EXIT_OK,
         "rc=%s" % rcm.exit_code(ok_report))
    cell("A3 全在位：两台中枢都进了报告，且按名字排序",
         [r["hub"] for r in ok_report["per_hub"]] == sorted(HUBS))
    cell("A4 全在位：逐成员状态都是 ok",
         all(it["status"] == rcm.ST_OK for r in ok_report["per_hub"] for it in r["members"]))
    cell("A5 sha256[:12] 与装置独立实现逐字符相同",
         all(rdc.sha256_12(FIXTURE_JOIN[m]) == own_sha12(FIXTURE_JOIN[m])
             for m in FIXTURE_MEMBERS))

    base = healthy_expected()

    # ---- B 组：缺 1 件 ----
    obs = healthy_observed(base)
    if not neg:
        obs["top001"]["files"]["quantum"] = {"present": False, "mode": "", "owner": "", "sha12": ""}
    # 注毒 ②（neg）：**不删**那一件，观测保持全在位 —— B1/B2 这两格照旧断言「缺件」，必须红。
    # 若它们仍然绿，说明装置根本没在看 present 这一位。
    rep = rcm.analyze(base, obs, HUBS)
    cell("B1 缺 1 件：退出码 1", rcm.exit_code(rep) == rcm.EXIT_DRIFT,
         "rc=%s" % rcm.exit_code(rep))
    cell("B2 缺 1 件：该成员状态 == missing",
         status_of(rep, "top001", "quantum") == rcm.ST_MISSING,
         status_of(rep, "top001", "quantum"))
    cell("B3 缺 1 件：另一台中枢照旧绿（不许连坐）",
         [r for r in rep["per_hub"] if r["hub"] == "lobster"][0]["ok"] is True)

    # ---- C 组：内容漂移 ----
    obs = healthy_observed(base)
    obs["lobster"]["files"]["codex"]["sha12"] = "deadbeefdead"
    rep = rcm.analyze(base, obs, HUBS)
    cell("C1 内容漂移：退出码 1", rcm.exit_code(rep) == rcm.EXIT_DRIFT)
    cell("C2 内容漂移：该成员状态 == drift",
         status_of(rep, "lobster", "codex") == rcm.ST_DRIFT,
         status_of(rep, "lobster", "codex"))

    # ---- D 组：mode / owner ----
    obs = healthy_observed(base)
    if not neg:
        obs["lobster"]["files"]["hermes"]["mode"] = "644"
    # 注毒 ③（neg）：**不改** mode，D1 照旧断言「状态 == mode」—— 必须红。
    rep = rcm.analyze(base, obs, HUBS)
    cell("D1 文件 mode 644：状态 == mode 且退出码 1",
         status_of(rep, "lobster", "hermes") == rcm.ST_MODE
         and rcm.exit_code(rep) == rcm.EXIT_DRIFT, status_of(rep, "lobster", "hermes"))
    obs = healthy_observed(base)
    obs["lobster"]["files"]["hermes"]["owner"] = "nobody"
    rep = rcm.analyze(base, obs, HUBS)
    cell("D2 文件 owner 非 root：状态 == owner 且退出码 1",
         status_of(rep, "lobster", "hermes") == rcm.ST_OWNER
         and rcm.exit_code(rep) == rcm.EXIT_DRIFT, status_of(rep, "lobster", "hermes"))
    obs = healthy_observed(base)
    obs["top001"]["dir"] = {"present": True, "mode": "755", "owner": rcm.OWNER}
    rep = rcm.analyze(base, obs, HUBS)
    cell("D3 目录 mode 755：dir_ok=False 且退出码 1",
         [r for r in rep["per_hub"] if r["hub"] == "top001"][0]["dir_ok"] is False
         and rcm.exit_code(rep) == rcm.EXIT_DRIFT)

    # ---- E 组：目录缺失 ----
    obs = healthy_observed(base)
    obs["top001"] = {"dir": {"present": False, "mode": rcm.NONE_MARK, "owner": rcm.NONE_MARK},
                     "files": {m: {"present": False, "mode": "", "owner": "", "sha12": ""}
                               for m in base}}
    rep = rcm.analyze(base, obs, HUBS)
    cell("E1 目录缺失：退出码 1 且 ok=False",
         rcm.exit_code(rep) == rcm.EXIT_DRIFT and rep["ok"] is False)
    cell("E2 目录缺失：八个成员全部 missing",
         all(it["status"] == rcm.ST_MISSING
             for r in rep["per_hub"] if r["hub"] == "top001" for it in r["members"]))

    # ---- F 组：采集失败**优先**，绝不退化 ----
    errs = [{"kind": "collect_error", "node": "lobster", "what": "镜像盘点", "why": "ssh 不通"}]
    rep = rcm.analyze(base, {"lobster": None, "top001": healthy_hub(base)}, HUBS, errs)
    cell("F1 采集失败：退出码 2（不是 1）", rcm.exit_code(rep) == rcm.EXIT_COLLECT,
         "rc=%s" % rcm.exit_code(rep))
    rep2 = rcm.analyze(base, healthy_observed(base), HUBS, errs)
    cell("F2 采集失败优先：即使全在位也判 2",
         rcm.exit_code(rep2) == rcm.EXIT_COLLECT and rep2["ok"] is False,
         "rc=%s ok=%s" % (rcm.exit_code(rep2), rep2["ok"]))
    cell("F3 名册为空：ok=False（不许空集蒙混成全绿）",
         rcm.analyze({}, {}, HUBS)["ok"] is False)

    # ---- G 组：探针解析 ----
    probe_out = ("%s%s %s\n" % (rcm.DIR_MARK, rcm.DIR_MODE, rcm.OWNER)
                 + "".join("%s%s %s %s %s\n" % (rcm.FILE_MARK, m, rcm.FILE_MODE,
                                                rcm.OWNER, base[m])
                           for m in FIXTURE_MEMBERS))
    parsed, why = rcm.parse_probe(probe_out, list(FIXTURE_MEMBERS))
    cell("G1 探针解析：八行全认出且 sha12 原样", parsed is not None
         and all(parsed["files"][m]["sha12"] == base[m] for m in FIXTURE_MEMBERS), why)
    cell("G2 探针解析：目录 mode/owner 原样",
         parsed is not None and parsed["dir"]["mode"] == rcm.DIR_MODE
         and parsed["dir"]["owner"] == rcm.OWNER)
    bad_out = "\n".join(probe_out.split("\n")[1:])       # 掐掉 DIR 行
    parsed_bad, why_bad = rcm.parse_probe(bad_out, list(FIXTURE_MEMBERS))
    cell("G3 探针读不懂（缺 DIR 行）：返回 None，不退化成「缺件」",
         parsed_bad is None and bool(why_bad), why_bad)
    short_out = "\n".join(probe_out.split("\n")[:5])     # 只回了前几行
    parsed_short, why_short = rcm.parse_probe(short_out, list(FIXTURE_MEMBERS))
    cell("G4 探针漏行：返回 None（漏行 ≠ 文件不在）",
         parsed_short is None and bool(why_short), why_short)

    # ---- H 组：命令串（口令不进 argv / 探针只读）----
    wcmd = rcm.write_cmd("/etc/nats/creds", "hermes")
    cell("H1 写入命令串里没有任何口令字面量",
         all(v not in wcmd for v in FIXTURE_JOIN.values()), wcmd[:80])
    cell("H2 写入命令串：umask 077 + chmod 700/600 + 走 stdin 的 cat >",
         "umask 077" in wcmd and ("chmod %s" % rcm.DIR_MODE) in wcmd
         and ("chmod %s" % rcm.FILE_MODE) in wcmd and "cat > " in wcmd, wcmd)
    pcmd = rcm.probe_cmd("/etc/nats/creds", list(FIXTURE_MEMBERS))
    cell("H3 探针命令串：只有 stat / sha256sum / cut，没有任何写操作",
         "stat -c" in pcmd and "sha256sum" in pcmd
         and all(w not in pcmd for w in ("rm ", "mv ", "chmod", "cat >", "mkdir", "systemctl")),
         pcmd[:80])
    cell("H4 回滚命令串只删镜像目录，碰不到 nats.conf",
         rcm.rollback_cmd("/etc/nats/creds").startswith("rm -rf /etc/nats/creds")
         and "nats.conf" not in rcm.rollback_cmd("/etc/nats/creds"))

    # ---- I 组：口令出不了门 ----
    rep_txt = rcm.render(ok_report) + rdc.canonical(ok_report)
    cell("I1 报告（人读 + 规范 JSON）里没有任何明文口令",
         all(v not in rep_txt for v in FIXTURE_JOIN.values()))
    cell("I2 报告里出现的是 sha256[:12]", base["hermes"] in rep_txt)

    # ---- J 组：路径口径 ----
    fake_target = {"transport": "ssh", "ssh_target": "root@example", "runtime_dir": "/opt/x"}
    want_dir = rdc.conf_dir_of("lobster", fake_target).rstrip("/") + "/" + rcm.MIRROR_SUBDIR
    cell("J1 镜像目录 == <conf_dir>/creds", rcm.mirror_dir_of("lobster", fake_target) == want_dir,
         rcm.mirror_dir_of("lobster", fake_target))
    cell("J2 镜像文件 == <conf_dir>/creds/<成员>.pass",
         rcm.mirror_path_of("lobster", fake_target, "hermes") == want_dir + "/hermes.pass")
    cell("J3 算不出目录时返回空串（不猜路径）",
         rcm.mirror_dir_of("thinkbook", {}) == "", repr(rcm.mirror_dir_of("thinkbook", {})))

    bad_cells = [c for c in CELLS if not c[1]]
    transcript = []
    for name, ok, detail in CELLS:
        transcript.append("%-58s %s%s" % (name, "OK" if ok else "**红**",
                                          ("  " + detail) if (not ok and detail) else ""))
    print("=" * 100)
    print("凭据镜像通道等价装置 —— 被测 tools/roster_creds_mirror.py")
    print("=" * 100)
    print("\n".join(transcript))
    print("合计 %d 格，不一致/不合格 %d 处" % (len(CELLS), len(bad_cells)))
    if bad_cells:
        for name, _ok, detail in bad_cells:
            print("  红: %s  %s" % (name, detail))
    digest = hashlib.md5("\n".join(transcript).encode("utf-8")).hexdigest()
    print("transcript_md5=%s" % digest)
    if neg:
        print("NEG：注毒后红 %d 处（>0 才算装置有敏感度）" % len(bad_cells))
        return 0 if len(bad_cells) > 0 else 1
    print("EQUIV_SENTINEL_OK")
    return 0 if not bad_cells else 1


if __name__ == "__main__":
    sys.exit(main())
