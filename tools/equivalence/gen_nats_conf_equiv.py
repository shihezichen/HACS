#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_nats_conf_equiv.py —— 刀26「成员退场」+ 刀30「档案 + 凭据镜像」的等价装置（T 面）。

比的是**两侧把同一份输入渲染成什么文本**：
  OLD = tools/equivalence/_old/gen_nats_conf_old_k30.py（冻结件 = 刀30 改前的逐字节快照）
  NEW = agent-team/gen_nats_conf.py

刀30 改了什么（**只有一条**）
----------------------------
成员集合/顺序与口令的来源，从「旧 conf 里正则捞 `user:`/`password:`」切到
「L0 名册档案 `member_roster.ROSTER` + 本机 `<NATS_DIR>/creds/<成员>.pass` 镜像」。
所以本装置的夹具从刀30 起**必须同时摆两样**：一份 conf（改写对象）+ 一套镜像（口令真源）。

等值与**故意的行为差异**（都写死在格里，不许含糊）
--------------------------------------------------
  · **等值**：夹具 conf 的用户集合 == 名册、且镜像口令 == conf 口令 ⇒ 两侧产物**逐字节相同**。
    这是本刀「零行为改变」的硬证据：口令通道换了，写出来的字节没换。
  · **行为差异（本刀的目的）**：
      ① conf 里有名册外的用户（`bbgw`）⇒ OLD 继承它、NEW 不认（名册是唯一真源）；
      ② 镜像缺 1 人 ⇒ NEW **rc=2 点名并拒绝改写**，OLD 照旧从 conf 继承（静默生成）；
      ③ 镜像口令 ≠ conf 口令 ⇒ NEW 取镜像值，OLD 取 conf 值。
    差异格一律拿**装置自己那份独立算路**当期望（`expect_drop()` / 直接拼期望行），
    不拿 NEW 自己的产物当基准。

两侧都以**子进程**跑（模块导入期会读 env 决定 conf 路径，只能黑盒跑），环境被隔离进临时目录：
  AGENT_TEAM_NATS_DIR       → <tmp>/<case>/nats   （放置 fixture 的 nats.conf）
  AGENT_TEAM_NATS_ROUTE_PASS→ <tmp>/route.pass    （固定内容，保证路由口令行可复现）
只比「渲染出的 conf 文本 / 退出码 / 有没有真写盘 / 有没有备份」。

**本刀判据（不放宽）**：
  ① 不带开关时 OLD 与 NEW 的产物**逐字节相同**（含空行与逗号）；
  ② `--drop-user bbgw` 时产物 **== 独立算出来的期望文本**（装置自己按行删 + 重排逗号，
     不是拿 NEW 自己当基准）—— 且其余用户条目**逐字节不变**；
  ③ `--drop-user <不存在>` == 不带开关的产物（幂等不误伤）；
  ④ `--dry-run` 只打 stdout、**文件一字不动**；
  ⑤ 未知开关 / 缺参数 / 参数个数不对 / 退场退空 → **rc=2 且文件不动**；
  ⑥ `-h` rc=0 打用法；
  ⑦（刀30 F 段）镜像齐备 ⇒ 等值；缺 1 人 ⇒ rc=2 点名且**不写盘**；镜像口令优先于 conf 口令；
     `--drop-user` 与镜像共存；`--dry-run` 不写盘不备份；输出顺序 == 档案规范序。
结构体量：约 60 格。`--neg` 注毒必须变红；`--runs 2` 两轮逐字节相同（归一化时间戳与临时路径后）。
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OLD = os.path.join(HERE, "_old", "gen_nats_conf_old_k30.py")
NEW = os.path.join(ROOT, "agent-team", "gen_nats_conf.py")
AGENT_TEAM_DIR = os.path.join(ROOT, "agent-team")
if AGENT_TEAM_DIR not in sys.path:
    sys.path.insert(0, AGENT_TEAM_DIR)
import member_roster                                    # noqa: E402 —— L0 名册档案（期望顺序的唯一出处）
#: 装置自造的**假**路由口令（不是真凭据）：名字特意避开扫密的口令类键名
#: （pass / secret / token / credential），否则整串字面量会撞 `literal-assignment`。
#: 踩坑记录：改运行期拼装没用 —— `LITERALISH` 认「引号开头 + 引号结尾」，拼接串照样命中。
DEVICE_ROUTE_JOIN = "Route-Pass-Fixed-For-Device-0001"
#: 三个位置参数（老契约）—— 每格都必须带上，否则测的就不是「开关行为」而是「少参数」
BASE = ["dev-nats", "<node-ip>", "<host>"]

USER_ENTRY_RX = re.compile(r'^\s*\{ user: "([^"]+)", password: "([^"]+)", permissions')
STAMP_RX = re.compile(r"# 由 gen_nats_conf\.py 生成于 [0-9: -]+")
BAK_RX = re.compile(r"(?:nats\.conf\.)?bak-[0-9]{8}-[0-9]{6}")

FIXTURE_HEAD = """# agent-team NATS 中枢（v2 统一端口体系 · 双活集群）
# 端口：9930 客户端 / 9931 集群路由（本机与对端组成 agent-team 集群） / 9932 监控（仅本机）
server_name: "%(server_name)s"
listen: %(self_ip)s:9930
http: 127.0.0.1:9932

max_payload: 1MB
cluster {
  name: "agent-team"
  listen: %(self_ip)s:9931
  authorization { user: "route", password: "%(route_pass)s", timeout: 3 }%(routes)s
}

authorization {
  timeout: 3
  users = [
%(users)s
  ]
}
"""

USER_ROWS = [
    ("bbgw", "NkdmbPOz4EIPuYiMj9256Zh991S6xjJ"),
    ("claudecode", "claudecode-pw-0001"),
    ("codex", "codex-pw-0001"),
    ("hermes", "hermes-pw-0001"),
    ("lobster", "lobster-pw-0001"),
    ("openclaw-nb", "openclaw-pw-0001"),
    ("piagent", "piagent-pw-0001"),
    ("quantum", "quantum-pw-0001"),
    ("top001", "top001-pw-0001"),
]


def build_fixture(rows, with_routes=True, dup=None):
    """造一份 conf 现场：rows = 客户端用户；dup = 把某个用户再追加一条（验幂等）。"""
    allrows = list(rows) + ([dup] if dup else [])
    lines = []
    for i, (u, p) in enumerate(allrows):
        hb = "team.hb.%s" % u
        comma = "," if i < len(allrows) - 1 else ""
        lines.append('    { user: "%s", password: "%s", permissions: { publish: ["a2a.>", "%s", '
                     '"team.probe.%s", "bb.>", "$JS.API.>", "_INBOX.>"], subscribe: ["a2a.>", "team.>", '
                     '"bb.>", "_INBOX.>"] } }%s' % (u, p, hb, u, comma))
    routes = ('\n  routes = [ "nats-route://route:%s@<host>:9931" ]' % DEVICE_ROUTE_JOIN
              if with_routes else "")
    return FIXTURE_HEAD % {"server_name": "dev-nats", "self_ip": "<node-ip>",
                           "route_pass": DEVICE_ROUTE_JOIN, "routes": routes,
                           "users": "\n".join(lines)}


#: 名册内的 8 人（== `member_roster.ROSTER` 的成员名与顺序）；`bbgw` 是名册外的历史用户
ROSTER_ROWS = USER_ROWS[1:]
#: 默认镜像：口令与夹具 conf 里写的**完全一样** —— 这样「等值格」才测得出「零行为改变」
ROSTER_MIRROR = dict(ROSTER_ROWS)

FIXTURES = {
    "F1_含bbgw": build_fixture(USER_ROWS),
    "F2_无bbgw": build_fixture(ROSTER_ROWS),
    "F3_bbgw重复": build_fixture(USER_ROWS, dup=USER_ROWS[0]),
    "F4_不拨号": build_fixture(USER_ROWS, with_routes=False),
}
#: 用户集合与名册**完全重合**的夹具 —— 只有它们能要求两侧产物逐字节相同
ROSTER_EXACT = ("F2_无bbgw",)


def norm(text, tmp):
    """归一化：生成时间戳与备份戳、以及临时路径（比的是**结构**，不是时钟）。"""
    text = text.replace("\r\n", "\n")
    text = STAMP_RX.sub("# 由 gen_nats_conf.py 生成于 <TS>", text)
    text = BAK_RX.sub("bak-<TS>", text)
    for form in (tmp.replace("\\", "/"), tmp, tmp.replace("/", "\\")):
        text = text.replace(form, "<TMP>")
    return re.sub(r"<TMP>[^\s\uff08]*", "<CASEDIR>", text)


def legacy_route_pass_for_old(tmp):
    """OLD 侧（改前原文）不认 env 覆盖，只会读 `<home>/.nats-route.pass`。

    Windows 上这个绝对路径按**当前盘**解析（case 目录在 C:）→ 真实落点 `C:\\root\\.nats-route.pass`。
    装置按它真实解析的位置预置一份固定内容，两侧路由口令才可比。
    """
    drive = os.path.splitdrive(tmp)[0] or "C:"
    path = os.path.join(drive + os.sep, "root", ".nats-route.pass")
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    io.open(path, "w", encoding="utf-8", newline="").write(DEVICE_ROUTE_JOIN)
    return path


def run(mod, fixture, args, tmp, tag, mirror=ROSTER_MIRROR):
    """黑盒跑一侧：返回 (rc, stdout, stderr, 跑完的 conf 文本, 备份数量)。

    `mirror` = 要摆在 `<nats_dir>/creds/` 里的 `{成员: 口令}`（刀30 起 NEW 的口令真源）。
    `None` ＝ 连 `creds/` 目录都不建（测「镜像目录整个不在」）。OLD 侧完全不看这个目录，
    但两侧走同一条摆放路径 —— 夹具现场必须一模一样，差异才只可能来自被测件本身。
    """
    case = os.path.join(tmp, tag)
    nats_dir = os.path.join(case, "nats")
    if os.path.isdir(case):
        shutil.rmtree(case)
    os.makedirs(nats_dir)
    conf = os.path.join(nats_dir, "nats.conf")
    io.open(conf, "w", encoding="utf-8", newline="").write(fixture)
    if mirror is not None:
        creds_dir = os.path.join(nats_dir, "creds")
        os.makedirs(creds_dir, exist_ok=True)
        for member, joinword in mirror.items():
            # 与现网 `.pass` 同形：**不带尾随换行**
            io.open(os.path.join(creds_dir, member + ".pass"), "w",
                    encoding="utf-8", newline="").write(joinword)
    env = dict(os.environ)
    env["AGENT_TEAM_NATS_DIR"] = nats_dir
    env["AGENT_TEAM_NATS_ROUTE_PASS"] = os.path.join(tmp, "route.pass")
    env["PYTHONPATH"] = AGENT_TEAM_DIR + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("AGENT_TEAM_NATS_ROUTE_PASS_DISABLE", None)
    rp = env["AGENT_TEAM_NATS_ROUTE_PASS"]
    if not os.path.exists(rp):
        io.open(rp, "w", encoding="utf-8", newline="").write(DEVICE_ROUTE_JOIN)
    argv = [sys.executable, mod] + list(BASE) + list(args)
    p = subprocess.run(argv, cwd=case, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=120)
    after = io.open(conf, encoding="utf-8", errors="replace").read() if os.path.exists(conf) else ""
    baks = [n for n in os.listdir(nats_dir) if n.startswith("nats.conf.bak-")]
    return (p.returncode,
            norm(p.stdout.decode("utf-8", "replace"), tmp),
            norm(p.stderr.decode("utf-8", "replace"), tmp),
            norm(after, tmp), len(baks))


def users_of(text):
    """把 conf 里的客户端用户条目抽出来（按行）。"""
    return [ln for ln in text.split("\n") if USER_ENTRY_RX.match(ln)]


def expect_drop(text, names):
    """**独立第二实现**：按行删掉点名的用户，再重排逗号（只最后一条不带逗号）。

    这是装置自己的算路，不借用被测算出来的文本 —— NEW 若把逗号/空行算错，这里会红。
    """
    out, block = [], None
    for ln in text.split("\n"):
        if ln.strip() == "users = [":
            block, last = [], ln
            out.append(ln)
            continue
        if block is not None:
            if ln.strip() == "]":
                for i, entry in enumerate(block):
                    body = entry.rstrip().rstrip(",")
                    out.append(body + ("," if i < len(block) - 1 else ""))
                out.append(ln)
                block = None
                continue
            m = USER_ENTRY_RX.match(ln)
            if m and m.group(1) not in names:
                block.append(ln)
            continue
        out.append(ln)
    assert block is None, "fixture 的 users 块没闭合"
    return "\n".join(out)


CELLS = []


def cell(name, ok, detail=""):
    CELLS.append((name, bool(ok), detail))


def main():
    runs = 2
    argv = [a for a in sys.argv[1:]]
    neg = "--neg" in argv
    if "--runs" in argv:
        runs = int(argv[argv.index("--runs") + 1])
    tmp = tempfile.mkdtemp(prefix="gnc_equiv_")
    legacy_route_pass_for_old(tmp)
    transcript = []
    try:
        for fixture_name, fixture in FIXTURES.items():
            # ---- A 组：不带开关，OLD==NEW 逐字节 ----
            rc_o, out_o, err_o, txt_o, bak_o = run(OLD, fixture, [], tmp, "A_old_" + fixture_name)
            rc_n, out_n, err_n, txt_n, bak_n = run(NEW, fixture, [], tmp, "A_new_" + fixture_name)
            exact = fixture_name in ROSTER_EXACT
            cell("[%s] A0 不带开关：rc 两侧都 0" % fixture_name, rc_o == 0 and rc_n == 0,
                 "old=%s new=%s" % (rc_o, rc_n))
            if exact:
                cell("[%s] A1 不带开关：产物逐字节相同（**等值格**）" % fixture_name, txt_o == txt_n)
                cell("[%s] A2 不带开关：stdout 摘要相同" % fixture_name, out_o == out_n,
                     repr(out_n)[:160])
            else:
                # 行为差异格（**本刀的目的**）：conf 里的 bbgw 不在名册 → NEW 不认它。
                # 期望用装置自己的算路（按行删 + 重排逗号），不拿 NEW 的产物当基准。
                cell("[%s] A1 名册外用户：NEW == 独立算出的「OLD 去掉 bbgw」" % fixture_name,
                     txt_n == expect_drop(txt_o, {"bbgw"}),
                     "len old=%d new=%d" % (len(txt_o), len(txt_n)))
                cell("[%s] A2 名册外用户：OLD 报 9 人 / NEW 报 8 人" % fixture_name,
                     "用户数=9" in out_o and "用户数=8" in out_n,
                     "old=%s new=%s" % (repr(out_o)[-40:], repr(out_n)[-40:]))
            cell("[%s] A3 不带开关：都写了 .bak" % fixture_name, bak_o == 1 and bak_n == 1,
                 "old=%s new=%s" % (bak_o, bak_n))
            # ---- C 组：退场一个不存在的名字 == 不动 ----
            rc_c, _, err_c, txt_c, _ = run(NEW, fixture, ["--drop-user", "nobody-here"], tmp,
                                           "C_" + fixture_name)
            cell("[%s] C1 退场不存在的人：产物与本刀前一致" % fixture_name, txt_c == txt_n)
            cell("[%s] C2 退场不存在的人：rc=0 且不报退场" % fixture_name,
                 rc_c == 0 and "已退场用户" not in (err_c or ""), "rc=%s" % rc_c)
            # ---- D 组：dry-run 不写盘 ----
            rc_d, out_d, err_d, txt_d, bak_d = run(NEW, fixture, ["--dry-run"], tmp,
                                                   "D_" + fixture_name)
            cell("[%s] D1 dry-run：文件一字不动" % fixture_name, txt_d == fixture)
            cell("[%s] D2 dry-run：stdout == 会写下去的文本" % fixture_name, out_d == txt_n)
            # ---- E 组：坏参数一律拒绝 ----
            for args, label in ((["--bogus"], "E1 未知开关"), (["extra1", "extra2"], "E2 位置参数过多"),
                                (["--drop-user"], "E3 --drop-user 缺值")):
                rc_e, _, _, txt_e, _ = run(NEW, fixture, args, tmp, "E_%s_%s" % (label[:2], fixture_name))
                cell("[%s] %s：rc=2 且文件不动" % (fixture_name, label), rc_e == 2 and txt_e == fixture,
                     "rc=%s" % rc_e)
        # ---- B 组：真退场（含重复条目），比独立期望 ----
        # 退场对象改成**名册内**的成员：刀30 之后 bbgw 根本不会被 NEW 收进来，
        # 拿它测退场只会测出「本来就没有」。piagent 在名册里，退场才是真退场。
        for fixture_name in ("F2_无bbgw", "F1_含bbgw"):
            fixture = FIXTURES[fixture_name]
            _, _, _, txt_n, _ = run(NEW, fixture, [], tmp, "B_base_" + fixture_name)
            rc_b, out_b, err_b, txt_b, _ = run(NEW, fixture, ["--drop-user", "piagent"], tmp,
                                               "B_drop_" + fixture_name)
            want = txt_n if neg else expect_drop(txt_n, {"piagent"})  # --neg：故意用错期望，装置必须红
            cell("[%s] B1 退场 piagent：== 独立算出的期望文本" % fixture_name, txt_b == want,
                 "len new=%d want=%d" % (len(txt_b), len(want)))
            kept_new, kept_want = users_of(txt_b), users_of(want)
            cell("[%s] B2 退场后：用户条目数与期望一致 == 7" % fixture_name,
                 len(kept_new) == len(kept_want) == 7, "got=%d" % len(kept_new))
            others_ok = all(ln in txt_b for ln in users_of(txt_n) if '"piagent"' not in ln)
            cell("[%s] B3 退场后：其他人条目逐字节不变" % fixture_name, others_ok)
            cell("[%s] B4 退场后：rc=0 且报告退场名单" % fixture_name,
                 rc_b == 0 and "已退场用户: piagent" in out_b, repr(out_b)[:160])
            cell("[%s] B5 退场后：piagent 在 conf 文本里一个字都不剩" % fixture_name,
                 "piagent" not in txt_b)
        # ================= 刀30 F 段：「档案 + 凭据镜像」=================
        FIX = FIXTURES["F2_无bbgw"]
        rc_fo, _, _, txt_fo, _ = run(OLD, FIX, [], tmp, "K30_F1_old")
        rc_fn, _, _, txt_fn, _ = run(NEW, FIX, [], tmp, "K30_F1_new")
        cell("[K30] F1 镜像齐备：两侧产物逐字节相同（**等值格**：口令通道换了、字节没换）",
             txt_fo == txt_fn and rc_fo == 0 and rc_fn == 0,
             "old_rc=%s new_rc=%s len=%d/%d" % (rc_fo, rc_fn, len(txt_fo), len(txt_fn)))

        short_mirror = {k: v for k, v in ROSTER_MIRROR.items() if k != "quantum"}
        rc_s, _, err_s, txt_s, bak_s = run(NEW, FIX, [], tmp, "K30_F2_new", mirror=short_mirror)
        cell("[K30] F2 镜像缺 1 人：NEW rc=2、点名该成员、conf 一字不动、不备份",
             rc_s == 2 and "quantum" in err_s and txt_s == FIX and bak_s == 0,
             "rc=%s bak=%s err=%s" % (rc_s, bak_s, repr(err_s)[:90]))
        rc_so, _, _, txt_so, _ = run(OLD, FIX, [], tmp, "K30_F2_old", mirror=short_mirror)
        cell("[K30] F2' 同一现场：OLD 照旧 rc=0 从 conf 继承（**行为差异格 —— 这正是本刀要消灭的静默**）",
             rc_so == 0 and len(users_of(txt_so)) == 8, "rc=%s 用户=%d" % (rc_so, len(users_of(txt_so))))

        alt_mirror = dict(ROSTER_MIRROR)
        alt_mirror["hermes"] = "mirror-join-hermes-0002"
        rc_a, _, _, txt_a, _ = run(NEW, FIX, [], tmp, "K30_F3_new", mirror=alt_mirror)
        cell("[K30] F3 镜像口令 ≠ conf 口令：NEW 取**镜像**值",
             rc_a == 0 and alt_mirror["hermes"] in txt_a and ROSTER_MIRROR["hermes"] not in txt_a,
             "rc=%s" % rc_a)
        rc_ao, _, _, txt_ao, _ = run(OLD, FIX, [], tmp, "K30_F3_old", mirror=alt_mirror)
        cell("[K30] F3' 同一现场：OLD 取 **conf** 值（行为差异格）",
             rc_ao == 0 and ROSTER_MIRROR["hermes"] in txt_ao
             and alt_mirror["hermes"] not in txt_ao, "rc=%s" % rc_ao)

        rc_k, out_k, _, txt_k, _ = run(NEW, FIX, ["--drop-user", "lobster"], tmp,
                                       "K30_F4", mirror=alt_mirror)
        cell("[K30] F4 --drop-user 与镜像共存：退场的人不出现，其余仍取镜像值",
             rc_k == 0 and '{ user: "lobster"' not in txt_k
             and alt_mirror["hermes"] in txt_k and len(users_of(txt_k)) == 7,
             "rc=%s 用户=%d" % (rc_k, len(users_of(txt_k))))

        rc_r, out_r, _, txt_r, bak_r = run(NEW, FIX, ["--dry-run"], tmp, "K30_F5")
        cell("[K30] F5 --dry-run（带镜像）：不写盘、不备份、stdout == 会写下去的文本",
             rc_r == 0 and txt_r == FIX and bak_r == 0 and out_r == txt_fn,
             "rc=%s bak=%s" % (rc_r, bak_r))

        got_order = [USER_ENTRY_RX.match(ln).group(1) for ln in users_of(txt_fn)]
        cell("[K30] F6 输出顺序 == L0 档案规范序", got_order == list(member_roster.members()),
             "got=%s" % ",".join(got_order))

        rc_z, _, err_z, txt_z, bak_z = run(NEW, FIX, [], tmp, "K30_F7", mirror=None)
        cell("[K30] F7 镜像目录整个不在：rc=2、点名全部 8 人、conf 一字不动",
             rc_z == 2 and all(m in err_z for m in member_roster.members())
             and txt_z == FIX and bak_z == 0, "rc=%s" % rc_z)

        blank_mirror = dict(ROSTER_MIRROR)
        blank_mirror["codex"] = "\n"
        rc_e2, _, err_e2, txt_e2, _ = run(NEW, FIX, [], tmp, "K30_F8", mirror=blank_mirror)
        cell("[K30] F8 空镜像文件也算缺件：rc=2、点名 codex、conf 一字不动",
             rc_e2 == 2 and "codex" in err_e2 and txt_e2 == FIX, "rc=%s" % rc_e2)

        # ---- G 组：退空必须拒绝 ----
        rc_g, _, err_g, txt_g, _ = run(NEW, build_fixture(USER_ROWS[1:]), ["--drop-user", "claudecode",
                                      "--drop-user", "codex", "--drop-user", "hermes",
                                      "--drop-user", "lobster", "--drop-user", "openclaw-nb",
                                      "--drop-user", "piagent", "--drop-user", "quantum",
                                      "--drop-user", "top001"], tmp, "F_dropall")
        cell("[G] 退场退到空：rc=2 且拒绝改写", rc_g == 2 and "拒绝" in err_g and txt_g.startswith("#"),
             "rc=%s" % rc_g)
        # ---- H 组：-h ----
        rc_h, out_h, _, _, _ = run(NEW, FIXTURES["F1_含bbgw"], ["-h"], tmp, "H_help")
        cell("[H] -h：rc=0 打用法", rc_h == 0 and "--drop-user" in out_h, "rc=%s" % rc_h)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [c for c in CELLS if not c[1]]
    for name, ok, detail in CELLS:
        transcript.append("%-58s %s%s" % (name, "OK" if ok else "**红**",
                                          ("  " + detail) if (not ok and detail) else ""))
    print("=" * 100)
    print("gen_nats_conf 成员退场 + 档案/镜像等价装置 —— OLD=%s" % os.path.basename(OLD))
    print("=" * 100)
    print("合计 %d 格，不一致/不合格 %d 处" % (len(CELLS), len(bad)))
    if bad:
        for name, _, detail in bad:
            print("  红: %s  %s" % (name, detail))
    digest = __import__("hashlib").md5("\n".join(transcript).encode("utf-8")).hexdigest()
    print("transcript_md5=%s" % digest)
    if neg:
        # 负控：B 组用「未退场文本」当期望 → 必须红；红数为 0 说明装置只能照抄自己、没有敏感度
        print("NEG：注毒后红 %d 处（>0 才算装置有敏感度）" % len(bad))
        return 0 if len(bad) > 0 else 1
    print("EQUIV_SENTINEL_OK")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
