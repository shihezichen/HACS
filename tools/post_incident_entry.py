# -*- coding: utf-8 -*-
"""按 append-only 口径，把本次事故与修复作为一条新条目追加进黑板（不改旧条目）。"""
import json
import pathlib
import urllib.request

BASE = "http://<host>:9940"
TOK = pathlib.Path(r"<deploy-dir>\client-token").read_text(encoding="utf-8").strip()

CONTENT = (
    "【事故与修复记录】2026-09-19 P4 验证阶段，Hermes 总控在真表上跑了一条破坏性 DDL 当"
    "“护栏测试”（ALTER TABLE blackboard_entries DROP COLUMN content），本意是验证 append-only "
    "能不能挡住结构变更 —— append-only 触发器只管 DML（UPDATE/DELETE/TRUNCATE），DDL 不在保护范围，"
    "content 列被真删，5 条历史条目正文丢失。已修复：①列已加回并恢复 NOT NULL 约束；"
    "②5 条正文按原始来源逐字还原（entry 1 取自上线时的原始 INSERT，entry 2/4、3/5 取自 e2e 脚本原文），"
    "0 空白；③向量与正文一致（向量是在正文存在时算的，还原后逐字相同）。"
    "教训三条：①黑板保证只覆盖数据不覆盖结构，改结构必须走 owner 复核；"
    "②验证护栏绝不能在真表上跑破坏性语句；③当时库无备份、WAL 未归档 —— 已补每日 pg_dump。"
)

payload = {
    "topic": "infra",
    "kind": "incident",
    "content": CONTENT,
    "refs": ["1", "2", "3", "4", "5"],
}
req = urllib.request.Request(BASE + "/entries", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST")
req.add_header("Content-Type", "application/json; charset=utf-8")
req.add_header("Authorization", "Bearer " + TOK)
with urllib.request.urlopen(req, timeout=30) as resp:
    print("HTTP", resp.status, resp.read().decode("utf-8"))
