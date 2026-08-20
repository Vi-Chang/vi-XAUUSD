from app.engines.virtual_profit_tracker import evaluate_virtual_profit

ENTRY = {
    "status": "ENTRY_TRIGGERED",
    "setup_id": "case-4494",
    "direction": "LONG",
    "suggested_entry": 4494.77,
    "stop_loss": 4490.27,
}


def evaluate(price, previous=None, closed=None, protection=None):
    return evaluate_virtual_profit(
        ENTRY,
        previous,
        current_price=price,
        closed_price=closed,
        latest_structure_protection=protection,
        candle_close_time="2026-08-20T13:00:00+00:00",
    )


def test_exact_r_targets_for_4494_case():
    state, events = evaluate(4494.77)
    assert events == []
    assert (state["tp1"], state["tp2"], state["tp3"]) == (4499.27, 4503.77, 4508.27)


def test_rallies_to_4527_emits_tp1_tp2_tp3_once():
    state, _ = evaluate(4494.77)
    all_events = []
    for price in (4499.27, 4503.77, 4508.27, 4527):
        state, events = evaluate(price, state)
        all_events.extend(events)
    assert [e["event_type"] for e in all_events] == ["TP1", "TP2", "TP3"]
    assert all("若你有在 4494.77 附近進場" in e["message"] for e in all_events)


def test_trailing_protection_updates_then_exits_remaining_profit():
    state, _ = evaluate(4527)
    state, _ = evaluate(4528, state, protection=4518)
    state, events = evaluate(4517, state, closed=4517, protection=4518)
    assert events[-1]["event_type"] == "TRAILING_EXIT"
    assert state["active"] is False
