"""存取控制(Phase 1 安全性 + 稽核強化):保護會改狀態/產生成本的寫入端點。

環境判定(不靠 mock_data_mode / hostname / Zeabur 變數 / 行情 provider 推測):
- 由 APP_ENV(development|test|production)決定;未知/空值 → production(fail-closed)。
- token 已設 → 一律強制驗證。
- token 未設 → 僅 development/test(或顯式 ALLOW_UNAUTHENTICATED_MUTATIONS)放行;
  production 一律拒絕,且在 startup/readiness 明確失敗(不等到 mutation 才發現)。

瀏覽器操作:token 換取短效 HttpOnly + SameSite=Strict(+ production Secure)session cookie,
永久 token 不進 HTML/JS/URL/localStorage,也不回傳明文、不寫入 log。
CSRF:session-cookie 認證的 mutation 額外驗證 Origin/Referer 同源;header-token(curl)不受此限。

單 worker 假設(本輪不引入 Redis):session 與節流狀態存行程內記憶體,均有容量上限與過期清理。
"""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response

from app.config import get_settings

logger = logging.getLogger(__name__)

HEADER_NAME = "X-Admin-Token"

# ── 行程內 session store(session_id → 到期 UTC 時間)──
_sessions: dict[str, datetime] = {}
_sessions_lock = threading.Lock()

# ── 節流:cooldown(端點 key → 上次受理 monotonic 秒)+ 滑動視窗(key → 時間戳列)──
_last_hit: dict[str, float] = {}
_attempts: dict[str, list[float]] = {}
_rate_lock = threading.Lock()
_MAX_RATE_KEYS = 512   # 節流字典容量上限(防記憶體 DoS;本專案 key 為固定少數 bucket)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_configured() -> str:
    return (get_settings().admin_token or "").strip()


# ── 環境判定 ────────────────────────────────────────────────

def app_env() -> str:
    return get_settings().app_env   # 已由 validator 正規化為 development/test/production


def is_production() -> bool:
    return app_env() == "production"


def unauthenticated_mutations_allowed() -> bool:
    """未設 token 時是否放行寫入:**僅** development/test。

    production 一律 False(即使 ALLOW_UNAUTHENTICATED_MUTATIONS=true 也不放行);
    該旗標在 production 只被視為組態錯誤(見 production_auth_misconfigured)。
    """
    return app_env() in ("development", "test")


def production_auth_misconfigured() -> tuple[bool, str]:
    """production 的寫入權限組態是否有誤;回傳 (有誤, 原因碼)。

    原因碼不含 token 內容或實際長度,可安全用於 readiness/log。
    """
    if not is_production():
        return False, ""
    s = get_settings()
    if s.allow_unauthenticated_mutations:
        return True, "allow_unauthenticated_mutations_set_in_production"
    tok = _token_configured()
    if not tok:
        return True, "admin_token_missing"
    if len(tok) < max(1, s.min_admin_token_length):
        return True, "admin_token_too_short"   # 只表達「未達門檻」,不洩露實際長度
    return False, ""


def production_token_missing() -> bool:
    """相容介面:production 寫入權限是否組態有誤(缺 token / 太短 / 旗標誤設)。"""
    bad, _why = production_auth_misconfigured()
    return bad


def token_matches(provided: str | None) -> bool:
    """constant-time 比對;未提供或未設定 token 一律 False(長度不同也安全,不早退洩露)。"""
    token = _token_configured()
    if not token or not provided:
        return False
    return hmac.compare_digest(provided.strip(), token)


# ── 瀏覽器 session ──────────────────────────────────────────

def _purge_expired_locked() -> None:
    now = _now()
    for sid in [k for k, exp in _sessions.items() if exp <= now]:
        _sessions.pop(sid, None)


def create_session() -> tuple[str, int]:
    """建立 session,回傳 (session_id, 有效秒數)。呼叫端須先驗過 token。

    session_id 使用 secrets(CSPRNG);超過容量上限時先清過期、必要時淘汰最舊。
    """
    s = get_settings()
    ttl = max(1, s.admin_session_ttl_minutes) * 60
    sid = secrets.token_urlsafe(32)   # 256-bit CSPRNG
    with _sessions_lock:
        _purge_expired_locked()
        cap = max(1, s.max_admin_sessions)
        while len(_sessions) >= cap:
            oldest = min(_sessions, key=lambda key: _sessions[key])  # 淘汰最早到期者
            _sessions.pop(oldest, None)
        _sessions[sid] = _now() + timedelta(seconds=ttl)
    return sid, ttl


def session_valid(sid: str | None) -> bool:
    if not sid:
        return False
    with _sessions_lock:
        _purge_expired_locked()
        exp = _sessions.get(sid)
    return exp is not None and exp > _now()


def destroy_session(sid: str | None) -> None:
    if not sid:
        return
    with _sessions_lock:
        _sessions.pop(sid, None)


def session_count() -> int:
    with _sessions_lock:
        return len(_sessions)


def set_session_cookie(response: Response, sid: str, max_age: int) -> None:
    s = get_settings()
    response.set_cookie(
        key=s.admin_session_cookie, value=sid, max_age=max_age,
        httponly=True, samesite="strict", path="/",
        secure=is_production(),   # production(https)強制 Secure;dev/test 本機 http 才不強制
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(get_settings().admin_session_cookie, httponly=True,
                           samesite="strict", path="/")


# ── CSRF / Origin 防護(僅針對 session-cookie 認證的 mutation)────────────

def _same_site(request: Request) -> bool:
    """Origin/Referer 的 host 需與請求 Host 相符。

    - Origin 存在 → 以 Origin 判定(最可靠)。
    - 無 Origin 但有 Referer → 以 Referer host 判定。
    - 兩者皆無 → 視為不通過(瀏覽器對跨站 POST 一定送 Origin;缺失代表非正常瀏覽器情境,
      此時應改走 header-token 路徑,而非 cookie)。
    """
    host = (request.headers.get("host") or "").split(",")[0].strip().lower()
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    src = origin or referer
    if not src or not host:
        return False
    try:
        parsed = urlsplit(src)
        src_host = (parsed.netloc or "").lower()
    except ValueError:
        return False
    return bool(src_host) and src_host == host


def is_authorized(request: Request) -> tuple[bool, str]:
    """回傳 (是否授權, 方式)。header-token(curl/自動化)不需 CSRF 檢查;
    session-cookie(瀏覽器)需通過 Origin/Referer 同源檢查。"""
    if token_matches(request.headers.get(HEADER_NAME)):
        return True, "header"
    if session_valid(request.cookies.get(get_settings().admin_session_cookie)):
        return True, "session"
    return False, "none"


async def require_admin(request: Request) -> None:
    """FastAPI 依賴:保護寫入端點(fail-closed + CSRF)。

    回應訊息一律固定字串,不含 token/環境變數/內部例外。
    """
    if _token_configured():
        ok, via = is_authorized(request)
        if not ok:
            raise HTTPException(status_code=401, detail="需要管理權限:請提供有效的管理憑證。")
        # CSRF:僅對「會改狀態的方法」+ session-cookie 路徑檢查 Origin 同源。
        # GET/HEAD 為安全且 idempotent,且 SameSite=Strict cookie 已防跨站送出,
        # 故不對一般 GET 要求 Origin(避免誤擋同源 GET fetch,其不帶 Origin header)。
        unsafe = request.method not in ("GET", "HEAD", "OPTIONS")
        if via == "session" and unsafe and not _same_site(request):
            raise HTTPException(status_code=403, detail="來源驗證失敗:請從本站操作。")
        return
    # 未設 token
    if unauthenticated_mutations_allowed():
        return
    raise HTTPException(
        status_code=503,
        detail="伺服器尚未設定管理權限(ADMIN_TOKEN),已停用寫入端點。")


def admin_status(request: Request) -> dict:
    ok, _via = is_authorized(request)
    return {
        "auth_required": bool(_token_configured()) or not unauthenticated_mutations_allowed(),
        "authenticated": ok,
        "app_env": app_env(),
    }


# ── 節流 / cooldown ─────────────────────────────────────────

def _trim_rate_keys_locked(store: dict) -> None:
    """容量保護:字典過大時清掉最舊的 key(本專案 key 為固定少數 bucket,通常不觸發)。"""
    if len(store) > _MAX_RATE_KEYS:
        for k in list(store)[: len(store) - _MAX_RATE_KEYS]:
            store.pop(k, None)


def rate_limit(key: str, cooldown_seconds: float) -> None:
    """簡易冷卻(monotonic clock):同一 key 冷卻秒數內重複 → 429(附 Retry-After)。"""
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
        _trim_rate_keys_locked(_last_hit)


def rate_limit_window(key: str, max_attempts: int, window_seconds: float) -> None:
    """滑動視窗限制(monotonic clock):視窗內超過 max_attempts → 429。

    用於登入端點防暴力破解:允許少量重試,超量才鎖,不會因一次錯誤全域鎖死。
    """
    now = time.monotonic()
    with _rate_lock:
        hits = [t for t in _attempts.get(key, []) if now - t < window_seconds]
        if len(hits) >= max_attempts:
            retry = max(1, int(window_seconds - (now - hits[0])))
            _attempts[key] = hits
            raise HTTPException(status_code=429,
                                detail=f"嘗試次數過多,請 {retry} 秒後再試。",
                                headers={"Retry-After": str(retry)})
        hits.append(now)
        _attempts[key] = hits
        _trim_rate_keys_locked(_attempts)


def reset_state_for_tests() -> None:
    with _sessions_lock:
        _sessions.clear()
    with _rate_lock:
        _last_hit.clear()
        _attempts.clear()
