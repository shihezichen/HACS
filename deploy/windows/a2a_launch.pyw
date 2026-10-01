#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""a2a_launch.pyw —— A2A 服务的启动壳（把 stdout/stderr 落到日志后原地跑 a2a_svc）

为什么要有这个壳（2026-09-29 事故）：
  计划任务直接用 pythonw.exe 跑 a2a_svc.py，而 pythonw 没有控制台，stdout/stderr
  被丢弃 —— 服务静默退出后**没有任何痕迹**，只能靠猜。a2a.env 里的 A2A_LOG_FILE
  也没人读（a2a_edge 不解析该文件）。

  本壳做的事：
    1. 读 <deploy-dir>\\a2a.env（键值配置的唯一入口，原来只写在注释里没人用）；
    2. 日志超 5MB 先轮转；
    3. 用 os.dup2 把 fd 1/2 指到日志，再原地 import a2a_svc 并跑 main()
       —— 不另起子进程，计划任务里那个进程就是服务本身，崩溃现场必留痕。

计划任务 AgentTeamA2ASvc 的动作应改为：
    pythonw.exe  <deploy-dir>\\app\\a2a_launch.pyw

为什么还要「等尾网就绪」（2026-09-30 事故）：
  开机 10:31:38 重启，a2a 直到 10:33:13 才被哨兵拉起 —— 根因是**开机早期尾网地址
  <node-ip> 还不存在**（同一时刻 teamd 日志：WinError 10051 网络不可达）。
  服务绑的是尾网 IP，地址没起来 → bind 直接炸（实测 WinError 10049「在其上下文中，
  该请求的地址无效」）。即「开机启动」与「尾网就绪」是**两件有时序依赖的事**，
  谁先谁后不由服务说了算，只能等。
  故本壳在 import 之前先轮询「这个地址现在绑得上吗」，最多等 A2A_BIND_WAIT_S 秒
  （默认 180），每轮都落日志；等不到就以**非零码退出**，把「重启我」交还给计划任务
  （RestartOnFailure）与哨兵，不静默烂在那里。

  测试用钩子：A2A_ENV_FILE 换配置文件，A2A_BIND_WAIT_S 缩等待窗口。

批⑪ 归位（`<deploy-dir>` → 本仓 `deploy/windows/`）：
  与同目录 `_boot.py` 同源同形的**部署件**。原件里的 4 处盘符字面量与 1 处内网 IP
  按部署件口径收口 —— 运行目录从 `__file__` 派生后 `os.environ.setdefault(...)`
  （与 `_boot.py` 的 `AGENT_TEAM_RUNTIME_DIR` / `AGENT_TEAM_LOG_DIR` 同一套），
  绑定地址退成 env + 占位符兜底（占位符语义 = 未配置，真值由 a2a.env / 部署面给）。
  能力一字未改：轮转日志、`os.dup2` 接管 fd 1/2、轮询等地址可绑、原地 `import a2a_svc`。
"""
import os
import shutil
import socket
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))            # <RUN_DIR>\app（部署面）
_RUN_DIR = os.path.dirname(_HERE)                             # <RUN_DIR>

# 运行面真值：与 _boot.py 同一套注入（setdefault —— 环境里已显式给出的值优先，本壳不覆盖）
os.environ.setdefault("AGENT_TEAM_RUNTIME_DIR", _RUN_DIR)
os.environ.setdefault("AGENT_TEAM_LOG_DIR", _RUN_DIR)

# 绝对包名 import 的搜索路径：部署面是 app 根（_HERE），仓内是仓根（deploy/windows 的上两级）
for _p in (_HERE, os.path.dirname(os.path.dirname(_HERE))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

RUNTIME_DIR = os.environ["AGENT_TEAM_RUNTIME_DIR"]
LOG_DIR = os.environ["AGENT_TEAM_LOG_DIR"]

ENV_FILE = os.environ.get("A2A_ENV_FILE") or os.path.join(RUNTIME_DIR, "a2a.env")
DEFAULT_LOG = os.path.join(LOG_DIR, "a2a_svc.log")
MAX_LOG_BYTES = 5 * 1024 * 1024
DEFAULT_BIND_WAIT_S = 180
BIND_POLL_S = 5

#: 绑定地址兜底 —— 占位符语义 = **未配置**（真值由 a2a.env 的 A2A_HOST 或 env 给）。
#: 仓内不写真实尾网地址；配不上的地址绑不上，会照常走下面的「等不到就退 3」那条路。
A2A_HOST_DEFAULT = "<node-ip>"

EXIT_BIND_TIMEOUT = 3          # 「地址一直没就绪」的专用退出码（非零 → 计划任务会重启）


def load_env(path: str) -> dict:
    cfg = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                cfg[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return cfg


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def host_bindable(host: str, port: int = 0) -> bool:
    """这个地址此刻绑得上吗（= 本机真有这个 IP）。绑不上不抛，只回 False。"""
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return False
    ok = False
    for family, socktype, proto, _c, sockaddr in infos:
        s = socket.socket(family, socktype, proto)
        try:
            s.bind(sockaddr)
            ok = True
        except OSError:
            ok = False
        finally:
            s.close()
        if ok:
            return True
    return False


def wait_host_ready(host: str, port: int, wait_s: int) -> bool:
    """等 host 变成「本机绑得上的地址」。环回地址无需等；超时返回 False。"""
    if _is_loopback(host):
        return True
    t0 = time.time()
    n = 0
    while True:
        n += 1
        if host_bindable(host):
            if n > 1:
                print("=== a2a_launch 尾网就绪：%s 可绑（等了 %.0fs）===" % (host, time.time() - t0))
            return True
        waited = time.time() - t0
        if waited >= wait_s:
            print("=== a2a_launch 放弃：%s:%s 等了 %.0fs 仍不可绑"
                  "（尾网/地址未就绪，退出码 %d，等计划任务或哨兵重来）==="
                  % (host, port, waited, EXIT_BIND_TIMEOUT))
            return False
        print("=== a2a_launch 等待尾网：%s:%s 尚未就绪（第 %d 次，已等 %.0fs/%ds）==="
              % (host, port, n, waited, wait_s))
        time.sleep(BIND_POLL_S)


def open_log(log_path: str):
    """把 fd1/2 接管到日志文件；接管不了也不炸（宁可丢日志，不能拦启动）。"""
    if os.path.exists(log_path) and os.path.getsize(log_path) > MAX_LOG_BYTES:
        try:
            shutil.move(log_path, log_path + ".1")
        except OSError:
            pass
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        sys.stdout = log
        sys.stderr = log
        return log
    except OSError:
        return None


def main() -> int:
    cfg = load_env(ENV_FILE)
    log_path = cfg.get("A2A_LOG_FILE") or DEFAULT_LOG
    open_log(log_path)                      # 越早接管 stdout 越好：此后的每一句都留痕

    host = cfg.get("A2A_HOST", os.environ.get("AGENT_TEAM_A2A_HOST") or A2A_HOST_DEFAULT)
    try:
        port = int(cfg.get("A2A_PORT", "9920") or 0)
    except ValueError:
        port = 9920
    try:
        wait_s = int(os.environ.get("A2A_BIND_WAIT_S") or cfg.get("A2A_BIND_WAIT_S")
                     or DEFAULT_BIND_WAIT_S)
    except ValueError:
        wait_s = DEFAULT_BIND_WAIT_S

    print("=== a2a_launch 启动 %s (pid=%s) env=%s host=%s:%s ==="
          % (datetime.now().isoformat(timespec="seconds"), os.getpid(), ENV_FILE, host, port))

    if not wait_host_ready(host, port, wait_s):
        return EXIT_BIND_TIMEOUT

    argv = [
        "a2a_svc.py",
        "--host", host,
        "--port", str(port),
        "--config", cfg.get("A2A_AGENTS_JSON", os.path.join(RUNTIME_DIR, "agents.json")),
        "--peers", cfg.get("A2A_PEERS_JSON", os.path.join(RUNTIME_DIR, "peers.json")),
    ]
    for key in ("A2A_SESSIONS_JSON", "A2A_HERMES_STATE_DB", "A2A_HERMES_AGENT_DIR"):
        if cfg.get(key):
            os.environ[key] = cfg[key]

    from node.c import a2a_svc           # 批① 起被搬件按绝对包名 import（原：裸名 a2a_svc）
    rc = a2a_svc.main(argv[1:])
    print("=== a2a_launch 退出 rc=%s ===" % rc)
    return rc or 0


if __name__ == "__main__":
    raise SystemExit(main())
