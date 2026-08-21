"""XSS 回歸(Phase 1):共用 escaping 行為 + 前端使用方式的靜態防護。

- JS 邏輯測試交給 Node(tests/js/escape.test.js);本機無 node 時跳過該段。
- 靜態防護:確保危險欄位(老師 note、AI 輸出)經過 h`` 跳脫、無殘留 inline onclick、
  escape.js 已載入 —— 防止日後有人又用未跳脫的 `${x}` 直接塞 innerHTML。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
ESCAPE_JS = ROOT / "app" / "static" / "js" / "escape.js"
NODE_TEST = ROOT / "tests" / "js" / "escape.test.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="無 node,略過 JS 邏輯測試")
def test_escape_js_behavior_via_node():
    """<script> / <img onerror> / 屬性 breakout / note / AI / provider error 跳脫。"""
    r = subprocess.run(["node", str(NODE_TEST)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=60)
    assert r.returncode == 0, f"node XSS 測試失敗:\n{r.stdout}\n{r.stderr}"


def test_escape_module_exists_and_loaded():
    assert ESCAPE_JS.exists(), "缺少共用 escaping 模組 escape.js"
    # escape.js 必須在 app.js 之前載入
    assert "/static/js/escape.js" in INDEX
    assert INDEX.index("escape.js") < INDEX.index("js/app.js")


def test_single_shared_escaper_imported():
    assert "window.XSS;" in APP_JS
    assert "joinSafe" in APP_JS and "trusted" in APP_JS


def test_no_generic_raw_bypass_in_app_js():
    """不得再有通用 raw() bypass;僅允許窄化的 trusted()(且只包字串字面值)。"""
    import re
    code = "\n".join(l for l in APP_JS.splitlines() if not l.strip().startswith("//"))
    # app.js 不得呼叫 raw((escape.js 內的別名不算)
    assert not re.search(r'[^A-Za-z_]raw\s*\(', code), "app.js 仍有通用 raw() bypass"
    # 每個 trusted(...) 的引數必須是非空字串字面值('...' 或 "...")
    hits = re.findall(r'trusted\(\s*(.+?)\s*\)', code)
    assert hits, "預期 app.js 有 trusted() 使用"
    for arg in hits:
        assert (len(arg) >= 2 and arg[0] in "'\"" and arg[-1] == arg[0]), \
            f"trusted() 只能包字串字面值,發現:{arg!r}"


def test_no_inline_event_handlers_in_app_js():
    """動態產生的 HTML 不得含 inline onclick(改用事件委派;配合嚴格 CSP)。"""
    import re
    # 排除註解行後,不得出現 onX= 內嵌事件屬性
    code = "\n".join(l for l in APP_JS.splitlines() if not l.strip().startswith("//"))
    assert not re.search(r'on(click|error|load|mouseover|change|submit)=', code), \
        "app.js 仍有 inline 事件處理器"


def test_dangerous_fields_go_through_h_template():
    """危險欄位所在的 innerHTML 一律用 h``(預設跳脫),不得用裸反引號模板。"""
    import re
    # 不得出現「innerHTML = `」(帶插值的裸模板);靜態單引號字串允許
    assert not re.search(r'innerHTML\s*=\s*`', APP_JS), \
        "發現未經 h`` 的 innerHTML 模板(可能有 XSS)"
    # 具體危險欄位確有經 h`` 呈現
    assert 'class="ai-oneliner">${ai.one_liner' in APP_JS         # AI one-liner


def test_mutation_calls_use_credentials():
    """postJSON 需帶 same-origin 憑證(session cookie),並在 401 時走共享登入重試。"""
    assert 'credentials: "same-origin"' in APP_JS
    assert "ensureLogin" in APP_JS and "/api/admin/login" in APP_JS


def test_no_permanent_token_persisted_in_frontend():
    """永久 token 不得寫入 localStorage/sessionStorage/URL/cookie。"""
    assert "localStorage" not in APP_JS and "sessionStorage" not in APP_JS
    # 不得把 token 放進 document.cookie 或 URL query
    import re
    assert not re.search(r'document\.cookie\s*=', APP_JS)
    assert "token=" not in APP_JS.replace('{ token }', '').replace('"token"', '')
