/* 共用 HTML escaping(單一來源,防 XSS)。
 * - esc(v):HTML-escape 所有中繼字元(& < > " ' /)。這是「跳脫」,非「過濾」——
 *   完整編碼,不猜哪些 HTML 安全(禁止不完整 regex sanitizer)。
 * - h`...`:tagged template,預設把每個 ${} 都 esc();要放「已知安全的 HTML 片段」
 *   必須明確用 raw() 包住 —— 預設安全,漏接一個欄位也只是被跳脫,不會 XSS。
 * - raw(html):標記某字串為「已是安全 HTML」,h`` 不再對它二次跳脫。
 * UMD:瀏覽器掛在 window.XSS;Node 用 module.exports(供測試)。
 */
(function (root, factory) {
  const mod = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = mod;
  root.XSS = mod;
})(typeof self !== "undefined" ? self : this, function () {
  const RAW = "__xss_raw__";
  const MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;",
                '"': "&quot;", "'": "&#39;", "/": "&#47;" };

  function esc(v) {
    if (v == null) return "";
    return String(v).replace(/[&<>"'/]/g, function (c) { return MAP[c]; });
  }

  function raw(html) {
    return { [RAW]: html == null ? "" : String(html) };
  }

  function isRaw(v) {
    return v != null && typeof v === "object" && Object.prototype.hasOwnProperty.call(v, RAW);
  }

  function h(strings) {
    const vals = Array.prototype.slice.call(arguments, 1);
    let out = strings[0];
    for (let i = 0; i < vals.length; i++) {
      const v = vals[i];
      out += (isRaw(v) ? v[RAW] : esc(v)) + strings[i + 1];
    }
    return out;
  }

  return { esc: esc, raw: raw, h: h, isRaw: isRaw };
});
