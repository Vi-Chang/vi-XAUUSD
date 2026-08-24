# Moving Goalpost / Confirmation Ladder audit

## Root cause

`evaluate_breakout_setups()` treated a newly detected resistance/support as a new
mandatory setup whenever the previous setup was no longer in one specific wait
status. A confirmed breakout that was outside the original narrow entry band was
also returned to a generic breakout wait state. Together these paths allowed a
confirmed 4615 setup to be replaced by 4620, 4628 and later levels.

## Fix

- A setup now freezes `primaryTrigger`, `setupId`, confirmation timeframe and
  invalidation until a terminal invalidation/expiry/missed lifecycle.
- Runtime and CI enforce the frozen-trigger invariant.
- Confirmation and execution are separate. ATR and signal quality determine the
  execution/chase range.
- A-grade: 1.0 ATR; B-grade: 0.8 ATR; C-grade: 0.6 ATR.
- Confirmed and within chase becomes `ENTER`; the next evaluation becomes
  `MANAGE/HOLD`. Later structure levels are targets/management context only.
- Confirmed but beyond chase becomes `MISSED / WAIT_RETEST`, never an unconfirmed
  wait for a higher goalpost.
- UI and Telegram vocabulary now distinguish ARMED, ENTER, MANAGE/HOLD and MISSED.

## 4615 replay

| Price | State | Frozen trigger |
|---:|---|---:|
| 4608 | WATCHING | 4615 |
| 4612 | ARMED | 4615 |
| 4615.5 | ENTER | 4615 |
| 4620 | MANAGE | 4615 |
| 4623 | MANAGE | 4615 |
| 4628 | MANAGE | 4615 |
| 4635 | MANAGE | 4615 |

No later resistance becomes a mandatory entry confirmation for the same setup.
