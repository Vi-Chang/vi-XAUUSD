"""完整分析 single-flight:同一行程內並發的「相容」分析請求只實際執行一次。

相容性(key):以「會影響輸出/成本」的參數為 key —— symbol 與 cached_only。
- symbol 不同 → 不同分析,不得共用。
- cached_only 不同 → 一個是降級快取、一個打即時 API,結果與成本不同,不得共用。
- trigger(manual/event/timed)不影響「計算結果」,只是稽核標籤 → 不納入 key。
  收斂發生時,實際跑的那一次以「發起者的 trigger」寫入 AnalysisRun(如實記錄單一次執行);
  其餘等待者取得同一結果。API 回應不含 trigger,不會對使用者錯標。

穩健性:
- 等待有逾時(逾時拋 TimeoutError,核心以 shield 續跑不被單一等待者取消連累)。
- 例外傳播給所有等待者;並以 done callback 回收例外,避免
  "Task exception was never retrieved"(即使所有等待者都逾時離開)。
- 完成後清除該 key 的 task 引用,下次呼叫可正常重試。

單 worker 假設(本輪不引入 Redis / 多 worker 協調)。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class SingleFlight:
    """把並發呼叫(依 key)收斂為單次執行的協調器(逐 event loop 建立鎖,測試安全)。"""

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inflight: dict[str, asyncio.Task] = {}
        self.run_count = 0
        self.collapsed_count = 0

    def _lock_for_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock = asyncio.Lock()
            self._loop = loop
            self._inflight = {}
        return self._lock

    async def _wrapped(self, factory):
        self.run_count += 1
        return await factory()

    def _reap(self, key: str, task: asyncio.Task) -> None:
        # 完成後:清除本 key 的引用(若仍指向自己)+ 回收例外(標記已取得)
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        if not task.cancelled():
            task.exception()   # 取得例外,避免 "never retrieved" 警告(成功則回 None)

    async def run(self, factory, *, timeout: float, key: str = "default"):
        """執行 factory();若同 key 已有進行中的執行,改為等待其結果。

        factory: 無參 callable,回傳 coroutine(實際的分析工作)。
        timeout: 等待進行中執行的最長秒數;逾時拋 asyncio.TimeoutError。
        key: 相容性鍵;不同 key 各自獨立執行,不互相共用。
        """
        lock = self._lock_for_loop()
        async with lock:
            task = self._inflight.get(key)
            if task is None or task.done():
                task = asyncio.create_task(self._wrapped(factory))
                self._inflight[key] = task
                task.add_done_callback(lambda t, k=key: self._reap(k, t))
            else:
                self.collapsed_count += 1
        # shield:等待者逾時/被取消不會連累仍在跑的核心分析,其他等待者仍取得結果
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            logger.warning("analysis single-flight wait timed out after %.0fs "
                           "(key=%s;核心分析仍在背景進行,本次請求放棄等待)", timeout, key)
            raise

    def reset_for_tests(self) -> None:
        self._inflight = {}
        self.run_count = 0
        self.collapsed_count = 0


# 全域唯一的完整分析 single-flight(手動、排程、首載共用同一協調器)
analysis_flight = SingleFlight()


async def run_analysis_shared(provider, *, trigger: str, tick=None,
                              cached_only: bool = False, symbol: str = "XAUUSD",
                              timeout: float | None = None):
    """核心 run_analysis 的 single-flight 入口。所有分析呼叫點一律走這裡。

    key = symbol + cached_only(相容性);trigger 僅為稽核標籤,不納入 key。
    """
    from app.config import get_settings
    from app.services.analysis_service import run_analysis
    if timeout is None:
        timeout = float(get_settings().analysis_lock_timeout_seconds)

    key = f"{symbol}:{int(bool(cached_only))}"

    def factory():
        return run_analysis(provider, trigger=trigger, tick=tick,
                            cached_only=cached_only, symbol=symbol)

    return await analysis_flight.run(factory, timeout=timeout, key=key)
