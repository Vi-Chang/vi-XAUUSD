from __future__ import annotations

from app.db.models import CurrentFinalDecision
from app.engines.canonical_conflict_resolver import (
    build_canonical_market_snapshot,
    engine_result_envelope,
    resolve_canonical_conflict,
)
from app.engines.decision_presentation import format_decision_message
from app.services.decision_outbox import _canonicalize_payload


def market(*, health: str = "GOOD", close15: str = "2026-08-26T01:15:00Z",
           close1h: str = "2026-08-26T01:00:00Z") -> dict:
    return {
        "symbol": "XAUUSD", "version": 42,
        "timestamp_utc": "2026-08-26T01:16:00Z",
        "normalized_analysis": {
            "currentPrice": 4640.0, "marketDataStatus": health,
            "marketDataTimestamp": "2026-08-26T01:16:00Z",
        },
        "closed_candles": {
            "15M": {"available": bool(close15), "close_time": close15},
            "1H": {"available": bool(close1h), "close_time": close1h},
            "4H": {"available": True, "close_time": "2026-08-26T00:00:00Z"},
        },
    }


def decision(*, b15: str = "BULLISH", b1h: str = "BULLISH",
             b4h: str = "BULLISH", structural: str = "BULLISH",
             live: str = "STRONG_LONG", execution: str = "LONG",
             long_score: int = 70, short_score: int = 35) -> dict:
    return {
        "decisionVersion": 7, "marketBias": structural,
        "structuralBias": structural, "liveMomentum": live,
        "liveBiasState": "ALIGNED", "executionBias": execution,
        "executionAllowed": True, "canEnter": False,
        "multiTimeframeBias": {"bias15m": b15, "bias1h": b1h,
                               "bias4h": b4h, "bias1d": "BULLISH"},
        "longScore": long_score, "shortScore": short_score,
        "newEntryDecision": {"action": "WAIT", "canEnter": False,
                             "tradeStatus": "WAIT_CONFIRMATION"},
    }


def test_a_timeframe_divergence_is_not_true_conflict():
    snapshot = build_canonical_market_snapshot(market())
    result = resolve_canonical_conflict(
        decision(b15="BULLISH", b1h="BEARISH", b4h="BEARISH"),
        market_snapshot=snapshot)
    assert result["conflictType"] == "TIMEFRAME_DIVERGENCE"
    assert result["tradePermission"] != "BLOCKED_SYSTEM"


def test_b_structural_and_live_opposition_is_bias_transition():
    snapshot = build_canonical_market_snapshot(market())
    source = decision(structural="BEARISH", live="STRONG_LONG", execution="NEUTRAL")
    result = resolve_canonical_conflict(source, market_snapshot=snapshot)
    assert result["conflictType"] == "BIAS_TRANSITION"
    assert result["conflictType"] != "TRUE_ENGINE_CONFLICT"


def test_c_old_engine_snapshot_is_discarded():
    snapshot = build_canonical_market_snapshot(market())
    old = {**snapshot, "snapshotId": "CMS-OLD"}
    engines = [engine_result_envelope("volume_engine", {}, old)]
    result = resolve_canonical_conflict(
        decision(), market_snapshot=snapshot, engine_results=engines)
    assert result["conflictType"] == "STALE_ENGINE_RESULT"
    assert result["conflictReasonTrace"]["discardedEngineResults"]


def test_d_missing_15m_is_data_condition_not_engine_conflict():
    snapshot = build_canonical_market_snapshot(market(health="STALE", close15=""))
    result = resolve_canonical_conflict(decision(), market_snapshot=snapshot)
    assert result["conflictType"] == "DATA_DEGRADED_CONDITION"
    assert result["tradePermission"] == "BLOCKED_DATA"
    assert result["canEnter"] is False


def test_divergence_and_stale_data_keep_separate_dimensions():
    snapshot = build_canonical_market_snapshot(market(health="STALE", close15=""))
    result = resolve_canonical_conflict(
        decision(b15="BULLISH", b1h="BEARISH", b4h="BEARISH"),
        market_snapshot=snapshot)
    assert result["conflictType"] == "TIMEFRAME_DIVERGENCE"
    assert result["canonicalDataHealthState"] == "DATA_STALE"
    assert result["tradePermission"] == "BLOCKED_DATA"


def test_e_near_tie_becomes_neutral_watch():
    snapshot = build_canonical_market_snapshot(market())
    result = resolve_canonical_conflict(
        decision(long_score=62, short_score=61), market_snapshot=snapshot)
    assert result["conflictType"] == "SCORE_NEAR_TIE"
    assert result["executionBias"] == "NEUTRAL"
    assert result["newEntryDecision"]["canEnter"] is False


def test_f_only_same_snapshot_opposite_resolvers_are_true_conflict():
    snapshot = build_canonical_market_snapshot(market())
    source = decision(execution="NEUTRAL")
    source["resolverOutputs"] = ["LONG", "SHORT"]
    result = resolve_canonical_conflict(source, market_snapshot=snapshot)
    assert result["conflictType"] == "TRUE_ENGINE_CONFLICT"
    assert result["tradePermission"] == "BLOCKED_SYSTEM"
    assert result["conflictReasonTrace"]["recomputeAttempted"] is True


def test_g_stale_telegram_version_reloads_latest_canonical_snapshot():
    canonical = decision(execution="SHORT", structural="BEARISH")
    canonical.update({"decisionVersion": 201, "conflictType": "NO_CONFLICT",
                      "snapshotId": "CMS-201"})
    row = CurrentFinalDecision(
        symbol="XAUUSD", decision_id="D-201", decision_version=201,
        decision_signature="sig", action="WAIT", direction="BEARISH",
        payload={"canonicalDecision": canonical, **canonical})
    payload = _canonicalize_payload({
        "decisionVersion": 200, "canonicalStateVersion": 200,
        "executionBias": "LONG", "canonicalDecision": {"executionBias": "LONG"}}, row)
    assert payload["canonicalStateVersion"] == 201
    assert payload["executionBias"] == "SHORT"
    assert payload["consumerStateReloaded"] is True


def test_h_stale_data_preserves_last_confirmed_bias_but_blocks_entry():
    snapshot = build_canonical_market_snapshot(market(health="STALE", close15=""))
    result = resolve_canonical_conflict(
        decision(execution="NEUTRAL"), market_snapshot=snapshot,
        previous={"executionBias": "LONG"})
    assert result["lastConfirmedBias"] == "LONG"
    assert result["executionAllowed"] is False


def test_i_new_closed_candle_creates_new_snapshot_identity():
    first = build_canonical_market_snapshot(market())
    second = build_canonical_market_snapshot(market(close15="2026-08-26T01:30:00Z"))
    assert first["snapshotId"] != second["snapshotId"]
    assert second["lastClosed15mId"].endswith("01:30:00Z")


def test_j_same_hourly_close_uses_one_consolidated_snapshot():
    data = market(close15="2026-08-26T02:00:00Z",
                  close1h="2026-08-26T02:00:00Z")
    snapshot = build_canonical_market_snapshot(data)
    assert snapshot["lastClosed15mTime"] == snapshot["lastClosed1hTime"]
    assert snapshot["snapshotCompleteness"] == "COMPLETE"


def test_stale_nested_setup_projection_is_recomputed_not_true_conflict():
    snapshot = build_canonical_market_snapshot(market())
    source = decision()
    source.update({
        "activeSetupId": "NEW", "engineSelectedSetupId": "OLD",
        "newEntryDecision": {"action": "WAIT", "canEnter": False,
                             "tradeStatus": "WAIT_CONFIRMATION",
                             "selectedSetup": {"setupId": "NEW"}},
    })
    result = resolve_canonical_conflict(
        source, market_snapshot=snapshot,
        consistency_errors=["ENGINE_CANONICAL_SETUP_CONFLICT"])
    assert result["engineSelectedSetupId"] == "NEW"
    assert result["conflictType"] == "NO_CONFLICT"


def test_data_version_mismatch_is_discarded_separately():
    snapshot = build_canonical_market_snapshot(market())
    envelope = engine_result_envelope("pattern_engine", {}, snapshot)
    envelope["marketStateVersion"] = int(snapshot["dataVersion"]) - 1
    result = resolve_canonical_conflict(
        decision(), market_snapshot=snapshot, engine_results=[envelope])
    assert result["conflictType"] == "DATA_VERSION_MISMATCH"
    assert result["conflictReasonTrace"]["discardedEngineResults"]


def test_conflict_telegram_is_plain_chinese_without_internal_code():
    message = format_decision_message({
        "event_type": "TIMEFRAME_DIVERGENCE",
        "timeframeState": {"15M": "BULLISH", "1H": "BEARISH", "4H": "BEARISH"},
        "structuralBias": "BEARISH",
    })
    assert "多空週期分歧" in message
    assert "TIMEFRAME_DIVERGENCE" not in message
    assert "ENGINE_CANONICAL_CONFLICT" not in message
