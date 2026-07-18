"""重要價位 + 價位候選編號制(spec 八)— 系統核心防幻覺機制。

- 支撐/壓力一律呈現為「區域」(寬度依 15M ATR),不假裝精準單一價。
- 每次分析產生帶唯一 ID 的候選清單(SUP_ZONE_01 / RES_ZONE_02 / SWING_LOW_15M_03…),
  劇本欄位只能引用這些 ID;AI(Phase 7)同樣只能引用 ID,禁止自創數字。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from app.config import get_settings
from app.engines.market_structure import StructureReport
from app.utils.timeutils import trading_day, trading_week_start


@dataclass
class CandidateLevel:
    level_id: str
    kind: str                   # SUP_ZONE/RES_ZONE/SWING_HIGH/SWING_LOW/PIVOT/ROUND/PDH/PDL/...
    price_low: float
    price_high: float
    strength: str               # STRONG/WEAK/INFO
    sources: list[str] = field(default_factory=list)

    @property
    def mid(self) -> float:
        return round((self.price_low + self.price_high) / 2, 2)

    def to_dict(self) -> dict:
        return {"id": self.level_id, "kind": self.kind,
                "price_low": round(self.price_low, 2), "price_high": round(self.price_high, 2),
                "strength": self.strength, "source": " + ".join(self.sources)}


@dataclass
class _RawLevel:
    price: float
    source: str
    weight: int   # 1=普通, 2=重要(前日高低/週 pivot/多次測試 swing)


def _daily_weekly_levels(daily_df: pd.DataFrame) -> list[_RawLevel]:
    """由日線(NY 17:00 切分)取 PDH/PDL/PDC、前週高低、Pivot(spec 八)。"""
    out: list[_RawLevel] = []
    if daily_df.empty or len(daily_df) < 2:
        return out
    closed = daily_df[daily_df["is_closed"]] if "is_closed" in daily_df.columns else daily_df
    if len(closed) < 1:
        return out
    prev = closed.iloc[-1]  # 最後一根已收線日線 = 前一交易日
    h, low, c = float(prev["high"]), float(prev["low"]), float(prev["close"])
    out += [_RawLevel(h, "PrevDayHigh", 2), _RawLevel(low, "PrevDayLow", 2),
            _RawLevel(c, "PrevDayClose", 1)]
    pivot = (h + low + c) / 3
    out += [_RawLevel(pivot, "DailyPivot", 1),
            _RawLevel(2 * pivot - low, "DailyR1", 1), _RawLevel(2 * pivot - h, "DailyS1", 1)]

    # 前週高低 + Weekly Pivot:以 trading_week_start 分組
    days = [trading_day(t.to_pydatetime()) for t in closed.index]
    weeks = pd.Series([trading_week_start(d) for d in days], index=closed.index)
    grouped = list(closed.groupby(weeks))
    if len(grouped) >= 2:
        _, prev_week = grouped[-2]
        wh, wl = float(prev_week["high"].max()), float(prev_week["low"].min())
        wc = float(prev_week["close"].iloc[-1])
        wp = (wh + wl + wc) / 3
        out += [_RawLevel(wh, "PrevWeekHigh", 2), _RawLevel(wl, "PrevWeekLow", 2),
                _RawLevel(wp, "WeeklyPivot", 2)]
    return out


def _round_numbers(price: float, step: float, span: float) -> list[_RawLevel]:
    lo = math.floor((price - span) / step) * step
    hi = math.ceil((price + span) / step) * step
    out = []
    x = lo
    while x <= hi:
        out.append(_RawLevel(round(x, 2), f"Round{int(step)}", 1))
        x += step
    return out


def _swing_levels(reports: dict[str, StructureReport]) -> list[_RawLevel]:
    out: list[_RawLevel] = []
    for tf, rep in reports.items():
        weight = 2 if tf in ("4H", "1D") else 1
        for sp in rep.swings[-6:]:
            tag = "SwingHigh" if sp.kind == "SWING_HIGH" else "SwingLow"
            out.append(_RawLevel(sp.price, f"{tag}_{tf}", weight))
    return out


def build_candidate_levels(*, price: float, atr15: float,
                           daily_df: pd.DataFrame,
                           structure_reports: dict[str, StructureReport]) -> list[CandidateLevel]:
    """聚合所有原始價位 → 合併為區域 → 編號(spec 八之候選編號制)。

    - 相距 level_cluster_atr_mult × ATR15 內的價位合併為同一區。
    - 區域半寬至少 zone_half_width_atr15_mult × ATR15。
    - 權重總和 >= 3 或含兩個以上來源 → STRONG。
    """
    s = get_settings()
    atr15 = max(atr15, price * 0.0003)  # 防 ATR 為 0
    raw: list[_RawLevel] = []
    raw += _daily_weekly_levels(daily_df)
    raw += _round_numbers(price, s.round_number_step, span=6 * atr15)
    raw += _swing_levels(structure_reports)
    raw.sort(key=lambda r: r.price)

    # 聚類
    cluster_dist = s.level_cluster_atr_mult * atr15
    clusters: list[list[_RawLevel]] = []
    for r in raw:
        if clusters and abs(r.price - clusters[-1][-1].price) <= cluster_dist:
            clusters[-1].append(r)
        else:
            clusters.append([r])

    half_w = s.zone_half_width_atr15_mult * atr15
    counters: dict[str, int] = {}
    out: list[CandidateLevel] = []

    def next_id(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}_{counters[prefix]:02d}"

    for cl in clusters:
        prices = [r.price for r in cl]
        weight = sum(r.weight for r in cl)
        sources = sorted({r.source for r in cl})
        lo = min(prices) - half_w / 2
        hi = max(prices) + half_w / 2
        mid = (lo + hi) / 2
        strong = weight >= 3 or len(sources) >= 2
        if mid < price:
            kind = "SUP_ZONE"
            strength = "STRONG" if strong else "WEAK"
        elif mid > price:
            kind = "RES_ZONE"
            strength = "STRONG" if strong else "WEAK"
        else:
            kind, strength = "MID_RANGE", "INFO"
        out.append(CandidateLevel(next_id(kind), kind, round(lo, 2), round(hi, 2),
                                  strength, sources))

    # 單點型候選:最近 15M/1H swing(進場/停損精細引用用)
    for tf in ("15M", "1H"):
        rep = structure_reports.get(tf)
        if not rep:
            continue
        if rep.last_swing_low is not None:
            out.append(CandidateLevel(next_id(f"SWING_LOW_{tf}"), f"SWING_LOW_{tf}",
                                      rep.last_swing_low, rep.last_swing_low, "INFO",
                                      [f"last confirmed swing low {tf}"]))
        if rep.last_swing_high is not None:
            out.append(CandidateLevel(next_id(f"SWING_HIGH_{tf}"), f"SWING_HIGH_{tf}",
                                      rep.last_swing_high, rep.last_swing_high, "INFO",
                                      [f"last confirmed swing high {tf}"]))
    return out


def resolve_ids(levels: list[CandidateLevel], ids: list[str | None]) -> dict[str, dict]:
    """後端反查:候選 ID → 實際數字(spec 八之5)。未知 ID 不會出現在結果中。"""
    table = {lv.level_id: lv.to_dict() for lv in levels}
    return {i: table[i] for i in ids if i and i in table}


def nearest_zone(levels: list[CandidateLevel], price: float, kind: str,
                 strength: str | None = None) -> CandidateLevel | None:
    """距離現價最近的指定類型區域(規則引擎選 entry/stop 用)。"""
    pool = [lv for lv in levels if lv.kind == kind and (strength is None or lv.strength == strength)]
    if not pool:
        pool = [lv for lv in levels if lv.kind == kind]
    if not pool:
        return None
    return min(pool, key=lambda lv: abs(lv.mid - price))
