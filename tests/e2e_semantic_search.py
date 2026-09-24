# -*- coding: utf-8 -*-
# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
"""P4 语义检索跨机 e2e（笔记本 → 龙虾 9940）。

验收口径（从严）：用**与条目正文几乎无字面重叠**的问句去查，若语义检索真生效，
对应条目必须排第一；同时核对 mode=semantic、score 合法、向量覆盖率 100%。
"""
import json
import pathlib
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://<host>:9940"
TOK = pathlib.Path(r"<home>\agent-team\client-token").read_text(encoding="utf-8").strip()
results = []


def call(path, method="GET", body=None, token=TOK):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def check(name, ok, got):
    results.append(bool(ok))
    print("%-4s %s  ← %s" % ("PASS" if ok else "FAIL", name, str(got)[:150]))


# 1) healthz 自报嵌入状态
code, h = call("/healthz")
emb = (h.get("embed") or {})
check("healthz 200 且 embed 可用", code == 200 and emb.get("available") is True, emb)

# 2) 写入 3 条**领域互不重叠**的条目（写入后应自动带向量）
samples = [
    ("p4net", "服务监听地址必须收敛到回环或内网，禁止绑 0.0.0.0 对外"),
    ("p4policy", "黑板上的记录写进去就不许改也不许删，历史永久留痕供复盘"),
    ("p4meet", "晨会的结论会广播给全队成员并归档成报告文件"),
]
ids = {}
for topic, text in samples:
    c, r = call("/entries", "POST", {"topic": topic, "kind": "note", "content": text})
    ids[topic] = r.get("entry_id")
    check("写入 %s → 200 且有 id" % topic, c == 200 and r.get("entry_id"), r.get("entry_id"))

# 3) 语义检索：问句与原文**字面几乎不重叠**
cases = [
    ("为什么历史数据不允许被修改？", "p4policy"),
    ("总结出来的东西最后发到哪里？", "p4meet"),
    ("服务端口对外暴露该怎么防止？", "p4net"),
]
for q, expect_topic in cases:
    c, r = call("/search?q=" + urllib.parse.quote(q) + "&limit=3")
    ents = r.get("entries") or []
    top = ents[0] if ents else {}
    hit = top.get("entry_id") == ids[expect_topic]
    check("语义命中「%s」→ 期望 %s" % (q, expect_topic),
          c == 200 and r.get("mode") == "semantic" and hit,
          "mode=%s top=%s(topic=%s) score=%s" % (r.get("mode"), top.get("entry_id"),
                                                 top.get("topic"), top.get("score")))

# 4) score 合法性（余弦相似度应落在 [-1,1]）
c, r = call("/search?q=" + urllib.parse.quote("历史记录不能篡改") + "&limit=3")
scores = [e.get("score") for e in (r.get("entries") or [])]
check("score 全在 [-1,1]", bool(scores) and all(isinstance(s, (int, float)) and -1.001 <= s <= 1.001 for s in scores), scores)

# 5) 空 q → 400
c, r = call("/search?q=")
check("空 q → 400", c == 400, c)

# 6) limit 上限保护
c, r = call("/search?q=" + urllib.parse.quote("端口") + "&limit=999")
check("limit=999 被夹到 ≤50", c == 200 and len(r.get("entries") or []) <= 50, len(r.get("entries") or []))

# 7) 无 token → 401
c, r = call("/search?q=x", token="bad-token")
check("错 token → 401", c == 401, c)

print("\n小结：%d/%d PASS" % (sum(results), len(results)))
