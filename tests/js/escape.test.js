/* 共用 escaping 的單元測試(純 Node,無框架)。
 * 涵蓋:<script>、<img onerror>、屬性 breakout、h`` 預設跳脫、raw() 通行。
 * 執行:node tests/js/escape.test.js(exit 0 = 通過)
 */
const path = require("path");
const { esc, raw, h } = require(path.join(__dirname, "..", "..", "app", "static", "js", "escape.js"));

let failures = 0;
function ok(cond, msg) {
  if (!cond) { failures++; console.error("FAIL:", msg); }
  else { console.log("ok:", msg); }
}
function noHtml(s, needles) {
  // 不得出現「可執行的原始標籤/屬性」;中繼字元必須被編碼
  for (const n of needles) ok(!s.includes(n), `不得含原始片段 ${JSON.stringify(n)} → ${s}`);
}

// 1) <script>
{
  const out = esc('<script>alert(1)</script>');
  ok(out.includes("&lt;script&gt;"), "<script> 被跳脫為 &lt;script&gt;");
  noHtml(out, ["<script>", "</script>"]);
}

// 2) <img src=x onerror=...>
{
  const out = esc('<img src=x onerror=alert(1)>');
  noHtml(out, ["<img", "onerror=alert(1)>"]);
  ok(out.includes("&lt;img"), "<img 被跳脫");
}

// 3) 屬性 breakout(跳出雙引號屬性)
{
  const payload = '" onmouseover="alert(1)';
  const out = h`<span title="${payload}">x</span>`;
  noHtml(out, ['title="" onmouseover="alert(1)"']);
  ok(out.includes("&quot;"), "雙引號被編碼為 &quot;,無法 breakout 屬性");
}

// 4) 老師 note(使用者輸入)走 h`` 預設跳脫
// 注意:onerror= 這個「文字」會留下,但因 < > 已被編碼(&lt;img...&gt;),
// 瀏覽器不會組成元素 → 為惰性文字。安全檢查看的是「原始標籤是否成形」。
{
  const note = '<img src=x onerror=alert(1)>惡意';
  const out = h`<div class="mentor-memo">老師備註:${note}</div>`;
  noHtml(out, ["<img"]);                       // 不得出現原始 <img 標籤起始
  ok(out.includes("老師備註:") && out.includes("&lt;img"), "note 內容以文字呈現,不執行");
}

// 5) AI one-liner / rationale
{
  const oneLiner = '</p><script>steal()</script>';
  const out = h`<div class="ai-oneliner">${oneLiner}</div>`;
  noHtml(out, ["<script>", "</p>"]);
}

// 6) provider error 文字
{
  const err = 'timeout <b>oops</b> <svg/onload=alert(1)>';
  const out = h`<div class="empty">AI 策略未產生:${err}</div>`;
  noHtml(out, ["<b>", "<svg"]);                // 原始標籤不得成形(onload= 文字惰性化)
  ok(out.includes("&lt;svg"), "<svg 被編碼");
}

// 7) h`` 預設跳脫 + raw() 通行(不二次跳脫)
{
  const frag = h`<li>${'<x>'}</li>`;          // 已跳脫的安全片段
  ok(frag === "<li>&lt;x&gt;</li>", "h`` 內插值被跳脫");
  const wrapped = h`<ul>${raw(frag)}</ul>`;    // raw 通行
  ok(wrapped === "<ul><li>&lt;x&gt;</li></ul>", "raw() 不對已安全片段二次跳脫");
  const notRaw = h`<ul>${frag}</ul>`;          // 未 raw → 會被再次跳脫(證明預設安全)
  ok(notRaw.includes("&amp;lt;"), "未 raw 的 HTML 片段被再次跳脫(預設安全)");
}

// 8) null/undefined 安全
{
  ok(esc(null) === "" && esc(undefined) === "", "null/undefined → 空字串");
}

if (failures) { console.error(`\n${failures} 項失敗`); process.exit(1); }
console.log("\n全部通過");
