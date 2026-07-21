"""固定輸出 JSON Schema(spec 二十二)。

- 劇本價位欄位(entry_zone_id / stop_loss_id / target_ids / invalidation_id)
  一律為候選價位 ID(spec 八),由後端反查填入實際數字後才呈現。
- 缺少數值使用 null,不得編造。
- MVP 階段由規則引擎填寫;Phase 7 起 AI 輸出必須通過本 Schema 驗證,
  引用不存在的 ID 時後端拒絕並重新請求(連續失敗 → NO_TRADE_AI_INVALID)。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CurrentPrice(BaseModel):
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    spread: float | None = None
    provider: str = ""
    last_update: str = ""


class DataQuality(BaseModel):
    status: Literal["GOOD", "DEGRADED", "STALE", "FAILED"] = "FAILED"
    missing_candles: list[str] = Field(default_factory=list)
    source_mismatch: bool = False
    warnings: list[str] = Field(default_factory=list)


class EventRisk(BaseModel):
    # P2:固有影響力與時間風險為兩個獨立維度
    event_impact: Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"] = "UNKNOWN"  # 靜態屬性
    time_risk: Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"] = "UNKNOWN"     # 由倒數推導
    level: Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"] = "UNKNOWN"         # 相容=time_risk
    event_lockout: bool = False
    next_event: str = ""
    minutes_remaining: int | None = None
    source: Literal["finnhub", "fmp", "manual", "none"] = "none"
    reason: str = ""


class CrossMarketContext(BaseModel):
    dxy: str = ""
    us2y: str = ""
    us10y: str = ""
    real_yield: str = ""
    vix: str = ""
    sp500: str = ""
    oil: str = ""
    silver: str = ""
    data_freshness: str = ""
    interpretation: str = ""


class TimeframeView(BaseModel):
    structure: str = ""
    momentum: str = ""
    closed_candle_only: bool = True
    interpretation: str = ""


class Timeframes(BaseModel):
    weekly: TimeframeView = TimeframeView()
    daily: TimeframeView = TimeframeView()
    h4: TimeframeView = TimeframeView()
    h1: TimeframeView = TimeframeView()
    m15: TimeframeView = TimeframeView()


class KeyLevels(BaseModel):
    strong_resistance_zones: list[dict] = Field(default_factory=list)
    weak_resistance_zones: list[dict] = Field(default_factory=list)
    strong_support_zones: list[dict] = Field(default_factory=list)
    weak_support_zones: list[dict] = Field(default_factory=list)
    liquidity_zones: list[dict] = Field(default_factory=list)
    range_midpoint: list[dict] = Field(default_factory=list)
    invalidation_levels: list[dict] = Field(default_factory=list)


ScenarioStatus = Literal["WATCH", "PREPARE", "TRIGGERED", "INVALIDATED", "INVALID"]


class Scenario(BaseModel):
    """交易劇本(setup)。

    frozen=True(BUGFIX R1/TC-08):禁止任何程式路徑單獨更新個別欄位;
    要變更只能用 model_copy(update=...) 產生新物件整組替換。
    """
    model_config = {"frozen": True}

    status: ScenarioStatus = "WATCH"
    setup: str = ""
    entry_zone_id: str | None = None
    required_confirmations: list[str] = Field(default_factory=list)
    stop_loss_id: str | None = None
    target_ids: list[str] = Field(default_factory=list)
    risk_reward: list[float] = Field(default_factory=list)
    invalidation_id: str | None = None
    expiration_time: str | None = None
    # 後端反查候選 ID 填入的實際數字(呈現用;AI 不得填寫此欄)
    resolved_prices: dict = Field(default_factory=dict)
    # ── BUGFIX R1/R3/R4:原子快照與可追溯性 ──
    created_at: str = ""            # setup 生成時間(UI 顯示「X 分鐘前」)
    snapshot_ts: str = ""           # 本次計算使用的價格快照時間戳
    structure_event_id: str | None = None   # 觸發本 setup 的結構事件(BOS/CHoCH)
    invalid_reasons: list[str] = Field(default_factory=list)  # INVALID 時的違規清單
    invalid_fatal: bool = False   # P1:FATAL(程式錯誤級)vs REJECT(條件不足)


class RiskManagerView(BaseModel):
    approved: bool = False
    position_risk_percent: float = 0.0
    estimated_position_size: float = 0.0
    daily_loss_limit_reached: bool = False
    consecutive_losses: int = 0
    veto_reasons: list[str] = Field(default_factory=list)


class PositionManagement(BaseModel):
    has_position: bool = False
    position_side: str = ""
    entry_price: float | None = None
    current_r_multiple: float | None = None
    recommended_action: str = ""
    partial_exit_plan: str = ""
    trailing_stop_plan: str = ""
    full_exit_condition: str = ""
    prohibited_actions: list[str] = Field(default_factory=list)


class TradingCoachView(BaseModel):
    behavior_flags: list[str] = Field(default_factory=list)
    early_exit_risk: str = ""
    greed_risk: str = ""
    chasing_risk: str = ""
    revenge_trade_risk: str = ""
    stop_loss_discipline: str = ""
    message: str = ""


DecisionAction = Literal[
    "NO_TRADE", "WATCH", "PREPARE_LONG", "PREPARE_SHORT", "LONG", "SHORT", "MANAGE", "EXIT"
]


class Decision(BaseModel):
    action: DecisionAction = "NO_TRADE"
    confidence_grade: Literal["S", "A", "B", "C", "X"] = "X"
    evidence_score: int = 0
    reason: str = ""
    next_bullish_trigger: str = ""
    next_bearish_trigger: str = ""
    next_recheck_time: str = ""


class BiasAnalysis(BaseModel):
    """多空證據傾向(v2.1 擴充)。

    由規則引擎「已成立條件」確定性加權計算(STRUCT ×2、其餘 ×1)。
    這是證據完整度的相對傾向,不是勝率、不是漲跌機率(spec 二十一)。
    """
    bull_pct: int = 50
    bear_pct: int = 50
    bull_evidence: list[str] = Field(default_factory=list)
    bear_evidence: list[str] = Field(default_factory=list)
    chase_flags: list[str] = Field(default_factory=list)
    disclaimer: str = "證據傾向 ≠ 勝率;僅代表當下多空條件的相對完整度(規格書二十一)"


class MentorSignalView(BaseModel):
    """老師帶單一筆 + 與系統方向的比對(純參考,不影響決策)。"""
    id: int
    direction: str
    entry_price: float
    stop_loss: float | None = None
    targets: list[float] = Field(default_factory=list)
    note: str = ""
    signal_time: str = ""
    system_direction: str | None = None
    alignment: str = "SYSTEM_NEUTRAL"           # ALIGNED / OPPOSITE / SYSTEM_NEUTRAL
    alignment_text: str = ""
    entry_vs_current: float | None = None
    entry_vs_current_text: str = ""


class MentorComparison(BaseModel):
    has_signals: bool = False
    signals: list[MentorSignalView] = Field(default_factory=list)
    note: str = "老師帶單僅供參考比對,不影響系統任何進出場判斷與證據分數"


class OffsetInfo(BaseModel):
    """價格校正資訊(讀取時由 price_offset 服務依當前資料源填入)。"""
    mode: str = "manual"                 # manual | auto
    value: float | None = None           # broker − active_source;未校準時為 None
    analysis_source: str = ""            # 動態:當前資料源(不寫死)
    trading_broker: str = "TMGM"
    calibrated: bool = False             # P0 fail-safe:未校準 → NO-SIGNAL
    calibration_warning: str = ""
    updated_at: str | None = None
    applied_to: list[str] = Field(default_factory=lambda: ["entry", "stop_loss", "targets"])
    auto_available: bool = False
    formula: str = "TMGM = TwelveData + Offset"
    note: str = ""


class Meta(BaseModel):
    prompt_version: str = ""
    strategy_version: str = ""
    model_version: str = ""
    llm_cost_usd_today: float = 0.0


class AnalysisResult(BaseModel):
    """spec 二十二之完整固定輸出。"""
    version: int = 0                # BUGFIX R6:遞增版本號(=analysis_runs.id)
    snapshot_ts: str = ""           # 本次分析使用的價格數據時間戳
    timestamp_utc: str = ""
    timestamp_taipei: str = ""
    symbol: str = "XAUUSD"
    current_price: CurrentPrice = CurrentPrice()
    data_quality: DataQuality = DataQuality()
    event_risk: EventRisk = EventRisk()
    cross_market_context: CrossMarketContext = CrossMarketContext()
    market_state: str = "INSUFFICIENT_DATA"
    timeframes: Timeframes = Timeframes()
    key_levels: KeyLevels = KeyLevels()
    long_scenario: Scenario = Scenario()
    short_scenario: Scenario = Scenario()
    bias_analysis: BiasAnalysis = BiasAnalysis()
    risk_manager: RiskManagerView = RiskManagerView()
    position_management: PositionManagement = PositionManagement()
    mentor_comparison: MentorComparison = MentorComparison()
    trading_coach: TradingCoachView = TradingCoachView()
    decision: Decision = Decision()
    offset_info: OffsetInfo = OffsetInfo()
    meta: Meta = Meta()
    summary_zh_tw: str = ""
    most_likely_user_mistake_now: str = ""


def validate_candidate_refs(result: AnalysisResult, known_ids: set[str]) -> list[str]:
    """檢查劇本引用的候選價位 ID 是否全部存在(spec 八之4)。

    回傳未知 ID 清單;非空即應拒絕該回覆(AI 層)或視為程式錯誤(規則引擎層)。
    """
    unknown: list[str] = []
    for scenario in (result.long_scenario, result.short_scenario):
        refs = [scenario.entry_zone_id, scenario.stop_loss_id, scenario.invalidation_id,
                *scenario.target_ids]
        unknown.extend(r for r in refs if r is not None and r != "" and r not in known_ids)
    return unknown
