# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
// 自测 2：tex.js（LaTeX→MathML）+ hl.js（代码高亮）+ codeblock.js（裸公式识别）
// 用法：node tex_test.js
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const sandbox = { window: {}, console };
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
["md.js", "tex.js", "hl.js", "codeblock.js"].forEach(f => {
  vm.runInContext(fs.readFileSync(path.join(__dirname, "ui", f), "utf8"), sandbox);
});
const { TeX, HL, MDX, MD } = sandbox.window;

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log("  ok  " + name); }
  else { fail++; console.log("  FAIL " + name + (extra ? "  <<< " + String(extra).slice(0, 220) : "")); }
}

console.log("== 1 块级公式（刚哥给的原例）");
let h = TeX.toMathML("\\int_0^1 x^2 \\, dx = \\frac{1}{3}");
ok("有 math 标签", h.includes("<math"), h);
ok("display=block", h.includes('display="block"'), h);
ok("积分号", h.includes("<mo>∫</mo>"), h);
ok("上下限 munderover", h.includes("<munderover>"), h);
ok("分式 mfrac", h.includes("<mfrac>"), h);
ok("上标 x^2", h.includes("<msup>") && h.includes("<mn>2</mn>"), h);

console.log("== 2 其他公式语法");
h = TeX.toMathMLInline("\\frac{a}{b} + \\sqrt[3]{x} \\le \\alpha \\cdot \\beta");
ok("mfrac", h.includes("<mfrac>"), h);
ok("mroot(开3次)", h.includes("<mroot>"), h);
ok("≤", h.includes("≤"), h);
ok("希腊字母 α β", h.includes("<mi>α</mi>") && h.includes("<mi>β</mi>"), h);
h = TeX.toMathMLInline("\\left( \\frac{1}{2} \\right)");
ok("left/right 括号", h.includes('stretchy="true"') && /<mo[^>]*>\(<\/mo>/.test(h), h);
h = TeX.toMathMLInline("\\sum_{n=1}^{\\infty} \\frac{1}{n^2} = \\frac{\\pi^2}{6}");
ok("求和限", h.includes("<munderover>") && h.includes("∞"), h);
ok("π", h.includes("π"), h);
h = TeX.toMathMLInline("\\text{总能量} = mc^2");
ok("\\text 中文", h.includes("<mtext>总能量</mtext>"), h);
h = TeX.toMathMLInline("\\mathbf{F} = m\\mathbf{a}");
ok("\\mathbf 粗体", h.includes('mathvariant="bold"'), h);
h = TeX.toMathMLInline("\\unknowncmd{x}");
ok("未知命令不吞内容", h.includes("\\unknowncmd"), h);

console.log("== 3 裸贴公式识别（真实病灶）");
ok("单行纯公式", MDX.isPureFormulaLine("\\int_0^1 x^2 \\, dx = \\frac{1}{3}"), "");
ok("$$ 包裹的行内串", MDX.isPureFormulaLine("E = mc^2") === false, "（无 \\命令，不算）");
ok("中文句子不算公式", !MDX.isPureFormulaLine("这是中文说明 \\frac{a}{b} 后面还有字"), "");
ok("方程行算公式", MDX.isPureFormulaLine("e^{i\\pi} + 1 = 0"), "");
ok("章节标题不算公式", !MDX.isPureFormulaLine("数学曲线 · 实时绘制"), "");
let t = MDX.texifyParagraph("数学曲线 · 实时绘制\n\\int_0^1 x^2 \\, dx = \\frac{1}{3}");
ok("混合段：标题留文字", t.includes("数学曲线") && !t.includes("<math"), t);
ok("混合段：公式成块", t.includes("<math"), t);

console.log("== 4 代码高亮");
h = HL.highlight("def add(a, b):\n    return a + b   # 求和", "python");
ok("python 关键字", /tk-kw">def<\/span>/.test(h) && h.includes('tk-kw">return'), h);
ok("注释", h.includes('tk-cmt"># 求和'), h);
ok("函数名", h.includes('tk-fn">add'), h);
h = HL.highlight('const x = "hi"; // c\nif (x) console.log(1);', "js");
ok("js 关键字/字符串/注释", h.includes('tk-kw">const') && h.includes('tk-str">"hi"') && h.includes("tk-cmt"), h);
h = HL.highlight("SELECT * FROM t WHERE a = 1 -- note", "sql");
ok("sql 关键字", h.includes('tk-kw">SELECT') && h.includes('tk-kw">FROM') && h.includes("tk-cmt"), h);
h = HL.highlight('{"a": true, "b": null}', "json");
ok("json", h.includes('tk-kw">true'), h);
h = HL.highlight("<div class='x'>hi</div>", "html");
ok("html 标签", h.includes('tk-tag'), h);
h = HL.highlight("echo $HOME && ls -la | grep x", "bash");
ok("bash", h.includes('tk-kw">echo') || h.includes('tk-kw">ls'), h);
h = HL.highlight("未知语言 \u0000 <script>alert(1)</script>", "zzz");
ok("未知语言不报错", typeof h === "string" && h.length > 0, h);
ok("代码里的 < 被转义", h.includes("&lt;script&gt;"), h);

console.log("== 5 安全");
h = HL.highlight('x = "<img src=x onerror=alert(1)>"', "python");
ok("字符串里的标签被转义", !/<img/i.test(h), h);
h = TeX.toMathML("<script>alert(1)</script>");
ok("公式里的标签被转义", !/<script/i.test(h), h);
h = MDX.texifyParagraph("<img src=x onerror=alert(1)>");
ok("裸文本里标签被转义", h === null || !/<img/i.test(h), h);

console.log("== 6 md.js 端到端（含 texify 增强）");
h = MD.render("数学曲线 · 实时绘制\n\\int_0^1 x^2 \\, dx = \\frac{1}{3}");
ok("md 输出含 math 块", h.includes("<math"), h);
h = MD.render("裸公式：\nE = mc^2\n$E=mc^2$");
ok("行内 $..$ 仍是 math", (h.match(/<math/g) || []).length >= 1, h);
h = MD.render("**粗体要明显**");
ok("strong 保留", h.includes("<strong>粗体要明显</strong>"), h);

console.log("\n结果: " + pass + " 通过, " + fail + " 失败");
process.exit(fail ? 1 : 0);
