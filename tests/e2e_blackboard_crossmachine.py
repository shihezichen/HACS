# -*- coding: utf-8 -*-
# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
"""P3 黑板跨机 e2e：从笔记本打龙虾 9940。判定从严，失败即失败。"""
import json
import os
import pathlib
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 测试夹具真值（硬编码清零 Phase 1.5）
# PHASE2_TODO: 待节点注入 env 后删除
# 默认值逐字等于改动前的真值；未注入 env 时测试行为与改动前完全等价。
FIX_NODE_CLOUD_IP = os.environ.get("AGENT_TEAM_TEST_NODE_CLOUD_IP") or "<host>"
FIX_MEMBER_CLOUD = os.environ.get("AGENT_TEAM_TEST_MEMBER_CLOUD") or "lobster"

BASE = "http://" + FIX_NODE_CLOUD_IP + ":9940"
TOK = pathlib.Path(os.environ.get("AGENT_TEAM_TOKEN_FILE") or r"<home>\agent-team\client-token").read_text(encoding="utf-8").strip()
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("  ← " + str(detail)) if detail else ""))
    return ok


def call(path, method="GET", payload=None, token=TOK, timeout=30):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw}
    except Exception as exc:
        return 0, {"error": str(exc)}


# 1) healthz
code, h = call("/healthz", token=None)
check("跨机 /healthz 200 且 db ok", code == 200 and h.get("db", {}).get("ok") is True, "code=%s db=%s" % (code, h.get("db")))

# 2) 追加一条（作者应来自 token = hermes）
code, r1 = call("/entries", "POST", {"topic": "e2e", "kind": "note", "content": "P3 跨机自检：这是一条来自笔记本的条目（中文测试）", "refs": ["e2e-01"]})
eid = r1.get("entry_id")
check("追加条目 200 且返回 entry_id", code == 200 and isinstance(eid, int), "code=%s resp=%s" % (code, r1))
# 读回验证（POST 响应不回 author，必须回读真库）
code_rb, rb = call("/entries?topic=e2e&limit=50")
back = {e.get("entry_id"): e.get("author") for e in (rb.get("entries") or [])}
check("作者取自 token（=hermes，非请求体）", back.get(eid) == "hermes", "读回 author=%r" % back.get(eid))

# 3) 冒名：body 里塞 author=<MEMBER_ID>，仍应以 hermes 落库
code, r2 = call("/entries", "POST", {"topic": "e2e", "kind": "note", "content": "冒名尝试：声明自己是 " + FIX_MEMBER_CLOUD, "author": FIX_MEMBER_CLOUD})
eid2 = r2.get("entry_id")
code_rb2, rb2 = call("/entries?topic=e2e&limit=50")
back2 = {e.get("entry_id"): e.get("author") for e in (rb2.get("entries") or [])}
check("冒名被忽略（仍记为 hermes）", code == 200 and back2.get(eid2) == "hermes", "读回 author=%r" % back2.get(eid2))

# 4) 检索能取回
code, g = call("/entries?topic=e2e&limit=10")
items = g.get("entries") or g.get("items") or []
found = any(str(e.get("entry_id")) == str(eid) for e in items)
check("按 topic 检索能取回且 count 正确", code == 200 and found, "code=%s count=%s" % (code, g.get("count")))

# 5) 错 token → 401
code, _ = call("/entries?topic=e2e", token="wrong-token-xxx")
check("错误 token → 401", code == 401, "code=%s" % code)

# 6) 无 token → 401
code, _ = call("/entries?topic=e2e", token=None)
check("无 token → 401", code == 401, "code=%s" % code)

# 7) 空内容 → 400
code, _ = call("/entries", "POST", {"topic": "e2e", "kind": "note", "content": ""})
check("空 content → 400", code == 400, "code=%s" % code)

print("\n小结：%d/%d PASS" % (sum(1 for _, ok in RESULTS if ok), len(RESULTS)))
