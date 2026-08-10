"""完整分析 single-flight(Phase 1):並發只執行一次核心分析。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.single_flight import SingleFlight


def test_concurrent_calls_execute_core_once():
    sf = SingleFlight()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        await asyncio.sleep(0.05)      # 模擬打 provider/LLM 的耗時
        return calls["n"]

    async def main():
        return await asyncio.gather(*[sf.run(factory, timeout=5) for _ in range(12)])

    results = asyncio.run(main())
    assert calls["n"] == 1                 # 核心只跑一次
    assert all(r == 1 for r in results)    # 所有並發請求拿到同一結果
    assert sf.run_count == 1
    assert sf.collapsed_count == 11         # 其餘 11 個被收斂


def test_sequential_calls_rerun_after_completion():
    sf = SingleFlight()
    n = {"c": 0}

    async def factory():
        n["c"] += 1
        return n["c"]

    async def main():
        a = await sf.run(factory, timeout=5)
        b = await sf.run(factory, timeout=5)
        return a, b

    a, b = asyncio.run(main())
    assert (a, b) == (1, 2) and sf.run_count == 2   # 前一次結束後,下一次重新執行


def test_wait_timeout_raises_but_core_keeps_running():
    sf = SingleFlight()
    done = {"v": None}

    async def factory():
        await asyncio.sleep(0.3)
        done["v"] = "finished"
        return "ok"

    async def main():
        with pytest.raises(asyncio.TimeoutError):
            await sf.run(factory, timeout=0.05)     # 等待逾時
        await asyncio.sleep(0.4)                     # 讓核心在背景跑完
        return done["v"]

    assert asyncio.run(main()) == "finished"         # 核心未被取消,仍完成


def test_exception_propagates_to_all_waiters():
    sf = SingleFlight()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        await asyncio.sleep(0.02)
        raise ValueError("boom")

    async def main():
        return await asyncio.gather(
            *[sf.run(factory, timeout=5) for _ in range(5)], return_exceptions=True)

    results = asyncio.run(main())
    assert calls["n"] == 1                            # 仍只執行一次
    assert all(isinstance(r, ValueError) for r in results)   # 例外傳給所有等待者


def test_reruns_after_failure_key_cleared():
    sf = SingleFlight()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise ValueError("boom")

    async def main():
        for _ in range(2):
            with pytest.raises(ValueError):
                await sf.run(factory, timeout=1)

    asyncio.run(main())
    assert calls["n"] == 2                            # 失敗後 key 清除,下次可重試


def test_different_keys_run_independently():
    sf = SingleFlight()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return calls["n"]

    async def main():
        return await asyncio.gather(sf.run(factory, timeout=5, key="A"),
                                    sf.run(factory, timeout=5, key="B"))

    a, b = asyncio.run(main())
    assert calls["n"] == 2 and sf.run_count == 2      # 不同 key → 各自執行,不共用


def test_timed_out_waiter_does_not_leak_unretrieved_exception():
    """所有等待者逾時離開後,背景 task 的例外仍被 reap(不留 unretrieved 警告)。"""
    sf = SingleFlight()
    unhandled = []

    async def factory():
        await asyncio.sleep(0.1)
        raise ValueError("late boom")

    async def main():
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, ctx: unhandled.append(ctx))
        with pytest.raises(asyncio.TimeoutError):
            await sf.run(factory, timeout=0.02)       # 等待者先逾時
        await asyncio.sleep(0.2)                       # 讓 task 完成 + reap callback 執行

    asyncio.run(main())
    assert not any("never retrieved" in str(c.get("message", "")) for c in unhandled)
    assert sf._inflight == {}                          # 完成後 key 引用已清除


def test_run_analysis_shared_collapses_concurrent(monkeypatch):
    """透過 run_analysis_shared:手動/排程/首載共用同一道鎖,只實際跑一次。"""
    import app.services.analysis_service as asvc
    from app.services import single_flight as sfmod
    sfmod.analysis_flight.reset_for_tests()
    calls = {"n": 0}

    async def fake_run(provider, *, trigger, tick=None, cached_only=False, symbol="XAUUSD"):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return SimpleNamespace(model_dump=lambda: {"n": calls["n"], "trigger": trigger})

    monkeypatch.setattr(asvc, "run_analysis", fake_run)

    async def main():
        # 模擬:排程(event)、手動、首載 同時進來(同 symbol + cached_only → 同 key)
        return await asyncio.gather(
            sfmod.run_analysis_shared(None, trigger="event", timeout=5),
            sfmod.run_analysis_shared(None, trigger="manual", timeout=5),
            sfmod.run_analysis_shared(None, trigger="manual", timeout=5),
        )

    res = asyncio.run(main())
    assert calls["n"] == 1                            # 只實際跑一次(trigger 不同不影響共用)
    assert all(r.model_dump()["n"] == 1 for r in res)


def test_run_analysis_shared_different_params_not_shared(monkeypatch):
    """cached_only 不同 → 結果/成本不同 → 不得錯誤共用。"""
    import app.services.analysis_service as asvc
    from app.services import single_flight as sfmod
    sfmod.analysis_flight.reset_for_tests()
    calls = {"n": 0}

    async def fake_run(provider, *, trigger, tick=None, cached_only=False, symbol="XAUUSD"):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return SimpleNamespace(model_dump=lambda: {"n": calls["n"], "cached_only": cached_only})

    monkeypatch.setattr(asvc, "run_analysis", fake_run)

    async def main():
        return await asyncio.gather(
            sfmod.run_analysis_shared(None, trigger="manual", cached_only=False, timeout=5),
            sfmod.run_analysis_shared(None, trigger="timed", cached_only=True, timeout=5),
        )

    asyncio.run(main())
    assert calls["n"] == 2                            # 不同 cached_only → 各跑一次
