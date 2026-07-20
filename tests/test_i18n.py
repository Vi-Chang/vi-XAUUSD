"""白話化 i18n:確保每個狀態/動作代碼都有對應中文,且無殘留規則編號。"""
from app import i18n
from app.engines.market_state import STATES


def test_all_market_states_have_zh():
    for code in STATES:
        assert code in i18n.MARKET_STATE_ZH, f"缺少市場狀態白話: {code}"
        assert i18n.MARKET_STATE_ZH[code] != code


def test_all_actions_have_zh():
    actions = ("NO_TRADE", "WATCH", "PREPARE_LONG", "PREPARE_SHORT",
               "LONG", "SHORT", "MANAGE", "EXIT")
    for a in actions:
        assert a in i18n.ACTION_ZH


def test_no_spec_or_oldproblem_refs_in_user_text():
    """使用者文字不得出現「spec X」「老問題 X」規則編號。"""
    from app.services.analysis_service import MISTAKE_BY_STATE
    for text in MISTAKE_BY_STATE.values():
        assert "spec" not in text.lower()
        assert "老問題" not in text


def test_dir_zh():
    assert i18n.dir_zh("LONG") == "做多"
    assert i18n.dir_zh("SHORT") == "做空"
