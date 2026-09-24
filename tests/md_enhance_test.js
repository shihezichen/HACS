# hacs-test-fixtures: 本文件为测试夹具，允许出现假凭据与网段边界值；危险类命中（真令牌/连接串/真实人名）仍按硬门禁拦截
const fs = require("fs"), vm = require("vm");

// MiniElement：支持 enhance 用到的 querySelector/querySelectorAll/childNodes
function El(tag) { this.tagName = tag.toUpperCase(); this.childNodes = []; this.parentNode = null; this._text = ""; this._raw = null; }
El.prototype.appendChild = function (c) { c.parentNode = this; this.childNodes.push(c); return c; };
El.prototype.replaceChild = function (nw, old) { nw.parentNode = this; const i = this.childNodes.indexOf(old); if (i >= 0) this.childNodes[i] = nw; return old; };
El.prototype._all = function () {
  const out = [];
  (function walk(n) { n.childNodes.forEach(c => { out.push(c); walk(c); }); })(this);
  return out;
};
El.prototype.querySelectorAll = function (sel) {
  const tags = sel.split(",").map(s => s.trim().toUpperCase());
  return this._all().filter(e => tags.indexOf(e.tagName) >= 0);
};
El.prototype.querySelector = function (sel) { return this.querySelectorAll(sel)[0] || null; };
Object.defineProperty(El.prototype, "textContent", {
  get() { return this._raw != null ? this._raw.replace(/<[^>]+>/g, "") : (this.childNodes.map(c => c.textContent).join("") + this._text); },
  set(v) { this._text = String(v); }
});
Object.defineProperty(El.prototype, "innerHTML", {
  get() { return this._raw != null ? this._raw : this.childNodes.map(c => c.innerHTML).join(""); },
  set(v) { this._raw = String(v); this.childNodes = []; }
});

const sb = { window: {}, console };
sb.window.window = sb.window;
sb.window.DOMParser = function () {
  this.parseFromString = function (html) {
    const root = new El("div");
    const body = html.replace(/^<div id="__mdx">/, "").replace(/<\/div>$/, "");
    const parts = body.split(/(<p>[\s\S]*?<\/p>|<math[\s\S]*?<\/math>)/g).filter(x => x !== "");
    parts.forEach(seg => {
      if (/^<p>/.test(seg)) {
        const p = new El("p");
        p.innerHTML = /^<p>([\s\S]*?)<\/p>$/.exec(seg)[1];
        // 真实 DOM 的 textContent 会把 <br> 变回换行；替身必须一致，否则测不准
        p.textContent = p.innerHTML.replace(/<br\s*\/?>/gi, "\n").replace(/<[^>]+>/g, "");
        root.appendChild(p);
      }
    });
    return { getElementById: id => (id === "__mdx" ? root : null) };
  };
};
sb.window.document = { createElement: t => new El(t) };
vm.createContext(sb);
["tex.js", "hl.js", "codeblock.js"].forEach(f => vm.runInContext(fs.readFileSync("ui/" + f, "utf8"), sb));
vm.runInContext(fs.readFileSync("ui/md.js", "utf8"), sb);
const { MD } = sb.window;

let pass = 0, fail = 0;
function ok(n, c, x) { c ? (pass++, console.log("  ok  " + n)) : (fail++, console.log("  FAIL " + n + "  <<< " + String(x).slice(0, 200))); }

let h = MD.render("数学曲线 · 实时绘制\n\\int_0^1 x^2 \\, dx = \\frac{1}{3}");
ok("整段裸公式 → math", h.includes("<math"), h);
ok("标题文字保留", h.includes("数学曲线"), h);
h = MD.render("只有文字和 https://a.b/c 链接");
ok("无公式段落不动", !h.includes("<math"), h);
h = MD.render("$$E=mc^2$$");
ok("$$ 包裹仍是 math", h.includes("<math"), h);
h = MD.render("**粗** `code`\n\n- A\n- B");
ok("粗体/代码/列表回归", h.includes("<strong>粗</strong>") && h.includes("<code>code</code>") && h.includes("<li>A</li>"), h);
h = MD.render("说明：\\frac{a}{b} 就是这样");
console.log("  info  行内裸公式段落 →", h.includes("<math") ? "被渲染" : "保持文字", String(h).slice(0, 120));

console.log("\n结果: " + pass + " 通过, " + fail + " 失败");
process.exit(fail ? 1 : 0);
