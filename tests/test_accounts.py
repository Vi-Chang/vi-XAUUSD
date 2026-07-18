"""帳戶層(老師帶單 vs 自己交易):種子、分帳統計、對照 API。"""
import pytest

from app.db.session import init_db
from app.services import account_service as acs
from app.services import position_service as ps


@pytest.fixture(autouse=True)
def _db():
    init_db()
    yield


def _account_ids() -> tuple[int, int]:
    accounts = acs.list_accounts()
    teacher = next(a for a in accounts if a["strategy_source"] == "TEACHER")
    self_acc = next(a for a in accounts if a["strategy_source"] == "SELF")
    return teacher["id"], self_acc["id"]


def test_default_accounts_seeded():
    accounts = acs.list_accounts()
    sources = {a["strategy_source"] for a in accounts}
    assert {"TEACHER", "SELF"} <= sources


def test_position_defaults_to_self_account():
    _, self_id = _account_ids()
    pos = ps.create_position(side="LONG", entry_price=4000.0, stop_loss=3990.0,
                             lot_size=0.1)
    assert pos.account_id == self_id


def test_unknown_account_rejected():
    with pytest.raises(ValueError):
        ps.create_position(side="LONG", entry_price=4000.0, stop_loss=3990.0,
                           lot_size=0.1, account_id=99999)


def test_per_account_stats_and_comparison():
    teacher_id, self_id = _account_ids()
    base_teacher = acs.account_stats(teacher_id)["total_trades"]
    base_self = acs.account_stats(self_id)["total_trades"]

    # 老師帳戶:一筆 +2R 獲利
    p1 = ps.create_position(side="LONG", entry_price=4000.0, stop_loss=3990.0,
                            lot_size=0.1, account_id=teacher_id)
    ps.close_position(p1.id, 4020.0)
    # 自己帳戶:一筆 -1R 虧損
    p2 = ps.create_position(side="SHORT", entry_price=4000.0, stop_loss=4010.0,
                            lot_size=0.1, account_id=self_id)
    ps.close_position(p2.id, 4010.0)

    t_stats = acs.account_stats(teacher_id)
    s_stats = acs.account_stats(self_id)
    assert t_stats["total_trades"] == base_teacher + 1
    assert s_stats["total_trades"] == base_self + 1
    assert t_stats["total_pnl_usd"] >= 200.0 - 0.01  # +20 × 0.1 × 100
    assert s_stats["total_pnl_usd"] <= -100.0 + 0.01  # -10 × 0.1 × 100

    data = acs.comparison()
    assert "note" in data and "勝率" in data["note"]
    by_src = {a["strategy_source"]: a["stats"] for a in data["accounts"]}
    assert by_src["TEACHER"]["total_trades"] >= 1
    assert by_src["SELF"]["total_trades"] >= 1


def test_accounts_api():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        accounts = c.get("/api/accounts").json()
        assert len(accounts) >= 2
        teacher = next(a for a in accounts if a["strategy_source"] == "TEACHER")

        r = c.post("/api/positions", json={
            "side": "LONG", "entry_price": 4000.0, "stop_loss": 3990.0,
            "lot_size": 0.1, "account_id": teacher["id"]})
        assert r.status_code == 200
        assert r.json()["account_id"] == teacher["id"]

        # 帳戶過濾
        rows = c.get(f"/api/positions?account_id={teacher['id']}").json()
        assert all(p["account_id"] == teacher["id"] for p in rows)

        cmp_data = c.get("/api/accounts/comparison").json()
        assert len(cmp_data["accounts"]) >= 2
        for a in cmp_data["accounts"]:
            assert {"total_trades", "win_rate", "avg_r", "profit_factor",
                    "max_drawdown_r", "behavior_flags"} <= set(a["stats"])
