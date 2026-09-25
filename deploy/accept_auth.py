#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""accept_auth.py —— 公网入口「先认证后暴露」验收（10 项），在 <HOST> 上跑。

为什么在 <HOST> 上跑：公网实例 9942 只绑 127.0.0.1，本机连不上，必须从机上打。
凭据纪律：临时验收账号的口令**在 <HOST> 上现生成、只在机上用**，只回传 HTTP 码与判词，
口令既不进命令行、也不进日志、更不进聊天。

顺序是有讲究的（踩过坑）：
  * 锁定是**按来源 IP** 记的，且失败表是 append-only（**删不掉、没有解锁后门**）→
    "连错锁死"这一项必须放**最后**跑，否则后面所有登录断言都被自己的锁挡住。
  * 反过来，任何一项看到「尝试过于频繁」都应判为 **SKIP（上轮锁定窗口未到点）**，
    不是 FAIL —— 重跑窗口期里最容易被误报成"认证坏了"。
用法：python accept_auth.py
"""

import os
import subprocess
import sys

# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_SSH_HOST = os.environ.get("AGENT_TEAM_SSH_HOST") or "lobster"
# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_VIEW_BASE = os.environ.get("AGENT_TEAM_VIEW_BASE") or "http://<host>:9941"
# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_RUNTIME_DIR = os.environ.get("AGENT_TEAM_RUNTIME_DIR") or "<deploy-dir>"   # 远端机运行目录
# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_ETC_DIR = os.environ.get("AGENT_TEAM_ETC_DIR") or "<deploy-dir>"
# PHASE2_TODO: 待节点注入 env 后删除
AGENT_TEAM_PUBLIC_IP = os.environ.get("AGENT_TEAM_PUBLIC_IP") or "<node-ip>"

HOST = AGENT_TEAM_SSH_HOST
TAIL = AGENT_TEAM_VIEW_BASE
PUB = os.environ.get("AGENT_TEAM_PUBLIC_BASE") or "http://127.0.0.1:9942"
USER = "acc_viewer"
USER2 = "acc_lock"
OPT = AGENT_TEAM_RUNTIME_DIR
DSN = AGENT_TEAM_ETC_DIR + "/pg.env"
PUBIP = AGENT_TEAM_PUBLIC_IP

REMOTE = r"""
set -u
PUB="%(pub)s"; TAIL="%(tail)s"; OPT="%(opt)s"
U="%(user)s"; U2="%(user2)s"; DSN="%(dsn)s"
PW=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)
PW2="$PW-b"
FAILED=0; SKIPPED=0
pass() { printf '  \033[32m✅ %%s\033[0m\n' "$1"; }
fail() { printf '  \033[31m❌ %%s\033[0m\n' "$1"; FAILED=$((FAILED+1)); }
skip() { printf '  \033[33m⏭  %%s\033[0m\n' "$1"; SKIPPED=$((SKIPPED+1)); }

mkuser() {  # $1=用户名 $2=口令（重跑时 adduser 会失败 → 退回 passwd 重置）
  printf '%%s' "$2" | python3 $OPT/view_auth.py adduser --user "$1" --role viewer --password-stdin --dsn-file $DSN --by acceptance >/dev/null 2>&1 \
  || printf '%%s' "$2" | python3 $OPT/view_auth.py passwd --user "$1" --password-stdin --dsn-file $DSN >/dev/null 2>&1
  python3 $OPT/view_auth.py enable --user "$1" --dsn-file $DSN >/dev/null 2>&1
}
mkuser "$U"  "$PW"
mkuser "$U2" "$PW2"

csrf_of() {  # 取一个新鲜 CSRF：cookie 落 $1，token 打 stdout
  rm -f "$1"
  curl -s -c "$1" "$PUB/login" | grep -o 'name="csrf"[^>]*value="[^"]*"' | sed 's/.*value="//;s/"//' | head -1
}
is_locked() { case "$1" in *频繁*|*锁定*) return 0 ;; *) return 1 ;; esac; }

JAR=/tmp/acc.jar.$$
rm -f $JAR

echo "== 1) 未登录访问公网根路径：应 302 去登录页 =="
R=$(curl -s -o /dev/null -w '%%{http_code}|%%{redirect_url}' "$PUB/")
echo "   $R"
case "$R" in 302*"/login"*) pass "302 → 登录页" ;; *) fail "未登录没被挡（应 302 去 /login）：$R" ;; esac

echo "== 2) 登录页可达：应 200 =="
C=$(curl -s -o /tmp/acc_login.$$ -w '%%{http_code}' "$PUB/login")
[ "$C" = "200" ] && pass "登录页 200" || fail "登录页 $C"

echo "== 3) 正确口令：应 302 + 发会话（必须排在「连错」之前，否则被自己的锁挡住）=="
CSRF=$(csrf_of $JAR)
[ -n "$CSRF" ] && pass "登录页带 CSRF 隐藏域" || fail "登录页没有 CSRF 隐藏域"
H=$(curl -s -b $JAR -c $JAR -o /tmp/acc_ok.$$ -D - -w '%%{http_code}' -X POST "$PUB/login" \
      --data-urlencode "action=login" --data-urlencode "user=$U" \
      --data-urlencode "password=$PW" --data-urlencode "csrf=$CSRF")
CODE=$(echo "$H" | tail -1)
if is_locked "$(cat /tmp/acc_ok.$$)"; then
  skip "来源 IP 正处于上轮验收的锁定窗口内（append-only、15 分钟自然失效）→ 本轮 3/4/5 项不适用"
  LOGIN_OK=0
else
  echo "$H" | grep -qi "^set-cookie: bbview_session=" && pass "登录后发会话 cookie" || fail "登录后没发会话 cookie"
  echo "$H" | grep -qi "^location:" && pass "登录后跳转（$(echo "$H" | grep -i '^location:' | tr -d '\r' | head -1)）" || fail "登录后没跳转"
  LOGIN_OK=1
fi

echo "== 4) 带会话访问：应 200，且不含主人情报 =="
if [ "${LOGIN_OK:-0}" = "1" ]; then
  C=$(curl -s -b $JAR -o /tmp/acc_in.$$ -w '%%{http_code}' "$PUB/")
  [ "$C" = "200" ] && pass "带会话 200" || fail "带会话 $C"
  grep -q "%(pubip)s" /tmp/acc_in.$$ && fail "公网页面泄露了公网 IP" || pass "公网页面无公网 IP 明文"
  if grep -qE "存在但无权查看" /tmp/acc_in.$$; then pass "越权内容按口径遮蔽"; else skip "本页没有需要遮蔽的条目"; fi
  echo "== 5) 错误口令：应被拒不建会话 =="
  JAR2=/tmp/acc.jar2.$$
  CSRF=$(csrf_of $JAR2)
  B=$(curl -s -b $JAR2 -o /tmp/acc_bad.$$ -w '%%{http_code}' -X POST "$PUB/login" \
        --data-urlencode "action=login" --data-urlencode "user=$U" \
        --data-urlencode "password=definitely-wrong-$$" --data-urlencode "csrf=$CSRF")
  grep -q "bbview_session=" $JAR2 2>/dev/null && fail "错误口令竟然发了会话 cookie" || pass "错误口令未建会话（HTTP $B）"
  grep -qE "不正确|错误|失败|频繁" /tmp/acc_bad.$$ && pass "错误口令给了提示文案" || fail "错误口令没有提示文案"
  rm -f $JAR2
else
  skip "3/4/5 未验（来源 IP 处于上轮锁定窗口内）"
fi

echo "== 6) 写请求：应 405 =="
C=$(curl -s -b $JAR -o /dev/null -w '%%{http_code}' -X POST "$PUB/api/sessions" -d 'x=1')
[ "$C" = "405" ] && pass "POST /api/* 405" || fail "POST /api/* 得到 $C（应 405）"

echo "== 7) 安全头 =="
HD=$(curl -sI "$PUB/login")
for h in "strict-transport-security" "content-security-policy" "x-content-type-options" "x-frame-options" "referrer-policy"; do
  echo "$HD" | grep -qi "^$h:" && pass "有 $h" || fail "缺 $h"
done

echo "== 8) 审计留痕（view_audit 最近 10 分钟，append-only）=="
docker exec -i agent-team-pg psql -U bb -d blackboard -At -c \
  "select event || ' × ' || count(*) from view_audit where ts > now() - interval '10 minutes' group by event order by event" \
  | sed 's/^/    /'

echo "== 9) 内网 (tailnet) 实例不受影响：应 200 免登录 =="
C=$(curl -s -o /dev/null -w '%%{http_code}' "$TAIL/api/sessions?limit=1")
[ "$C" = "200" ] && pass "9941 tailnet 免登录 200" || fail "9941 得到 $C（内网回归了！）"

echo "== 10) 暴力破解锁定（放最后：跑完来源 IP 会被锁 ~15 分钟，这是设计使然）=="
LOCKED=""
for i in 1 2 3 4 5 6 7; do
  J=/tmp/acc_l.$$; S=$(csrf_of $J)
  RESP=$(curl -s -b $J -X POST "$PUB/login" \
      --data-urlencode "action=login" --data-urlencode "user=$U2" \
      --data-urlencode "password=wrong-$i" --data-urlencode "csrf=$S")
  if is_locked "$RESP"; then
    LOCKED=$(echo "$RESP" | grep -oE "[^<>]*(频繁|锁定)[^<>]*" | head -1)
    LOCKED="第 $i 次即被挡：$LOCKED"; break
  fi
  rm -f $J
done
[ -n "$LOCKED" ] && pass "$LOCKED" || fail "连错 7 次都没锁定"
echo "    （锁定按来源 IP 记、失败表 append-only 删不掉 → 来源 IP 约 15 分钟内不能再登录，重跑验收请避开这个窗口）"

# 收尾：临时账号一律停用（不留后门）
python3 $OPT/view_auth.py disable --user $U  --dsn-file $DSN >/dev/null 2>&1
python3 $OPT/view_auth.py disable --user $U2 --dsn-file $DSN >/dev/null 2>&1
rm -f $JAR /tmp/acc_login.$$ /tmp/acc_ok.$$ /tmp/acc_bad.$$ /tmp/acc_in.$$
echo
echo "RESULT_FAILED=$FAILED SKIPPED=$SKIPPED"
""" % {"pub": PUB, "tail": TAIL, "user": USER, "user2": USER2,
       "opt": OPT, "dsn": DSN, "pubip": PUBIP}


def main():
    print("### 公网入口验收（在 <HOST> 上执行，口令只在机上现生成）")
    p = subprocess.run(["ssh", "-o", "ConnectTimeout=10", HOST, REMOTE],
                       capture_output=True, timeout=300)
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    print(out)
    if p.returncode != 0:
        print("ssh rc=%d\n%s" % (p.returncode, err[:600]))
    tail = [l for l in out.splitlines() if l.startswith("RESULT_FAILED=")]
    if not tail:
        print("⚠️ 没拿到汇总行（脚本中途出错），请看上面输出")
        return 2
    parts = dict(kv.split("=") for kv in tail[-1].split())
    n, s = int(parts["RESULT_FAILED"]), int(parts["SKIPPED"])
    verdict = "全部通过 ✅" if n == 0 and s == 0 else (
        "通过（有 %d 项因锁定窗口跳过）⏭" % s if n == 0 else "有未过项 ❌")
    print("### 结论：%s（失败 %d 项，跳过 %d 项）" % (verdict, n, s))
    return 0 if n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
