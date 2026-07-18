"""Dashboard 後端 API(/api/candles、history、structure events、靜態首頁)。"""
from fastapi.testclient import TestClient


def get_client():
    from app.main import app
    return TestClient(app)


def test_root_serves_dashboard():
    with get_client() as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "XAUUSD" in r.text
        assert "lightweight-charts" in r.text


def test_candles_endpoint_validation():
    with get_client() as c:
        assert c.get("/api/candles?timeframe=3M").status_code == 400


def test_candles_endpoint_after_analysis():
    with get_client() as c:
        # 觸發一次分析(mock 模式)寫入 K 棒後,candles 端點應有資料
        assert c.post("/api/analysis/run").status_code == 200
        r = c.get("/api/candles?timeframe=15M&limit=50")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) > 0
        row = rows[-1]
        assert {"time", "open", "high", "low", "close", "volume", "is_closed"} <= set(row)
        times = [x["time"] for x in rows]
        assert times == sorted(times)
        assert len(set(times)) == len(times)  # 無重複 K 棒


def test_history_and_structure_events():
    with get_client() as c:
        c.post("/api/analysis/run")
        hist = c.get("/api/analysis/history?limit=5").json()
        assert len(hist) >= 1
        assert {"run_time", "market_state", "action", "grade"} <= set(hist[0])
        evs = c.get("/api/structure/events?timeframe=15M").json()
        for ev in evs:
            assert {"event_type", "time", "price", "still_valid"} <= set(ev)


def test_upcoming_events_endpoint():
    with get_client() as c:
        r = c.get("/api/events/upcoming")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
