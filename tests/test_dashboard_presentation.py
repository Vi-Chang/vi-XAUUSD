from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/css/style.css").read_text(encoding="utf-8")
JS = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")


def test_decision_home_precedes_advanced_chart_and_contains_two_routes():
    assert HTML.index('id="decision-home"') < HTML.index('class="chart-card card"')
    assert 'id="home-long-route"' in HTML
    assert 'id="home-short-route"' in HTML
    assert HTML.count('id="home-next-action"') == 1


def test_advanced_analysis_is_collapsed_by_default():
    assert ".layout > .chart-card," in CSS
    assert ".layout > .side," in CSS
    assert "body.analysis-details-open" in CSS
    assert 'id="analysis-details-toggle"' in HTML


def test_display_layer_uses_canonical_values_without_hardcoded_prices():
    assert "renderDecisionHome(canonical, finalState)" in JS
    assert "canonical.canonicalNextTrigger" in JS
    assert '(candidate || {}).entryZone' in JS
    assert "candidate?.tacticalStop" in JS
    assert "candidate?.targets" in JS
    for example_price in ("4659", "4651", "4667", "4676"):
        assert example_price not in HTML


def test_home_has_one_display_action_vocabulary_and_plain_language_state():
    assert 'const icons = {WAIT: "🟡", READY: "🟠", BUY: "🟢", SELL: "🔴", INVALID: "⚫"}' in JS
    assert 'entry.tradeStatus === "ENTRY_READY"' in JS
    assert 'canonical.setupState === "ENTRY_READY"' not in JS
    assert '["CONFIRMED", "ARMED"].includes(canonical.setupState)' not in JS
    for label in ("強勢偏多", "偏多回踩", "偏多整理", "多空拉鋸", "偏空反彈", "偏空整理", "強勢偏空", "結構失效"):
        assert label in JS


def test_critical_health_overrides_actionable_entry_display():
    assert "canonical.dataStale" in JS
    assert "canonical.closedCandleAvailable === false" in JS
    assert 'if (critical) displayState = "INVALID"' in JS
    assert "暫停進場，等待資料恢復" in JS
