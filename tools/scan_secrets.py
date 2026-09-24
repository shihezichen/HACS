#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HACS 提交前密钥扫描 —— 硬门禁。

用法：
    python tools/scan_secrets.py .              # 扫全仓
    python tools/scan_secrets.py frontend/ui/   # 扫指定目录（ui/ 一视同仁，不享豁免）

退出码：0 = 干净可提交；1 = 命中，禁止提交。

设计口径：
  * 两路一起扫 —— ①已知凭证指纹（token 前缀、私钥头、连接串）；②通用「赋值即口令」模式。
  * 二进制/大文件按扩展名跳过，但跳过即报「跳过」不算通过。
  * 只报文件名 + 行号 + 命中种类的掩码，**不回显命中的原文**（避免扫描日志自己变成泄漏源）。
"""
from __future__ import annotations

import os
import re
import sys

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".pytest_cache"}
SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tgz",
    ".7z", ".whl", ".bin", ".gguf", ".safetensors", ".onnx", ".mp4", ".mov", ".woff",
    ".woff2", ".ttf", ".eot", ".so", ".dll", ".exe", ".pyc", ".sqlite", ".db",
}
MAX_BYTES = 2 * 1024 * 1024

# 路 1：已知凭证指纹 —— (名称, 正则)
FINGERPRINTS = [
    ("github-token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("aws-access-key", re.compile(r"\b(AKIA|LTAI)[A-Za-z0-9]{12,}\b")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("feishu-app-secret", re.compile(r"\bcli_[a-z0-9]{16,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("pg-dsn-with-password", re.compile(r"postgres(?:ql)?://[^/\s:@]+:[^/\s:@]+@")),
    ("url-basic-auth", re.compile(r"https?://[^/\s:@]+:[^/\s:@]+@")),
    ("bearer-literal", re.compile(r"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._\-]{16,}")),
]

# 路 2：通用「键名像口令 + 值是字面量」
SECRETISH = re.compile(
    r"(?i)\b([a-z0-9_]*"
    r"(?:pass(?:wd|word)?|secret|token|api[_-]?key|access[_-]?key|credential|private[_-]?key)"
    r"[a-z0-9_]*)\b\s*[:=]\s*(.+)"
)
PLACEHOLDER = re.compile(
    r"""(?xi)
    ^\s*$
    | ^\s*[\"']?\s*[\"']?\s*$                 # 空串
    | [\"']?\$\{[A-Z0-9_]+\}[\"']?            # ${ENV} 或 "${ENV}"
    | [\"']?\$\{?[A-Za-z_][A-Za-z0-9_]*\}?    # $VAR 或 "$VAR"
    | ^\s*[\"']?(?:<[^>]+>|xxx+|todo|fixme|changeme|placeholder|example|redacted|none|null|true|false|\*{3,})[\"']?\s*,?\s*$
    | os\.(?:environ|getenv)                  # 从环境读
    | getenv\(|environ\[
    """,
)
# 键名后缀判定：避免把 `token_budget: 4096` 这类误报
# 词边界口径：`CTX_MIN_TOKENS_FLOOR` 里的 TOKENS 不算「口令类键名」，
# 但 `api_key` / `token` / `passwd` / `secret_key` 这类独立词仍要拦。
KEY_HINT = re.compile(
    r"(?i)(?<![a-z])(?:pass(?:wd|word)?|secret|token|api[_-]?key|access[_-]?key|credential|private[_-]?key)(?![a-z])"
)
# 只有「字面量形态」的值才可能是硬编码凭据：引号包起来、裸标识符、或数字开头。
# 表达式/取下标/函数调用（`header[7:].strip()`、`os.environ[...]`、`str(agent.get(...))`）是运行时值，放过。
LITERALISH = re.compile(
    r"^(?:"
    r"[\"'].*[\"']\s*,?\s*$|"        # 引号字面量（整串）
    r"[A-Za-z_][A-Za-z0-9_]*\s*,?\s*$|"  # 裸标识符（整串）
    r"\d"                                 # 数字开头
    r")"
)
NUMERICISH = re.compile(r"^[\"']?\d+(?:[\.,]\d+)?[\"']?\s*,?\s*$")
#: 值若本身就是「全大写常量名」（`token = A2A_TOKEN`），说明是源码里引用另一个变量，
#: 不是硬编码凭据 —— 放过。真凭据通常带引号、或混合大小写/数字/符号。
CONSTNAME = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

# 允许清单：路径前缀（相对仓库根）或文件名后缀
ALLOW_PATHS = (
    "tools/scan_secrets.py",   # 本文件自身含正则样本
    ".env.example",            # 样板，只允许占位（按后缀匹配，`xxx.env.example` 也算）
    "docs/",
)

#: 测试夹具标记：文件里出现它，则跳过「路 2 赋值即口令」判定。
#: 测试必须用真形态的假凭据（`TOKEN = "unit-test-token"`）才测得准，这类字面量无法与
#: 真凭据自动区分，故要求文件显式声明；**路 1 指纹（真 token 前缀 / 私钥头 / 连接串）照扫不误**。
FIXTURE_MARK = "hacs-test-fixtures"


def _is_allowed(rel: str) -> bool:
    rel = rel.replace(os.sep, "/")
    base = rel.rsplit("/", 1)[-1]
    return any(rel == p or rel.startswith(p) or base.endswith(p) for p in ALLOW_PATHS)


def _mask(hit: str) -> str:
    hit = hit.strip()
    if len(hit) <= 6:
        return "*" * len(hit)
    return "%s…%s（len=%d）" % (hit[:3], hit[-2:], len(hit))


def scan(root: str):
    findings = []
    scanned = skipped = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            ext = os.path.splitext(name)[1].lower()
            if ext in SKIP_EXT:
                skipped += 1
                continue
            try:
                if os.path.getsize(full) > MAX_BYTES:
                    skipped += 1
                    continue
                with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                    lines = fh.read().splitlines()
                fixture = any(FIXTURE_MARK in ln for ln in lines)
            except OSError:
                skipped += 1
                continue
            scanned += 1
            if _is_allowed(rel):
                # 允许清单也要过路 1 指纹（样板里出现真 token 照样拦）；
                # 但 `<user>:<pass>@` 这类尖括号占位是样板本身，不算命中。
                for idx, line in enumerate(lines, 1):
                    for label, rx in FINGERPRINTS:
                        m = rx.search(line)
                        if not m:
                            continue
                        if "<" in m.group(0) or PLACEHOLDER.search(line):
                            continue
                        findings.append((rel, idx, label, _mask(m.group(0))))
                continue
            for idx, line in enumerate(lines, 1):
                for label, rx in FINGERPRINTS:
                    m = rx.search(line)
                    if m:
                        findings.append((rel, idx, label, _mask(m.group(0))))
                        break
                else:
                    if fixture:
                        continue   # 测试夹具：跳过「赋值即口令」（假凭据），路 1 指纹已照扫
                    m = SECRETISH.match(line.strip())
                    if m and KEY_HINT.search(m.group(1)):
                        value = m.group(2).strip().rstrip(",")
                        value = re.sub(r"\s+#.*$", "", value).strip()   # 剥行尾注释
                        if not PLACEHOLDER.match(value) and not NUMERICISH.match(value) \
                                and not CONSTNAME.match(value) and LITERALISH.match(value):
                            findings.append((rel, idx, "literal-assignment:%s" % m.group(1), _mask(value)))
    return findings, scanned, skipped


def main(argv):
    root = argv[1] if len(argv) > 1 else "."
    if not os.path.exists(root):
        print("路径不存在：%s" % root)
        return 2
    findings, scanned, skipped = scan(root)
    print("扫描根：%s" % os.path.abspath(root))
    print("已扫文本文件：%d 个；按扩展名/体积跳过：%d 个" % (scanned, skipped))
    if not findings:
        print("结果：干净，可提交。")
        return 0
    print("结果：命中 %d 处，禁止提交。" % len(findings))
    for rel, idx, label, masked in findings:
        print("  %s:%d  [%s]  %s" % (rel, idx, label, masked))
    print("处置：改 ${ENV} 占位 / 移出仓 / 加 .gitignore 后重跑。")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
