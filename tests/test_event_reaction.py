from app.engines.event_reaction import assess_event_reaction


def test_post_event_without_closed_candle_stays_waiting():
    result = assess_event_reaction(post_event_wait=True, m15_closed_at="", macd_hist=None,
                                   dxy_chg_pct=None, us10y_chg=None)
    assert result.status == "awaiting_close"


def test_post_event_requires_cross_market_confirmation():
    result = assess_event_reaction(post_event_wait=True, m15_closed_at="2026-08-13T12:30:00Z",
                                   macd_hist=0.5, dxy_chg_pct=-0.2, us10y_chg=-0.03)
    assert result.status == "confirmed"


def test_missing_cross_market_data_cannot_confirm_news_trade():
    result = assess_event_reaction(post_event_wait=True, m15_closed_at="2026-08-13T12:30:00Z",
                                   macd_hist=-0.5, dxy_chg_pct=None, us10y_chg=None)
    assert result.status == "mixed"
    assert "維持等待" in result.message
