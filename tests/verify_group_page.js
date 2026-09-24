// 真机验证：无头 Chrome 打开 team_chat 群页，确认三条需求（分组/接收者高亮/md 渲染）已生效。
// 用法：node verify_group_page.js
const { execFileSync } = require("child_process");
const fs = require("fs");

const ws = JSON.parse(execFileSync("curl", ["-s", "http://127.0.0.1:9222/json/list"], { encoding: "utf8" }))
  .find(t => t.type === "page");

(async () => {
  const WebSocket = require("ws");
  const sock = new WebSocket(ws.webSocketDebuggerUrl, { perMessageDeflate: false });
  let id = 0; const pend = new Map();
  const send = (method, params) => new Promise((res, rej) => {
    const i = ++id; pend.set(i, { res, rej });
    sock.send(JSON.stringify({ id: i, method, params }));
  });
  sock.on("message", (d) => {
    const m = JSON.parse(d.toString());
    if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.rej(m.error) : p.res(m.result); }
  });
  await new Promise(r => sock.on("open", r));

  await send("Runtime.enable");
  await send("Page.enable");
  const url = process.env.CHAT_URL || "http://<host>:9984/";
  await send("Page.navigate", { url });
  await new Promise(r => setTimeout(r, 9000));

  const ev = async (expr) => (await send("Runtime.evaluate", { expression: expr, returnByValue: true })).result.value;

  const out = {};
  out.title = await ev("document.title");
  out.topicGroups = await ev("document.querySelectorAll('#log .tgrp').length");
  out.msgsInGroups = await ev("document.querySelectorAll('#log .tgrp .tgb .msg').length");
  out.msgsTotal = await ev("document.querySelectorAll('#log .msg').length");
  out.topicHeads = await ev("[...document.querySelectorAll('#log .tgh .tgt')].slice(0,4).map(e=>e.textContent)");
  out.collapsible = await ev("!!(document.querySelector('#log .tgh') && document.querySelector('#log .tgh').onclick)");
  out.audColored = await ev("document.querySelectorAll('#log .msg .h .aud').length");
  out.audComputedColor = await ev("(function(){var a=document.querySelector('#log .msg .h .aud');if(!a)return null;return getComputedStyle(a).color})()");
  out.authorColor = await ev("(function(){var b=document.querySelector('#log .msg .h b');if(!b)return null;return getComputedStyle(b).color})()");
  out.mdBodies = await ev("document.querySelectorAll('#log .msg .b.md').length");
  out.mdStructures = await ev("document.querySelectorAll('#log .msg .b.md strong, #log .msg .b.md li, #log .msg .b.md pre, #log .msg .b.md code, #log .msg .b.md table, #log .msg .b.md blockquote').length");
  out.mathNodes = await ev("document.querySelectorAll('#log .msg .b.md .md-math, #log .msg .b.md .md-mathblock').length");
  out.rawEscapeLeak = await ev("[...document.querySelectorAll('#log .msg .b.md')].some(b=>/<(img|b|script)\\b/i.test(b.textContent||''))");
  out.mdLoaded = await ev("!!(window.MD && window.MD.render)");

  console.log(JSON.stringify(out, null, 2));

  // 折叠功能实测：点第一个组头
  const before = await ev("document.querySelectorAll('#log .tgrp.closed').length");
  await ev("document.querySelector('#log .tgh').click()");
  await new Promise(r => setTimeout(r, 300));
  const after = await ev("document.querySelectorAll('#log .tgrp.closed').length");
  console.log("collapsed before/after click:", before, after);
  await ev("document.querySelector('#log .tgh').click()");

  // 截图
  const shot = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync("/tmp/group_page.png", Buffer.from(shot.data, "base64"));
  console.log("screenshot: /tmp/group_page.png", fs.statSync("/tmp/group_page.png").size, "bytes");
  sock.close();
})().catch(e => { console.error("ERR", e); process.exit(1); });
