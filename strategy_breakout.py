"""
STRATEGY — MULTI DAILY BREAKOUT  (live detector)
================================================
1:1 live port of BacktestBreakout_6coins_2000d_RR3.py signal generation.

  TF       : 15m
  Range    : NY 09:00-13:00 opening range (America/New_York, DST-aware)
  Entry    : a 15m candle that CLOSES beyond the locked range, gated by
             EMA200 trend, ADX>=20, ATR% band, 1.2x volume surge, min range size.
             Up to 4 trades/day per symbol, one open at a time.
  Exit     : SL at opposite range edge, fixed TP at 3R, trail SL once +1.5R
             (1.5*ATR behind extreme), trail TP once +2.5R (close pulls back
             1.2*ATR from extreme), 48h (192-bar) time-stop.
             (exit_model = BREAKOUT_TRAIL)
"""

import logging
from datetime import datetime, timezone, timedelta
from strategy_base import (SignalEvent, BaseStrategy, EXIT_BREAKOUT_TRAIL, fmt_ts)
from strategy_indicators import ema_series, atr_series, adx_series

log = logging.getLogger('breakout')

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except Exception:
    NY_TZ = None

SESSION_START_HOUR = 9
SESSION_END_HOUR   = 13

EMA_TREND  = 200
ADX_PERIOD = 14
ATR_PERIOD = 14
VOL_MA     = 20

RR              = 3.0
USE_RANGE_SL    = True
ATR_SL_MULT     = 1.5
TRAIL_TRIGGER_R = 1.5
TRAIL_SL_ATR    = 1.5
TP_LOCK_TRIGGER_R = 2.5
TRAIL_TP_ATR    = 1.2
MAX_HOLD_CANDLES = 192

FILTER_EMA200_TREND  = True
FILTER_ADX_MIN       = 20.0
FILTER_MIN_ATR_PCT   = 0.10
FILTER_MAX_ATR_PCT   = 5.0
FILTER_VOL_SURGE     = 1.2
FILTER_MIN_RANGE_PCT = 0.20
MAX_TRADES_PER_DAY   = 4

MIN_BARS = EMA_TREND + 10


def _ny_parts(ms):
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    if NY_TZ is not None:
        dt = dt.astimezone(NY_TZ)
    else:
        dt = dt - timedelta(hours=4)
    return dt.strftime('%Y-%m-%d'), dt.hour


def _in_session(ms):
    _, h = _ny_parts(ms)
    return SESSION_START_HOUR <= h < SESSION_END_HOUR


def _indicators(candles):
    if len(candles) < MIN_BARS:
        return None
    closes = [c['c'] for c in candles]
    highs  = [c['h'] for c in candles]
    lows   = [c['l'] for c in candles]
    vols   = [c.get('v', 0.0) for c in candles]
    n = len(candles)

    e200 = ema_series(closes, EMA_TREND)
    adx_s = adx_series(highs, lows, closes, ADX_PERIOD)
    atr_s = atr_series(highs, lows, closes, ATR_PERIOD)
    atr = atr_s[-1]
    atr_pct = (atr / closes[-1] * 100) if (atr and closes[-1] > 0) else None
    vol_ma = sum(vols[-VOL_MA:]) / VOL_MA if n >= VOL_MA else None

    return {'ema200': e200[-1], 'adx': adx_s[-1], 'atr': atr,
            'atr_pct': atr_pct, 'vol': vols[-1], 'vol_ma': vol_ma}


class _State:
    def __init__(self):
        self.cur_day = None
        self.range_high = None
        self.range_low = None
        self.range_locked = False
        self.trades_today = 0
        self.trade_open = False
        self.last_processed_ts = 0


class BreakoutStrategy(BaseStrategy):
    id       = 'BR'
    full_id  = 'BREAKOUT_NY4H'
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
        log.info(f"{symbol} BREAKOUT: trade closed ({outcome}) — gate open")

    def on_candle_close(self, symbol, candle, candles_15m, candles_1h=None):
        st = self._st(symbol)
        ts = candle['t']
        if ts <= st.last_processed_ts:
            return
        st.last_processed_ts = ts

        ind = _indicators(candles_15m)
        day, _ = _ny_parts(ts)

        # new NY day → reset opening range
        if day != st.cur_day:
            st.cur_day = day
            st.range_high = None
            st.range_low = None
            st.range_locked = False
            st.trades_today = 0

        in_sess = _in_session(ts)
        if in_sess:
            st.range_high = candle['h'] if st.range_high is None else max(st.range_high, candle['h'])
            st.range_low  = candle['l'] if st.range_low  is None else min(st.range_low,  candle['l'])
            return  # never trade inside the range-building window

        if (not in_sess) and (st.range_high is not None) and (not st.range_locked):
            st.range_locked = True

        if not st.range_locked or ind is None:
            return
        if st.trade_open:
            return
        if st.trades_today >= MAX_TRADES_PER_DAY:
            return
        if any(ind.get(k) is None for k in ('ema200', 'adx', 'atr', 'atr_pct', 'vol_ma')):
            return

        c_close = candle['c']
        atr = ind['atr']
        rng = st.range_high - st.range_low
        rng_pct = rng / c_close * 100 if c_close else 0

        if rng_pct < FILTER_MIN_RANGE_PCT:
            return
        if not (FILTER_MIN_ATR_PCT <= ind['atr_pct'] <= FILTER_MAX_ATR_PCT):
            return
        if FILTER_ADX_MIN and ind['adx'] < FILTER_ADX_MIN:
            return
        if ind['vol_ma'] and ind['vol'] < FILTER_VOL_SURGE * ind['vol_ma']:
            return

        long_bo  = c_close > st.range_high
        short_bo = c_close < st.range_low
        if not (long_bo or short_bo):
            return
        d = 'LONG' if long_bo else 'SHORT'

        if FILTER_EMA200_TREND:
            if d == 'LONG' and c_close <= ind['ema200']:
                return
            if d == 'SHORT' and c_close >= ind['ema200']:
                return

        entry = c_close  # live market fill at signal close (≈ next-open in backtest)
        if USE_RANGE_SL:
            sl = st.range_low if d == 'LONG' else st.range_high
        else:
            sl = entry - ATR_SL_MULT * atr if d == 'LONG' else entry + ATR_SL_MULT * atr
        risk = abs(entry - sl)
        if risk <= 0:
            return
        tp = entry + RR * risk if d == 'LONG' else entry - RR * risk

        st.trade_open = True
        st.trades_today += 1

        sig = SignalEvent(
            strategy='BREAKOUT_NY4H', symbol=symbol, direction=d,
            entry_price=entry, sl_price=sl, tp_price=tp,
            signal_ts=ts, signal_time=fmt_ts(ts),
            reason=(f"NY4H breakout {d} | range {rng_pct:.2f}% | "
                    f"ADX={ind['adx']:.1f} ATR%={ind['atr_pct']:.2f} "
                    f"vol={ind['vol']/ind['vol_ma']:.2f}x"),
            exit_model=EXIT_BREAKOUT_TRAIL,
            entry_type='MARKET',
            atr_at_signal=atr, init_risk=risk,
            trail_trigger_r=TRAIL_TRIGGER_R, trail_sl_atr=TRAIL_SL_ATR,
            tp_lock_trigger_r=TP_LOCK_TRIGGER_R, trail_tp_atr=TRAIL_TP_ATR,
            max_hold_candles=MAX_HOLD_CANDLES,
            indicators={'adx': ind['adx'], 'atr': atr, 'atr_pct': ind['atr_pct'],
                        'range_pct': rng_pct, 'rr': RR},
        )
        log.info(f"[SIGNAL] {symbol} BREAKOUT {d} | entry~{entry:.6f} "
                 f"SL={sl:.6f} TP={tp:.6f} | trade {st.trades_today}/{MAX_TRADES_PER_DAY} today")
        self._emit(sig)
