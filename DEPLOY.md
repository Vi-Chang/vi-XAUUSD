# 部署設定(Zeabur)

線上網址:https://vi-xauusd.zeabur.app/

## AI 分析層環境變數(V2,供應商可切換)

所有 LLM 呼叫都走後端統一模組 `app/llm/client.py`,**API Key 只存在於環境變數,
絕不寫死在程式碼、絕不出現在前端 bundle**。

### 模式 A:Gemini 免費層(預設,API 成本 $0)

| 變數 | 值 | 說明 |
|---|---|---|
| `LLM_ENABLED` | `true` | AI 層總開關 |
| `LLM_PROVIDER` | `gemini` | 預設值,可不設 |
| `LLM_MODEL` | `gemini-3.5-flash` | 預設值,可不設(`gemini-2.5-flash` 已對新 Key 停供) |
| `GEMINI_API_KEY` | `AIza...` | **必填**。到 [Google AI Studio](https://aistudio.google.com/apikey) 免費申請 |

Gemini 免費層限制:**10 RPM / 250 次每日**。系統內建保護(超限自動退回純規則引擎,不會報錯白屏):

| 變數 | 預設 | 說明 |
|---|---|---|
| `LLM_RPM_LIMIT` | `8` | 每分鐘呼叫上限(滑動視窗;超過先排隊,排太久友善拒絕) |
| `LLM_DAILY_CALL_LIMIT` | `200` | 每日呼叫上限(預留餘裕);超過當日停用 AI、隔日自動恢復 |
| `LLM_DAILY_BUDGET_USD` | `3.0` | 費用斷路器(只對價格表內的付費模型有效;免費層恆為 $0) |
| `LLM_CACHE_MINUTES` | `45` | 輸入指紋相同(盤面無實質變化)直接重用舊結果,不重打 API |

另有 429 指數退避重試(1s → 2s → 4s,最多 3 次),重試耗盡回傳繁中友善訊息。

### 模式 B:OpenAI 相容端點(OpenAI / Groq / DeepSeek / OpenRouter)

**只改環境變數即可切換,不動程式碼:**

| 變數 | 範例 |
|---|---|
| `LLM_PROVIDER` | `openai_compatible` |
| `LLM_BASE_URL` | OpenAI:`https://api.openai.com/v1`;Groq:`https://api.groq.com/openai/v1`;DeepSeek:`https://api.deepseek.com/v1`;OpenRouter:`https://openrouter.ai/api/v1` |
| `LLM_API_KEY` | 該供應商的 API Key |
| `LLM_MODEL` | 例:`gpt-4o-mini` / `llama-3.3-70b-versatile` / `deepseek-chat` |

> 付費模型的費用斷路器要生效,需在 `app/llm/usage.py` 的 `PRICING_PER_M`
> 表中有該模型價格;表中沒有的模型成本記為 $0(僅次數保護)。

## 存取控制 / 管理權限(Phase 1 安全性)

會改狀態或產生成本的**寫入端點**(分析觸發、老師帶單、offset、持倉操作)受管理權限保護。

| 變數 | 說明 |
|---|---|
| `APP_ENV` | `development`/`test`/`production`;未知/空 → `production`。**決定 fail-open/closed,不靠 mock 判斷** |
| `ADMIN_TOKEN` | 管理 token(走環境變數,勿硬編碼)。產生方式:`openssl rand -hex 32` |
| `ALLOW_UNAUTHENTICATED_MUTATIONS` | 顯式逃生門(預設 false);production 也放行未認證寫入,僅特例使用 |
| `ADMIN_SESSION_TTL_MINUTES` / `MAX_ADMIN_SESSIONS` | session 有效時間 / 上限(防記憶體 DoS) |
| `ADMIN_LOGIN_MAX_ATTEMPTS` / `ADMIN_LOGIN_WINDOW_SECONDS` | 登入防暴力(滑動視窗) |
| `ANALYSIS_RUN_COOLDOWN_SECONDS` | `/api/analysis/run` 手動觸發冷卻(預設 20s) |

行為:
- **`APP_ENV=production` 未設 `ADMIN_TOKEN` → 所有寫入端點回 503,`/health/ready` 回 not-ready(reason `admin_token_missing`),啟動時記 CRITICAL log。** 即使 `MOCK_DATA_MODE=true` 也不放行。**部署前務必設定 `ADMIN_TOKEN`**,否則儀表板新增/修改全部停用。
- 未設 token 只在 `development`/`test`(或顯式 `ALLOW_UNAUTHENTICATED_MUTATIONS=true`)才放行。
- 自動化 / curl:header 帶 `X-Admin-Token: <token>`(constant-time 比對;不受 Origin 檢查限制)。
- 瀏覽器:提示輸入 token → 換取 HttpOnly + SameSite=Strict(+ production `Secure`)+ `Path=/` + `Max-Age` 的 session cookie(永久 token 不進 HTML/JS/URL/localStorage、不回傳、不寫 log)。
- **CSRF**:session-cookie 認證的 mutation 額外驗證 `Origin`/`Referer` 同源(跨站或缺失 → 403);header-token 路徑不受此限(保留 curl 用法)。WebSocket 同源檢查(跨站 Origin 拒絕)。未加寬鬆 CORS。
- 讀取端點(dashboard、health、K 棒、分析結果)維持公開。
- **`production` 關閉 `/docs`、`/redoc`、`/openapi.json`**(縮小管理端點可探測面);dev/test 保留。

安全標頭:`Content-Security-Policy`(`script-src 'self'`,無 inline script;inline 事件已改事件委派)、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`。

> ⚠️ **多 worker 警告**:session、rate-limit、single-flight 皆為**單一行程內記憶體**。若未來以多 worker(如 `uvicorn --workers N` / gunicorn)部署,這些狀態不跨 worker 共享 —— session 會因 worker 不同而失效、rate-limit 與 single-flight 去重會失準。多 worker 需改用共享儲存(Redis 等)。目前部署維持**單 worker**。

> 前端 XSS 防護:所有動態 HTML 經單一 `escape.js`(`h`` 預設跳脫、`SafeHtml` 型別、`joinSafe`;`trusted()` 僅限程式碼內字面值)。本機開發 `scripts/dev_server.py` 已設 `APP_ENV=development`。

## 公開 / 私人資料邊界(隱私 A+B)

公開黃金市場分析維持開放;個人資料受保護。

- **公開(免登入)**:市場分析 dashboard —— 價格、圖表、市場狀態、技術指標、市場結構、關鍵價位/FVG、事件風險、規則引擎與 AI **市場分析**、公開多空劇本。
  - `GET /api/analysis/latest` 回傳**公開投影**(單一集中 allowlist:`app/services/public_view.py`),移除所有私人欄位;**無副作用**(匿名或已登入的 GET 都不觸發 LLM/provider/新分析/Telegram;無快取時唯讀 DB,再無則回「尚無分析」)。只有受保護的 `POST /api/analysis/run` 或排程可產生新分析。
  - 公開 WebSocket:`/ws`(public alias)—— 只傳公開投影。
- **私人(需 admin session/header)**:`GET /api/accounts`、`/api/accounts/comparison`、`/api/positions`、`/api/behavior/flags`、`/api/mentor/history`、`/api/mentor/signals`,以及私人 WebSocket `/ws/private`(須有效 session cookie + 同源;session 過期即停止傳送)。
  - 未登入回固定 401,不洩露私人欄位是否存在。
  - 前端:未登入時私人面板顯示「🔒 私人資料,登入後查看」(不殘留舊 DOM);單一共享登入流程(多個 401 只跳一次);登入後重載私人面板 + 連 `/ws/private`;登出/過期清除私人 DOM 並關閉私人 WS。永久 token 不進 localStorage/sessionStorage/URL/HTML/cookie。
- **隱私不變式**:公開 payload 以 allowlist 建構(新增欄位預設不公開),並有遞迴 key 斷言禁止 `position_management`/`mentor_comparison`/`trading_coach`/`lot_size`/`pnl`/`account`/`behavior_flags`/`note` 等私人 key。公開 AI 文字為市場分析(生成時未餵入個人持倉/老師資料);決策以「市場層 `market_decision`」呈現,不洩露持倉 MANAGE 覆寫。
- **舊資料(text-level leakage)防線**:公開投影有版本戳記 `privacy_boundary_version`。只有此版 position-free pipeline 產生的分析才可公開 AI/summary/mistake 等**自由文字**;舊資料(無戳記或缺 `market_decision`)一律回 `{available:false, reason:"analysis_refresh_required"}`,**不** fallback 舊 decision、不做關鍵字清洗。投影任何例外/型別錯誤一律 fail-closed(回安全 envelope,絕不外送原始 full payload;log 只含錯誤類別 + version/id)。
- **首次部署**:privacy 版本上線後,舊分析不公開;排程產生第一筆新版分析後公開 dashboard 自動恢復。匿名 GET 不會為刷新而觸發分析;admin 可經受保護 `POST /api/analysis/run` 立即刷新。UI 顯示「分析格式已更新,等待下一次排程」(不揭露內部版本/安全細節)。
- **`privacy_boundary_version` 升版規則**:此戳記(`app/services/public_view.py`,單一真實來源)代表「公開自由文字的資料來源與 AI snapshot 已通過隱私審查」。**下列任一改變都必須重新審查並 +1**:(1) AI snapshot/input fields、(2) public allowlist、(3) `market_decision` 生成位置、(4) summary/mistake 資料來源、(5) private/public projection、(6) legacy fallback 政策。升版後,舊戳記的既有分析自動停止公開(fail-closed),直到排程/管理員產生新版。`tests/test_privacy_boundary.py` 的 invariant test 會確認常數、pipeline 蓋章、schema 欄位同源(禁止硬編碼三份數字)。

## 健康檢查端點

| 端點 | 用途 |
|---|---|
| `GET /health` | 綜合監控(保留原有欄位 + readiness/監控摘要),供 UptimeRobot 等 |
| `GET /health/live` | Liveness:行程存活即 200(外部 provider 暫時失敗不影響) |
| `GET /health/ready` | Readiness:能否提供新鮮分析;未就緒回 503。**週末休市視為就緒**(不誤判 stale) |

Readiness `reason` 值:`ok` / `market_closed` / `api_only`(刻意關排程)/ `warming_up` / `no_data` / `data_stale` / `component_down` / `scheduler_disabled`(誤設)/ `admin_token_missing`(production 缺 token,優先於休市,不被 market_closed 掩蓋)。所有 readiness 判定只讀本地狀態,不同步呼叫外部 API;timestamps 一律 UTC-aware;不輸出帳號/token/DB URL/例外訊息/內部路徑。

## Zeabur 更新環境變數的注意事項

`zeabur variable env -f <file>` 是**整組覆蓋**:每次更新必須包含完整變數集,
不能只給要改的那幾個(否則其餘變數會被清掉)。

## 部署流程

1. 停掉本機 dev server(否則 `xauusd_dev.db` 被鎖住)。
2. 把 `.env`、`*.db`、`mentor_trades*.json` 暫移出專案目錄(Zeabur 會上傳整個工作目錄)。
3. `npx zeabur@latest deploy -i=false --project-id 6a5b6a73b2014c9217fe6752 --service-id 6a5b6aa7b2014c9217fe6765 --environment-id 6a5b6a73b0b7a4abeb4e4d89`
4. 檔案移回,以 `npx zeabur@latest deployment get -i=false --service-id … --env-id … --json`
   等 `status == "RUNNING"`(**不要**用 /health 判斷 —— 滾動更新期間舊容器也回 200)。
5. `curl https://vi-xauusd.zeabur.app/api/analysis/latest` 確認新欄位存在。
