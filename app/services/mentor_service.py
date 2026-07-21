"""老師帶單(mentor_signal)服務 — 僅供參考比對,完全與持倉脫鉤。

嚴格邊界(依需求):
- 老師帶單「不算持倉」:不進入 has_position 判斷、不切 MANAGE、不找/擋新交易。
- 比對結果(老師方向 vs 系統方向、老師進場 vs 現價差)純顯示,
  不影響任何進出場判斷、不加減證據分數。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import MentorSignal
from app.db.session import db_session

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_signal(*, direction: str, entry_price: float, stop_loss: float | None = None,
                  targets: list[float] | None = None, note: str | None = None,
                  signal_time: datetime | None = None) -> dict:
    direction = direction.upper()
    if direction not in ("LONG", "SHORT"):
        raise ValueError("direction 必須是 LONG 或 SHORT")
    with db_session() as db:
        sig = MentorSignal(direction=direction, entry_price=entry_price,
                           stop_loss=stop_loss, targets=targets or [], note=note,
                           signal_time=signal_time or _now(), is_active=True,
                           created_at=_now())
        db.add(sig)
        db.flush()
        db.refresh(sig)
        return _to_dict(sig)


def deactivate_signal(signal_id: int) -> None:
    with db_session() as db:
        sig = db.get(MentorSignal, signal_id)
        if sig is None:
            raise ValueError("老師帶單不存在")
        sig.is_active = False


def list_active_signals() -> list[MentorSignal]:
    """進行中的老師帶單;CLOSED 歷史匯入單一律排除(不得混算)。"""
    with db_session() as db:
        return list(db.execute(select(MentorSignal)
                               .where(MentorSignal.is_active.is_(True),
                                      MentorSignal.status != "CLOSED")
                               .order_by(MentorSignal.signal_time.desc())).scalars().all())


# 匯入批次 → 已知資料缺口(避免日後誤判為「該期間空手」)
KNOWN_GAPS: dict[str, list[str]] = {
    "MENTOR_HISTORY_2026Q2": ["2026-04-28 ~ 2026-05-13"],
}


def history_block() -> dict:
    """老師帶單歷史紀錄(CLOSED 匯入單)+ 統計摘要 + 已知缺口。純顯示。"""
    from app.utils.timeutils import ensure_utc
    with db_session() as db:
        rows = list(db.execute(select(MentorSignal)
                               .where(MentorSignal.status == "CLOSED")
                               .order_by(MentorSignal.close_time.desc())).scalars().all())
    from app.utils.formatting import fmt_price
    trades = [{
        "id": r.id, "direction": r.direction,
        "entry_price": fmt_price(r.entry_price), "close_price": fmt_price(r.close_price),
        "points": r.points, "lots": r.lots,
        "pl_usd": r.pl_usd, "swap_usd": r.swap_usd, "net_usd": r.net_usd,
        "stop_loss": fmt_price(r.stop_loss),   # 歷史匯入無停損資料 → null
        "r_multiple": r.r_multiple, "r_source": r.r_source,
        "close_time": ensure_utc(r.close_time).isoformat() if r.close_time else None,
        "import_batch": r.import_batch,
    } for r in rows]

    pls = [t["pl_usd"] for t in trades if t["pl_usd"] is not None]
    wins = [x for x in pls if x > 0]
    losses = [x for x in pls if x < 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    gaps = sorted({g for t in trades if t["import_batch"]
                   for g in KNOWN_GAPS.get(t["import_batch"], [])})
    return {
        "trades": trades,
        "summary": {
            "count": len(trades), "wins": len(wins), "losses": len(losses),
            "net_pl_usd": round(sum(pls), 2),
            "net_after_fees_usd": round(sum(t["net_usd"] for t in trades
                                            if t["net_usd"] is not None), 2),
            "gross_profit": round(gross_win, 2),
            "gross_loss": round(-gross_loss, 2),
            "profit_factor": (round(gross_win / gross_loss, 3)
                              if gross_loss > 0 else None),
        },
        "known_gaps": gaps,
        "note": ("歷史匯入單(TMGM App 截圖):無停損/停利/開倉時間資料,"
                 "不算持倉、不影響任何進出場判斷與證據分數"),
    }


def _to_dict(sig: MentorSignal) -> dict:
    from app.utils.formatting import fmt_price
    from app.utils.timeutils import ensure_utc
    return {"id": sig.id, "direction": sig.direction,
            "entry_price": fmt_price(sig.entry_price),
            "stop_loss": fmt_price(sig.stop_loss),
            "targets": [fmt_price(t) for t in (sig.targets or [])], "note": sig.note or "",
            "signal_time": ensure_utc(sig.signal_time).isoformat(), "is_active": sig.is_active}


def _system_direction(system_action: str) -> str | None:
    if system_action in ("LONG", "PREPARE_LONG"):
        return "LONG"
    if system_action in ("SHORT", "PREPARE_SHORT"):
        return "SHORT"
    return None


def compare_signal(sig: MentorSignal, system_action: str,
                   current_price: float | None) -> dict:
    """老師帶單 vs 系統方向比對 + 老師進場與現價差(純顯示)。"""
    sys_dir = _system_direction(system_action)
    if sys_dir is None:
        alignment, alignment_text = "SYSTEM_NEUTRAL", "系統目前無明確方向,無法比對"
    elif sys_dir == sig.direction:
        alignment, alignment_text = "ALIGNED", "老師方向與系統一致"
    else:
        alignment, alignment_text = "OPPOSITE", "老師方向與系統相反"

    gap = None
    gap_text = ""
    if current_price is not None:
        gap = round(current_price - sig.entry_price, 2)
        if gap > 0:
            gap_text = f"現價比老師進場高 {gap}"
        elif gap < 0:
            gap_text = f"現價比老師進場低 {abs(gap)}"
        else:
            gap_text = "現價正好在老師進場價"

    d = _to_dict(sig)
    d.update(system_direction=sys_dir, alignment=alignment, alignment_text=alignment_text,
             entry_vs_current=gap, entry_vs_current_text=gap_text)
    return d


def comparison_block(system_action: str, current_price: float | None) -> dict:
    """供分析結果附帶顯示;讀取 active 老師帶單並與系統方向比對。"""
    sigs = list_active_signals()
    return {
        "has_signals": bool(sigs),
        "signals": [compare_signal(s, system_action, current_price) for s in sigs],
        "note": "老師帶單僅供參考比對,不影響系統任何進出場判斷與證據分數",
    }
