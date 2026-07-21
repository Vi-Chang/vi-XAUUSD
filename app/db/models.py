"""資料庫模型:spec 第三節要求的 19 張表。

所有時間欄位一律儲存 UTC(timezone-aware);顯示時再轉 Asia/Taipei。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Account(Base):
    """帳戶層:區分策略來源(老師帶單 vs 自己交易),供分開統計與對照。"""
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    strategy_source: Mapped[str] = mapped_column(String(16))   # TEACHER / SELF / OTHER
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Instrument(Base):
    """1. instruments"""
    __tablename__ = "instruments"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True)          # XAUUSD
    provider_symbol: Mapped[str] = mapped_column(String(32))              # XAU_USD / XAU/USD / GC=F
    display_name: Mapped[str] = mapped_column(String(64), default="Gold Spot / USD")
    pip_value: Mapped[float] = mapped_column(Float, default=0.01)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Candle(Base):
    """2. candles — 每根 K 棒(spec 三之欄位要求)"""
    __tablename__ = "candles"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))                     # 15M/30M/1H/4H/1D/1W/5M
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)             # Tick Volume(強制標記)
    bid_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    data_provider: Mapped[str] = mapped_column(String(32))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "open_time", "data_provider", name="uq_candle"),
        Index("ix_candles_lookup", "symbol", "timeframe", "open_time"),
    )


class LivePrice(Base):
    """3. live_prices"""
    __tablename__ = "live_prices"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    bid: Mapped[float] = mapped_column(Float)
    ask: Mapped[float] = mapped_column(Float)
    mid: Mapped[float] = mapped_column(Float)
    spread: Mapped[float] = mapped_column(Float)
    provider: Mapped[str] = mapped_column(String(32))
    quote_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_live_prices_time", "symbol", "quote_time"),)


class IndicatorSnapshot(Base):
    """4. indicators — 每次分析當下各週期指標快照(JSON)"""
    __tablename__ = "indicators"
    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_run_id: Mapped[int | None] = mapped_column(ForeignKey("analysis_runs.id"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    candle_open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    values: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketStructure(Base):
    """5. market_structures — 結構事件(spec 六)"""
    __tablename__ = "market_structures"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    event_type: Mapped[str] = mapped_column(String(32))   # SWING_HIGH/BOS_UP/CHOCH_DOWN/FAILED_BREAKOUT...
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    price: Mapped[float] = mapped_column(Float)
    confirming_candles: Mapped[list] = mapped_column(JSON)   # 使用哪些已收線 K 棒確認(open_time list)
    invalidation_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    still_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_ms_lookup", "symbol", "timeframe", "event_time"),)


class KeyLevel(Base):
    """6. key_levels"""
    __tablename__ = "key_levels"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(48))          # PDH/PDL/PIVOT/ROUND/SWING/...
    price_low: Mapped[float] = mapped_column(Float)
    price_high: Mapped[float] = mapped_column(Float)
    strength: Mapped[str] = mapped_column(String(32))      # STRONG_SUPPORT/WEAK_RESISTANCE/...
    source: Mapped[str] = mapped_column(String(255))
    trading_day: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CandidateLevel(Base):
    """7. candidate_levels — 價位候選編號制(spec 八,防幻覺核心)"""
    __tablename__ = "candidate_levels"
    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_run_id: Mapped[int | None] = mapped_column(ForeignKey("analysis_runs.id"), nullable=True)
    level_id: Mapped[str] = mapped_column(String(48))      # e.g. SUP_ZONE_01
    kind: Mapped[str] = mapped_column(String(48))
    price_low: Mapped[float] = mapped_column(Float)
    price_high: Mapped[float] = mapped_column(Float)
    strength: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_cand_run", "analysis_run_id", "level_id"),)


class EconomicEvent(Base):
    """8. economic_events"""
    __tablename__ = "economic_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    country: Mapped[str] = mapped_column(String(8))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    impact: Mapped[str] = mapped_column(String(16))        # HIGH/MEDIUM/LOW
    previous: Mapped[str | None] = mapped_column(String(64), nullable=True)
    forecast: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revised: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32))        # finnhub/fmp/manual
    __table_args__ = (UniqueConstraint("name", "event_time", "source", name="uq_event"),)


class NewsItem(Base):
    """9. news_items(選配)"""
    __tablename__ = "news_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    headline: Mapped[str] = mapped_column(String(512))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(64))
    related_markets: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AnalysisRun(Base):
    """10. analysis_runs — 每次分析完整快照(spec 二十五)"""
    __tablename__ = "analysis_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trigger: Mapped[str] = mapped_column(String(48))       # m15_close/manual/state_change/...
    market_state: Mapped[str] = mapped_column(String(48))
    decision_action: Mapped[str] = mapped_column(String(32))
    confidence_grade: Mapped[str] = mapped_column(String(2))
    evidence_score: Mapped[int] = mapped_column(Integer, default=0)
    data_quality_status: Mapped[str] = mapped_column(String(16))
    result_json: Mapped[dict] = mapped_column(JSON)        # 固定輸出 JSON(spec 二十二)
    prompt_version: Mapped[str] = mapped_column(String(32))
    strategy_version: Mapped[str] = mapped_column(String(32))
    model_version: Mapped[str] = mapped_column(String(64))
    # 事後結果回填(排程於 15m/1h/4h/1d 後更新)
    outcome_15m: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_4h: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_1d: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_tags: Mapped[list | None] = mapped_column(JSON, nullable=True)


class TradeScenario(Base):
    """11. trade_scenarios — 多/空劇本(價位欄位一律為候選 ID)"""
    __tablename__ = "trade_scenarios"
    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"))
    direction: Mapped[str] = mapped_column(String(8))      # LONG/SHORT
    status: Mapped[str] = mapped_column(String(16))        # WATCH/PREPARE/TRIGGERED/INVALIDATED
    setup: Mapped[str] = mapped_column(Text)
    entry_zone_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    stop_loss_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    target_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    invalidation_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    required_confirmations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    risk_reward: Mapped[list | None] = mapped_column(JSON, nullable=True)
    expiration_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Position(Base):
    """12. positions(OANDA Practice 自動同步為主;手動輸入為輔)"""
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    lot_size: Mapped[float] = mapped_column(Float)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    planned_targets: Mapped[list | None] = mapped_column(JSON, nullable=True)
    partial_exit_history: Mapped[list | None] = mapped_column(JSON, nullable=True)
    stop_modification_history: Mapped[list | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="oanda")   # oanda/manual
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)


class MentorSignal(Base):
    """老師帶單(僅供參考)— 老師發出的方向與價位,我並未實際下單,不算持倉。

    完全獨立於 positions:純參考比對用,絕不進入「有無持倉」判斷、
    不影響任何進出場決策、不加減證據分數。
    """
    __tablename__ = "mentor_signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    direction: Mapped[str] = mapped_column(String(8))          # LONG / SHORT
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    targets: Mapped[list | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    signal_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # ── 歷史紀錄擴充(IMPORT-MENTOR-HISTORY:已平倉匯入單)──
    status: Mapped[str] = mapped_column(String(8), default="OPEN")   # OPEN | CLOSED
    open_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    lots: Mapped[float | None] = mapped_column(Float, nullable=True)
    pl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)    # 不含持倉費用
    swap_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_usd: Mapped[float | None] = mapped_column(Float, nullable=True)   # pl - swap
    points: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_source: Mapped[str | None] = mapped_column(String(12), nullable=True)  # ACTUAL/ESTIMATED/UNKNOWN
    import_batch: Mapped[str | None] = mapped_column(String(48), nullable=True)
    account_no: Mapped[str | None] = mapped_column(String(24), nullable=True)
    __table_args__ = (
        # 冪等匯入:重跑不得重複寫入
        Index("uq_mentor_import", "account_no", "close_time", "entry_price",
              "close_price", unique=True),
    )


class TradeJournal(Base):
    """13. trade_journal — 成交紀錄(Trading Coach 輸入)"""
    __tablename__ = "trade_journal"
    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    units: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    transaction_type: Mapped[str] = mapped_column(String(32))
    transaction_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="oanda")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)


class BehaviorFlag(Base):
    """14. behavior_flags — 交易教練行為標籤(spec 十九)"""
    __tablename__ = "behavior_flags"
    id: Mapped[int] = mapped_column(primary_key=True)
    flag: Mapped[str] = mapped_column(String(32))          # EARLY_EXIT/GREED_HOLD/...
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict] = mapped_column(JSON)           # 實際價格/時間/持倉紀錄
    corrective_action: Mapped[str] = mapped_column(Text)
    trade_journal_id: Mapped[int | None] = mapped_column(ForeignKey("trade_journal.id"), nullable=True)


class Alert(Base):
    """15. alerts — 已發送通知(去重/冷卻依據)"""
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(16))         # INFO/WATCH/TRIGGER/RISK/MANAGE/EXIT
    topic: Mapped[str] = mapped_column(String(128))        # 去重 key
    message: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(16), default="telegram")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (Index("ix_alerts_topic", "topic", "sent_at"),)


class ProviderHealth(Base):
    """16. provider_health — 各資料源健康與配額"""
    __tablename__ = "provider_health"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))        # OK/DEGRADED/DOWN
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    quota_used_today: Mapped[int] = mapped_column(Integer, default=0)
    quota_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("provider", name="uq_provider"),)


class SystemSetting(Base):
    """17. system_settings — 可於設定頁修改的參數覆寫"""
    __tablename__ = "system_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    value: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketCalendarEntry(Base):
    """18. market_calendar — 假日/提前收市(spec 四)"""
    __tablename__ = "market_calendar"
    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_date: Mapped[date] = mapped_column(Date, unique=True)   # NY 日曆日
    kind: Mapped[str] = mapped_column(String(16))                    # HOLIDAY/EARLY_CLOSE
    description: Mapped[str] = mapped_column(String(128))


class LlmUsage(Base):
    """19. llm_usage — 每日 Token 與費用統計(Phase 7 使用;MVP 建表)"""
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    usage_day: Mapped[date] = mapped_column(Date)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("usage_day", "provider", "model", name="uq_llm_day"),)
