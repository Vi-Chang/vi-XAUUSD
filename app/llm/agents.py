"""V2 四 Agent:Macro / Technical / Sentiment 分析師 + Decision 決策引擎。

Token 紀律(對所有 Agent 一體適用,寫死在 system prompt):
- 只用 user JSON 內的數字;禁止自行計算指標、禁止憑記憶補行情。
- 資料為 null = 缺資料,誠實說缺,不腦補。
- 輸出繁體中文、短句;不重複輸入裡已有的數字清單。
"""
from __future__ import annotations

import asyncio

from app.llm.client import call_json
from app.schemas.ai import ANALYST_SCHEMA, DECISION_SCHEMA, AnalystView

_COMMON = ("你只能使用 user 提供 JSON 內的資料;禁止自行計算指標、禁止引用資料中"
           "不存在的數字、禁止憑記憶補行情。欄位為 null 即為缺資料,須降低 strength "
           "並在 key_points 註明。輸出繁體中文;key_points 最多 4 條、每條 30 字內;"
           "one_line 為一句話結論。strength 為 0-100 的傾向強度。")

MACRO_SYSTEM = (
    "你是 XAUUSD 黃金巨集面分析師。依據 cross(DXY 美元指數、美十年債殖利率、VIX)"
    "與 event(事件行事曆)評估巨集面對黃金的多空傾向:美元走強/實質利率上行對金價偏壓,"
    "反之偏撐;重大事件臨近提高不確定性。" + _COMMON)

TECHNICAL_SYSTEM = (
    "你是 XAUUSD 技術面分析師。依據 tf(Daily→4H→1H→15M 的趨勢/EMA 排列/MACD/RSI/ADX,"
    "由大到小推理)、state(市場狀態)、levels(支撐壓力區)、fvg(未回補缺口)、"
    "bias(規則引擎證據)評估技術面多空傾向。大週期優先,小週期定時機。" + _COMMON)

SENTIMENT_SYSTEM = (
    "你是 XAUUSD 市場情緒分析師。目前無新聞資料源,只能依 VIX 水位、事件臨近程度"
    "(event.impact / time_risk)與追價旗標(bias.chase)評估情緒面;"
    "資料有限時 strength 靠近 50 並如實說明依據薄弱。" + _COMMON)

DECISION_SYSTEM = (
    "你是 XAUUSD 首席決策官,整合三位分析師意見(user.analysts)與市場快照(user.snapshot),"
    "輸出單一、不自相矛盾的交易策略。鐵則:\n"
    "1) 價位欄位(entry_id/stop_loss_id/tp1~tp3_id)只能填 snapshot.levels 或 snapshot.fvg "
    "中存在的 id,禁止自創數字;找不到合適價位就填 null。\n"
    "2) action.type 只有 Buy/Sell/Wait。禁止只說觀望:Wait 時 wait_condition 必填"
    "(具體等什麼),且任何情況 next_trigger 必填 = 下一個高勝率進場條件"
    "(引用具體 id + K 棒收盤條件,例:15分K收盤站上 RES_ZONE_01 上緣則轉多)。\n"
    "3) Buy 的停損 id 區間必須低於進場 id 區間、TP 依序更高;Sell 相反。程式會驗證,"
    "違反會被退回。\n"
    "4) win_rates 兩者合計 100(這是依當前證據的主觀評估機率,非歷史統計)。\n"
    "5) scenarios 恰好 3 個(主劇本/次劇本/黑天鵝),probability_pct 合計 100,"
    "每個附具體 trigger 與 plan。\n"
    "6) snapshot.gates.event_lockout=true 時 action 必須 Wait(重大數據鎖定,不可推翻)。\n"
    "7) 持倉存在(snapshot.position.has=true)時,策略須以管理現有持倉為優先。\n"
    "8) rationale 100 字內;risk_warning 說最實際的風險;one_liner 一句話結論。\n"
    "9) confidence.score 0-100,factors 列 2-4 個影響信心的因素。\n"
    "10) 不重複輸入數字、不解釋你在做什麼,直接給結果。繁體中文。")


async def run_analysts(snapshot: dict) -> tuple[dict[str, AnalystView], float]:
    """三位分析師並行;單一失敗以 NEUTRAL 代替(不擋全局)。回傳 (views, 成本)。"""

    async def one(name: str, system: str):
        return await call_json(system=system, user_payload=snapshot,
                               schema=ANALYST_SCHEMA, max_tokens=2000)

    tasks = {"macro": one("macro", MACRO_SYSTEM),
             "technical": one("technical", TECHNICAL_SYSTEM),
             "sentiment": one("sentiment", SENTIMENT_SYSTEM)}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    views: dict[str, AnalystView] = {}
    cost = 0.0
    for name, res in zip(tasks.keys(), results):
        if isinstance(res, Exception):
            views[name] = AnalystView(one_line=f"{name} 分析失敗,以中性代替")
        else:
            data, c = res
            cost += c
            views[name] = AnalystView(**data)
    return views, cost


async def run_decision(snapshot: dict, analysts: dict[str, AnalystView],
                       feedback: str | None = None) -> tuple[dict, float]:
    """決策引擎;feedback 為守門退回原因(重試時附上)。"""
    payload = {"snapshot": snapshot,
               "analysts": {k: v.model_dump() for k, v in analysts.items()}}
    if feedback:
        payload["validator_feedback"] = f"上一次輸出被程式退回,原因:{feedback}。請修正後重出。"
    # 中文 JSON 的 token 密度高,輸出上限給足(免費層不計費,截斷才是風險)
    return await call_json(system=DECISION_SYSTEM, user_payload=payload,
                           schema=DECISION_SCHEMA, max_tokens=8000)
