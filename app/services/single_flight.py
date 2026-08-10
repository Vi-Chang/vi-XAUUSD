"""完整分析 single-flight:同一行程內並發的分析請求只實際執行一次。

問題:排程(structure_l2 觸發、定時保底)與手動 API(/api/analysis/run)、
首載(/api/analysis/latest 於無快取時)都會呼叫核心 run_analysis;彼此可能重疊,
造成同時打行情 provider / LLM、寫入重複 AnalysisRun。

解法:把核心 run_analysis 包進 single-flight —— 進行中即有請求時,後到者「共用」
同一個執行結果,而非各跑一次。等待有逾時,不會永久卡住;例外會傳播給所有等待者。

單 worker 假設(本輪不引入 Redis / 多 worker 協調)。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class SingleFlight:
    """把並發呼叫收斂為單次執行的協調器(逐 event loop 建立鎖,測試安全)。"""

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self.run_count = 0          # 實際執行次數(測試用:證明只跑一次)
        self.collapsed_count = 0    # 被收斂(共用他人結果)的次數

    def _lock_for_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock = asyncio.Lock()
            self._loop = loop
            self._task = None
        return self._lock

    async def _wrapped(self, factory):
        self.run_count += 1
        return await factory()

    async def run(self, factory, *, timeout: float):
        """執行 factory();若已有進行中的執行,改為等待其結果。

        factory: 無參 callable,回傳 coroutine(實際的分析工作)。
        timeout: 等待進行中執行的最長秒數;逾時拋 asyncio.TimeoutError。
        """
        lock = self._lock_for_loop()
        async with lock:
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._wrapped(factory))
                mine = True
            else:
                mine = False
                self.collapsed_count += 1
            task = self._task
        # shield:等待者逾時/被取消不會連累仍在跑的核心分析,其他等待者仍取得結果
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            logger.warning("analysis single-flight wait timed out after %.0fs "
                           "(核心分析仍在背景進行,本次請求放棄等待)", timeout)
            raise

    def reset_for_tests(self) -> None:
        self._task = None
        self.run_count = 0
        self.collapsed_count = 0


# 全域唯一的完整分析 single-flight(手動、排程、首載共用同一道鎖)
analysis_flight = SingleFlight()


async def run_analysis_shared(provider, *, trigger: str, tick=None,
                              cached_only: bool = False, timeout: float | None = None):
    """核心 run_analysis 的 single-flight 入口。所有分析呼叫點一律走這裡。"""
    from app.config import get_settings
    from app.services.analysis_service import run_analysis
    if timeout is None:
        timeout = float(get_settings().analysis_lock_timeout_seconds)

    def factory():
        return run_analysis(provider, trigger=trigger, tick=tick, cached_only=cached_only)

    return await analysis_flight.run(factory, timeout=timeout)
