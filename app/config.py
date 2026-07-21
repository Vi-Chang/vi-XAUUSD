"""集中設定:所有規則參數、API Key、旗標都在這裡(spec 二十七之 16)。

任何門檻(SOURCE_MISMATCH、追價 ATR 倍數、事件鎖定時間…)禁止散落在程式碼中,
一律經由 Settings 取得,才能寫入設定頁與回測調參。
"""
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("mt5_login", "mt5_server_utc_offset_hours", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """.env 中留空的選填數值欄位(如 MT5_LOGIN=)視為未設定。"""
        return None if v == "" else v

    # ── 執行模式 ──
    mock_data_mode: bool = True
    disable_scheduler: bool = False
    timezone_display: str = "Asia/Taipei"

    # ── 儲存 ──
    database_url: str = "sqlite:///./xauusd.db"
    redis_url: str = ""  # 空字串 = 用行程內記憶體快取

    # ── 主要行情 Provider 選擇 ──
    # auto:mock 模式 → mock;否則 mt5(有設定時)→ oanda
    primary_provider: str = "auto"  # auto | mt5 | oanda | mock

    # ── MT5 / TMGM(經由本機已登入的 MetaTrader 5 終端機)──
    mt5_symbol: str = "XAUUSD"          # TMGM 部分帳戶類型後綴不同(如 XAUUSD.pro),依終端機為準
    mt5_terminal_path: str = ""         # 留空 = 自動尋找已安裝的 MT5
    mt5_login: int | None = None        # 留空 = 直接附掛已登入的終端機(建議,免存密碼)
    mt5_password: str = ""
    mt5_server: str = ""                # 例:TMGM-Demo / TMGM-Live
    # 伺服器時區與 UTC 的時差(小時)。留空 = 開盤時自動偵測;
    # 多數 NY-close 對齊券商(含 TMGM)為冬令 +2 / 夏令 +3。
    mt5_server_utc_offset_hours: int | None = None

    # ── Capital.com(備選 Provider;Demo 帳戶免費 REST API)──
    capital_api_key: str = ""
    capital_identifier: str = ""        # 登入 Email
    capital_api_password: str = ""      # 建立 API Key 時設定的專用密碼
    capital_demo: bool = True           # true = demo 環境(demo-api-capital)
    capital_epic: str = "GOLD"          # Capital.com 的黃金現貨代碼

    # ── OANDA Practice(備選 Provider)──
    oanda_env: str = "practice"  # practice | live(live 僅供未來,MVP 不使用)
    oanda_api_token: str = ""
    oanda_account_id: str = ""

    # ── Twelve Data 免費層 ──
    twelve_data_api_key: str = ""
    twelve_data_daily_limit: int = 800
    twelve_data_minute_limit: int = 8

    # ── 事件 / 跨市場(Phase 6)──
    finnhub_api_key: str = ""
    fmp_api_key: str = ""
    fred_api_key: str = ""
    manual_events_path: str = "data/manual_events.json"
    manual_events_stale_days: int = 7

    # ── 通知 ──
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notify_cooldown_seconds: int = 900
    heartbeat_minutes: int = 30
    # 分級通知門檻:低於此嚴重度只寫 log,達到才推 Telegram(DEBUG<INFO<WARN<ERROR)
    # 預設 WARN:一切正常時手機不響,只有資料延遲/異常才推播(靜默 heartbeat)
    notify_level: str = "WARN"
    telegram_mention: str = ""              # ERROR 時前綴(如 @yourname);私聊可留空
    data_lag_warn_minutes: int = 60         # 最新 K 棒落後現在超過此分鐘數 → WARN
    daily_summary_hour_utc: int = 22        # 每日摘要最早發送的 UTC 時(約台北 06:00)

    # ── LLM(Phase 7;MVP 不呼叫)──
    llm_provider: str = "anthropic"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    llm_daily_budget_usd: float = 3.0

    # ── 硬性安全開關 ──
    auto_trading_enabled: bool = False          # 永遠預設 false(spec 一)
    tradingview_webhook_enabled: bool = False   # Webhook 為付費選配(spec 二之4)

    # ── 資料品質 ──
    live_poll_seconds: int = 15                 # 即時價輪詢間隔(provider 可強制放大)
    source_mismatch_pct: float = 0.0005         # 0.05%
    source_mismatch_atr_mult: float = 0.3       # × 15M ATR
    source_mismatch_event_relax_mult: float = 2.0  # 高波動事件時段放寬倍數
    stale_price_seconds: int = 60

    # ── 市場狀態分類 ──
    state_event_max_age_minutes: int = 180  # FAILED_* 事件超過此時間不再定義市場狀態

    # ── 市場結構引擎 ──
    swing_left_bars: int = 2
    swing_right_bars: int = 2
    swing_min_atr_mult: float = 0.5             # 相鄰 swing 最小 ATR 距離
    swing_min_move_pct: float = 0.0008          # 相鄰 swing 最小百分比距離
    false_break_confirm_bars: int = 3           # 假突破:N 根已收線內收回
    false_break_min_atr_mult: float = 0.1       # 突破幅度最低門檻(× ATR)

    # ── 重要價位 / 區域 ──
    zone_half_width_atr15_mult: float = 0.5     # 區域半寬 = 此倍數 × 15M ATR
    round_number_step: float = 25.0             # 心理關卡間距(黃金)
    level_cluster_atr_mult: float = 0.6         # 相距此倍數 ATR 內的價位合併為同一區

    # ── 追價規則(spec 十五)──
    chase_atr_mult: float = 1.5
    no_chase_near_level_atr_mult: float = 0.75  # 0.5~1.0 之間,預設 0.75

    # ── 事件風控 ──
    event_lockout_minutes: int = 30
    post_event_wait_m15_bars: int = 1

    # ── 風控預設(spec 十六)──
    risk_per_trade_pct: float = 0.5
    daily_max_loss_pct: float = 1.5
    weekly_max_loss_pct: float = 4.0
    max_trades_per_day: int = 3
    stop_after_consecutive_losses: int = 2

    # ── 持倉(手動輸入)──
    gold_contract_oz: float = 100.0     # 1 標準手黃金 = 100 盎司(PnL 計算用,可調)

    # ── TMGM 價格校正(Price Offset)──
    # 分析永遠用 TwelveData;僅劇本進場/停損/停利「輸出價」套用此 Offset 校正為 TMGM 掛單價。
    # Offset = TMGM Price − TwelveData Price(可於 Dashboard 即時修改,存 system_settings)。
    price_offset: float = 0.0
    offset_mode: str = "manual"                 # manual | auto(auto 需 TMGM 即時源,目前保留 UI)
    offset_max_age_hours: int = 24              # offset 超過此時效視為未校準 → NO-SIGNAL
    analysis_source_label: str = "TwelveData"
    trading_broker_label: str = "TMGM"

    # ── 分層更新頻率(三層架構)──
    # 第 1 層:報價層(快速報價源:Capital.com/OANDA;無 Key 時降級用 TD 300s)
    tier1_quote_seconds: int = 60
    tier1_fail_alert_after: int = 5          # 連續失敗 N 次才發一則系統警告
    # 第 2 層:結構層(純程式邏輯,禁 AI)
    tier2_check_seconds: int = 300
    tier2_touch_pct: float = 0.0005          # 觸及候選價位:距離 ≤ 0.05%
    tier2_anomaly_range_mult: float = 2.5    # 5 分鐘振幅 > 近 20 根 5 分桶平均 × 此倍數
    tier2_level_cooldown_minutes: int = 60   # 同一價位觸及事件冷卻
    # 第 3 層:完整分析(事件觸發 + 定時保底)
    tier3_max_age_minutes: int = 60          # 距上次完整分析超過此時間 → 定時保底
    # 頻率保護
    twelve_data_soft_limit: int = 600        # TD 當日達此用量 → 警告 + 降級(只用快取資料)

    # ── Setup 一致性與時效(BUGFIX spec R2/R4/R6)──
    setup_min_rr1: float = 1.5              # 第一目標最低賺賠比,低於即 INVALID
    setup_price_band_pct: float = 0.05      # 所有價位須在現價 ±5% 內(防幻覺價位)
    setup_stale_deviation_pct: float = 0.005  # 現價偏離 entry > 0.5% → STALE
    setup_expiry_bars: int = 8              # 生成後 N 根 15M 未觸發 → STALE
    snapshot_expiry_bars: int = 2           # 快照超過 N 根 15M 無新版本 → 全頁過期警示
    mistake_repeat_log_versions: int = 8    # 「最易犯的錯」連續 N 版相同 → log 警示

    # ── 分析 ──
    candle_history_count: int = 300
    analysis_timeframes: tuple[str, ...] = ("1D", "4H", "1H", "15M")
    aux_timeframes: tuple[str, ...] = ("1W", "30M")

    # ── Golden Dataset ──
    golden_dataset_min_hit_rate: float = 0.85
    golden_dataset_dir: str = "tests/golden_dataset"


@lru_cache
def get_settings() -> Settings:
    """單例設定;測試可用 get_settings.cache_clear() 重載。"""
    return Settings()
