from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db.models import AnalysisRun
from app.db.session import db_session, init_db
from app.engines.confidence import (
    GRADING_VERSION,
    confidence_label,
    get_confidence_grade,
)
from app.main import app
from app.services.confidence_history import backfill_confidence_history
from app.services.decision_outbox import format_telegram_event


def test_canonical_grade_boundaries_ignore_trade_status():
    expected = {
        75: "B", 70: "B", 60: "C", 45: "C", 20: "D", 80: "A", None: "U"
    }
    for score, grade in expected.items():
        assert get_confidence_grade(score) == grade
    assert confidence_label(None) == "未評級"


def _legacy(run_id: int, score: int, *, status: str, reason: str) -> AnalysisRun:
    return AnalysisRun(
        id=run_id,
        run_time=datetime(2099, 8, 21, tzinfo=timezone.utc),
        trigger=f"CONFIDENCE-BACKFILL-{run_id}", market_state="RANGE",
        decision_action="WATCH", confidence_grade="C", evidence_score=score,
        signal_score=None, grading_version="", trade_status=status,
        can_enter=False, blocked_reason=reason, data_quality_status="GOOD",
        result_json={
            "decision": {"action": "WATCH", "evidence_score": score,
                         "reason": reason},
            "decision_trace": {"blockingReasons": (
                ["RISK_REWARD_TOO_LOW"] if status == "BLOCKED_RR" else [])},
        },
        prompt_version="test", strategy_version="test", model_version="test",
    )


def test_history_backfill_regrades_scores_and_preserves_trade_block():
    init_db()
    ids = (910075, 910070, 910020)
    with db_session() as db:
        db.execute(delete(AnalysisRun).where(AnalysisRun.id.in_(ids)))
        db.add_all([
            _legacy(ids[0], 75, status="WAIT_CONFIRMATION", reason="等待確認"),
            _legacy(ids[1], 70, status="BLOCKED_RR", reason="賺賠比未達門檻"),
            _legacy(ids[2], 20, status="WAIT_CONFIRMATION", reason="等待確認"),
        ])
    assert backfill_confidence_history() >= 3
    with db_session() as db:
        rows = db.execute(select(AnalysisRun).where(
            AnalysisRun.id.in_(ids)).order_by(AnalysisRun.id.desc())).scalars().all()
        by_score = {row.signal_score: row for row in rows}
        assert by_score[75].confidence_grade == "B"
        assert by_score[70].confidence_grade == "B"
        assert by_score[70].trade_status == "BLOCKED_RR"
        assert by_score[70].can_enter is False
        assert by_score[20].confidence_grade == "D"
        assert all(row.grading_version == GRADING_VERSION for row in rows)
        assert all(row.result_json["decision"]["evidence_score"] == row.evidence_score
                   for row in rows)

    response = TestClient(app).get("/api/analysis/history?limit=100")
    assert response.status_code == 200
    history = {row["signal_score"]: row for row in response.json()
               if row["trigger"].startswith("CONFIDENCE-BACKFILL-")}
    assert history[75]["grade"] == "B"
    assert history[70]["grade"] == "B"
    assert history[70]["trade_status"] == "BLOCKED_RR"
    assert history[70]["can_enter"] is False
    assert history[20]["grade"] == "D"


def test_frontend_has_no_fixed_c_fallback_and_displays_trade_permission():
    root = Path(__file__).parents[1]
    messages = (root / "app/static/js/messages.js").read_text(encoding="utf-8")
    app_js = (root / "app/static/js/app.js").read_text(encoding="utf-8")
    assert 'D: "D級（低信心）"' in messages
    assert 'U: "未評級"' in messages
    assert 'B: "B級（中高信心）"' in messages
    assert "r.can_enter ? \"可以考慮進場\" : \"尚不可進場\"" in app_js
    assert "r.signal_score == null ? \"未取得\" : r.signal_score" in app_js
    assert "signalScore == null ? \"未取得\" : signalScore" in app_js


def test_telegram_keeps_b_grade_when_rr_blocks_entry():
    message = format_telegram_event({
        "currentState": "LONG_WATCH", "direction": "LONG",
        "currentPrice": 4529.96, "latestClosedCandlePrice": 4528,
        "candleCloseTime": "2026-08-21T01:15:00+00:00",
        "calculatedAt": "2026-08-21T01:16:00+00:00",
        "signalScore": 70, "confidenceGrade": "B",
        "tradeStatus": "BLOCKED_RR", "canEnter": False,
        "blockedReason": "賺賠比未達門檻",
        "confirmation": "等待更好的進場位置",
    })
    assert "訊號信心：B級（中高信心）（70）" in message
    assert "原因：賺賠比未達門檻" in message
    assert "BLOCKED_RR" not in message
    assert "現在先不要進場" in message
    assert "條件成立後：系統會重新檢查進場區" in message
