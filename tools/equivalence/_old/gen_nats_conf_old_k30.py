#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_nats_conf.py —— 在一台中枢机上就地重写 <NATS_DIR>/nats.conf

做两件事（不改端口、不改口令、不改 server_name）：
  1) 启用集群路由 9931：本机与对端组成 nats cluster，跨中枢互通（消除单点/脑裂）
  2) 成员用户白名单统一形状：publish 收窄为「自己那条」心跳/探测主题，堵掉伪造他人心跳

用法（在目标机上以 root 跑）：
  python3 gen_nats_conf.py <本机server_name> <本机tailnet_ip> <对端tailnet_ip>
路由口令从 <PASS_FILE> 读（不存在则生成）。
"""
import os
import re
import secrets
import shutil
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import node_profile as _npf  # noqa: E402  L0 节点档案：宿主事实唯一真源（配置目录 / 路由凭据文件）

# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_NATS_DIR = os.environ.get("AGENT_TEAM_NATS_DIR") or _npf.nats_conf_dir_posix()
CONF = AGENT_TEAM_NATS_DIR + "/nats.conf"
# 路由凭据文件：位置由 L0 档案给（posix 惯例值）；可用 env 顶掉 —— 与上面的 NATS_DIR 同款
# 覆盖口，给等价装置/演练用（不开这个口，装置就得往 /root 那种真系统路径上写文件）。
ROUTE_PASS_FILE = (os.environ.get("AGENT_TEAM_NATS_ROUTE_PASS")
                   or _npf.nats_route_pass_file())

CLUSTER_NAME = "agent-team"
ROUTE_USER = "route"

# 一个凭据代表多个成员时的登记表：本机 teamd 用单一凭据同时为这几个成员发心跳。
# 笔记本 teamd 以 hermes 凭据同时代表 5 个成员（现状拓扑，不是特权）——teamd 改成
# 「一成员一凭据」后，这里应当清空，白名单随之自动收成只剩自己。
HB_DELEGATION = {
    # 2026-09-20: teamd 已改为「一成员一凭据」, 笔记本 5 个成员各持自己的口令文件、
    # 各走一条独立连接 → 本表清空, 白名单对所有人收成同一形状(只允许发自己的心跳)。
    # 若将来又出现「一个凭据代表多成员」的拓扑, 才需要在这里登记。
}



def hb_subjects(user):
    """该凭据有权发布的心跳主题 = 自己 + 它代表的成员。"""
    names = [user] + list(HB_DELEGATION.get(user, []))
    return [x for i, x in enumerate(names) if x not in names[:i]]


def perm_list(user):
    """每个成员的白名单形状完全一致，只把它实际代表的那几条心跳主题代入 → 平等且不可冒名。"""
    hb = ", ".join('"team.hb.%s"' % n for n in hb_subjects(user))
    return (
        'publish: ["a2a.>", %s, "team.probe.%s", "bb.>", "$JS.API.>", "_INBOX.>"], '
        'subscribe: ["a2a.>", "team.>", "bb.>", "_INBOX.>"]' % (hb, user)
    )


USAGE = ("用法: gen_nats_conf.py <server_name> <self_ip> <peer_ip> "
         "[--drop-user <成员名>]... [--dry-run]")


def parse_argv(argv):
    """解析参数：**三个位置参数的老契约原样保留**，另加两个可选开关。

    `--drop-user <名>`（可重复）＝成员退场：从白名单里剔除该用户，其余用户口令原样保留。
    这是**唯一**该走的退场路径 —— 本文件的注释要求「改配置请改脚本再重跑，别手改本文件」，
    手删一行会丢掉可复现性与留痕。
    `--dry-run` ＝只把结果打到 stdout，不写盘、不备份（用于先看再动）。
    未知开关一律**拒绝**（返回 None），不做静默忽略。

    返回 `(pos, drop_users, dry_run)`；参数不合法返回 `(None, None, None)`；`-h` 返回 `("help", None, None)`。
    """
    pos, drop, dry = [], [], False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            return "help", None, None
        if a == "--drop-user":
            if i + 1 >= len(argv):
                return None, None, None
            drop.append(argv[i + 1])
            i += 2
        elif a == "--dry-run":
            dry = True
            i += 1
        elif a.startswith("--"):
            return None, None, None
        else:
            pos.append(a)
            i += 1
    if len(pos) != 3:
        return None, None, None
    return pos, drop, dry


def main():
    if len(sys.argv) == 1:
        sys.stderr.write(USAGE + "\n")
        return 2
    argv = sys.argv[1:]
    pos, drop_users, dry_run = parse_argv(argv)
    if pos == "help":
        print(USAGE)
        return 0
    if pos is None:
        sys.stderr.write(USAGE + "\n")
        return 2
    server_name, self_ip, peer_ip = pos[0], pos[1], pos[2]

    if not os.path.exists(CONF):
        sys.stderr.write("找不到 %s\n" % CONF)
        return 2
    with open(CONF, "r", encoding="utf-8") as fh:
        old = fh.read()

    # 保留原有用户口令（原样复用，不新造、不丢失）
    users = re.findall(r'user:\s*"([^"]+)",\s*password:\s*"([^"]+)"', old)
    # 幂等：重跑时旧配置里已含集群授权行（user: "route"），别把它当成客户端用户再收进来
    seen = set()
    uniq = []
    for u, p in users:
        if u == ROUTE_USER or u in seen:
            continue
        seen.add(u)
        uniq.append((u, p))
    users = uniq
    if not users:
        sys.stderr.write("解析不到任何用户，拒绝改写\n")
        return 2

    # ---- 成员退场（--drop-user）：只剔除点名的用户；其余用户的口令/权限串**原样保留** ----
    drop_set = set(d for d in drop_users if d)
    dropped = sorted(set(u for u, _ in users if u in drop_set))
    if drop_set:
        kept = [up for up in users if up[0] not in drop_set]
        if not kept:
            sys.stderr.write("拒绝改写：--drop-user 会把用户清空\n")
            return 2
        users = kept

    # 路由口令：文件里没有就生成（不落 argv、不进日志）
    if os.path.exists(ROUTE_PASS_FILE):
        with open(ROUTE_PASS_FILE, "r", encoding="utf-8") as fh:
            route_pass = fh.read().strip()
    else:
        route_pass = secrets.token_urlsafe(24)
        fd = os.open(ROUTE_PASS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(route_pass)
    if not route_pass:
        sys.stderr.write("路由口令为空，拒绝改写\n")
        return 2

    lines = []
    lines.append("# agent-team NATS 中枢（v2 统一端口体系 · 双活集群）")
    lines.append("# 端口：9930 客户端 / 9931 集群路由（本机与对端组成 agent-team 集群） / 9932 监控（仅本机）")
    lines.append("# 不上 JetStream：真相源是黑板(PG)；集群只做消息路由，不做持久化")
    lines.append("# 由 gen_nats_conf.py 生成于 %s（改配置请改脚本再重跑，别手改本文件）"
                 % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append('server_name: "%s"' % server_name)
    lines.append("listen: %s:9930" % self_ip)
    lines.append("http: 127.0.0.1:9932")
    lines.append("")
    lines.append("max_payload: 1MB")
    lines.append("max_connections: 64")
    lines.append('write_deadline: "2s"')
    lines.append('ping_interval: "2m"')
    lines.append("ping_max: 3")
    lines.append("")
    lines.append("debug: false")
    lines.append("trace: false")
    lines.append("logtime: true")
    lines.append("")
    lines.append("# ---- 集群路由：两台中枢互为一个集群，任一在线即可全通畅 ----")
    lines.append("cluster {")
    lines.append('  name: "%s"' % CLUSTER_NAME)
    lines.append("  listen: %s:9931" % self_ip)
    lines.append('  authorization { user: "%s", password: "%s", timeout: 3 }'
                 % (ROUTE_USER, route_pass))
    # peer_ip=none → 本机不主动拨号，只接受对端拨入（避免两侧互拨产生重复路由条目）
    if peer_ip and peer_ip.strip().lower() != "none":
        lines.append('  routes = [ "nats-route://%s:%s@%s:9931" ]' % (ROUTE_USER, route_pass, peer_ip))
    lines.append("}")
    lines.append("")
    lines.append("authorization {")
    lines.append("  timeout: 3")
    lines.append("  users = [")
    for idx, (user, password) in enumerate(users):
        comma = "," if idx < len(users) - 1 else ""
        lines.append('    { user: "%s", password: "%s", permissions: { %s } }%s'
                     % (user, password, perm_list(user), comma))
    lines.append("  ]")
    lines.append("}")
    lines.append("")

    new = "\n".join(lines)

    if dry_run:
        sys.stdout.write(new)
        sys.stderr.write("dry-run：未写盘、未备份（用户数=%d；退场=%s）\n"
                         % (len(users), ",".join(dropped) or "无"))
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(CONF, "%s.bak-%s" % (CONF, stamp))
    with open(CONF, "w", encoding="utf-8") as fh:
        fh.write(new)
    try:
        os.chmod(CONF, 0o640)
    except Exception:
        pass

    print("已重写 %s（备份 .bak-%s）" % (CONF, stamp))
    if dropped:
        print("已退场用户: %s" % ",".join(dropped))
    print("server_name=%s listen=9930/9931 peer=%s 用户数=%d" % (server_name, peer_ip, len(users)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
