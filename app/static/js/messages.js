/* 前端顯示文字集中管理(白話化)。與後端 app/i18n.py 對應。
 * 只翻顯示文字;程式判斷用的代碼(market_state、action、CHASE_* 等)不動。 */
"use strict";

const MSG = {
  state: {
    STRONG_BULL_TREND: "大週期偏多",
    STRONG_BEAR_TREND: "大週期偏空",
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

const LIFECYCLE_ZH = {
  NO_SETUP: "暫無有效機會", BREAKOUT_PENDING: "突破待確認",
  WAITING_FOR_ENTRY: "等待進入進場區", READY: "符合進場條件",
  MISSED_ENTRY_WAIT_RETEST: "已錯過進場，等待回踩",
  FAILED_BREAKOUT: "突破失敗", EXPIRED: "劇本已失效",
  INVALID: "劇本結構無效", POSITION_MANAGEMENT: "持倉管理中", WATCH: "觀察中",
};

const BLOCK_REASON_ZH = {
  WAITING_CANDLE_CLOSE: "等待15分鐘K棒收盤",
  BREAKOUT_NOT_CONFIRMED: "突破尚未確認", RETEST_NOT_CONFIRMED: "回踩尚未確認",
  PRICE_OUTSIDE_ENTRY_ZONE: "價格不在理想進場區",
  ENTRY_ALREADY_MISSED: "已錯過理想進場位置",
  RISK_REWARD_TOO_LOW: "賺賠比未達到門檻", STRUCTURE_INVALID: "價格結構無效",
  DATA_INCOMPLETE: "行情資料尚未完整", SIGNAL_CONFLICT: "多空訊號互相衝突",
  EXISTING_POSITION: "已有持倉，進入持倉管理", SETUP_EXPIRED: "劇本等待時間已結束",
  MARKET_DATA_STALE: "行情資料已經過期",
};

const QUALITY_ZH = { GOOD: "良好", EXCELLENT: "優秀", FAIR: "普通", POOR: "不佳",
  DEGRADED: "部分可用", STALE: "已過期", FAILED: "失效" };
const GRADE_ZH = { A: "A級（高信心）", B: "B級（中等信心）", C: "C級（低信心）",
  S: "S級（極高信心）", X: "無法評估" };

function translated(map, code, kind) {
  if (map[code]) return map[code];
  console.warn(`未知${kind}`, code);
  return "未知狀態";
}
const stateZh = (c) => translated(MSG.state, c, "市場狀態");
const actionZh = (c) => translated(MSG.action, c, "決策");
const qualityZh = (c) => translated(QUALITY_ZH, c, "品質");
const gradeZh = (c) => translated(GRADE_ZH, c, "信心等級");
const lifecycleZh = (c) => translated(LIFECYCLE_ZH, c, "劇本階段");
const blockReasonZh = (c) => translated(BLOCK_REASON_ZH, c, "阻擋原因");
