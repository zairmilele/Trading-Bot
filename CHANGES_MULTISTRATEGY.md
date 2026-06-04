# MULTI-STRATEGY UPGRADE — What changed

This build adds **three new strategies** that run **simultaneously alongside the
existing S2 (FVG Retest)** strategy on the same bot, same Binance Futures account,
same Railway deployment. S2 logic is unchanged.

```
S2 (FVG Retest)      8 coins   $20 × 50x   FIXED_LP        (unchanged)
AxisPro              10 coins  $20 × 50x   PARTIAL_TRAIL   (new)
Multi Daily Breakout 6 coins   $20 × 25x   BREAKOUT_TRAIL  (new)
ICT Opening Gap      14 coins  $20 × 50x   FIXED_TRAIL     (new)
```

33 unique 15m symbols are streamed; AxisPro additionally streams a 1h feed for its
HTF bias (10 symbols). Each strategy carries its own coin list, indicators, risk,
and exit model.

---

## The three strategies (ported 1:1 from your backtests)

**AxisPro** — `strategy_axispro.py` (from `AxisPro_Backtest_15m_top50.py`)
15m entries with a 1h EMA200 bias gate, ATR-expansion filter, break-of-structure
across the last pivot (+ATR buffer), Fibonacci pullback into 38–62% (cancel >80%),
strong-close confirmation inside the EMA21/EMA50 band, EMA200 filter, cooldown.
Exits: SL = wider of (1.6×ATR, swing50) capped at 6×ATR; **TP1 at 1.5R closes 50%**,
SL→break-even, then **trail the runner by EMA21** to **TP2 at 3.0R**.
Coins: SOL SUI TRX WLD OP RUNE HBAR ALGO DYDX JUP (AAVE/WIF/FET/LDO dropped per notes).

**Multi Daily Breakout** — `strategy_breakout.py` (from `BacktestBreakout_6coins_2000d_RR3.py`)
NY 09:00–13:00 opening-range breakout (America/New_York, DST-aware). A 15m candle
that **closes** beyond the locked range triggers, gated by EMA200 trend, ADX≥20,
ATR% band (0.10–5.0), 1.2× volume surge, min range 0.20%. Up to 4 trades/day,
one open at a time. Exits: SL at opposite range edge, TP at 3R, trailing SL after
+1.5R (1.5×ATR), trailing TP after +2.5R (close pullback 1.2×ATR), 48h time-stop.
Coins: XLM ZEC SUI INJ TAO FET. **Leverage 25x** (the rest are 50x).

**ICT Opening Gap v3** — `strategy_ict.py` (from `ict_gap_v3_backtest.py`)
Daily gap = [min(prevClose, dayOpen), max(...)], midpoint fade via a **resting LIMIT**
order at mid ±0.02%. Fixed 0.5% SL / 1.5% TP (3R) plus a % trailing stop (arms +0.5%,
trails 0.4%). One trade/day; unfilled limits are cancelled at the UTC day boundary.
Coins: AIGENSYN BEAT CRCL ESPORT GENI HET HYPE IO MST MU SNDK XLM XPL ZEC.

---

## New / changed files

| File | Change |
|------|--------|
| `strategy_indicators.py` | **new** — EMA, Wilder ATR/ADX, SMA-of-series, pivots (match backtests) |
| `strategy_base.py` | **new** — shared `SignalEvent` (all exit-model fields) + `BaseStrategy` |
| `strategy_axispro.py` / `strategy_breakout.py` / `strategy_ict.py` | **new** — the three detectors |
| `strategy_registry.py` | **new** — `StrategyDispatcher`: coin universes, routing, startup symbol validation |
| `step1_candle_engine.py` | volume added to candles; **1h feed** added; callback now `(symbol, candle, candles_15m, candles_1h)` |
| `step2_signal_detector.py` | imports shared `SignalEvent`; adds thin `S2Strategy` wrapper (S2 logic untouched) |
| `step3_order_manager.py` | positions keyed by **(strategy, symbol)** + symbol lock; per-strategy config/loss counters; LIMIT entries; partial closes; ATR/EMA/% trailing; bar-close engine |
| `main.py` | builds the dispatcher, validates symbols, wires 4 strategies, forwards bar closes to the exit engine |
| `static/app.js`, `templates/index.html`, `static/style.css` | dashboard now shows all four strategies + filter |

---

## ⚠️ Important things to know before going live

1. **One Binance position per symbol per account.** In one-way mode Binance holds a
   single net position per symbol. Several coins are shared across strategies:
   - WLD → S2 + AxisPro
   - SUI → AxisPro + Breakout
   - XPL → S2 + ICT
   - XLM, ZEC → Breakout + ICT

   The bot keys positions by `(strategy, symbol)` for independent stats, **but enforces
   a symbol-level execution lock**: if a symbol already has a live position from any
   strategy, a second strategy's signal on that symbol is **skipped** (logged). This
   prevents two strategies colliding on the same Binance position. Truly independent
   trading of a shared coin would require separate sub-accounts / API keys.

2. **Tokenized-stock-style ICT coins.** CRCL, MST, MU, SNDK (and possibly others) may
   not be listed as Binance USDT-M Futures perpetuals. On startup the bot fetches
   `exchangeInfo` and **drops any unlisted coin with a warning** — the bot won't crash,
   those coins just won't trade. Check the startup log to see which (if any) were dropped.

3. **Live vs backtest fidelity.**
   - Entries fire at the **signal candle's close** (live market order), matching S2's
     existing convention (the backtests modelled "next-open").
   - Trailing/partial management runs on **15m candle closes** (same cadence as the
     backtest's per-bar loops), with real SL/TP algo orders as exchange-side backstops.
   - ICT uses a **single resting limit** committed to the first side (above/below) that
     triggers each day; the backtest could scan both sides. Documented in `strategy_ict.py`.
   - AxisPro's TP1 partial is best-effort on the rare margin-cap retry path (falls back
     to a single TP at TP2 + trail).

4. **Test on TESTNET first.** `TESTNET` defaults to `true` (demo/paper). This is a large
   amount of new live-trading logic that has been syntax- and dry-run-validated but not
   run against a live exchange. Validate on testnet, watch `bot.log`, confirm entries,
   SL/TP, partials, and trailing behave as expected before flipping `TESTNET=false`.

5. **Keep gunicorn at `--workers 1`** (Procfile) — multiple workers would each start a
   bot and duplicate trades.

---

## Quick local smoke test

```bash
pip install requests python-dotenv websocket-client flask
python _dryrun_test.py     # offline: detectors + exit engine, no network
```

Environment variables (same as before): `BINANCE_API_KEY`, `BINANCE_API_SECRET`,
`TESTNET` (default true), plus the existing optional Supabase / Telegram vars.
