"""
STRATEGY REGISTRY + DISPATCHER
==============================
Defines the four live strategies, their coin universes, and routes every 15m
candle close to the strategies that watch that symbol. Also routes trade-close
feedback back to the originating strategy so its per-symbol gate re-opens.

Coin universes (per the user's notes / backtests):
  S2        — from step1.SYMBOLS (unchanged original universe)
  AxisPro   — SOL SUI TRX WLD OP RUNE HBAR ALGO DYDX JUP   (AAVE/WIF/FET/LDO dropped)
  Breakout  — XLM ZEC SUI INJ TAO FET
  ICT Gap   — AIGENSYN BEAT CRCL ESPORT GENI HET HYPE IO MST MU SNDK XLM XPL ZEC

Several coins are shared across strategies (e.g. WLD in S2+AxisPro, SUI in
AxisPro+Breakout, XLM/ZEC in Breakout+ICT, XPL in S2+ICT). The order manager
keys positions by (strategy, symbol), so each strategy trades its coins
independently even when another strategy holds the same coin.
"""

import logging

from step1_candle_engine import SYMBOLS as S2_SYMBOLS
from step2_signal_detector import S2Strategy
from strategy_axispro import AxisProStrategy
from strategy_breakout import BreakoutStrategy
from strategy_ict import ICTGapStrategy

log = logging.getLogger('registry')

AXISPRO_SYMBOLS = [
    "SOLUSDT", "SUIUSDT", "TRXUSDT", "WLDUSDT", "OPUSDT",
    "RUNEUSDT", "HBARUSDT", "ALGOUSDT", "DYDXUSDT", "JUPUSDT",
]

BREAKOUT_SYMBOLS = [
    "XLMUSDT", "ZECUSDT", "SUIUSDT", "INJUSDT", "TAOUSDT", "FETUSDT",
]

ICT_SYMBOLS = [
    "AIGENSYNUSDT", "BEATUSDT", "CRCLUSDT", "ESPORTUSDT", "GENIUSDT",
    "HETUSDT", "HYPEUSDT", "IOUSDT", "MSTUSDT", "MUUSDT", "SNDKUSDT",
    "XLMUSDT", "XPLUSDT", "ZECUSDT",
]


class StrategyDispatcher:
    """Owns all strategies and fans candle closes out to the right ones."""

    def __init__(self, valid_symbols=None):
        """
        valid_symbols: optional set of symbols confirmed to exist on the futures
        endpoint. When provided, each strategy's universe is filtered to it so a
        mistyped or unlisted coin (e.g. a tokenized-stock perp the venue doesn't
        carry) is simply dropped with a warning instead of breaking the stream.
        """
        self.strategies = [
            S2Strategy(S2_SYMBOLS),
            AxisProStrategy(AXISPRO_SYMBOLS),
            BreakoutStrategy(BREAKOUT_SYMBOLS),
            ICTGapStrategy(ICT_SYMBOLS),
        ]

        if valid_symbols is not None:
            valid = set(valid_symbols)
            for s in self.strategies:
                kept = [sym for sym in s.symbols if sym in valid]
                dropped = [sym for sym in s.symbols if sym not in valid]
                if dropped:
                    log.warning(f"{s.full_id}: dropping {len(dropped)} symbol(s) "
                                f"not listed on futures: {', '.join(dropped)}")
                s.symbols = kept

        # symbol -> [strategies watching it]  (15m)
        self._by_symbol = {}
        for s in self.strategies:
            for sym in s.symbols:
                self._by_symbol.setdefault(sym, []).append(s)

        # full_id -> strategy (for trade-close routing)
        self._by_id = {s.full_id: s for s in self.strategies}

    # ── universes the engine subscribes to ───────────────────────────────────
    @property
    def symbols_15m(self):
        return sorted(self._by_symbol.keys())

    @property
    def symbols_1h(self):
        out = set()
        for s in self.strategies:
            if s.needs_1h:
                out.update(s.symbols)
        return sorted(out)

    # ── wiring ────────────────────────────────────────────────────────────────
    def set_signal_handler(self, handler):
        for s in self.strategies:
            s.on_signal = handler

    # ── engine callback: a 15m candle just closed for `symbol` ────────────────
    def on_15m_close(self, symbol, candle, candles_15m, candles_1h=None):
        for s in self._by_symbol.get(symbol, []):
            try:
                c1h = candles_1h if s.needs_1h else None
                s.on_candle_close(symbol, candle, candles_15m, c1h)
            except Exception as e:
                log.error(f"{s.full_id} error on {symbol}: {e}", exc_info=True)

    # ── manager feedback: a position closed ───────────────────────────────────
    def on_trade_closed(self, strategy_full_id, symbol, outcome):
        s = self._by_id.get(strategy_full_id)
        if s is None:
            # tolerate short ids / renames
            for cand in self.strategies:
                if strategy_full_id and strategy_full_id.startswith(cand.id):
                    s = cand
                    break
        if s is not None:
            try:
                s.on_trade_closed(symbol, outcome)
            except Exception as e:
                log.error(f"on_trade_closed error: {e}", exc_info=True)

    def summary(self):
        return ", ".join(f"{s.full_id}={len(s.symbols)}" for s in self.strategies)
