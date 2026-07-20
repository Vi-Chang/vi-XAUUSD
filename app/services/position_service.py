"""手動持倉管理(spec 十三 C 之手動輸入途徑、十七之持倉管理規則)。

- 持倉、停損修改歷史、分批平倉歷史全部入庫,供記錄與復盤。
- 行為偵測(spec 十九,確定性規則):
  * STOP_WIDENING:停損往虧損方向移動 → 立即記 behavior_flags。
  * EARLY_EXIT:未達 1R 即平掉超過 50% 部位 → 記 behavior_flags。
- 持倉管理建議依 spec 十七的 R 階段規則產生,不使用情緒字眼。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db.models import BehaviorFlag, Position
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
                    account_id: int | None = None) -> Position:
    """建立手動持倉(掛帳戶)。停損方向錯誤(多單停損高於進場等)直接拒絕。"""
    side = side.upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError("side 必須是 LONG 或 SHORT")
    if lot_size <= 0:
        raise ValueError("lot_size 必須大於 0")
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
        return (f"已經賠到或超過賠錢出場價了(賺賠比 {r} 倍),照紀律該出就出,"
                "千萬別凹單放大虧損。", prohibited)
    if r < 1.0:
        return (f"還沒回本(賺賠比 {r} 倍):賠錢出場價守原本的位置,別急著移到成本價;"
                "除非行情邏輯壞了,否則別被小震盪洗掉。", prohibited)
    if r < 2.0:
        return (f"小賺了(賺賠比 {r} 倍):可以先落袋 2~3 成,剩下的看 15 分K 管理,"
                "先別急著保本以免正常回踩被掃。", prohibited)
    return (f"賺不少了(賺賠比 {r} 倍):再落袋 3~5 成,留 2~4 成續抱賺趨勢,"
            "賠錢出場價跟著結構往上移;分批出,不是全跑、也不是全賭。", prohibited)


def _flag(db, flag: str, evidence: dict, action: str) -> None:
    db.add(BehaviorFlag(flag=flag, detected_at=_now(), evidence=evidence,
                        corrective_action=action))


def modify_stop(position_id: int, new_stop: float) -> tuple[Position, str | None]:
    """修改停損;往虧損方向移動 → 記 STOP_WIDENING(spec 十九)。"""
    with db_session() as db:
        pos = db.get(Position, position_id)
        if pos is None or not pos.is_open:
            raise ValueError("持倉不存在或已平倉")
        old = pos.stop_loss
        widening = (old is not None and
                    ((pos.side == "LONG" and new_stop < old) or
                     (pos.side == "SHORT" and new_stop > old)))
        hist = list(pos.stop_modification_history or [])
        hist.append({"time": _now().isoformat(), "old_stop": old, "new_stop": new_stop,
                     "widening": bool(widening)})
        pos.stop_modification_history = hist
        pos.stop_loss = new_stop
        flag = None
        if widening:
            flag = "STOP_WIDENING"
            _flag(db, flag,
                  {"position_id": pos.id, "side": pos.side, "entry": pos.entry_price,
                   "old_stop": old, "new_stop": new_stop, "time": _now().isoformat()},
                  "停損只能往獲利方向移動;請立即恢復原結構失效點停損,"
                  "並檢視是否在期待價格回來(老問題 16)")
        db.flush()
        db.refresh(pos)
        return pos, flag


def partial_exit(position_id: int, percent: float, price: float) -> tuple[Position, str | None]:
    """分批平倉;未達 1R 即平掉 >50% → 記 EARLY_EXIT(spec 十九)。"""
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
        flag = None
        if r_at_exit is not None and r_at_exit < 1.0 and percent > 50:
            flag = "EARLY_EXIT"
            _flag(db, flag,
                  {"position_id": pos.id, "side": pos.side, "entry": pos.entry_price,
                   "exit_price": price, "percent": percent, "r_at_exit": r_at_exit,
                   "time": _now().isoformat()},
                  f"於 R={r_at_exit}(未達第一目標)平掉 {percent}% 部位;"
                  "若交易邏輯未失效,請依計畫分批而非恐懼出場(老問題 14)")
        if sum(float(x["percent"]) for x in hist) >= 100:
            pos.is_open = False
            pos.close_time = _now()
        db.flush()
        db.refresh(pos)
        return pos, flag


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
    view = {
        "id": pos.id, "account_id": pos.account_id,
        "side": pos.side, "entry_price": pos.entry_price,
        "stop_loss": pos.stop_loss, "lot_size": pos.lot_size,
        "open_time": ensure_utc(pos.open_time).isoformat(),
        "close_time": ensure_utc(pos.close_time).isoformat() if pos.close_time else None,
        "is_open": pos.is_open,
        "planned_targets": pos.planned_targets or [],
        "partial_exit_history": pos.partial_exit_history or [],
        "stop_modification_history": pos.stop_modification_history or [],
        "remaining_percent": round(remaining_fraction(pos) * 100, 1),
        "current_price": current_price,
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


def recent_behavior_flags(limit: int = 20) -> list[dict]:
    with db_session() as db:
        rows = db.execute(select(BehaviorFlag)
                          .order_by(BehaviorFlag.detected_at.desc())
                          .limit(limit)).scalars().all()
    return [{"flag": r.flag, "detected_at": ensure_utc(r.detected_at).isoformat(),
             "evidence": r.evidence, "corrective_action": r.corrective_action}
            for r in rows]
