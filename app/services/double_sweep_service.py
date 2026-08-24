"""Runtime persistence and shadow-mode projection for DOUBLE_SWEEP events."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from app.db.models import DoubleSweepRecord
from app.db.session import db_session
from app.engines.double_sweep import detect_double_sweeps, edge_lifecycle

PROFILE_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "double_sweep_profile.json"
)


def load_profile(order: str, path: Path = PROFILE_PATH) -> dict:
    if not path.exists():
        return {
            "sampleSize": 0,
            "sampleConfidence": "LOW_CONFIDENCE",
            "confidenceWeight": 0.0,
            "directionalBias": "NONE",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return dict((payload.get("profiles") or {}).get(order) or {})
    except (OSError, ValueError, TypeError):
        return {
            "sampleSize": 0,
            "sampleConfidence": "LOW_CONFIDENCE",
            "confidenceWeight": 0.0,
            "directionalBias": "NONE",
        }


def persist_event(event: dict, profile: dict) -> bool:
    """Insert once. Existing immutable event rows are never overwritten."""
    with db_session() as db:
        existing = db.execute(
            select(DoubleSweepRecord).where(
                DoubleSweepRecord.event_id == event["eventId"]
            )
        ).scalar_one_or_none()
        if existing:
            return False
        db.add(
            DoubleSweepRecord(
                event_id=event["eventId"],
                symbol=event["symbol"],
                sweep_order=event["order"],
                confirmed_at=pd.Timestamp(event["confirmedAt"]).to_pydatetime(),
                reference_high=float(event["referenceHigh"]),
                reference_low=float(event["referenceLow"]),
                reference_atr=float(event["referenceAtr"]),
                detection_version=event["detectionVersion"],
                event_payload=event,
                profile_snapshot=profile,
                created_at=datetime.now(timezone.utc),
            )
        )
    return True


def evaluate_double_sweep_monitor(
    candles: pd.DataFrame | None,
    *,
    symbol: str,
    current_price: float,
    regime4h: str,
    structure1h: str,
    macro_context: str,
    now: datetime,
    previous: dict | None = None,
) -> tuple[dict, list[dict]]:
    if candles is None or candles.empty:
        return {"status": "NO_EVENT", "message": "尚無足夠的15分鐘收盤資料"}, []
    events = detect_double_sweeps(
        candles,
        symbol=symbol,
        regime4h=regime4h,
        structure1h=structure1h,
        macro_context=macro_context,
    )
    if not events:
        return {"status": "NO_EVENT", "message": "目前沒有完成的雙邊掃價事件"}, []
    event = events[-1].to_dict()
    profile = load_profile(event["order"])
    lifecycle = edge_lifecycle(event, profile, now=now, current_price=current_price)
    inserted = persist_event(event, profile)
    state = {
        "status": lifecycle["edgeStatus"],
        "event": event,
        "profile": profile,
        "lifecycle": lifecycle,
        "shadowMode": True,
        "strategyEffect": "CONTEXT_ONLY"
        if profile.get("sampleSize", 0) >= 20
        else "NONE",
        "message": (
            "歷史樣本不足，僅記錄不影響交易決策"
            if profile.get("sampleSize", 0) < 20
            else "統計結果僅調整背景信心，不會建立進場或改寫風控價"
        ),
    }
    prior = previous or {}
    notifications = []
    if inserted:
        notifications.append(
            {
                "event_type": "DOUBLE_SWEEP_CONFIRMED",
                "eventId": event["eventId"],
                "currentState": lifecycle["edgeStatus"],
                "doubleSweepEvent": state,
                "direction": profile.get("directionalBias", "NONE"),
                "candleCloseTime": event["confirmedAt"],
                "transitionReason": f"完成{event['order']}雙邊掃價並收回參考區間",
            }
        )
    old_status = prior.get("status")
    if old_status not in {"DECAYING", "EXHAUSTED", "EXPIRED"} and lifecycle[
        "edgeStatus"
    ] in {"DECAYING", "EXHAUSTED", "EXPIRED"}:
        notifications.append(
            {
                "event_type": "DOUBLE_SWEEP_EDGE_CONSUMED",
                "eventId": event["eventId"],
                "currentState": lifecycle["edgeStatus"],
                "doubleSweepEvent": state,
                "direction": profile.get("directionalBias", "NONE"),
                "candleCloseTime": event["confirmedAt"],
                "transitionReason": "雙邊掃價的歷史統計優勢已衰減或過期",
            }
        )
    return state, notifications
