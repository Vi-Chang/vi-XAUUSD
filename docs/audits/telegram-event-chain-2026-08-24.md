# XAUUSD Telegram 事件鏈稽核（2026-08-24 13:30–18:00 UTC+8）

## 取證結論

正式分析紀錄在整段期間持續更新，並非 scheduler 或行情監控停止。正式 API 只保存／公開 15M、1H、4H K 線；1M、5M 時戳無法由歷史紀錄還原，這是既有可觀測性缺口。修正後每輪 lifecycle audit 會保存可取得的 `sourceTimestamps`，缺值不再假裝存在。

舊系統實際 Telegram：13:47「等待最新市場結構」、14:17「做空條件成立／等待回踩」，其後至 18:00 無主要通知。

## 逐輪正式分析與修正後候選事件

|時間|現價|執行來源|修正後主要事件|通知理由|
|---|---:|---|---|---|
|13:31|4638.38|15M 收盤|SETUP_FORMING|新收盤結構形成，尚無可執行區|
|13:46|4634.76|15M 收盤|ENTRY_APPROACHING|價格接近候選區，尚未確認|
|13:52|4641.78|結構事件|NEW_STRUCTURE|盤中新結構改變候選條件|
|13:57|4637.88|結構事件|SETUP_WEAKENING|結構／RR 轉弱|
|14:01|4641.80|15M 收盤|SETUP_FORMING|以新收盤重新計算|
|14:07|4648.92|即時事件|RETRACE_APPROACHING|接近 4648.23–4651.94|
|14:09|4650.94|關鍵價穿越|RETRACE_ZONE_ENTERED|進入區域，但非收盤確認|
|14:16|4655.29|15M 收盤|WAIT_RETRACE|價格高於空方區；不是方向性追空，等待空方確認／回測|
|14:17|4657.39|即時事件|SETUP_WEAKENING|RR／確認不足，禁止 ENTRY_READY|
|14:31|4644.88|15M 收盤|ENTRY_INVALIDATED|狀態轉為 FAILED_BREAKOUT，舊劇本失效|
|14:32|4646.30|即時事件|SETUP_FORMING|失效後仍持續監控新劇本|
|14:46|4645.06|15M 收盤|SETUP_WEAKENING|假突破狀態延續，條件未完整|
|15:01|4651.86|15M 收盤|NEW_STRUCTURE|新收盤改變結構與候選區|
|15:16|4646.26|15M 收盤|WAIT_RETRACE|等待新的合理回測|
|15:31|4641.60|15M 收盤|RETRACE_APPROACHING|接近重新計算後的區域|
|15:37|4646.60|即時事件|SETUP_WEAKENING|離開觀察區且確認不足|
|15:46|4633.16|15M 收盤|RETRACE_ZONE_ENTERED|價格進入新回測區，等待收盤確認|
|15:47|4631.90|即時事件|RETRACE_ZONE_ENTERED|同區同狀態，面板更新、Telegram 去重|
|15:48|4633.63|即時事件|RETRACE_ZONE_ENTERED|同區同狀態，面板更新、Telegram 去重|
|15:58|4643.45|即時事件|REENTRY_AVAILABLE|離區後出現重新評估機會|
|16:01|4639.07|15M 收盤|NEW_STRUCTURE|新收盤重算 entry／stop／targets|
|16:16|4633.97|15M 收盤|TARGET_UPDATED|關鍵條件由約 4642.09 更新至 4629.91|
|16:31|4631.08|15M 收盤|RETRACE_APPROACHING|接近新區域，尚未 ENTRY_READY|
|16:46|4644.27|15M 收盤|PRICE_RAN_AWAY|離開合理區，等待回測而非追價|
|16:58|4637.13|即時事件|WAIT_RETRACE|回落但尚未完成確認|
|17:01|4641.65|15M 收盤|SETUP_WEAKENING|RR／位置仍不合格|
|17:08|4641.76|即時事件|NO_TRADE|沒有改變操作，Telegram 靜默|
|17:16|4638.50|15M 收盤|SETUP_WEAKENING|原資料同時 ENTER_LONG、RR不足、WAIT_RETEST；一致性防線改為不可進場|
|17:18|4636.58|即時事件|WAIT_RETRACE|同一操作，僅更新面板|
|17:31|4636.31|15M 收盤|NEW_STRUCTURE|依新收盤重新計算|
|17:46|4641.72|15M 收盤|SETUP_WEAKENING|風險條件未通過|
|17:53|4643.67|即時事件|ENTRY_INVALIDATED|舊 setup 到期／失效，持續建立下一結構|

## 抑制規則

- 同 lifecycle signature、同 setup、同實質區域：`SKIP_DUPLICATE_EVENT`。
- 狀態相同且 entry／stop／target／RR 未達實質變化：`SKIP_NO_MEANINGFUL_DECISION_CHANGE`。
- 僅 currentPrice、quoteTime、calculatedAt 改變：只更新面板。
- entry zone、失效價、目標、RR bucket、方向或 lifecycle 改變：允許新通知。

## 修正後資料流

`Scheduler / quote / closed candle → Decision Engine（每輪執行） → Signal Lifecycle（每輪保存） → DecisionEvent → Meaningful-change gate → Durable outbox → Telegram receipt`

通知失敗不會停止分析；watchdog 獨立檢查行情、15M 決策週期、outbox worker 及卡住／失敗的通知。
