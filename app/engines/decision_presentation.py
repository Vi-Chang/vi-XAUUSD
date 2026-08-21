"""One user-facing vocabulary for the web panel and Telegram."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.engines.confidence import confidence_label, normalize_signal_score

TAIPEI = ZoneInfo("Asia/Taipei")


def plain_trade_status(state: str, *, can_enter: bool = False) -> str:
    """Translate engine lifecycle states into an unambiguous user action."""
    value = str(state or "WAIT")
    if can_enter or value.endswith("READY") or value.startswith("ENTRY_READY_"):
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
        "SHORT_TERM_WEAK_HTF_BULLISH": "⚠️【短線轉弱，先觀望】",
    }
    if state.endswith("BIAS"):
        action = "等待，尚未到進場區，請勿追價。"
        tone = "warning"
    elif state.endswith("WATCH"):
        action = "等待，尚不可進場，請勿追價。"
        tone = "warning"
    elif state.endswith("READY"):
        action = "進場條件已成立。"
        tone = "long_ready" if state == "LONG_READY" else "short_ready"
    elif state.endswith("MANAGE"):
        action = "依最新防守價與分批止盈計畫管理。"
        tone = "manage"
    elif state == "DATA_STALE":
        action, tone = "資料過期，暫停交易。", "danger"
    else:
        action, tone = str(event.get("flatAction") or "等待新的確認條件。"), "neutral"
    return {
        "title": titles.get(state, "⚪【市場決策更新】"),
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


def format_decision_message(event: dict) -> str:
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
    if state == "SHORT_TERM_WEAK_HTF_BULLISH":
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
    if state.endswith("READY"):
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


def _format_decision_assistant(event: dict, assistant: dict) -> str:
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
        f"劇本：{names.get(setup.get('type'), setup.get('type'))}",
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
    direction = str(setup.get("direction") or "LONG")
    trigger = float(setup.get("breakoutTrigger") or 0)
    zone = f"{float(setup.get('entryZoneLow') or 0):.2f}–{float(setup.get('entryZoneHigh') or 0):.2f}"
    retest = f"{float(setup.get('retestZoneLow') or 0):.2f}–{float(setup.get('retestZoneHigh') or 0):.2f}"
    if state in {"ENTRY_READY_BREAKOUT", "ENTRY_READY_RETEST",
                 "BREAKOUT_ENTRY_READY", "PULLBACK_ENTRY_READY"}:
        entry_type = ("突破進場" if state in {"ENTRY_READY_BREAKOUT", "BREAKOUT_ENTRY_READY"}
                      else "回踩進場")
        if entry_type == "回踩進場":
            zone = (f"{float(setup.get('pullbackEntryZoneLow') or setup.get('entryZoneLow') or 0):.2f}–"
                    f"{float(setup.get('pullbackEntryZoneHigh') or setup.get('entryZoneHigh') or 0):.2f}")
        risk = abs(float(setup.get("entryZoneHigh") or trigger) -
                   float(setup.get("stopPrice") or trigger))
        reward = abs(float(setup.get("tp1") or trigger) -
                     float(setup.get("entryZoneHigh") or trigger))
        rr = reward / risk if risk else 0
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
    if event_type.startswith("TAKE_PROFIT"):
        lines.extend([
            f"觸發價：{float(target):.2f}" if isinstance(target, (int, float)) else "觸發價：—",
            f"{conditional}：建議平倉 {position.get('percent', 0)}%",
            f"剩餘部位防守調整至：{float(protection):.2f}",
            (f"下一目標：{float(next_level):.2f}" if isinstance(next_level, (int, float))
             else "下一步：剩餘 40% 採 15M 結構移動止盈"),
        ])
    elif event_type == "EARLY_EXIT":
        lines.extend([
            f"觸發原因：{position.get('earlyExitCondition')}",
            f"最新15M收盤：{float(position['closedPrice']):.2f}",
            f"{conditional}：建議減倉或退出剩餘部位",
            f"剩餘部位防守價：{float(protection):.2f}",
        ])
    elif event_type in ("STOP_TRIGGERED", "STRUCTURE_INVALIDATED"):
        lines.extend([
            f"防守價：{float(protection):.2f}",
            f"{conditional}：依風控規則退出",
            "這是防守／停損訊號，不是止盈訊號",
        ])
    else:
        lines.extend([
            f"新的追蹤防守價：{float(protection):.2f}",
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
