# XAUUSD 即時多週期 AI 自動分析系統(v2)

依規格書 v2 建置。目標不是保證預測漲跌,而是產生**可驗證、可追蹤、可回測**的交易計畫。

> **目前進度:MVP(Phase 1–5 + Telegram)已完成。**
> 依規格,MVP 必須實際跑滿 4 週、確認「資料正確、結構判定與人工看圖一致」後,
> 才繼續 Phase 6(事件/跨市場)→ 7(三角色 AI)→ 8(Dashboard)→ 9(兩軌回測)→ 10(Paper Trading)。

核心原則:
**大週期決定背景,1 小時決定當日方向,15 分鐘等待正式觸發;價格結構優先於指標,已收線資料優先於盤中變動,風控可以否決任何交易,沒有優勢就等待。**

---

## 1. 快速開始(零 API Key 展示模式)

```bash
pip install -r requirements.txt
python -m pytest                 # 34 個測試(含 Golden Dataset 驗收)
python scripts/run_demo.py       # 模擬資料完整分析一輪,輸出固定 JSON
```

啟動 API(模擬模式):

```bash
copy .env.example .env           # 預設 MOCK_DATA_MODE=true、SQLite
python -m uvicorn app.main:app --reload
# GET  http://127.0.0.1:8000/health
# GET  http://127.0.0.1:8000/api/analysis/latest
# POST http://127.0.0.1:8000/api/analysis/run
# GET  http://127.0.0.1:8000/api/price
# WS   ws://127.0.0.1:8000/ws
```

## 2. Docker 啟動(PostgreSQL + Redis)

```bash
copy .env.example .env
docker compose up --build
```

資料庫初始化:`python scripts/init_db.py`(開發)或 `alembic revision --autogenerate -m init && alembic upgrade head`(正式)。

## 3. 行情來源設定(實盤資料模式)

### 主力:TMGM(經由 MetaTrader 5,免費)

你已有 TMGM 帳戶,設定步驟:

1. 在**這台 Windows 電腦**安裝 MetaTrader 5 終端機(TMGM 官網下載),登入你的 TMGM 帳戶(Demo 或 Live 皆可,系統只讀行情,不會下單)。
2. `pip install MetaTrader5`(已裝好;此套件僅 Windows 可用,故不在 requirements.txt)。
3. 在 MT5「市場報價」確認黃金代碼(通常是 `XAUUSD`,某些帳戶類型有後綴如 `XAUUSD.pro`),不同時填入 `.env` 的 `MT5_SYMBOL`。
4. `.env` 設 `MOCK_DATA_MODE=false`,啟動系統。**MT5 終端機必須保持開啟**,程式會自動附掛已登入的終端機(不需要在 .env 存密碼)。

時區防呆:MT5 回傳的是券商伺服器時間(TMGM 慣例冬令 UTC+2/夏令 UTC+3),系統會在開盤時自動偵測時差;日線/週線**不用券商日線**,一律由 1H 資料依 NY 17:00 ET 本地聚合,保證與本系統切分規則一致。

限制:MT5 Provider 只能在這台 Windows 主機上跑(Docker/Linux 部署時請改用 OANDA)。

### 備選與其他免費來源

| 來源 | 用途 | 申請網址 | 關鍵限制 |
|---|---|---|---|
| TMGM (MT5) | **主力**即時報價 + K 棒 + 成交紀錄 | 你已有帳戶 | 需 MT5 終端機常駐;僅 Windows |
| Capital.com | 備選(REST+WS,Demo 免費,可跑 Docker) | 平台 Settings → API integrations 產生 Key | Session 10 分鐘閒置逾時(已自動處理) |
| OANDA Practice | 備選(REST,可跑 Docker) | https://www.oanda.com/demo-account/tpa/personal_token | 免費模擬帳戶;遵守速率限制 |
| Twelve Data | 備援報價 | https://twelvedata.com/pricing | 800 次/日、8 次/分 |
| Finnhub | 經濟日曆 + 新聞(Phase 6) | https://finnhub.io/register | 免費層額度可能調整 |
| FRED | 殖利率/總經(Phase 6) | https://fred.stlouisfed.org/docs/api/api_key.html | 日更、非即時 |
| yfinance | 開發/回測 | 免申請 | 非官方、延遲、GC=F 期貨基差 |
| CFTC COT | 黃金持倉(Phase 6) | 官網公開下載 | 每週五更新 |
| TradingView | 人工看圖(圖表資料源選 **OANDA:XAUUSD**) | 免費版 | Webhook 需付費 → 預設關閉 |
| LLM API(Phase 7) | AI 分析 | https://console.anthropic.com | 唯一變動成本,受每日預算上限控管 |

Provider 優先序由 `.env` 的 `PRIMARY_PROVIDER` 控制(`auto` = mock 模式用 mock,否則 MT5 → OANDA)。

(OANDA 備選申請:註冊 → Practice 帳戶 → Manage API Access → 產生 Token 填入 `.env`。)

Telegram:向 @BotFather 建 bot 取得 Token;把 bot 加入對話後用
`https://api.telegram.org/bot<TOKEN>/getUpdates` 查 chat_id,填入 `.env`。

## 4. 目錄結構

```
app/
├── main.py                  # FastAPI:/health、分析 API、WebSocket
├── config.py                # 全部規則參數集中於此(可調)
├── logging_config.py        # 結構化 JSON logging
├── db/models.py             # 19 張資料表(見下)
├── schemas/analysis.py      # 固定輸出 JSON Schema + 候選 ID 驗證
├── providers/               # Adapter Pattern:oanda / twelve_data / yfinance / mock
├── engines/
│   ├── data_quality.py      # 10 項檢查 + 休市防呆 + SOURCE_MISMATCH
│   ├── indicators.py        # EMA/MACD/RSI/KD/ATR/ADX/BB/SuperTrend/Ichimoku/VWAP…
│   ├── market_structure.py  # Swing/HH/HL/LH/LL/BOS/CHoCH/假突破(Golden Dataset 驗收)
│   ├── key_levels.py        # 重要價位 → 候選價位編號制(SUP_ZONE_01…)
│   ├── market_state.py      # 13 種市場狀態分類
│   └── rule_engine.py       # WATCH/PREPARE/NO_TRADE + 追價偵測 + evidence_score
├── services/                # market_calendar / candle_service / analysis_service
│                            # / scheduler(APScheduler)/ heartbeat / event_service
├── notifications/           # Telegram(分級/去重/冷卻)+ log fallback
└── utils/timeutils.py       # NY 17:00 ET 日線切分(zoneinfo,DST 安全)
data/manual_events.json      # 手動維護高影響事件(每週日更新)
tests/golden_dataset/        # 人工標註標準答案 + 驗收測試(<85% 即 fail)
scripts/run_demo.py          # 零 Key 一鍵展示
```

## 5. 資料庫(19 張表,全 UTC)

instruments、candles(含 bid/ask/spread/is_closed/provider)、live_prices、indicators、
market_structures(含確認 K 棒與失效價)、key_levels、**candidate_levels**(價位候選編號制)、
economic_events、news_items、analysis_runs(完整 JSON 快照 + 事後結果回填)、
trade_scenarios(價位欄位=候選 ID)、positions、trade_journal、behavior_flags、
alerts、provider_health(含 Twelve Data 配額)、system_settings、market_calendar、llm_usage。

## 6. 關鍵設計(防呆機制)

- **日線切分**:一律 NY 17:00 ET(`zoneinfo America/New_York`,禁止硬編碼 UTC 偏移);OANDA 請求帶 `dailyAlignment=17`。含 DST 切換測試。
- **休市防呆**:週五 17:00 ET → 週日 18:00 ET 休市 + 每日 17:00–18:00 ET 維護 + 假日表;休市期間不誤報 STALE、不觸發分析、(未來)不呼叫 LLM。
- **已收線原則**:結構/突破/交叉只用 `is_closed=true`;未收線一律標 `PROVISIONAL`(狀態顯示為 `*_PENDING_CONFIRMATION`)。
- **價位候選編號制**:所有劇本價位只能引用 Python 產生的候選 ID,後端反查填數字;未知 ID → 拒絕(`NO_TRADE_AI_INVALID`)。這徹底封死 AI 編造價位的後門。
- **SOURCE_MISMATCH**:門檻 = `max(0.05%, 0.3 × 15M ATR)`,事件時段自動放寬(皆可調)。
- **追價規則**:距 15M 結構點 > 1.5 ATR 或距強支撐/壓力 < 0.75 ATR → `CHASE_*_RISK`,不給進場。
- **心跳**:交易時段每 30 分鐘 Heartbeat;關鍵 job 停擺 5 分鐘內發 RISK 警報;`/health` 供 UptimeRobot。
- **Volume = Tick Volume**,介面與分析中明確標記,不冒充交易所成交量。
- **硬性風控由規則引擎強制執行**(資料品質、事件鎖定、休市),未來 AI 層無權推翻。

## 7. 測試

```bash
python -m pytest                       # 全部
python -m pytest tests/golden_dataset  # 只跑結構引擎驗收
```

Golden Dataset:請依 `tests/golden_dataset/README.md` 持續加入**真實歷史**標註段
(至少 3 段:趨勢/盤整/假突破)。任何結構參數調整必須先通過此測試。

## 8. MVP 之後(依規格順序)

Phase 6 事件與跨市場 → Phase 7 三角色 AI(Market Analyst / Risk Manager / Trading Coach + Decision Engine,LLM 觸發政策與每日預算已在 config 預留)→ Phase 8 Dashboard → Phase 9 兩軌回測(規則引擎 Walk-forward;AI 層僅前向 Paper Trading)→ Phase 10 模擬交易 ≥8 週 → Phase 11(選配)TradingView Webhook。

**安全開關(永遠預設關閉)**:`AUTO_TRADING_ENABLED=false`、`TRADINGVIEW_WEBHOOK_ENABLED=false`。
本系統只提供分析與通知,不自動下單。
