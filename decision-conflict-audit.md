# XAUUSD Decision Consistency Audit — 2026-08-22

## 結論

23:46／23:47 類型問題是 P0。根因不是單一文案，而是三個狀態一致性缺口同時存在：

1. Telegram outbox 在排隊時保存決策，但真正送出前沒有重新比對最新 FinalDecision，舊 WAIT 可以在新 ENTER_LONG 後才送出。
2. Dashboard snapshot 會自行從 continuation／breakout ledger 再選一次 setup，因此 action 可能來自 FinalDecision，進場區與 chase limit 卻來自另一個 scenario/version。
3. 即時報價與完整分析可同時從相同 previous state 計算；原本只用 JSON monitor state 覆寫，沒有資料庫 current-row lock、舊 K 棒防線或全域單調版本。

## 真實案例取證

| 時間／範圍 | 舊決策 | 新決策 | 價格資料 | Root cause | Severity | 修復 |
|---|---|---|---|---|---|---|
| 使用者回報 23:46→23:47 | ENTER_LONG | WAIT／NO_CHASE | 新區 4607.13–4610.43；舊區 4601.36–4605.05；舊 chase 4607.35 | 舊 outbox payload 延遲送出＋presentation 重選舊 setup | P0 | 已修復 |
| 正式 DB 最近可用紀錄（15 分鐘視窗） | ENTRY | WAIT／NO_TRADE | 2 組 | 缺少 current decision delivery guard | P0 | 已修復 |
| 正式 DB 最近可用紀錄（15 分鐘視窗） | entry zone A | entry zone B | 7 組 | 多個 setup source 未由 FinalDecision 固化 | P1 | 已修復 |
| 正式 DB 119 筆 SENT | 決策版本缺失 | — | 115 筆舊格式 | 舊 notification schema 未保存 decision identity | P2 | 新事件已修復；舊歷史保留 |
| EXPIRED→同 setup WAIT | 0 組 | — | 最近可用期間 | 未發現 | P2 | 無需修復 |

其中正式歷史可辨識到兩次 ENTRY 後 15 分鐘內轉 WAIT／NO_TRADE；至少一筆 ENTER 訊息的現價已不在訊息所列 entry zone，證明先前缺少發布前價格一致性 validator。

## 呼叫鏈

```text
market data
→ regime / breakout / pullback / continuation / entry / risk engines
→ SignalCandidate[]
→ FinalDecisionEngine
→ DecisionConsistencyValidator
→ CurrentFinalDecision（transaction + row lock）
→ DecisionEvent + TelegramNotification
→ delivery-time current-decision guard
→ Telegram
```

## 修正後全域規則

- `current_final_decisions.symbol` 唯一索引保證每個商品只有一個 current decision。
- 新 decision 發布時，在同一交易內遞增版本並取消尚未發送的舊通知。
- 舊 candle、舊 data version、舊 evaluatedAt 無法覆蓋較新的 current row。
- Telegram enqueue 與 delivery 兩次檢查 decisionId；已 superseded 的通知改為 `CANCELLED`。
- FinalDecision 固化 entry zone、chase limit、stop、targets、RR、score 與 scenario version。
- Dashboard snapshot 只讀 FinalDecision 固化值，不再重新挑 setup。
- 同一根確認 K、價格仍在已核准進場區時，舊候選重算不得把 ENTER 無理由翻回 WAIT。
- 所有內部矛盾 fail closed 為 `NO_TRADE / SYSTEM_DECISION_CONFLICT`。

## Legacy／直接通知 Audit

- `notify_entry_plan`：已是 deprecated no-op。
- `notify_market_monitor_events`：已是 legacy no-op。
- `process_short_alert`：只產生 SignalFact，不直接發 Telegram。
- 市場交易通知唯一發送者：`decision_outbox.deliver_pending_telegram`。
- heartbeat、資料來源失效、分析工作失敗仍可走 operational notifier；它們是系統健康警報，不包含進出場方向。

## Race Condition Audit

| 類型 | 防線 |
|---|---|
| cron + polling／即時報價 + 完整分析 | current row lock、source candle/data version/evaluatedAt 比較 |
| 多 worker／多 replica | unique symbol current row、`SELECT ... FOR UPDATE`、outbox row lock |
| queue delay／retry／restart | delivery-time decisionId/version guard |
| stale cache | DB current row 為發布真值；API read path 不重算 |
| out-of-order candle | 舊 `sourceCandleCloseTime` 拒絕發布 |
| out-of-order worker | 相同 candle 下舊 data version/evaluatedAt 拒絕發布 |
| scenario/version 價格混用 | `priceScenarioVersions` validator；不一致時 fail closed |

## Conflict Metrics

- `decision_conflict_count`
- `stale_notification_blocked_count`
- `superseded_notification_count`
- `out_of_order_decision_count`
- `duplicate_decision_count`
- `multi_current_decision_count`

## 尚存風險

- 舊歷史 115 筆通知沒有 decision version，無法事後百分之百還原 scenario lineage；保留原始資料，不偽造版本。
- Telegram API 已開始傳送後、但尚未回傳 receipt 的極短網路窗口無法撤回；新版本透過送出前二次 current check 將窗口縮到最小。
- SQLite 測試環境不具 PostgreSQL 完整 row-lock 語意；正式環境使用 PostgreSQL，並以整合測試覆蓋單調發布與 supersede。
