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
| `ADMIN_TOKEN` | 管理 token(走環境變數,勿硬編碼)。產生方式:`openssl rand -hex 32` |
| `ADMIN_SESSION_TTL_MINUTES` | 瀏覽器登入 session 有效時間(預設 720 = 12h) |
| `ANALYSIS_RUN_COOLDOWN_SECONDS` | `/api/analysis/run` 手動觸發冷卻(預設 20s) |

行為:
- **正式環境(`MOCK_DATA_MODE=false`)未設定 `ADMIN_TOKEN` → 所有寫入端點回 503**(fail-closed,不默認放行)。**部署前務必在 Zeabur 設定 `ADMIN_TOKEN`**,否則儀表板的新增/修改功能會全部停用。
- 自動化 / curl:在 header 帶 `X-Admin-Token: <token>`(constant-time 比對)。
- 瀏覽器:第一次點寫入操作時會提示輸入 token → 換取 HttpOnly + SameSite=Strict session cookie(永久 token 不進 HTML/JS/URL/localStorage)。
- 讀取端點(dashboard 顯示、health、K 棒、分析結果)維持公開。
- 回應與 log 不含 token、環境變數或內部例外。

另已加入安全標頭:`Content-Security-Policy`(script-src 'self')、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`。

## 健康檢查端點

| 端點 | 用途 |
|---|---|
| `GET /health` | 綜合監控(保留原有欄位 + readiness/監控摘要),供 UptimeRobot 等 |
| `GET /health/live` | Liveness:行程存活即 200(外部 provider 暫時失敗不影響) |
| `GET /health/ready` | Readiness:能否提供新鮮分析;未就緒回 503。**週末休市視為就緒**(不誤判 stale) |

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
