"""
STRATEGY — AXIS-PRO  (live detector)
====================================
1:1 live port of AxisPro_Backtest_15m_top50.py signal generation.

  Entry TF : 15m      Bias TF : 1h (EMA200)
  Flow     : HTF bias gate -> ATR expansion -> break of structure across last
             pivot (+ATR buffer) -> Fib pullback into 38-62% (cancel >80%) ->
             strong-close confirmation inside EMA21/EMA50 band, above/below
             EMA200, cooldown.
  Exit     : SL = wider of (1.6*ATR, swing50) capped 6*ATR.
             TP1 = 1.5R closes 50% -> SL to break-even -> trail runner by EMA21.
             TP2 = 3.0R closes the rest.  (exit_model = PARTIAL_TRAIL)

Per-symbol state (last_sh/last_sl, armed flags, impulse leg, fib zone, cooldown)
is carried across candle closes exactly like the backtest carried it across the
bar loop.
"""

import logging
from strategy_base import (SignalEvent, BaseStrategy, EXIT_PARTIAL_TRAIL, fmt_ts)
from strategy_indicators import (ema_series, atr_series, sma_of_series,
                                 pivot_high, pivot_low)

log = logging.getLogger('axispro')

CANDLE_MS_15 = 15 * 60 * 1000

# ── Params (Pine defaults, identical to the backtest) ─────────────────────────
EMA_FAST   = 21
EMA_MID    = 50
EMA_200    = 200
HTF_EMA    = 200
PIVOT_L    = 5
PIVOT_R    = 5
IMP_LOOK   = 60
FIB_MIN    = 0.38
FIB_MAX    = 0.62
FIB_CANCEL = 0.80
COOLDOWN   = 5

ATR_LEN     = 14
ATR_BASE_L  = 100
ATR_RATIO   = 1.05
USE_ATR_EXP = True

SL_X_ATR    = 1.6
MAX_SL_XATR = 6.0
SWING_LB    = 50

TP1_RR  = 1.5
TP2_RR  = 3.0
TP1_PCT = 0.50
MOVE_BE = True
TRAIL   = True

USE_EMA200  = True
USE_BOS_BUF = True
BOS_BUF_ATR = 0.10
USE_STRONG_C = True
NEAR_BAND_MODE = "band"
NEAR_ATR_MULT  = 1.0

MIN_BARS_15 = EMA_200 + 10        # warm-up gate on 15m
MIN_BARS_1H = HTF_EMA + 5         # warm-up gate on 1h


class _State:
    def __init__(self):
        self.last_sh = None
        self.last_sl = None
        self.armed_long = False
        self.armed_short = False
        self.imp_start = None
        self.imp_end = None
        self.z_lo = self.z_hi = self.z_inv = None
        self.last_entry_ts = None
        self.trade_open = False
        self.last_processed_ts = 0


class AxisProStrategy(BaseStrategy):
    id       = 'AX'
    full_id  = 'AXISPRO'
    needs_1h = True

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
        st.armed_long = st.armed_short = False
        log.info(f"{symbol} AXISPRO: trade closed ({outcome}) — gate open")

    def _htf_bias(self, candles_1h):
        if not candles_1h or len(candles_1h) < MIN_BARS_1H:
            return 0
        closes = [c['c'] for c in candles_1h]
        e200 = ema_series(closes, HTF_EMA)
        if e200[-1] is None:
            return 0
        last_close = closes[-1]
        if last_close > e200[-1]:
            return 1
        if last_close < e200[-1]:
            return -1
        return 0

    def on_candle_close(self, symbol, candle, candles_15m, candles_1h=None):
        st = self._st(symbol)
        ts = candle['t']
        if ts <= st.last_processed_ts:
            return
        st.last_processed_ts = ts

        n = len(candles_15m)
        if n < MIN_BARS_15:
            return

        closes = [c['c'] for c in candles_15m]
        highs  = [c['h'] for c in candles_15m]
        lows   = [c['l'] for c in candles_15m]
        opens  = [c['o'] for c in candles_15m]

        ema_f = ema_series(closes, EMA_FAST)
        ema_m = ema_series(closes, EMA_MID)
        ema2  = ema_series(closes, EMA_200)
        atr_s = atr_series(highs, lows, closes, ATR_LEN)
        atr_base = sma_of_series(atr_s, ATR_BASE_L)

        i = n - 1   # current (just-closed) bar

        # ── confirm a pivot that has completed PIVOT_R bars back ──────────────
        pv_idx = i - PIVOT_R
        if pv_idx >= 0:
            ph = pivot_high(highs, pv_idx, PIVOT_L, PIVOT_R)
            pl = pivot_low(lows, pv_idx, PIVOT_L, PIVOT_R)
            if ph is not None:
                st.last_sh = ph
            if pl is not None:
                st.last_sl = pl

        c_close = closes[i]; c_open = opens[i]
        c_high = highs[i]; c_low = lows[i]
        prev_close = closes[i - 1]

        atr = atr_s[i]
        if atr is None or ema_f[i] is None or ema_m[i] is None or ema2[i] is None:
            return

        ab = atr_base[i]
        atr_ok = (not USE_ATR_EXP) or (ab is not None and ab > 0 and atr / ab >= ATR_RATIO)

        bias = self._htf_bias(candles_1h)
        bias_long = bias > 0
        bias_short = bias < 0

        bos_buf = atr * BOS_BUF_ATR

        cross_up = st.last_sh is not None and prev_close <= st.last_sh and c_close > st.last_sh
        cross_dn = st.last_sl is not None and prev_close >= st.last_sl and c_close < st.last_sl

        bos_up0 = bias_long and atr_ok and cross_up
        bos_dn0 = bias_short and atr_ok and cross_dn
        bos_up = (bos_up0 and c_close > st.last_sh + bos_buf) if USE_BOS_BUF else bos_up0
        bos_dn = (bos_dn0 and c_close < st.last_sl - bos_buf) if USE_BOS_BUF else bos_dn0

        lo_win = i - IMP_LOOK + 1
        global_low = min(lows[max(0, lo_win):i + 1])
        global_high = max(highs[max(0, lo_win):i + 1])

        if bos_up:
            st.armed_long, st.armed_short = True, False
            st.imp_start, st.imp_end = global_low, c_high
        if bos_dn:
            st.armed_short, st.armed_long = True, False
            st.imp_start, st.imp_end = global_high, c_low

        if st.armed_long and st.imp_start is not None and st.imp_end is not None:
            rng = st.imp_end - st.imp_start
            st.z_hi = st.imp_end - rng * FIB_MIN
            st.z_lo = st.imp_end - rng * FIB_MAX
            st.z_inv = st.imp_end - rng * FIB_CANCEL
        if st.armed_short and st.imp_start is not None and st.imp_end is not None:
            rng = st.imp_start - st.imp_end
            st.z_lo = st.imp_end + rng * FIB_MIN
            st.z_hi = st.imp_end + rng * FIB_MAX
            st.z_inv = st.imp_end + rng * FIB_CANCEL

        if st.armed_long and st.z_inv is not None and c_low < st.z_inv:
            st.armed_long = False
        if st.armed_short and st.z_inv is not None and c_high > st.z_inv:
            st.armed_short = False

        touch_long = st.armed_long and st.z_lo is not None and c_low <= st.z_hi and c_high >= st.z_lo
        touch_short = st.armed_short and st.z_lo is not None and c_high >= st.z_lo and c_low <= st.z_hi

        if USE_STRONG_C:
            confirm_long = c_close > ema_f[i] and c_close > highs[i - 1]
            confirm_short = c_close < ema_f[i] and c_close < lows[i - 1]
        else:
            confirm_long = c_close > c_open and c_close > ema_f[i]
            confirm_short = c_close < c_open and c_close < ema_f[i]

        if NEAR_BAND_MODE == "band":
            band_lo = min(ema_f[i], ema_m[i]); band_hi = max(ema_f[i], ema_m[i])
            near_long = band_lo <= c_close <= band_hi
            near_short = band_lo <= c_close <= band_hi
        else:
            near_long = (c_close - ema_f[i]) <= NEAR_ATR_MULT * atr
            near_short = (ema_f[i] - c_close) <= NEAR_ATR_MULT * atr

        ema_ok_long = (not USE_EMA200) or c_close > ema2[i]
        ema_ok_short = (not USE_EMA200) or c_close < ema2[i]

        cd_ok = (st.last_entry_ts is None or
                 (ts - st.last_entry_ts) // CANDLE_MS_15 > COOLDOWN)

        long_sig = cd_ok and touch_long and confirm_long and near_long and ema_ok_long
        short_sig = cd_ok and touch_short and confirm_short and near_short and ema_ok_short

        if not (long_sig or short_sig):
            return
        if st.trade_open:
            return   # one position per symbol for this strategy

        direction = 'LONG' if long_sig else 'SHORT'

        # entry ≈ next candle open → live market fill at signal close
        entry = c_close
        sw_low = min(lows[max(0, i - SWING_LB + 1):i + 1])
        sw_high = max(highs[max(0, i - SWING_LB + 1):i + 1])

        if direction == 'LONG':
            sl_atr = entry - atr * SL_X_ATR
            sl0 = min(sl_atr, sw_low)
            if MAX_SL_XATR > 0 and (entry - sl0) > atr * MAX_SL_XATR:
                sl0 = entry - atr * MAX_SL_XATR
            risk = entry - sl0
            tp1 = entry + risk * TP1_RR
            tp2 = entry + risk * TP2_RR
        else:
            sl_atr = entry + atr * SL_X_ATR
            sl0 = max(sl_atr, sw_high)
            if MAX_SL_XATR > 0 and (sl0 - entry) > atr * MAX_SL_XATR:
                sl0 = entry + atr * MAX_SL_XATR
            risk = sl0 - entry
            tp1 = entry - risk * TP1_RR
            tp2 = entry - risk * TP2_RR

        if risk <= 0:
            return

        st.trade_open = True
        st.last_entry_ts = ts

        sig = SignalEvent(
            strategy='AXISPRO', symbol=symbol, direction=direction,
            entry_price=entry, sl_price=sl0, tp_price=tp2,
            signal_ts=ts, signal_time=fmt_ts(ts),
            reason=(f"AxisPro {direction} | BOS+FibPB | bias={bias} "
                    f"ATRx={atr/ab:.2f} risk={risk/entry*100:.2f}%"),
            exit_model=EXIT_PARTIAL_TRAIL,
            entry_type='MARKET',
            tp1_price=tp1, tp2_price=tp2, tp1_frac=TP1_PCT,
            move_be_after_tp1=MOVE_BE,
            trail_ema_period=EMA_FAST if TRAIL else None,
            init_risk=risk,
            indicators={'ema21': ema_f[i], 'ema50': ema_m[i], 'ema200': ema2[i],
                        'atr': atr, 'bias': bias, 'tp1_rr': TP1_RR, 'tp2_rr': TP2_RR},
        )
        log.info(f"[SIGNAL] {symbol} AXISPRO {direction} | entry~{entry:.6f} "
                 f"SL={sl0:.6f} TP1={tp1:.6f} TP2={tp2:.6f}")
        self._emit(sig)
