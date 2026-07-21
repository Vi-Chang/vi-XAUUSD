"""P3:對外輸出價位統一 2 位小數(單一 formatter,內部保持全精度)。"""
from app.utils.formatting import fmt_price


def test_fmt_price():
    assert fmt_price(4066.52545) == 4066.53
    assert fmt_price(4066.5) == 4066.5
    assert fmt_price(None) is None


def test_validator_messages_use_2dp():
    from app.engines.setup_validator import validate_prices
    reasons = validate_prices("SHORT", entry=4085.14159, sl=4066.52545,
                              tps=[4050.0], current_price=4080.0)
    joined = ";".join(reasons)
    assert "4066.53" in joined and "4085.14" in joined
    assert "52545" not in joined          # 不得出現 5 位小數


def test_candles_api_rounded():
    from fastapi.testclient import TestClient

    from app.db.session import init_db
    from app.main import app
    init_db()
    with TestClient(app) as c:
        c.post("/api/analysis/run")       # 產生 mock K 棒
        rows = c.get("/api/candles?timeframe=15M&limit=20").json()
        for r in rows:
            for k in ("open", "high", "low", "close"):
                assert r[k] == round(r[k], 2), f"{k}={r[k]} 超過 2 位小數"


def test_level_dicts_rounded():
    from app.engines.key_levels import CandidateLevel
    lv = CandidateLevel("SUP_ZONE_01", "SUP_ZONE", 4012.34567, 4015.98765, "STRONG", ["t"])
    d = lv.to_dict()
    assert d["price_low"] == 4012.35 and d["price_high"] == 4015.99
