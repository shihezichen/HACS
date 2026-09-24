#!/bin/bash
# 安装/升级「团队黑板·只读看板」（blackboard_view.py，端口 9941）
#
# 在**龙虾（lobster）**上、仓库目录里跑：
#     sudo bash install_blackboard_view.sh                 # 用单元里写死的 <host>
#     sudo bash install_blackboard_view.sh <host>  # 显式指定要绑的 tailnet IP
#
# 干的事：装脚本 → 装 systemd 单元 → daemon-reload → enable --now → 打印状态。
# 不碰数据库、不改黑板服务、不写任何条目 —— 这是个只读看板。
set -u

UNIT=agent-team-blackboard-view.service
APP_DIR=<deploy-dir>
APP=blackboard_view.py
TOKEN_FILE=<deploy-dir>/client-token
PORT=9941
BIND_IP="${1:-}"

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

die() { echo "❌ $*" >&2; exit 1; }
ok()  { echo "✅ $*"; }

[ "$(id -u)" = "0" ] || die "请用 root 跑（sudo bash $0）"
[ -f "$SRC_DIR/$APP" ]  || die "找不到 $SRC_DIR/$APP"
[ -f "$SRC_DIR/$UNIT" ] || die "找不到 $SRC_DIR/$UNIT"
command -v systemctl >/dev/null 2>&1 || die "这台机器没有 systemd"
command -v python3   >/dev/null 2>&1 || die "没装 python3"

echo "== 1/6 语法自检 =="
python3 -m py_compile "$SRC_DIR/$APP" || die "$APP 语法错误，拒绝安装"
ok "py_compile 通过（纯标准库，无需 pip install）"

echo "== 2/6 前置检查 =="
if [ -r "$TOKEN_FILE" ]; then
  ok "token 文件存在：$TOKEN_FILE（$(wc -c < "$TOKEN_FILE") 字节，内容不打印）"
else
  echo "⚠️  $TOKEN_FILE 不可读 —— 服务照样起得来，但页面会一直亮「上游拒绝凭据」黄条"
fi

echo "== 3/6 安装程序到 $APP_DIR =="
mkdir -p "$APP_DIR" || die "创建 $APP_DIR 失败"
install -m 0755 "$SRC_DIR/$APP" "$APP_DIR/$APP" || die "复制 $APP 失败"
ok "$APP_DIR/$APP"

echo "== 4/6 安装 systemd 单元 =="
install -m 0644 "$SRC_DIR/$UNIT" "/etc/systemd/system/$UNIT" || die "复制单元失败"
if [ -n "$BIND_IP" ]; then
  sed -i "s|--host [0-9.]\+|--host $BIND_IP|" "/etc/systemd/system/$UNIT" || die "改写 --host 失败"
  ok "单元已改绑 --host $BIND_IP"
fi
grep -E '^\s+--host|^\s+--upstream|^\s+--port' "/etc/systemd/system/$UNIT" | sed 's/^/    /'

echo "== 5/6 daemon-reload + enable --now =="
systemctl daemon-reload || die "daemon-reload 失败"
systemctl enable --now "$UNIT" || die "enable --now 失败，看 journalctl -u $UNIT -n 50"
sleep 2

echo "== 6/6 状态 =="
systemctl status "$UNIT" --no-pager -l | head -n 20
echo
echo "-- 最近日志 --"
journalctl -u "$UNIT" -n 15 --no-pager

echo
# 从单元里回读实际绑的地址（用 sed，不依赖 GNU grep 的 -P）
HOST_LINE=$(sed -n 's/.*--host \([0-9.]\{1,\}\).*/\1/p' "/etc/systemd/system/$UNIT" | head -n1)
[ -n "$HOST_LINE" ] || HOST_LINE=127.0.0.1

if command -v curl >/dev/null 2>&1; then
  echo "-- 自检 http://$HOST_LINE:$PORT/healthz（看板自身，不代表上游 9940 通）--"
  curl -s -m 5 "http://$HOST_LINE:$PORT/healthz" || echo "（探测失败，看上面的日志）"
  echo
  echo "-- 上游联通性 http://$HOST_LINE:$PORT/api/health --"
  curl -s -m 8 "http://$HOST_LINE:$PORT/api/health" | head -c 400 || echo "（探测失败）"
  echo
fi

echo
if systemctl is-active --quiet "$UNIT"; then
  ok "看板已启动。tailnet 内浏览器打开：http://$HOST_LINE:$PORT/"
else
  die "服务未处于 active 状态，排查：journalctl -u $UNIT -n 50 --no-pager"
fi
echo "常用命令：systemctl restart $UNIT ｜ journalctl -u $UNIT -f"
