# -*- coding: utf-8 -*-
"""刀34 · chat_svc 静态面「备份 / 临时件一律拦」的**等价-增严**矩阵（可复跑）。

用法：
    python tools/equivalence/chat_svc_static_filter_equiv.py

本刀改的是 `agent-team/chat_svc.py` 里 `_serve_static()` 与 `_serve_admin()` 的
`blocked` 判据 —— 各加一条「任一路径段命中 `STATIC_BACKUP_RE` 即拦」。
增严的理由写在该常量处：`tools/deploy.py` 部署第 1 步留下的 `<path>.bak-<ts>` 备份
**就地**落在静态根 `ui/` 里（实测 lobster:<deploy-dir>/ui/ 48 件里有
`group.html.bak-*` / `group.css.bak-*` / `codeblock.js.bak-inline-` /
`calc.html.bak-vN-*` 四类），既有的 `STATIC_EXCLUDE`（逐件名单，只有
serve.py / mock.js）与隐藏件判定（只认 `.` 开头）**都拦不住**，于是历史版本被当
静态件 serve 了出去。

**为什么另起一个装置而不是改 `chat_svc_http_equiv.py`**：那个装置有两条冻结基线
（`_old/chat_svc_old.py` = 刀8a 之前、`_old/chat_svc_old_8b.py` = 刀8a 收尾），
它们连同上面那 300 多例是刀8 的证据，一个字不能动；本刀的「改前」是刀33 收尾的
工作树，另冻一份 `_old/chat_svc_old_k34.py`（**新名，不覆盖任何既有 `_old/*`**）。
**装置骨架不重造**：假库 / 假名册 / 假鉴权 / 起服务 / 发请求全部**直接 import 复用**
`chat_svc_http_equiv` 里那一套（见下方 `import chat_svc_http_equiv as base`），
本件只贡献「静态面用例表 + 等价-增严判据」。

判据（这是本装置与普通等价装置唯一的不同）：
  · 同一张用例表喂**改前 / 改后**两个实例；
  · 表里每格预先标注期望：`same` = 两侧必须**逐字节相同**（状态码 + 状态原因 +
    **有序**响应头 + 正文）；`tighten` = 改前**命中**（200 且有正文）、改后**拦**
    （404 且 `error.code == "not_found"`）；
  · 只要出现「`same` 格不一致」或「`tighten` 格没按 200→404 走」就非 0 退出。
  · 反方向（放宽：改前拦、改后命中）在任何一格出现都直接判负 —— `tighten` 格断言的是
    **确定的方向**，`same` 格断言的是**逐字节不动**，两者合起来把「只许收紧」钉死。

覆盖面（≥30 格，实际见收尾统计）：
  · 正控：`ui/group.html`、`ui/group.css`、`ui/md.js`、`/` → group.html、
    子目录 index、MIME 钉死的 css/js、二进制 png、无扩展名未命中 → SPA fallback；
    外加**形近但不该拦**的一组（`backup.html` / `bak.html` / `a.bakery.js` /
    `orig.js` / `tmp.js` / `a.pre.js`）—— 这组是「判据没有过宽」的证据；
  · 负控（本刀新拦）：`.bak-<ts>` / `.bak-inline-` / `.bak` / `.bak.js` / `.bak_1` /
    `.tmp` / `~` / `.orig` / `.rej` / `.swp` / `.pre-*`，含**子目录件**与**目录段**命中；
  · 既有判据（一格不许动）：隐藏件 `.hidden`、排除表 `serve.py` / `mock.js`、
    目录穿越 `/../app.json` 与 `/../../etc/passwd`、带扩展名未命中 → 404、
    写方法 → 405；
  · `/calc/admin` 面同样跑一遍（`_serve_admin` 与 `_serve_static` 是两处判据，
    只改一处的话这一组会当场变红）。

退出码：0 = 全部按期望（等价 + 定向增严），1 = 有格不合期望，2 = 装置自身没跑成。
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
AG = os.path.join(REPO, "agent-team")
OLD = os.path.join(HERE, "_old")
OLD_FILE = os.path.join(OLD, "chat_svc_old_k34.py")

sys.path[:0] = [HERE]
import chat_svc_http_equiv as base                                 # noqa: E402

SAME, TIGHTEN = "same", "tighten"

# ------------------------------------------------------------------ 静态根（确定性）
#
# 内容全部写死；两侧**共用同一棵树**（不是各造一棵）—— 否则比的是两棵树而不是两份判据。

#: 正控件：名字形近备份件但**不该**被拦 —— 判据过宽的话这一组会当场变红。
LOOKALIKE = {
    "backup.html": b"<html>backup word in name</html>\n",
    "bak.html": b"<html>bak prefix, no dot</html>\n",
    "a.bakery.js": b"console.log('bakery')\n",
    "orig.js": b"console.log('orig')\n",
    "tmp.js": b"console.log('tmp')\n",
    "a.pre.js": b"console.log('pre dot, no hyphen')\n",
    "swap.css": b".swap{}\n",
    "rejoin.js": b"console.log('rejoin')\n",
}

#: 负控件：本刀要拦的形状。取值就是线上 `ui/` 里实见的那几类 + 判据里其余分支各一。
BACKUPS = {
    "group.html.bak-20260926-155258": b"<html>old group page</html>\n",
    "group.css.bak-20260901-120000": b".old{color:red}\n",
    "codeblock.js.bak-inline-": b"console.log('old codeblock')\n",
    "calc.html.bak-v1-openclaw-20260924": b"<html>old calc</html>\n",
    "a.tmp": b"tmp payload\n",
    "a~": b"editor leftover\n",
    "x.orig": b"merge leftover\n",
    "x.rej": b"merge reject\n",
    "x.swp": b"vim swap\n",
    "x.bak": b"plain bak\n",
    "x.bak.js": b"console.log('bak dot ext')\n",
    "x.bak_1": b"bak underscore\n",
    "x.pre-k34": b"pre-hyphen leftover\n",
}


def make_tree(root):
    """造 `<root>/ui`（静态根）、`<root>/admin-ui`（后台根）与 `<root>/app.json`。

    `app.json` 落在**静态根之外、上一级**：`/../app.json` 那两格穿越用例要的就是
    「目标真实存在，但在根外」—— 目标不存在的话那一格比的是「都 404」，
    验不到穿越判据本身。
    """
    ui = os.path.join(root, "ui")
    admin = os.path.join(root, "admin-ui")
    for path in (os.path.join(ui, "sub"), os.path.join(ui, "bakdir.bak-1"),
                 os.path.join(admin, "assets")):
        os.makedirs(path, exist_ok=True)

    io.open(os.path.join(root, "app.json"), "wb").write(b'{"root":"outside-ui"}\n')

    # --- 静态根 ui/ ---
    io.open(os.path.join(ui, "group.html"), "wb").write(
        b"<!doctype html><html><head><meta charset=utf-8><title>group</title></head>"
        b"<body><div id=app>\xe7\xbe\xa4\xe9\xa1\xb5\xe9\x9d\xa2</div></body></html>\n")
    io.open(os.path.join(ui, "group.css"), "wb").write(b"body{margin:0}\n")
    io.open(os.path.join(ui, "md.js"), "wb").write(b"export function render(){}\n")
    io.open(os.path.join(ui, "calc.html"), "wb").write(b"<html>calc</html>\n")
    io.open(os.path.join(ui, "a.png"), "wb").write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    io.open(os.path.join(ui, "serve.py"), "wb").write(b"print(1)\n")
    io.open(os.path.join(ui, "mock.js"), "wb").write(b"export const mock = 1\n")
    io.open(os.path.join(ui, ".hidden"), "wb").write(b"secret\n")
    io.open(os.path.join(ui, "sub", "group.html"), "wb").write(b"<html>sub</html>\n")
    io.open(os.path.join(ui, "sub", "nested.html.bak-1"), "wb").write(b"<html>old sub</html>\n")
    io.open(os.path.join(ui, "bakdir.bak-1", "inner.html"), "wb").write(b"<html>inner</html>\n")
    for name, blob in list(LOOKALIKE.items()) + list(BACKUPS.items()):
        io.open(os.path.join(ui, name), "wb").write(blob)

    # --- 后台根 admin-ui/（`_serve_admin` 是第二处判据，必须单独覆盖）---
    io.open(os.path.join(admin, "index.html"), "wb").write(b"<html>admin</html>\n")
    io.open(os.path.join(admin, "keep.js"), "wb").write(b"console.log('keep')\n")
    io.open(os.path.join(admin, ".hidden"), "wb").write(b"secret\n")
    io.open(os.path.join(admin, "a.tmp"), "wb").write(b"tmp\n")
    io.open(os.path.join(admin, "a~"), "wb").write(b"leftover\n")
    io.open(os.path.join(admin, "index.html.bak-20260101-000000"), "wb").write(
        b"<html>old admin</html>\n")
    io.open(os.path.join(admin, "assets", "app.js"), "wb").write(b"console.log('app')\n")
    io.open(os.path.join(admin, "assets", "app.js.bak-inline-"), "wb").write(
        b"console.log('old app')\n")
    return ui, admin


# ------------------------------------------------------------------ 用例表
#
# (用例名, 方法, 路径, 期望)   期望 ∈ {SAME, TIGHTEN}

CASES_STATIC = [
    # --- 正控：本刀一格不许动 ---
    ("P01 群聊前端页",            "GET",  "/group.html", SAME),
    ("P02 群聊样式表",            "GET",  "/group.css", SAME),
    ("P03 Markdown 渲染器",       "GET",  "/md.js", SAME),
    ("P04 计算器前台页",          "GET",  "/calc.html", SAME),
    ("P05 二进制 png",            "GET",  "/a.png", SAME),
    ("P06 根 → group.html",       "GET",  "/", SAME),
    ("P07 子目录 index",          "GET",  "/sub", SAME),
    ("P08 子目录 尾斜杠",          "GET",  "/sub/", SAME),
    ("P09 子目录内件",            "GET",  "/sub/group.html", SAME),
    ("P10 SPA 兜底 无扩展名",      "GET",  "/nope", SAME),
    ("P11 SPA 兜底 多级路径",      "GET",  "/g/abc/def", SAME),
    ("P12 HEAD 群聊前端页",        "HEAD", "/group.html", SAME),
    ("P13 HEAD 根页",             "HEAD", "/", SAME),
    # --- 正控 · 形近但不该拦（判据没有过宽的证据）---
    ("N01 backup.html 形近",      "GET",  "/backup.html", SAME),
    ("N02 bak.html 形近",         "GET",  "/bak.html", SAME),
    ("N03 a.bakery.js 形近",      "GET",  "/a.bakery.js", SAME),
    ("N04 orig.js 形近",          "GET",  "/orig.js", SAME),
    ("N05 tmp.js 形近",           "GET",  "/tmp.js", SAME),
    ("N06 a.pre.js 点无连字符",     "GET",  "/a.pre.js", SAME),
    ("N07 swap.css 形近",         "GET",  "/swap.css", SAME),
    ("N08 rejoin.js 形近",        "GET",  "/rejoin.js", SAME),
    # --- 负控：本刀新拦（命中 → 拦）---
    ("B01 group.html.bak-<ts>",   "GET",  "/group.html.bak-20260926-155258", TIGHTEN),
    ("B02 group.css.bak-<ts>",    "GET",  "/group.css.bak-20260901-120000", TIGHTEN),
    ("B03 codeblock.js.bak-inline-", "GET", "/codeblock.js.bak-inline-", TIGHTEN),
    ("B04 calc.html.bak-v1-...",  "GET",  "/calc.html.bak-v1-openclaw-20260924", TIGHTEN),
    ("B05 a.tmp 临时件",           "GET",  "/a.tmp", TIGHTEN),
    ("B06 a~ 编辑器残留",           "GET",  "/a~", TIGHTEN),
    ("B07 x.orig 合并残留",         "GET",  "/x.orig", TIGHTEN),
    ("B08 x.rej 合并残片",          "GET",  "/x.rej", TIGHTEN),
    ("B09 x.swp vim 交换件",        "GET",  "/x.swp", TIGHTEN),
    ("B10 x.bak 裸 .bak",          "GET",  "/x.bak", TIGHTEN),
    ("B11 x.bak.js .bak 后带点",    "GET",  "/x.bak.js", TIGHTEN),
    ("B12 x.bak_1 .bak 后带下划线",  "GET",  "/x.bak_1", TIGHTEN),
    ("B13 x.pre-k34 预备件",        "GET",  "/x.pre-k34", TIGHTEN),
    ("B14 子目录内备份件",           "GET",  "/sub/nested.html.bak-1", TIGHTEN),
    ("B15 目录段命中（件本身正常）",   "GET",  "/bakdir.bak-1/inner.html", TIGHTEN),
    ("B16 HEAD 备份件",            "HEAD", "/group.html.bak-20260926-155258", TIGHTEN),
    ("B17 备份件带查询串",           "GET",  "/a.tmp?v=1", TIGHTEN),
    # --- 既有判据：一格不许动 ---
    ("E01 隐藏件 .hidden",         "GET",  "/.hidden", SAME),
    ("E02 排除表 serve.py",        "GET",  "/serve.py", SAME),
    ("E03 排除表 mock.js",         "GET",  "/mock.js", SAME),
    ("E04 目录穿越 ../app.json",    "GET",  "/../app.json", SAME),
    ("E05 目录穿越 ../../etc/passwd", "GET", "/../../etc/passwd", SAME),
    ("E06 带扩展名未命中 → 404",     "GET",  "/nope.js", SAME),
    ("E07 带扩展名未命中 css",       "GET",  "/nope.css", SAME),
    ("E08 写方法 → 405",           "PUT",  "/group.html", SAME),
    ("E09 写方法打备份件 → 405",     "PUT",  "/a.tmp", SAME),
    ("E10 写方法打根 → 405",        "POST", "/", SAME),
]

CASES_ADMIN = [
    ("A01 后台首页（精确）",        "GET",  "/calc/admin", SAME),
    ("A02 后台首页 直取",          "GET",  "/calc/admin/index.html", SAME),
    ("A03 后台资源 app.js",        "GET",  "/calc/admin/assets/app.js", SAME),
    ("A04 后台 keep.js",          "GET",  "/calc/admin/keep.js", SAME),
    ("A05 后台 SPA 兜底",          "GET",  "/calc/admin/nope", SAME),
    ("A06 后台 带扩展名未命中",      "GET",  "/calc/admin/nope.js", SAME),
    ("A07 后台 隐藏件",            "GET",  "/calc/admin/.hidden", SAME),
    ("A08 后台 目录穿越",          "GET",  "/calc/admin/../../app.json", SAME),
    ("A09 HEAD 后台首页",          "HEAD", "/calc/admin", SAME),
    ("A10 后台 index.html.bak-<ts>", "GET", "/calc/admin/index.html.bak-20260101-000000", TIGHTEN),
    ("A11 后台 app.js.bak-inline-", "GET", "/calc/admin/assets/app.js.bak-inline-", TIGHTEN),
    ("A12 后台 a.tmp",            "GET",  "/calc/admin/a.tmp", TIGHTEN),
    ("A13 后台 a~",               "GET",  "/calc/admin/a~", TIGHTEN),
]


# ------------------------------------------------------------------ 判据

def _is_hit(snap, method="GET"):
    """「命中」= 200 且**真取到了件**。

    HEAD 不写体（`_send_bytes` 里 `b"" if self.command == "HEAD"`），所以那一维
    只能看 `Content-Length` —— 它报的是真实体长（刀8b 已钉死这条），> 0 即取到了件。
    """
    status, _reason, hdrs, body = snap
    if status != 200:
        return False
    length = dict(hdrs).get("content-length")
    if method == "HEAD":
        return length is not None and length.isdigit() and int(length) > 0
    return bool(body)


def _is_blocked(snap, method="GET"):
    """「拦」= 404 且错误码是 `not_found`（不是 `ui_not_deployed` 那种「根没部署」）。

    HEAD 同样读不到正文，改判「404 + JSON 应答 + 体长非 0」—— 同一条路径的 GET 例
    已把 `error.code` 逐字节钉过（每个 HEAD 负控都有对应的 GET 负控），
    这里不重复要求正文。
    """
    status, _reason, hdrs, body = snap
    if status != 404:
        return False
    if method == "HEAD":
        head = dict(hdrs)
        length = head.get("content-length")
        return ((head.get("content-type") or "").startswith("application/json")
                and length is not None and length.isdigit() and int(length) > 0)
    try:
        doc = json.loads(body.decode("utf-8"))
    except Exception:
        return False
    return (doc.get("ok") is False
            and (doc.get("error") or {}).get("code") == "not_found")


def _verdict(snap, method="GET"):
    """用于打印的行为标签。"""
    if _is_hit(snap, method):
        return "命中(200)"
    if _is_blocked(snap, method):
        return "拦(404%s)" % ("" if method == "HEAD" else " not_found")
    return "%s" % (snap[0],)


def run_matrix(title, cases, p_old, p_new):
    """跑一张矩阵：打印「改前行为 → 改后行为」，返回 (不合期望的格数, 总格数, 增严格数)。"""
    print("\n== %s ==" % title)
    print("%-32s %-6s %-46s %-9s %-18s %-22s %s"
          % ("用例", "方法", "路径", "期望", "改前", "改后", "结论"))
    print("-" * 168)
    bad = 0
    tightened = 0
    for name, method, path, want in cases:
        old = base.call(p_old, method, path, "none", None)
        new = base.call(p_new, method, path, "none", None)
        if want is SAME:
            ok = old == new
            note = "逐字节相同" if ok else ("**不一致**\n    旧=%r\n    新=%r" % (old, new))
        else:
            ok = _is_hit(old, method) and _is_blocked(new, method)
            tightened += 1 if ok else 0
            note = ("命中→拦（定向增严）" if ok else
                    "**没按 200→404 走**\n    旧=%r\n    新=%r" % (old, new))
        # 反方向护栏：任何一格「改前拦、改后命中」都是放宽，直接判负。
        if _is_blocked(old, method) and _is_hit(new, method):
            ok = False
            note = "**放宽了**（改前拦 / 改后命中）—— 本刀只许收紧"
        bad += 0 if ok else 1
        print("%-32s %-6s %-46s %-9s %-18s %-22s %s"
              % (name, method, path[:46], want, _verdict(old, method),
                 _verdict(new, method),
                 ("✓ " + note) if ok else ("✗ " + note)))
    return bad, len(cases), tightened


def check_frozen_baseline():
    """冻结件自身的护栏：`_old/chat_svc_old_k34.py` 必须是**改前**那一版。

    两条断言：
      · 冻结件里**没有** `STATIC_BACKUP_RE`（有就说明冻错了 —— 冻的是改后）；
      · 工作树那份**有**（没有就说明 B 段压根没落地，矩阵会全绿但什么也没验）。
    """
    print("\n== 冻结件方向断言（防止把「改后」当成「改前」冻进去）==")
    bad = 0
    old_raw = io.open(OLD_FILE, "rb").read()
    new_raw = io.open(os.path.join(AG, "chat_svc.py"), "rb").read()
    for label, raw, want in (("冻结基线 _old/chat_svc_old_k34.py", old_raw, False),
                             ("工作树 agent-team/chat_svc.py", new_raw, True)):
        has = b"STATIC_BACKUP_RE" in raw
        ok = has is want
        bad += 0 if ok else 1
        print("  %s %s：STATIC_BACKUP_RE %s（期望%s）"
              % ("✓" if ok else "✗", label, "在" if has else "不在",
                 "在" if want else "不在"))
    return bad


def check_untouched_constants(mod_old, mod_new):
    """「SPA fallback / MIME / no-cache 一字不动」的直接断言（不靠矩阵间接推）。"""
    print("\n== 未动面断言（SPA 兜底文件名 / MIME 钉死表 / 缓存口径 / 既有排除表）==")
    bad = 0
    probes = [
        ("DEFAULT_DOC", mod_old.DEFAULT_DOC, mod_new.DEFAULT_DOC),
        ("STATIC_CACHE", mod_old.STATIC_CACHE, mod_new.STATIC_CACHE),
        ("STATIC_EXCLUDE", sorted(mod_old.STATIC_EXCLUDE), sorted(mod_new.STATIC_EXCLUDE)),
        ("STATIC_EXTS", sorted(mod_old.STATIC_EXTS), sorted(mod_new.STATIC_EXTS)),
        ("MIME_OVERRIDE", mod_old.MIME_OVERRIDE, mod_new.MIME_OVERRIDE),
        ("ADMIN_DOC", mod_old.ChatHandler.ADMIN_DOC, mod_new.ChatHandler.ADMIN_DOC),
    ]
    for name, a, b in probes:
        ok = a == b
        bad += 0 if ok else 1
        print("  %s %-16s 改前 == 改后 ? %s" % ("✓" if ok else "✗", name, ok))
    return bad


def check_regex_shape(mod_new):
    """判据本身的逐名断言：该拦的拦、该放的放（HTTP 矩阵之外的第二份证据）。"""
    print("\n== 判据逐名断言 STATIC_BACKUP_RE ==")
    rx = mod_new.STATIC_BACKUP_RE
    expect_hit = sorted(BACKUPS) + ["nested.html.bak-1", "bakdir.bak-1"]
    expect_miss = sorted(LOOKALIKE) + ["group.html", "group.css", "md.js", "tex.js",
                                       "hl.js", "codeblock.js", "group_view.js",
                                       "group_transport.js", "index.html", "calc.html",
                                       "a.png", "sub", "assets", "app.js"]
    bad = 0
    for name in expect_hit:
        ok = bool(rx.search(name))
        bad += 0 if ok else 1
        print("  %s 拦  %s" % ("✓" if ok else "✗", name))
    for name in expect_miss:
        ok = not rx.search(name)
        bad += 0 if ok else 1
        print("  %s 放  %s" % ("✓" if ok else "✗", name))
    return bad, len(expect_hit) + len(expect_miss)


# ------------------------------------------------------------------ 主流程

def main():
    if not os.path.isfile(OLD_FILE):
        print("缺少刀34 基线快照 %s —— 它必须是**改 chat_svc.py 之前**那一版的逐字节副本"
              % OLD_FILE)
        return 2

    sys.path[:0] = [OLD, AG]
    import chat_svc_old_k34 as mod_old                              # noqa: E402
    import chat_svc as mod_new                                      # noqa: E402
    import view_auth                                                # noqa: E402

    base.COOKIE_NAME = view_auth.SESSION_COOKIE
    # 静态面本来就不鉴权；钉成 tailnet 免登录，两侧同一条件，省掉 cookie 这一维
    # （鉴权面是刀8 的责任田，`chat_svc_http_equiv.py` 里已有 300 多例盯着）。
    view_auth.in_tailnet = lambda ip: True

    import tempfile
    root = tempfile.mkdtemp(prefix="k34-static-")
    ui, admin = make_tree(root)
    for mod in (mod_old, mod_new):
        base.setup_runtime(mod, ui)
        mod.ChatHandler.CHAT_ADMIN_UI_DIR = admin

    p_old, p_new = base.free_port(), base.free_port()
    s_old = base.start(mod_old.ChatHandler, p_old)
    s_new = base.start(mod_new.ChatHandler, p_new)

    print("改前（冻结基线）%s   :%d" % (OLD_FILE, p_old))
    print("改后（工作树）  %s   :%d" % (os.path.join(AG, "chat_svc.py"), p_new))
    print("静态根 ui/ = %s\n后台根 admin-ui/ = %s" % (ui, admin))

    bad = 0
    bad += check_frozen_baseline()
    bad += check_untouched_constants(mod_old, mod_new)
    rx_bad, rx_cells = check_regex_shape(mod_new)
    bad += rx_bad

    b1, n1, t1 = run_matrix("同源静态面 `_serve_static()` · 改前 vs 改后", CASES_STATIC,
                            p_old, p_new)
    b2, n2, t2 = run_matrix("后台静态面 `_serve_admin()` · 改前 vs 改后", CASES_ADMIN,
                            p_old, p_new)
    bad += b1 + b2

    for srv in (s_old, s_new):
        srv.shutdown()
        srv.server_close()

    cells = n1 + n2 + rx_cells + 2 + 6
    same_cells = sum(1 for c in CASES_STATIC + CASES_ADMIN if c[3] is SAME)
    tighten_cells = sum(1 for c in CASES_STATIC + CASES_ADMIN if c[3] is TIGHTEN)

    print("\n" + "=" * 168)
    print("矩阵格数 %d（HTTP 用例 %d = 等价 %d + 定向增严 %d；判据逐名 %d；"
          "冻结方向 2；未动面 6）" % (cells, n1 + n2, same_cells, tighten_cells, rx_cells))
    if bad:
        print("✗ 有 %d 格不合期望" % bad)
        return 1
    print("✓ 全部 %d 格按期望：%d 格逐字节相同（状态码 + 状态原因 + 有序响应头 + 正文），"
          "%d 格由「命中(200)」变「拦(404 not_found)」，**无一格放宽**"
          % (cells, same_cells, t1 + t2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
