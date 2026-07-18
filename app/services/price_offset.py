"""TMGM 價格校正(Price Offset)模組。

設計原則(量化正確性):
- 分析資料來源永遠是 TwelveData —— K 棒、EMA/RSI/MACD/ATR、支撐壓力、結構、趨勢
  全部在 TwelveData 空間計算,本模組完全不介入分析。
- Offset 只在「輸出邊界」套用到劇本的進場/停損/停利價(resolved_prices),
  將 TwelveData 分析價換算為 TMGM 實際掛單價:TMGM = TwelveData + Offset。
- Offset 存 system_settings(可即時修改、雲端 Postgres 持久化);
  修改 Offset 不重跑分析、不消耗 TwelveData 配額(套用於讀取時)。
- 模式:manual(使用者自訂)/ auto(TMGM 即時源自動算,目前無來源 → 保留 UI)。
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone

from app.config import get_settings
from app.db.models import SystemSetting
from app.db.session import db_session

logger = logging.getLogger(__name__)

OFFSET_KEY = "price_offset"
MODE_KEY = "offset_mode"
VALID_MODES = ("manual", "auto")


def get_offset() -> tuple[float, str]:
    """回傳 (offset 值, 模式);system_settings 覆寫 config 預設。"""
    s = get_settings()
    value, mode = s.price_offset, s.offset_mode
    try:
        with db_session() as db:
            rows = {r.key: r.value for r in db.query(SystemSetting)
                    .filter(SystemSetting.key.in_([OFFSET_KEY, MODE_KEY])).all()}
        if OFFSET_KEY in rows:
            value = float(rows[OFFSET_KEY])
        if MODE_KEY in rows and rows[MODE_KEY] in VALID_MODES:
            mode = rows[MODE_KEY]
    except Exception as exc:  # noqa: BLE001 — 讀取失敗退回 config 預設,不影響分析
        logger.warning("read offset setting failed: %s", exc)
    return round(value, 3), mode


def set_offset(value: float | None = None, mode: str | None = None) -> tuple[float, str]:
    """更新 Offset 值與/或模式(upsert 至 system_settings)。"""
    if mode is not None and mode not in VALID_MODES:
        raise ValueError(f"mode 必須是 {VALID_MODES}")
    now = datetime.now(timezone.utc)
    with db_session() as db:
        def upsert(key: str, val) -> None:
            row = db.query(SystemSetting).filter(SystemSetting.key == key).one_or_none()
            if row is None:
                db.add(SystemSetting(key=key, value=str(val), updated_at=now))
            else:
                row.value = str(val)
                row.updated_at = now

        if value is not None:
            upsert(OFFSET_KEY, round(float(value), 3))
        if mode is not None:
            upsert(MODE_KEY, mode)
    return get_offset()


def offset_info() -> dict:
    """Dashboard 右上角資訊 + 校正說明。"""
    s = get_settings()
    value, mode = get_offset()
    return {
        "mode": mode,
        "value": value,
        "analysis_source": s.analysis_source_label,
        "trading_broker": s.trading_broker_label,
        "applied_to": ["entry", "stop_loss", "targets"],
        "auto_available": False,  # 目前無 TMGM 即時價來源,auto 僅保留 UI
        "formula": "TMGM = TwelveData + Offset",
        "note": ("分析、K棒、EMA/RSI/MACD/ATR、支撐壓力、趨勢全部使用 TwelveData;"
                 "僅劇本進場/停損/停利價套用 Offset 校正為 TMGM 掛單價"),
    }


def apply_offset_to_result(result: dict) -> dict:
    """回傳套用當前 Offset 的結果副本。

    只修改 long/short 劇本的 resolved_prices(entry/stop/target 實際數字);
    current_price、key_levels、指標、結構等分析欄位一律保持 TwelveData 原值。
    """
    value, _ = get_offset()
    out = copy.deepcopy(result)
    out["offset_info"] = offset_info()
    if not value:
        return out
    for key in ("long_scenario", "short_scenario"):
        sc = out.get(key) or {}
        for lv in (sc.get("resolved_prices") or {}).values():
            if "price_low" in lv and lv["price_low"] is not None:
                lv["price_low"] = round(lv["price_low"] + value, 2)
            if "price_high" in lv and lv["price_high"] is not None:
                lv["price_high"] = round(lv["price_high"] + value, 2)
            lv["offset_applied"] = value
    return out
