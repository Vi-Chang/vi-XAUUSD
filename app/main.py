"""FastAPI 入口:Dashboard、K 棒 API、分析 API、WebSocket 即時推送。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __version__
from app.config import get_settings
from app.db.session import init_db
from app.logging_config import setup_logging
from app.notifications.telegram import build_notification_manager
from app.providers import get_primary_provider
from app.services.heartbeat import health_payload
from app.services.scheduler import build_scheduler, state
from app.utils.timeutils import ensure_utc

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
CHART_TIMEFRAMES = ("15M", "1H", "4H", "1D")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    s = get_settings()
    init_db()
    state.provider = get_primary_provider()
    from app.providers import get_fast_quote_provider
    state.fast_provider = get_fast_quote_provider()
    logger.info("tiered: fast quote provider = %s",
                state.fast_provider.name if state.fast_provider else
                f"none (L1 degraded to {state.provider.name})")
    state.notifier = build_notification_manager()
    # 備援交叉驗證:主力已是 Twelve Data 時跳過(自己驗自己沒有意義)
    if (s.twelve_data_api_key and not s.mock_data_mode
            and state.provider.name != "twelve_data"):
        from app.providers.twelve_data import TwelveDataProvider
        state.secondary = TwelveDataProvider()
    from datetime import datetime, timezone
    state.started_at = datetime.now(timezone.utc)
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
    if state.fast_provider:
        await state.fast_provider.close()
    if state.secondary:
        await state.secondary.close()


app = FastAPI(title="XAUUSD Multi-Timeframe Analysis (MVP)", version=__version__,
              lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Dashboard(深色交易終端風格;完整功能見 app/static/)。"""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict:
    """供外部監控(UptimeRobot 免費方案)輪詢。"""
    return health_payload(state)


def _serve_result(raw: dict) -> dict:
    """統一讀取邊界:TMGM Offset 校正 → 時效/一致性標記(BUGFIX R2/R4/R6)。"""
    from app.services.freshness import annotate_freshness
    from app.services.price_offset import apply_offset_to_result
    tick = state.quote_cache.fresh_tick(max_age_seconds=600) if state.quote_cache else None
    return annotate_freshness(apply_offset_to_result(raw),
                              current_mid=tick.mid if tick else None)


@app.get("/api/analysis/latest")
async def latest_analysis() -> dict:
    """最新分析結果(固定 JSON,spec 二十二)。輸出時套用 Offset + 時效標記。"""
    if state.latest_result is None:
        from app.services.analysis_service import run_analysis
        result = await run_analysis(state.provider, trigger="manual")
        state.latest_result = result.model_dump()
    return _serve_result(state.latest_result)


@app.post("/api/analysis/run")
async def trigger_analysis() -> dict:
    """使用者手動請求分析(LLM 觸發政策允許來源之一)。"""
    from app.services.analysis_service import run_analysis
    result = await run_analysis(state.provider, trigger="manual")
    state.latest_result = result.model_dump()   # 儲存 TwelveData 原值(分析真值)
    return _serve_result(state.latest_result)


class MentorSignalReq(BaseModel):
    direction: str
    entry_price: float
    stop_loss: float | None = None
    targets: list[float] = Field(default_factory=list)
    note: str | None = None


@app.get("/api/mentor/signals")
async def get_mentor_signals() -> dict:
    """老師帶單(僅供參考)+ 與目前系統方向的比對。"""
    from app.services.mentor_service import comparison_block
    action = "NO_TRADE"
    if state.latest_result:
        action = state.latest_result.get("decision", {}).get("action", "NO_TRADE")
    cur = None
    try:
        cur = (await state.provider.get_live_price()).mid
    except Exception:  # noqa: BLE001
        pass
    return comparison_block(action, cur)


@app.post("/api/mentor/signals")
async def create_mentor_signal(req: MentorSignalReq) -> dict:
    """新增一筆老師帶單(不算持倉,純參考比對)。"""
    from app.services.mentor_service import create_signal
    try:
        return create_signal(direction=req.direction, entry_price=req.entry_price,
                             stop_loss=req.stop_loss, targets=req.targets, note=req.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/mentor/history")
async def get_mentor_history() -> dict:
    """老師帶單歷史紀錄(CLOSED 匯入單)+ 統計 + 已知缺口。與進行中訊號分開。"""
    from app.services.mentor_service import history_block
    return history_block()


@app.post("/api/mentor/signals/{signal_id}/deactivate")
async def deactivate_mentor_signal(signal_id: int) -> dict:
    from app.services.mentor_service import deactivate_signal
    try:
        deactivate_signal(signal_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


@app.get("/api/offset")
async def get_offset_api() -> dict:
    """TMGM 價格校正資訊(右上角資訊面板 + 校正說明)。"""
    from app.services.price_offset import offset_info
    return offset_info()


class OffsetReq(BaseModel):
    value: float | None = None
    mode: str | None = None


@app.post("/api/offset")
async def set_offset_api(req: OffsetReq) -> dict:
    """手動修改 Offset 值或模式;即時生效,不重跑分析。"""
    from app.services.price_offset import offset_info, set_offset
    try:
        set_offset(req.value, req.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return offset_info()


@app.get("/api/analysis/history")
async def analysis_history(limit: int = 20) -> list[dict]:
    """歷史分析紀錄(復盤分頁用)。"""
    from sqlalchemy import select

    from app.db.models import AnalysisRun
    from app.db.session import db_session
    limit = max(1, min(limit, 100))
    with db_session() as db:
        rows = db.execute(select(AnalysisRun)
                          .order_by(AnalysisRun.run_time.desc())
                          .limit(limit)).scalars().all()
    return [{
        "run_time": ensure_utc(r.run_time).isoformat(),
        "trigger": r.trigger, "market_state": r.market_state,
        "action": r.decision_action, "grade": r.confidence_grade,
        "evidence_score": r.evidence_score, "quality": r.data_quality_status,
    } for r in rows]


@app.get("/api/candles")
async def candles_api(timeframe: str = "15M", limit: int = 300) -> list[dict]:
    """資料庫已儲存 K 棒(圖表用;與分析引擎同一份資料,spec 之一致性要求)。"""
    if timeframe not in CHART_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {CHART_TIMEFRAMES}")
    limit = max(10, min(limit, 1000))
    from sqlalchemy import select

    from app.db.models import Candle
    from app.db.session import db_session
    with db_session() as db:
        rows = db.execute(select(Candle)
                          .where(Candle.symbol == "XAUUSD", Candle.timeframe == timeframe)
                          .order_by(Candle.open_time.desc(), Candle.received_at.desc())
                          .limit(limit * 2)).scalars().all()
    seen: set = set()
    out: list[dict] = []
    for r in rows:  # 同一 open_time 取最新 received_at(desc 排序下先出現者)
        t = ensure_utc(r.open_time)
        if t in seen:
            continue
        seen.add(t)
        out.append({"time": int(t.timestamp()), "open": r.open, "high": r.high,
                    "low": r.low, "close": r.close, "volume": r.volume,
                    "is_closed": r.is_closed})
    out.reverse()
    return out[-limit:]


@app.get("/api/structure/events")
async def structure_events(timeframe: str = "15M", limit: int = 40) -> list[dict]:
    """市場結構事件(圖表標記 BOS/CHoCH/假突破用)。"""
    from sqlalchemy import select

    from app.db.models import MarketStructure
    from app.db.session import db_session
    limit = max(1, min(limit, 200))
    with db_session() as db:
        rows = db.execute(select(MarketStructure)
                          .where(MarketStructure.timeframe == timeframe)
                          .order_by(MarketStructure.event_time.desc())
                          .limit(limit)).scalars().all()
    return [{
        "event_type": r.event_type,
        "time": int(ensure_utc(r.event_time).timestamp()),
        "price": r.price, "still_valid": r.still_valid,
    } for r in rows]


@app.get("/api/events/upcoming")
async def upcoming_events(limit: int = 5) -> list[dict]:
    """即將到來的高影響經濟事件(倒數計時與時間軸標記用)。"""
    from datetime import datetime, timezone

    from app.services.event_service import load_manual_events, translate_event_name
    try:
        events, _ = load_manual_events()
    except Exception:  # noqa: BLE001
        return []
    now = datetime.now(timezone.utc)
    out = []
    for ev in events:
        try:
            t = datetime.fromisoformat(ev["time_utc"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if t >= now:
            out.append({"name": ev.get("name"),
                        "name_zh": translate_event_name(ev.get("name", "")),
                        "country": ev.get("country"),
                        "impact": ev.get("impact"), "time": int(t.timestamp())})
    out.sort(key=lambda e: e["time"])
    return out[:limit]


# ── 手動持倉管理(spec 十三 C 手動輸入途徑)──────────────────
class PositionCreateReq(BaseModel):
    side: str
    entry_price: float
    stop_loss: float | None = None
    lot_size: float = Field(gt=0)
    planned_targets: list[float] = Field(default_factory=list)
    account_id: int | None = None  # 未指定時掛預設 SELF 帳戶


class StopModifyReq(BaseModel):
    stop_loss: float


class PartialExitReq(BaseModel):
    percent: float = Field(gt=0, le=100)
    price: float | None = None  # 未提供時使用當前市價


class CloseReq(BaseModel):
    price: float | None = None


async def _price_or_market(price: float | None) -> float:
    if price is not None:
        return price
    tick = await state.provider.get_live_price()
    return tick.mid


@app.get("/api/accounts")
async def get_accounts() -> list[dict]:
    """帳戶清單(帳戶A 老師帶單 / 帳戶B 自己交易,可擴充)。"""
    from app.services.account_service import list_accounts
    return list_accounts()


@app.get("/api/accounts/comparison")
async def accounts_comparison() -> dict:
    """對照頁:各帳戶分開統計並列(spec 二十四指標)。"""
    from app.services.account_service import comparison
    return comparison()


@app.get("/api/positions")
async def get_positions(include_closed: bool = True,
                        account_id: int | None = None) -> list[dict]:
    from app.services.position_service import list_positions, position_view
    try:
        tick = await state.provider.get_live_price()
        cur = tick.mid
    except Exception:  # noqa: BLE001
        cur = None
    return [position_view(p, cur)
            for p in list_positions(include_closed=include_closed, account_id=account_id)]


@app.post("/api/positions")
async def create_position_api(req: PositionCreateReq) -> dict:
    from app.services.position_service import create_position, position_view
    try:
        pos = create_position(side=req.side, entry_price=req.entry_price,
                              stop_loss=req.stop_loss, lot_size=req.lot_size,
                              planned_targets=req.planned_targets,
                              account_id=req.account_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    cur = await _price_or_market(None) if state.provider else None
    return position_view(pos, cur)


@app.post("/api/positions/{position_id}/stop")
async def modify_stop_api(position_id: int, req: StopModifyReq) -> dict:
    from app.services.position_service import modify_stop, position_view
    try:
        pos, flag = modify_stop(position_id, req.stop_loss)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    cur = await _price_or_market(None)
    out = position_view(pos, cur)
    out["behavior_flag"] = flag
    if flag and state.notifier:
        await state.notifier.notify("RISK", f"behavior:{flag}",
                                    f"交易教練:偵測到 {flag}(停損往虧損方向移動)。"
                                    f"請恢復原結構失效點停損。")
    return out


@app.post("/api/positions/{position_id}/partial_exit")
async def partial_exit_api(position_id: int, req: PartialExitReq) -> dict:
    from app.services.position_service import partial_exit, position_view
    price = await _price_or_market(req.price)
    try:
        pos, flag = partial_exit(position_id, req.percent, price)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    out = position_view(pos, price)
    out["behavior_flag"] = flag
    return out


@app.post("/api/positions/{position_id}/close")
async def close_position_api(position_id: int, req: CloseReq) -> dict:
    from app.services.position_service import close_position, position_view
    price = await _price_or_market(req.price)
    try:
        pos, flag = close_position(position_id, price)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    out = position_view(pos, price)
    out["behavior_flag"] = flag
    return out


@app.get("/api/behavior/flags")
async def behavior_flags(limit: int = 20) -> list[dict]:
    from app.services.position_service import recent_behavior_flags
    return recent_behavior_flags(limit=max(1, min(limit, 100)))


@app.get("/api/price")
async def current_price() -> dict:
    tick = await state.provider.get_live_price()
    return {"symbol": tick.symbol, "bid": tick.bid, "ask": tick.ask, "mid": tick.mid,
            "spread": tick.spread, "provider": tick.provider,
            "quote_time": tick.quote_time.isoformat()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """即時推送:tick(未收線 K 棒跳動)、candle_closed、analysis。"""
    await ws.accept()
    state.ws_clients.add(ws)
    try:
        import json
        if state.latest_result:
            await ws.send_text(json.dumps(
                {"type": "analysis", "data": _serve_result(state.latest_result)},
                ensure_ascii=False, default=str))
        while True:
            await ws.receive_text()  # keepalive;client 可送任意訊息
    except WebSocketDisconnect:
        pass
    finally:
        state.ws_clients.discard(ws)
