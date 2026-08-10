"""存取控制(Phase 1 安全性):保護會改狀態/產生成本的寫入端點。

設計目標(依需求約束):
- 管理 token 走環境變數(ADMIN_TOKEN),禁止硬編碼、禁止回傳給訪客。
- token 從 request header(X-Admin-Token)傳入,constant-time 比對。
- 未設定 token:mock/開發模式放行;正式(mock=false)一律拒絕(fail-closed)。
- 瀏覽器操作:以 token 換取短效 HttpOnly + SameSite=Strict session cookie,
  永久 token 不進 HTML/JS/URL/localStorage,也不回傳明文。
- 回應與例外不得洩露 token、環境變數或內部細節。

單 worker 假設(本輪不引入 Redis):session 與節流狀態存行程內記憶體。
"""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, Response

from app.config import get_settings

logger = logging.getLogger(__name__)

HEADER_NAME = "X-Admin-Token"

# ── 行程內 session store(session_id → 到期 UTC 時間)──
_sessions: dict[str, datetime] = {}
_sessions_lock = threading.Lock()

# ── 節流(端點 key → 上次受理的 monotonic 秒)──
_last_hit: dict[str, float] = {}
_rate_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_configured() -> str:
    return (get_settings().admin_token or "").strip()


def _is_dev_open() -> bool:
    """未設定 token 時是否放行:只有 mock/開發模式放行,正式一律 fail-closed。"""
    s = get_settings()
    return bool(s.mock_data_mode)


def token_matches(provided: str | None) -> bool:
    """constant-time 比對;未提供或未設定 token 一律 False。"""
    token = _token_configured()
    if not token or not provided:
        return False
    return hmac.compare_digest(provided.strip(), token)


# ── 瀏覽器 session ──────────────────────────────────────────

def _purge_expired() -> None:
    now = _now()
    with _sessions_lock:
        for sid in [k for k, exp in _sessions.items() if exp <= now]:
            _sessions.pop(sid, None)


def create_session() -> tuple[str, int]:
    """建立 session,回傳 (session_id, 有效秒數)。呼叫端須先驗過 token。"""
    s = get_settings()
    ttl = max(1, s.admin_session_ttl_minutes) * 60
    sid = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[sid] = _now() + timedelta(seconds=ttl)
    return sid, ttl


def session_valid(sid: str | None) -> bool:
    if not sid:
        return False
    _purge_expired()
    with _sessions_lock:
        exp = _sessions.get(sid)
    return exp is not None and exp > _now()


def destroy_session(sid: str | None) -> None:
    if not sid:
        return
    with _sessions_lock:
        _sessions.pop(sid, None)


def set_session_cookie(response: Response, sid: str, max_age: int) -> None:
    s = get_settings()
    response.set_cookie(
        key=s.admin_session_cookie, value=sid, max_age=max_age,
        httponly=True, samesite="strict",
        secure=not s.mock_data_mode,   # 正式(https)才要求 secure;本機 http 開發不強制
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(get_settings().admin_session_cookie, httponly=True,
                           samesite="strict")


# ── 授權判定 ────────────────────────────────────────────────

def is_authorized(request: Request) -> bool:
    """header token(自動化/curl)或有效 session cookie(瀏覽器)其一即可。"""
    if token_matches(request.headers.get(HEADER_NAME)):
        return True
    return session_valid(request.cookies.get(get_settings().admin_session_cookie))


async def require_admin(request: Request) -> None:
    """FastAPI 依賴:保護寫入端點。

    - 已設定 token:必須帶對的 header token 或有效 session,否則 401。
    - 未設定 token:mock/開發放行;正式回 503(fail-closed,不默認放行)。
    回應訊息一律為固定字串,不含 token/環境變數/內部例外。
    """
    if _token_configured():
        if is_authorized(request):
            return
        raise HTTPException(status_code=401, detail="需要管理權限:請提供有效的管理憑證。")
    if _is_dev_open():
        return
    raise HTTPException(
        status_code=503,
        detail="伺服器尚未設定管理權限(ADMIN_TOKEN),已停用寫入端點。")


def admin_status(request: Request) -> dict:
    """給前端判斷登入狀態用(不洩露 token 是否存在的細節之外的資訊)。"""
    return {
        "auth_required": bool(_token_configured()) or not _is_dev_open(),
        "authenticated": is_authorized(request),
    }


# ── 節流 / cooldown ─────────────────────────────────────────

def rate_limit(key: str, cooldown_seconds: float) -> None:
    """簡易冷卻:同一 key 在冷卻秒數內重複呼叫 → 429(附 Retry-After)。"""
    if cooldown_seconds <= 0:
        return
    now = time.monotonic()
    with _rate_lock:
        last = _last_hit.get(key)
        if last is not None and now - last < cooldown_seconds:
            retry = max(1, int(cooldown_seconds - (now - last)))
            raise HTTPException(status_code=429,
                                detail=f"請求太頻繁,請 {retry} 秒後再試。",
                                headers={"Retry-After": str(retry)})
        _last_hit[key] = now


def reset_state_for_tests() -> None:
    """測試用:清空 session 與節流狀態。"""
    with _sessions_lock:
        _sessions.clear()
    with _rate_lock:
        _last_hit.clear()
