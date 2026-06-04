# Fixes Applied — Live vs Backtest Win Rate Gap

## Problem

The bot was closing ~90% of live trades as losses despite the same strategy
backtesting cleanly. Loss sizes were uniformly clustered near the SL distance
(0.5%), and log analysis showed positive adverse slippage on nearly every
trade. Multiple correlated SHORT entries were also opening within the same
second on different alts and stopping out together.

## Root cause

`step3_order_manager.py` rebased the SL/TP off the **actual fill price** after
slippage, instead of the **signal price** the strategy intended:

```python
# OLD (buggy)
sl_pct   = signal.sl_price / signal.entry_price
sl_price = actual_entry * sl_pct
```

When an entry filled 0.10–0.15% past the signal price (normal for market
orders on confirmed crossover candles), the SL got pushed the same 0.10–0.15%
deeper. Combined with a flat 0.5% SL, this left the trade pre-stopped before
it had a chance to develop.

## Fixes

### Fix 1 — Slippage rejection (step3_order_manager.py)
After the entry fills, compare actual fill to signal price. If the fill is
worse by more than `MAX_ADVERSE_SLIPPAGE_PCT` (default 0.15%), close the
position immediately and skip the trade. Better to take a tiny round-trip fee
loss than a full SL hit on a pre-stopped trade.

### Fix 2 — Anchor SL/TP to signal price (step3_order_manager.py)
SL and TP are now placed at `signal.sl_price` and `signal.tp_price` directly,
not rebased off the slipped entry. This makes live execution match the
backtest assumption that SL/TP distances are measured from the signal candle's
close.

### Fix 3 — ATR-based stops (step2_signal_detector.py)
SL distance is now `max(0.5%, 1.5 × ATR%)` instead of a flat 0.5%. TP distance
is `3 × SL distance` (preserving the original 1:3 risk-reward). On volatile
candles where the average bar range exceeds 0.5%, this prevents normal noise
from chopping out the trade. Falls back to flat 0.5%/1.5% if ATR is
unavailable for any reason.

### Fix 4 — Per-bar correlated entry cap (step3_order_manager.py)
Limits new entries to `MAX_NEW_POSITIONS_PER_BAR` (default 3) within any single
15-minute candle. When multiple alts fire EMA crossovers simultaneously
(common during market-wide moves), this prevents a single reversal tick from
stopping out 4–5 positions at once.

## Tunable parameters

In `step3_order_manager.py`:
- `MAX_ADVERSE_SLIPPAGE_PCT = 0.15` — tighten to 0.10 for stricter execution,
  loosen to 0.20 if too many trades are getting rejected
- `MAX_NEW_POSITIONS_PER_BAR = 3` — raise if you want more aggressive
  participation, lower if you still see clustered losses

In `step2_signal_detector.py`:
- `S1_ATR_SL_MULT = 1.5` — raise to 2.0 for wider stops on volatile assets
- `S1_RR_RATIO = 3.0` — your original 1:3 ratio; lower it (e.g. 2.0) if you
  want higher win rate at the cost of smaller wins

## What was NOT changed

- Entry logic (the 6 filters in `_check_s1`)
- Strategy parameters (ADX threshold, EMA periods)
- WebSocket / candle engine
- Order sizing / leverage / margin caps
- Symbol list

## Files modified

- `step2_signal_detector.py` — ATR-based SL/TP, extra indicators in signal
- `step3_order_manager.py` — slippage rejection, anchored SL/TP, per-bar cap

---

# S2 Migration — FVG Retest replacing EMA Cross

## Summary

The live bot has been migrated from S1 (EMA 9/26 Cross with multi-filter
confirmation) to S2 (FVG Retest + EMA50/100 + ADX). S1 is no longer active.

## Strategy change

| Aspect          | OLD (S1)                              | NEW (S2)                          |
|-----------------|---------------------------------------|-----------------------------------|
| Indicators      | EMA 9/26/200, MACD, ADX, DI±, ATR     | EMA 50, EMA 100, ADX(14)          |
| Entry trigger   | EMA 9/26 cross + 6 filters            | 3-candle FVG → retest → confirm   |
| Entry timing    | Close of confirm candle               | OPEN of next candle (market fill) |
| SL              | ATR-based with 0.5% floor             | Flat 0.5% from entry              |
| TP              | 3:1 RR                                | Flat 1.0% (RR 2.0)                |
| ADX min         | 25                                    | 20                                |
| Lock Profit     | None                                  | At 50% to TP, lock 10% of TP dist |
| Direction       | LONG + SHORT                          | LONG + SHORT                      |
| Strategy ID     | S1_EMA_CROSS                          | S2_FVG_RETEST                     |

## Symbol universe

Expanded from 15 to **29 symbols** to match the backtest exactly:

  Originals (9): BTC, ETH, SOL, XRP, BNB, LINK, TAO, RUNE, LTC
  Added    (20): ADA, AVAX, DOT, NEAR, ATOM, APT, SUI, ARB, OP, POL,
                 UNI, AAVE, MKR, DOGE, 1000SHIB, 1000PEPE, FET, RENDER,
                 TRX, BCH

NOTE: `POSITION_CAPS` only contains empirical caps for the original symbols.
New alts fall back to the conservative $2,886 default. To unlock larger
notionals on the new symbols, re-run `find_caps.py`.

## Lock Profit (LP) implementation

When price reaches 50% of the way from entry to TP, the SL algo order is
cancelled and replaced with a new SL at 10% of the way to TP. After arming,
any "SL hit" exits at the locked-in profit price and is recorded as outcome
**LP_WIN** (distinct from regular WIN at TP).

LP arming:
- Polled every 15s by the position monitor
- If new-SL placement fails after old-SL was cancelled, the original SL is
  restored (best-effort) so the position is never left unprotected silently
- LP-armed positions reset the consecutive-loss counter on exit (it's a win)

## Equivalence verification

A bit-for-bit equivalence test (`equiv_test.py`) was run between the live
detector and the backtest's signal generation logic, using identical inputs.
Across 10 random seeds × ~22 signals each = **227 signals**, every single
one matched on `(timestamp, direction)` with ADX delta = 0.0 (exact match
to floating-point precision). Indicators (EMA50, EMA100, ADX) also match
the backtest exactly.

## Safeguards retained

Both safeguards from the previous version are kept (they protect live
execution but are not in the backtest):
- Slippage rejection at 0.15% (Fix #1)
- Per-bar cap of 3 new entries per 15m candle (Fix #4)

## Bug fixes done in passing

While porting, two pre-existing latent bugs were fixed:

1. `retry_entry * sl_pct` and `retry_entry * tp_pct` in the -4005 and -2027
   retry blocks referenced variables (`sl_pct`, `tp_pct`) that were never
   defined. The retry paths would have NameErrored if ever triggered. Now
   correctly anchored to the signal-derived `sl_price` / `tp_price`.

2. The emergency-close path on TP/SL placement failure called
   `detector.on_trade_closed(...)` but `detector` was unbound (should have
   been `self.detector`). Fixed.

## Files modified

- `step1_candle_engine.py` — new 29-symbol list, EMA50/100/ADX-only indicators
- `step2_signal_detector.py` — full S2 (FVG Retest) port from BacktestZair_S2_only.py
- `step3_order_manager.py` — LP arming, LP_WIN outcome, S2 strategy config,
  retry-block bug fixes, emergency_pos detector ref fix
- `main.py` — banner updated to S2-only, consec_losses fallback to {S2:0}
- `templates/index.html` — S1 strategy box removed, S2 renamed to "FVG RETEST",
  S1 dropdown option removed
- `static/app.js` — S1 stats display dropped, LP_WIN treated as win in totals
  and styling, S2_FVG_RETEST as the canonical S2 strategy ID

---

# Tune (post-S2 migration)

## Symbol set narrowed to 8

Per user request, replaced the 29-symbol universe with a curated 8-symbol set:

  BTCUSDT, ENAUSDT, WLDUSDT, PUMPUSDT, XPLUSDT, 1000SHIBUSDT, APTUSDT, MONUSDT

Note: PUMPUSDT, XPLUSDT, and MONUSDT are recently-listed pairs that launched
with reduced max leverage (5-20x rather than 50x). The order manager's
existing -2027 retry logic backs off to a valid leverage tier automatically,
so this is handled transparently — the bot will simply trade these at
whatever leverage Binance allows for them.

POSITION_CAPS only contains an empirical cap for BTCUSDT in the new set;
the other 7 symbols fall back to the conservative $2,886 default. Re-run
find_caps.py if you want to discover real caps for the new symbols.

## Risk profile changed: 0.4% SL / 1.2% TP (RR 3.0)

Previous: 0.5% SL / 1.0% TP (RR 2.0).
New:      0.4% SL / 1.2% TP (RR 3.0).

Updated in:
- step2_signal_detector.py: S2_SL_PCT=0.4, S2_RR_TARGET=3.0
- step3_order_manager.py: emergency_pos hardcoded SL/TP, slippage comment
- main.py: banner text

Heads-up: the slippage rejection threshold is unchanged at 0.15%. With the
narrower 0.4% SL, a 0.15% adverse fill now burns ~37% of the SL budget
(vs ~30% under the old 0.5% SL). The threshold is still effective, but if
you see a lot of [SLIPPAGE-REJECT] log lines, you may want to tighten it
(e.g. 0.10%) to be more selective about which fills you accept.

LP arming math is unchanged structurally (still 50% trigger / 10% lock of
TP distance). With the new TP distance of 1.2%, LP arms when price has
moved 0.6% in your favor and locks in 0.12% profit. Net of fees
(0.05% taker entry + 0.05% taker exit on the LP-armed STOP = 0.10% round
trip), an LP_WIN nets approximately +0.02% per trade.
