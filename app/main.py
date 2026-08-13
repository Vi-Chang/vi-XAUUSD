"""FastAPI 入口:Dashboard、K 棒 API、分析 API、WebSocket 即時推送。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __version__
from app.config import get_settings
from app.db.session import init_db
from app.logging_config import setup_logging
from app.notifications.telegram import build_notification_manager
from app.providers import get_primary_provider
from app.security import (
    admin_status,
    clear_session_cookie,
    create_session,
    destroy_session,
    rate_limit,
    rate_limit_window,
    require_admin,
    set_session_cookie,
    token_matches,
)
from app.services.heartbeat import health_payload, liveness_payload, readiness_payload
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
    # 容器重啟後從持久化分析恢復健康狀態，避免 API 有資料但
    # last_full_analysis 仍為空的跨程序／滾動部署不一致。
    _load_last_analysis_from_db()
    # 組態安全檢查:production 寫入權限組態有誤 → 明確在啟動時失敗(不等到 mutation 才發現)。
    # 不中止行程(讓公開 dashboard/health 仍可服務),但 readiness 會回報 not-ready。
    from app.security import production_auth_misconfigured
    _auth_bad, _auth_why = production_auth_misconfigured()
    if _auth_bad:
        logger.critical("SECURITY: APP_ENV=production 寫入權限組態有誤(%s)—— "
                        "所有寫入端點已停用(fail-closed),/health/ready 將回報 not-ready。"
                        "請修正組態後重啟。", _auth_why)
    scheduler = None
    if not s.disable_scheduler:
        scheduler = build_scheduler()
        scheduler.start()
        state.scheduler_started = True
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


# production 關閉互動式 API 文件(/docs、/redoc、/openapi.json),縮小管理端點的可探測面。
# 單人正式產品不需要公開 API explorer;開發/測試環境保留以便除錯。
_docs_off = get_settings().app_env == "production"
app = FastAPI(
    title="XAUUSD Multi-Timeframe Analysis (MVP)", version=__version__, lifespan=lifespan,
    docs_url=None if _docs_off else "/docs",
    redoc_url=None if _docs_off else "/redoc",
    openapi_url=None if _docs_off else "/openapi.json",
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# script-src 'self':所有腳本皆同源外部檔(圖表庫/messages/escape/app),不用 inline。
# style-src 允許 'unsafe-inline':lightweight-charts 會動態設定 inline style,且首頁有
# 少量 inline style;樣式注入風險遠低於腳本,權衡後保留。
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    return response


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Dashboard(深色交易終端風格;完整功能見 app/static/)。"""
    return FileResponse(STATIC_DIR / "index.html")


# ── 管理權限:token → 短效 session(瀏覽器操作用;永久 token 不外洩)──
class AdminLoginReq(BaseModel):
    token: str | None = None   # 也可改用 X-Admin-Token header 傳入


@app.post("/api/admin/login")
async def admin_login(request: Request, response: Response,
                      req: AdminLoginReq | None = None) -> dict:
    """以管理 token 換取 HttpOnly session cookie。token 可走 header 或 body。

    - 獨立的登入頻率限制(滑動視窗)防暴力猜 token。
    - 失敗一律相同固定訊息,不洩露 token 是否接近/長度/其他差異(constant-time 比對)。
    - 回應與 log 不含 token。
    """
    s = get_settings()
    rate_limit_window("admin_login", s.admin_login_max_attempts, s.admin_login_window_seconds)
    from app.security import HEADER_NAME
    provided = request.headers.get(HEADER_NAME) or (req.token if req else None)
    if not token_matches(provided):
        raise HTTPException(status_code=401, detail="管理憑證無效。")
    sid, ttl = create_session()
    set_session_cookie(response, sid, ttl)
    return {"ok": True}


@app.post("/api/admin/logout")
async def admin_logout(request: Request, response: Response) -> dict:
    destroy_session(request.cookies.get(get_settings().admin_session_cookie))
    clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/admin/status")
async def admin_status_api(request: Request) -> dict:
    """前端判斷是否需要/已登入(不洩露 token 內容)。"""
    return admin_status(request)


@app.get("/health")
async def health() -> dict:
    """綜合監控(供 UptimeRobot 等)。保留原有欄位並附 readiness 摘要。"""
    return health_payload(state)


@app.get("/health/live")
async def health_live() -> dict:
    """Liveness:行程是否存活。外部 provider 暫時失敗不影響此判定,恆回 200。"""
    return liveness_payload(state)


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
    """Readiness:是否能提供新鮮分析。休市為正常(ready),非 stale failure。

    未就緒回 503(含原因),供負載平衡/監控判斷是否導流。
    """
    payload = readiness_payload(state)
    code = 200 if payload.get("ready") else 503
    return JSONResponse(payload, status_code=code)


def _current_mid() -> float | None:
    tick = state.quote_cache.fresh_tick(max_age_seconds=600) if state.quote_cache else None
    return tick.mid if tick else None


def _serve_result(raw: dict) -> dict:
    """私人/完整讀取邊界:TMGM Offset 校正 → 時效/一致性標記(BUGFIX R2/R4/R6)。"""
    from app.services.freshness import annotate_freshness
    from app.services.price_offset import apply_offset_to_result
    return annotate_freshness(apply_offset_to_result(raw), current_mid=_current_mid())


def _serve_public(raw: dict) -> dict:
    """公開讀取邊界:不套用個人 offset(用分析原始價位)→ 時效標記 → 公開投影(allowlist)。"""
    from app.services.freshness import annotate_freshness
    from app.services.public_view import public_analysis
    annotated = annotate_freshness(raw, current_mid=_current_mid())
    return public_analysis(annotated)


def _load_last_analysis_from_db() -> dict | None:
    """讀取 DB 最後一筆成功分析(唯讀,無副作用);供匿名首載使用,不觸發新分析。"""
    from sqlalchemy import select

    from app.db.models import AnalysisRun
    from app.db.session import db_session
    try:
        with db_session() as db:
            row = db.execute(select(AnalysisRun)
                             .order_by(AnalysisRun.run_time.desc())
                             .limit(1)).scalars().first()
            if not row or not row.result_json:
                return None
            raw = dict(row.result_json)
            state.latest_result = raw
            state.last_full_analysis = ensure_utc(row.run_time)
            return raw
    except Exception as exc:  # noqa: BLE001
        logger.warning("load last analysis failed: %s", exc)
        return None


async def _run_full_analysis_shared(trigger: str) -> dict:
    """經 single-flight 執行核心分析並更新共享狀態(手動/首載共用)。"""
    import asyncio
    from datetime import datetime, timezone

    from app.services.single_flight import run_analysis_shared
    try:
        result = await run_analysis_shared(state.provider, trigger=trigger)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=503,
                            detail="分析忙碌中,請稍後再試。") from exc
    state.latest_result = result.model_dump()   # 儲存 TwelveData 原值(分析真值)
    state.last_full_analysis = datetime.now(timezone.utc)
    return state.latest_result


@app.get("/api/analysis/latest")
async def latest_analysis(request: Request) -> dict:
    """最新分析結果(公開市場分析投影)。

    無副作用:匿名/已登入的 GET 皆不得觸發 LLM / provider refresh / 新 AnalysisRun /
    Telegram。無記憶體快取時,唯讀 DB 最後一筆;仍無 → 回安全的「尚無分析」狀態。
    只有受保護的 POST /api/analysis/run 或排程可產生新分析。
    """
    from app.security import is_authorized
    raw = state.latest_result
    if raw is None:
        raw = _load_last_analysis_from_db()      # 唯讀,不觸發任何成本行為
        if raw is not None:
            state.latest_result = raw            # read-through 快取
    if raw is None:
        return {"public": True, "available": False,
                "reason": "尚無分析結果,請稍後(排程或管理員觸發後產生)"}
    # 已登入(admin)→ 完整私人 payload;匿名 → 公開投影(移除持倉/老師/紀律等私人資料)
    ok, _via = is_authorized(request)
    return _serve_result(raw) if ok else _serve_public(raw)


@app.post("/api/analysis/run", dependencies=[Depends(require_admin)])
async def trigger_analysis() -> dict:
    """使用者手動請求分析(受管理權限保護 + 節流,避免濫用與重跑)。"""
    rate_limit("analysis_run", get_settings().analysis_run_cooldown_seconds)
    await _run_full_analysis_shared("manual")
    return _serve_result(state.latest_result)


class MentorSignalReq(BaseModel):
    direction: str
    entry_price: float
    stop_loss: float | None = None
    targets: list[float] = Field(default_factory=list)
    note: str | None = None


@app.get("/api/mentor/signals", dependencies=[Depends(require_admin)])
async def get_mentor_signals() -> dict:
    """老師帶單(僅供參考)+ 與目前系統方向的比對。"""
    from app.services.mentor_service import comparison_block
    action = "NO_TRADE"
    if state.latest_result:
        action = state.latest_result.get("decision", {}).get("action", "NO_TRADE")
    cur = None
    try:
        cur = (await state.provider.get_live_price()).mid
    except Exception:
        logger.debug("comparison live price unavailable", exc_info=True)
    return comparison_block(action, cur)


@app.post("/api/mentor/signals", dependencies=[Depends(require_admin)])
async def create_mentor_signal(req: MentorSignalReq) -> dict:
    """新增一筆老師帶單(不算持倉,純參考比對)。"""
    from app.services.mentor_service import create_signal
    try:
        return create_signal(direction=req.direction, entry_price=req.entry_price,
                             stop_loss=req.stop_loss, targets=req.targets, note=req.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/mentor/history", dependencies=[Depends(require_admin)])
async def get_mentor_history() -> dict:
    """老師帶單歷史紀錄(CLOSED 匯入單)+ 統計 + 已知缺口。與進行中訊號分開。"""
    from app.services.mentor_service import history_block
    return history_block()


@app.post("/api/mentor/signals/{signal_id}/deactivate", dependencies=[Depends(require_admin)])
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


@app.post("/api/offset", dependencies=[Depends(require_admin)])
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


@app.get("/api/performance")
async def performance_api(limit: int = 5000) -> dict:
    """匿名規則訊號績效；不包含帳戶、部位或個人交易資料。"""
    from app.db.session import db_session
    from app.services.performance_service import performance_report
    limit = max(100, min(limit, 10000))
    with db_session() as db:
        return performance_report(db, limit=limit)


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
        from app.utils.formatting import fmt_price
        out.append({"time": int(t.timestamp()), "open": fmt_price(r.open),
                    "high": fmt_price(r.high), "low": fmt_price(r.low),
                    "close": fmt_price(r.close), "volume": r.volume,
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
    from app.utils.formatting import fmt_price
    return [{
        "event_type": r.event_type,
        "time": int(ensure_utc(r.event_time).timestamp()),
        "price": fmt_price(r.price), "still_valid": r.still_valid,
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
    position_timeframe: Literal["15M", "1H", "4H", "1D", "unknown"] = "unknown"
    original_thesis: str = Field(default="", max_length=500)
    max_loss_usd: float | None = Field(default=None, gt=0)
    allow_event_hold: bool | None = None


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


@app.get("/api/accounts", dependencies=[Depends(require_admin)])
async def get_accounts() -> list[dict]:
    """帳戶清單(帳戶A 老師帶單 / 帳戶B 自己交易,可擴充)。"""
    from app.services.account_service import list_accounts
    return list_accounts()


@app.get("/api/accounts/comparison", dependencies=[Depends(require_admin)])
async def accounts_comparison() -> dict:
    """對照頁:各帳戶分開統計並列(spec 二十四指標)。"""
    from app.services.account_service import comparison
    return comparison()


@app.get("/api/positions", dependencies=[Depends(require_admin)])
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


@app.post("/api/positions", dependencies=[Depends(require_admin)])
async def create_position_api(req: PositionCreateReq) -> dict:
    from app.services.position_service import create_position, position_view
    try:
        pos = create_position(side=req.side, entry_price=req.entry_price,
                              stop_loss=req.stop_loss, lot_size=req.lot_size,
                              planned_targets=req.planned_targets,
                              account_id=req.account_id,
                              position_timeframe=req.position_timeframe,
                              original_thesis=req.original_thesis,
                              max_loss_usd=req.max_loss_usd,
                              allow_event_hold=req.allow_event_hold)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    cur = await _price_or_market(None) if state.provider else None
    return position_view(pos, cur)


@app.post("/api/positions/{position_id}/stop", dependencies=[Depends(require_admin)])
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


@app.post("/api/positions/{position_id}/partial_exit", dependencies=[Depends(require_admin)])
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


@app.post("/api/positions/{position_id}/close", dependencies=[Depends(require_admin)])
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


@app.get("/api/behavior/flags", dependencies=[Depends(require_admin)])
async def behavior_flags(limit: int = 20) -> list[dict]:
    from app.services.position_service import recent_behavior_flags
    return recent_behavior_flags(limit=max(1, min(limit, 100)))


@app.get("/api/price")
async def current_price() -> dict:
    from app.utils.formatting import fmt_price
    tick = await state.provider.get_live_price()
    return {"symbol": tick.symbol, "bid": fmt_price(tick.bid), "ask": fmt_price(tick.ask),
            "mid": fmt_price(tick.mid), "spread": fmt_price(tick.spread),
            "provider": tick.provider, "quote_time": tick.quote_time.isoformat()}


def _ws_origin_ok(ws: WebSocket) -> bool:
    """WebSocket 同源檢查:有 Origin 時須與 Host 相符;無 Origin(非瀏覽器)放行。"""
    origin = ws.headers.get("origin")
    if not origin:
        return True
    host = (ws.headers.get("host") or "").split(",")[0].strip().lower()
    from urllib.parse import urlsplit
    try:
        return bool(host) and urlsplit(origin).netloc.lower() == host
    except ValueError:
        return False


def _ws_session_id(ws: WebSocket) -> str | None:
    return ws.cookies.get(get_settings().admin_session_cookie)


async def _ws_send(ws: WebSocket, payload: dict) -> None:
    import json
    await ws.send_text(json.dumps(payload, ensure_ascii=False, default=str))


@app.websocket("/ws")
async def ws_public(ws: WebSocket) -> None:
    """公開即時頻道(public alias):只傳公開投影(tick、candle_closed、公開 analysis)。"""
    if not _ws_origin_ok(ws):
        await ws.close(code=1008)   # 跨站 Origin 拒絕
        return
    await ws.accept()
    state.ws_public.add(ws)
    try:
        if state.latest_result:
            await _ws_send(ws, {"type": "analysis", "data": _serve_public(state.latest_result)})
        while True:
            await ws.receive_text()  # keepalive
    except WebSocketDisconnect:
        pass
    finally:
        state.ws_public.discard(ws)


@app.websocket("/ws/private")
async def ws_private(ws: WebSocket) -> None:
    """私人即時頻道:須有效 admin session cookie + 同源;傳完整私人 payload。

    使用安全 session cookie(非 header/URL/subprotocol token)。session 過期由 broadcast 檢查並關閉。
    """
    from app.security import session_valid
    if not _ws_origin_ok(ws):
        await ws.close(code=1008)   # 跨站 Origin 拒絕
        return
    sid = _ws_session_id(ws)
    if not session_valid(sid):
        await ws.close(code=1008)   # 無有效 session → 拒絕私人頻道
        return
    await ws.accept()
    state.ws_private[ws] = sid
    try:
        if state.latest_result:
            await _ws_send(ws, {"type": "analysis", "data": _serve_result(state.latest_result)})
        while True:
            await ws.receive_text()  # keepalive
            if not session_valid(_ws_session_id(ws)):   # 收到訊息時再驗一次
                await ws.close(code=1008)
                break
    except WebSocketDisconnect:
        pass
    finally:
        state.ws_private.pop(ws, None)
