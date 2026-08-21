"""公開分析 payload 的單一集中投影(allowlist 優先)。

原則:
- **allowlist**:公開回應只保留白名單頂層欄位;未列出的一律不公開(未來新增欄位預設私密)。
- 私人欄位(帳戶/持倉/PnL/手數/老師 note/紀律)絕不進入公開 payload。
- 決策以 `market_decision`(未被「持倉管理 MANAGE 覆寫」污染的市場層決策)呈現為公開 `decision`。
- 公開視圖使用「分析原始價位」(不套用個人券商 offset;offset 屬私人校正,不公開)。
- AI 文字為市場分析(生成時未餵入個人持倉/老師資料,見 analysis_service),故可公開。

此模組為唯一的公開序列化器;所有公開端點(GET /api/analysis/latest、公開 WS)一律經此。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 隱私邊界版本:只有此版(position-free / public-safe pipeline)產生的分析才可公開自由文字。
# 舊資料(缺此戳記或戳記不符)一律不公開 AI/summary/mistake 等自由文字 → fail-closed。
#
# 語意:此戳記代表「公開自由文字的資料來源與 AI snapshot 已通過隱私審查」。
# ⚠️ 升版規則(下列任一改變都必須重新審查隱私邊界並 +1,舊戳記資料即自動不公開):
#   1. AI snapshot / input fields(app/llm/snapshot.py 或餵給 generate_ai_strategy 的欄位)
#   2. public allowlist(PUBLIC_ALLOWLIST)
#   3. market_decision 生成位置/時機(analysis_service 的捕捉點)
#   4. summary_zh_tw / most_likely_user_mistake_now 的資料來源
#   5. private/public projection 邏輯(public_analysis)
#   6. legacy fallback 政策(_unavailable / 版本閘門)
# 此常數為唯一真實來源(single source of truth):analysis_service 蓋章時 import 本常數,
# 不得各處硬編碼數字(見 tests/test_privacy_boundary.py 的 invariant test)。
PRIVACY_BOUNDARY_VERSION = 6

# 公開允許的頂層欄位(白名單)。決策另由 market_decision 映射為 decision。
PUBLIC_ALLOWLIST: tuple[str, ...] = (
    "version", "snapshot_ts", "timestamp_utc", "timestamp_taipei", "symbol",
    "current_price", "data_quality", "event_risk", "cross_market_context",
    "market_state", "timeframes", "key_levels",
    "long_scenario", "short_scenario",
    "bias_analysis", "normalized_analysis", "ai_strategy",
    "summary_zh_tw", "most_likely_user_mistake_now",
    "calibration_status", "calibration_sample_size",
    "calibration_min_sample_size", "calibration_message",
    "freshness",
    "decision_trace", "entry_engine", "directional_alert",
    "hypothetical_exit_advisor", "breakout_alert", "virtual_profit_tracker",
    "trade_plan_manager",
    "breakout_setup_manager",
    "trend_continuation_engine",
    "final_decision_state",
    "decision_snapshot",
)

# 明確禁止出現在公開 payload 的欄位名(含巢狀;供測試遞迴斷言)。
PRIVATE_KEY_DENYLIST: frozenset[str] = frozenset({
    "position_management", "mentor_comparison", "trading_coach", "risk_manager",
    "account", "account_id", "account_name", "accounts",
    "lot_size", "pnl", "pnl_usd", "net_usd", "swap_usd", "unrealized_pnl",
    "drawdown", "max_drawdown_r", "behavior_flags", "corrective_action",
    "stop_modification_history", "partial_exit_history", "planned_targets",
    "offset_info",   # 個人券商價格校正(揭露券商與掛單價差)→ 不公開
    "note",          # 老師私人備註
})


def _public_meta(meta: dict | None) -> dict:
    """公開 meta:只保留版本資訊,移除內部成本(llm_cost_usd_today)。"""
    meta = meta or {}
    return {
        "prompt_version": meta.get("prompt_version", ""),
        "strategy_version": meta.get("strategy_version", ""),
        "model_version": meta.get("model_version", ""),
    }


def _unavailable(reason: str = "analysis_refresh_required") -> dict:
    """安全的 unavailable envelope(不含任何原始 payload)。"""
    return {"public": True, "available": False, "reason": reason}


def public_analysis(full: dict) -> dict:
    """把完整分析 dict 投影為公開 payload(allowlist + 版本閘門 + fail-closed)。

    fail-closed:缺版本戳記 / 缺 market_decision / 型別錯誤 / 投影例外 / 殘留私人 key,
    一律回安全 unavailable envelope,**絕不** fallback 回傳原始 full payload。
    log 只含錯誤類別與 version/id,不記錄完整 payload。
    """
    ver = full.get("privacy_boundary_version") if isinstance(full, dict) else None
    vid = full.get("version") if isinstance(full, dict) else None
    try:
        if not isinstance(full, dict):
            return _unavailable("unavailable")
        from app.config import get_settings
        from app.engines.normalized_analysis import validate_api_payload
        full = validate_api_payload(full, strict=get_settings().app_env == "development")
        # 版本閘門:舊資料(戳記缺失/不符)不得公開自由文字(text-level leakage 防線)
        if int(ver or 0) != PRIVACY_BOUNDARY_VERSION:
            return _unavailable("analysis_refresh_required")
        # 市場層決策必須存在;不得 fallback 用可能被持倉污染的舊 decision
        md = full.get("market_decision")
        if not isinstance(md, dict):
            logger.error("public projection: missing market_decision (ver=%s id=%s)", ver, vid)
            return _unavailable("analysis_refresh_required")

        out: dict = {k: full[k] for k in PUBLIC_ALLOWLIST if k in full}
        out["decision"] = md
        out["meta"] = _public_meta(full.get("meta"))
        out["public"] = True
        out["available"] = True

        # 防禦性:確認公開 payload 無私人 key(allowlist 理論上已保證;仍雙重把關)
        leaked = assert_no_private_keys(out)
        if leaked:
            logger.error("public projection leaked keys=%s (id=%s)", leaked, vid)
            return _unavailable("unavailable")
        return out
    except Exception as exc:  # noqa: BLE001 — 任何例外一律 fail-closed,不外洩原始資料
        logger.error("public projection failed: %s (ver=%s id=%s)",
                     type(exc).__name__, ver, vid)
        return _unavailable("unavailable")


def collect_keys(obj) -> set[str]:
    """遞迴收集 dict 內所有 key(供隱私斷言:確認公開 payload 無私人 key)。"""
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(k)
            found |= collect_keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found |= collect_keys(v)
    return found


def assert_no_private_keys(public_payload: dict) -> list[str]:
    """回傳公開 payload 中出現的私人 key(空 = 安全)。用於測試與防禦性檢查。"""
    keys = collect_keys(public_payload)
    return sorted(keys & PRIVATE_KEY_DENYLIST)
