# Golden Dataset — 市場結構引擎驗收(spec 六,強制)

沒有標準答案,你永遠不知道程式判的結構和你眼睛看的是不是同一回事。
本目錄存放「人工標註的標準答案」,`test_golden.py` 會將結構引擎輸出與標註比對,
**命中率低於 `GOLDEN_DATASET_MIN_HIT_RATE`(預設 0.85)即測試失敗**。
任何結構引擎參數調整,必須先通過本測試才能合併(spec 二十五)。

## 目標(使用者待辦)

至少 3 段、各約 300 根的**真實歷史** 15M 與 1H K 棒,涵蓋:
1. 明確趨勢段
2. 盤整段
3. 含假突破/假跌破段

目前僅含 `example_zigzag_15m.json`(合成資料,由 `make_example.py` 產生,
轉折點即數學上的 ground truth)。請依下方格式持續擴充真實標註。

## 標註格式(*.json)

```json
{
  "meta": {
    "symbol": "XAUUSD",
    "timeframe": "15M",
    "description": "2026-06 CPI 前後的假跌破段",
    "annotator": "manual",
    "params": {"left": 2, "right": 2}
  },
  "candles": [
    {"open_time": "2026-06-10T08:00:00+00:00", "open": 4650.0, "high": 4652.1,
     "low": 4648.3, "close": 4651.0}
  ],
  "annotations": {
    "swing_highs": [{"time": "2026-06-10T10:15:00+00:00", "price": 4661.2}],
    "swing_lows":  [{"time": "2026-06-10T12:30:00+00:00", "price": 4644.8}],
    "bos":   [{"time": "2026-06-10T14:00:00+00:00", "direction": "UP"}],
    "choch": [{"time": "...", "direction": "DOWN"}],
    "false_breakouts": [{"time": "...", "kind": "FAILED_BREAKDOWN"}]
  }
}
```

- `time` 一律填該事件「確認 K 棒」的 open_time(UTC)。
- 沒有的類別留空陣列即可;命中率只計算有標註的類別。
- 比對容忍:swing ±1 根 K 棒、價格 0.2%;BOS/CHoCH/假突破 ±2 根 K 棒。

## 標註流程建議

1. 從 OANDA 匯出(或用 `scripts/` 加一支匯出腳本)一段 300 根 K 棒存成 `candles`。
2. 在 TradingView(資料源選 OANDA:XAUUSD)人工圈出 swing / BOS / 假突破。
3. 把確認 K 棒的 open_time 與價位抄進 `annotations`。
4. `pytest tests/golden_dataset` 驗證。
