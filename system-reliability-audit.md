# XAUUSD system reliability audit — 2026-08-24

## Actual dependency graph

| Layer | Authoritative implementation | Input → output | Time / refresh | Failure mode |
|---|---|---|---|---|
| Provider | `app/providers/*` through `MarketDataProvider` | external quote/candles → `PriceTick`/`Candle` | provider UTC; L1 poll + candle refresh | retry, quota failure, fallback |
| Live quote | `scheduler.job_quote_l1` + `QuoteCache` | validated tick → forward-only live quote | quote event time + received time | invalid spread raises; duplicate/late tick rejected |
| Candle store | `candle_service` + `candles` table | provider candles → UTC persisted candles | open/close time + `is_closed` | incomplete/forming candle remains provisional |
| Indicators/structure | `indicators`, `market_structure`, `key_levels` | closed candles → indicators/swings/levels | finalized candle events | missing/warm-up data blocks quality |
| Analysis | `analysis_service.run_analysis` | one candle set + quote → normalized analysis | `timestamp_utc`, market timestamp | fail-closed data gate |
| Setup/execution | `breakout_setup_manager`, `setup_lifecycle`, `entry_engine` | structure + closed 15M + risk → frozen setup | setup/candle IDs | illegal transition rejected; trigger freeze asserted |
| Decision SSOT | `final_decision_engine` + `current_decision_store` | candidates → one `FinalDecision` | candle/data version + evaluated time | stale write rejected and audited |
| Position management | `position_service`, `trade_plan` | actual/conditional position → management events | stable position/trade-plan IDs | stop widening and invalid price geometry rejected |
| Notification | `alert_aggregator` → `decision_outbox` → Telegram worker | canonical event → durable outbox | semantic key + decision version | retry-safe, superseded pending row cancelled |
| API/UI | `main.py`, `realtime_presentation`, `app.js` | canonical snapshot + live facts → dashboard | REST/WebSocket | quote/strategy versions displayed separately |
| AI | `llm/service.py` | deterministic snapshot → narrative | cached input fingerprint | cannot author price, setup or hard risk fields |

## Single sources of truth verified

- Current publishable decision: `current_final_decisions` and its immutable payload.
- Durable notification: `decision_events` + `telegram_notifications` outbox.
- Frozen setup trigger: persisted breakout setup ledger plus runtime/CI assertion.
- Freshness calculation: `engines/freshness_state.evaluate_freshness_state`.
- Cost-aware risk/reward for rule scenarios: `scenario_safety.calculate_risk_reward`.
- Actual open position: `positions`; hypothetical management uses separate `tradePlanId`.

Remaining duplication is recorded below and is not described as fully closed.

## Bug registry

| ID | Severity | Symptom / root cause | Fix and evidence | Status |
|---|---|---|---|---|
| REL-001 | P0 | A late tick could overwrite the newest quote because `QuoteCache.add` ignored event time | forward-only UTC comparison; late-tick fault injection | FIXED / VERIFIED |
| REL-002 | P0 | `ask < bid`, non-finite or non-positive quotes could enter the live cache | canonical quote invariant before mutation | FIXED / VERIFIED |
| REL-003 | P0 | `modify_stop` recorded widening but still persisted it | long stop may only rise; short stop may only fall; DB service rejects write | FIXED / VERIFIED |
| REL-004 | P0 | trade plan accepted a wrong-side stop because it only used absolute distance | direction-aware entry/SL/TP invariant | FIXED / VERIFIED |
| REL-005 | P1 | first received old tick looked fresh because freshness used receive time only | quote freshness now uses the worse of event age and receive age | FIXED / VERIFIED |
| REL-006 | P1 | closed market changed only prose while machine state remained stale | canonical `MARKET_CLOSED`, `DISCONNECTED`, `RECOVERING`, `FRESH`, `DEGRADED`, `STALE` health state | FIXED / VERIFIED |
| REL-007 | P1 | moving confirmation goalpost | frozen primary trigger and positive replay in commit `772e2b4` | FIXED / VERIFIED |
| REL-008 | P1 | stale decision/outbox race | transactional version check, cancellation and semantic unique key already present | VERIFIED |
| REL-009 | P1 | executable setup, delivery validation and presentation calculated RR independently | all live decision paths now call `scenario_safety.calculate_risk_reward`; outcome/performance paths retain distance metrics, not trade authorization | FIXED / VERIFIED |
| REL-010 | P1 | no full tick-level provider reconnect/backfill canary has run in production | requires deployment observation across real finalized 15M events | NOT_FIXED |
| REL-011 | P2 | health endpoint lacked late/duplicate quote counters | exposed reliability counters | FIXED / VERIFIED |

## Invariants and negative proof

- Late event time cannot move the authoritative quote backward.
- Exact duplicate tick is idempotent.
- `ask >= bid`; quote values are finite and positive.
- LONG: stop below entry, targets above entry, stop never moves downward.
- SHORT: stop above entry, targets below entry, stop never moves upward.
- Confirmed setup primary trigger is immutable.
- A stale decision version cannot replace a newer persisted decision.
- Notification delivery is durable and deduplicated by database uniqueness.

The fault-injection tests deliberately submit the invalid cases above and require
an exception/rejection. A permissive implementation therefore fails CI.

## Bug-pattern scan

| Pattern | Result | Evidence |
|---|---|---|
| implicit timezone | mitigated | provider dataclasses document UTC; canonical parser handles aware/legacy values |
| stale quote overwrite | found/fixed | `QuoteCache.add` |
| stop moving goalpost | found/fixed | `position_service.modify_stop` |
| trigger moving goalpost | previously found/fixed | `breakout_setup_manager` + `assert_trigger_frozen` |
| duplicate Telegram | guarded | durable semantic unique index and outbox retry tests |
| stale decision write | guarded | row lock + candle/data/evaluation ordering |
| duplicate RR logic | found | legacy engines/read models; tracked as REL-009 |
| silent background failure | no empty `catch` found in production Python | scheduler/heartbeat paths log failures |
| frontend stale overwrite | guarded at decision store; browser still consumes live quote overlay intentionally | snapshot and realtime fact versions |

## Closure statement

This audit must not be labelled `SYSTEM RELIABILITY AUDIT PASSED` until REL-010
has production canary evidence. The P0 defects
found in this pass are closed with reproduction, fix and regression coverage.
