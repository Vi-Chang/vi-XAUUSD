"""交易資格閘門(單一權威):資料過期/異常、來源異常、休市或證據不足時,
系統一律停止輸出可執行的做多/做空訊號,改為 NO_TRADE(資料不足,暫不交易)。

設計原則:
- **唯一判斷點**:providers、排程器、手動分析與前端一律以本函式的結果為準,
  不得各自複製一套資料品質/新鮮度判斷(避免多套判斷互相矛盾)。
- **在 AI 呼叫前執行**:analysis_service 先過閘門,不合格即不呼叫付費 AI(省成本)。
- **不硬編門檻**:所有門檻經 config.Settings 取得(可寫入設定頁/回測調參)。
- **不洩漏內部錯誤/secret/私人資料**:reason 一律為使用者友善繁中,不含例外堆疊或憑證。

回傳結構化 TradeEligibility(eligible/code/reason/data_age_seconds/source_status/
market_status/spread_status/evidence_status),供 API、排程器與前端一致使用。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from app.config import get_settings
from app.engines.data_quality import DataQualityReport
from app.providers.base import PriceTick
from app.schemas.analysis import TradeEligibility

# 允許的時鐘偏移(報價時間些微超前 now 視為正常;超過此秒數視為異常未來時間)
_MAX_FUTURE_SKEW_SECONDS = 5.0


def _finite_number(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def evaluate_trade_eligibility(
    *,
    tick: PriceTick | None,
    quality: DataQualityReport,
    market_state: str = "",
    atr15: float | None = None,
    evidence_score: int | None = None,
    now: datetime | None = None,
    is_fallback: bool = False,
    historical_mode: bool = False,
) -> TradeEligibility:
    """評估此刻是否可輸出可執行交易訊號。不合格 → eligible=False + 阻擋碼/原因。

    參數:
      tick:最新報價(None = 無報價)。
      quality:資料品質報告(提供 status 與 market_open,已含市場行事曆防呆)。
      market_state:市場狀態(INSUFFICIENT_DATA 代表 K 棒不足/指標無法計算)。
      atr15:15M ATR(供點差相對門檻;None 時僅用絕對門檻)。
      evidence_score:規則引擎證據分數(低於門檻 → 證據不足)。
      is_fallback:是否使用 fallback/快取報價(降級模式,套用較寬但仍有界的時齡上限)。
      historical_mode:是否為「明確標示的歷史分析模式」(休市時仍允許;預設 False)。
    """
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    market_status = "OPEN" if quality.market_open else "CLOSED"

    def block(code: str, reason: str, *, data_age: float | None,
              source: str, spread: str, evidence: str) -> TradeEligibility:
        return TradeEligibility(
            eligible=False, code=code, reason=reason, data_age_seconds=data_age,
            source_status=source, market_status=market_status,
            spread_status=spread, evidence_status=evidence)

    # 1) 沒有可用報價
    if tick is None:
        return block("NO_QUOTE", "沒有可用的即時報價,無法判斷行情,暫不交易。",
                     data_age=None, source="NO_QUOTE", spread="UNKNOWN", evidence="UNKNOWN")

    # 2) 報價時間無效 / 無法解析 / 異常未來時間
    qt = getattr(tick, "quote_time", None)
    if not isinstance(qt, datetime):
        return block("INVALID_QUOTE_TIME", "報價時間無效或無法解析,無法確認資料新鮮度,暫不交易。",
                     data_age=None, source="INVALID", spread="UNKNOWN", evidence="UNKNOWN")
    qt_utc = qt if qt.tzinfo is not None else qt.replace(tzinfo=timezone.utc)
    try:
        data_age = (now - qt_utc).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return block("INVALID_QUOTE_TIME", "報價時間無效或無法解析,無法確認資料新鮮度,暫不交易。",
                     data_age=None, source="INVALID", spread="UNKNOWN", evidence="UNKNOWN")
    if not math.isfinite(data_age) or data_age < -_MAX_FUTURE_SKEW_SECONDS:
        return block("INVALID_QUOTE_TIME", "報價時間異常(未來時間或無法計算),暫不交易。",
                     data_age=None, source="INVALID", spread="UNKNOWN", evidence="UNKNOWN")
    data_age = round(data_age, 1)

    # 3) 報價數值無效:非有限數值 / 非正數 / 賣價低於買價
    bid, ask = tick.bid, tick.ask
    if not (_finite_number(bid) and _finite_number(ask)) or bid <= 0 or ask <= 0:
        return block("INVALID_QUOTE", "報價數值無效(非有限數值或非正數),暫不交易。",
                     data_age=data_age, source="INVALID", spread="INVALID", evidence="UNKNOWN")
    if ask < bid:
        return block("INVALID_QUOTE", "報價異常:賣價低於買價(ask < bid),不採用,暫不交易。",
                     data_age=data_age, source="INVALID", spread="INVALID", evidence="UNKNOWN")

    spread = ask - bid
    spread_cap = max(s.gate_spread_max_abs, s.gate_spread_max_atr15_mult * (atr15 or 0.0))
    spread_too_wide = spread > spread_cap
    spread_status = "TOO_WIDE" if spread_too_wide else "OK"

    # 來源/資料品質狀態(供結構化輸出)
    if quality.status == "FAILED":
        source_status = "FAILED"
    elif quality.status == "STALE":
        source_status = "STALE"
    elif quality.status == "DEGRADED" or quality.source_mismatch:
        source_status = "DEGRADED"
    else:
        source_status = "OK"

    # ── 阻擋順序:資料有效性 → 市場 → 新鮮度 → 點差 → 品質 → 歷史 → 證據 ──
    # 4) 來源整體失敗
    if source_status == "FAILED":
        return block("DATA_FAILED", "資料來源異常(無有效行情),暫不交易。",
                     data_age=data_age, source=source_status, spread=spread_status, evidence="UNKNOWN")
    # 5) 休市且非「明確歷史分析模式」
    if not quality.market_open and not historical_mode:
        return block("MARKET_CLOSED", "現在休市,先不動作,暫不交易。",
                     data_age=data_age, source=source_status, spread=spread_status, evidence="UNKNOWN")
    # 6) 即時市場期間資料過期(STALE;data_quality 已依 provider 輪詢頻率放寬且含行事曆防呆)
    if source_status == "STALE":
        return block("STALE_QUOTE", "行情資料已過期,不照過期價格做決定,暫不交易。",
                     data_age=data_age, source=source_status, spread=spread_status, evidence="UNKNOWN")
    # 7) 使用 fallback/快取報價且已超過允許時齡上限
    if is_fallback and data_age > s.gate_fallback_quote_max_age_seconds:
        return block("FALLBACK_CACHE_STALE",
                     "目前使用備援/快取資料且已超過允許時效,暫不交易。",
                     data_age=data_age, source=source_status, spread=spread_status, evidence="UNKNOWN")
    # 8) 點差異常過大
    if spread_too_wide:
        return block("SPREAD_TOO_WIDE",
                     "點差異常過大(市場流動性不足或報價跳動),暫不交易。",
                     data_age=data_age, source=source_status, spread=spread_status, evidence="UNKNOWN")
    # 9) 來源降級 / 主備價差過大 / K 線缺漏或斷裂
    if source_status == "DEGRADED":
        return block("DATA_DEGRADED", "資料品質不足(來源降級、主備價差或 K 線缺漏),暫不交易。",
                     data_age=data_age, source=source_status, spread=spread_status, evidence="UNKNOWN")
    # 10) K 棒不足 / 時間序列斷裂 / 關鍵指標無法計算
    if market_state == "INSUFFICIENT_DATA":
        return block("INSUFFICIENT_HISTORY", "K 棒不足或關鍵指標無法計算,暫不交易。",
                     data_age=data_age, source=source_status, spread=spread_status, evidence="UNKNOWN")
    # 11) 分析證據低於最低門檻
    evidence_status = "OK"
    if evidence_score is not None:
        if evidence_score < s.gate_min_evidence_score:
            return block("NO_EVIDENCE", "目前分析證據不足以支持可執行訊號,暫不交易。",
                         data_age=data_age, source=source_status, spread=spread_status,
                         evidence="INSUFFICIENT")
    else:
        evidence_status = "UNKNOWN"

    return TradeEligibility(
        eligible=True, code="OK",
        reason="資料品質與市場條件符合,允許輸出可執行判斷。",
        data_age_seconds=data_age, source_status=source_status,
        market_status=market_status, spread_status=spread_status,
        evidence_status=evidence_status)
