"""手動持倉管理(spec 十三 C 之手動輸入途徑、十七之持倉管理規則)。

- 持倉、停損修改歷史、分批平倉歷史全部入庫,供記錄與復盤。
- 持倉管理建議依 spec 十七的 R 階段規則產生,不使用情緒字眼。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db.models import Position
from app.db.session import db_session
from app.utils.timeutils import ensure_utc

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def default_account_id() -> int | None:
    """預設掛到 SELF(自己交易)帳戶。"""
    from app.db.models import Account
    with db_session() as db:
        acc = db.execute(select(Account).where(Account.strategy_source == "SELF")
                         .order_by(Account.id)).scalars().first()
        return acc.id if acc else None


def create_position(*, side: str, entry_price: float, stop_loss: float | None,
                    lot_size: float, planned_targets: list[float] | None = None,
                    open_time: datetime | None = None,
                    account_id: int | None = None, position_timeframe: str = "unknown",
                    original_thesis: str = "", max_loss_usd: float | None = None,
                    allow_event_hold: bool | None = None) -> Position:
    """建立手動持倉(掛帳戶)。停損方向錯誤(多單停損高於進場等)直接拒絕。"""
    side = side.upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError("side 必須是 LONG 或 SHORT")
    if lot_size <= 0:
        raise ValueError("lot_size 必須大於 0")
    position_timeframe = position_timeframe.upper()
    if position_timeframe not in ("15M", "1H", "4H", "1D", "UNKNOWN"):
        raise ValueError("position_timeframe 必須是 15M、1H、4H、1D 或 unknown")
    if max_loss_usd is not None and max_loss_usd <= 0:
        raise ValueError("max_loss_usd 必須大於 0")
    if stop_loss is not None:
        if side == "LONG" and stop_loss >= entry_price:
            raise ValueError("多單停損必須低於進場價")
        if side == "SHORT" and stop_loss <= entry_price:
            raise ValueError("空單停損必須高於進場價")
    if account_id is None:
        account_id = default_account_id()
    else:
        from app.db.models import Account
        with db_session() as db:
            if db.get(Account, account_id) is None:
                raise ValueError(f"帳戶 {account_id} 不存在")
    with db_session() as db:
        pos = Position(symbol="XAUUSD", side=side, entry_price=entry_price,
                       stop_loss=stop_loss, lot_size=lot_size,
                       position_timeframe=("unknown" if position_timeframe == "UNKNOWN"
                                           else position_timeframe),
                       original_thesis=original_thesis.strip()[:500],
                       max_loss_usd=max_loss_usd, allow_event_hold=allow_event_hold,
                       open_time=ensure_utc(open_time) if open_time else _now(),
                       planned_targets=planned_targets or [],
                       partial_exit_history=[], stop_modification_history=[],
                       source="manual", is_open=True, account_id=account_id)
        db.add(pos)
        db.flush()
        db.refresh(pos)
        return pos


def list_positions(include_closed: bool = True, limit: int = 20,
                   account_id: int | None = None) -> list[Position]:
    with db_session() as db:
        q = select(Position).order_by(Position.open_time.desc()).limit(limit)
        if not include_closed:
            q = q.where(Position.is_open.is_(True))
        if account_id is not None:
            q = q.where(Position.account_id == account_id)
        return list(db.execute(q).scalars().all())


def _direction(side: str) -> int:
    return 1 if side == "LONG" else -1


def r_multiple(pos: Position, current_price: float) -> float | None:
    """目前 R 倍數 =(現價-進場)方向化 / 初始風險距離。無停損時無法計算。"""
    if pos.stop_loss is None:
        return None
    risk = abs(pos.entry_price - _initial_stop(pos))
    if risk <= 0:
        return None
    return round(_direction(pos.side) * (current_price - pos.entry_price) / risk, 2)


def _initial_stop(pos: Position) -> float:
    """初始停損(R 的分母永遠用最初風險,避免移動停損後 R 定義漂移)。"""
    for h in (pos.stop_modification_history or []):
        if h.get("old_stop") is not None:
            return float(h["old_stop"])
    return float(pos.stop_loss)


def remaining_fraction(pos: Position) -> float:
    exited = sum(float(x.get("percent", 0)) for x in (pos.partial_exit_history or []))
    return max(0.0, 1.0 - exited / 100.0)


def unrealized_pnl(pos: Position, current_price: float) -> float:
    oz = get_settings().gold_contract_oz
    return round(_direction(pos.side) * (current_price - pos.entry_price)
                 * pos.lot_size * remaining_fraction(pos) * oz, 2)


def recommended_action(pos: Position, current_price: float) -> tuple[str, list[str]]:
    """依 spec 十七的階段規則產生建議與禁止事項。"""
    r = r_multiple(pos, current_price)
    prohibited = [
        "別因為一根小黑K或指標超買就把單全部出掉",
        "別把賠錢出場價往賠更多的方向挪(凹單)",
        "到目標別因為想多賺就取消原本的停利",
    ]
    if r is None:
        return "你還沒設賠錢出場價!請立刻補上,不然虧多少自己都不知道。", prohibited
    if r <= -1.0:
        return ((f"已經賠到或超過賠錢出場價了(賺賠比 {r} 倍),照紀律該出就出,"
                "千萬別凹單放大虧損。"), prohibited)
    if r < 1.0:
        return ((f"還沒回本(賺賠比 {r} 倍):賠錢出場價守原本的位置,別急著移到成本價;"
                "除非行情邏輯壞了,否則別被小震盪洗掉。"), prohibited)
    if r < 2.0:
        return ((f"小賺了(賺賠比 {r} 倍):可以先落袋 2~3 成,剩下的看 15 分K 管理,"
                "先別急著保本以免正常回踩被掃。"), prohibited)
    return ((f"賺不少了(賺賠比 {r} 倍):再落袋 3~5 成,留 2~4 成續抱賺趨勢,"
            "賠錢出場價跟著結構往上移;分批出,不是全跑、也不是全賭。"), prohibited)


def modify_stop(position_id: int, new_stop: float) -> tuple[Position, str | None]:
    """修改停損並保留客觀修改歷史。"""
    with db_session() as db:
        pos = db.get(Position, position_id)
        if pos is None or not pos.is_open:
            raise ValueError("持倉不存在或已平倉")
        old = pos.stop_loss
        from app.engines.trading_invariants import validate_stop_update
        validate_stop_update(pos.side, previous_stop=old, new_stop=new_stop)
        widening = (old is not None and
                    ((pos.side == "LONG" and new_stop < old) or
                     (pos.side == "SHORT" and new_stop > old)))
        hist = list(pos.stop_modification_history or [])
        hist.append({"time": _now().isoformat(), "old_stop": old, "new_stop": new_stop,
                     "widening": bool(widening)})
        pos.stop_modification_history = hist
        pos.stop_loss = new_stop
        db.flush()
        db.refresh(pos)
        return pos, None


def update_position_context(
    position_id: int,
    *,
    position_timeframe: str,
    original_thesis: str,
    max_loss_usd: float | None,
    allow_event_hold: bool | None,
) -> Position:
    """補齊既有持倉的決策背景，不改動交易價格或部位。"""
    if position_timeframe not in {"15M", "1H", "4H", "1D", "unknown"}:
        raise ValueError("position_timeframe 必須是 15M、1H、4H、1D 或 unknown")
    if max_loss_usd is not None and max_loss_usd <= 0:
        raise ValueError("max_loss_usd 必須大於 0")
    with db_session() as db:
        pos = db.get(Position, position_id)
        if pos is None or not pos.is_open:
            raise ValueError("找不到未平倉部位")
        pos.position_timeframe = position_timeframe
        pos.original_thesis = original_thesis.strip()
        pos.max_loss_usd = max_loss_usd
        pos.allow_event_hold = allow_event_hold
        db.flush()
        db.refresh(pos)
        return pos


def partial_exit(position_id: int, percent: float, price: float) -> tuple[Position, str | None]:
    """分批平倉並保留客觀成交紀錄。"""
    if not 0 < percent <= 100:
        raise ValueError("percent 必須在 (0, 100]")
    with db_session() as db:
        pos = db.get(Position, position_id)
        if pos is None or not pos.is_open:
            raise ValueError("持倉不存在或已平倉")
        r_at_exit = r_multiple(pos, price)
        hist = list(pos.partial_exit_history or [])
        hist.append({"time": _now().isoformat(), "percent": percent, "price": price,
                     "r_at_exit": r_at_exit})
        pos.partial_exit_history = hist
        if sum(float(x["percent"]) for x in hist) >= 100:
            pos.is_open = False
            pos.close_time = _now()
        db.flush()
        db.refresh(pos)
        return pos, None


def close_position(position_id: int, price: float) -> tuple[Position, str | None]:
    """全部平倉(等同一次 100% 減剩餘部位的分批)。"""
    with db_session() as db:
        pos = db.get(Position, position_id)
        if pos is None or not pos.is_open:
            raise ValueError("持倉不存在或已平倉")
        remaining = remaining_fraction(pos) * 100
    return partial_exit(position_id, remaining if remaining > 0 else 100, price)


def position_view(pos: Position, current_price: float | None) -> dict:
    """單筆持倉的完整檢視(API 回應用)。"""
    from app.utils.formatting import fmt_price
    view = {
        "id": pos.id, "account_id": pos.account_id,
        "side": pos.side, "entry_price": fmt_price(pos.entry_price),
        "stop_loss": fmt_price(pos.stop_loss), "lot_size": pos.lot_size,
        "position_timeframe": pos.position_timeframe or "unknown",
        "original_thesis": pos.original_thesis or "",
        "max_loss_usd": pos.max_loss_usd,
        "allow_event_hold": pos.allow_event_hold,
        "open_time": ensure_utc(pos.open_time).isoformat(),
        "close_time": ensure_utc(pos.close_time).isoformat() if pos.close_time else None,
        "is_open": pos.is_open,
        "planned_targets": [fmt_price(t) for t in (pos.planned_targets or [])],
        "partial_exit_history": pos.partial_exit_history or [],
        "stop_modification_history": pos.stop_modification_history or [],
        "remaining_percent": round(remaining_fraction(pos) * 100, 1),
        "current_price": fmt_price(current_price),
        "r_multiple": None, "unrealized_pnl": None,
        "recommended_action": "", "prohibited_actions": [],
    }
    if current_price is not None and pos.is_open:
        view["r_multiple"] = r_multiple(pos, current_price)
        view["unrealized_pnl"] = unrealized_pnl(pos, current_price)
        action, prohibited = recommended_action(pos, current_price)
        view["recommended_action"] = action
        view["prohibited_actions"] = prohibited
    return view


