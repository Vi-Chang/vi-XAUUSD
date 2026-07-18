"""帳戶層統計:老師帶單 vs 自己交易,分開統計 + 對照(spec 二十四之統計指標)。

統計來源 = 該帳戶「已平倉」的手動持倉(realized);
依 spec 二十四:不得只用勝率評估,優先看 Expectancy(平均 R)、
最大回撤、獲利因子;對照輸出附此提醒。
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.config import get_settings
from app.db.models import Account, BehaviorFlag, Position
from app.db.session import db_session

logger = logging.getLogger(__name__)


def list_accounts() -> list[dict]:
    with db_session() as db:
        rows = db.execute(select(Account).where(Account.is_active.is_(True))
                          .order_by(Account.id)).scalars().all()
    return [{"id": a.id, "name": a.name, "strategy_source": a.strategy_source,
             "description": a.description} for a in rows]


def _direction(side: str) -> int:
    return 1 if side == "LONG" else -1


def realized_pnl(pos: Position) -> float:
    """已實現損益(USD)= 各分批平倉之和。"""
    oz = get_settings().gold_contract_oz
    total = 0.0
    for x in (pos.partial_exit_history or []):
        price = x.get("price")
        pct = float(x.get("percent", 0)) / 100.0
        if price is None:
            continue
        total += _direction(pos.side) * (float(price) - pos.entry_price) * pos.lot_size * pct * oz
    return round(total, 2)


def realized_r(pos: Position) -> float | None:
    """已實現 R = Σ(平倉比例 × 該次 R);任一筆缺 R(無停損)則回傳 None。"""
    exits = pos.partial_exit_history or []
    if not exits:
        return None
    total = 0.0
    for x in exits:
        r = x.get("r_at_exit")
        if r is None:
            return None
        total += float(x.get("percent", 0)) / 100.0 * float(r)
    return round(total, 2)


def account_stats(account_id: int) -> dict:
    """單一帳戶統計(僅已平倉持倉)。"""
    with db_session() as db:
        closed = db.execute(select(Position).where(
            Position.account_id == account_id, Position.is_open.is_(False))
            .order_by(Position.close_time)).scalars().all()
        pos_ids = {p.id for p in db.execute(select(Position).where(
            Position.account_id == account_id)).scalars().all()}
        flags = db.execute(select(BehaviorFlag)).scalars().all()

    pnls = [realized_pnl(p) for p in closed]
    rs = [r for r in (realized_r(p) for p in closed) if r is not None]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))

    # 最大回撤(以已實現 R 權益曲線;R 不可得時以 0 計)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in closed:
        equity += (realized_r(p) or 0.0)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    flag_count = sum(1 for f in flags
                     if (f.evidence or {}).get("position_id") in pos_ids)

    n = len(closed)
    return {
        "total_trades": n,
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(100 * len(wins) / n, 1) if n else None,
        "total_pnl_usd": round(sum(pnls), 2),
        "total_r": round(sum(rs), 2) if rs else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,   # Expectancy
        "profit_factor": (round(gross_win / gross_loss, 2) if gross_loss > 0
                          else (None if not wins else float("inf"))),
        "max_drawdown_r": round(max_dd, 2),
        "behavior_flags": flag_count,
    }


def comparison() -> dict:
    """對照頁 payload:各帳戶統計並列。"""
    accounts = list_accounts()
    out = []
    for a in accounts:
        stats = account_stats(a["id"])
        # JSON 不支援 Infinity → 以 None 表示「無虧損樣本」
        if stats["profit_factor"] == float("inf"):
            stats["profit_factor"] = None
        out.append({**a, "stats": stats})
    return {
        "accounts": out,
        "note": ("不得只用勝率評估(spec 二十四):優先觀察 Expectancy(平均 R)、"
                 "最大回撤、獲利因子與紀律(行為標籤數);樣本數不足時任何結論都不可靠"),
    }
