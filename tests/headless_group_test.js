# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
// 群页渲染自测：用 chat_svc 的真实 API（只读）喂 group.html，断言三条需求的落地效果。
// 用法：node headless_group_test.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const HOST = process.env.CHAT_HOST || "http://<host>:9984";
const SID = process.env.CHAT_SID || "meet_20260923_7fea98";
const URL = process.env.CHAT_PAGE || "/";
const TOKA = process.env.CHAT_TOKEN_FILE || "<deploy-dir>/client-token";

let token = "";
try { token = fs.readFileSync(TOKA, "utf8").trim(); } catch (e) {}

async function api(p) {
  const r = await fetch(HOST + p, { headers: token ? { Authorization: "Bearer " + token } : {} });
  return await r.json();
}

(async () => {
  const msgs = (await api(`/api/sessions/${SID}/messages?limit=60`)).messages || [];
  const agents = (await api("/api/agents")).agents || [];
  console.log("messages:", msgs.length, "agents:", agents.length);

  // 1) 主题分组：按 group.html 同一套规则复算，检查分组是否合理
  let lastTid = -1, next = 0, groups = [];
  msgs.forEach(m => {
    const isHuman = !(m.author && m.author.type === "agent");
    if (!groups.length || isHuman || lastTid < 0) {
      lastTid = next++; groups.push({ tid: lastTid, n: 0, head: m.text || "" });
    }
    groups[lastTid].n++;
  });
  console.log("主题组数:", groups.length, "（消息", msgs.length, "条）");
  groups.slice(0, 8).forEach(g => console.log("  组" + g.tid + " " + g.n + " 条 | " + String(g.head).replace(/\s+/g, " ").slice(0, 40)));

  // 2) 接收者高亮数据面：audience 字段是否齐全
  const withAud = msgs.filter(m => ((m.meta || {}).audience || []).length).length;
  console.log("带 audience 的消息:", withAud + "/" + msgs.length);

  // 3) Markdown：把 md.js 拿出来，对每条消息跑一遍
  const sandbox = { window: {} };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "ui", "md.js"), "utf8"), sandbox);
  const MD = sandbox.window.MD;
  let mdCount = 0;
  msgs.forEach(m => {
    const h = MD.render(m.text || "");
    if (/<strong>|<li>|<pre|<code|md-math|<table|<h[1-6]/.test(h)) mdCount++;
  });
  console.log("含 markdown 结构的消息:", mdCount + "/" + msgs.length);
  const sample = msgs.find(m => /```|\*\*/.test(m.text || ""));
  if (sample) {
    console.log("抽样 #" + sample.seq + " 渲染前:", String(sample.text).slice(0, 60).replace(/\n/g, "\\n"));
    console.log("抽样 #" + sample.seq + " 渲染后:", MD.render(sample.text).slice(0, 200));
  }
})();
