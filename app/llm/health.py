"""LLM 健康狀態(僅供監控/health 端點)。

只記錄「最後成功時間、最後錯誤時間、最後錯誤類別名」。
嚴禁記錄 prompt、API key、回應內容或完整例外訊息 —— 只存例外的 class 名稱。
單 worker、行程內記憶體。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_last_success_at: datetime | None = None
_last_error_at: datetime | None = None
_last_error_type: str | None = None   # 只存例外 class 名稱,不存訊息內容


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_success() -> None:
    global _last_success_at
    with _lock:
        _last_success_at = _now()


def record_error(exc: BaseException) -> None:
    global _last_error_at, _last_error_type
    with _lock:
        _last_error_at = _now()
        _last_error_type = type(exc).__name__   # 僅類別名,不含敏感訊息


def snapshot() -> dict:
    with _lock:
        return {
            "last_success_at": _last_success_at.isoformat() if _last_success_at else None,
            "last_error_at": _last_error_at.isoformat() if _last_error_at else None,
            "last_error_type": _last_error_type,
        }


def reset_for_tests() -> None:
    global _last_success_at, _last_error_at, _last_error_type
    with _lock:
        _last_success_at = _last_error_at = _last_error_type = None
