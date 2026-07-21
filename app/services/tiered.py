"""三層更新頻率架構(spec:報價快 / 結構中速 / 完整分析慢速+事件觸發)。

第 1 層 報價層(預設 60s):只抓最新報價寫入記憶體快取;禁 Twelve Data K 棒、禁 AI。
第 2 層 結構層(預設 300s):純程式邏輯 —— 觸及候選價位 / 突破 15 分K 前高前低 /
        異常波動;任一成立 → 標記事件觸發第 3 層;同價位事件 60 分鐘冷卻。
第 3 層 完整分析:事件觸發 + 定時保底(預設 60 分鐘),執行既有 run_analysis。

三層彼此獨立:任何一層失敗只影響自己(第 3 層另有定時保底兜底)。
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.providers.base import PriceTick

logger = logging.getLogger(__name__)

BUCKET_SEC = 300  # 異常波動判定用的 5 分鐘桶


@dataclass
class TierEvent:
    key: str          # 冷卻用唯一鍵(level_id / BREAK_HIGH / ANOMALY…)
    reason_zh: str    # 白話觸發原因(訊息開頭用)


class QuoteCache:
    """第 1 層報價快取 + 5 分鐘桶振幅統計(供異常波動判定)。"""

    def __init__(self, max_buckets: int = 40) -> None:
        self.last_tick: PriceTick | None = None
        self.last_update: datetime | None = None
        self._bucket_start: datetime | None = None
        self._bucket_hi: float | None = None
        self._bucket_lo: float | None = None
        self._closed_ranges: deque[float] = deque(maxlen=max_buckets)

    def add(self, tick: PriceTick) -> None:
        now = datetime.now(timezone.utc)
        self.last_tick = tick
        self.last_update = now
        bucket = now.replace(second=0, microsecond=0)
        bucket -= timedelta(minutes=bucket.minute % 5)
        if self._bucket_start != bucket:
            if self._bucket_hi is not None and self._bucket_lo is not None:
                self._closed_ranges.append(self._bucket_hi - self._bucket_lo)
            self._bucket_start = bucket
            self._bucket_hi = self._bucket_lo = tick.mid
        else:
            self._bucket_hi = max(self._bucket_hi, tick.mid)
            self._bucket_lo = min(self._bucket_lo, tick.mid)

    def fresh_tick(self, max_age_seconds: int) -> PriceTick | None:
        if self.last_tick is None or self.last_update is None:
            return None
        age = (datetime.now(timezone.utc) - self.last_update).total_seconds()
        return self.last_tick if age <= max_age_seconds else None

    def current_bucket_range(self) -> float | None:
        if self._bucket_hi is None or self._bucket_lo is None:
            return None
        return self._bucket_hi - self._bucket_lo

    def avg_closed_range(self, n: int = 20) -> float | None:
        rows = list(self._closed_ranges)[-n:]
        if len(rows) < 10:   # 樣本太少不判定(開機暖身期)
            return None
        return sum(rows) / len(rows)


class EventCooldown:
    """同一事件鍵在冷卻時間內只觸發一次(防盤整洗版)。"""

    def __init__(self) -> None:
        self._fired: dict[str, datetime] = {}

    def allow(self, key: str, cooldown_minutes: int) -> bool:
        now = datetime.now(timezone.utc)
        last = self._fired.get(key)
        if last is not None and (now - last) < timedelta(minutes=cooldown_minutes):
            return False
        self._fired[key] = now
        return True


def _latest_candidate_levels() -> list:
    """讀最近一次分析產生的候選價位(含區域與 15M swing 單點)。"""
    from sqlalchemy import select

    from app.db.models import AnalysisRun, CandidateLevel
    from app.db.session import db_session
    with db_session() as db:
        run_id = db.execute(select(AnalysisRun.id)
                            .order_by(AnalysisRun.run_time.desc())
                            .limit(1)).scalar_one_or_none()
        if run_id is None:
            return []
        return list(db.execute(select(CandidateLevel)
                               .where(CandidateLevel.analysis_run_id == run_id))
                    .scalars().all())


def check_structure_events(price: float, cache: QuoteCache,
                           cooldown: EventCooldown) -> list[TierEvent]:
    """第 2 層核心:純程式邏輯,回傳本輪成立的事件(已過冷卻)。"""
    s = get_settings()
    events: list[TierEvent] = []
    levels = _latest_candidate_levels()

    swing_high = None
    swing_low = None
    for lv in levels:
        # a. 觸及候選價位(距離 ≤ tier2_touch_pct;在區間內視為距離 0)
        if lv.price_low <= price <= lv.price_high:
            dist_pct = 0.0
        else:
            dist_pct = min(abs(price - lv.price_low), abs(price - lv.price_high)) / price
        if dist_pct <= s.tier2_touch_pct and lv.kind in ("SUP_ZONE", "RES_ZONE"):
            if cooldown.allow(f"touch:{lv.level_id}", s.tier2_level_cooldown_minutes):
                kind_zh = "支撐區" if lv.kind == "SUP_ZONE" else "壓力區"
                events.append(TierEvent(
                    f"touch:{lv.level_id}",
                    f"價格觸及候選{kind_zh} {lv.level_id}"
                    f"({lv.price_low:.2f}–{lv.price_high:.2f})"))
        if lv.kind == "SWING_HIGH_15M":
            swing_high = lv.price_low
        if lv.kind == "SWING_LOW_15M":
            swing_low = lv.price_low

    # b. 突破 15 分K 前高/前低(沿用系統結構價位;正式 BOS/CHoCH 由第 3 層收線確認)
    if swing_high is not None and price > swing_high:
        if cooldown.allow("break:high", s.tier2_level_cooldown_minutes):
            events.append(TierEvent(
                "break:high",
                f"價格突破 15 分K 前高 {swing_high:.2f}(可能順勢突破,待收線確認)"))
    if swing_low is not None and price < swing_low:
        if cooldown.allow("break:low", s.tier2_level_cooldown_minutes):
            events.append(TierEvent(
                "break:low",
                f"價格跌破 15 分K 前低 {swing_low:.2f}(可能順勢跌破,待收線確認)"))

    # c. 異常波動:目前 5 分鐘桶振幅 > 近 20 桶平均 × 倍數
    cur = cache.current_bucket_range()
    avg = cache.avg_closed_range(20)
    if cur is not None and avg is not None and avg > 0 and \
            cur > avg * s.tier2_anomaly_range_mult:
        if cooldown.allow("anomaly", s.tier2_level_cooldown_minutes):
            events.append(TierEvent(
                "anomaly",
                f"5 分鐘內波動異常放大(振幅 {cur:.2f},是近期平均 {avg:.2f} 的 "
                f"{cur / avg:.1f} 倍)"))
    return events
