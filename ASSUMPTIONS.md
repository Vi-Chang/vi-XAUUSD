# ASSUMPTIONS — 開發假設與待確認事項

規格書要求:遇到不確定事項時採用安全、可替換、可設定的預設值,並記錄於此。

## 時間與行事曆

1. **交易日歸屬**:NY 17:00 ET 之後的行情歸屬「次一日曆日」交易日(與 OANDA `dailyAlignment=17` 一致)。
2. **每日維護休市**:採 17:00–18:00 ET(黃金 CFD 常見);若 OANDA 實際時段不同,改 `app/utils/timeutils.py` 的 `DAILY_OPEN_HOUR` 即可。
3. **提前收市日**(平安夜等)保守處理為**整日休市**——寧可少分析一天,不可在薄流動性時段誤產訊號。
4. 內建假日表只涵蓋 2026–2027 元旦/聖誕;其餘假日請寫入 `market_calendar` 表。

## 資料源

4a. **TMGM 無公開 REST API**,經由本機已登入的 MT5 終端機(官方 `MetaTrader5` 套件)取行情;
    終端機必須常駐開啟,且僅 Windows 可用(Docker 部署改用 OANDA)。
4b. **MT5 伺服器時差**:預設自動偵測(比對最新 tick 與 UTC);休市中無新鮮 tick 時
    暫用 NY-close 券商慣例(美國夏令 +3 / 冬令 +2),開盤後自動校正,可用
    `MT5_SERVER_UTC_OFFSET_HOURS` 手動鎖定。偵測錯誤的失敗模式是「大量缺 K 棒
    → NO_TRADE_DATA_QUALITY」,不會產生錯誤分析。
4c. **MT5 模式的日/週線不用券商日線**,由 1H 已收線資料依 NY 17:00 ET 本地聚合
    (`candle_service.aggregate_candles`),避免伺服器時區與本系統切分不一致。
4d. MT5 歷史 K 棒的 spread 欄位以商品目前點差近似(MT5 不提供逐棒歷史 bid/ask 蠟燭);
    回測需要精確 spread 時改用 OANDA 歷史資料(price=BAM)。
4e. **Twelve Data 主力模式(無券商帳戶)**:即時價 5 分鐘一輪、K 棒收線才重抓
    (邊界快取)、1D/1W 由長 1H(5000 根 ≈ 217 天)每 6 小時刷新後本地聚合,
    合計約 450 次/日 < 800 免費額度;STALE 門檻自動放寬為輪詢間隔 ×1.5。
    代價:無 bid/ask/spread、無備援交叉驗證、1D 歷史深度約 200 根。
4f. **Twelve Data 週末殭屍報價**(實測發現):XAU/USD 在休市時段仍持續發布幾乎
    不動的報價。已在 Provider 層以 `filter_market_hours` 剔除休市 K 棒,
    analysis_service 另有一道含假日表的防禦性過濾(所有 Provider 通用)。
5. **Twelve Data 免費層 `/price` 無 bid/ask**,備援報價以 mid 近似(bid=ask=mid);交叉驗證只比較 mid,故無影響。
6. **4H/1D/1W 缺漏檢查停用**:各 provider 對 4H 以上的對齊方式不同(OANDA 依 dailyAlignment),
   缺 K 棒檢查只對 UTC 整點對齊的 5M–1H 執行,避免誤報。
7. OANDA Streaming API 尚未接(MVP 用 15 秒 REST 輪詢,已足夠 15M 級分析);Phase 6+ 可補 Streaming + 斷線補齊(補齊機制已存在:每次抓 300 根覆蓋窗口自動回補)。
8. mock 模式以固定 seed 隨機漫步產生資料,跨週期由同一條 5M 路徑聚合,行為與真實資料一致。

## 資料庫與基礎設施

9. **本機快速展示用 SQLite、Redis 可留空**(行程內記憶體快取);Docker Compose 為正式配置(PostgreSQL+Redis)。TimescaleDB 為日後擴充選項。
10. DB 操作為同步 SQLAlchemy Session(查詢皆為短操作);量大後可換 async engine。
11. 通知冷卻狀態存在行程記憶體(重啟即重置);多實例部署時應改存 Redis。

## 引擎參數(全部在 `app/config.py`,可回測調整)

12. Swing 確認:左 2 / 右 2 根、最小距離 max(0.5×ATR, 0.08%)。
13. 假突破:突破幅度 ≥0.1×ATR、3 根已收線內收回。
14. 區域寬度:半寬 0.5×15M ATR;價位聚類距離 0.6×ATR;心理關卡間距 25。
15. 追價:1.5×ATR;距強支撐/壓力 0.75×ATR 內禁追(規格允許 0.5–1.0)。
16. 壓縮判定:15M BB 寬 < 0.004 且 1H ADX < 20(近似值,待回測校正)。
17. evidence_score = 每項成立條件 10 分 + 品質 GOOD 10 分 + 無追價 10 分,上限 100(純加總,無主觀成分)。

## MVP 邊界

18. 經濟事件目前只讀 `data/manual_events.json`(Finnhub/FMP 為 Phase 6);來源失效 → `EVENT_RISK_UNKNOWN`。
19. `trading_coach` / `position_management` / `cross_market_context` 欄位在 MVP 輸出中為空殼(Schema 已定,Phase 6–7 填入)。
20. LLM 完全未接(`llm_cost_usd_today` 恆為 0);觸發政策與預算欄位已預留。
21. 週線 K 棒的 close_time 推算為「開盤後第 5 個交易日 17:00 ET」;以 OANDA 回傳實際值為準(儲存時用 provider 值覆蓋)。

## 環境

22. 本機 Python 3.14(規格書寫 3.12;Docker 映像固定 python:3.12-slim,兩者皆通過測試語法相容)。
23. 開發機為 Windows + OneDrive 目錄;正式部署建議移出 OneDrive 同步資料夾(SQLite 檔案鎖與同步衝突風險)。
