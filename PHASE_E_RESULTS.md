# Phase E — Fibonacci Retracement Strategy: Results & Verdict

**Date:** 2026-06-09
**Strategy:** `FiboRetrace.py` — rule-based, 1H entry / 4H trend, long-only spot
**Pairs:** BTC/USDT, ETH/USDT (OKX)

## Summary verdict

The Fibonacci-retracement trend-rider is **profitable in-sample but fails
out-of-sample**. It captures bull-market beta, not a regime-robust edge.
**Not production-ready as a fire-and-forget bot.**

## How it works

- 4H uptrend (close > 4H EMA50) → find last confirmed swing low→high (no lookahead)
- Buy when price retraces into the 0.5–0.618 "golden zone"
- SL just below the 0.786 invalidation level (tight)
- TP at a Fibonacci extension (best in-sample: 1.618–1.8 × range)
- Low win-rate / high-payoff: ~15% WR carried by a few huge trend rides

## In-sample (2023-03 → 2025-03, bull/trending)

TP-extension sweep (stop 0.786, base 4H trend gate):

| TP ext | Win% | Total % | Drawdown |
|--------|------|---------|----------|
| 1.0    | 17.1 | −10.9   | 25% |
| 1.5    | 15.4 | +1.96   | — |
| 1.618  | 15.4 | +7.02   | 20% |
| **1.8**| 15.4 | **+17.77** | 19% |
| 2.0    | 13.4 | +0.1    | 19% |
| 2.2    | 10.4 | −22.6   | 27% |

Best in-sample: ext 1.8 (+17.77%). Profitable zone ext 1.5–1.8.
Note: even the best **underperforms buy-and-hold** (market +152% over the window).

## Out-of-sample (2021-01 → 2023-01, includes 2022 bear) — ext 1.618

| Macro gate | Trades | Win% | Total % | Drawdown |
|------------|--------|------|---------|----------|
| none (base)        | 125 | 7.2 | −56.9 | 57% |
| 4H EMA200          | 83  | 7.3 | −39.4 | 41% |
| daily SMA200       | 63  | 6.3 | −32.9 | 33% |
| daily + 4H EMA200  | 53  | 7.5 | **−23.8** | 27% |

**Win-rate collapses 15% → 7% out-of-sample.** Regime gating reduces the
bleeding (−57% → −24%) but cannot fix it — the entry signal itself does not
generalise beyond the 2023-24 trend regime.

## Why it overfit

- The +17.77% came from ~21 winning trades over 2 years — a handful of clean
  sustained trends unique to 2023-24.
- 2021 (choppy top) and 2022 (bear) lack those trends; retracement-buys with a
  far TP just bleed via the stop, regardless of gating.
- It is long-only: it has no way to profit when the macro trend is down.

## What was ruled out (didn't help)

- Stop width (swing-low vs 0.786): both ~2.6 WR-pts short of breakeven in-sample
- Break-even-at-1R: **harmful** — cut the big winners, WR 39% → 12%
- Trend-strength filters (EMA50 slope, 4H EMA200): no WR lift in-sample
- Macro regime gates (4H EMA200, daily SMA200): reduce OOS loss, don't fix it

## Options for next session (user to decide)

1. **Alert/assist mode** — bot signals when price hits the Fibo zone in a
   confirmed trend; you pull the trigger manually. Keeps your discretion
   (when to sit out, which swing) that the mechanical rules can't capture.
2. **Bull-only tactical** — deploy ONLY under a strict daily-200 bull regime,
   with monitoring; accept it as worse-than-hold beta capture. Low value.
3. **Add the short side** — symmetric Fibo retracement shorts in 4H/daily
   downtrends so 2022-type bears can profit. Significant work, re-overfit risk.
4. **Different approach** — the discretionary edge (reading structure, knowing
   when not to trade) may not mechanise cleanly into a full-auto bot.

**Recommendation:** Option 1 (alert/assist) is the most honest fit — it
automates the *scanning* (which Fibo setups exist right now) while leaving the
*judgement* with you, where the real edge lives.
