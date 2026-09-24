# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
// 自测：node md_test.js  —— 断言渲染结果与 XSS 安全
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(path.join(__dirname, "ui", "md.js"), "utf8");
const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const MD = sandbox.window.MD;

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log("  ok  " + name); }
  else { fail++; console.log("  FAIL " + name + (extra ? "  <<< " + extra : "")); }
}

console.log("== 1 基础");
let h = MD.render("**粗** 和 *斜* 和 ~~删~~ 和 `code`");
ok("strong", h.includes("<strong>粗</strong>"), h);
ok("em", h.includes("<em>斜</em>"), h);
ok("del", h.includes("<del>删</del>"), h);
ok("code", h.includes("<code>code</code>"), h);

console.log("== 2 数学");
h = MD.render("质能方程 $E=mc^2$ 与 \\(a^2+b^2=c^2\\)");
ok("inline $..$", h.includes('<span class="md-math">E=mc^2</span>'), h);
ok("inline \\(..\\)", h.includes('<span class="md-math">a^2+b^2=c^2</span>'), h);
h = MD.render("$$\n\\int_0^1 x^2 dx = \\frac{1}{3}\n$$");
ok("block $$", h.includes('md-mathblock') && h.includes("\\frac{1}{3}"), h);
h = MD.render("价格 $5 到 $9 之间");
ok("价格不误判", !h.includes("md-math"), h);

console.log("== 3 代码块 / 表格 / 引用 / 列表");
h = MD.render("```python\ndef add(a,b):\n    return a+b\n```");
ok("fence", h.includes('<pre class="md-code"') && h.includes("    return a+b"), h);
h = MD.render("| 项 | 值 |\n|---|---|\n| 分组 | 主题 |");
ok("table", h.includes("<table>") && h.includes("<th>项</th>") && h.includes("<td>主题</td>"), h);
h = MD.render("> 引用一\n> 引用二");
ok("blockquote", h.includes("<blockquote>引用一<br>引用二</blockquote>"), h);
h = MD.render("- A\n- B\n\n1. C\n2. D");
ok("ul", h.includes("<ul><li>A</li><li>B</li></ul>"), h);
ok("ol", h.includes("<ol><li>C</li><li>D</li></ol>"), h);
h = MD.render("## 标题");
ok("heading", h.includes("<h2>标题</h2>"), h);

console.log("== 4 安全（关键）");
h = MD.render("<img src=x onerror=alert(1)>");
ok("无 img 标签", !/<img/i.test(h), h);
ok("字面显示", h.includes("&lt;img"), h);
h = MD.render("<b>不该变粗</b>");
ok("无 b 标签", !/<b>/i.test(h), h);
h = MD.render("[x](javascript:alert(1))");
ok("javascript: 链接被拦", !/javascript:/i.test(h.replace(/&quot;/g, '"')) || !h.includes("<a "), h);
h = MD.render('`*not bold*`');
ok("code 内 * 不变粗", !h.includes("<em>"), h);

console.log("== 5 综合（真实消息样本）");
h = MD.render("@hermes 三点：\n\n**1. 结论** —— 已通过\n\n- 曲线接口\n- 上下文水位\n\n$$x^2$$\n\n```js\nlet a=1;\n```\n\n| 项 | 值 |\n|---|---|\n| 状态 | 绿 |\n");
ok("综合渲染无异常", h.length > 100 && !h.includes("undefined"), h.slice(0, 200));

console.log("\n结果: " + pass + " 通过, " + fail + " 失败");
process.exit(fail ? 1 : 0);
