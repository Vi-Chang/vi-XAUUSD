# Twelve Data 429 / Market Data Reliability Audit

## Request call graph before this change

1. `scheduler.job_quote_l1` called `provider.get_live_price`.
2. Startup/structure/candle-close/manual triggers converged through analysis single-flight.
3. `analysis_service.run_analysis` called `refresh_candles` for 1D, 4H, 1H,
   15M, 1W, 30M and 5M.
4. `TwelveDataProvider` downloaded one series for each direct timeframe and a
   separate 5000-bar 1H series for local 1D/1W aggregation.
5. Position and `/api/price` endpoints could call the provider directly.
6. Cross-check could instantiate another Twelve Data provider.

Telegram renderers and state-machine engines did not call Twelve Data directly.
They consumed the analysis result.

## Root causes

- Provider cache and quota accounting were process-local. Every replica or
  rolling restart started with an empty cache and a fresh budget estimate.
- A cold start used about seven requests (quote + six candle downloads). One
  ordinary 1H download duplicated the long 1H history used by 1D/1W.
- The HTTP retry happened below quota accounting. A nominal seven-request cold
  start could create up to fourteen physical requests while metrics still
  under-reported usage.
- Changed timeframes downloaded the full requested history (normally 300 bars)
  instead of merging a small incremental update.
- 429 was treated like a normal HTTP failure. Fast retry and concurrent callers
  could amplify the rate-limit event.
- `httpx` exception text can contain the complete request URL, including the
  `apikey` query parameter. The previous retry wrapper included that exception
  text in the final provider error.
- Deployment documents assumed one worker, but there was no distributed owner
  lock preventing two Zeabur instances from polling simultaneously.

## Request volume

Before, a cold-start minute was approximately 7 external requests and up to 14
when the single retry fired. Cross-check or a second instance could push the
same minute beyond the eight-request free-tier limit.

After, a cold start is normally 6 requests (one quote and five candle requests).
The canonical long 1H history is shared by 1H, 1D and 1W. Normal operation is
estimated at about 0.4 requests/minute averaged across a 23-hour trading day
(roughly 540-550/day), with an hourly boundary burst normally 4-6 requests.
Circuit breaker and priority budget prevent retry storms from exceeding the
local eight-request budget.

## New flow

`Twelve Data -> MarketDataService -> canonical cache/LKG -> analysis -> Canonical State Machine -> Telegram`

- Concurrent key: `provider:symbol:timeframe`.
- Initial history: configured 300 bars (long 1H initializes up to 5000 once).
- Incremental refresh: configured 5 bars, merged by open time into rolling 300.
- Core order: 15M, 1H, 4H, 1D; 30M and 1W are optional.
- 429: `CLOSED -> OPEN -> HALF_OPEN(one probe) -> CLOSED`.
- Recovery: a successful probe forces core timeframe resynchronization before
  market-data health returns to GOOD.
- Multi-instance: Redis scheduler ownership lock; followers serve APIs but do
  not poll market data.

## Secret audit

The current tree and reachable Git history were searched for non-empty
`TWELVE_DATA_API_KEY` assignments and literal `apikey=` values outside tests.
No hard-coded Twelve Data key was found. Runtime secrets remain environment
settings. All log, exception and notification formatting now passes through the
central sanitizer.

