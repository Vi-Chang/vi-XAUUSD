"""使用者顯示文字集中管理(白話化)。

原則:
- 這裡只放「給人看的顯示文字」。程式邏輯用的代碼(market_state、decision.action、
  條件前綴 STRUCT:/LEVEL:/CHASE_* 等)一律不動,只在顯示層翻成白話。
- 語氣用「你」,像朋友在旁邊講解;數字要附判讀;不出現規則編號。
"""
from __future__ import annotations

# 市場狀態 → 白話(代碼本身是邏輯用,不改)
MARKET_STATE_ZH: dict[str, str] = {
    "STRONG_BULL_TREND": "強勢上漲",
    "STRONG_BEAR_TREND": "強勢下跌",
    "BULLISH_PULLBACK": "上漲中的回檔",
    "BEARISH_REBOUND": "下跌中的反彈",
    "RANGE": "區間盤整",
    "COMPRESSION": "窄幅整理(準備變盤)",
    "BREAKOUT_PENDING_CONFIRMATION": "剛突破,還要等確認",
    "BREAKDOWN_PENDING_CONFIRMATION": "剛跌破,還要等確認",
    "FAILED_BREAKOUT": "假突破,漲不上去又掉回來",
    "FAILED_BREAKDOWN": "假跌破,跌不下去又漲回來",
    "STRUCTURE_TRANSITION": "多空換手中,方向還不明",
    "EVENT_DRIVEN_VOLATILITY": "消息面大波動",
    "INSUFFICIENT_DATA": "資料不足",
}

# 決策動作 → 白話
ACTION_ZH: dict[str, str] = {
    "NO_TRADE": "不進場(觀望)",
    "WATCH": "先看著",
    "PREPARE_LONG": "準備做多",
    "PREPARE_SHORT": "準備做空",
    "LONG": "做多",
    "SHORT": "做空",
    "MANAGE": "顧好手上的單",
    "EXIT": "出場",
}

# 結構事件 → 白話(圖表標記 / 說明用)
EVENT_TYPE_ZH: dict[str, str] = {
    "BOS_UP": "順勢突破↑",
    "BOS_DOWN": "順勢跌破↓",
    "CHOCH_UP": "反轉↑",
    "CHOCH_DOWN": "反轉↓",
    "FAILED_BREAKOUT": "假突破",
    "FAILED_BREAKDOWN": "假跌破",
}

# 追價風險代碼 → 白話標籤
CHASE_ZH: dict[str, str] = {
    "CHASE_LONG_RISK": "追多風險",
    "CHASE_SHORT_RISK": "追空風險",
}


def state_zh(code: str) -> str:
    return MARKET_STATE_ZH.get(code, code)


def action_zh(code: str) -> str:
    return ACTION_ZH.get(code, code)


def dir_zh(direction: str) -> str:
    return {"LONG": "做多", "SHORT": "做空"}.get(direction, direction)
