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

# 公開允許的頂層欄位(白名單)。決策另由 market_decision 映射為 decision。
PUBLIC_ALLOWLIST: tuple[str, ...] = (
    "version", "snapshot_ts", "timestamp_utc", "timestamp_taipei", "symbol",
    "current_price", "data_quality", "event_risk", "cross_market_context",
    "market_state", "timeframes", "key_levels",
    "long_scenario", "short_scenario",
    "bias_analysis", "ai_strategy",
    "summary_zh_tw", "most_likely_user_mistake_now",
    "freshness",
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


def public_analysis(full: dict) -> dict:
    """把完整分析 dict 投影為公開 payload(allowlist)。輸入不被就地修改。"""
    if not isinstance(full, dict):
        return {"available": False}
    out: dict = {k: full[k] for k in PUBLIC_ALLOWLIST if k in full}
    # 決策:一律使用市場層 market_decision(未被持倉 MANAGE 覆寫);缺則退回安全空決策
    md = full.get("market_decision")
    out["decision"] = md if isinstance(md, dict) else {
        "action": "NO_TRADE", "confidence_grade": "X", "evidence_score": 0, "reason": ""}
    out["meta"] = _public_meta(full.get("meta"))
    out["public"] = True     # 明確標記此為公開投影(前端可據此顯示登入提示)
    return out


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
