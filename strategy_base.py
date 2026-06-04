"""
STRATEGY BASE
=============
Canonical SignalEvent (shared by all strategies) and the BaseStrategy interface
the dispatcher uses to drive every strategy uniformly.

The original build had a single S2-only SignalEvent. To run four strategies with
four different exit models, the SignalEvent now carries an ``exit_model`` tag plus
the parameters each model needs. All new fields are optional with defaults, so the
S2 path is byte-for-byte unchanged (it just uses exit_model='FIXED_LP').

EXIT MODELS
-----------
  FIXED_LP        S2       market entry, fixed SL, fixed TP, lock-profit at 50%→10%
  PARTIAL_TRAIL   AxisPro  market entry, SL0, TP1 closes 50% → SL to break-even →
                           trail runner by EMA(trail_ema_period), TP2 closes rest
  BREAKOUT_TRAIL  Breakout market entry, fixed SL (range edge), fixed TP (3R),
                           trail SL once +trail_trigger_r (by trail_sl_atr*ATR),
                           trail TP once +tp_lock_trigger_r (close pulls back
                           trail_tp_atr*ATR from extreme), time-stop at max_hold
  FIXED_TRAIL     ICT      LIMIT entry at limit_price, fixed SL/TP (%), then a
                           percentage trailing stop (arms at trail_activate_pct,
                           trails trail_pct behind best); unfilled limit cancelled
                           at period_end_ts
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable, List


# Exit-model identifiers
EXIT_FIXED_LP       = 'FIXED_LP'
EXIT_PARTIAL_TRAIL  = 'PARTIAL_TRAIL'
EXIT_BREAKOUT_TRAIL = 'BREAKOUT_TRAIL'
EXIT_FIXED_TRAIL    = 'FIXED_TRAIL'


@dataclass
class SignalEvent:
    # ── core (all strategies) ────────────────────────────────────────────────
    strategy:    str            # full id, e.g. 'AXISPRO', 'BREAKOUT_NY4H', 'ICT_NDOG', 'S2_FVG_RETEST'
    symbol:      str
    direction:   str            # 'LONG' | 'SHORT'
    entry_price: float          # indicative entry (signal-candle close); manager re-prices market fills
    sl_price:    float
    tp_price:    float          # primary TP (final target). For AxisPro this equals tp2.
    signal_ts:   int            # candle close timestamp (ms)
    signal_time: str
    reason:      str

    # ── exit model selector ──────────────────────────────────────────────────
    exit_model:  str = EXIT_FIXED_LP

    # ── entry type ────────────────────────────────────────────────────────────
    entry_type:  str = 'MARKET'         # 'MARKET' | 'LIMIT'
    limit_price: Optional[float] = None  # for LIMIT entries (ICT)
    period_end_ts: Optional[int] = None  # cancel unfilled LIMIT at this ms timestamp (ICT day end)

    # ── S2 lock-profit ────────────────────────────────────────────────────────
    lp_trigger_pct: Optional[float] = None
    lp_lock_pct:    Optional[float] = None

    # ── AxisPro partial-trail params ──────────────────────────────────────────
    tp1_price:        Optional[float] = None
    tp2_price:        Optional[float] = None
    tp1_frac:         Optional[float] = None     # fraction closed at TP1 (e.g. 0.5)
    move_be_after_tp1: bool = True
    trail_ema_period: Optional[int] = None       # 21 for AxisPro (trail runner by this EMA)

    # ── Breakout / R-based trail params ───────────────────────────────────────
    atr_at_signal:     Optional[float] = None    # frozen ATR used for trail distances + R math
    init_risk:         Optional[float] = None    # |entry - sl|
    trail_trigger_r:   Optional[float] = None
    trail_sl_atr:      Optional[float] = None
    tp_lock_trigger_r: Optional[float] = None
    trail_tp_atr:      Optional[float] = None
    max_hold_candles:  Optional[int]   = None

    # ── ICT percentage-trail params ───────────────────────────────────────────
    trail_activate_pct: Optional[float] = None
    trail_pct:          Optional[float] = None

    # ── diagnostics ─────────────────────────────────────────────────────────
    indicators: dict = field(default_factory=dict)


def fmt_ts(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


class BaseStrategy:
    """
    Uniform interface the dispatcher drives. Each concrete strategy:
      - declares `id` (short, e.g. 'AX'), `full_id` (e.g. 'AXISPRO'), `symbols`
      - sets `needs_1h = True` if it requires an HTF 1h feed (AxisPro)
      - implements on_candle_close(symbol, candle, candles_15m, candles_1h)
      - calls self._emit(SignalEvent) when a setup confirms
      - implements on_trade_closed(symbol, outcome) to release its per-symbol gate

    `on_signal` is set by main(); it forwards to alerts + order manager.
    """
    id:       str = 'XX'
    full_id:  str = 'BASE'
    symbols:  List[str] = []
    needs_1h: bool = False

    def __init__(self):
        self.on_signal: Optional[Callable] = None

    # candle is the just-closed candle dict {t,o,h,l,c,v}; candles_15m is the
    # rolling list ending with `candle`; candles_1h is the rolling 1h list (or None).
    def on_candle_close(self, symbol: str, candle: dict,
                        candles_15m: list, candles_1h: Optional[list] = None):
        raise NotImplementedError

    def on_trade_closed(self, symbol: str, outcome: str):
        pass

    def _emit(self, signal: SignalEvent):
        if self.on_signal:
            self.on_signal(signal)
