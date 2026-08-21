"""One user-facing vocabulary for the web panel and Telegram."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.engines.confidence import confidence_label, normalize_signal_score

TAIPEI = ZoneInfo("Asia/Taipei")


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
    view = event.get("presentation") or build_decision_presentation(event)
    price = float(event.get("currentPrice") or 0)
    closed = event.get("latestClosedCandlePrice")
    closed_price = f"{float(closed):.2f}" if isinstance(closed, (int, float)) else "未知"
    closed_time = _local_time(str(event.get("candleCloseTime") or ""))
    data_time = _local_time(str(event.get("calculatedAt") or ""))
    state = str(event.get("currentState") or "WAIT")
    score = normalize_signal_score(event.get("signalScore"))
    score_text = str(score) if score is not None else "無有效分數"
    lines = [
        view["title"],
        f"目前動作：{view['currentAction']}",
        f"現價：{price:.2f}",
        f"訊號信心：{confidence_label(score)}（{score_text}）",
        f"交易狀態：{event.get('tradeStatus') or 'WAIT_CONFIRMATION'}",
        f"進場許可：{'可以考慮進場' if event.get('canEnter') else '尚不可進場'}",
        f"最新已收盤 15M：{closed_price}（{closed_time}，UTC+8）",
    ]
    if event.get("blockedReason"):
        lines.append(f"阻擋原因：{event['blockedReason']}")
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
            f"尚未成立：{view['missingCondition'] or event.get('flatAction') or '等待確認'}",
            f"下一個觸發：{view['nextTrigger']}",
            f"條件失效價：{view['invalidation']}",
        ])
    lines.append(f"資料時間：{data_time}（UTC+8）")
    return "\n".join(lines)
