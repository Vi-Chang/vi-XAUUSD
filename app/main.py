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


@app.get("/", include_in_schema=False)
async def index():
    """簡易首頁:系統狀態一覽與端點連結(完整 Dashboard 為 Phase 8)。"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse("""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>XAUUSD 分析系統</title>
<style>
  body{font-family:system-ui,-apple-system,"Noto Sans TC",sans-serif;max-width:720px;
       margin:40px auto;padding:0 20px;line-height:1.7;color:#222;background:#fafafa}
  @media (prefers-color-scheme:dark){body{color:#ddd;background:#111}a{color:#7ab8ff}
    .card{background:#1b1b1b;border-color:#333}}
  h1{font-size:1.4rem}
  .card{background:#fff;border:1px solid #e3e3e3;border-radius:10px;padding:16px 20px;margin:14px 0}
  code{background:rgba(127,127,127,.15);padding:2px 6px;border-radius:5px}
  #status{white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:.85rem}
</style></head><body>
<h1>XAUUSD 即時多週期分析系統(MVP)</h1>
<div class="card">
  <b>API 端點</b><br>
  <a href="/health">/health</a> — 系統健康與資料源狀態<br>
  <a href="/api/analysis/latest">/api/analysis/latest</a> — 最新完整分析(固定 JSON)<br>
  <a href="/api/price">/api/price</a> — 即時報價<br>
  <a href="/docs">/docs</a> — API 文件(Swagger)
</div>
<div class="card"><b>目前狀態</b><div id="status">載入中…</div></div>
<div class="card" style="font-size:.85rem;opacity:.75">
  本系統僅提供分析與通知,不執行任何交易(AUTO_TRADING_ENABLED=false)。<br>
  日線以紐約 17:00 ET 切分;結構判定只使用已收線 K 棒;沒有優勢就等待。
</div>
<script>
(async()=>{
  try{
    const h=await (await fetch('/health')).json();
    const a=await (await fetch('/api/analysis/latest')).json();
    document.getElementById('status').textContent=
      `市場:${h.market_open?'開盤中':'休市'} | 資料源:${h.provider} | 系統:${h.status}\\n`+
      `狀態:${a.market_state} | 決策:${a.decision.action}(${a.decision.confidence_grade})\\n`+
      `價格:${a.current_price.mid ?? 'n/a'} | 資料品質:${a.data_quality.status}\\n`+
      `${a.summary_zh_tw}\\n提醒:${a.most_likely_user_mistake_now}`;
  }catch(e){document.getElementById('status').textContent='狀態載入失敗:'+e}
})();
</script></body></html>""")


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
