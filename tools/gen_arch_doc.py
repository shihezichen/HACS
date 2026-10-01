#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终态架构文档生成器（刀32 · 阶段6 收尾件）。

用法：
    python tools/gen_arch_doc.py          # 无参数，路径全写死为仓内相对路径

干什么
------
把 `tools/arch_doc_template.md`（正文唯一出处，人改文案只改它）里的占位符替换成
**实时取到的数字**，写出 `docs/architecture.md`（本仓的架构文档真位置）。
源码仓里本件写的是内部完整版 `agent-team/docs/architecture-final.md`，再由
`tools/hacs_manifest.json` + 脱敏器 `tools/sanitizers/generic_deploy.py` 映射成对外版；
本仓（对外仓）没有那两件，产物就只有 `docs/architecture.md` 这一份。

三条纪律
--------
① **只做占位符替换**，一个字的正文都不在 Python 里拼 —— 正文改动一律改模板。
② **不许静默留坑**：替换完还剩 `{{` 即视为占位符漏实现，退出码 2。
③ **幂等**：同一 HEAD 连跑两次，产物 md5 必须相同 —— 故不写时间戳、不写随机数、
   不写绝对路径（`NK_DATE` 取的是 HEAD 的提交日期，不是墙钟）。

占位符
------
    {{NK_FILES}}        件数        ← 实时跑 tools/check_layers.py，解析表头行
    {{NK_EDGES}}        层间 import 边 ← 同上
    {{NK_PORTS}}        登记端口个数  ← tools/hacs_layers.json 的 ports 键数（排除 _readme）
    {{NK_LAYER_TABLE}}  层 | 职责 | 件数 的 markdown 表 ← 由 layers + files 现算
    {{NK_HEAD}}         git rev-parse --short HEAD
    {{NK_DATE}}         git log -1 --date=short --format=%ad

退出码：0 成功 / 2 取数或替换失败。
纯标准库，无第三方依赖。
"""
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

TEMPLATE = os.path.join(HERE, "arch_doc_template.md")
LAYERS_JSON = os.path.join(HERE, "hacs_layers.json")
CHECK_LAYERS = os.path.join(HERE, "check_layers.py")
OUT = os.path.join(REPO, "docs", "architecture.md")

#: check_layers.py 的表头行形态：`分层门禁 · 台账（件数 82，层间 import 边 108）`
HEADER_RX = re.compile(r"件数\s*(\d+)\s*[，,]\s*层间\s*import\s*边\s*(\d+)")


def die(msg, raw=None):
    """取数或替换失败 —— 退出码 2，并把原文打出来给人看（不静默、不猜）。"""
    sys.stderr.write("✗ gen_arch_doc：%s\n" % msg)
    if raw is not None:
        sys.stderr.write("---- 原文 ----\n%s\n---- 原文结束 ----\n" % raw)
    raise SystemExit(2)


def run(argv):
    """跑子进程，回 (退出码, stdout, stderr)。

    强制子进程以 UTF-8 输出（Windows 控制台默认 GBK，管道里会按 locale 编码），
    这样解析口径与平台无关。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.Popen(argv, cwd=REPO, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        die("起不来子进程 %r：%s" % (argv, exc))
    out, err = proc.communicate()
    return (proc.returncode,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"))


def guard_numbers():
    """{{NK_FILES}} / {{NK_EDGES}} —— 实时跑分层门禁，从表头行解析。"""
    rc, out, err = run([sys.executable, CHECK_LAYERS])
    m = HEADER_RX.search(out)
    if not m:
        die("check_layers.py 的表头行解析不出「件数 N，层间 import 边 M」（退出码 %d）"
            % rc, raw=(out + err))
    return m.group(1), m.group(2)


def git_head():
    """{{NK_HEAD}} / {{NK_DATE}} —— HEAD 的短 SHA 与提交日期（不是墙钟）。"""
    rc, out, err = run(["git", "rev-parse", "--short", "HEAD"])
    if rc != 0 or not out.strip():
        die("git rev-parse --short HEAD 失败（退出码 %d）" % rc, raw=(out + err))
    head = out.strip().splitlines()[0].strip()

    rc, out, err = run(["git", "log", "-1", "--date=short", "--format=%ad"])
    if rc != 0 or not out.strip():
        die("git log -1 --date=short --format=%%ad 失败（退出码 %d）" % rc, raw=(out + err))
    date = out.strip().splitlines()[0].strip()
    return head, date


def load_model():
    try:
        return json.load(io.open(LAYERS_JSON, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        die("读不动层归属表 %s：%s" % (LAYERS_JSON, exc))


def port_count(model):
    """{{NK_PORTS}} —— ports 的键数，排除说明键 _readme。"""
    ports = model.get("ports")
    if not isinstance(ports, dict):
        die("层归属表里没有 ports 表（或形态不对）")
    return str(len([k for k in ports if k != "_readme"]))


def layer_table(model):
    """{{NK_LAYER_TABLE}} —— 按 layers 的声明顺序出 markdown 表；件数由 files 现算。"""
    layers = model.get("layers")
    files = model.get("files")
    if not isinstance(layers, dict) or not isinstance(files, dict):
        die("层归属表里没有 layers / files（或形态不对）")

    counts = {}
    for path, meta in files.items():
        code = (meta or {}).get("layer")
        if not code:
            die("层归属表里 %s 没写 layer" % path)
        counts[code] = counts.get(code, 0) + 1

    unknown = sorted(set(counts) - set(layers))
    if unknown:
        die("files 里出现 layers 未声明的层：%s" % "、".join(unknown))

    rows = ["| 层 | 一句话职责 | 件数 |", "| --- | --- | --- |"]
    for code, duty in layers.items():
        rows.append("| %s | %s | %d |" % (code, duty, counts.get(code, 0)))
    return "\n".join(rows)


def main():
    if not os.path.isfile(TEMPLATE):
        die("找不到模板 %s" % TEMPLATE)
    text = io.open(TEMPLATE, encoding="utf-8", newline=None).read()

    model = load_model()
    files, edges = guard_numbers()
    head, date = git_head()

    values = {
        "NK_FILES": files,
        "NK_EDGES": edges,
        "NK_PORTS": port_count(model),
        "NK_LAYER_TABLE": layer_table(model),
        "NK_HEAD": head,
        "NK_DATE": date,
    }

    declared = set(re.findall(r"\{\{([A-Z_]+)\}\}", text))
    missing = sorted(declared - set(values))
    if missing:
        die("模板里有本生成器没实现的占位符：%s" % "、".join(missing))

    for key, val in values.items():
        text = text.replace("{{%s}}" % key, val)

    if "{{" in text:
        leftovers = sorted(set(re.findall(r"\{\{[^}]*\}\}", text)))
        die("替换后仍残留占位符：%s" % "、".join(leftovers))

    out_dir = os.path.dirname(OUT)
    if not os.path.isdir(out_dir):
        die("输出目录不存在：%s" % out_dir)
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(text)

    print("✓ 已生成 docs/architecture.md")
    print("  件数 %s · 层间 import 边 %s · 登记端口 %s 个 · HEAD %s（%s）"
          % (values["NK_FILES"], values["NK_EDGES"], values["NK_PORTS"], head, date))
    print("  字节 %d · 行 %d" % (len(text.encode("utf-8")), text.count("\n")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
