"""
STRATEGY — ICT OPENING GAP v3  (live detector)
==============================================
Live port of ict_gap_v3_backtest.py (NDOG daily, midpoint-fade, LIMIT entry).

  TF      : 15m
  Gap     : per UTC day, gap = [min(prevClose, dayOpen), max(prevClose, dayOpen)],
            midpoint = centre.
  Entry   : once price has traded ABOVE the gap, rest a BUY LIMIT at
            mid*(1+0.02%) (fade down to midpoint); once price has traded BELOW,
            rest a SELL LIMIT at mid*(1-0.02%). One trade per day per symbol.
  Exit    : fixed 0.5% SL, 1.5% TP (3R), plus a % trailing stop that arms at
            +0.5% and trails 0.4% behind best. (exit_model = FIXED_TRAIL)
  Unfilled limits are cancelled by the order manager at the UTC day boundary
  (period_end_ts).

LIVE NOTE: with a single resting limit we commit to the FIRST side whose
precondition (above/below the gap) triggers that day. The backtest, scanning
intrabar, could in rare both-sided days take the other side after the first
never filled. This is the one intentional simplification vs the backtest, made
to guarantee one fill per day and avoid double exposure.
"""

import logging
from datetime import datetime, timezone
from strategy_base import (SignalEvent, BaseStrategy, EXIT_FIXED_TRAIL, fmt_ts)

log = logging.getLogger('ict_gap')

MS_PER_DAY = 24 * 60 * 60 * 1000

GAP_TYPE         = 'NDOG'      # daily
LONG_OFFSET_PCT  = 0.02
SHORT_OFFSET_PCT = 0.02        # symmetric (ASYMMETRIC=False in the backtest)
SL_PCT             = 0.5
TP_PCT             = 1.5
TRAIL_ACTIVATE_PCT = 0.5
TRAIL_PCT          = 0.4
USE_TRAILING       = True


def _day_key(ms):
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return (dt.year, dt.month, dt.day)


class _State:
    def __init__(self):
        self.cur_period = None
        self.gap_top = self.gap_btm = self.gap_mid = None
        self.price_was_above = False
        self.price_was_below = False
        self.emitted_today = False     # limit already placed this day
        self.prev_close = None
        self.trade_open = False
        self.last_processed_ts = 0


class ICTGapStrategy(BaseStrategy):
    id       = 'IC'
    full_id  = 'ICT_NDOG'
    needs_1h = False

    def __init__(self, symbols):
        super().__init__()
        self.symbols = list(symbols)
        self._states = {}

    def _st(self, symbol):
        if symbol not in self._states:
            self._states[symbol] = _State()
        return self._states[symbol]

    def on_trade_closed(self, symbol, outcome):
        st = self._st(symbol)
        st.trade_open = False
        # emitted_today stays True so we honour one-trade-per-day.
        log.info(f"{symbol} ICT: trade closed ({outcome})")

    def on_candle_close(self, symbol, candle, candles_15m, candles_1h=None):
        st = self._st(symbol)
        ts = candle['t']
        if ts <= st.last_processed_ts:
            return
        st.last_processed_ts = ts

        o, hi, lo, c = candle['o'], candle['h'], candle['l'], candle['c']
        pk = _day_key(ts)

        if pk != st.cur_period:
            st.cur_period = pk
            prev_close = st.prev_close if st.prev_close is not None else o
            st.gap_top = max(prev_close, o)
            st.gap_btm = min(prev_close, o)
            st.gap_mid = (st.gap_top + st.gap_btm) / 2
            st.price_was_above = False
            st.price_was_below = False
            st.emitted_today = False

        if st.gap_top is not None:
            if hi > st.gap_top:
                st.price_was_above = True
            if lo < st.gap_btm:
                st.price_was_below = True

        can_emit = (st.gap_mid is not None and not st.emitted_today
                    and not st.trade_open)

        if can_emit and (st.price_was_above or st.price_was_below):
            if st.price_was_above:
                direction = 'LONG'
                entry = st.gap_mid * (1 + LONG_OFFSET_PCT / 100)
                sl = entry * (1 - SL_PCT / 100)
                tp = entry * (1 + TP_PCT / 100)
            else:
                direction = 'SHORT'
                entry = st.gap_mid * (1 - SHORT_OFFSET_PCT / 100)
                sl = entry * (1 + SL_PCT / 100)
                tp = entry * (1 - TP_PCT / 100)

            period_end = ((ts // MS_PER_DAY) + 1) * MS_PER_DAY  # next UTC midnight

            st.emitted_today = True
            st.trade_open = True  # occupy the (strategy,symbol) slot while the limit rests

            sig = SignalEvent(
                strategy='ICT_NDOG', symbol=symbol, direction=direction,
                entry_price=entry, sl_price=sl, tp_price=tp,
                signal_ts=ts, signal_time=fmt_ts(ts),
                reason=(f"ICT gap fade {direction} | mid={st.gap_mid:.6f} "
                        f"gap=[{st.gap_btm:.6f},{st.gap_top:.6f}]"),
                exit_model=EXIT_FIXED_TRAIL,
                entry_type='LIMIT',
                limit_price=entry,
                period_end_ts=period_end,
                trail_activate_pct=TRAIL_ACTIVATE_PCT if USE_TRAILING else None,
                trail_pct=TRAIL_PCT if USE_TRAILING else None,
                indicators={'gap_top': st.gap_top, 'gap_btm': st.gap_btm,
                            'gap_mid': st.gap_mid, 'sl_pct': SL_PCT, 'tp_pct': TP_PCT},
            )
            log.info(f"[SIGNAL] {symbol} ICT {direction} LIMIT@{entry:.6f} "
                     f"SL={sl:.6f} TP={tp:.6f} (cancel @ {fmt_ts(period_end)})")
            self._emit(sig)

        st.prev_close = c
