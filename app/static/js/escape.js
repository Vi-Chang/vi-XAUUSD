/* 共用 HTML escaping(單一來源,防 XSS)—— 以品牌型別 SafeHtml 收斂,
 * 讓「安全片段的組合」不需要通用 bypass。
 *
 * - esc(v):把任意值轉為「安全的 HTML 文字」。SafeHtml 直接放行,其餘一律完整跳脫。
 * - h`...`:tagged template。每個 ${} 都經 esc();回傳 SafeHtml(已跳脫)。
 *          巢狀:h`` 的結果是 SafeHtml,插進外層 h`` 會「原樣放行、不二次跳脫」——
 *          所以巢狀片段不需要任何 bypass 包裝。
 * - joinSafe(arr):把「片段陣列」合成單一 SafeHtml(非 SafeHtml 的元素會被跳脫)。
 *                取代 `.map(h``).join("")`,避免中途變成裸字串。
 * - trusted(s):把「程式碼內的固定可信 HTML 字面值」標記為 SafeHtml。
 *              **僅可用於字串字面值**;嚴禁傳入任何 API/DB/LLM/provider/使用者資料。
 *
 * 安全不變式:唯一能產生「原始 HTML」的途徑是 SafeHtml;而 SafeHtml 只可能來自
 * h``(其插值已跳脫)、joinSafe(同上)、或 trusted(字面值)。任何外部資料只要經過
 * h``/esc 就被跳脫,漏接也只是被跳脫(頂多雙重編碼的顯示問題),不會 XSS。
 *
 * UMD:瀏覽器掛在 window.XSS;Node 用 module.exports(供測試)。
 */
(function (root, factory) {
  const mod = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = mod;
  root.XSS = mod;
})(typeof self !== "undefined" ? self : this, function () {
  const MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;",
                '"': "&quot;", "'": "&#39;", "/": "&#47;" };

  function SafeHtml(value) { this.value = value; }
  SafeHtml.prototype.toString = function () { return this.value; };

  function isSafe(v) { return v instanceof SafeHtml; }

  function esc(v) {
    if (v == null) return "";
    if (v instanceof SafeHtml) return v.value;          // 已安全 → 原樣放行
    return String(v).replace(/[&<>"'/]/g, function (c) { return MAP[c]; });
  }

  function h(strings) {
    const vals = Array.prototype.slice.call(arguments, 1);
    let out = strings[0];
    for (let i = 0; i < vals.length; i++) {
      out += esc(vals[i]) + strings[i + 1];             // 預設跳脫每個 ${}
    }
    return new SafeHtml(out);
  }

  function joinSafe(arr, sep) {
    const s = sep == null ? "" : String(sep);
    return new SafeHtml((arr || []).map(esc).join(s));  // 非 SafeHtml 元素也會被跳脫
  }

  // 僅供「程式碼內固定可信 HTML 字面值」使用;不得傳入任何外部資料。
  function trusted(literalHtml) {
    return new SafeHtml(literalHtml == null ? "" : String(literalHtml));
  }

  return {
    esc: esc, h: h, joinSafe: joinSafe, trusted: trusted,
    isSafe: isSafe, SafeHtml: SafeHtml,
    raw: trusted,   // 向後相容別名(語意同 trusted:僅限字面值)
  };
});
