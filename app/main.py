"""FastAPI 入口:/health、分析 API、WebSocket 推送。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app import __version__
from app.config import get_settings
from app.db.session import init_db
from app.logging_config import setup_logging
from app.notifications.telegram import build_notification_manager
from app.providers import get_primary_provider
from app.services.heartbeat import health_payload
from app.services.scheduler import build_scheduler, state

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    s = get_settings()
    init_db()
    state.provider = get_primary_provider()
    state.notifier = build_notification_manager()
    # 備援交叉驗證:主力已是 Twelve Data 時跳過(自己驗自己沒有意義)
    if (s.twelve_data_api_key and not s.mock_data_mode
            and state.provider.name != "twelve_data"):
        from app.providers.twelve_data import TwelveDataProvider
        state.secondary = TwelveDataProvider()
    scheduler = None
    if not s.disable_scheduler:
        scheduler = build_scheduler()
        scheduler.start()
        logger.info("scheduler started (mock=%s, provider=%s)",
                    s.mock_data_mode, state.provider.name)
    yield
    if scheduler:
        scheduler.shutdown(wait=False)
    if state.provider:
        await state.provider.close()
    if state.secondary:
        await state.secondary.close()


app = FastAPI(title="XAUUSD Multi-Timeframe Analysis (MVP)", version=__version__,
              lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    """供外部監控(UptimeRobot 免費方案)輪詢。"""
    return health_payload(state)


@app.get("/api/analysis/latest")
async def latest_analysis() -> dict:
    """最新分析結果(固定 JSON,spec 二十二)。"""
    if state.latest_result is not None:
        return state.latest_result
    from app.services.analysis_service import run_analysis
    result = await run_analysis(state.provider, trigger="manual")
    state.latest_result = result.model_dump()
    return state.latest_result


@app.post("/api/analysis/run")
async def trigger_analysis() -> dict:
    """使用者手動請求分析(LLM 觸發政策允許來源之一)。"""
    from app.services.analysis_service import run_analysis
    result = await run_analysis(state.provider, trigger="manual")
    state.latest_result = result.model_dump()
    return state.latest_result


@app.get("/api/price")
async def current_price() -> dict:
    tick = await state.provider.get_live_price()
    return {"symbol": tick.symbol, "bid": tick.bid, "ask": tick.ask, "mid": tick.mid,
            "spread": tick.spread, "provider": tick.provider,
            "quote_time": tick.quote_time.isoformat()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """即時推送分析結果(每次 15M 收線分析後廣播)。"""
    await ws.accept()
    state.ws_clients.add(ws)
    try:
        import json
        if state.latest_result:
            await ws.send_text(json.dumps(state.latest_result, ensure_ascii=False, default=str))
        while True:
            await ws.receive_text()  # keepalive;client 可送任意訊息
    except WebSocketDisconnect:
        pass
    finally:
        state.ws_clients.discard(ws)
