"""One user-facing vocabulary for the web panel and Telegram."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

from app.engines.confidence import confidence_label, normalize_signal_score
from app.engines.user_facing_trade_message import UserFacingTradeMessageBuilder

TAIPEI = ZoneInfo("Asia/Taipei")


def plain_trade_status(state: str, *, can_enter: bool = False) -> str:
    """Translate engine lifecycle states into an unambiguous user action."""
    value = str(state or "WAIT")
    # Lifecycle labels describe a setup, not trade permission. Only the
    # canonical decision may grant the green actionable presentation.
    if can_enter:
        return "🟢 現在可以進場"
    if value in {"MISSED_ENTRY", "MISS_ENTRY"}:
        return "🔴 這個進場點已經錯過，不要追價"
    if value in {"EXPIRED", "SETUP_EXPIRED", "INVALIDATED", "NO_ENTRY", "NO_SETUP"}:
        return "⚪ 目前沒有適合的進場機會"
    return "🟡 現在先不要進場"


def _plain_lifecycle(state: str) -> str:
    return {
        "WAIT_CONFIRMATION": "還不能進場，正在等確認",
        "WAIT_BREAKOUT_CONFIRMATION": "等 15 分鐘 K 棒收盤突破後才能進場",
        "WAIT_BREAKOUT_OR_PULLBACK": "同步等待突破，或等待價格回到較好的回踩區",
        "WAIT_PULLBACK_CONFIRMATION": "價格已進入回踩區，正在等 15 分鐘止跌確認",
        "BREAKOUT_CONFIRMED": "突破已由收盤確認，正在檢查進場位置",
        "WAIT_RETEST": "突破已確認，但目前偏離合理位置，等待回踩",
        "CONFIRMED_WAIT_RETEST": "突破已確認，但目前偏離合理位置，等待回踩",
        "ENTRY_READY": "可以進場",
        "ENTRY_READY_BREAKOUT": "突破進場條件成立",
        "ENTRY_READY_RETEST": "回踩進場條件成立",
        "BREAKOUT_ENTRY_READY": "突破進場條件成立",
        "PULLBACK_ENTRY_READY": "回踩進場條件成立",
        "PULLBACK_INVALIDATED": "原本看漲的回踩條件已被破壞",
        "PULLBACK_BREACH_PENDING_CLOSE": "價格盤中跌穿回踩防守位，暫停進場並等待15M收盤",
        "MISSED_ENTRY": "進場點已錯過，現在不要追價",
        "MISS_ENTRY": "進場點已錯過，現在不要追價",
        "EXPIRED": "原本的進場條件已失效，已重新計算",
        "SETUP_EXPIRED": "原本的進場條件已失效，已重新計算",
        "INVALIDATED": "原本判斷已失效，等待新的機會",
        "NO_ENTRY": "目前沒有合格的進場機會",
        "ARMED": "接近原始確認價，等待 15 分鐘收盤",
        "ENTER": "原始確認已完成，目前仍在可執行距離",
        "MANAGE": "訊號已進入管理，後續價位用於止盈與保護",
        "HOLD": "訊號進行中，依目標與保護價管理",
    }.get(state, "等待新的市場條件")


def build_decision_presentation(event: dict) -> dict:
    state = str(event.get("currentState") or event.get("state") or "WAIT")
    direction = str(event.get("direction") or "NONE")
    titles = {
        "LONG_BIAS": "🟡【行情偏多｜尚未到進場區】",
        "LONG_WATCH": "🟡【偏多等待確認｜尚不可進場】",
        "LONG_READY": "🟢【多單進場條件成立】",
        "LONG_MANAGE": "🔵【多單持倉管理】",
        "SHORT_BIAS": "🟡【行情偏空｜尚未到進場區】",
        "SHORT_WATCH": "🟡【偏空等待確認｜尚不可進場】",
        "SHORT_READY": "🟠【空單進場條件成立】",
        "SHORT_MANAGE": "🔵【空單持倉管理】",
        "DATA_STALE": "🔴【資料過期｜暫停交易】",
        "INVALIDATED": "🟠【原交易計畫已失效】",
        "MISSED_ENTRY": "🟡【原進場區已錯過｜禁止追價】",
        "CONFIRMED_WAIT_RETEST": "🟡【突破確認完成｜等待回踩】",
        "HTF_BULLISH_LTF_WEAKENING": "⚠️【短線轉弱，先觀望】",
        "SHORT_TERM_RECOVERING": "⚪【短線正在恢復，還差最後確認】",
        "SHORT_TERM_BULLISH_RESTORED": "🟢【短線重新轉強】",
        "BEARISH_CONFIRMED": "🔴【短線已正式轉空】",
        "ARMED": "🟡【接近原始確認價｜等待收盤】",
        "ENTER": "🟢【原始進場條件成立｜可以進場】",
        "MANAGE": "🔵【訊號進行中｜持倉管理】",
        "HOLD": "🔵【訊號進行中｜依計畫管理】",
    }
    if state.endswith("BIAS"):
        action = "等待，尚未到進場區，請勿追價。"
        tone = "warning"
    elif state.endswith("WATCH"):
        action = "等待，尚不可進場，請勿追價。"
        tone = "warning"
    elif state.endswith("READY") and bool(event.get("canEnter")):
        action = "進場條件已成立。"
        tone = "long_ready" if state == "LONG_READY" else "short_ready"
    elif state.endswith("MANAGE"):
        action = "依最新防守價與分批止盈計畫管理。"
        tone = "manage"
    elif state == "DATA_STALE":
        action, tone = "資料過期，暫停交易。", "danger"
    else:
        action, tone = str(event.get("flatAction") or "等待新的確認條件。"), "neutral"
    title = titles.get(state, "⚪【市場方向暫無法確認】")
    if state.endswith("READY") and not bool(event.get("canEnter")):
        title = ("🟡【空方劇本已確認｜等待可執行價格】"
                 if direction == "SHORT" else
                 "🟡【多方劇本已確認｜等待可執行價格】")
    return {
        "title": title,
        "tone": tone,
        "currentAction": action,
        "state": state,
        "direction": direction,
        "missingCondition": str(event.get("missingCondition") or ""),
        "nextTrigger": str(event.get("confirmation") or "等待最新市場結構"),
        "invalidation": str(
            event.get("cancelCondition")
            or (f"防守價 {float(event['stopLoss']):.2f} 被已收盤 K 線突破"
                if isinstance(event.get("stopLoss"), (int, float)) else "依最新結構防守價失效")
        ),
    }


def _local_time(raw: str) -> str:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            TAIPEI
        ).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw or "未知"


def _closed_candle_text(candle: dict) -> str:
    if not candle.get("available"):
        return f"不可用（{candle.get('error_reason') or 'UNKNOWN'}）"
    try:
        opened = datetime.fromisoformat(str(candle["open_time"]).replace("Z", "+00:00")).astimezone(TAIPEI)
        closed = datetime.fromisoformat(str(candle["close_time"]).replace("Z", "+00:00")).astimezone(TAIPEI)
        return f"{opened:%H:%M}–{closed:%H:%M}｜收盤 {float(candle['close_price']):.2f}"
    except (KeyError, TypeError, ValueError):
        return "不可用（PARSE_ERROR）"


def _format_decision_message_legacy(event: dict) -> str:
    canonical_type = str(event.get("event_type") or "")
    if canonical_type in {
            "EARLY_ENTRY_WATCH", "EARLY_ENTRY_PREPARE", "EARLY_ENTRY_REPLACED",
            "EARLY_ENTRY_MISSED",
            "EARLY_ENTRY_INVALIDATED"}:
        side = str(event.get("candidateSide") or event.get("direction") or "LONG")
        word = "做多" if side == "LONG" else "做空"
        zone = event.get("candidateZone") or event.get("entryZone") or {}
        low = zone.get("low") if zone.get("low") is not None else zone.get("lower")
        high = zone.get("high") if zone.get("high") is not None else zone.get("upper")
        zone_text = (f"{float(low):.2f}–{float(high):.2f}"
                     if isinstance(low, (int, float)) and isinstance(high, (int, float))
                     else "依最新結構計算")
        defense = event.get("candidateDefenseLevel") or event.get("stopLoss")
        reasons = list(event.get("candidateReasons") or [])
        plain_reason = {
            "SWEEP_RECLAIM": "價格掃過關鍵位後快速收回",
            "FAILED_BREAKDOWN": "下方跌破沒有延續，價格重新收回",
            "FAILED_BREAKOUT": "上方突破沒有延續，價格重新跌回",
            "SUPPORT_REJECTION": "支撐區出現明顯承接",
            "RESISTANCE_REJECTION": "壓力區出現明顯賣壓",
            "MICRO_HIGHER_LOW": "短線低點開始墊高",
            "MICRO_LOWER_HIGH": "短線高點開始下移",
            "BREAKOUT_COMPRESSION": "價格靠近突破位並收斂",
        }.get(reasons[0] if reasons else "", "價格與結構正在形成候選機會")
        price = float(event.get("currentPrice") or 0)
        if canonical_type == "EARLY_ENTRY_REPLACED":
            verb = "重新站回" if side == "LONG" else "重新跌回"
            return "\n".join([
                f"🟡【新的{word}機會形成】", f"現價：{price:.2f}",
                f"新觀察區：{zone_text}",
                "原因：舊候選區已失效，系統已立即依最新短線結構重新掃描。",
                f"下一步：等待15M收盤{verb}新確認位置，通過風控後才可進場。",
            ])
        if canonical_type == "EARLY_ENTRY_WATCH":
            lines = [f"👀【開始留意{word}機會】", f"現價：{price:.2f}",
                     f"觀察區：{zone_text}",
                     "現在：只是接近有效價區，尚不可進場。",
                     "下一步：等待價格反應與短線結構確認；成立後會再通知。"]
            if str(event.get("dataHealth") or "") == "DEGRADED_15M":
                lines.append("資料提醒：15M 資料降級，只能觀察，不能確認進場。")
            return "\n".join(lines)
        if canonical_type == "EARLY_ENTRY_PREPARE":
            lines = [f"🟡【準備{word}】", f"現價：{price:.2f}",
                     f"候選區：{zone_text}", f"發生什麼事：{plain_reason}",
                     "現在：先準備，不是正式進場訊號。",
                     "下一步：等待已收盤 15 分鐘 K 棒通過正式進場確認。"]
            if isinstance(defense, (int, float)):
                verb = "跌破" if side == "LONG" else "站上"
                lines.append(f"失效：15M 收盤{verb} {float(defense):.2f}")
            return "\n".join(lines)
        if canonical_type == "EARLY_ENTRY_MISSED":
            return "\n".join([f"⚪【{word}機會已錯過】", f"現價：{price:.2f}",
                                f"原候選區：{zone_text}", "價格已離開安全進場區，目前不要追價。",
                                "下一步：等待新的回踩或突破回測機會。"])
        return "\n".join([f"🔴【{word}準備取消】", f"現價：{price:.2f}",
                            "剛才形成中的結構已失效，目前不要進場。",
                            "下一步：等待新的結構形成。"])
    recovery = event.get("fakeBreakoutRecovery") or {}
    if canonical_type in {
        "FAKE_BREAKOUT_CONFIRMED", "OPPOSITE_SETUP_CONFIRMED",
        "RECOVERY_SETUP_INVALIDATED",
    } or recovery:
        next_action = event.get("nextAction") or recovery.get("nextAction") or {}
        failed_direction = str(recovery.get("invalidatedBreakoutDirection") or "")
        opposite = str(recovery.get("oppositeDirection") or "")
        price = float(event.get("currentPrice") or 0)
        trigger = next_action.get("triggerLevel")
        strong = next_action.get("strongConfirmationLevel")
        invalidation = next_action.get("invalidationLevel")
        targets = next_action.get("targets") or []
        if canonical_type == "RECOVERY_SETUP_INVALIDATED":
            return "\n".join([
                "⚪【快速收復劇本已取消】",
                f"現價：{price:.2f}",
                "原因：價格沒有守住收復條件，這次反向觀察不再使用。",
                "現在：先不要進場，等待系統重新建立市場結構。",
            ])
        failed_text = "空頭跌破" if failed_direction == "SHORT" else "多頭突破"
        bias_text = "多方" if opposite == "LONG" else "空方"
        title = (
            f"🟢【{failed_text}失敗｜{bias_text}重新取得優勢】"
            if opposite == "LONG" else
            f"🔴【{failed_text}失敗｜{bias_text}重新取得優勢】"
        )
        confirmed = canonical_type == "OPPOSITE_SETUP_CONFIRMED"
        lines = [
            title,
            f"現價：{price:.2f}",
            ("發生什麼事：關鍵位跌破後快速站回，且下跌沒有延續。"
             if failed_direction == "SHORT" else
             "發生什麼事：關鍵位突破後快速跌回，且上漲沒有延續。"),
            ("現在：反向觀察條件已由收盤確認，仍需通過進場位置、停損與盈虧比。"
             if confirmed else
             "現在：先不要追價，等待下一根已收盤 15 分鐘 K 棒確認。"),
        ]
        if failed_direction:
            lines.insert(3, f"原{'空方' if failed_direction == 'SHORT' else '多方'}劇本：已取消")
        if isinstance(trigger, (int, float)):
            verb = "站上" if opposite == "LONG" else "跌破"
            lines.append(f"下一觸發：15M 收盤{verb} {float(trigger):.2f}")
        if isinstance(strong, (int, float)):
            verb = "站穩" if opposite == "LONG" else "跌破並站穩"
            lines.append(f"較強確認：15M 收盤{verb} {float(strong):.2f}")
        if isinstance(invalidation, (int, float)):
            verb = "跌破" if opposite == "LONG" else "站上"
            lines.append(f"取消條件：15M 收盤{verb} {float(invalidation):.2f}")
        if targets:
            lines.append("參考目標：" + "、".join(f"{float(item):.2f}" for item in targets[:3]))
        return "\n".join(lines)
    break_state = event.get("breakLifecycle") or {}
    if canonical_type in {"BREAK_PENDING", "LIQUIDITY_SWEEP_CANDIDATE",
                          "FAILED_BREAKDOWN", "FAILED_BREAKOUT",
                          "BREAK_CONFIRMED", "RECLAIM_FAILED"}:
        level = float(event.get("triggerLevel") or break_state.get("level") or 0)
        if canonical_type in {"FAILED_BREAKDOWN", "FAILED_BREAKOUT"}:
            title = "🟢【XAUUSD 關鍵位快速收復】"
            detail = ("下方跌破缺乏延續，疑似空頭陷阱／流動性掃盤。"
                      if canonical_type == "FAILED_BREAKDOWN"
                      else "上方突破缺乏延續，警戒多頭陷阱。")
        elif canonical_type in {"BREAK_CONFIRMED", "RECLAIM_FAILED"}:
            title, detail = "🔴【XAUUSD 突破確認】", "已收破且後續延續，原 tactical structure 失效。"
        else:
            title, detail = "🟡【XAUUSD 跌破尚未確認】", "價格曾越過關鍵位，但尚未取得延續確認。"
        return "\n".join([title, f"關鍵價：{level:.2f}", detail,
                           f"突破品質：{break_state.get('break_confidence', 0)} / 100",
                           f"延續：{break_state.get('follow_through', '不足')}",
                           f"快速收復：{'是' if break_state.get('reclaim_level') else '否'}"])
    profit = event.get("positionProfitDecision") or {}
    if canonical_type in {"TP_HIT", "PROFIT_GIVEBACK_ALERT", "TRAILING_STOP_UPDATE",
                          "PROFIT_STATE_CHANGED"} and profit:
        title = ("⚠️【XAUUSD 獲利回吐擴大】" if canonical_type == "PROFIT_GIVEBACK_ALERT"
                 else "🚀【XAUUSD 延伸行情確認】" if profit.get("extension_confirmed")
                 else "🟢【XAUUSD 已進入主要停利區】")
        return "\n".join([title,
            f"成本：{float(profit.get('reference_entry') or 0):.2f}",
            f"現價：{float(profit.get('current_price') or 0):.2f}",
            f"目前浮盈：{float(profit.get('current_unrealized_profit') or 0):.2f}",
            f"最高浮盈：{float(profit.get('max_unrealized_profit') or 0):.2f}",
            f"已回吐：{float(profit.get('profit_giveback_ratio') or 0):.0%}",
            f"停利分數：{profit.get('take_profit_score', 0)} / 100",
            f"建議：{profit.get('position_action')}",
            f"獲利保護：{profit.get('profit_protection_level') or '—'}"])
    if canonical_type in {"WICK_REJECTION_CHANGED", "REJECTION_BREAKOUT_CHANGED"}:
        state = str(event.get("currentState") or "")
        breakout = str(event.get("breakoutState") or "NONE")
        zone = event.get("zone") or {}
        range_text = (f"{float(zone['low']):.2f}–{float(zone['high']):.2f}"
                      if isinstance(zone.get("low"), (int, float)) and
                      isinstance(zone.get("high"), (int, float)) else "等待新區域")
        if breakout == "BREAKOUT_CONFIRMED":
            return ("🟢【XAUUSD｜拒絕區已被收盤突破】\n"
                    f"區域：{range_text}\n15M 實體已突破並確認守住，續攻條件改善。")
        if state == "REPEATED_UPPER_WICK_REJECTION":
            return ("⚠️【XAUUSD｜15M 上方連續出現賣壓】\n"
                    f"區域：{range_text}\n多方動能可能仍在修復，但價格尚未突破供給區。\n目前：不追多。")
        if state == "REPEATED_LOWER_WICK_REJECTION":
            return ("⚠️【XAUUSD｜15M 下方連續出現承接】\n"
                    f"區域：{range_text}\n空方動能可能仍偏弱，但價格尚未跌破承接區。\n目前：不追空。")
    canonical = event.get("canonicalDecision") or {}
    if canonical_type == "DATA_RECOVERED":
        closed = event.get("latestClosedCandlePrice")
        return "\n".join([
            "✅【XAUUSD 行情資料已恢復】",
            (f"最新15M收盤：{float(closed):.2f}" if isinstance(closed, (int, float))
             else "最新15M收盤已恢復。"),
            (f"資料時間：{event.get('closedBarTimestamp')}"
             if event.get("closedBarTimestamp") else "資料時間：最新有效收盤"),
            "策略已使用最新收盤重新計算；沒有新決策時不會重複通知。",
        ])
    if canonical_type in {
            "DEFENSE_TEST", "DEFENSE_RECLAIMED", "DEFENSE_HELD",
            "DEFENSE_BROKEN_CONFIRMED"}:
        defense = event.get("defenseLevel") or canonical.get("defenseLevel")
        price = float(event.get("currentPrice") or 0)
        bias = str(event.get("marketBias") or canonical.get("marketBias") or "NEUTRAL")
        defense_state = str(event.get("defenseState") or
                            canonical.get("defenseState") or "TESTING")
        data_health = str(event.get("dataHealth") or
                          canonical.get("dataHealth") or "UNKNOWN")
        market_context = (event.get("marketContext") or
                          canonical.get("marketContext") or {})
        structure_1h = str(market_context.get("structure1h") or "UNKNOWN")
        structure_15m = str(market_context.get("structure15m") or "UNKNOWN")
        structure_labels = {
            "BULLISH": "🟢 偏多", "BEARISH": "🔴 偏空",
            "BEARISH_CORRECTION": "🟠 空方修正",
            "BULLISH_CORRECTION": "🟠 多方修正",
            "RANGE": "⚪ 盤整", "UNKNOWN": "🟠 最新資料待確認",
        }
        health_text = {
            "HEALTHY": "🟢 正常", "RECOVERING": "🟡 資料恢復中",
            "DEGRADED": "🟠 15M 資料延遲", "DEGRADED_15M": "🟠 15M 資料延遲",
            "STALE": "🔴 行情資料過期",
        }.get(data_health, "🟠 資料狀態待確認")
        side = str(event.get("defenseSide") or canonical.get("defenseSide") or
                   ("LONG" if bias == "BULLISH" else
                    "SHORT" if bias == "BEARISH" else ""))
        buffer = event.get("confirmationBuffer")
        if not isinstance(buffer, (int, float)):
            buffer = canonical.get("confirmationBuffer")
        buffer = float(buffer) if isinstance(buffer, (int, float)) else 0.0
        confirmed_break_level = (
            float(defense) - buffer if isinstance(defense, (int, float)) and side == "LONG"
            else float(defense) + buffer if isinstance(defense, (int, float)) and side == "SHORT"
            else None)
        if defense_state == "BROKEN_PENDING_CLOSE":
            reclaim_word = "站回" if side == "LONG" else "跌回"
            break_word = "跌破" if side == "LONG" else "站上"
            scenario_name = "Long" if side == "LONG" else "Short"
            defense_name = "多方" if side == "LONG" else "空方"
            return "\n".join([
                "【XAUUSD 現在怎麼做】", "🟠 暫停新進場", f"現價：{price:.2f}",
                f"市場方向：{'🟢 高週期仍偏多' if bias == 'BULLISH' else '🔴 高週期仍偏空' if bias == 'BEARISH' else '⚪ 高週期中性'}",
                f"資料狀態：{health_text}",
                (f"目前狀態：盤中已{'跌破' if side == 'LONG' else '站上'}"
                 f"{defense_name}防守 {float(defense):.2f}，等待收盤確認"
                 if isinstance(defense, (int, float)) else
                 "目前狀態：盤中已穿越防守，等待收盤確認"),
                "新進場：禁止",
                "現在等：最新 15M K 棒正式收盤",
                (f"✅ 收盤重新{reclaim_word} {float(defense):.2f} → 剛收回防守，判斷是否為假跌破並重算原方向入口"
                 if isinstance(defense, (int, float)) else
                 "✅ 收盤收回防守 → 判斷是否為假突破並重算入口"),
                (f"❌ 收盤確認{break_word} {confirmed_break_level:.2f} → 取消目前 {scenario_name} Scenario；高週期方向保留，等待新結構"
                 if confirmed_break_level is not None else
                 f"❌ 收盤確認失守 → 取消目前 {scenario_name} Scenario；高週期方向保留，等待新結構"),
                "目前不做多，也不提前追空。" if side == "LONG"
                else "目前不做空，也不提前追多。",
            ])
        if canonical_type == "DEFENSE_HELD":
            return "\n".join([
                "✅【XAUUSD｜15M 防守確認守住】",
                f"現價：{price:.2f}", f"市場方向：{'🟢 偏多' if bias == 'BULLISH' else '🔴 偏空'}",
                f"資料狀態：{health_text}",
                f"防守位置：{float(defense):.2f}" if isinstance(defense, (int, float)) else "防守位置：—",
                "防守被救回後，後續15M收盤仍持續守住。",
                "下一步：重新計算原方向的合理進場區；未通過 RR 前仍不進場。",
            ])
        if canonical_type == "DEFENSE_RECLAIMED":
            return "\n".join([
                "🟠【XAUUSD｜防守剛收回，尚未確認守穩】",
                f"現價：{price:.2f}",
                f"市場方向：{'🟢 偏多' if bias == 'BULLISH' else '🔴 偏空'}",
                f"資料狀態：{health_text}",
                f"防守位置：{float(defense):.2f}" if isinstance(defense, (int, float)) else "防守位置：—",
                "盤中曾失守，但最新15M收盤已重新收回防守位置。",
                "現在：先不進場；等待下一根15M持續守住，或重新站回局部結構。",
            ])
        if canonical_type == "DEFENSE_BROKEN_CONFIRMED":
            return "\n".join([
                "⚪【XAUUSD 原多方劇本已失效】" if side == "LONG"
                else "⚪【XAUUSD 原空方劇本已失效】", f"現價：{price:.2f}",
                f"高週期方向：{'🟢 偏多' if bias == 'BULLISH' else '🔴 偏空' if bias == 'BEARISH' else '⚪ 中性'}",
                f"1H 結構：{structure_labels.get(structure_1h, '🟠 最新資料待確認')}",
                f"15M 結構：{structure_labels.get(structure_15m, '🟠 最新資料待確認')}",
                f"資料狀態：{health_text}",
                f"防守位置：{float(defense):.2f}" if isinstance(defense, (int, float)) else "防守位置：—",
                "15M 已收盤確認跌破／站上防守緩衝。",
                "原交易劇本已永久失效，不再等待原防守恢復。",
                "現在：等待新的止跌／reclaim，或跌破後反抽失敗且 RR 合格。",
                "目前不抄底，也不在急跌後追空；系統不會因此直接反手。",
            ])
        return "\n".join([
            "【XAUUSD 現在怎麼做】", "🟠 先不要進場", f"現價：{price:.2f}",
            f"市場方向：{'🟢 偏多' if bias == 'BULLISH' else '🔴 偏空' if bias == 'BEARISH' else '⚪ 中性'}",
            f"資料狀態：{health_text}",
            "目前狀態：價格接近原方向防守，但尚未方向性穿越",
            f"防守位置：{float(defense):.2f}" if isinstance(defense, (int, float)) else "防守位置：—",
            "現在等：這根 15M K 棒正式收盤。",
            "收盤守住 → 重新檢查原方向入口",
            "收盤確認失守 → 只取消這個交易劇本，高週期方向保留；目前不提前反手。",
        ])
    if canonical_type == "MARKET_BEHAVIOR_CHANGED" and str(
            canonical.get("notificationRoute") or "NEW_ENTRY") != "POSITION_MANAGEMENT":
        labels = {
            "STRONG_RISE": "急漲", "SLOW_RISE": "緩步上升", "RANGE": "盤整",
            "PULLBACK": "多頭回檔", "SLOW_BEARISH_DRIFT": "緩步下降",
            "STRONG_DECLINE": "急跌", "REBOUND": "空頭反彈",
            "REVERSAL_WARNING": "反轉警告", "REVERSAL_CONFIRMED": "反轉已確認",
        }
        behavior = str(event.get("marketBehavior") or "RANGE")
        previous = str(event.get("previousBehavior") or "RANGE")
        bias = str(event.get("marketBias") or "NEUTRAL")
        action = ("暫停追多，等待止跌確認。" if bias == "BULLISH" and behavior in {
            "SLOW_BEARISH_DRIFT", "PULLBACK", "REVERSAL_WARNING"}
            else "等待下一個已收盤 K 棒確認。")
        return "\n".join([
            "⚠️【XAUUSD｜15M 價格行為改變】",
            f"由「{labels.get(previous, previous)}」變為「{labels.get(behavior, behavior)}」。",
            f"大方向：{'偏多' if bias == 'BULLISH' else '偏空' if bias == 'BEARISH' else '中立'}（價格行為不會自動改寫大方向）",
            f"信心分數：{event.get('behaviorConfidence') or 0}/100",
            f"現在：{action}",
        ])
    if canonical_type == "NEW_RECLAIM_EVENT":
        return "\n".join([
            "🔄【XAUUSD｜出現新的 reclaim 結構】",
            "舊交易劇本：維持失效，不會重新啟用。",
            f"新候選劇本：{event.get('newScenarioId') or event.get('scenarioId') or '正在建立'}",
            "系統將依最新結構重新計算進場、防守、止盈與 RR。",
            "目前：等待新劇本完成確認，尚不可進場。",
        ])
    if canonical:
        entry = canonical.get("newEntryDecision") or {}
        trigger = canonical.get("canonicalNextTrigger") or {}
        position = canonical.get("positionManagement") or {}
        route = str(canonical.get("notificationRoute") or "NEW_ENTRY")
        completeness = canonical.get("decisionCompleteness") or {}
        candle = canonical.get("closedCandle") or {}
        action = str(canonical.get("primaryAction") or "WAIT")
        if route == "POSITION_MANAGEMENT":
            lines = ["🔵【XAUUSD 持倉管理】",
                     f"現價：{float(event.get('currentPrice') or position.get('currentPrice') or 0):.2f}"]
            for item in position.get("perPositionDecisions") or []:
                side = "多單" if item.get("side") == "LONG" else "空單"
                lines.extend([
                    f"{side} {item.get('positionClass') or 'CORE'}｜成本 {item.get('actualEntryPrice')}｜數量 {item.get('actualSize')}",
                    f"目前動作：{item.get('positionAction') or position.get('action') or 'HOLD'}",
                    f"短線持倉防守：{item.get('tacticalDefenseLevel') or '—'}",
                    f"大結構失效：{item.get('structuralInvalidationLevel') or '—'}",
                ])
            if not candle.get("available"):
                lines.append(f"⚠️ 已收15M資料缺口：{candle.get('error_reason') or 'UNKNOWN'}；硬停損與即時風控仍持續。")
            else:
                lines.append(f"最新已收15M：{_closed_candle_text(candle)}")
            lines.extend([
                f"新開部位：{entry.get('action') or 'WAIT'}，{'可評估' if entry.get('canEnter') else '不追價／不加碼'}。",
                "主通知已依實際持倉切換為持倉管理。",
            ])
            return "\n".join(lines)
        entry_confirmation = str(canonical.get("entryConfirmation") or "")
        scenario_validity = str(canonical.get("scenarioValidity") or
                                "PENDING_CONFIRMATION")
        if entry_confirmation in {"WAIT_15M_CLOSE", "BLOCKED_BY_DATA"}:
            bias = str(canonical.get("marketBias") or "NEUTRAL")
            lines = [
                "【XAUUSD 資料確認中】", "🟠 暫停新進場",
                f"市場方向：{'🟢 偏多' if bias == 'BULLISH' else '🔴 偏空' if bias == 'BEARISH' else '⚪ 中性'}",
                "資料狀態：最新 15M 收盤暫缺",
                "系統仍保留原市場方向，但取得最新已收盤 K 棒以前，不產生新的 ENTRY_READY。",
                "原進場、停損與止盈：暫不具執行效力。",
            ]
            if (scenario_validity in {"INVALIDATED", "STALE"}
                    or str(canonical.get("scenarioState") or "") == "INVALIDATED"):
                lines.extend([
                    "原交易劇本：仍維持已確認失效。",
                    "資料延遲不會讓策略狀態退回等待原防守。",
                ])
            return "\n".join(lines)
        if scenario_validity in {"INVALIDATED", "STALE"}:
            bias = str(canonical.get("marketBias") or "NEUTRAL")
            return "\n".join([
                "⚪【XAUUSD｜原交易劇本已失效】",
                f"市場方向：{'🟢 偏多' if bias == 'BULLISH' else '🔴 偏空' if bias == 'BEARISH' else '⚪ 中性'}（未被這次失效改寫）",
                "目前動作：先不要進場。",
                "原進場、停損與止盈：暫不具執行效力。",
                "下一步：等待系統依最新短線結構建立新劇本。",
            ])
        if completeness and not completeness.get("valid"):
            return ("⚠️【XAUUSD 決策資料不完整】\n"
                    "暫停交易判斷。\n"
                    f"原因：{'、'.join(completeness.get('errors') or ['UNKNOWN'])}")
        title = ("🟢【XAUUSD｜現在可以進場】" if action in {"BUY", "SELL"}
                 else "🟡【XAUUSD｜現在先不要進場】")
        lines = [title, f"現價：{float(event.get('currentPrice') or 0):.2f}",
                 f"原因：{canonical.get('primaryReason')}",
                 f"最近可執行觸發：{trigger.get('label')}"]
        chosen = entry.get("selectedSetup") or {}
        if chosen:
            zone = chosen.get("entryZone") or {}
            lines.extend([
                f"{chosen.get('entryZoneLabel')}：{zone.get('low') or '—'}～{zone.get('high') or '—'}",
                (f"可執行 RR：{chosen.get('executableRR')}" if chosen.get('executableRR') is not None
                 else f"預估 RR：{chosen.get('estimatedRR') if chosen.get('estimatedRR') is not None else '—'}"),
            ])
        opportunities = canonical.get("entryOpportunities") or []
        labels = {"SHALLOW_PULLBACK": "淺回踩",
                  "DEEP_PULLBACK": "深度備案",
                  "BREAKOUT_RETEST": "突破回測"}
        for opportunity in opportunities[:3]:
            zone = opportunity.get("entry_zone") or {}
            role = ("備用觀察區" if opportunity.get("anchor_role") ==
                    "DEEP_PULLBACK_BACKUP" else
                    labels.get(opportunity.get("type"), opportunity.get("type")))
            lines.append(
                f"{role} "
                f"{zone.get('lower', '—')}～{zone.get('upper', '—')}｜"
                f"預估 RR {opportunity.get('estimated_rr') if opportunity.get('estimated_rr') is not None else '—'}")
        if not position.get("positionKnown"):
            lines.append("持倉：未取得實際持倉資料")
        else:
            lines.append(
                f"持倉：{position.get('actualSide')} 成本 {position.get('actualEntryPrice')}，"
                f"目前動作 {position.get('action')}")
        lines.append(f"收盤確認來源：最新已收15M {_closed_candle_text(candle) if candle else canonical.get('lastClosedCandleTime') or '—'}")
        return "\n".join(lines)
    if canonical_type == "NEW_RECLAIM_EVENT":
        return "\n".join([
            "🔄【XAUUSD｜出現新的 reclaim 結構】",
            "舊交易劇本：維持失效，不會重新啟用。",
            f"新候選劇本：{event.get('newScenarioId') or event.get('scenarioId') or '正在建立'}",
            "系統將依最新結構重新計算進場、防守、止盈與 RR。",
            "目前：等待新劇本完成確認，尚不可進場。",
        ])
    if canonical_type in {"DATA_DELAYED", "DATA_STALE"}:
        scenario_invalid = str(canonical.get("scenarioState") or "") == "INVALIDATED"
        lines = [
            "🔴【XAUUSD 行情資料延遲】",
            "資料恢復前暫停新的進場確認；持倉防守提醒不會因此關閉。",
        ]
        if scenario_invalid:
            lines.extend([
                "原交易劇本：仍維持已確認失效。",
                "資料延遲不會讓策略狀態退回等待原防守。",
            ])
        return "\n".join(lines)
    if canonical_type in {"ENTRY_READY", "ENTRY_NOW"}:
        zone = event.get("entryZone") or {}
        return "\n".join([
            "🟢【XAUUSD 進場條件成立】",
            f"方向：{'做多' if event.get('direction') == 'LONG' else '做空'}",
            f"現價：{float(event.get('currentPrice') or 0):.2f}",
            f"可執行區：{zone.get('low', '—')}～{zone.get('high', '—')}",
            f"失效價：{event.get('stopLoss') or '—'}",
            f"風險報酬比：{event.get('effectiveRR') or '—'}",
            "下一步：條件已成立，不再等待更高或更低的新門檻。",
        ])
    if canonical_type == "WAIT_RETEST":
        return "\n".join([
            "🟡【XAUUSD 訊號已成立，但價格已跑遠】",
            f"目前：{float(event.get('currentPrice') or 0):.2f}",
            "下一步：等待第一次合理回踩，不是等待更遠的新突破。",
        ])
    if canonical_type in {
        "POSITION_WARNING", "SOFT_INVALIDATION_PENDING", "SOFT_INVALIDATED",
        "HARD_INVALIDATED", "POSITION_RECOVERED",
        "POSITION_DATA_RISK",
    }:
        thesis = event.get("tradeThesis") or {}
        warning = event.get("warningLevel") or thesis.get("warningLevel")
        hard = event.get("hardInvalidation") or (thesis.get("hardInvalidation") or {}).get("level")
        current = float(event.get("currentPrice") or 0)
        direction = "多單" if event.get("direction") == "LONG" else "空單"
        common = [
            f"現價：{current:.2f}",
            f"交易論點：{thesis.get('thesisDescription') or '依原始結構建立的交易論點'}",
            f"警戒線：{float(warning):.2f}" if isinstance(warning, (int, float)) else "警戒線：—",
            f"結構硬失效：{float(hard):.2f}" if isinstance(hard, (int, float)) else "結構硬失效：—",
        ]
        if canonical_type == "POSITION_WARNING":
            return "\n".join(["⚠️【持倉進入警戒｜尚未正式失效】", *common,
                              f"若你持有{direction}：禁止加碼，等待15M收盤確認。"])
        if canonical_type == "POSITION_DATA_RISK":
            return "\n".join(["🔴【持倉資料風險】", *common,
                              "行情資料已過期；停止新進場，保留既定 emergency stop 與券商端保護。"])
        if canonical_type == "SOFT_INVALIDATION_PENDING":
            return "\n".join(["🟠【交易論點受壓｜等待有限時間收復】", *common,
                              f"收復期限：{event.get('reclaimDeadline') or '下一根15M'}",
                              "尚未退出，但期限不會再延長。"])
        if canonical_type == "POSITION_RECOVERED":
            return "\n".join(["✅【防守成功｜原交易論點仍有效】", *common,
                              f"若你持有{direction}：恢復依原計畫管理，不代表建立新進場。"])
        if canonical_type == "SOFT_INVALIDATED":
            return "\n".join(["🔴【交易論點明顯受損】", *common,
                              f"若你持有{direction}：依原風控退出／減倉，不得把防守線移遠。"])
        return "\n".join(["⛔【原交易論點正式失效】", *common,
                          f"若你持有{direction}：立即依風控退出。"])
    if canonical_type in {"POSITION_DEFEND", "STOP_TRIGGERED", "POSITION_EXIT"}:
        return "\n".join([
            "⚠️【XAUUSD 持倉條件提醒】",
            f"事件：{canonical_type}",
            f"目前：{float(event.get('currentPrice') or 0):.2f}",
            f"防守價：{event.get('stopLoss') or '—'}",
            "若你持有相同方向部位：請依原風控處理；防守價不向不利方向移動。",
        ])
    double_sweep = event.get("doubleSweepEvent") or {}
    if double_sweep.get("event"):
        return _format_double_sweep(event, double_sweep)
    assistant = event.get("decisionAssistant") or {}
    if assistant:
        return _format_decision_assistant(event, assistant)
    continuation_event = event.get("trendContinuationEvent") or {}
    if continuation_event.get("setupId"):
        return _format_trend_continuation_event(event, continuation_event)
    breakout_event = event.get("breakoutSetupEvent") or {}
    if breakout_event.get("setupId"):
        return _format_breakout_setup_event(event, breakout_event)
    position_event = event.get("positionEvent") or {}
    if position_event.get("tradePlanId"):
        return _format_position_event(event, position_event)
    active_plans = event.get("activeTradePlans") or []
    if str(event.get("currentState") or "").endswith("MANAGE") and active_plans:
        return _format_management_plan(event, active_plans[0])
    view = event.get("presentation") or build_decision_presentation(event)
    price = float(event.get("currentPrice") or 0)
    closed = event.get("latestClosedCandlePrice")
    closed_price = f"{float(closed):.2f}" if isinstance(closed, (int, float)) else "未知"
    closed_time = _local_time(str(event.get("candleCloseTime") or ""))
    data_time = _local_time(str(event.get("calculatedAt") or ""))
    state = str(event.get("currentState") or "WAIT")
    if state in {"BULLISH_RESTORED", "SHORT_TERM_BULLISH_RESTORED"}:
        restored_level = event.get("triggerLevel")
        return "\n".join([
            "🟢【短線重新轉強】",
            (f"15分鐘已收盤站回 {float(restored_level):.2f} 上方。"
             if isinstance(restored_level, (int, float)) else "15分鐘已收盤站回重新轉強位置。"),
            "原本的短線轉弱判斷已取消。",
            "目前重新評估突破進場與回踩進場。",
        ])
    if state == "BEARISH_CONFIRMED":
        return ("🔴【短線已正式轉空】\n"
                "15分鐘與1小時已收盤結構同步轉空。\n"
                "目前重新評估空方進場位置、失效價與賺賠比。")
    if state == "HTF_BULLISH_LTF_WEAKENING":
        regain = event.get("triggerLevel")
        short_level = event.get("longDefensePrice") or event.get("stopLoss")
        lines = [
            "⚠️【短線轉弱，先觀望】",
            "15分鐘正在回檔，但1H／4H還沒有正式翻空。",
            "現在不追多，也先不追空。",
            (f"重新轉強：15M收盤站上 {float(regain):.2f}"
             if isinstance(regain, (int, float)) else "重新轉強：等待15M收盤站回最新壓力"),
            (f"轉空確認：15M／1H收盤跌破 {float(short_level):.2f}"
             if isinstance(short_level, (int, float)) else "轉空確認：等待15M／1H跌破最新支撐"),
        ]
        return "\n".join(lines)
    score = normalize_signal_score(event.get("signalScore"))
    score_text = str(score) if score is not None else "無有效分數"
    headline = plain_trade_status(state, can_enter=bool(event.get("canEnter")))
    lines = [
        "【XAUUSD 現在怎麼做】",
        headline,
        f"現價：{price:.2f}",
        f"市場方向：{view['title']}",
        f"原因：{event.get('blockedReason') or view['missingCondition'] or event.get('transitionReason') or view['currentAction']}",
        f"訊號信心：{confidence_label(score)}（{score_text}）",
        f"最新已收盤 15M：{closed_price}（{closed_time}，UTC+8）",
    ]
    reasons = list(event.get("transitionReasons") or [])
    if not reasons and event.get("transitionReason"):
        reasons = [str(event["transitionReason"])]
    if reasons:
        lines.append("變化：\n" + "\n".join(f"• {reason}" for reason in reasons))
    completed = event.get("completedTriggers") or []
    if completed:
        item = completed[-1]
        verb = "站上" if item.get("condition") == "closeAbove" else "跌破"
        lines.append(f"已完成：15M 收盤{verb} {float(item['level']):.2f}")
    if state.endswith("READY") and bool(event.get("canEnter")):
        zone = event.get("entryZone") or {}
        targets = event.get("targets") or []
        lines.extend([
            f"建議進場區間：{zone.get('low', '—')}–{zone.get('high', '—')}",
            f"防守價：{event.get('stopLoss') or '—'}",
            "分批止盈價：" + ("／".join(str(value) for value in targets) or "—"),
            f"條件失效標準：{view['invalidation']}",
        ])
    else:
        lines.extend([
            f"下一個觸發：{view['nextTrigger']}",
            "確認方式：不是瞬間碰到價格，而是等這根 15 分鐘 K 棒真正收完。",
            "條件成立後：系統會重新檢查進場區、追價上限與風控，符合才通知可以進場。",
            f"判斷取消：{view['invalidation']}（碰到這裡代表原本方向不再成立）",
            "下一步：條件成立、失效或出現新的回踩機會時，系統會主動通知。",
        ])
    lines.append(f"資料時間：{data_time}（UTC+8）")
    return "\n".join(lines)


def _format_double_sweep(event: dict, state: dict) -> str:
    item = state["event"]
    profile = state.get("profile") or {}
    lifecycle = state.get("lifecycle") or {}
    order = "先掃上方、再掃下方" if item.get("order") == "HIGH_THEN_LOW" else "先掃下方、再掃上方"
    sample = int(profile.get("sampleSize") or 0)
    if sample < 20:
        statistical = f"歷史同類樣本只有 {sample} 筆，暫不影響買賣判斷"
    else:
        bias_key = str(profile.get("directionalBias") or "")
        bias = {"UP": "偏向後續上行", "DOWN": "偏向後續下行"}.get(
            bias_key, "沒有明確方向")
        statistical = f"歷史同類 {sample} 筆，{bias}；剩餘統計優勢 {float(lifecycle.get('doubleSweepEdgeRemaining') or 0) * 100:.0f}%"
    return "\n".join([
        "🔎【XAUUSD｜雙邊掃價已確認】",
        f"型態：{order}",
        f"參考區間：{float(item['referenceLow']):.2f}～{float(item['referenceHigh']):.2f}",
        f"收回品質：{item.get('reclaimQuality', 0)} 分",
        f"統計說明：{statistical}",
        "現在怎麼做：這是市場背景資訊，不會單獨叫你進場，也不會改寫停損。",
    ])


def _format_decision_assistant(event: dict, assistant: dict) -> str:
    regime = str(assistant.get("regime") or "")
    regime_state = assistant.get("regimeState") or {}
    reclaim = regime_state.get("reclaimLevel")
    if regime == "SHORT_TERM_RECOVERING":
        return "\n".join([
            "⚪【短線正在恢復，還差最後確認】",
            "大方向仍偏多，15分鐘動能已改善。",
            (f"最後確認：15分鐘K棒收盤站上 {float(reclaim):.2f}"
             if isinstance(reclaim, (int, float)) else "最後確認：等待15分鐘收盤站回最新壓力"),
            "現在先不要追價；確認後會重新計算突破與回踩進場。",
        ])
    if regime == "SHORT_TERM_BULLISH_RESTORED":
        return "\n".join([
            "🟢【短線重新轉強】",
            (f"15分鐘已收盤站回 {float(reclaim):.2f} 上方。"
             if isinstance(reclaim, (int, float)) else "15分鐘已收盤站回重新轉強位置。"),
            "原本的短線轉弱判斷已取消。",
            "目前正在重新評估突破進場、回踩進場、追價上限與賺賠比。",
        ])
    if regime == "BEARISH_CONFIRMED":
        return ("🔴【短線已正式轉空】\n"
                "15分鐘與1小時已收盤結構同步轉空。\n"
                "這不是單一指標轉弱；系統已重新評估空方進場與風控。")
    action = str(assistant.get("actionSummary") or "現在先等")
    icons = {"現在可以進": "🟢", "現在先等": "🟡", "不要追價": "🔴",
             "短線轉弱": "⚠️", "沒有好機會": "⚪", "等回踩": "🟡",
             "等突破": "🟡", "多方失效": "❌", "空方失效": "❌"}
    icon = icons.get(action, "🟡")
    direction = "做多" if assistant.get("direction") == "LONG" else "做空" if assistant.get("direction") == "SHORT" else "暫無方向"
    zone = assistant.get("entryZone") or {}
    zone_text = (f"{float(zone['low']):.2f}–{float(zone['high']):.2f}"
                 if isinstance(zone.get("low"), (int, float)) and isinstance(zone.get("high"), (int, float)) else "尚未形成")
    invalidation = assistant.get("invalidation")
    targets = assistant.get("targets") or []
    target_text = "／".join(f"{float(v):.2f}" for v in targets[:3]) or "尚未形成"
    if assistant.get("canEnter"):
        return "\n".join([
            f"{icon}【{action}｜{direction}】",
            f"進場區：{zone_text}",
            f"失效位：{float(invalidation):.2f}" if isinstance(invalidation, (int, float)) else "失效位：尚未形成",
            f"目標：{target_text}",
            f"訊號品質：{assistant.get('entryQualityGrade')}級（{assistant.get('entryQualityScore')}分）｜RR {float(assistant.get('rewardRiskRatio') or 0):.2f}",
        ])
    reasons = assistant.get("noTradeReasons") or assistant.get("why", {}).get("blocked") or []
    reason = str(reasons[0]) if reasons else "條件尚未完整"
    return "\n".join([
        f"{icon}【{action}】",
        f"現價：{float(event.get('currentPrice') or 0):.2f}",
        f"原因：{reason}",
        f"下一步：{assistant.get('nextTrigger') or '等待新結構'}",
        (f"失效位：{float(invalidation):.2f}" if isinstance(invalidation, (int, float)) else "失效位：等待結構形成"),
    ])


def _format_trend_continuation_event(event: dict, continuation_event: dict) -> str:
    setup = continuation_event.get("setup") or {}
    direction = str(setup.get("direction") or continuation_event.get("direction") or "LONG")
    is_long = direction == "LONG"
    side = "多單" if is_long else "空單"
    close_action = "跌破" if is_long else "站上"
    names = {
        "SHALLOW_PULLBACK_LONG": "淺回踩續漲",
        "BREAKOUT_RETEST_LONG": "突破回踩做多",
        "BULL_FLAG_CONTINUATION": "旗形突破做多",
        "MOMENTUM_CONTINUATION": "多方動能延續（風險較高）",
        "SHALLOW_PULLBACK_SHORT": "淺反彈續跌",
        "BREAKOUT_RETEST_SHORT": "跌破回測做空",
        "BEAR_FLAG_CONTINUATION": "空方旗形跌破",
        "MOMENTUM_CONTINUATION_SHORT": "空方動能延續（風險較高）",
    }
    return "\n".join([
        f"🟢【{side}進場條件成立｜可以進場】",
        f"劇本：{names.get(str(setup.get('type') or ''), str(setup.get('type') or ''))}",
        f"建議進場區：{float(setup.get('entryZoneLow') or 0):.2f}–{float(setup.get('entryZoneHigh') or 0):.2f}",
        f"現價：{float(event.get('currentPrice') or 0):.2f}",
        f"防守價：{float(setup.get('stopPrice') or 0):.2f}",
        f"TP1：{float(setup.get('tp1') or 0):.2f}",
        f"TP2：{float(setup.get('tp2') or 0):.2f}",
        f"TP3：{float(setup.get('tp3') or 0):.2f}",
        f"預估賺賠比：{float(setup.get('riskReward') or 0):.2f}",
        f"訊號信心：{int(setup.get('signalScore') or 0)}（不是勝率）",
        f"條件失效：15M 收盤{close_action} {float(setup.get('stopPrice') or 0):.2f}",
        f"資料時間：{_local_time(str(event.get('calculatedAt') or ''))}（UTC+8）",
    ])


def _format_breakout_setup_event(event: dict, breakout_event: dict) -> str:
    setup = breakout_event.get("setup") or {}
    state = str(breakout_event.get("currentState") or "")
    direction = cast(Literal["LONG", "SHORT"], str(setup.get("direction") or "LONG"))
    trigger = float(setup.get("breakoutTrigger") or 0)
    zone = f"{float(setup.get('entryZoneLow') or 0):.2f}–{float(setup.get('entryZoneHigh') or 0):.2f}"
    retest = f"{float(setup.get('retestZoneLow') or 0):.2f}–{float(setup.get('retestZoneHigh') or 0):.2f}"
    actionable = (bool(event.get("canEnter")) and
                  str(event.get("finalAction") or "") in {"ENTER_LONG", "ENTER_SHORT"})
    if (state in {"ENTRY_READY_BREAKOUT", "ENTRY_READY_RETEST",
                  "BREAKOUT_ENTRY_READY", "PULLBACK_ENTRY_READY"} and actionable):
        entry_type = ("突破進場" if state in {"ENTRY_READY_BREAKOUT", "BREAKOUT_ENTRY_READY"}
                      else "回踩進場")
        if entry_type == "回踩進場":
            zone = (f"{float(setup.get('pullbackEntryZoneLow') or setup.get('entryZoneLow') or 0):.2f}–"
                    f"{float(setup.get('pullbackEntryZoneHigh') or setup.get('entryZoneHigh') or 0):.2f}")
        from app.engines.scenario_safety import calculate_risk_reward
        entry_for_rr = float(setup.get("entryZoneHigh") or trigger)
        rr_details = calculate_risk_reward(
            direction, evaluation_entry_price=entry_for_rr,
            stop_loss=float(setup.get("stopPrice") or trigger),
            target_price=float(setup.get("tp1") or trigger))
        rr = float(rr_details["ratio"] or 0)
        return "\n".join([
            "🟢🟢【進場條件成立】",
            f"方向：{'做多' if direction == 'LONG' else '做空'}",
            f"劇本：{setup.get('setupId')}",
            f"進場類型：{entry_type}",
            f"建議進場區：{zone}",
            f"現價：{float(event.get('currentPrice') or 0):.2f}",
            f"防守價：{float(setup.get('stopPrice') or 0):.2f}",
            f"TP1：{float(setup.get('tp1') or 0):.2f}",
            f"TP2：{float(setup.get('tp2') or 0):.2f}",
            f"TP3：{float(setup.get('tp3') or 0):.2f}",
            f"賺賠比：{rr:.2f}",
            "目前狀態：可以進場",
            (f"條件失效：15M 收盤{'跌破' if direction == 'LONG' else '站上'} "
             f"{float(setup.get('stopPrice') or 0):.2f}"),
            f"資料時間：{_local_time(str(event.get('calculatedAt') or ''))}（UTC+8）",
        ])
    current = float(event.get("currentPrice") or 0)
    max_chase = float(setup.get("maxChasePrice") or 0)
    verb = "站上" if direction == "LONG" else "跌破"
    breakout_zone = zone
    next_setup = next((item for item in reversed(event.get("breakoutSetups") or [])
                       if item.get("setupId") != setup.get("setupId")
                       and item.get("direction") == direction
                       and item.get("status") == "WAIT_BREAKOUT_CONFIRMATION"), None)
    if state in {"EXPIRED", "SETUP_EXPIRED", "INVALIDATED", "PULLBACK_INVALIDATED"}:
        lifecycle_text = ("原本判斷已失效，等待新的機會" if state == "INVALIDATED"
                          else "原本的進場條件已失效，已重新計算")
        lines = ["🔄【進場條件已更新】",
                 plain_trade_status(state),
                 lifecycle_text,
                 f"舊條件：15 分鐘 K 棒收盤{verb} {trigger:.2f}，現在已不再使用。"]
        if next_setup:
            next_trigger = float(next_setup.get("breakoutTrigger") or 0)
            lines.append(f"新條件：15 分鐘 K 棒收盤{verb} {next_trigger:.2f}。")
        else:
            lines.append("新條件：市場結構正在重新計算，形成後會再通知。")
        lines.extend(["原因：市場結構已改變，舊條件不再適用。",
                      "下一步：等待新的突破或回踩機會，現在不要追價。",
                      f"資料時間：{_local_time(str(event.get('calculatedAt') or ''))}（UTC+8）"])
        return "\n".join(lines)
    side_word = "買" if direction == "LONG" else "賣"
    cancel_word = "跌破" if direction == "LONG" else "站上"
    lines = ["🟡【XAUUSD｜現在先不要進】", f"現價：{current:.2f}"]
    pullback_low = setup.get("pullbackEntryZoneLow")
    pullback_high = setup.get("pullbackEntryZoneHigh")
    pullback_zone = (f"{float(pullback_low):.2f}–{float(pullback_high):.2f}"
                     if isinstance(pullback_low, (int, float)) and isinstance(pullback_high, (int, float))
                     else "尚未形成有效重疊區")
    if state == "PULLBACK_BREACH_PENDING_CLOSE":
        lines.extend([
            "⚠️ 回踩防守位盤中已失守，暫停進場。",
            "等15M收盤；收破就取消，重新站回才再評估。",
        ])
    elif state in {"WAIT_RETEST", "WAIT_PULLBACK_CONFIRMATION"}:
        lines.extend([
            f"↩️ 回踩{side_word}",
            f"價格已到 {retest}，先不要進，等15M確認止跌。",
        ])
    else:
        lines.extend([
            f"🚀 突破{side_word}",
            f"等15M收盤{verb} {trigger:.2f}",
            f"可接受進場：{breakout_zone}",
            (f"超過 {max_chase:.2f} 不追" if max_chase else "離進場區太遠就不追"),
            f"↩️ 回踩{side_word}",
            (f"回到 {pullback_zone} 後，等15M確認止跌"
             if isinstance(pullback_low, (int, float)) and isinstance(pullback_high, (int, float))
             else ("目前還沒有明確的回踩買點，先等系統找到新的支撐區。"
                   if direction == "LONG"
                   else "目前還沒有明確的反彈賣點，先等系統找到新的壓力區。")),
        ])
    lines.extend([
        f"❌ {'多單' if direction == 'LONG' else '空單'}取消：15M收盤{cancel_word} {float(setup.get('stopPrice') or 0):.2f}",
        "有新機會時我會再通知。",
    ])
    return "\n".join(lines)


def _format_position_event(event: dict, position: dict) -> str:
    side = "多單" if position.get("side") == "LONG" else "空單"
    conditional = f"若你持有{side}"
    event_type = str(position.get("event_type") or "")
    titles = {
        "TAKE_PROFIT_1": f"🟢【{side}第一止盈觸發】",
        "TAKE_PROFIT_2": f"🟢【{side}第二止盈觸發】",
        "TAKE_PROFIT_3": f"🟢【{side}第三止盈觸發｜啟動移動止盈】",
        "EARLY_EXIT": f"🟠【{side}動能轉弱｜建議減倉或退出】",
        "TRAILING_STOP_UPDATE": f"🔵【{side}移動防守更新】",
        "STOP_TRIGGERED": f"🔴【{side}防守條件已觸發】",
        "STRUCTURE_INVALIDATED": f"🔴【{side}結構失效】",
    }
    price = float(position.get("price") or event.get("currentPrice") or 0)
    protection = position.get("newProtectionPrice")
    target = position.get("targetPrice")
    next_level = position.get("nextLevel")
    lines = [titles.get(event_type, f"🔵【{side}持倉管理】"), f"現價：{price:.2f}"]
    protection_text = (f"{float(protection):.2f}"
                       if isinstance(protection, (int, float)) else "—")
    if event_type.startswith("TAKE_PROFIT"):
        lines.extend([
            f"觸發價：{float(target):.2f}" if isinstance(target, (int, float)) else "觸發價：—",
            f"{conditional}：建議平倉 {position.get('percent', 0)}%",
            f"剩餘部位防守調整至：{protection_text}",
            (f"下一目標：{float(next_level):.2f}" if isinstance(next_level, (int, float))
             else "下一步：剩餘 40% 採 15M 結構移動止盈"),
        ])
    elif event_type == "EARLY_EXIT":
        lines.extend([
            f"觸發原因：{position.get('earlyExitCondition')}",
            f"最新15M收盤：{float(position['closedPrice']):.2f}",
            f"{conditional}：建議減倉或退出剩餘部位",
            f"剩餘部位防守價：{protection_text}",
        ])
    elif event_type in ("STOP_TRIGGERED", "STRUCTURE_INVALIDATED"):
        lines.extend([
            f"防守價：{protection_text}",
            f"{conditional}：依風控規則退出",
            "這是防守／停損訊號，不是止盈訊號",
        ])
    else:
        lines.extend([
            f"新的追蹤防守價：{protection_text}",
            f"{conditional}：依更新後防守價管理剩餘部位",
        ])
    lines.append(f"資料時間：{_local_time(str(event.get('calculatedAt') or ''))}（UTC+8）")
    return "\n".join(lines)


def _format_management_plan(event: dict, plan: dict) -> str:
    side = "多單" if plan.get("direction") == "LONG" else "空單"
    completed = set(plan.get("completedEvents") or [])
    next_target = ("TP1" if "TAKE_PROFIT_1" not in completed else
                   "TP2" if "TAKE_PROFIT_2" not in completed else "TP3／移動止盈")
    return "\n".join([
        f"🔵【{side}持倉管理｜下一目標{next_target}】",
        f"若你持有{side}，可依下列條件管理；系統不假設你已經進場。",
        f"現價：{float(event.get('currentPrice') or 0):.2f}",
        f"參考進場區：{float(plan['entryZoneLow']):.2f}–{float(plan['entryZoneHigh']):.2f}",
        f"目前浮盈／風險倍數：{float(plan.get('currentR') or 0):.2f}R",
        f"TP1：{float(plan['tp1Price']):.2f}，觸發後建議平倉30%",
        f"TP2：{float(plan['tp2Price']):.2f}，觸發後建議再平倉30%",
        f"TP3：{float(plan['tp3Price']):.2f}，剩餘40%移動止盈",
        f"目前防守價：{float(plan['trailingStopPrice']):.2f}",
        f"提前退出條件：{plan['earlyExitCondition']}",
        f"下一觸發：{next_target}",
        f"資料時間：{_local_time(str(event.get('calculatedAt') or ''))}（UTC+8）",
    ])


_USER_MESSAGE_BUILDER = UserFacingTradeMessageBuilder()


def format_decision_message(event: dict) -> str:
    """The only public Telegram/web trade-message rendering gateway."""
    return _USER_MESSAGE_BUILDER.build(event, _format_decision_message_legacy)
