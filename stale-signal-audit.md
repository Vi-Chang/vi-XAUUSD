# XAUUSD 過期訊號與最後送出驗證稽核

## P0 根因

2026-08-21 23:46 產生的 `ENTER_LONG` 決策將 4607.13～4610.43 視為可執行區，
但通知進入 transactional outbox 後，worker 只檢查 `decisionId/decisionVersion`，沒有在
實際呼叫 Telegram API 前重新取得最新 bid/ask、最新已收盤 15M K 棒、劇本版本、
進場區、追價上限、失效價、目標及有效賺賠比。若 worker 延遲到 00:00 才送出，舊
payload 仍會被直接排版；此時價格約 4617，使用者收到的卻仍是 4607.13～4610.43
可以進場。

## 重複與衝突來源

1. 決策產生時驗證與通知送出時驗證是兩條路，後者缺少交易安全檢查。
2. 面板讀取記憶體中的分析快照，Telegram 讀取 outbox payload；兩者可能停在不同時間。
3. 重試、重啟與排程延遲雖不會繞過 outbox 去重，卻可能讓「唯一一則」過期訊息晚到。
4. 舊程式將通知文字視為已完成結果；現在改為只保存結構化候選，送出前再以唯一目前
   `CurrentFinalDecision` 與最新市場資料產生文字。

## 單一安全出口

`PreDeliveryTradeSafetyGate` 現在同時服務 Telegram 與面板/API。每一則正式進場通知在
真正送出前重新驗證：

- 決策 ID、版本與 scenario 版本仍是目前版本。
- 排隊時間、決策時間與 `validUntil` 尚未過期。
- 最新 tick 未過期，並以多單 ask／空單 bid 判斷實際成交側價格。
- 沒有更新的已收盤 15M K 棒要求整體重算。
- 價格仍在進場區及追價界線內，且失效價、第一目標尚未觸發。
- 價格漂移、點差、滑價後賺賠比及 risk gate 仍通過。

任何一項失敗皆 fail closed：取消該 outbox 訊息、保存取消原因與 delivery snapshot、
寫入 conflict audit，並撤銷面板的進場許可。重試永遠重新驗證，不會沿用第一次結果。

## 延遲案例修正結果

- decision price：4608.00
- approved entry zone：4607.13～4610.43
- delivery ask：4617.00
- queue age：840 秒
- 結果：`ENTRY_PRICE_OUT_OF_RANGE`
- Telegram API 呼叫：0
- 新狀態：`WAIT_RETEST`，白話提示為「原本進場點已錯過，現在不要追；接下來等待回踩。」

## 監控

管理端 conflict metrics 新增過期進場、進場區外、決策過期、K 棒更新、RR 重驗失敗、
排隊逾時與價格漂移等計數。每筆取消通知保存 decision/delivery price、bid/ask、漂移、
queue/decision/candle age、最新 tick 與最新已收盤 K 棒時間，供事故重播。
