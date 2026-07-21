"""各來源 API 呼叫計數器(每日,UTC 日切)。

Twelve Data 已有 QuotaTracker(硬上限);本模組提供跨來源的統一「觀測」計數,
供 /health 顯示與軟上限降級判斷。行程內記憶體即可(重啟歸零屬可接受誤差,
因為 TD 硬上限另有 QuotaTracker 把關)。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

_counts: dict[str, dict] = {}


def _today() -> date:
    return datetime.now(timezone.utc).date()


def bump(source: str, n: int = 1) -> int:
    """記一次呼叫,回傳該來源今日累計。"""
    row = _counts.get(source)
    if row is None or row["day"] != _today():
        row = {"day": _today(), "count": 0}
        _counts[source] = row
    row["count"] += n
    return row["count"]


def used_today(source: str) -> int:
    row = _counts.get(source)
    if row is None or row["day"] != _today():
        return 0
    return row["count"]


def snapshot() -> dict[str, int]:
    return {src: used_today(src) for src in sorted(_counts)}
