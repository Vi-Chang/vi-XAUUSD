# Freshness / realtime decision audit (2026-08-24)

## Root cause

1. The backend had several independent freshness calculations. Some parsed legacy
   timestamps locally while other paths used provider-aware UTC values.
2. The browser displayed timestamps by slicing the raw ISO string. A UTC value
   such as `00:14Z` was therefore shown as `00:14`, not `08:14 Asia/Taipei`.
3. Quote WebSocket updates refreshed the chart and headline price, but entry
   distance, trigger, defense and chase facts remained on the last strategy
   snapshot.
4. Intrabar crossing and closed-candle confirmation were not exposed as two
   explicit deterministic facts. This made a correct wait-for-close decision
   look stale.
5. Scenario cards rendered raw persisted setup rows, so duplicate historical
   rows could occupy the primary area.

## Fixed flow

`provider UTC timestamp -> parse_utc -> canonical freshness state -> deterministic
realtime presentation -> API/WebSocket -> Asia/Taipei presentation`

The canonical freshness object contains market, event, calendar and strategy
freshness. Live facts contain the latest quote, latest closed 15M candle,
intrabar/closed confirmation, entry/trigger/invalidation distances, chase state,
defense state, RR and the next real 15M boundary.

AI output remains contextual only and cannot overwrite those live facts.

## Event behavior

- Every quote updates presentation facts and the dashboard.
- A new trigger crossing causes immediate deterministic re-evaluation but cannot
  manufacture a candle-close confirmation.
- The existing candle-close scheduler launches a full strategy evaluation.
- `stale -> fresh` launches an immediate full evaluation.
- Telegram remains sourced from FinalDecision events and the durable transactional
  outbox. Its database unique semantic key prevents retry, worker or restart
  duplicates.

## Verification

- UTC fixture: `00:16Z - 00:14Z = 120 seconds = fresh`.
- Taipei presentation: `08:16 - 08:14 = 120 seconds`.
- Desktop and 390x844 mobile previews show Taiwan time, quote age, trigger state
  and live 15M countdown without JavaScript errors or horizontal overflow.
- Full automated suite: 671 passed.
