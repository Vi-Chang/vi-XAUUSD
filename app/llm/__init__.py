"""V2 AI 分析層(4 Agent:Macro / Technical / Sentiment / Decision)。

原則:
- 程式算好所有指標,AI 禁止自行計算與腦補數字。
- 固定緊湊 JSON 輸入、固定 10 區塊輸出、價位一律引用候選 ID。
- 程式硬性風控(資料品質、事件鎖定、Offset fail-safe)AI 不可推翻。
- Token 節約:輸入指紋快取、每日預算斷路器、短輸出。
"""
