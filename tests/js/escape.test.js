/* 共用 escaping 的單元測試(純 Node,無框架)。
 * 涵蓋:</div><script>、backtick、${...}、quotes、ampersand、encoded HTML、
 *      屬性 breakout、巢狀惡意值、SafeHtml 語意(h/joinSafe/trusted)。
 * 執行:node tests/js/escape.test.js(exit 0 = 通過)
 */
const path = require("path");
const X = require(path.join(__dirname, "..", "..", "app", "static", "js", "escape.js"));
const { esc, h, joinSafe, trusted, isSafe } = X;

let failures = 0;
function ok(cond, msg) {
  if (!cond) { failures++; console.error("FAIL:", msg); }
  else { console.log("ok:", msg); }
}
function noTag(s, needles) {
  for (const n of needles) ok(!String(s).includes(n), `不得含原始標籤 ${JSON.stringify(n)} → ${s}`);
}

// ── esc:各種危險字元 ──
{
  ok(esc("<") === "&lt;", "< → &lt;");
  ok(esc(">") === "&gt;", "> → &gt;");
  ok(esc("&") === "&amp;", "& → &amp;");
  ok(esc('"') === "&quot;", '" → &quot;');
  ok(esc("'") === "&#39;", "' → &#39;");
  ok(esc("/") === "&#47;", "/ → &#47;");
  ok(esc(null) === "" && esc(undefined) === "", "null/undefined → 空字串");
}

// ── 必測 payloads ──
{
  // </div><script>
  const p1 = "</div><script>alert(1)</script>";
  noTag(h`<div class="mentor-memo">${p1}</div>`, ["</div><script>", "<script>", "</script>"]);

  // backtick + ${...}(不得被當成 template 執行,純文字)
  const p2 = "`${alert(1)}`";
  const out2 = String(h`<p>${p2}</p>`);
  ok(out2.includes("`") && out2.includes("${alert(1)}"), "backtick / ${} 以文字保留,不執行");
  noTag(out2, ["<script"]);

  // quotes 屬性 breakout
  const p3 = '" onmouseover="alert(1)';
  const out3 = String(h`<span title="${p3}">x</span>`);
  ok(out3.includes("&quot;"), "雙引號 → &quot;,無法跳出屬性");
  noTag(out3, ['title="" onmouseover=']);

  // ampersand(不得破壞既有實體,一律編碼)
  ok(esc("a & b &amp; c").indexOf("&amp;") === 2, "& 一律編碼(含既有 &amp; 也再編碼,安全)");

  // encoded HTML(雙重編碼攻擊嘗試)—— 應原樣當文字,不解碼
  const p5 = "&lt;script&gt;alert(1)&lt;/script&gt;";
  const out5 = String(h`<div>${p5}</div>`);
  ok(out5.includes("&amp;lt;script&amp;gt;"), "已編碼字串再被編碼(不會被瀏覽器解回標籤)");

  // img/onerror、svg/onload
  noTag(h`<div>${"<img src=x onerror=alert(1)>"}</div>`, ["<img"]);
  noTag(h`<div>${"<svg/onload=alert(1)>"}</div>`, ["<svg"]);
}

// ── 巢狀惡意值(物件欄位)──
{
  const apiObj = { name: "</td><script>steal()</script>", one_line: "<img src=x onerror=1>" };
  const frag = h`<tr><td>${apiObj.name}</td><td>${apiObj.one_line}</td></tr>`;
  noTag(frag, ["<script>", "<img", "</td><script>"]);
}

// ── SafeHtml 語意:h 回傳 SafeHtml、巢狀不二次跳脫 ──
{
  const inner = h`<li>${"<x>"}</li>`;                 // 已跳脫的安全片段
  ok(isSafe(inner), "h`` 回傳 SafeHtml");
  ok(String(inner) === "<li>&lt;x&gt;</li>", "h`` 內插值被跳脫");
  const outer = h`<ul>${inner}</ul>`;                 // SafeHtml 巢狀 → 原樣放行
  ok(String(outer) === "<ul><li>&lt;x&gt;</li></ul>", "SafeHtml 巢狀不二次跳脫,不需 bypass");
}

// ── joinSafe:片段陣列合成;非 SafeHtml 元素仍被跳脫 ──
{
  const arr = [h`<li>a</li>`, h`<li>${"<b>"}</li>`];
  ok(String(joinSafe(arr)) === "<li>a</li><li>&lt;b&gt;</li>", "joinSafe 合成 SafeHtml 陣列");
  // 若不小心傳入裸字串(HTML),joinSafe 會跳脫它(安全預設,無 bypass)
  ok(String(joinSafe(["<script>"])) === "&lt;script&gt;", "joinSafe 對裸字串仍跳脫");
}

// ── 安全預設:忘記 joinSafe 而用 .join("") 再插值 → 被再次跳脫(不 XSS)──
{
  const joined = [h`<li>x</li>`].join("");            // → 裸字串 "<li>x</li>"
  const reWrapped = String(h`<ul>${joined}</ul>`);     // 裸字串被 esc → 雙重編碼
  ok(reWrapped.includes("&lt;li&gt;"), "裸字串再插值會被跳脫(漏接只造成雙重編碼,非 XSS)");
}

// ── trusted:僅字面值(語意驗證,非可執行檢查)──
{
  ok(String(trusted("<span>ok</span>")) === "<span>ok</span>", "trusted 保留字面 HTML");
  ok(isSafe(trusted("x")), "trusted 回傳 SafeHtml");
}

if (failures) { console.error(`\n${failures} 項失敗`); process.exit(1); }
console.log("\n全部通過");
