"""TMGM 價格校正(Price Offset):只改劇本輸出價,不動分析。"""
import pytest

from app.db.session import init_db
from app.services import price_offset as po


@pytest.fixture(autouse=True)
def _db():
    init_db()
    po.set_offset(0.0, "manual")  # 每測試重置
    yield
    po.set_offset(0.0, "manual")


def _sample_result() -> dict:
    return {
        "current_price": {"bid": 4017.0, "ask": 4017.4, "mid": 4017.2},
        "key_levels": {"strong_support_zones": [{"price_low": 4000.0, "price_high": 4002.0}]},
        "long_scenario": {"resolved_prices": {
            "SUP_ZONE_01": {"price_low": 4019.8, "price_high": 4020.2},
            "SWING_LOW_15M_01": {"price_low": 4025.0, "price_high": 4025.0},
        }},
        "short_scenario": {"resolved_prices": {
            "T1": {"price_low": 4010.0, "price_high": 4010.0},
        }},
    }


def test_default_offset_zero_no_change():
    r = _sample_result()
    out = po.apply_offset_to_result(r)
    assert out["long_scenario"]["resolved_prices"]["SUP_ZONE_01"]["price_low"] == 4019.8
    assert out["offset_info"]["value"] == 0.0
    assert out["offset_info"]["analysis_source"] == "TwelveData"
    assert out["offset_info"]["trading_broker"] == "TMGM"


def test_offset_applied_to_scenario_prices_only():
    po.set_offset(-0.48, "manual")
    r = _sample_result()
    out = po.apply_offset_to_result(r)
    lp = out["long_scenario"]["resolved_prices"]
    # 進場 4020 → 4019.52,停損 4025 → 4024.52(spec 範例)
    assert lp["SUP_ZONE_01"]["price_low"] == 4019.32   # 4019.8 - 0.48
    assert lp["SUP_ZONE_01"]["price_high"] == 4019.72
    assert lp["SWING_LOW_15M_01"]["price_low"] == 4024.52
    assert lp["SWING_LOW_15M_01"]["offset_applied"] == -0.48
    # 停利 4010 → 4009.52
    assert out["short_scenario"]["resolved_prices"]["T1"]["price_low"] == 4009.52


def test_analysis_fields_untouched():
    po.set_offset(-0.48, "manual")
    r = _sample_result()
    out = po.apply_offset_to_result(r)
    # 分析價(current_price)與支撐壓力區(key_levels)保持 TwelveData 原值
    assert out["current_price"]["mid"] == 4017.2
    assert out["key_levels"]["strong_support_zones"][0]["price_low"] == 4000.0
    # 原始輸入未被 mutate(deepcopy)
    assert r["long_scenario"]["resolved_prices"]["SUP_ZONE_01"]["price_low"] == 4019.8


def test_offset_persists_and_mode():
    po.set_offset(1.25, "manual")
    val, mode = po.get_offset()
    assert val == 1.25 and mode == "manual"
    po.set_offset(mode="auto")
    val, mode = po.get_offset()
    assert val == 1.25 and mode == "auto"   # 只改模式,值保留


def test_invalid_mode_rejected():
    with pytest.raises(ValueError):
        po.set_offset(mode="xyz")


def test_offset_api_roundtrip():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/offset", json={"value": -0.48, "mode": "manual"})
        assert r.status_code == 200
        info = r.json()
        assert info["value"] == -0.48
        assert info["auto_available"] is False
        assert c.get("/api/offset").json()["value"] == -0.48
        # 分析輸出的劇本價已套用 offset
        a = c.get("/api/analysis/latest").json()
        assert a["offset_info"]["value"] == -0.48
        for sc in (a["long_scenario"], a["short_scenario"]):
            for lv in sc.get("resolved_prices", {}).values():
                assert lv.get("offset_applied") == -0.48
        c.post("/api/offset", json={"value": 0.0})
