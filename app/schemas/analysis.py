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

from app.schemas.ai import AiStrategy

DataQualityStatus = Literal["GOOD", "DEGRADED", "STALE", "FAILED"]
EventImpact = Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"]
EventSource = Literal["official", "finnhub", "fmp", "manual", "none"]
ConfidenceGrade = Literal["S", "A", "B", "C", "X"]


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
    source: EventSource = "none"
    reason: str = ""
    data_updated_at: str = ""
    event_phase: Literal["upcoming", "post_release", "unknown"] = "unknown"
    post_event_wait: bool = False
    reaction_status: Literal["not_applicable", "awaiting_close", "confirmed", "mixed"] = "not_applicable"
    xauusd_confirmation: str = "not_checked"
    dxy_confirmation: str = "not_available"
    yield_confirmation: str = "not_available"
    actual: float | None = None
    forecast: float | None = None
    previous: float | None = None
    reaction_message: str = ""
    outcome_status: Literal["not_available", "pending", "available"] = "not_available"
    outcome_source: str = ""
    surprise: float | None = None
    fundamental_bias: Literal["bullish_xauusd", "bearish_xauusd", "neutral", "unknown"] = "unknown"


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
    lifecycle_status: Literal[
        "NO_SETUP", "BREAKOUT_PENDING", "WAITING_FOR_ENTRY", "READY",
        "MISSED_ENTRY_WAIT_RETEST", "FAILED_BREAKOUT", "EXPIRED", "INVALID",
        "POSITION_MANAGEMENT", "WATCH",
    ] = "NO_SETUP"
    planned_entry: float | None = None
    stop_loss_price: float | None = None
    rr_calculation_basis: str = ""
    rr_details: list[dict] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    raw_price_debug: dict = Field(default_factory=dict)
    setup_id: str = ""
    breakout_at: str = ""
    closed_bars_since_breakout: int = 0


class DecisionTrace(BaseModel):
    analysisId: int = 0
    setupId: str = ""
    finalDecision: str = "WATCH"
    lifecycleStatus: str = "NO_SETUP"
    direction: Literal["LONG", "SHORT", "NONE"] = "NONE"
    triggerLevel: float | None = None
    breakoutAt: str = ""
    closedBarsSinceBreakout: int = 0
    confirmationPassed: bool = False
    entryZoneValid: bool = False
    priceInsideEntryZone: bool = False
    riskRewardPassed: bool = False
    structurePassed: bool = False
    dataQualityPassed: bool = False
    positionStatus: Literal["FLAT", "OPEN", "UNKNOWN"] = "UNKNOWN"
    blockingReasons: list[str] = Field(default_factory=list)
    evaluatedAt: str = ""
    marketSnapshotAt: str = ""


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
    current_price: float | None = None
    unrealized_pnl: float | None = None
    structural_risk: str = ""
    account_risk: str = ""
    risk_release_condition: str = ""
    data_timestamp: str = ""


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


class AnalysisEvidence(BaseModel):
    id: str
    direction: Literal["bullish", "bearish"]
    category: str
    label: str
    sourceEvent: str | None = None
    level: float | None = None
    candleTime: str | None = None
    reason: str = ""
    data_updated_at: str = ""


class TimeframeAssessment(BaseModel):
    timeframe: Literal["1D", "4H", "1H", "15M"]
    trend: Literal["bullish", "bearish", "neutral"] = "neutral"
    momentum: Literal["accelerating", "stable", "weakening", "pullback", "reversal_risk"] = "stable"
    label: str = ""
    familyScores: dict[str, float] = Field(default_factory=dict)
    closedCandleTime: str = ""


class DynamicConfirmationLevel(BaseModel):
    kind: Literal["support", "resistance"]
    price: float
    buffer: float
    timeframe: Literal["1D", "4H", "1H", "15M"]
    source: str


class AssessmentReason(BaseModel):
    code: str
    priority: int
    message: str
    evidenceFamilies: list[str] = Field(default_factory=list)


class InvalidationCondition(BaseModel):
    code: str
    message: str
    timeframe: str = ""
    price: float | None = None
    source: str = ""


class MarketAssessment(BaseModel):
    regime: Literal[
        "strong_bullish", "bullish", "range", "bearish", "strong_bearish"
    ] = "range"
    shortTermWeakness: Literal[
        "none", "early_warning", "confirmed", "accelerating"
    ] = "none"
    twoSidedRisk: Literal[
        "normal", "downside_continuation", "oversold_rebound", "high_whipsaw"
    ] = "normal"
    reversalState: Literal[
        "none", "oversold_without_reversal", "selling_exhaustion_candidate",
        "reclaim_attempt", "reversal_confirmed", "reversal_failed"
    ] = "none"


class NewEntryDecision(BaseModel):
    readiness: Literal["ready", "wait_confirmation", "avoid_chasing", "no_trade"] = "no_trade"
    longAllowed: bool = False
    shortAllowed: bool = False
    longReason: str = ""
    shortReason: str = ""


class ExistingPositionAssessment(BaseModel):
    direction: Literal["long", "short", "unknown"] = "unknown"
    positionTimeframe: Literal["15M", "1H", "4H", "1D", "unknown"] = "unknown"
    riskLevel: Literal["normal", "elevated", "high", "critical"] = "normal"
    action: Literal[
        "insufficient_context", "follow_original_plan", "monitor_reclaim",
        "reduce_risk_if_needed", "exit_on_confirmed_invalidation", "exit_confirmed"
    ] = "insufficient_context"
    thesisStatus: Literal[
        "intact", "under_pressure", "invalidation_testing", "invalidated", "unknown"
    ] = "unknown"
    warnings: list[str] = Field(default_factory=list)
    invalidationEvidence: list[AnalysisEvidence] = Field(default_factory=list)
    recoveryEvidence: list[AnalysisEvidence] = Field(default_factory=list)
    contextComplete: bool = False
    message: str = ""


class TradingDecision(BaseModel):
    marketAssessment: MarketAssessment = Field(default_factory=MarketAssessment)
    newEntryDecision: NewEntryDecision = Field(default_factory=NewEntryDecision)
    existingPositionAssessment: ExistingPositionAssessment = Field(
        default_factory=ExistingPositionAssessment)


class NormalizedAnalysisState(BaseModel):
    """畫面判斷的唯一來源；所有欄位屬同一報價與同一根已收盤 K 棒。"""
    generatedAt: str = ""
    marketDataTimestamp: str = ""
    currentPrice: float | None = None
    trendBias: Literal["bullish", "bearish", "neutral"] = "neutral"
    tacticalBias: Literal["bullish", "bearish", "neutral"] = "neutral"
    setupState: Literal[
        "OBSERVE", "LONG_WATCH", "SHORT_WATCH", "LONG_READY", "SHORT_READY", "NO_CHASE"
    ] = "OBSERVE"
    triggerLevel: float | None = None
    invalidationLevel: float | None = None
    expiresAt: str = ""
    missingCondition: str = ""
    nextCheckTime: str = ""
    bullishTriggerLevel: float | None = None
    bearishTriggerLevel: float | None = None
    falseBreakProtectionLevel: float | None = None
    falseBreakProtectionExpiresAt: str = ""
    breakoutState: Literal["confirmed", "testing", "failed", "none"] = "none"
    entryTiming: Literal["favorable", "chase", "wait", "invalid"] = "wait"
    longEvidence: list[AnalysisEvidence] = Field(default_factory=list)
    shortEvidence: list[AnalysisEvidence] = Field(default_factory=list)
    invalidatedEvidence: list[AnalysisEvidence] = Field(default_factory=list)
    eventDataStatus: Literal["GOOD", "STALE", "FAILED"] = "FAILED"
    marketDataStatus: Literal["GOOD", "STALE", "FAILED"] = "FAILED"
    bullPct: int = 50
    bearPct: int = 50
    evidenceTotal: int = 0
    riskDirection: Literal["long", "short", "both", "wait", "none"] = "none"
    riskLabel: str = "等待確認"
    riskMessage: str = ""
    marketStateCode: str = "INSUFFICIENT_DATA"
    marketStateLabel: str = "資料不足"
    tradingScript: str = ""
    mostLikelyMistake: str = ""
    consistencyValid: bool = True
    consistencyErrors: list[str] = Field(default_factory=list)
    consistencyMessage: str = ""
    sourceTimestamps: dict[str, str] = Field(default_factory=dict)
    sourcePrices: dict[str, float] = Field(default_factory=dict)
    marketRegime: Literal[
        "strong_bullish", "bullish", "range", "bearish", "strong_bearish"
    ] = "range"
    shortTermMomentum: Literal[
        "accelerating", "stable", "weakening", "pullback", "reversal_risk"
    ] = "stable"
    entryReadiness: Literal["ready", "wait_confirmation", "avoid_chasing", "no_trade"] = "no_trade"
    dataConfidence: Literal["high", "medium", "low", "insufficient"] = "insufficient"
    supportState: Literal[
        "none", "testing_support", "intrabar_breach", "confirmed_breakdown",
        "failed_breakdown", "retest_rejected"
    ] = "none"
    trendScore: int = 50
    entryQualityScore: int = 0
    technicalBiasLabel: str = "中性"
    timeframeAssessments: list[TimeframeAssessment] = Field(default_factory=list)
    confirmationLevels: list[DynamicConfirmationLevel] = Field(default_factory=list)
    lastClosedCandleTimestamp: str = ""
    eventDataTimestamp: str = ""
    freshnessBySource: dict[str, str] = Field(default_factory=dict)
    eventRisk: Literal["low", "medium", "high", "unknown"] = "unknown"
    shortTermWeakness: Literal["none", "early_warning", "confirmed", "accelerating"] = "none"
    positionRisk: Literal["normal", "elevated", "high", "critical"] = "normal"
    riskOverride: Literal[
        "none", "block_new_long", "block_new_short", "protect_existing_long",
        "protect_existing_short", "suspend_all_entries"
    ] = "suspend_all_entries"
    longEntryAllowed: bool = False
    shortEntryAllowed: bool = False
    reasons: list[AssessmentReason] = Field(default_factory=list)
    invalidationConditions: list[InvalidationCondition] = Field(default_factory=list)
    existingLongGuidance: str = ""
    existingShortGuidance: str = ""
    structuralInvalidationNote: str = ""
    tradingDecision: TradingDecision = Field(default_factory=TradingDecision)


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


class TradeEligibility(BaseModel):
    """交易資格閘門結果(app/engines/trade_gate)。

    單一權威:資料過期/異常/休市/證據不足時 eligible=False,系統一律 NO_TRADE。
    僅含資料品質/市場/報價層資訊,不含任何個人/持倉資料(可公開安全,但預設不進公開
    allowlist 以維持既有公開 schema 相容;公開端以 decision.action=NO_TRADE + reason 表達)。
    """
    eligible: bool = True
    code: str = "OK"                # OK / NO_QUOTE / INVALID_QUOTE_TIME / INVALID_QUOTE /
                                    # DATA_FAILED / MARKET_CLOSED / STALE_QUOTE /
                                    # FALLBACK_CACHE_STALE / SPREAD_TOO_WIDE / DATA_DEGRADED /
                                    # INSUFFICIENT_HISTORY / NO_EVIDENCE
    reason: str = ""
    data_age_seconds: float | None = None
    source_status: str = "OK"       # OK / DEGRADED / STALE / FAILED / INVALID / NO_QUOTE
    market_status: str = "OPEN"     # OPEN / CLOSED
    spread_status: str = "OK"       # OK / TOO_WIDE / INVALID / UNKNOWN
    evidence_status: str = "OK"     # OK / INSUFFICIENT / UNKNOWN


class Meta(BaseModel):
    prompt_version: str = ""
    strategy_version: str = ""
    model_version: str = ""
    llm_cost_usd_today: float = 0.0


class TacticalShadowRecord(BaseModel):
    """Paper-only tactical signal used for outcome calibration."""
    enabled: bool = True
    liveAdviceEnabled: bool = False
    setupState: Literal[
        "OBSERVE", "LONG_WATCH", "SHORT_WATCH", "LONG_READY", "SHORT_READY", "NO_CHASE"
    ] = "OBSERVE"
    direction: Literal["LONG", "SHORT", "NONE"] = "NONE"
    referencePrice: float | None = None
    triggerLevel: float | None = None
    invalidationLevel: float | None = None
    expiresAt: str = ""
    createdAt: str = ""
    eligibleForOutcome: bool = False
    parameters: dict[str, float | int] = Field(default_factory=dict)


class AnalysisResult(BaseModel):
    """spec 二十二之完整固定輸出。"""
    version: int = 0                # BUGFIX R6:遞增版本號(=analysis_runs.id)
    # 隱私邊界戳記:標記本結果由 position-free / public-safe pipeline 產生(0 = 舊資料,不可公開自由文字)
    calibration_status: Literal["collecting", "sufficient"] = "collecting"
    calibration_sample_size: int = 0
    calibration_min_sample_size: int = 30
    calibration_message: str = ""
    privacy_boundary_version: int = 0
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
    normalized_analysis: NormalizedAnalysisState = NormalizedAnalysisState()
    tactical_shadow: TacticalShadowRecord = Field(default_factory=TacticalShadowRecord)
    decision_trace: DecisionTrace = Field(default_factory=DecisionTrace)
    risk_manager: RiskManagerView = RiskManagerView()
    position_management: PositionManagement = PositionManagement()
    mentor_comparison: MentorComparison = MentorComparison()
    trading_coach: TradingCoachView = TradingCoachView()
    decision: Decision = Decision()
    # 市場層決策(未被「持倉管理 MANAGE 覆寫」污染);公開投影以此為 decision,避免洩露持倉。
    market_decision: Decision = Decision()
    # 交易資格閘門結果(資料/市場/報價層;預設 eligible=True 以相容舊資料)。
    trade_eligibility: TradeEligibility = TradeEligibility()
    ai_strategy: AiStrategy = Field(default_factory=AiStrategy)   # V2 AI 分析層
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
