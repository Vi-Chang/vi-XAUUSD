"""TMGM 價格校正(Price Offset)— 以「資料源」為 key 的對應表(P0 修復)。

設計原則:
- 分析資料永遠來自各 provider 原值,本模組不介入分析。
- Offset 只在輸出邊界套用到劇本的進場/停損/停利價:TMGM = 分析源價 + Offset。
- **每個資料源各自一筆 offset**(twelve_data / capital_com / oanda …),
  存 system_settings key `price_offset:{source}`,內容 JSON:
  {"broker": "TMGM", "value": <num>, "updated_at": <iso>}。
- **Fail-safe(P0 核心)**:當前 active_source 查無 offset、或 updated_at 超過
  OFFSET_MAX_AGE_HOURS(預設 24h)→ NO-SIGNAL:剝除所有 Entry/SL/TP、
  決策降級並顯示「offset 未校準,暫停出訊」。
  寧可不出訊號,也不可用未知偏移的價位出訊號;禁止用 0 當預設值繼續出訊。
- mock(開發模式)豁免:視為已校準、offset=0。
- 資料源切換時寫 log:舊源、新源、套用的 offset 值。
"""
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone

from app.config import get_settings
from app.db.models import SystemSetting
from app.db.session import db_session

logger = logging.getLogger(__name__)

LEGACY_KEY = "price_offset"          # 舊制單一全域值(遷移用,視為 twelve_data 的)
MODE_KEY = "offset_mode"
VALID_MODES = ("manual", "auto")
BROKER = "TMGM"

_last_active_source: str | None = None   # 資料源切換偵測(log 用)


def _source_key(source: str) -> str:
    return f"price_offset:{source}"


def active_source() -> str:
    """當前分析使用的報價源(= 面板顯示的資料源)。"""
    try:
        from app.services.scheduler import state
        latest = state.latest_result or {}
        src = (latest.get("current_price") or {}).get("provider")
        if src:
            return src
        if state.provider is not None:
            return state.provider.name
    except Exception:  # noqa: BLE001
        pass
    s = get_settings()
    if s.mock_data_mode:
        return "mock"
    return s.primary_provider if s.primary_provider not in ("auto", "") else "twelve_data"


def _read_entry(source: str) -> dict | None:
    """讀取某來源的 offset 紀錄;含舊制遷移(legacy → twelve_data)。"""
    with db_session() as db:
        row = db.query(SystemSetting).filter(
            SystemSetting.key == _source_key(source)).one_or_none()
        if row is not None:
            try:
                return json.loads(row.value)
            except (TypeError, ValueError):
                return None
        if source == "twelve_data":   # 舊制單一值視為 twelve_data 的校準
            legacy = db.query(SystemSetting).filter(
                SystemSetting.key == LEGACY_KEY).one_or_none()
            if legacy is not None:
                try:
                    return {"broker": BROKER, "value": float(legacy.value),
                            "updated_at": (legacy.updated_at.isoformat()
                                           if legacy.updated_at else None)}
                except (TypeError, ValueError):
                    return None
    return None


def get_mode() -> str:
    s = get_settings()
    try:
        with db_session() as db:
            row = db.query(SystemSetting).filter(SystemSetting.key == MODE_KEY).one_or_none()
            if row is not None and row.value in VALID_MODES:
                return row.value
    except Exception:  # noqa: BLE001
        pass
    return s.offset_mode


def get_offset_for(source: str) -> dict:
    """回傳 {source, broker, value, updated_at, calibrated, reason}。

    calibrated=False 的情況:無紀錄、紀錄損壞、或超過時效。mock 恆為已校準 0。
    """
    s = get_settings()
    if source == "mock":
        # 開發模式豁免:恆為已校準(不受時效限制),沿用已存值或 0
        try:
            entry = _read_entry(source)
        except Exception:  # noqa: BLE001
            entry = None
        return {"source": source, "broker": BROKER,
                "value": round(float(entry["value"]), 3) if entry and entry.get("value") is not None else 0.0,
                "updated_at": (entry or {}).get("updated_at"),
                "calibrated": True, "reason": "mock 開發模式豁免"}
    entry = None
    try:
        entry = _read_entry(source)
    except Exception as exc:  # noqa: BLE001
        logger.warning("read offset entry failed for %s: %s", source, exc)
    if entry is None or entry.get("value") is None:
        return {"source": source, "broker": BROKER, "value": None, "updated_at": None,
                "calibrated": False,
                "reason": f"資料源 {source} 尚未校準 offset(查無紀錄)"}
    updated_at = entry.get("updated_at")
    if updated_at:
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(updated_at)).total_seconds() / 3600
            if age_h > s.offset_max_age_hours:
                return {"source": source, "broker": entry.get("broker", BROKER),
                        "value": float(entry["value"]), "updated_at": updated_at,
                        "calibrated": False,
                        "reason": (f"offset 已超過時效({age_h:.0f}h > "
                                   f"{s.offset_max_age_hours}h),需重新校準")}
        except (TypeError, ValueError):
            pass
    return {"source": source, "broker": entry.get("broker", BROKER),
            "value": round(float(entry["value"]), 3), "updated_at": updated_at,
            "calibrated": True, "reason": ""}


def get_offset() -> tuple[float, str]:
    """相容介面:回傳 (active source 的 offset 值或 0, 模式)。

    注意:fail-safe 判斷請用 get_offset_for();此函數僅供顯示相容。
    """
    info = get_offset_for(active_source())
    return (info["value"] if info["calibrated"] and info["value"] is not None else 0.0,
            get_mode())


def set_offset(value: float | None = None, mode: str | None = None,
               source: str | None = None) -> dict:
    """更新 offset(預設寫入當前 active_source 的紀錄)與/或全域模式。"""
    if mode is not None and mode not in VALID_MODES:
        raise ValueError(f"mode 必須是 {VALID_MODES}")
    src = source or active_source()
    now = datetime.now(timezone.utc)
    with db_session() as db:
        def upsert(key: str, val: str) -> None:
            row = db.query(SystemSetting).filter(SystemSetting.key == key).one_or_none()
            if row is None:
                db.add(SystemSetting(key=key, value=val, updated_at=now))
            else:
                row.value = val
                row.updated_at = now

        if value is not None:
            upsert(_source_key(src), json.dumps({
                "broker": BROKER, "value": round(float(value), 3),
                "updated_at": now.isoformat()}))
            logger.info("OFFSET_SET source=%s value=%s", src, round(float(value), 3))
        if mode is not None:
            upsert(MODE_KEY, mode)
    return offset_info()


def offset_info() -> dict:
    """Dashboard 資訊 + 校正狀態(含 fail-safe 警示)。"""
    global _last_active_source
    src = active_source()
    entry = get_offset_for(src)
    if _last_active_source is not None and _last_active_source != src:
        logger.info("OFFSET_SOURCE_SWITCH old=%s new=%s applied_offset=%s calibrated=%s",
                    _last_active_source, src, entry["value"], entry["calibrated"])
    _last_active_source = src
    return {
        "mode": get_mode(),
        "value": entry["value"] if entry["calibrated"] else None,
        "analysis_source": src,                       # 動態,不再寫死
        "trading_broker": entry["broker"],
        "calibrated": entry["calibrated"],
        "calibration_warning": entry["reason"],
        "updated_at": entry["updated_at"],
        "applied_to": ["entry", "stop_loss", "targets"],
        "auto_available": False,
        "formula": f"{entry['broker']} = {src} + Offset",
        "note": (f"分析與 K 棒使用各 provider 原值;僅劇本進場/停損/停利價套用 "
                 f"Offset({entry['broker']} − {src})校正為掛單價"),
    }


def _strip_all_scenario_prices(out: dict, warning: str) -> None:
    """NO-SIGNAL:不輸出任何 Entry/SL/TP,決策降級(P0 fail-safe)。"""
    for key in ("long_scenario", "short_scenario"):
        sc = out.get(key)
        if not sc:
            continue
        sc["resolved_prices"] = {}
        sc["entry_zone_id"] = None
        sc["stop_loss_id"] = None
        sc["invalidation_id"] = None
        sc["target_ids"] = []
        sc["risk_reward"] = []
        if sc.get("status") in ("PREPARE", "TRIGGERED"):
            sc["status"] = "WATCH"
    d = out.get("decision") or {}
    if d.get("action") not in ("NO_TRADE",):
        d["action"] = "WATCH"
        d["confidence_grade"] = "X"
        d["evidence_score"] = 0
        d["reason"] = warning
        out["decision"] = d
    out["no_signal"] = True


def apply_offset_to_result(result: dict) -> dict:
    """輸出邊界:套用當前資料源的 offset;未校準 → NO-SIGNAL。"""
    out = copy.deepcopy(result)
    info = offset_info()
    out["offset_info"] = info

    if not info["calibrated"]:
        warning = (f"⚠ offset 未校準({info['calibration_warning']}),暫停出訊:"
                   f"請到右上角校正 {info['trading_broker']} − {info['analysis_source']} 的價差")
        logger.warning("OFFSET_NOT_CALIBRATED source=%s → NO-SIGNAL",
                       info["analysis_source"])
        _strip_all_scenario_prices(out, warning)
        return out

    value = info["value"] or 0.0
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
