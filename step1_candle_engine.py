"""
STEP 1 OF 4 — Candle Engine
============================
Binance Futures WebSocket bot — S2 (FVG Retest) live runner

What this file does:
  - Connects to Binance Futures WebSocket for all symbols
  - Maintains a rolling window of closed 15m candles per symbol
  - Computes the indicators S2 needs on every candle close:
      EMA50, EMA100, ADX(14)
  - Exposes a callback: on_candle_close(symbol, candle, indicators)
  - Seeds indicator history by fetching REST candles on startup
  - Handles reconnection automatically

Strategy parity note:
  Indicator periods, formulas, and warm-up gating mirror BacktestZair_S2_only.py
  exactly. The 200-bar warm-up requirement (EMA_TREND) is kept as the gate that
  blocks signals until at least 210 closed bars are available, even though S2
  itself only needs EMA50/EMA100 — the gate matches the backtest so live and
  backtest agree on what counts as "ready to trade".

Dependencies:
  pip install websocket-client requests

Run this file standalone to verify it connects and prints candle closes:
  python3 step1_candle_engine.py

Configuration (edit the block below):
  TESTNET = True   → uses demo-fapi (paper trading)
  TESTNET = False  → uses live api.binance.com  (careful!)
"""

import json
import threading
import time
import requests
import logging
import sys
import io
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional for step1 standalone test
from collections import deque
from datetime import datetime, timezone

# Force UTF-8 output on Windows so special characters don't crash the logger
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ============================================================================
# CONFIGURATION
# ============================================================================

TESTNET       = os.getenv('TESTNET', 'true').lower() == 'true'
INTERVAL      = os.getenv('INTERVAL', '15m')
HTF_INTERVAL  = os.getenv('HTF_INTERVAL', '1h')   # AxisPro bias timeframe
CANDLE_LIMIT  = 300      # rolling 15m window per symbol (>=210 for warm-up gates)
HTF_LIMIT     = 400      # rolling 1h window (>=210 for EMA200 bias)
RECONNECT_SEC = 5        # seconds between reconnection attempts

# Indicator periods (must match BacktestZair_S2_only.py)
EMA_SMALL     = 50       # S2 emaLen1
EMA_BIG       = 100      # S2 emaLen2
EMA_TREND     = 200      # warm-up gate only - need >=210 bars before live signals
ADX_PERIOD    = 14
ATR_PERIOD    = 14       # not used by S2 strategy itself, kept for safeguards/diagnostics

# REST and WebSocket endpoints — all futures, no spot
# Market data comes from public Binance Futures (fapi) — no API key needed.
# Trade execution goes to demo-fapi (testnet) or fapi (live).
if TESTNET:
    REST_BASE           = "https://demo-fapi.binance.com/fapi"   # order placement
else:
    REST_BASE           = "https://fapi.binance.com/fapi"

REST_DATA_BASE = "https://fapi.binance.com/fapi"   # candle seeding (always live public)

# WebSocket — futures stream only
if TESTNET:
    WS_BASE = "wss://fstream.binancefuture.com/stream"
else:
    WS_BASE = "wss://fstream.binance.com/stream"

# ============================================================================
# SYMBOL UNIVERSE — user-curated 8-symbol set
# All 8 must exist on Binance Futures (fapi). Note that several of the newer
# listings (PUMPUSDT, XPLUSDT, MONUSDT) launched with reduced max leverage
# (typically 5-20x rather than 50x). The order manager's -2027 retry logic
# will automatically back off to a valid leverage tier on first try, so this
# is handled transparently.
# ============================================================================

SYMBOLS = [
    "BTCUSDT",
    "ENAUSDT",
    "WLDUSDT",
    "PUMPUSDT",
    "XPLUSDT",
    "1000SHIBUSDT",
    "APTUSDT",
    "MONUSDT",
]

# Deduplicate while preserving order (defensive — no overlap expected)
_seen = set(); _deduped = []
for _s in SYMBOLS:
    if _s not in _seen:
        _seen.add(_s); _deduped.append(_s)
SYMBOLS = _deduped

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger('candle_engine')

# ============================================================================
# INDICATOR CALCULATIONS
# Same formulas as BacktestZair_S2_only.py (EMA seeding via SMA, Wilder ADX)
# ============================================================================

def _ema_from_list(values, period):
    """Return EMA series (list, same length as values, None until warm).
    Seeded with simple average of first `period` values, then standard EMA.
    Mirrors ema_series() in BacktestZair_S2_only.py exactly.
    """
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, n):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def compute_indicators(candles):
    """
    Given a list of candle dicts:
      {'t': timestamp_ms, 'o': float, 'h': float, 'l': float, 'c': float}
    Returns a dict of indicator values AT THE LAST CANDLE, or None if
    not enough data.

    Output keys S2 reads:
      ema50, ema100, ema50_prev, close_prev, adx
    Plus raw OHLC for convenience.

    Warm-up gate: requires >= EMA_TREND + 10 = 210 candles (matches backtest).
    """
    if len(candles) < EMA_TREND + 10:
        return None

    closes = [c['c'] for c in candles]
    highs  = [c['h'] for c in candles]
    lows   = [c['l'] for c in candles]
    n      = len(candles)

    # ── EMAs (50 and 100 — S2 needs both, current and previous) ─────────────
    ema50_s  = _ema_from_list(closes, EMA_SMALL)
    ema100_s = _ema_from_list(closes, EMA_BIG)

    ema50  = ema50_s[-1]
    ema100 = ema100_s[-1]

    # Previous EMA50 + previous close — required for break-cross detection.
    # The backtest carries prev_ema50 and prev_close across loop iterations;
    # here we expose them in the indicator dict so the detector can read them
    # alongside the rest. Detector still keeps its own per-symbol "prev" cache
    # but having them on the snapshot is useful for logging/audit.
    ema50_prev = ema50_s[-2]  if len(ema50_s)  >= 2 else None
    close_prev = closes[-2]    if len(closes)   >= 2 else None

    if None in (ema50, ema100, ema50_prev, close_prev):
        return None

    # ── ADX (Wilder) — exactly as in the backtest ───────────────────────────
    p = ADX_PERIOD
    tr_raw = [0.0] * n
    dm_p   = [0.0] * n
    dm_n   = [0.0] * n
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        tr_raw[i] = max(h - l, abs(h - pc), abs(l - pc))
        up   = highs[i]    - highs[i - 1]
        down = lows[i - 1] - lows[i]
        if up > down and up > 0:   dm_p[i] = up
        if down > up and down > 0: dm_n[i] = down

    s_tr = [0.0]*n; s_dp = [0.0]*n; s_dn = [0.0]*n
    if n > p:
        s_tr[p] = sum(tr_raw[1:p+1])
        s_dp[p] = sum(dm_p[1:p+1])
        s_dn[p] = sum(dm_n[1:p+1])
        for i in range(p+1, n):
            s_tr[i] = s_tr[i-1] - s_tr[i-1]/p + tr_raw[i]
            s_dp[i] = s_dp[i-1] - s_dp[i-1]/p + dm_p[i]
            s_dn[i] = s_dn[i-1] - s_dn[i-1]/p + dm_n[i]

    dx_s = [None]*n
    for i in range(p, n):
        atr_v = s_tr[i]
        if atr_v == 0: continue
        dip = 100.0 * s_dp[i] / atr_v
        din = 100.0 * s_dn[i] / atr_v
        denom = dip + din
        dx_s[i] = 0.0 if denom == 0 else 100.0 * abs(dip - din) / denom

    first_dx = next((i for i in range(n) if dx_s[i] is not None), None)
    adx_s = [None]*n
    if first_dx is not None:
        se = first_dx + p
        if se <= n:
            sv = [dx_s[i] for i in range(first_dx, se) if dx_s[i] is not None]
            if len(sv) == p:
                adx_s[se-1] = sum(sv) / p
                for i in range(se, n):
                    if dx_s[i] is not None and adx_s[i-1] is not None:
                        adx_s[i] = (adx_s[i-1] * (p-1) + dx_s[i]) / p

    adx = adx_s[-1]

    # ── ATR (Wilder) — kept for diagnostics/logging, unused by S2 strategy ──
    atr_s = [None] * n
    tr2   = [0.0] * n
    for i in range(1, n):
        tr2[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
    if n > ATR_PERIOD:
        atr_s[ATR_PERIOD] = sum(tr2[1:ATR_PERIOD+1]) / ATR_PERIOD
        for i in range(ATR_PERIOD+1, n):
            atr_s[i] = (atr_s[i-1] * (ATR_PERIOD-1) + tr2[i]) / ATR_PERIOD
    atr = atr_s[-1]

    return {
        # Raw OHLC snapshot (latest closed candle)
        'open':   candles[-1]['o'],
        'high':   candles[-1]['h'],
        'low':    candles[-1]['l'],
        'close':  candles[-1]['c'],
        'time':   candles[-1]['t'],

        # S2 indicators
        'ema50':       ema50,
        'ema100':      ema100,
        'ema50_prev':  ema50_prev,
        'close_prev':  close_prev,
        'adx':         adx,

        # Diagnostics (not consumed by S2 but useful for logs)
        'atr':         atr,
        'atr_pct':     (atr / candles[-1]['c'] * 100) if (atr and candles[-1]['c'] > 0) else None,
    }


# ============================================================================
# CANDLE STORE — one rolling deque per symbol
# ============================================================================

class CandleStore:
    """Thread-safe rolling candle buffer per symbol."""

    def __init__(self, symbols, limit=CANDLE_LIMIT):
        self._lock    = threading.Lock()
        self._candles = {sym: deque(maxlen=limit) for sym in symbols}

    def seed(self, symbol, candle_list):
        """Load historical candles (list of dicts) at startup."""
        with self._lock:
            for c in candle_list:
                self._candles[symbol].append(c)

    def push(self, symbol, candle):
        """Append a newly closed candle."""
        with self._lock:
            self._candles[symbol].append(candle)

    def get_list(self, symbol):
        """Return a snapshot list (safe copy) for indicator computation."""
        with self._lock:
            return list(self._candles[symbol])

    def size(self, symbol):
        with self._lock:
            return len(self._candles[symbol])


# ============================================================================
# REST SEEDER — fetch historical candles at startup
# ============================================================================

def seed_symbol(symbol, store, limit=CANDLE_LIMIT, interval=INTERVAL):
    """Fetch `limit` closed candles from Binance Futures REST and load into store."""
    try:
        resp = requests.get(
            f"{REST_DATA_BASE}/v1/klines",
            params={'symbol': symbol, 'interval': interval, 'limit': limit},
            timeout=15
        )
        if resp.status_code != 200:
            log.warning(f"Seed {symbol} [{interval}]: HTTP {resp.status_code}")
            return False
        data = resp.json()
        if not isinstance(data, list) or len(data) == 0:
            log.warning(f"Seed {symbol} [{interval}]: empty response")
            return False

        candles = []
        for row in data[:-1]:   # exclude the still-open last candle
            candles.append({
                't': int(row[0]),
                'o': float(row[1]),
                'h': float(row[2]),
                'l': float(row[3]),
                'c': float(row[4]),
                'v': float(row[5]),
            })
        store.seed(symbol, candles)
        log.info(f"Seeded {symbol} [{interval}]: {len(candles)} candles")
        return True
    except Exception as e:
        log.error(f"Seed {symbol} [{interval}] error: {e}")
        return False


def seed_all(symbols, store, limit=CANDLE_LIMIT, interval=INTERVAL):
    """Seed all symbols in parallel threads."""
    threads = []
    for sym in symbols:
        t = threading.Thread(target=seed_symbol, args=(sym, store, limit, interval), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.05)   # gentle rate limiting
    for t in threads:
        t.join()
    log.info(f"Seeding complete. Sizes: " +
             ", ".join(f"{s}={store.size(s)}" for s in symbols[:5]) + " ...")


# ============================================================================
# WEBSOCKET ENGINE
# ============================================================================

class CandleEngine:
    """
    Manages a combined WebSocket stream for all symbols.
    Calls on_candle_close(symbol, candle_dict, indicators_dict) on each close.

    Usage:
        def my_callback(symbol, candle, indicators):
            print(symbol, indicators['ema50'])

        engine = CandleEngine(SYMBOLS, callback=my_callback)
        engine.start()   # blocks forever, reconnects on drop
    """

    def __init__(self, symbols, callback=None, symbols_1h=None):
        # symbols       : 15m universe (union of all strategies)
        # symbols_1h    : subset that also needs a 1h feed (AxisPro bias)
        # callback(symbol, candle, candles_15m, candles_1h)
        self.symbols    = list(symbols)
        self.symbols_1h = list(symbols_1h or [])
        self.callback   = callback
        self.store      = CandleStore(self.symbols, limit=CANDLE_LIMIT)
        self.store_1h   = CandleStore(self.symbols_1h, limit=HTF_LIMIT) if self.symbols_1h else None
        self._running = False
        # Watchdog: tracks last time ANY websocket message arrived (not just candle closes).
        # Binance sends partial-candle updates every ~1-2 seconds, so a silence of more than
        # ~60 seconds means the stream is dead even if the socket reports "connected".
        # Railway's network sometimes drops streams without delivering a close event, so we
        # need this to detect it; the library's built-in ping/pong is not enough.
        self._last_msg_ts = 0
        self._ws_app      = None   # set inside _ws_loop so the watchdog can close it

    def get_1h_list(self, symbol):
        return self.store_1h.get_list(symbol) if (self.store_1h and symbol in self.symbols_1h) else None

    def start(self):
        """Seed history (both timeframes) then start WebSocket loop (blocking)."""
        log.info(f"CandleEngine starting -- {len(self.symbols)} symbols (15m), "
                 f"{len(self.symbols_1h)} symbols (1h bias)")
        log.info(f"Testnet: {TESTNET}  |  Interval: {INTERVAL}  |  HTF: {HTF_INTERVAL}")

        log.info("Seeding historical 15m candles...")
        seed_all(self.symbols, self.store, limit=CANDLE_LIMIT, interval=INTERVAL)
        if self.store_1h:
            log.info("Seeding historical 1h candles (AxisPro bias)...")
            seed_all(self.symbols_1h, self.store_1h, limit=HTF_LIMIT, interval=HTF_INTERVAL)

        self._running = True

        # Start the watchdog before the websocket loop. It runs in its own daemon thread
        # and force-closes the socket if no messages arrive for too long, which causes
        # run_forever to return and the outer reconnect loop to fire.
        watchdog = threading.Thread(target=self._watchdog_loop, daemon=True, name='ws_watchdog')
        watchdog.start()

        self._ws_loop()

    def stop(self):
        self._running = False

    def _watchdog_loop(self):
        """
        Detect silent websocket failures (connection 'open' but no data flowing).
        On Railway and similar PaaS networks, streams sometimes die without
        delivering a close frame, so the library never invokes on_close and
        run_forever blocks forever. Checking message-arrival staleness is the
        only reliable way to catch this.
        """
        STALE_AFTER_SEC = 90   # Binance sends partial-candle updates every ~1-2s; 90s is generous
        CHECK_EVERY_SEC = 30
        while self._running:
            time.sleep(CHECK_EVERY_SEC)
            if self._last_msg_ts == 0:
                # Haven't received the first message yet. Don't trip on cold start.
                continue
            silence = time.time() - self._last_msg_ts
            if silence > STALE_AFTER_SEC:
                log.warning(
                    f"[WATCHDOG] No websocket messages for {silence:.0f}s -- "
                    f"stream appears frozen. Forcing reconnect."
                )
                ws = self._ws_app
                if ws is not None:
                    try:
                        ws.close()   # makes run_forever return; outer loop reconnects
                    except Exception as e:
                        log.error(f"[WATCHDOG] Error closing stale socket: {e}")
                # Reset so we don't keep firing close() in a loop while the new socket warms up
                self._last_msg_ts = time.time()

    def _ws_loop(self):
        """Outer loop -- reconnects on any error. Single futures stream for all symbols."""
        import websocket

        while self._running:
            parts = [f"{s.lower()}@kline_{INTERVAL}" for s in self.symbols]
            parts += [f"{s.lower()}@kline_{HTF_INTERVAL}" for s in self.symbols_1h]
            streams = "/".join(parts)
            url     = f"{WS_BASE}?streams={streams}"
            log.info(f"Connecting WebSocket ({len(parts)} streams: "
                     f"{len(self.symbols)}x{INTERVAL} + {len(self.symbols_1h)}x{HTF_INTERVAL})...")

            ws = websocket.WebSocketApp(
                url,
                on_message = self._on_message,
                on_error   = self._on_error,
                on_close   = self._on_close,
                on_open    = self._on_open,
            )
            self._ws_app = ws   # so the watchdog can close it

            ws.run_forever(ping_interval=20, ping_timeout=10)

            self._ws_app = None
            if self._running:
                log.warning(f"WebSocket disconnected. Reconnecting in {RECONNECT_SEC}s...")
                time.sleep(RECONNECT_SEC)

    def _on_open(self, ws):
        log.info("WebSocket connected [OK]")
        self._last_msg_ts = time.time()   # reset watchdog on (re)connect

    def _on_error(self, ws, error):
        log.error(f"WebSocket error: {error}")

    def _on_close(self, ws, code, msg):
        log.info(f"WebSocket closed: {code} {msg}")

    def _on_message(self, ws, raw):
        # Stamp the watchdog FIRST, before any parsing -- even partial-candle ticks count
        # as "stream alive" for staleness detection.
        self._last_msg_ts = time.time()
        try:
            msg  = json.loads(raw)
            data = msg.get('data', {})
            k    = data.get('k', {})

            if not k.get('x', False):
                return   # candle not yet closed - ignore

            symbol   = data.get('s', '').upper()
            interval = k.get('i', '')

            candle = {
                't': int(k['t']),
                'o': float(k['o']),
                'h': float(k['h']),
                'l': float(k['l']),
                'c': float(k['c']),
                'v': float(k.get('v', 0.0)),
            }

            # ── 1h bias candle: just update the HTF store (AxisPro reads it lazily) ──
            if interval == HTF_INTERVAL:
                if self.store_1h and symbol in self.symbols_1h:
                    self.store_1h.push(symbol, candle)
                return

            # ── 15m candle: update store and fire the strategy callback ──────────
            if interval != INTERVAL or symbol not in self.symbols:
                return

            self.store.push(symbol, candle)
            candles_15m = self.store.get_list(symbol)
            candles_1h  = self.get_1h_list(symbol)

            ts = datetime.fromtimestamp(candle['t']/1000, tz=timezone.utc).strftime('%H:%M')
            log.debug(f"{symbol} 15m close @ {ts}  close={candle['c']:.6f}  "
                      f"({len(candles_15m)} bars)")

            if self.callback:
                try:
                    self.callback(symbol, candle, candles_15m, candles_1h)
                except Exception as e:
                    log.error(f"Callback error for {symbol}: {e}", exc_info=True)

        except Exception as e:
            log.error(f"Message parse error: {e}", exc_info=True)


# ============================================================================
# STANDALONE TEST -- run this file directly to verify connectivity
# ============================================================================

def _test_callback(symbol, candle, indicators):
    ts = datetime.fromtimestamp(candle['t']/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(
        f"\n{'='*60}\n"
        f"  {symbol}  |  {ts}\n"
        f"  close={candle['c']:.4f}  open={candle['o']:.4f}\n"
        f"  EMA50={indicators['ema50']:.4f}  EMA100={indicators['ema100']:.4f}\n"
        f"  ADX={indicators['adx']:.2f}\n"
        f"{'='*60}"
    )


if __name__ == '__main__':
    mode = 'Testnet' if TESTNET else 'LIVE'
    order_mode = 'Testnet (paper money)' if TESTNET else 'LIVE (real money!)'
    print(f"""
+------------------------------------------------------+
|  STEP 1 -- Candle Engine  (standalone test mode)     |
|                                                      |
|  Connects to Binance {mode:<31}|
|  Watching {len(SYMBOLS)} symbols on {INTERVAL:<36}|
|                                                      |
|  Market data : live Binance (public, no key needed)  |
|  Orders      : {order_mode:<37}|
|  Press Ctrl+C to stop.                               |
+------------------------------------------------------+
""")

    # Reduce symbol list for testing so seed is fast
    TEST_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
    print(f"Test mode: watching {TEST_SYMBOLS}\n")

    engine = CandleEngine(TEST_SYMBOLS, callback=_test_callback)
    try:
        engine.start()
    except KeyboardInterrupt:
        print("\nStopped.")
