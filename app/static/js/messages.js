/* 前端顯示文字集中管理(白話化)。與後端 app/i18n.py 對應。
 * 只翻顯示文字;程式判斷用的代碼(market_state、action、CHASE_* 等)不動。 */
"use strict";

const MSG = {
  state: {
    STRONG_BULL_TREND: "強勢上漲",
    STRONG_BEAR_TREND: "強勢下跌",
    BULLISH_PULLBACK: "上漲中的回檔",
    BEARISH_REBOUND: "下跌中的反彈",
    RANGE: "區間盤整",
    COMPRESSION: "窄幅整理(準備變盤)",
    BREAKOUT_PENDING_CONFIRMATION: "剛突破,還要等確認",
    BREAKDOWN_PENDING_CONFIRMATION: "剛跌破,還要等確認",
    FAILED_BREAKOUT: "假突破,漲不上去又掉回來",
    FAILED_BREAKDOWN: "假跌破,跌不下去又漲回來",
    STRUCTURE_TRANSITION: "多空換手中,方向還不明",
    EVENT_DRIVEN_VOLATILITY: "消息面大波動",
    INSUFFICIENT_DATA: "資料不足",
  },
  action: {
    NO_TRADE: "不進場(觀望)",
    WATCH: "先看著",
    PREPARE_LONG: "準備做多",
    PREPARE_SHORT: "準備做空",
    LONG: "做多",
    SHORT: "做空",
    MANAGE: "顧好手上的單",
    EXIT: "出場",
  },
  event: {
    BOS_UP: "順勢突破↑", BOS_DOWN: "順勢跌破↓",
    CHOCH_UP: "反轉↑", CHOCH_DOWN: "反轉↓",
    FAILED_BREAKOUT: "假突破", FAILED_BREAKDOWN: "假跌破",
  },
  chase: { CHASE_LONG_RISK: "追多風險", CHASE_SHORT_RISK: "追空風險" },
};

const SC_STATUS_ZH = {
  WATCH: "先觀察", PREPARE: "準備中", TRIGGERED: "可進場", INVALIDATED: "已失效",
  INVALID: "已攔截",
};

const stateZh = (c) => (MSG.state[c] || c);
const actionZh = (c) => (MSG.action[c] || c);
