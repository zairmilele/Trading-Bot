"""
STEP 2 OF 4 -- Signal Detector
==============================
Receives every candle close from Step 1 (CandleEngine).
Checks S2 entry conditions.
Emits SignalEvent objects when a valid trade setup is found.

STRATEGY 2 -- FVG Retest + EMA 50/100 + ADX  (LONG + SHORT)
  Three-candle Fair Value Gap forms      -> tracked
  Price retests FVG zone                 (depth <=70%, age <=20 bars)
  Confirmation candle closes:
    - body >=50% of range (impulse)
    - close above/below BOTH EMA50 and EMA100
    - EMA50 above/below EMA100 (trend alignment)
    - ADX(14) >= 20
    - either continuation (allowContinuation=ON) or fresh break across EMA50
  Entry: NEXT candle's open (handled by order manager via market order at signal time)
  SL: 0.5% from entry. TP: 1.0% from entry (RR = 2.0).
  Lock Profit: when price reaches 50% of TP distance, SL is moved to entry+10% of TP
               distance (i.e. the trade can no longer lose -- it locks in 10% profit).

  One trade per symbol at a time. New signals blocked while a trade is open.

How it connects:
  from step2_signal_detector import SignalDetector, SignalEvent
  detector = SignalDetector()
  engine   = CandleEngine(SYMBOLS, callback=detector.on_candle_close)
  engine.start()

  detector.on_signal = my_handler   # called with (SignalEvent,)

Logic parity:
  This file is a 1:1 port of BacktestZair_S2_only.py's signal-generation logic.
  The FVG state machine, retest detection, confirmation logic, and trigger order
  are identical. The only difference is that the backtest knows the next candle's
  open at signal time (because it has the full series), while the live bot fires
  a market order on signal-candle close -- the order manager fills at current
  market price, which IS the next candle's open in real time.

Dependencies:
  requests  (already installed from Step 1)
"""

import sys
import io
import threading
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable

# Canonical SignalEvent now lives in strategy_base so all four strategies share it.
# (S2 only fills the FIXED_LP fields, so its behaviour is unchanged.)
from strategy_base import SignalEvent, EXIT_FIXED_LP, BaseStrategy

# Fix Windows console encoding -- must happen before any print or logging
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

log = logging.getLogger('signal_detector')

# 15-minute bar in milliseconds — used to convert timestamp differences to bar counts
CANDLE_MS = 15 * 60 * 1000

# ============================================================================
# CONFIGURATION -- must match BacktestZair_S2_only.py exactly
# ============================================================================

# S2 -- FVG Retest parameters (mirrors backtest structure; SL/TP user-tuned)
S2_EMA_SMALL          = 50           # emaLen1
S2_EMA_BIG            = 100          # emaLen2
S2_ADX_MIN            = 20.0         # adxThreshold
S2_SL_PCT             = 0.4          # SL distance as % of entry  (was 0.5)
S2_RR_TARGET          = 3.0          # rrTarget -- TP_dist = SL_dist * RR  (was 2.0)
S2_TP_PCT             = S2_SL_PCT * S2_RR_TARGET   # 1.2% (was 1.0%)
S2_MAX_FVG_AGE        = 20           # maxFvgAge (bars)
S2_MAX_RETEST_DEPTH   = 70.0         # maxRetestDepthPct (%)
S2_MIN_BODY_PCT       = 50.0         # minBodyPct (% of candle range)
S2_USE_IMPULSE        = True
S2_USE_FVG_AGE        = True
S2_USE_RETEST_DEPTH   = True
S2_USE_TREND_FILTER   = True
S2_ALLOW_CONTINUATION = True         # allowContinuationAfterCross = ON
S2_LP_TRIGGER_PCT     = 50.0         # arm LP at 50% to TP
S2_LP_LOCK_PCT        = 10.0         # lock 10% of TP distance from entry
S2_MAX_STORED_FVG     = 80


# ============================================================================
# SIGNAL EVENT
# ============================================================================
# SignalEvent is now imported from strategy_base (shared across all strategies).
# S2 constructs it with exit_model defaulting to FIXED_LP and the lp_* fields,
# so behaviour is identical to the original single-strategy build.


# ============================================================================
# FVG TRACKER -- per-symbol persistent state
# Direct port of FVGTracker class in BacktestZair_S2_only.py
# Identifier semantics changed from "birth_idx" (bar index) to "birth_t" (ms).
# Age is computed as (now_t - birth_t) // CANDLE_MS, which is identical to
# bar-index difference for any fixed-interval timeframe.
# ============================================================================

class FVGTracker:
    """
    Tracks Fair Value Gaps and their lifecycle for one symbol.
      state 0  = active (not yet retested)
      state 1  = retested (waiting for confirmation)
      state 2  = used (signal fired)
      state -1 = invalidated

    Direction: 1 = bullish FVG, -1 = bearish FVG.
    """
    def __init__(self, max_stored=S2_MAX_STORED_FVG):
        self.fvgs = []
        self.max_stored = max_stored

    def add(self, top, bot, direction, birth_t):
        self.fvgs.append({
            'top': top, 'bot': bot, 'dir': direction,
            'state': 0, 'birth_t': birth_t, 'retest_t': None,
        })
        if len(self.fvgs) > self.max_stored:
            self.fvgs.pop(0)

    def update_and_find_ready(self, candle, current_t, ema_small, ema_big,
                              adx, c_open, c_close,
                              max_age, max_retest_depth, use_age, use_depth, use_trend,
                              use_impulse, min_body_pct, allow_continuation,
                              ema_small_prev, close_prev):
        """
        Walk active FVGs, update state, return 'LONG' / 'SHORT' / None
        if confirmation just fired this bar.

        current_t is the candle close timestamp (ms). FVG age in bars is
        (current_t - birth_t) // CANDLE_MS.
        """
        body_high = max(c_open, c_close)
        body_low  = min(c_open, c_close)
        candle_range = candle['h'] - candle['l']
        body_size = abs(c_close - c_open)
        body_pct = (body_size / candle_range * 100.0) if candle_range > 0 else 0.0

        bull_impulse_ok = (not use_impulse) or (c_close > c_open and body_pct >= min_body_pct)
        bear_impulse_ok = (not use_impulse) or (c_close < c_open and body_pct >= min_body_pct)

        ema_bull_trend = ema_small > ema_big
        ema_bear_trend = ema_small < ema_big

        bull_break = (close_prev is not None and ema_small_prev is not None
                      and c_close > ema_small and close_prev <= ema_small_prev)
        bear_break = (close_prev is not None and ema_small_prev is not None
                      and c_close < ema_small and close_prev >= ema_small_prev)

        above_both = c_close > ema_small and c_close > ema_big
        below_both = c_close < ema_small and c_close < ema_big

        is_trending = (not use_trend) or (adx is not None and adx >= S2_ADX_MIN)

        ready_long = False
        ready_short = False

        for f in self.fvgs:
            if f['state'] in (-1, 2):
                continue

            top, bot, d = f['top'], f['bot'], f['dir']
            fvg_height = abs(top - bot)
            fvg_age_bars = (current_t - f['birth_t']) // CANDLE_MS

            bull_invalid = (d == 1 and body_low < bot)
            bear_invalid = (d == -1 and body_high > top)
            age_expired = use_age and fvg_age_bars > max_age

            if bull_invalid or bear_invalid or age_expired:
                f['state'] = -1
                continue

            age_ok = (not use_age) or fvg_age_bars <= max_age

            if f['state'] == 0:
                touched = (candle['l'] <= top and candle['h'] >= bot)
                can_retest = current_t > f['birth_t']

                if d == 1:
                    bull_depth = max(0.0, top - candle['l']) / fvg_height * 100.0 if fvg_height > 0 else 0.0
                    depth_ok = (not use_depth) or bull_depth <= max_retest_depth
                    if can_retest and touched and not bull_invalid and age_ok and depth_ok:
                        f['state'] = 1
                        f['retest_t'] = current_t
                else:
                    bear_depth = max(0.0, candle['h'] - bot) / fvg_height * 100.0 if fvg_height > 0 else 0.0
                    depth_ok = (not use_depth) or bear_depth <= max_retest_depth
                    if can_retest and touched and not bear_invalid and age_ok and depth_ok:
                        f['state'] = 1
                        f['retest_t'] = current_t

            # Confirmation can only happen on a bar STRICTLY AFTER the retest bar
            # (matches backtest's `current_idx > f['retest_idx']` test exactly --
            # retest and confirmation cannot be the same bar).
            if f['state'] == 1 and f['retest_t'] is not None and current_t > f['retest_t']:
                bull_confirm = bull_break or allow_continuation
                bear_confirm = bear_break or allow_continuation

                bull_ready = (d == 1 and bull_confirm and above_both
                              and ema_bull_trend and is_trending and bull_impulse_ok)
                bear_ready = (d == -1 and bear_confirm and below_both
                              and ema_bear_trend and is_trending and bear_impulse_ok)

                if bull_ready:
                    f['state'] = 2
                    ready_long = True
                if bear_ready:
                    f['state'] = 2
                    ready_short = True

        if ready_long and ready_short:
            return None
        if ready_long:  return 'LONG'
        if ready_short: return 'SHORT'
        return None


# ============================================================================
# PER-SYMBOL STATE -- tracked independently for each symbol
# ============================================================================

class SymbolState:
    """All mutable state for one symbol."""

    def __init__(self, symbol: str):
        self.symbol = symbol

        # Trade gate -- blocks new signals while a position is open on this symbol
        self.trade_open = False

        # Persistent FVG tracker for this symbol
        self.fvg_tracker = FVGTracker()

        # Last seen candle close + EMA50, used by the next bar's confirmation logic
        # (the backtest carries prev_close and prev_ema50 across loop iterations).
        self.prev_close  = None
        self.prev_ema50  = None

        # Tracks the timestamp of the last candle we processed -- avoids
        # double-processing if the same close gets delivered twice (shouldn't
        # happen with Binance's WebSocket, but cheap to defend against).
        self.last_processed_ts = 0


# ============================================================================
# SIGNAL DETECTOR -- main class
# ============================================================================

class SignalDetector:
    """
    Plug this into CandleEngine as the callback.

        detector = SignalDetector()
        detector.on_signal = my_trade_handler
        engine = CandleEngine(SYMBOLS, callback=detector.on_candle_close)

    on_signal is called with a SignalEvent whenever conditions are met.
    It is called from the WebSocket message thread -- keep it fast or
    dispatch to a queue (Step 3 does this).
    """

    def __init__(self):
        self._states  = {}                      # symbol -> SymbolState
        self._lock    = threading.Lock()
        self.on_signal: Optional[Callable] = None
        # candle_list cache: updated by engine via set_candle_list()
        self._candle_lists = {}                 # symbol -> list[dict]

    # ── Called by CandleEngine to share candle history ────────────────────────
    def set_candle_list(self, symbol: str, candle_list: list):
        """CandleEngine calls this after each push so we have access to history."""
        with self._lock:
            self._candle_lists[symbol] = candle_list

    def _get_state(self, symbol: str) -> SymbolState:
        if symbol not in self._states:
            self._states[symbol] = SymbolState(symbol)
        return self._states[symbol]

    # ── Main entry point -- called by CandleEngine on every closed candle ─────
    def on_candle_close(self, symbol: str, candle: dict, ind: dict):
        """
        symbol  : e.g. 'BTCUSDT'
        candle  : {'t': ms, 'o': float, 'h': float, 'l': float, 'c': float}
        ind     : output of compute_indicators() from Step 1
        """
        # Guard: skip if any S2-required indicator is None (warm-up not done)
        required = ['ema50', 'ema100', 'adx']
        if any(ind.get(k) is None for k in required):
            return

        state = self._get_state(symbol)
        ts    = candle['t']

        # Idempotency: only process each candle close once per symbol
        if ts <= state.last_processed_ts:
            return
        state.last_processed_ts = ts

        try:
            self._check_s2(symbol, candle, ind, state)
        finally:
            # ALWAYS update prev_close/prev_ema50 at the end of every bar,
            # regardless of whether the trade gate is open or whether a signal
            # fired. The backtest does this unconditionally too -- the cross
            # detection on bar N+1 needs accurate state from bar N even if N
            # was skipped because a trade was open. Skipping the update would
            # cause spurious "fresh break" detections after a trade closes.
            state.prev_close = candle['c']
            state.prev_ema50 = ind['ema50']

    # ==========================================================================
    # STRATEGY 2 -- FVG Retest
    # ==========================================================================

    def _check_s2(self, symbol: str, candle: dict, ind: dict, state: SymbolState):
        # ── FVG detection runs even when the trade gate is open ───────────────
        # The backtest adds new FVGs on every bar; only the SIGNAL itself is
        # gated. We mirror that: keep the tracker fed so the FVG list is
        # accurate when the gate reopens.
        candle_list = self._candle_lists.get(symbol, [])
        n_total = len(candle_list)

        # Detect a new 3-candle FVG forming on this bar.
        # Backtest:   c_now = live[idx], c_2ago = live[idx - 2]
        # Live:       c_now is the just-closed candle (last in candle_list),
        #             c_2ago is two before it.
        # We need at least 3 candles in the list to look 2 back.
        if n_total >= 3:
            c_now  = candle_list[-1]
            c_2ago = candle_list[-3]
            # Bullish FVG: candle now's low is strictly above candle 2-ago's high
            if c_now['l'] > c_2ago['h']:
                state.fvg_tracker.add(top=c_now['l'], bot=c_2ago['h'],
                                      direction=1, birth_t=c_now['t'])
            # Bearish FVG: candle now's high is strictly below candle 2-ago's low
            if c_now['h'] < c_2ago['l']:
                state.fvg_tracker.add(top=c_2ago['l'], bot=c_now['h'],
                                      direction=-1, birth_t=c_now['t'])

        # Block NEW signal emission while a trade is open on this symbol
        if state.trade_open:
            return

        # ── Walk FVGs and look for confirmation on this just-closed bar ───────
        # Use the per-symbol prev_close/prev_ema50 captured at the END of the
        # previous bar (same semantics as the backtest). DO NOT use
        # ind['close_prev']/ind['ema50_prev'] -- those would lead to subtle
        # off-by-one issues if a candle is ever processed twice due to
        # network duplication; the per-symbol cache is the authoritative source.
        ready_dir = state.fvg_tracker.update_and_find_ready(
            candle           = candle,
            current_t        = candle['t'],
            ema_small        = ind['ema50'],
            ema_big          = ind['ema100'],
            adx              = ind['adx'],
            c_open           = candle['o'],
            c_close          = candle['c'],
            max_age          = S2_MAX_FVG_AGE,
            max_retest_depth = S2_MAX_RETEST_DEPTH,
            use_age          = S2_USE_FVG_AGE,
            use_depth        = S2_USE_RETEST_DEPTH,
            use_trend        = S2_USE_TREND_FILTER,
            use_impulse      = S2_USE_IMPULSE,
            min_body_pct     = S2_MIN_BODY_PCT,
            allow_continuation = S2_ALLOW_CONTINUATION,
            ema_small_prev   = state.prev_ema50,
            close_prev       = state.prev_close,
        )

        if ready_dir is None:
            return

        # ── Build the SignalEvent ────────────────────────────────────────────
        # Entry semantics (live vs backtest):
        #   Backtest: entry = open of candle idx+1 (the bar right after the signal bar)
        #   Live    : we cannot know that open exactly, but a market order placed
        #             the instant the signal bar closes WILL fill at that very
        #             same price -- the next bar's first tick IS its open.
        # So we use the signal-candle close as the indicative entry for SL/TP
        # math. The order manager re-fetches the live ticker price at order
        # time and uses ITS fill price for the actual position's entry. SL/TP
        # placement on Binance still anchors to the SIGNAL price (Fix #2 from
        # CHANGES.md), so any small drift between candle close and order fill
        # does not push the stop deeper.

        entry = candle['c']     # indicative -- order manager will re-price at fill

        if ready_dir == 'LONG':
            sl = entry * (1 - S2_SL_PCT / 100.0)
            tp = entry * (1 + S2_TP_PCT / 100.0)
        else:
            sl = entry * (1 + S2_SL_PCT / 100.0)
            tp = entry * (1 - S2_TP_PCT / 100.0)

        signal = SignalEvent(
            strategy    = 'S2_FVG_RETEST',
            symbol      = symbol,
            direction   = ready_dir,
            entry_price = entry,
            sl_price    = sl,
            tp_price    = tp,
            signal_ts   = candle['t'],
            signal_time = _fmt_ts(candle['t']),
            reason      = (f"FVG retest {ready_dir} confirmed | "
                           f"EMA50={ind['ema50']:.4f} EMA100={ind['ema100']:.4f} | "
                           f"ADX={ind['adx']:.1f} (>={S2_ADX_MIN})"),
            lp_trigger_pct = S2_LP_TRIGGER_PCT,
            lp_lock_pct    = S2_LP_LOCK_PCT,
            indicators  = {
                'ema50':      ind['ema50'],
                'ema100':     ind['ema100'],
                'adx':        ind['adx'],
                'sl_pct':     S2_SL_PCT,
                'tp_pct':     S2_TP_PCT,
                'rr':         S2_RR_TARGET,
            }
        )

        # Open the trade gate -- closed when on_trade_closed() is called
        state.trade_open = True

        log.info(f"[SIGNAL] {symbol} S2 {ready_dir} | entry~{entry:.6f} "
                 f"SL={sl:.6f} TP={tp:.6f} | {signal.reason}")

        self._emit(signal)

    # ==========================================================================
    # TRADE OUTCOME FEEDBACK -- called by Step 3 when a trade closes
    # ==========================================================================

    def on_trade_closed(self, symbol: str, strategy: str, outcome: str):
        """
        Step 3 calls this when SL or TP is hit (or the trade is otherwise closed)
        so the detector can clear the trade_open flag and accept new signals.

        outcome: 'WIN' | 'LP_WIN' | 'LOSS' | 'MANUAL_CLOSE'
        """
        state = self._get_state(symbol)

        # Accept any S2 strategy variant just to be safe -- if we ever rename
        # the strategy ID, the gate still releases.
        if strategy and strategy.startswith('S2'):
            state.trade_open = False
            log.info(f"{symbol} S2: trade closed ({outcome}) -- gate open")

    # ==========================================================================
    # INTERNAL HELPERS
    # ==========================================================================

    def _emit(self, signal: SignalEvent):
        """Call the registered signal handler."""
        if self.on_signal:
            try:
                self.on_signal(signal)
            except Exception as e:
                log.error(f"Signal handler error: {e}", exc_info=True)
        else:
            # No handler set yet -- just log it
            log.warning(f"Signal emitted but no handler set: {signal.symbol} "
                        f"{signal.strategy} {signal.direction}")


# ============================================================================
# HELPER
# ============================================================================

def _fmt_ts(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


# ============================================================================
# S2 STRATEGY — BaseStrategy adapter
# ============================================================================
# Wraps the original SignalDetector so the multi-strategy dispatcher can drive
# S2 the same way it drives the new strategies. The S2 logic itself (FVGTracker,
# confirmation, lock-profit) is untouched — this only computes the S2 indicators
# from the rolling 15m list and forwards each close, exactly as the old
# main.candle_callback did.

class S2Strategy(BaseStrategy):
    id       = 'S2'
    full_id  = 'S2_FVG_RETEST'
    needs_1h = False

    def __init__(self, symbols):
        super().__init__()
        self.symbols  = list(symbols)
        self._detector = SignalDetector()

    # Wire the emit handler through to the underlying detector too
    def _bind(self):
        self._detector.on_signal = self._emit

    def on_candle_close(self, symbol, candle, candles_15m, candles_1h=None):
        # compute_indicators lives in step1 — import lazily to avoid an import cycle
        from step1_candle_engine import compute_indicators
        ind = compute_indicators(candles_15m)
        if ind is None:
            return
        if self._detector.on_signal is not self._emit:
            self._bind()
        self._detector.set_candle_list(symbol, candles_15m)
        self._detector.on_candle_close(symbol, candle, ind)

    def on_trade_closed(self, symbol, outcome):
        self._detector.on_trade_closed(symbol, self.full_id, outcome)


# ============================================================================
# STANDALONE TEST -- wire Step 1 + Step 2 together and watch for signals
# ============================================================================

if __name__ == '__main__':
    logging.basicConfig(
        level   = logging.INFO,
        format  = '%(asctime)s  %(levelname)-7s  %(message)s',
        datefmt = '%Y-%m-%d %H:%M:%S',
        handlers = [
            logging.FileHandler('bot.log', encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ]
    )

    # Import Step 1
    try:
        from step1_candle_engine import CandleEngine, SYMBOLS
    except ImportError:
        print("ERROR: step1_candle_engine.py must be in the same folder.")
        sys.exit(1)

    # ── Signal handler (prints to terminal) ───────────────────────────────────
    def handle_signal(sig: SignalEvent):
        print(f"""
+{'='*62}+
|  *** SIGNAL DETECTED ***
|  Strategy : {sig.strategy}
|  Symbol   : {sig.symbol}
|  Direction: {sig.direction}
|  Time     : {sig.signal_time}
|  Entry    : {sig.entry_price:.6f}
|  SL       : {sig.sl_price:.6f}
|  TP       : {sig.tp_price:.6f}
|  Reason   : {sig.reason[:55]}
+{'='*62}+
""")

    # ── Wire Step 1 -> Step 2 ────────────────────────────────────────────────
    detector = SignalDetector()
    detector.on_signal = handle_signal

    def patched_callback(symbol, candle, indicators):
        # Share candle list with detector before calling on_candle_close
        candle_list = engine.store.get_list(symbol)
        detector.set_candle_list(symbol, candle_list)
        detector.on_candle_close(symbol, candle, indicators)

    TEST_SYMBOLS = SYMBOLS   # watch all symbols
    engine = CandleEngine(TEST_SYMBOLS, callback=patched_callback)

    print(f"""
+------------------------------------------------------+
|  STEP 2 -- Signal Detector  (test mode)              |
|                                                      |
|  Watching {len(TEST_SYMBOLS)} symbols on 15m candles            |
|  S2: FVG Retest + EMA50/100 + ADX (LONG + SHORT)     |
|                                                      |
|  Signals print here when detected.                   |
|  Also logged to bot.log                              |
|  Press Ctrl+C to stop.                               |
+------------------------------------------------------+
""")

    try:
        engine.start()
    except KeyboardInterrupt:
        print("\nStopped.")
