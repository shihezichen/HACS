// ============================================================================
// codeblock.js —— 块级 LaTeX 自动识别 + 代码块高亮贴片（2026-09-24）
//
// 解决刚哥反馈 1 的真实病灶：Agent 常把公式**裸贴**在正文里（无 $ 也无 \[ \]），例如
//
//     数学曲线 · 实时绘制
//     \int_0^1 x^2 \, dx = \frac{1}{3}
//
// 旧版只能原样显示。这里做两件事：
//   ① 若整个段落「从头到尾就是一条 LaTeX 公式」→ 直接渲染成块级公式；
//   ② 否则按行扫：**以 \命令 开头**的连续行抽出来单独成公式块，其余保持普通段落。
//
// 以及：把 md.js 产出的 <pre class="md-code"><code> 按语言上色（调 hl.js）。
//
// 安全：所有输入先 esc()，模块只生成白名单标签。
// ============================================================================
// md.js 的 enhance() 用 `global` 指代浏览器 window（见 md.js 的 IIFE 参数改法）；
// 这里为了跟 md.js 保持一致，统一定义 global/window 两个别名。
(function (global) {
  "use strict";

  var WIN = (typeof window !== "undefined") ? window : global;

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  var T = function () { return WIN.TeX; };
  var H = function () { return WIN.HL; };

  // 一条「纯公式」串：允许 \命令、_ ^ { } ( ) [ ] , . ; : = + - * / | ' 空格 数字 字母
  function looksLikeLabelLine(t) { return /^[A-Za-z\u4e00-\u9fa5][^=]{0,24}[:：]$/.test(t); }

  function countMathMarks(t) {
    var m = 0;
    m += (t.match(/\\[A-Za-z]+/g) || []).length * 2;   // 反斜杠命令是强信号
    m += (t.match(/[_^]/g) || []).length;
    m += (t.match(/[{}]/g) || []).length;
    m += (t.match(/[=+\-*/]/g) || []).length;
    return m;
  }

  function isPureFormulaLine(t) {
    if (!t) return false;
    if (t.length > 400) return false;
    if (!/^[\\A-Za-z0-9\s_{}^()\[\].,;:+\-*/=|'"!<>\\]+$/.test(t)) return false;
    if (!/\\[A-Za-z]+/.test(t)) return false;            // 必须有 \命令
    if (countMathMarks(t) < 3) return false;
    // 自然语言干扰：连续 3 个以上「词 + 空格」且含中文 → 不是公式
    if (/[\u4e00-\u9fa5]/.test(t) && !/\\text\{/.test(t)) return false;
    return true;
  }

  // 把一段纯文本（已 esc 的段落文本，含换行）转成 html：
  // 纯公式段 → 块级公式；否则逐行判断，公式行抽出来成为一个公式块
  // ⚠️ 注意：调用方可能已经把换行变成了 <br>（md.js 的段落就是这么干的），
  //     所以这里要先把 <br> / <br/> / <br /> 一并当作换行来处理。
  function texifyParagraph(text) {
    var raw = String(text == null ? "" : text).replace(/<br\s*\/?>/gi, "\n");
    var lines = raw.split(/\r?\n/).map(function (l) { return l.trim(); }).filter(function (l) { return l !== ""; });
    if (!lines.length) return null;

    // 整段就是一条公式
    if (lines.length === 1 && isPureFormulaLine(lines[0])) {
      return T().toMathML(lines[0]);
    }
    // 多行但整体就是一条公式（LaTeX 常被拆行写）
    var joined = lines.join(" ");
    if (lines.length > 1 && lines.every(function (l) { return !looksLikeLabelLine(l); }) &&
        isPureFormulaLine(joined)) {
      return T().toMathML(joined);
    }

    // 混合：把公式行挑出来做成独立块
    var out = [], buf = [], i;
    function flushProse() {
      if (!buf.length) return;
      out.push("<p>" + buf.map(function (l) { return linkifyPlain(l); }).join("<br>") + "</p>");
      buf = [];
    }
    for (i = 0; i < lines.length; i++) {
      var l = lines[i];
      if (isPureFormulaLine(l)) {
        flushProse();
        out.push(T().toMathML(l));
      } else {
        buf.push(l);
      }
    }
    flushProse();
    return out.join("");
  }

  // 纯文本段落里也把裸链接点亮（md.js 的 inline 只管 markdown 路径）
  function linkifyPlain(t) {
    return esc(t).replace(/(^|[^"'>=])(https?:\/\/[^\s<]+)/g,
      function (m, pre, u) {
        return pre + '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + u + "</a>";
      });
  }

  // ---- 代码块高亮贴片 ----
  function applyHighlight(root) {
    if (!root) return 0;
    var n = 0;
    var blocks = root.querySelectorAll ? root.querySelectorAll("pre.md-code") : [];
    Array.prototype.forEach.call(blocks, function (pre) {
      if (pre.dataset.hl) return;
      var codeEl = pre.querySelector("code");
      if (!codeEl) return;
      var lang = pre.getAttribute("data-lang") || "";
      var raw = codeEl.textContent || "";
      var fn = H();
      if (!fn) { pre.dataset.hl = "0"; return; }
      try {
        codeEl.innerHTML = fn.highlight(raw, lang);
      } catch (e) {
        codeEl.textContent = raw;
      }
      pre.dataset.hl = "1";
      n++;
    });
    return n;
  }

  WIN.MDX = {
    texifyParagraph: texifyParagraph,
    isPureFormulaLine: isPureFormulaLine,
    applyHighlight: applyHighlight,
    esc: esc
  };
})(typeof window !== "undefined" ? window : this);
