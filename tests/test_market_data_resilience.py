import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import ClassVar

import pytest

from app.db.session import init_db
from app.providers.base import (
    Candle,
    MarketDataProvider,
    PriceTick,
    ProviderError,
    ProviderRateLimitedError,
)
from app.providers.twelve_data import (
    QuotaTracker,
    TwelveDataCircuitBreaker,
    TwelveDataProvider,
)
from app.services.candle_service import refresh_candles
from app.services.market_data_notifications import notify_market_data_transition
from app.services.market_data_service import MarketDataService
from app.services.secret_sanitizer import REDACTED, sanitize, sanitize_text


def candles(timeframe="15M", count=300):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    minutes = {"15M": 15, "30M": 30, "1H": 60, "4H": 240}.get(timeframe, 1440)
    return [Candle(
        symbol="XAUUSD", timeframe=timeframe,
        open_time=now - timedelta(minutes=minutes * (count - index)),
        close_time=now - timedelta(minutes=minutes * (count - index - 1)),
        open=100 + index, high=101 + index, low=99 + index,
        close=100.5 + index, is_closed=True, data_provider="fake",
    ) for index in range(count)]


class FakeProvider(MarketDataProvider):
    name = "fake"

    def __init__(self):
        self.candle_calls = 0
        self.quote_calls = 0
        self.requested_counts = []
        self.fail_timeframes = set()
        self.delay = 0.0

    async def get_live_price(self, symbol="XAUUSD"):
        self.quote_calls += 1
        return PriceTick(symbol=symbol, bid=100, ask=100,
                         quote_time=datetime.now(timezone.utc), provider=self.name)

    async def get_candles(self, symbol="XAUUSD", timeframe="15M", count=300):
        self.candle_calls += 1
        self.requested_counts.append(count)
        if self.delay:
            await asyncio.sleep(self.delay)
        if timeframe in self.fail_timeframes:
            raise RuntimeError(f"{timeframe} unavailable")
        return candles(timeframe, count)


async def test_ten_concurrent_callers_share_one_external_request():
    raw = FakeProvider()
    raw.delay = 0.01
    service = MarketDataService(raw)
    results = await asyncio.gather(*[
        service.get_candles("XAUUSD", "15M", 300, caller=f"module-{index}")
        for index in range(10)
    ])
    assert raw.candle_calls == 1
    assert all(len(result) == 300 for result in results)


async def test_valid_cache_does_not_increase_external_requests():
    raw = FakeProvider()
    service = MarketDataService(raw)
    await service.get_candles("XAUUSD", "15M", 300)
    await service.get_candles("XAUUSD", "15M", 300)
    assert raw.candle_calls == 1


async def test_incremental_refresh_fetches_five_and_keeps_rolling_300():
    raw = FakeProvider()
    service = MarketDataService(raw)
    await service.get_candles("XAUUSD", "15M", 300)
    service._candles[("XAUUSD", "15M")].fetched_at -= timedelta(minutes=16)
    result = await service.get_candles("XAUUSD", "15M", 300)
    assert raw.requested_counts == [300, 5]
    assert len(result) == 300


async def test_quote_refresh_never_downloads_candle_history():
    raw = FakeProvider()
    service = MarketDataService(raw)
    await service.get_quote(caller="quote_scheduler")
    await service.get_quote(caller="dashboard")
    assert raw.quote_calls == 1
    assert raw.candle_calls == 0


async def test_follower_replica_never_spends_external_provider_quota():
    raw = FakeProvider()
    service = MarketDataService(raw)
    service.set_external_fetch_allowed(False)
    with pytest.raises(ProviderError, match="僅讀取共享行情"):
        await service.get_quote(caller="follower_api")
    with pytest.raises(ProviderError, match="僅讀取共享行情"):
        await service.get_candles("XAUUSD", "15M", 300, caller="follower_api")
    assert raw.quote_calls == 0
    assert raw.candle_calls == 0


async def test_lkg_candles_survive_provider_failure_and_are_marked_stale():
    raw = FakeProvider()
    service = MarketDataService(raw)
    original = await service.get_candles("XAUUSD", "15M", 300)
    service._candles[("XAUUSD", "15M")].fetched_at -= timedelta(minutes=16)
    raw.fail_timeframes.add("15M")
    fallback = await service.get_candles("XAUUSD", "15M", 300)
    assert fallback[-1].open_time == original[-1].open_time
    assert service.health_snapshot()["status"] == "DEGRADED"
    assert service.health_snapshot()["timeframes"]["15M"]["freshness"] == "STALE"


async def test_optional_30m_failure_does_not_abort_core_analysis():
    init_db()
    raw = FakeProvider()
    raw.fail_timeframes.add("30M")
    service = MarketDataService(raw)
    result = await refresh_candles(service, ("15M", "30M"), 300)
    assert len(result["15M"]) == 300
    # A previous analysis may have persisted a last-known-good optional 30M
    # series.  Both an empty optional series and that safe fallback are valid;
    # neither may abort the core 15M analysis.
    assert result["30M"] == [] or len(result["30M"]) == 300
    health = service.health_snapshot()
    assert health["status"] == "GOOD"
    assert health["analysisHealth"] == "DEGRADED"
    assert health["optionalDegraded"] is True


class Response429:
    status_code = 429
    headers: ClassVar = {"Retry-After": "60"}

    def json(self):
        return {}


class Client429:
    def __init__(self):
        self.calls = 0

    async def get(self, *_args, **_kwargs):
        self.calls += 1
        return Response429()

    async def aclose(self):
        return None


class Response200:
    status_code = 200
    headers: ClassVar = {}

    def json(self):
        return {"price": "101.25"}

    def raise_for_status(self):
        return None


class ClientSequence(Client429):
    async def get(self, *_args, **_kwargs):
        self.calls += 1
        return Response429() if self.calls == 1 else Response200()


def td_settings():
    return SimpleNamespace(
        twelve_data_api_key="SUPER_SECRET_KEY",
        twelve_data_daily_limit=800,
        twelve_data_minute_limit=8,
        twelve_data_transient_retries=1,
        twelve_data_rate_limit_base_backoff_seconds=30,
        twelve_data_rate_limit_max_backoff_seconds=240,
        twelve_data_rate_limit_jitter_ratio=0.0,
    )


async def test_429_opens_circuit_and_twenty_followups_make_no_external_calls(monkeypatch):
    circuit = TwelveDataCircuitBreaker()
    monkeypatch.setattr("app.providers.twelve_data.get_settings", td_settings)
    monkeypatch.setattr("app.providers.twelve_data._shared_circuit", circuit)
    provider = TwelveDataProvider()
    provider.quota = QuotaTracker(800, 100)
    client = Client429()
    provider._client = client
    with pytest.raises(ProviderRateLimitedError):
        await provider.get_live_price()
    assert circuit.state == "OPEN"
    for _ in range(20):
        with pytest.raises(ProviderRateLimitedError):
            await provider.get_live_price()
    assert client.calls == 1


async def test_half_open_allows_one_probe_and_signals_recovery(monkeypatch):
    circuit = TwelveDataCircuitBreaker()
    monkeypatch.setattr("app.providers.twelve_data.get_settings", td_settings)
    monkeypatch.setattr("app.providers.twelve_data._shared_circuit", circuit)
    provider = TwelveDataProvider()
    provider.quota = QuotaTracker(800, 100)
    provider._client = ClientSequence()
    with pytest.raises(ProviderRateLimitedError):
        await provider.get_live_price()
    circuit.open_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    tick = await provider.get_live_price()
    assert tick.mid == 101.25
    assert circuit.state == "CLOSED"
    assert provider.consume_recovery_signal() is True
    assert provider.consume_recovery_signal() is False


class Notifier:
    def __init__(self):
        self.calls = []

    async def notify(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return True


async def test_legacy_market_data_notifier_cannot_bypass_canonical_outbox():
    notifier = Notifier()
    payload = {
        "normalized_analysis": {
            "trendBias": "bearish", "currentPrice": 100,
            "lastClosedCandleTimestamp": "2026-08-25T00:00:00+00:00",
            "confirmationLevels": [
                {"kind": "support", "price": 98.0},
                {"kind": "resistance", "price": 102.0},
            ],
        },
        "final_decision_state": {"marketDirection": "BEARISH"},
    }
    degraded = {"status": "DEGRADED", "provider": "twelve_data"}
    current = await notify_market_data_transition(
        notifier=notifier, previous="GOOD", health=degraded, payload=payload)
    await notify_market_data_transition(
        notifier=notifier, previous=current, health=degraded, payload=payload)
    healthy = {"status": "GOOD", "provider": "twelve_data"}
    current = await notify_market_data_transition(
        notifier=notifier, previous=current, health=healthy, payload=payload)
    await notify_market_data_transition(
        notifier=notifier, previous=current, health=healthy, payload=payload)
    assert current == "GOOD"
    assert notifier.calls == []


def test_all_secret_output_is_redacted():
    raw = (
        "https://api.twelvedata.com/time_series?apikey=ABC123&symbol=XAUUSD "
        "Authorization: Bearer TOKEN456 postgres://user:DBPASS@db/xauusd"
    )
    cleaned = sanitize_text(raw)
    assert "ABC123" not in cleaned
    assert "TOKEN456" not in cleaned
    assert "DBPASS" not in cleaned
    assert REDACTED in cleaned
    assert sanitize({"api_key": "ABC123", "nested": raw})["api_key"] == REDACTED
