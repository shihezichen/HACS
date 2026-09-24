# -*- coding: utf-8 -*-
"""真机验证（Linux 侧）：headless Chrome + CDP，确认群页三条需求已生效。"""
import json
import subprocess
import sys
import time
import urllib.request

CHROME = None
for c in ("/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
    try:
        if subprocess.run(["test", "-x", c]).returncode == 0:
            CHROME = c
            break
    except Exception:
        pass
print("chrome bin:", CHROME)
if not CHROME:
    sys.exit("no chrome")

import websocket  # python-websocket-client

PORT = 9333
proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                         "--remote-debugging-port=%d" % PORT,
                         "--user-data-dir=/tmp/chrome-h460", "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(4)

ver = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json/list" % PORT))
page = [t for t in ver if t.get("type") == "page"][0]
ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30)

_id = [0]


def cdp(method, params=None):
    _id[0] += 1
    ws.send(json.dumps({"id": _id[0], "method": method, "params": params or {}}))
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == _id[0]:
            return m.get("result", {})


def ev(expr):
    r = cdp("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    return (r.get("result") or {}).get("value")


cdp("Runtime.enable")
cdp("Page.enable")
url = "http://<host>:9984/"
cdp("Page.navigate", {"url": url})
time.sleep(12)

out = {
    "url": url,
    "title": ev("document.title"),
    "mdLoaded": ev("!!(window.MD && window.MD.render)"),
    "topicGroups": ev("document.querySelectorAll('#log .tgrp').length"),
    "msgsInGroups": ev("document.querySelectorAll('#log .tgrp .tgb .msg').length"),
    "msgsTotal": ev("document.querySelectorAll('#log .msg').length"),
    "topicHeads": ev("[...document.querySelectorAll('#log .tgh .tgt')].slice(0,5).map(e=>e.textContent)"),
    "collapsible": ev("!!(document.querySelector('#log .tgh')||{}).onclick"),
    "audNodes": ev("document.querySelectorAll('#log .msg .h .aud').length"),
    "audColor": ev("(function(){var a=document.querySelector('#log .msg .h .aud');return a?getComputedStyle(a).color:null})()"),
    "authorColor": ev("(function(){var b=document.querySelector('#log .msg .h b');return b?getComputedStyle(b).color:null})()"),
    "mdBodies": ev("document.querySelectorAll('#log .msg .b.md').length"),
    "mdStructures": ev("document.querySelectorAll('#log .msg .b.md strong,#log .msg .b.md li,#log .msg .b.md pre,#log .msg .b.md code,#log .msg .b.md table,#log .msg .b.md blockquote').length"),
    "mathNodes": ev("document.querySelectorAll('#log .msg .b.md .md-math,#log .msg .b.md .md-mathblock').length"),
    "xssLeak": ev("[...document.querySelectorAll('#log .msg .b.md')].some(b=>/<(img|b|script)[ >]/i.test(b.textContent||''))"),
    "probeMsgFound": ev("[...document.querySelectorAll('#log .msg')].some(m=>(m.textContent||'').indexOf('功能自测')>=0)"),
    "probeProbe": ev("(function(){var ms=[...document.querySelectorAll('#log .msg')].filter(m=>(m.textContent||'').indexOf('功能自测')>=0);if(!ms.length)return null;var m=ms[0];return {strong:!!m.querySelector('.b.md strong'),code:!!m.querySelector('.b.md pre.md-code'),math:!!m.querySelector('.b.md .md-math'),mathblock:!!m.querySelector('.b.md .md-mathblock'),table:!!m.querySelector('.b.md table'),quote:!!m.querySelector('.b.md blockquote'),li:!!m.querySelector('.b.md li'),rawImg:(m.querySelector('.b.md')||{}).innerHTML?m.querySelector('.b.md').innerHTML.indexOf('&lt;img'):null}})()"),
}
print(json.dumps(out, ensure_ascii=False, indent=2))

# 折叠实测
b = ev("document.querySelectorAll('#log .tgrp.closed').length")
ev("document.querySelector('#log .tgh').click()")
time.sleep(0.4)
a = ev("document.querySelectorAll('#log .tgrp.closed').length")
print("collapse: closed before=%s after=%s" % (b, a))
ev("document.querySelector('#log .tgh').click()")
time.sleep(0.3)

shot = cdp("Page.captureScreenshot", {"format": "png"})
open("/tmp/group_page.png", "wb").write(__import__("base64").b64decode(shot["data"]))
print("screenshot bytes:", len(open("/tmp/group_page.png", "rb").read()))

# 全页长截图（供人工目检）
shot2 = cdp("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True,
                                       "clip": {"x": 0, "y": 0, "width": 1400, "height": 2400, "scale": 1}})
open("/tmp/group_page_full.png", "wb").write(__import__("base64").b64decode(shot2["data"]))
print("fullpage bytes:", len(open("/tmp/group_page_full.png", "rb").read()))

ws.close()
proc.terminate()
