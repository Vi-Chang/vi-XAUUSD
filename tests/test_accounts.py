"""實際交易帳戶：種子、統計與 API。"""
import pytest

from app.db.session import init_db
from app.services import account_service as acs
from app.services import position_service as ps


@pytest.fixture(autouse=True)
def _db():
    init_db()
    yield


def _account_id() -> int:
    accounts = acs.list_accounts()
    self_acc = next(a for a in accounts if a["strategy_source"] == "SELF")
    return self_acc["id"]


def test_default_accounts_seeded():
    accounts = acs.list_accounts()
    sources = {a["strategy_source"] for a in accounts}
    assert sources == {"SELF"}


def test_position_defaults_to_self_account():
    self_id = _account_id()
    pos = ps.create_position(side="LONG", entry_price=4000.0, stop_loss=3990.0,
                             lot_size=0.1)
    assert pos.account_id == self_id


def test_unknown_account_rejected():
    with pytest.raises(ValueError):
        ps.create_position(side="LONG", entry_price=4000.0, stop_loss=3990.0,
                           lot_size=0.1, account_id=99999)


def test_per_account_stats_and_comparison():
    self_id = _account_id()
    base_self = acs.account_stats(self_id)["total_trades"]
    # 自己帳戶:一筆 -1R 虧損
    p2 = ps.create_position(side="SHORT", entry_price=4000.0, stop_loss=4010.0,
                            lot_size=0.1, account_id=self_id)
    ps.close_position(p2.id, 4010.0)

    s_stats = acs.account_stats(self_id)
    assert s_stats["total_trades"] == base_self + 1
    assert s_stats["total_pnl_usd"] <= -100.0 + 0.01  # -10 × 0.1 × 100

    data = acs.comparison()
    assert "note" in data and "勝率" in data["note"]
    assert data["accounts"][0]["strategy_source"] == "SELF"


def test_accounts_api():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        accounts = c.get("/api/accounts").json()
        assert len(accounts) == 1
        account = accounts[0]

        r = c.post("/api/positions", json={
            "side": "LONG", "entry_price": 4000.0, "stop_loss": 3990.0,
            "lot_size": 0.1, "account_id": account["id"]})
        assert r.status_code == 200
        assert r.json()["account_id"] == account["id"]

        # 帳戶過濾
        rows = c.get(f"/api/positions?account_id={account['id']}").json()
        assert all(p["account_id"] == account["id"] for p in rows)

        cmp_data = c.get("/api/accounts/comparison").json()
        assert len(cmp_data["accounts"]) == 1
        for a in cmp_data["accounts"]:
            assert {"total_trades", "win_rate", "avg_r", "profit_factor",
                    "max_drawdown_r"} <= set(a["stats"])
