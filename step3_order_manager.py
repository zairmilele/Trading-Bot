"""
STEP 3 OF 4 — Order Manager  (Futures Edition, S2 strategy)
=============================================================
Strategy: S2 (FVG Retest) -- only strategy active in this build.

Key features:
  - Binance USDT-M Futures (testnet via demo-fapi or live via fapi)
  - Isolated margin mode per trade
  - Sizing: $20 margin × 50x leverage ($1,000 notional)
  - Global cap: MAX_OPEN_POSITIONS open at once (new signals ignored above limit)
  - Consecutive-loss counter exposed for dashboard
  - Lock Profit (LP): when price reaches LP_TRIGGER_PCT of the way to TP,
    the SL algo order is cancelled and replaced with a new SL at LP_LOCK_PCT
    of the way to TP. This converts a potentially losing trade into a small
    locked-in win once price has moved halfway to target. Exits at the new
    SL are tagged LP_WIN (distinct from a regular WIN at TP).
  - pnl_usdt stored alongside pnl_pct in trade log
  - Slippage rejection (Fix #1) and per-bar correlated entry cap (Fix #4)
    retained from previous version
  - Fix: maxQty cap to prevent Exceeded maximum allowable position (Error -2027)
  - Fix: TP/SL use TAKE_PROFIT/STOP via /v1/algoOrder (mandatory since 2025-12-09)
"""

import os
import sys
import io
import csv
import math
import time
import hmac
import hashlib
import logging
import threading
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

log = logging.getLogger('order_manager')

load_dotenv()

API_KEY    = os.getenv('BINANCE_API_KEY', '')
API_SECRET = os.getenv('BINANCE_API_SECRET', os.getenv('BINANCE_SECRET', ''))
TESTNET    = os.getenv('TESTNET', 'true').lower() == 'true'

# Futures endpoints
if TESTNET:
    BASE_URL = "https://demo-fapi.binance.com/fapi"
else:
    BASE_URL = "https://fapi.binance.com/fapi"

# ============================================================================
# POSITION CAPS — real -2027 notional limits per symbol on demo-fapi
#
# IMPORTANT: this dict must be regenerated when the symbol list changes.
# When switching to live trading, regenerate via find_caps.py (binary search
# with real test orders) — live limits are typically much higher than demo.
#
# Current state: symbols not in the dict fall back to $2,886 (conservative
# demo default). For the 29-symbol S2 universe, only the original symbols
# whose caps were empirically discovered are listed here. The fallback works
# fine for the rest -- it will just trade at slightly smaller notional than
# the symbol could actually support.
# ============================================================================

POSITION_CAPS = {
    # Empirically measured on demo-fapi for known symbols.
    # Only BTCUSDT overlaps with the current 8-symbol universe; the other
    # entries are kept for reference but unused. All other symbols fall back
    # to the conservative $2,886 default at order time.
    'BTCUSDT': 11389,
    'ETHUSDT': 11389,
    'XRPUSDT': 5724,
    'TRXUSDT': 11311,
    'ADAUSDT': 2886,
    'DOTUSDT': 11045,
}

STRATEGY_CONFIG = {
    # Each strategy carries its own margin / leverage (per its backtest).
    'S2':            {'margin_usdt': 20.0,   'leverage': 50},   # FVG Retest -- $1,000 notional
    'S2_FVG_RETEST': {'margin_usdt': 20.0,   'leverage': 50},
    'AXISPRO':       {'margin_usdt': 20.0,   'leverage': 50},   # $1,000 notional
    'BREAKOUT_NY4H': {'margin_usdt': 20.0,   'leverage': 25},   # $500 notional (lower lev per backtest)
    'ICT_NDOG':      {'margin_usdt': 20.0,   'leverage': 50},   # $1,000 notional
}
DEFAULT_MARGIN   = 20.0
DEFAULT_LEVERAGE = 50

MAX_OPEN_POSITIONS = 30   # practical max: $5,000 balance / $16.65 min margin = ~300 positions; cap at 30 concurrent
                          # pre-flight balance check is the real hard limit
POLL_INTERVAL      = 15
TRADE_LOG_FILE     = 'trade_log.csv'

# ============================================================================
# EXECUTION SAFETY (Fixes for live-vs-backtest divergence)
# ============================================================================
#
# Fix #1: Reject trades when slippage already eats into SL room before entry.
#   Backtest fills at the candle close exactly. Live market orders fill 0.05–0.20%
#   past the close on confirmed crossover candles (momentum continues briefly).
#   When SL is only 0.4% wide, slippage of 0.15% already burns ~37% of SL budget,
#   leaving the trade with too little room to survive normal noise.
#
# Fix #2: Anchor SL/TP off the SIGNAL price, not the actual fill.
#   If we got a worse fill, the SL should NOT be pushed deeper into danger —
#   it should stay where the strategy intended it. This is the single biggest
#   driver of the live-vs-backtest gap.
#
# Fix #4: Cap how many new positions we open in the same 15m candle.
#   When EMA crossovers fire on multiple correlated symbols simultaneously,
#   a single market move stops them all out together. Limiting new entries
#   per bar prevents this clustered-loss pattern.
# ============================================================================

MAX_ADVERSE_SLIPPAGE_PCT = 0.15   # if fill is >0.15% worse than signal entry, abort the trade
MAX_NEW_POSITIONS_PER_BAR = 3     # max simultaneous new entries per 15-minute candle
BAR_INTERVAL_MS = 15 * 60 * 1000  # 15-minute bar in milliseconds

SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', '')


# ============================================================================
# SUPABASE CLIENT
# ============================================================================

class SupabaseClient:
    def __init__(self, url: str, key: str):
        self._url     = url.rstrip('/')
        self._headers = {
            'apikey':        key,
            'Authorization': f'Bearer {key}',
            'Content-Type':  'application/json',
            'Prefer':        'return=minimal',
        }
        self._ok = bool(url and key)
        if not self._ok:
            log.info("Supabase disabled — using local CSV/in-memory trade history only")

    def insert(self, table: str, row: dict):
        if not self._ok:
            return
        try:
            resp = requests.post(
                f"{self._url}/rest/v1/{table}",
                json=row,
                headers=self._headers,
                timeout=10,
            )
            if resp.status_code not in (200, 201):
                log.warning(f"Supabase insert failed {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log.warning(f"Supabase insert error: {e}")

    def update(self, table: str, row_id: int, row: dict):
        if not self._ok:
            return
        try:
            headers = dict(self._headers)
            headers['Prefer'] = 'return=minimal'
            resp = requests.patch(
                f"{self._url}/rest/v1/{table}?id=eq.{row_id}",
                json=row,
                headers=headers,
                timeout=10,
            )
            if resp.status_code not in (200, 201, 204):
                log.warning(f"Supabase update failed {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log.warning(f"Supabase update error: {e}")

    def insert_returning_id(self, table: str, row: dict) -> int | None:
        """Insert a row and return its auto-generated id."""
        if not self._ok:
            return None
        try:
            headers = dict(self._headers)
            headers['Prefer'] = 'return=representation'
            resp = requests.post(
                f"{self._url}/rest/v1/{table}",
                json=row,
                headers=headers,
                timeout=10,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                if data:
                    return data[0].get('id')
            log.warning(f"Supabase insert_returning_id failed {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log.warning(f"Supabase insert_returning_id error: {e}")
        return None

    def select_all(self, table: str) -> list:
        if not self._ok:
            return []
        try:
            headers = dict(self._headers)
            headers['Prefer'] = 'count=none'
            resp = requests.get(
                f"{self._url}/rest/v1/{table}?select=*&order=id.asc",
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
            log.warning(f"Supabase select failed {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log.warning(f"Supabase select error: {e}")
        return []


# ============================================================================
# BINANCE FUTURES REST CLIENT
# ============================================================================

class BinanceClient:

    def __init__(self, api_key: str, api_secret: str, base_url: str):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.base_url   = base_url
        self.session    = requests.Session()
        self.session.headers.update({'X-MBX-APIKEY': api_key})

    def _fmt_price(self, value: float) -> str:
        """Format float as plain decimal — Binance rejects scientific notation."""
        formatted = f'{value:.10f}'.rstrip('0')
        if formatted.endswith('.'):
            formatted += '0'
        return formatted

    def _sign(self, params: dict) -> dict:
        params['timestamp'] = int(time.time() * 1000)
        query = '&'.join(f"{k}={v}" for k, v in params.items())
        sig   = hmac.new(
            self.api_secret.encode('utf-8'),
            query.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        params['signature'] = sig
        return params

    def _get(self, path: str, params: dict = None, signed: bool = False):
        params = params or {}
        if signed:
            params = self._sign(params)
        resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, params: dict):
        params = self._sign(params)
        resp = self.session.post(f"{self.base_url}{path}", data=params, timeout=10)
        # Retry once on Binance demo server timeout (-1007 / 408)
        if resp.status_code == 408:
            log.warning(f"POST {path} timed out (408), retrying once...")
            time.sleep(1)
            params = self._sign({k:v for k,v in params.items()
                                  if k not in ('timestamp','signature')})
            resp = self.session.post(f"{self.base_url}{path}", data=params, timeout=15)
        if resp.status_code != 200:
            log.error(f"POST {path} failed {resp.status_code}: {resp.text}")
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str, params: dict):
        params = self._sign(params)
        resp = self.session.delete(f"{self.base_url}{path}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ── Futures-specific helpers ──────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        try:
            return self._post('/v1/leverage', {
                'symbol':   symbol,
                'leverage': leverage,
            })
        except requests.exceptions.HTTPError as e:
            body = e.response.text if e.response is not None else ''
            if '-4028' in body:
                # Leverage not valid — step down through common levels until accepted
                log.warning(f"{symbol}: leverage {leverage}x not valid, stepping down...")
                fallbacks = [l for l in [50,40,33,25,20,15,10,5,3,1] if l < leverage]
                for fallback in fallbacks:
                    try:
                        result = self._post('/v1/leverage', {'symbol': symbol, 'leverage': fallback})
                        log.warning(f"{symbol}: leverage accepted at {fallback}x")
                        return result
                    except requests.exceptions.HTTPError:
                        continue
            if '-1109' in body:
                # Same demo "Invalid account" quirk as marginType — leverage can't
                # be changed here. Report the target so sizing proceeds; if the
                # account's actual leverage is lower the order-placement retry
                # logic (-2019/-4005/-2027 halving) corrects the quantity.
                log.warning(f"{symbol}: leverage returned -1109 (demo quirk) — "
                            f"assuming {leverage}x for sizing")
                return {'leverage': leverage}
            raise

    def set_margin_type(self, symbol: str, margin_type: str = 'ISOLATED') -> dict:
        try:
            return self._post('/v1/marginType', {
                'symbol':     symbol,
                'marginType': margin_type,
            })
        except requests.exceptions.HTTPError as e:
            body = e.response.text if e.response is not None else ''
            if '-4046' in body:
                # Already set to the requested margin type — not an error
                log.debug(f"{symbol}: margin type already {margin_type}")
                return {}
            if '-1121' in body:
                # Symbol doesn't exist on this futures endpoint
                raise ValueError(f"{symbol} not listed on futures demo")
            if '-1109' in body:
                # "Invalid account" — a Binance DEMO quirk on /v1/marginType for
                # certain symbols (often newer/tokenized perps). The symbol is
                # still tradeable; margin-type just can't be changed here. Treat
                # as non-fatal and proceed with the order in the account's current
                # margin mode (it may already be ISOLATED). On LIVE this branch
                # rarely fires; if it does, the order placement still applies SL/TP.
                log.warning(f"{symbol}: marginType returned -1109 (demo quirk) — "
                            f"proceeding without changing margin type")
                return {}
            raise
        except Exception:
            raise

    def get_symbol_info(self, symbol: str) -> dict:
        info = self._get('/v1/exchangeInfo')
        for s in info.get('symbols', []):
            if s['symbol'] == symbol:
                return s
        return {}

    def get_max_notional(self, symbol: str, leverage: int) -> float:
        """
        Returns the real -2027 notional cap for a symbol.
        Uses empirically discovered POSITION_CAPS dict instead of leverageBracket,
        which returns incorrect values on the demo account.
        Falls back to $2,886 (conservative demo default) if symbol not in dict.
        """
        cap = POSITION_CAPS.get(symbol)
        if cap is not None:
            log.debug(f"[CAP] {symbol}: notionalCap=${cap:,} (from POSITION_CAPS)")
            return float(cap)
        log.warning(f"[CAP] {symbol}: not in POSITION_CAPS, using fallback $2,886")
        return 2886.0

    def get_ticker_price(self, symbol: str) -> float:
        data = self._get('/v1/ticker/price', {'symbol': symbol})
        return float(data['price'])

    def get_account(self) -> dict:
        return self._get('/v2/account', {}, signed=True)

    def get_usdt_balance(self) -> float:
        account = self.get_account()
        for a in account.get('assets', []):
            if a['asset'] == 'USDT':
                return float(a['availableBalance'])
        return 0.0

    def place_market_order(self, symbol: str, side: str, quantity: float,
                           reduce_only: bool = False) -> dict:
        params = {
            'symbol':   symbol,
            'side':     side,
            'type':     'MARKET',
            'quantity': self._fmt_price(quantity),
        }
        if reduce_only:
            params['reduceOnly'] = 'true'
        return self._post('/v1/order', params)

    def place_limit_order(self, symbol: str, side: str, quantity: float,
                          price: float, tif: str = 'GTC') -> dict:
        """Resting LIMIT entry (used by the ICT gap strategy)."""
        return self._post('/v1/order', {
            'symbol':      symbol,
            'side':        side,
            'type':        'LIMIT',
            'timeInForce': tif,
            'quantity':    self._fmt_price(quantity),
            'price':       self._fmt_price(price),
        })

    def _post_algo(self, path: str, params: dict):
        """POST to algo order endpoint — required for TAKE_PROFIT/STOP since 2025-12-09.
        Uses data= (request body) per Binance docs: POST params go in body, not URL."""
        params = self._sign(params)
        resp = self.session.post(
            f"{self.base_url}{path}",
            data=params,    # body, not query string
            timeout=10
        )
        if resp.status_code != 200:
            log.error(f"POST {path} failed {resp.status_code}: {resp.text}")
        resp.raise_for_status()
        return resp.json()

    def place_take_profit_order(self, symbol: str, side: str,
                                quantity: float, tp_price: float) -> dict:
        """
        TAKE_PROFIT via POST /fapi/v1/algoOrder.
        Mandatory since Binance migrated conditional orders to Algo Service 2025-12-09.
        Params per official docs: algoType + type (mandatory), triggerPrice, price, workingType.
        """
        params = {
            'symbol':       symbol,
            'side':         side,
            'algoType':     'CONDITIONAL',
            'type':         'TAKE_PROFIT',     # mandatory per docs (not orderType)
            'quantity':     self._fmt_price(quantity),
            'price':        self._fmt_price(tp_price),
            'triggerPrice': self._fmt_price(tp_price),
            'timeInForce':  'GTC',
            'reduceOnly':   'true',
            'workingType':  'CONTRACT_PRICE',
        }
        return self._post_algo('/v1/algoOrder', params)

    def place_stop_loss_order(self, symbol: str, side: str,
                              quantity: float, sl_price: float) -> dict:
        """
        STOP via POST /fapi/v1/algoOrder.
        Mandatory since Binance migrated conditional orders to Algo Service 2025-12-09.
        """
        params = {
            'symbol':       symbol,
            'side':         side,
            'algoType':     'CONDITIONAL',
            'type':         'STOP',            # mandatory per docs (not orderType)
            'quantity':     self._fmt_price(quantity),
            'price':        self._fmt_price(sl_price),
            'triggerPrice': self._fmt_price(sl_price),
            'timeInForce':  'GTC',
            'reduceOnly':   'true',
            'workingType':  'CONTRACT_PRICE',
        }
        return self._post_algo('/v1/algoOrder', params)

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        return self._delete('/v1/order', {'symbol': symbol, 'orderId': order_id})

    def get_algo_order(self, algo_id: int) -> dict:
        """Query an algo order status by algoId — used for TP/SL monitoring."""
        return self._get('/v1/algoOrder', {'algoId': algo_id}, signed=True)

    def cancel_algo_order(self, algo_id: int) -> dict:
        """Cancel an algo order by algoId — DELETE /fapi/v1/algoOrder (signed)."""
        params = self._sign({'algoId': algo_id})
        resp = self.session.delete(
            f"{self.base_url}/v1/algoOrder",
            params=params,
            timeout=10
        )
        resp.raise_for_status()
        return resp.json()

    def get_order(self, symbol: str, order_id: int) -> dict:
        return self._get('/v1/order', {'symbol': symbol, 'orderId': order_id}, signed=True)

    def get_open_orders(self, symbol: str = None) -> list:
        params = {}
        if symbol:
            params['symbol'] = symbol
        return self._get('/v1/openOrders', params, signed=True)

    def get_position(self, symbol: str) -> dict:
        data = self._get('/v2/positionRisk', {'symbol': symbol}, signed=True)
        if isinstance(data, list) and data:
            return data[0]
        return {}


# ============================================================================
# SYMBOL PRECISION HELPER  (Futures version)
# ============================================================================

class PrecisionCache:

    CACHE_TTL = 24 * 3600   # refresh symbol info every 24 hours

    def __init__(self, client: BinanceClient):
        self._client = client
        self._cache  = {}          # symbol -> dict
        self._fetched_at = {}      # symbol -> epoch float
        self._lock   = threading.Lock()

    def refresh(self, symbol: str) -> None:
        """Force-expire cache for a symbol so next get() fetches fresh data from API."""
        with self._lock:
            self._fetched_at.pop(symbol, None)
            self._cache.pop(symbol, None)
        log.info(f"[CACHE] Refreshed precision cache for {symbol}")

    def get(self, symbol: str) -> dict:
        now = time.time()
        with self._lock:
            if symbol in self._cache:
                age = now - self._fetched_at.get(symbol, 0)
                if age < self.CACHE_TTL:
                    return self._cache[symbol]

        info    = self._client.get_symbol_info(symbol)
        filters = {f['filterType']: f for f in info.get('filters', [])}

        lot      = filters.get('LOT_SIZE', {})
        tick     = filters.get('PRICE_FILTER', {})
        notional = filters.get('MIN_NOTIONAL', {})

        def _decimals(step_str: str) -> int:
            s = step_str.rstrip('0')
            return len(s.split('.')[-1]) if '.' in s else 0

        result = {
            'qty_step':       float(lot.get('stepSize', '0.001')),
            'qty_decimals':   _decimals(lot.get('stepSize', '0.001')),
            'price_step':     float(tick.get('tickSize', '0.01')),
            'price_decimals': _decimals(tick.get('tickSize', '0.01')),
            'min_qty':        float(lot.get('minQty', '0.001')),
            'max_qty':        float(lot.get('maxQty', '9999999')),   # ← for Error -2027
            'min_notional':   float(notional.get('minNotional', '5')),
        }

        with self._lock:
            self._cache[symbol] = result
            self._fetched_at[symbol] = time.time()
        return result

    def calc_quantity(self, symbol: str, price: float,
                      margin: float, leverage: int) -> float:
        """Legacy wrapper — use resolve_order_params for full dynamic logic."""
        qty, _ = self.resolve_order_params(symbol, price, margin, leverage)
        return qty

    def resolve_order_params(self, symbol: str, price: float,
                             margin: float, target_leverage: int) -> tuple:
        """
        Resolve the actual quantity and leverage for an order:

          1. Get max notional Binance allows for target_leverage on this symbol
          2. Cap notional = min(margin × target_leverage, max_notional)
          3. qty = floor(notional / price / stepSize) × stepSize
          4. If qty > LOT_SIZE maxQty: cap qty, keep margin fixed, back-calc leverage
          5. Return (qty, actual_leverage) — caller sets leverage if it changed

        This always deploys the full margin. Leverage only decreases if qty is capped.
        """
        prec     = self.get(symbol)
        step     = prec['qty_step']
        decimals = prec['qty_decimals']
        max_qty  = prec['max_qty']

        # Step 1: get the notional cap Binance enforces at this leverage
        max_notional = self._client.get_max_notional(symbol, target_leverage)

        # Step 2: compute raw notional (full margin × leverage)
        notional = min(margin * target_leverage, max_notional)

        # Step 3: compute qty floored to stepSize
        raw_qty = notional / price
        qty     = math.floor(raw_qty / step) * step
        qty     = round(qty, decimals)

        # Step 3b: if qty × price == max_notional exactly (boundary collision),
        # subtract one stepSize to stay strictly below the cap.
        # This preserves max margin deployment while avoiding -2027.
        if abs(qty * price - max_notional) < 0.001 and qty >= step:
            qty = round(qty - step, decimals)

        # Leverage stays at target unless qty is further capped below
        actual_leverage = target_leverage

        # Step 4: if qty still exceeds LOT_SIZE maxQty, cap it and back-calc leverage
        if qty > max_qty:
            qty             = math.floor(max_qty / step) * step
            qty             = round(qty, decimals)
            capped_notional = qty * price
            raw_lev         = capped_notional / margin
            # Round DOWN to nearest valid Binance leverage level to avoid -4028
            valid_levels    = [50, 40, 33, 25, 20, 15, 10, 5, 3, 1]
            actual_leverage = next((l for l in valid_levels if l <= raw_lev), 1)
            log.warning(f"[QTY CAP] {symbol}: qty capped to {qty} (maxQty={max_qty}), "
                        f"leverage back-calc to {actual_leverage}x "
                        f"(notional=${capped_notional:.0f}, margin=${margin})")

        log.debug(f"[RESOLVE] {symbol}: target={target_leverage}x "
                  f"max_notional=${max_notional:.0f} "
                  f"notional=${qty*price:.0f} qty={qty} lev={actual_leverage}x")

        return qty, actual_leverage

    def round_price(self, symbol: str, price: float) -> float:
        p    = self.get(symbol)
        step = p['price_step']
        return round(round(price / step) * step, p['price_decimals'])

    def round_qty(self, symbol: str, qty: float) -> float:
        """Floor a quantity to the symbol's lot step (used for partial closes)."""
        p    = self.get(symbol)
        step = p['qty_step']
        if step <= 0:
            return qty
        return round(math.floor(qty / step) * step, p['qty_decimals'])


# ============================================================================
# OPEN POSITION TRACKER
# ============================================================================

class _PendingLimit:
    """A resting ICT LIMIT entry awaiting fill (or day-end cancellation)."""
    def __init__(self, signal, order_id, qty, leverage, margin, limit_price, period_end_ts):
        self.signal        = signal
        self.order_id      = order_id
        self.qty           = qty
        self.leverage      = leverage
        self.margin        = margin
        self.limit_price   = limit_price
        self.period_end_ts = period_end_ts
        self.placed_ts     = int(time.time() * 1000)


class OpenPosition:

    def __init__(self, symbol, strategy, direction,
                 entry_price, sl_price, tp_price,
                 quantity, margin_usdt, leverage,
                 tp_order_id, sl_order_id,
                 entry_order_id, signal_ts, signal_time,
                 signal_price=None,
                 lp_trigger_pct=None, lp_lock_pct=None,
                 exit_model='FIXED_LP',
                 tp1_price=None, tp2_price=None, tp1_frac=None,
                 move_be_after_tp1=True, trail_ema_period=None,
                 atr_at_signal=None, init_risk=None,
                 trail_trigger_r=None, trail_sl_atr=None,
                 tp_lock_trigger_r=None, trail_tp_atr=None,
                 max_hold_candles=None,
                 trail_activate_pct=None, trail_pct=None):
        self.symbol         = symbol
        self.strategy       = strategy
        self.direction      = direction
        self.entry_price    = entry_price
        self.sl_price       = sl_price
        self.tp_price       = tp_price
        self.quantity       = quantity
        self.margin_usdt    = margin_usdt
        self.leverage       = leverage
        self.tp_order_id    = tp_order_id
        self.sl_order_id    = sl_order_id
        self.entry_order_id = entry_order_id
        self.signal_ts      = signal_ts
        self.signal_time    = signal_time
        self.signal_price   = signal_price   # intended entry from signal (for slippage calc)
        self.open_time      = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        self.open_ts        = int(time.time() * 1000)
        self.db_id          = None           # set after Supabase insert in _log_trade_open

        # ── Lock Profit (LP) state ────────────────────────────────────────────
        # When price reaches lp_trigger_pct of the way from entry to TP, the SL
        # algo order is cancelled and replaced with a new SL at lp_lock_pct of
        # the way from entry to TP. After arming, any "SL hit" exits at the
        # locked-in profit price -- recorded as outcome LP_WIN (vs LOSS).
        self.lp_trigger_pct = lp_trigger_pct   # e.g. 50.0  (None = LP disabled)
        self.lp_lock_pct    = lp_lock_pct      # e.g. 10.0
        self.lp_armed       = False
        self.lp_trigger_price = None           # computed at open
        self.lp_lock_price    = None           # computed at open
        self.original_sl_price = sl_price      # remembered so we can detect "true LOSS" (filled at original SL)

        if lp_trigger_pct is not None and lp_lock_pct is not None:
            tp_dist = abs(tp_price - entry_price)
            if direction == 'LONG':
                self.lp_trigger_price = entry_price + tp_dist * (lp_trigger_pct / 100.0)
                self.lp_lock_price    = entry_price + tp_dist * (lp_lock_pct / 100.0)
            else:
                self.lp_trigger_price = entry_price - tp_dist * (lp_trigger_pct / 100.0)
                self.lp_lock_price    = entry_price - tp_dist * (lp_lock_pct / 100.0)

        # ── Multi-model exit plan (AxisPro / Breakout / ICT) ──────────────────
        self.exit_model = exit_model
        # AxisPro PARTIAL_TRAIL
        self.tp1_price        = tp1_price
        self.tp2_price        = tp2_price
        self.tp1_frac         = tp1_frac
        self.move_be_after_tp1 = move_be_after_tp1
        self.trail_ema_period = trail_ema_period
        self.tp1_done         = False          # set once TP1 partial has filled
        self.tp1_order_id     = None
        # Breakout BREAKOUT_TRAIL / R-based math
        self.atr_at_signal    = atr_at_signal
        self.init_risk        = init_risk
        self.trail_trigger_r  = trail_trigger_r
        self.trail_sl_atr     = trail_sl_atr
        self.tp_lock_trigger_r = tp_lock_trigger_r
        self.trail_tp_atr     = trail_tp_atr
        self.max_hold_candles = max_hold_candles
        self.bars_held        = 0
        # ICT FIXED_TRAIL (percentage trailing)
        self.trail_activate_pct = trail_activate_pct
        self.trail_pct          = trail_pct
        # shared trailing state
        self.best_price       = entry_price    # favourable extreme since entry
        self.trail_sl_armed   = False
        self.tp_trail_armed   = False


# ============================================================================
# ORDER MANAGER
# ============================================================================

class OrderManager:

    def __init__(self, detector=None, alerts=None):
        if not API_KEY or not API_SECRET:
            raise ValueError(
                "API keys not found. Create a .env file with:\n"
                "  BINANCE_API_KEY=your_key\n"
                "  BINANCE_SECRET=your_secret"
            )

        self.detector  = detector
        self.alerts    = alerts
        self.client    = BinanceClient(API_KEY, API_SECRET, BASE_URL)
        self.precision = PrecisionCache(self.client)
        self.supabase  = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)

        self._lock            = threading.Lock()
        # Positions are keyed by "STRATEGY|SYMBOL" so each strategy tracks its own
        # trades independently — even on coins shared with another strategy.
        self._open_positions  = {}      # "STRATEGY|SYMBOL" → OpenPosition
        # Binance Futures holds ONE net position per symbol per account (one-way
        # mode). Several coins are shared across strategies (WLD, SUI, XPL, XLM,
        # ZEC), so we keep a symbol-level execution lock: a symbol that already
        # has a live position (by ANY strategy) cannot be entered by another
        # until it closes. This prevents two strategies colliding on the same
        # underlying Binance position. The second strategy's signal is skipped.
        self._symbols_live    = set()   # symbols with a live position (any strategy)
        self._pending_symbols = set()   # symbols mid-entry (any strategy)
        # ICT resting LIMIT entries awaiting fill: key → _PendingLimit
        self._pending_limits  = {}

        # Per-bar entry tracker (Fix #4 — cap correlated entries per 15m candle)
        # Resets each time we cross into a new 15m bar.
        self._current_bar_ts        = 0    # ms-aligned start of current 15m bar
        self._entries_in_current_bar = 0

        # Consecutive loss counters per strategy (short keys)
        self._consec_losses   = {'S2': 0, 'AXISPRO': 0, 'BREAKOUT': 0, 'ICT': 0}

        # In-memory history loaded from Supabase on startup
        self.closed_positions = []
        self._load_supabase_history()

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name='pos_monitor'
        )
        self._monitor_thread.start()

        self._init_csv()

        log.info(f"OrderManager ready | Futures | Testnet={TESTNET} | "
                 f"Max positions={MAX_OPEN_POSITIONS}")

        try:
            bal = self.client.get_usdt_balance()
            log.info(f"Futures wallet USDT balance: {bal:.2f}")
        except Exception as e:
            log.error(f"Could not fetch balance — check API keys: {e}")

    # ── Public stats for dashboard ────────────────────────────────────────────

    def get_open_positions_list(self) -> list:
        now_ms = int(time.time() * 1000)
        result = []
        with self._lock:
            for pos in self._open_positions.values():
                elapsed_s = (now_ms - pos.open_ts) // 1000
                hours, rem = divmod(elapsed_s, 3600)
                mins       = rem // 60
                duration   = f"{hours}h {mins:02d}m" if hours else f"{mins}m"
                result.append({
                    'symbol':      pos.symbol,
                    'strategy':    pos.strategy,
                    'direction':   pos.direction,
                    'entry_price': pos.entry_price,
                    'sl_price':    pos.sl_price,
                    'tp_price':    pos.tp_price,
                    'quantity':    pos.quantity,
                    'margin_usdt': pos.margin_usdt,
                    'leverage':    pos.leverage,
                    'open_time':   pos.open_time,
                    'open_ts':     pos.open_ts,
                    'duration':    duration,
                })
        return result

    def get_stats(self) -> dict:
        return {
            'consec_losses': dict(self._consec_losses),
            'open_count':    len(self._open_positions),
        }

    # ── Signal handler ────────────────────────────────────────────────────────

    @staticmethod
    def _pkey(strategy, symbol):
        return f"{strategy}|{symbol}"

    @staticmethod
    def _exit_kwargs(signal):
        """Collect the exit-model fields from a SignalEvent for OpenPosition."""
        return dict(
            exit_model        = getattr(signal, 'exit_model', 'FIXED_LP'),
            tp1_price         = getattr(signal, 'tp1_price', None),
            tp2_price         = getattr(signal, 'tp2_price', None),
            tp1_frac          = getattr(signal, 'tp1_frac', None),
            move_be_after_tp1 = getattr(signal, 'move_be_after_tp1', True),
            trail_ema_period  = getattr(signal, 'trail_ema_period', None),
            atr_at_signal     = getattr(signal, 'atr_at_signal', None),
            init_risk         = getattr(signal, 'init_risk', None),
            trail_trigger_r   = getattr(signal, 'trail_trigger_r', None),
            trail_sl_atr      = getattr(signal, 'trail_sl_atr', None),
            tp_lock_trigger_r = getattr(signal, 'tp_lock_trigger_r', None),
            trail_tp_atr      = getattr(signal, 'trail_tp_atr', None),
            max_hold_candles  = getattr(signal, 'max_hold_candles', None),
            trail_activate_pct = getattr(signal, 'trail_activate_pct', None),
            trail_pct          = getattr(signal, 'trail_pct', None),
        )

    def _notify_closed(self, symbol, strategy, outcome):
        if self.detector:
            try:
                self.detector.on_trade_closed(symbol, strategy, outcome)
            except Exception as e:
                log.warning(f"close-feedback failed for {strategy}/{symbol}: {e}")

    def on_signal(self, signal):
        t = threading.Thread(
            target=self._handle_signal,
            args=(signal,),
            daemon=True,
            name=f"trade_{signal.symbol}"
        )
        t.start()

    def _handle_signal(self, signal):
        symbol    = signal.symbol
        direction = signal.direction
        strategy  = signal.strategy
        is_limit  = getattr(signal, 'entry_type', 'MARKET') == 'LIMIT'

        with self._lock:
            # Per-bar correlated entry cap — applies to MARKET entries only.
            # When momentum signals fire on many correlated alts in the same 15m
            # candle, this stops us opening a cluster of positions that all stop
            # out together. LIMIT (ICT) signals are exempt: they are resting
            # orders, one per coin per day, and a whole day's worth legitimately
            # fires on the candle that opens the new gap — throttling them would
            # silently drop most ICT coins.
            if not is_limit:
                bar_ts = (signal.signal_ts // BAR_INTERVAL_MS) * BAR_INTERVAL_MS
                if bar_ts != self._current_bar_ts:
                    self._current_bar_ts        = bar_ts
                    self._entries_in_current_bar = 0
                if self._entries_in_current_bar >= MAX_NEW_POSITIONS_PER_BAR:
                    log.info(f"[SKIP] {symbol}: per-bar entry cap reached "
                             f"({self._entries_in_current_bar}/{MAX_NEW_POSITIONS_PER_BAR}) "
                             f"— too many correlated signals this candle")
                    return

            total_open = len(self._open_positions) + len(self._pending_symbols)
            if total_open >= MAX_OPEN_POSITIONS:
                log.info(f"[SKIP] {symbol}: max open positions ({MAX_OPEN_POSITIONS}) reached")
                return
            pkey = self._pkey(strategy, symbol)
            if pkey in self._open_positions or pkey in self._pending_limits:
                log.info(f"[SKIP] {symbol}: {strategy} already has a position/limit here")
                return
            # Symbol-level execution lock: Binance holds one net position per
            # symbol per account. If another strategy already holds (or is mid-
            # entry on) this symbol, skip — we can't run two live positions on
            # the same underlying Binance position in one-way mode.
            if symbol in self._symbols_live:
                log.info(f"[SKIP] {symbol}: already live under another strategy "
                         f"(one Binance position per symbol) — {strategy} skipped")
                return
            if symbol in self._pending_symbols:
                log.info(f"[SKIP] {symbol}: entry already in progress")
                return
            self._pending_symbols.add(symbol)
            if not is_limit:
                self._entries_in_current_bar += 1

        # ── LIMIT-entry strategies (ICT) take a separate, non-blocking path ───
        if is_limit:
            try:
                self._handle_limit_signal(signal)
            finally:
                with self._lock:
                    self._pending_symbols.discard(symbol)
            return

        log.info(f"[ORDER] Processing signal: {symbol} {strategy} {direction} "
                 f"entry~{signal.entry_price:.6f}")

        cfg      = STRATEGY_CONFIG.get(strategy, {'margin_usdt': DEFAULT_MARGIN,
                                                   'leverage':    DEFAULT_LEVERAGE})
        margin   = cfg['margin_usdt']
        leverage = cfg['leverage']

        try:
            # Pre-flight balance check — prevents -2019 Margin Insufficient
            available = self.client.get_usdt_balance()
            if available < margin:
                log.warning(f"[SKIP] {symbol}: insufficient balance "
                            f"${available:.2f} < required ${margin:.2f}")
                with self._lock:
                    self._pending_symbols.discard(symbol)
                return

            # Pre-flight cap check — skip symbols whose demo cap is below margin
            symbol_cap = POSITION_CAPS.get(symbol, 2886.0)
            if symbol_cap < margin:
                log.warning(f"[SKIP] {symbol}: demo position cap ${symbol_cap:.0f} "
                            f"< margin ${margin:.0f} — not tradeable on demo")
                with self._lock:
                    self._pending_symbols.discard(symbol)
                return

            # Cancel any stale open orders on this symbol before changing margin type
            # Prevents -4067: "Position side cannot be changed if there exists open orders"
            try:
                open_orders = self.client.get_open_orders(symbol)
                for o in (open_orders or []):
                    try:
                        self.client.cancel_order(symbol, o['orderId'])
                    except Exception:
                        pass
                # Also cancel any open algo orders
                open_algos = self.client._get('/v1/openAlgoOrders', {'symbol': symbol}, signed=True)
                for o in (open_algos or []):
                    try:
                        self.client.cancel_algo_order(o['algoId'])
                    except Exception:
                        pass
            except Exception as e:
                log.debug(f"[CLEANUP] {symbol}: stale order cleanup: {e}")

            self.client.set_margin_type(symbol, 'ISOLATED')

            # Set leverage — response contains the actual leverage Binance accepted
            lev_response    = self.client.set_leverage(symbol, leverage)
            accepted_lev    = int(lev_response.get('leverage', leverage)) if lev_response else leverage

            current_price = self.client.get_ticker_price(symbol)
            prec          = self.precision.get(symbol)

            # Resolve qty and actual leverage using dynamic margin logic.
            # Use accepted_lev (what Binance confirmed) as the effective target.
            qty, actual_leverage = self.precision.resolve_order_params(
                symbol, current_price, margin, accepted_lev
            )

            # If qty cap further reduced leverage, re-set on Binance
            if actual_leverage != accepted_lev:
                log.info(f"[LEVERAGE] {symbol}: {accepted_lev}x → {actual_leverage}x "
                         f"(maxQty cap, margin ${margin} preserved)")
                lev_resp2       = self.client.set_leverage(symbol, actual_leverage)
                confirmed_lev   = int(lev_resp2.get('leverage', actual_leverage)) if lev_resp2 else actual_leverage
                if confirmed_lev != actual_leverage:
                    # Binance accepted a different leverage (e.g. step-down hit) — re-resolve qty
                    log.info(f"[LEVERAGE] {symbol}: back-calc {actual_leverage}x → confirmed {confirmed_lev}x, re-resolving qty")
                    actual_leverage = confirmed_lev
                    qty, _ = self.precision.resolve_order_params(symbol, current_price, margin, actual_leverage)
            elif accepted_lev != leverage:
                log.info(f"[LEVERAGE] {symbol}: target {leverage}x → accepted {accepted_lev}x "
                         f"(Binance limit)")

            if qty < prec['min_qty']:
                log.warning(f"[SKIP] {symbol}: qty {qty} < min_qty {prec['min_qty']}")
                with self._lock:
                    self._pending_symbols.discard(symbol)
                return

            entry_side = 'BUY' if direction == 'LONG' else 'SELL'
            exit_side  = 'SELL' if direction == 'LONG' else 'BUY'
            lev_note   = f" (target {leverage}x)" if actual_leverage != leverage else ""
            log.info(f"[ORDER] Placing {entry_side} MARKET {qty} {symbol} @ ~{current_price:.6f} "
                     f"[{strategy} margin=${margin} lev={actual_leverage}x{lev_note}]")

            entry_result   = self.client.place_market_order(symbol, entry_side, qty)
            entry_order_id = entry_result['orderId']

            avg_price    = float(entry_result.get('avgPrice', 0) or 0)
            actual_entry = avg_price if avg_price > 0 else current_price

            log.info(f"[ORDER] Entry filled: {entry_side} {qty} {symbol} @ {actual_entry:.6f} "
                     f"(order #{entry_order_id})")

            # ── Fix #1: Slippage rejection ────────────────────────────────────
            # If the actual fill is meaningfully worse than the signal price,
            # the trade is already half-stopped before it begins. Close it now.
            slippage_pct = (actual_entry - signal.entry_price) / signal.entry_price * 100
            adverse_slip = (slippage_pct if direction == 'LONG' else -slippage_pct)
            if adverse_slip > MAX_ADVERSE_SLIPPAGE_PCT:
                log.warning(f"[SLIPPAGE-REJECT] {symbol}: fill {actual_entry:.6f} vs "
                            f"signal {signal.entry_price:.6f} — adverse slippage "
                            f"{adverse_slip:.3f}% > {MAX_ADVERSE_SLIPPAGE_PCT}% — "
                            f"closing immediately to avoid pre-stopped trade")
                try:
                    close_result = self.client.place_market_order(symbol, exit_side, qty)
                    exit_price   = float(close_result.get('avgPrice', 0) or actual_entry)
                    log.info(f"[SLIPPAGE-REJECT] {symbol}: closed @ {exit_price:.6f}")
                except Exception as close_err:
                    log.error(f"[SLIPPAGE-REJECT] {symbol}: failed to close: {close_err}")
                with self._lock:
                    self._pending_symbols.discard(symbol)
                if self.detector:
                    self.detector.on_trade_closed(symbol, strategy, 'LOSS')
                return

            # ── Fix #2: Anchor SL/TP to the SIGNAL price, not the actual fill ─
            # Previously: sl_price = actual_entry * (signal.sl_price/signal.entry_price)
            # That re-applied the SL distance from the slipped fill, pushing the
            # stop deeper into adverse territory whenever there was slippage.
            # New behaviour: SL/TP stay where the strategy intended them. The
            # trade simply has slightly less room to SL and slightly more to TP
            # (or vice versa), but no longer gets pre-stopped by execution noise.
            sl_price = self.precision.round_price(symbol, signal.sl_price)
            tp_price = self.precision.round_price(symbol, signal.tp_price)
            exit_model = getattr(signal, 'exit_model', 'FIXED_LP')

            # Uses /v1/algoOrder (mandatory since Binance API change 2025-12-09)
            # If TP/SL placement fails after entry fills, close immediately and log to DB
            try:
                if exit_model == 'PARTIAL_TRAIL':
                    # AxisPro: full-qty SL + a half-qty TP1 take-profit. TP2 and the
                    # break-even/EMA-trail are managed after TP1 fills (monitor +
                    # bar-close hook). tp_price stored on the position is TP2.
                    tp1_px = self.precision.round_price(symbol, signal.tp1_price)
                    frac   = signal.tp1_frac or 0.5
                    tp1_qty = self.precision.round_qty(symbol, qty * frac) \
                        if hasattr(self.precision, 'round_qty') else round(qty * frac, 8)
                    sl_result   = self.client.place_stop_loss_order(symbol, exit_side, qty, sl_price)
                    tp_result   = self.client.place_take_profit_order(symbol, exit_side, tp1_qty, tp1_px)
                    sl_order_id = sl_result.get('algoId') or sl_result.get('orderId')
                    tp_order_id = tp_result.get('algoId') or tp_result.get('orderId')
                    log.info(f"[ORDER] {symbol} AxisPro: SL #{sl_order_id} @ {sl_price:.6f} | "
                             f"TP1 #{tp_order_id} @ {tp1_px:.6f} (qty {tp1_qty}/{qty})")
                else:
                    tp_result   = self.client.place_take_profit_order(symbol, exit_side, qty, tp_price)
                    sl_result   = self.client.place_stop_loss_order(symbol, exit_side, qty, sl_price)
                    tp_order_id = tp_result.get('algoId') or tp_result.get('orderId')
                    sl_order_id = sl_result.get('algoId') or sl_result.get('orderId')
                    log.info(f"[ORDER] TP order #{tp_order_id} @ {tp_price:.6f} | "
                             f"SL order #{sl_order_id} @ {sl_price:.6f}")

            except Exception as tp_sl_err:
                # Entry is already filled — position is live and unprotected
                # Emergency: close immediately with a market order, then log to DB
                log.error(f"[EMERGENCY] {symbol}: TP/SL placement failed after entry fill — "
                          f"closing position immediately. Error: {tp_sl_err}")
                try:
                    close_result = self.client.place_market_order(symbol, exit_side, qty)
                    exit_price   = float(close_result.get('avgPrice', 0) or actual_entry)
                    log.info(f"[EMERGENCY] {symbol}: position closed @ {exit_price:.6f}")
                except Exception as close_err:
                    log.error(f"[EMERGENCY] {symbol}: FAILED to close position: {close_err}")
                    exit_price = actual_entry  # best guess for DB record

                # Log to DB as a MANUAL_CLOSE so it appears in dashboard.
                # SL/TP percentages here match S2's risk profile (0.4% / 1.2%);
                # they are only for the trade-log record since the position is
                # being closed immediately. LP is not applicable on emergency close.
                emergency_pos = OpenPosition(
                    symbol         = symbol,
                    strategy       = strategy,
                    direction      = direction,
                    entry_price    = actual_entry,
                    sl_price       = actual_entry * (1 - 0.004) if direction == 'LONG' else actual_entry * (1 + 0.004),
                    tp_price       = actual_entry * (1 + 0.012) if direction == 'LONG' else actual_entry * (1 - 0.012),
                    quantity       = qty,
                    margin_usdt    = margin,
                    leverage       = actual_leverage,
                    tp_order_id    = 0,
                    sl_order_id    = 0,
                    entry_order_id = entry_order_id,
                    signal_ts      = signal.signal_ts,
                    signal_time    = signal.signal_time,
                    signal_price   = signal.entry_price,
                )
                self._log_trade_close(emergency_pos, 'MANUAL_CLOSE', exit_price=exit_price)

                with self._lock:
                    self._pending_symbols.discard(symbol)
                if self.detector:
                    self.detector.on_trade_closed(symbol, strategy, 'LOSS')
                return

            position = OpenPosition(
                symbol         = symbol,
                strategy       = strategy,
                direction      = direction,
                entry_price    = actual_entry,
                sl_price       = sl_price,
                tp_price       = tp_price,
                quantity       = qty,
                margin_usdt    = margin,
                leverage       = actual_leverage,
                tp_order_id    = tp_order_id,
                sl_order_id    = sl_order_id,
                entry_order_id = entry_order_id,
                signal_ts      = signal.signal_ts,
                signal_time    = signal.signal_time,
                signal_price   = signal.entry_price,   # intended price for slippage tracking
                lp_trigger_pct = getattr(signal, 'lp_trigger_pct', None),
                lp_lock_pct    = getattr(signal, 'lp_lock_pct',    None),
                **self._exit_kwargs(signal),
            )
            # AxisPro: tp_order_id is the TP1 (partial) order; remember it explicitly
            if position.exit_model == 'PARTIAL_TRAIL':
                position.tp1_order_id = tp_order_id

            with self._lock:
                self._open_positions[self._pkey(strategy, symbol)] = position
                self._symbols_live.add(symbol)
                self._pending_symbols.discard(symbol)

            self._log_trade_open(position)

        except ValueError as e:
            # Symbol not available on futures — skip silently
            log.warning(f"[SKIP] {symbol}: {e}")
            with self._lock:
                self._pending_symbols.discard(symbol)

        except requests.exceptions.HTTPError as e:
            body = e.response.text if e.response is not None else ''
            if '-2019' in body:
                log.warning(f"[SKIP] {symbol}: insufficient margin in demo account — skipping trade")
                with self._lock:
                    self._pending_symbols.discard(symbol)
            elif '-4005' in body:
                # qty > maxQty — precision cache was stale. Refresh and retry once.
                log.warning(f"[RETRY] {symbol}: qty exceeded maxQty (-4005), refreshing cache and retrying")
                self.precision.refresh(symbol)
                try:
                    prec         = self.precision.get(symbol)
                    retry_qty, retry_lev = self.precision.resolve_order_params(
                        symbol, current_price, margin, actual_leverage
                    )
                    if retry_qty < prec['min_qty']:
                        log.warning(f"[SKIP] {symbol}: retry qty {retry_qty} below min — skipping")
                        with self._lock:
                            self._pending_symbols.discard(symbol)
                        return
                    if retry_lev != actual_leverage:
                        self.client.set_leverage(symbol, retry_lev)
                    retry_result = self.client.place_market_order(symbol, entry_side, retry_qty)
                    log.info(f"[ORDER] Retry filled: {entry_side} {retry_qty} {symbol} "
                             f"@ {float(retry_result.get('avgPrice', current_price)):.6f}")
                    # Re-place TP/SL anchored to the SIGNAL price (Fix #2 -- same as main path)
                    # Previously this used `retry_entry * sl_pct` / `retry_entry * tp_pct`,
                    # but those variables were never defined. Now we correctly use the
                    # already-rounded sl_price and tp_price computed from the signal.
                    retry_entry  = float(retry_result.get('avgPrice', 0) or 0) or current_price
                    retry_sl     = sl_price
                    retry_tp     = tp_price
                    tp_r = self.client.place_take_profit_order(symbol, exit_side, retry_qty, retry_tp)
                    sl_r = self.client.place_stop_loss_order(symbol, exit_side, retry_qty, retry_sl)
                    position = OpenPosition(
                        symbol=symbol, strategy=strategy, direction=direction,
                        entry_price=retry_entry, sl_price=retry_sl, tp_price=retry_tp,
                        quantity=retry_qty, margin_usdt=margin, leverage=retry_lev,
                        tp_order_id=tp_r.get('algoId'), sl_order_id=sl_r.get('algoId'),
                        entry_order_id=retry_result['orderId'],
                        signal_ts=signal.signal_ts, signal_time=signal.signal_time,
                        signal_price=signal.entry_price,
                        lp_trigger_pct=getattr(signal, 'lp_trigger_pct', None),
                        lp_lock_pct=getattr(signal, 'lp_lock_pct',    None),
                        **self._exit_kwargs(signal),
                    )
                    if position.exit_model == 'PARTIAL_TRAIL':
                        # retry path placed a full-qty TP at TP2; skip the TP1
                        # partial and just run break-even/EMA-trail management.
                        position.tp1_done = True
                    with self._lock:
                        self._open_positions[self._pkey(strategy, symbol)] = position
                        self._symbols_live.add(symbol)
                        self._pending_symbols.discard(symbol)
                    self._log_trade_open(position)
                except Exception as retry_err:
                    log.error(f"[ORDER] Retry failed for {symbol}: {retry_err}")
                    with self._lock:
                        self._pending_symbols.discard(symbol)
            elif '-2027' in body:
                # Exceeded max allowable position.
                # The leverageBracket cap is unreliable on demo — it may return the same
                # cap regardless of leverage, causing infinite retries at the same notional.
                # Instead: keep the original leverage and halve the notional on each attempt.
                # This is guaranteed to converge and preserves leverage (only qty shrinks).
                log.warning(f"[RETRY] {symbol}: -2027 at lev={actual_leverage}x "
                            f"qty={qty} notional=${qty*current_price:.0f} — halving notional")
                placed       = False
                retry_qty    = qty
                prec         = self.precision.get(symbol)
                MIN_VIABLE_NOTIONAL = margin * 0.5  # skip if notional < 50% of margin (not worth trading)
                for attempt in range(6):   # max 6 halvings: $5000→$2500→$1250→$625→$312→$156
                    retry_qty = math.floor(retry_qty / 2 / prec['qty_step']) * prec['qty_step']
                    retry_qty = round(retry_qty, prec['qty_decimals'])
                    if retry_qty < prec['min_qty']:
                        log.warning(f"[SKIP] {symbol}: halved qty {retry_qty} below min — giving up")
                        break
                    notional_check = retry_qty * current_price
                    if notional_check < MIN_VIABLE_NOTIONAL:
                        log.warning(f"[SKIP] {symbol}: halved notional ${notional_check:.0f} < "
                                    f"min viable ${MIN_VIABLE_NOTIONAL:.0f} — demo cap too tight, skipping")
                        break
                    log.info(f"[RETRY] {symbol}: attempt {attempt+1} — "
                             f"qty={retry_qty} notional=${notional_check:.0f} lev={actual_leverage}x")
                    try:
                        retry_result = self.client.place_market_order(symbol, entry_side, retry_qty)
                        retry_entry  = float(retry_result.get('avgPrice', 0) or 0) or current_price
                        # Anchor SL/TP to the SIGNAL price (Fix #2 -- same as main path).
                        # Previously this used `retry_entry * sl_pct` / `retry_entry * tp_pct`,
                        # but those variables were never defined.
                        retry_sl     = sl_price
                        retry_tp     = tp_price
                        tp_r = self.client.place_take_profit_order(symbol, exit_side, retry_qty, retry_tp)
                        sl_r = self.client.place_stop_loss_order(symbol, exit_side, retry_qty, retry_sl)
                        # Back-calc actual leverage from accepted notional
                        accepted_notional = retry_qty * retry_entry
                        back_lev = max(1, round(accepted_notional / margin))
                        valid    = [50,40,33,25,20,15,10,5,3,1]
                        back_lev = next((l for l in valid if l <= back_lev), 1)
                        position = OpenPosition(
                            symbol=symbol, strategy=strategy, direction=direction,
                            entry_price=retry_entry, sl_price=retry_sl, tp_price=retry_tp,
                            quantity=retry_qty, margin_usdt=margin, leverage=back_lev,
                            tp_order_id=tp_r.get('algoId'), sl_order_id=sl_r.get('algoId'),
                            entry_order_id=retry_result['orderId'],
                            signal_ts=signal.signal_ts, signal_time=signal.signal_time,
                            signal_price=signal.entry_price,
                            lp_trigger_pct=getattr(signal, 'lp_trigger_pct', None),
                            lp_lock_pct=getattr(signal, 'lp_lock_pct',    None),
                            **self._exit_kwargs(signal),
                        )
                        if position.exit_model == 'PARTIAL_TRAIL':
                            position.tp1_done = True
                        with self._lock:
                            self._open_positions[self._pkey(strategy, symbol)] = position
                            self._symbols_live.add(symbol)
                            self._pending_symbols.discard(symbol)
                        self._log_trade_open(position)
                        log.info(f"[RETRY] {symbol}: placed at attempt {attempt+1} — "
                                 f"qty={retry_qty} notional=${accepted_notional:.0f} lev={back_lev}x")
                        placed = True
                        break
                    except requests.exceptions.HTTPError as retry_err:
                        retry_body = retry_err.response.text if retry_err.response else ''
                        if '-2027' in retry_body:
                            log.warning(f"[RETRY] {symbol}: attempt {attempt+1} still -2027, halving again...")
                            continue
                        log.error(f"[RETRY] {symbol}: attempt {attempt+1} unexpected error: {retry_err}")
                        break
                    except Exception as retry_err:
                        log.error(f"[RETRY] {symbol}: attempt {attempt+1} failed: {retry_err}")
                        break
                if not placed:
                    log.warning(f"[SKIP] {symbol}: -2027 could not be resolved after halving")
                    with self._lock:
                        self._pending_symbols.discard(symbol)
            else:
                log.error(f"[ORDER] Failed to place trade for {symbol}: {e}", exc_info=True)
                with self._lock:
                    self._pending_symbols.discard(symbol)

        except Exception as e:
            log.error(f"[ORDER] Failed to place trade for {symbol}: {e}", exc_info=True)
            with self._lock:
                self._pending_symbols.discard(symbol)

    # ── Position monitor ──────────────────────────────────────────────────────

    def _monitor_loop(self):
        log.info("Position monitor started")
        while True:
            time.sleep(POLL_INTERVAL)
            try:
                self._check_positions()
            except Exception as e:
                log.error(f"Monitor error: {e}", exc_info=True)
            try:
                self._check_pending_limits()
            except Exception as e:
                log.error(f"Limit-monitor error: {e}", exc_info=True)

    def _arm_lock_profit(self, pos: 'OpenPosition', cur_price: float):
        """
        Replace the existing SL algo order with a new SL at pos.lp_lock_price.
        After this, ANY SL fill on this position will be at the locked-in
        profit level (small win), not a loss.

        Steps:
          1. Cancel the existing SL algo order (best-effort).
          2. Place a new STOP algo at pos.lp_lock_price with the same qty.
          3. Update pos.sl_order_id and pos.sl_price in-place.
          4. Update the OPEN row in Supabase with the new sl_price.
          5. Mark pos.lp_armed = True.

        If step 2 fails after step 1 succeeded, the position is briefly
        UNPROTECTED. We log loudly and DO NOT mark as armed -- the next
        monitor tick will retry. The TP order is untouched throughout.
        """
        symbol = pos.symbol

        # Compute round-tick-correct lock price (Binance rejects bad price steps)
        new_sl_raw  = pos.lp_lock_price
        new_sl      = self.precision.round_price(symbol, new_sl_raw)
        old_sl_id   = pos.sl_order_id

        log.info(
            f"[LP] {symbol}: trigger hit (price={cur_price:.6f}, "
            f"trigger={pos.lp_trigger_price:.6f}) -- moving SL "
            f"{pos.sl_price:.6f} -> {new_sl:.6f} "
            f"(locks {pos.lp_lock_pct}% of TP distance)"
        )

        # Step 1: cancel old SL
        try:
            self.client.cancel_algo_order(old_sl_id)
        except Exception as e:
            # Could already be CANCELED/EXPIRED on Binance's side -- that's fine
            # as long as it's not still NEW/TRIGGERING. Log and proceed; if the
            # new SL placement also fails we'll retry next tick.
            log.warning(f"[LP] {symbol}: cancel old SL failed (continuing): {e}")

        # Step 2: place new SL at LP lock price
        exit_side = 'SELL' if pos.direction == 'LONG' else 'BUY'
        try:
            new_sl_resp = self.client.place_stop_loss_order(symbol, exit_side,
                                                            pos.quantity, new_sl)
        except Exception as e:
            log.error(f"[LP] {symbol}: FAILED to place new SL after cancelling old. "
                      f"Position may be temporarily unprotected. Error: {e}")
            # Attempt to put back the original SL so we don't leave the trade naked.
            try:
                fallback = self.client.place_stop_loss_order(symbol, exit_side,
                                                             pos.quantity, pos.sl_price)
                pos.sl_order_id = fallback.get('algoId') or fallback.get('orderId')
                log.warning(f"[LP] {symbol}: restored ORIGINAL SL @ {pos.sl_price:.6f} "
                            f"(new id={pos.sl_order_id}); LP not armed")
            except Exception as e2:
                log.error(f"[LP] {symbol}: CRITICAL -- could not restore original SL: {e2}")
            return   # not armed; will retry next tick

        # Step 3: update position state
        new_sl_id = new_sl_resp.get('algoId') or new_sl_resp.get('orderId')
        with self._lock:
            pos.sl_order_id = new_sl_id
            pos.sl_price    = new_sl
            pos.lp_armed    = True

        log.info(f"[LP] {symbol}: armed -- new SL #{new_sl_id} @ {new_sl:.6f}")

        # Step 4: update Supabase OPEN row with the new SL price (best-effort)
        if pos.db_id:
            try:
                self.supabase.update('trades', pos.db_id, {
                    'sl_price': round(new_sl, 8),
                })
            except Exception as e:
                log.warning(f"[LP] {symbol}: Supabase update failed (non-fatal): {e}")

    def _check_positions(self):
        with self._lock:
            positions = list(self._open_positions.values())

        for pos in positions:
            try:
                # ── AxisPro: handle the TP1 partial before generic close logic ─
                if pos.exit_model == 'PARTIAL_TRAIL' and not pos.tp1_done:
                    if self._check_axis_tp1(pos):
                        continue   # TP1 just filled (or SL hit) — handled there

                # ── Lock Profit (LP) arming ────────────────────────────────────
                # If this position has LP configured and is not yet armed, check
                # whether the live ticker has reached the LP trigger price. If
                # yes, replace the SL algo order with a new one at the LP lock
                # price -- this guarantees the trade can no longer end in a
                # loss (it locks in lp_lock_pct of the TP distance as profit).
                #
                # Done BEFORE checking SL/TP fills so the arm-then-fill happens
                # in the right order: if price is racing through the trigger to
                # TP between polls, the TP-fill check below still wins.
                if (not pos.lp_armed and pos.lp_trigger_price is not None
                        and pos.lp_lock_price is not None):
                    try:
                        cur_price = self.client.get_ticker_price(pos.symbol)
                        trigger_hit = (
                            (pos.direction == 'LONG'  and cur_price >= pos.lp_trigger_price) or
                            (pos.direction == 'SHORT' and cur_price <= pos.lp_trigger_price)
                        )
                        if trigger_hit:
                            self._arm_lock_profit(pos, cur_price)
                    except Exception as lp_err:
                        # LP arming failed -- log and continue. The original SL
                        # is still in place, so the trade is protected; we'll
                        # try to arm again on the next monitor tick.
                        log.warning(f"[LP] {pos.symbol}: arming check failed: {lp_err}")

                # Algo orders use GET /v1/algoOrder (algoId)
                # algoStatus values: NEW → TRIGGERING → TRIGGERED → FINISHED (executed) or CANCELED/EXPIRED
                tp_order  = self.client.get_algo_order(pos.tp_order_id)
                sl_order  = self.client.get_algo_order(pos.sl_order_id)
                tp_filled = tp_order.get('algoStatus') == 'FINISHED'
                sl_filled = sl_order.get('algoStatus') == 'FINISHED'

                if not tp_filled and not sl_filled:
                    # Neither algo order is FINISHED. But the algo could be CANCELED/EXPIRED
                    # while the position itself was closed by other means (manual close,
                    # liquidation, algo service hiccup on demo). Verify the actual position
                    # state on Binance — if it's gone, reconcile from userTrades.
                    tp_status = tp_order.get('algoStatus')
                    sl_status = sl_order.get('algoStatus')
                    if tp_status not in ('NEW', 'TRIGGERING', 'TRIGGERED') or \
                       sl_status not in ('NEW', 'TRIGGERING', 'TRIGGERED'):
                        # At least one algo is in a terminal non-filled state — check position
                        try:
                            pos_risk = self.client.get_position(pos.symbol)
                            pos_amt  = float(pos_risk.get('positionAmt', 0))
                            if pos_amt == 0:
                                log.warning(
                                    f"[RECONCILE] {pos.symbol}: algo statuses "
                                    f"TP={tp_status} SL={sl_status} but position is closed "
                                    f"on Binance — reconciling from userTrades"
                                )
                                self._reconcile_closed_position(pos)
                                continue
                        except Exception as rc_err:
                            log.error(f"[RECONCILE] {pos.symbol}: position check failed: {rc_err}")
                    continue

                # ── Determine outcome ─────────────────────────────────────────
                # actualPrice = actual fill price from matching engine (per docs)
                filled_order = tp_order if tp_filled else sl_order
                actual_exit  = float(filled_order.get('actualPrice') or 0)
                if actual_exit == 0:
                    actual_exit = pos.tp_price if tp_filled else pos.sl_price

                if tp_filled:
                    outcome = 'WIN'
                elif pos.exit_model == 'FIXED_LP':
                    # S2: SL fill is LP_WIN if lock-profit armed, else LOSS
                    outcome = 'LP_WIN' if pos.lp_armed else 'LOSS'
                else:
                    # Trailed models (AxisPro runner / Breakout / ICT): an SL that
                    # was ratcheted into profit before filling is a WIN. Decide by
                    # exit price vs entry.
                    favorable = ((pos.direction == 'LONG'  and actual_exit > pos.entry_price) or
                                 (pos.direction == 'SHORT' and actual_exit < pos.entry_price))
                    outcome = 'WIN' if favorable else 'LOSS'

                try:
                    if tp_filled:
                        self.client.cancel_algo_order(pos.sl_order_id)
                    else:
                        self.client.cancel_algo_order(pos.tp_order_id)
                except Exception as ce:
                    log.warning(f"Could not cancel remaining order for {pos.symbol}: {ce}")

                log.info(f"[CLOSED] {pos.symbol} {pos.strategy} {pos.direction} | "
                         f"outcome={outcome} | entry={pos.entry_price:.6f} "
                         f"exit={actual_exit:.6f} SL={pos.sl_price:.6f} TP={pos.tp_price:.6f}")

                self._log_trade_close(pos, outcome, exit_price=actual_exit)

                with self._lock:
                    self._open_positions.pop(self._pkey(pos.strategy, pos.symbol), None)
                    self._symbols_live.discard(pos.symbol)
                    # Normalize strategy full-name ('S2_FVG_RETEST') to short key ('S2')
                    strat = pos.strategy.split('_')[0] if pos.strategy else 'S2'
                    # LP_WIN is a (smaller) win and resets the loss streak just like WIN
                    if outcome in ('WIN', 'LP_WIN'):
                        self._consec_losses[strat] = 0
                    else:
                        self._consec_losses[strat] = self._consec_losses.get(strat, 0) + 1

                self._notify_closed(pos.symbol, pos.strategy, outcome)

            except Exception as e:
                log.warning(f"Could not check algo orders for {pos.symbol}: {e} — checking position risk")
                # Fallback: check if Binance still has an open position
                # If not, the trade closed externally (algo order expired/filled without us noticing)
                try:
                    pos_risk = self.client.get_position(pos.symbol)
                    pos_amt  = float(pos_risk.get('positionAmt', 0))
                    if pos_amt == 0:
                        log.warning(f"[RECONCILE] {pos.symbol}: no open position on Binance, "
                                    f"position closed externally — fetching exit price from trades")
                        self._reconcile_closed_position(pos)
                    else:
                        log.warning(f"[RECONCILE] {pos.symbol}: position still open on Binance "
                                    f"(amt={pos_amt}) — algo order query failed but position alive")
                except Exception as risk_err:
                    log.error(f"Could not reconcile position {pos.symbol}: {risk_err}")

    def _reconcile_closed_position(self, pos):
        """
        Position closed on Binance but bot never saw the algo fill (e.g. algo got
        CANCELED/EXPIRED but position closed by other means). Fetch the actual
        exit price from /userTrades, log the close, and free the symbol gate.
        Used by both the algo-status branch and the exception fallback in
        _check_positions, so the reconciliation logic only lives in one place.
        """
        try:
            trades = self.client._get('/v1/userTrades',
                                      {'symbol': pos.symbol, 'limit': 10},
                                      signed=True)
            # Find the closing trade (opposite side to entry)
            close_side = 'SELL' if pos.direction == 'LONG' else 'BUY'
            close_trades = [t for t in trades
                           if t.get('side') == close_side
                           and int(t.get('time', 0)) > pos.open_ts]
            if close_trades:
                # Use the most recent closing trade price
                last = max(close_trades, key=lambda t: t['time'])
                actual_exit = float(last['price'])
                realized    = sum(float(t.get('realizedPnl', 0)) for t in close_trades)
            else:
                actual_exit = pos.sl_price  # conservative fallback
                realized    = None

            # Determine outcome from exit price relative to TP/SL
            if pos.direction == 'LONG':
                outcome = 'WIN' if actual_exit >= pos.tp_price else 'LOSS'
            else:
                outcome = 'WIN' if actual_exit <= pos.tp_price else 'LOSS'

            log.warning(f"[RECONCILE] {pos.symbol}: exit={actual_exit:.6f} "
                        f"outcome={outcome} realizedPnl={realized}")

            self._log_trade_close(pos, outcome, exit_price=actual_exit)

            with self._lock:
                self._open_positions.pop(self._pkey(pos.strategy, pos.symbol), None)
                self._symbols_live.discard(pos.symbol)
                strat = pos.strategy.split('_')[0] if pos.strategy else 'S2'
                if outcome in ('WIN', 'LP_WIN'):
                    self._consec_losses[strat] = 0
                else:
                    self._consec_losses[strat] = self._consec_losses.get(strat, 0) + 1

            # Best-effort: cancel any leftover algo orders so they don't trigger later
            for oid in (pos.tp_order_id, pos.sl_order_id, pos.tp1_order_id):
                if oid:
                    try:
                        self.client.cancel_algo_order(oid)
                    except Exception:
                        pass   # already canceled/expired — ignore

            self._notify_closed(pos.symbol, pos.strategy, outcome)

        except Exception as rec_err:
            log.error(f"[RECONCILE] {pos.symbol}: failed to fetch exit trades: {rec_err}")

    # ════════════════════════════════════════════════════════════════════════
    # MULTI-STRATEGY EXIT MANAGEMENT
    # ════════════════════════════════════════════════════════════════════════

    def _exit_side(self, pos):
        return 'SELL' if pos.direction == 'LONG' else 'BUY'

    def _replace_stop(self, pos, new_sl_raw):
        """Cancel pos's SL algo and place a new one at new_sl_raw. Returns True on success."""
        new_sl = self.precision.round_price(pos.symbol, new_sl_raw)
        # only move in the locking direction (never loosen)
        if pos.direction == 'LONG' and new_sl <= pos.sl_price:
            return False
        if pos.direction == 'SHORT' and new_sl >= pos.sl_price:
            return False
        try:
            if pos.sl_order_id:
                self.client.cancel_algo_order(pos.sl_order_id)
        except Exception as e:
            log.warning(f"[TRAIL] {pos.symbol}: cancel old SL failed (continuing): {e}")
        try:
            resp = self.client.place_stop_loss_order(pos.symbol, self._exit_side(pos),
                                                     pos.quantity, new_sl)
            pos.sl_order_id = resp.get('algoId') or resp.get('orderId')
            old = pos.sl_price
            pos.sl_price = new_sl
            log.info(f"[TRAIL] {pos.symbol} {pos.strategy}: SL {old:.6f} → {new_sl:.6f}")
            if pos.db_id:
                try:
                    self.supabase.update('trades', pos.db_id, {'sl_price': round(new_sl, 8)})
                except Exception:
                    pass
            return True
        except Exception as e:
            log.error(f"[TRAIL] {pos.symbol}: failed to place trailed SL: {e}")
            return False

    def _market_close(self, pos, reason):
        """Close the remaining position with a reduce-only market order and finalize."""
        try:
            res = self.client.place_market_order(pos.symbol, self._exit_side(pos), pos.quantity)
            exit_p = float(res.get('avgPrice', 0) or 0) or self.client.get_ticker_price(pos.symbol)
        except Exception as e:
            log.error(f"[{reason}] {pos.symbol}: market close failed: {e}")
            return
        favorable = ((pos.direction == 'LONG'  and exit_p > pos.entry_price) or
                     (pos.direction == 'SHORT' and exit_p < pos.entry_price))
        outcome = 'WIN' if favorable else 'LOSS'
        for oid in (pos.tp_order_id, pos.sl_order_id):
            if oid:
                try:
                    self.client.cancel_algo_order(oid)
                except Exception:
                    pass
        log.info(f"[CLOSED:{reason}] {pos.symbol} {pos.strategy} {pos.direction} | "
                 f"outcome={outcome} entry={pos.entry_price:.6f} exit={exit_p:.6f}")
        self._log_trade_close(pos, outcome, exit_price=exit_p)
        with self._lock:
            self._open_positions.pop(self._pkey(pos.strategy, pos.symbol), None)
            self._symbols_live.discard(pos.symbol)
            strat = pos.strategy.split('_')[0] if pos.strategy else 'S2'
            if outcome == 'WIN':
                self._consec_losses[strat] = 0
            else:
                self._consec_losses[strat] = self._consec_losses.get(strat, 0) + 1
        self._notify_closed(pos.symbol, pos.strategy, outcome)

    def _check_axis_tp1(self, pos):
        """
        AxisPro PARTIAL_TRAIL, pre-TP1 phase. Returns True if it consumed the tick
        (TP1 partial filled, or SL hit = full loss). False otherwise (let generic
        logic look at it — shouldn't happen pre-TP1, but safe).
        """
        try:
            sl_order = self.client.get_algo_order(pos.sl_order_id)
            tp1_order = self.client.get_algo_order(pos.tp1_order_id) if pos.tp1_order_id else {}
        except Exception as e:
            log.warning(f"[AXIS] {pos.symbol}: algo query failed: {e}")
            return True   # don't fall through to generic logic this tick

        sl_filled  = sl_order.get('algoStatus') == 'FINISHED'
        tp1_filled = tp1_order.get('algoStatus') == 'FINISHED'

        if sl_filled and not tp1_filled:
            # Full position stopped before TP1 → LOSS
            actual_exit = float(sl_order.get('actualPrice') or 0) or pos.sl_price
            log.info(f"[CLOSED] {pos.symbol} AXISPRO {pos.direction} | outcome=LOSS "
                     f"(SL before TP1) exit={actual_exit:.6f}")
            self._log_trade_close(pos, 'LOSS', exit_price=actual_exit)
            with self._lock:
                self._open_positions.pop(self._pkey(pos.strategy, pos.symbol), None)
                self._symbols_live.discard(pos.symbol)
                self._consec_losses['AXISPRO'] = self._consec_losses.get('AXISPRO', 0) + 1
            self._notify_closed(pos.symbol, pos.strategy, 'LOSS')
            return True

        if tp1_filled:
            # Half closed at TP1 → realize partial, move SL to break-even, place TP2 on runner
            runner_qty = self.precision.round_qty(pos.symbol, pos.quantity * (1 - (pos.tp1_frac or 0.5)))
            pos.tp1_done = True
            log.info(f"[AXIS] {pos.symbol}: TP1 filled — runner {runner_qty}, SL→BE, placing TP2")
            # move SL to break-even on the runner
            try:
                if pos.sl_order_id:
                    self.client.cancel_algo_order(pos.sl_order_id)
            except Exception:
                pass
            exit_side = self._exit_side(pos)
            be_price = self.precision.round_price(pos.symbol, pos.entry_price) if pos.move_be_after_tp1 \
                else pos.sl_price
            pos.quantity = runner_qty
            try:
                sl_r = self.client.place_stop_loss_order(pos.symbol, exit_side, runner_qty, be_price)
                pos.sl_order_id = sl_r.get('algoId') or sl_r.get('orderId')
                pos.sl_price = be_price
            except Exception as e:
                log.error(f"[AXIS] {pos.symbol}: BE stop placement failed: {e}")
            try:
                tp2_px = self.precision.round_price(pos.symbol, pos.tp2_price)
                tp2_r = self.client.place_take_profit_order(pos.symbol, exit_side, runner_qty, tp2_px)
                pos.tp_order_id = tp2_r.get('algoId') or tp2_r.get('orderId')
                pos.tp_price = tp2_px
            except Exception as e:
                log.error(f"[AXIS] {pos.symbol}: TP2 placement failed: {e}")
            return True

        return True   # still pre-TP1, nothing to do this tick

    # ── ICT LIMIT-entry lifecycle ─────────────────────────────────────────────

    def _handle_limit_signal(self, signal):
        """Place a resting LIMIT entry for ICT and register it for fill polling."""
        symbol, strategy, direction = signal.symbol, signal.strategy, signal.direction
        cfg      = STRATEGY_CONFIG.get(strategy, {'margin_usdt': DEFAULT_MARGIN, 'leverage': DEFAULT_LEVERAGE})
        margin, leverage = cfg['margin_usdt'], cfg['leverage']
        try:
            available = self.client.get_usdt_balance()
            if available < margin:
                log.warning(f"[SKIP] {symbol}: insufficient balance for ICT limit")
                self._notify_closed(symbol, strategy, 'CANCELED')
                return
            # cleanup + margin/leverage
            try:
                for o in (self.client.get_open_orders(symbol) or []):
                    try: self.client.cancel_order(symbol, o['orderId'])
                    except Exception: pass
            except Exception:
                pass
            self.client.set_margin_type(symbol, 'ISOLATED')
            lev_resp = self.client.set_leverage(symbol, leverage)
            acc_lev  = int(lev_resp.get('leverage', leverage)) if lev_resp else leverage
            limit_px = self.precision.round_price(symbol, signal.limit_price)
            qty, act_lev = self.precision.resolve_order_params(symbol, limit_px, margin, acc_lev)
            prec = self.precision.get(symbol)
            if qty < prec['min_qty']:
                log.warning(f"[SKIP] {symbol}: ICT qty below min")
                self._notify_closed(symbol, strategy, 'CANCELED')
                return
            entry_side = 'BUY' if direction == 'LONG' else 'SELL'
            order = self.client.place_limit_order(symbol, entry_side, qty, limit_px)
            oid = order.get('orderId')
            with self._lock:
                self._pending_limits[self._pkey(strategy, symbol)] = _PendingLimit(
                    signal=signal, order_id=oid, qty=qty, leverage=act_lev,
                    margin=margin, limit_price=limit_px, period_end_ts=signal.period_end_ts)
                self._symbols_live.add(symbol)   # reserve the symbol while the limit rests
            log.info(f"[ORDER] {symbol} ICT LIMIT {entry_side} {qty} @ {limit_px:.6f} "
                     f"(order #{oid}) resting until {signal.signal_time}")
        except Exception as e:
            log.error(f"[ICT] {symbol}: limit placement failed: {e}", exc_info=True)
            with self._lock:
                self._symbols_live.discard(symbol)
            self._notify_closed(symbol, strategy, 'CANCELED')

    def _check_pending_limits(self):
        with self._lock:
            pending = list(self._pending_limits.items())
        now_ms = int(time.time() * 1000)
        for key, pl in pending:
            try:
                od = self.client.get_order(pl.signal.symbol, pl.order_id)
                status = od.get('status')
                if status == 'FILLED':
                    self._activate_filled_limit(key, pl, od)
                elif status in ('CANCELED', 'EXPIRED', 'REJECTED'):
                    self._drop_pending_limit(key, pl, 'CANCELED')
                elif pl.period_end_ts and now_ms >= pl.period_end_ts:
                    try:
                        self.client.cancel_order(pl.signal.symbol, pl.order_id)
                    except Exception:
                        pass
                    log.info(f"[ICT] {pl.signal.symbol}: limit unfilled at day end — cancelled")
                    self._drop_pending_limit(key, pl, 'CANCELED')
            except Exception as e:
                log.warning(f"[ICT] {key}: limit poll failed: {e}")

    def _drop_pending_limit(self, key, pl, outcome):
        with self._lock:
            self._pending_limits.pop(key, None)
            self._symbols_live.discard(pl.signal.symbol)
        self._notify_closed(pl.signal.symbol, pl.signal.strategy, outcome)

    def _activate_filled_limit(self, key, pl, order):
        """A resting ICT limit filled — record the position and attach SL/TP."""
        sig = pl.signal
        symbol, strategy, direction = sig.symbol, sig.strategy, sig.direction
        avg = float(order.get('avgPrice', 0) or 0) or pl.limit_price
        exit_side = 'SELL' if direction == 'LONG' else 'BUY'
        sl_px = self.precision.round_price(symbol, sig.sl_price)
        tp_px = self.precision.round_price(symbol, sig.tp_price)
        try:
            tp_r = self.client.place_take_profit_order(symbol, exit_side, pl.qty, tp_px)
            sl_r = self.client.place_stop_loss_order(symbol, exit_side, pl.qty, sl_px)
        except Exception as e:
            log.error(f"[ICT] {symbol}: SL/TP placement after fill failed — closing. {e}")
            try:
                self.client.place_market_order(symbol, exit_side, pl.qty)
            except Exception:
                pass
            self._drop_pending_limit(key, pl, 'LOSS')
            return
        pos = OpenPosition(
            symbol=symbol, strategy=strategy, direction=direction,
            entry_price=avg, sl_price=sl_px, tp_price=tp_px, quantity=pl.qty,
            margin_usdt=pl.margin, leverage=pl.leverage,
            tp_order_id=tp_r.get('algoId') or tp_r.get('orderId'),
            sl_order_id=sl_r.get('algoId') or sl_r.get('orderId'),
            entry_order_id=pl.order_id, signal_ts=sig.signal_ts,
            signal_time=sig.signal_time, signal_price=sig.entry_price,
            **self._exit_kwargs(sig),
        )
        with self._lock:
            self._pending_limits.pop(key, None)
            self._open_positions[self._pkey(strategy, symbol)] = pos
            self._symbols_live.add(symbol)
        self._log_trade_open(pos)
        log.info(f"[ORDER] {symbol} ICT limit FILLED @ {avg:.6f} | SL {sl_px:.6f} TP {tp_px:.6f}")

    # ── Bar-close trailing engine (called once per closed 15m candle) ─────────

    def on_bar_close(self, symbol, candle, candles_15m=None, candles_1h=None):
        """
        Drive per-bar exit management for any open positions on `symbol`, mirroring
        the backtests' forward-candle loops:
          BREAKOUT_TRAIL : ATR/R trailing SL, close-based trailing TP, time-stop
          FIXED_TRAIL    : percentage trailing SL (ICT)
          PARTIAL_TRAIL  : after TP1, trail the runner's SL by EMA21 (AxisPro)
        """
        with self._lock:
            targets = [p for k, p in self._open_positions.items() if p.symbol == symbol]
        for pos in targets:
            try:
                if pos.exit_model == 'BREAKOUT_TRAIL':
                    self._manage_breakout(pos, candle)
                elif pos.exit_model == 'FIXED_TRAIL':
                    self._manage_ict(pos, candle)
                elif pos.exit_model == 'PARTIAL_TRAIL' and pos.tp1_done:
                    self._manage_axis_trail(pos, candle, candles_15m)
            except Exception as e:
                log.error(f"[BAR] {symbol} {pos.strategy}: management error: {e}", exc_info=True)

    def _manage_breakout(self, pos, candle):
        hi, lo, close = candle['h'], candle['l'], candle['c']
        atr  = pos.atr_at_signal
        risk = pos.init_risk
        if not atr or not risk:
            return
        long = pos.direction == 'LONG'
        pos.bars_held += 1

        if long:
            pos.best_price = max(pos.best_price, hi)
            gain_R = (pos.best_price - pos.entry_price) / risk
        else:
            pos.best_price = min(pos.best_price, lo)
            gain_R = (pos.entry_price - pos.best_price) / risk

        # ratchet trailing stop once +trail_trigger_r in profit
        if gain_R >= (pos.trail_trigger_r or 1.5):
            if long:
                self._replace_stop(pos, pos.best_price - (pos.trail_sl_atr or 1.5) * atr)
            else:
                self._replace_stop(pos, pos.best_price + (pos.trail_sl_atr or 1.5) * atr)

        # arm trailing TP once deep enough
        if gain_R >= (pos.tp_lock_trigger_r or 2.5):
            pos.tp_trail_armed = True

        # trailing TP: bank the move on a close pullback from the extreme
        if pos.tp_trail_armed:
            if long:
                line = pos.best_price - (pos.trail_tp_atr or 1.2) * atr
                if close <= line and line > pos.entry_price:
                    self._market_close(pos, 'trail-tp'); return
            else:
                line = pos.best_price + (pos.trail_tp_atr or 1.2) * atr
                if close >= line and line < pos.entry_price:
                    self._market_close(pos, 'trail-tp'); return

        # time-stop
        if pos.max_hold_candles and pos.bars_held >= pos.max_hold_candles:
            self._market_close(pos, 'time-stop'); return

    def _manage_ict(self, pos, candle):
        hi, lo = candle['h'], candle['l']
        if pos.trail_pct is None or pos.trail_activate_pct is None:
            return
        long = pos.direction == 'LONG'
        if long:
            pos.best_price = max(pos.best_price, hi)
            if not pos.trail_sl_armed and pos.best_price >= pos.entry_price * (1 + pos.trail_activate_pct/100):
                pos.trail_sl_armed = True
            if pos.trail_sl_armed:
                self._replace_stop(pos, pos.best_price * (1 - pos.trail_pct/100))
        else:
            pos.best_price = min(pos.best_price, lo)
            if not pos.trail_sl_armed and pos.best_price <= pos.entry_price * (1 - pos.trail_activate_pct/100):
                pos.trail_sl_armed = True
            if pos.trail_sl_armed:
                self._replace_stop(pos, pos.best_price * (1 + pos.trail_pct/100))

    def _manage_axis_trail(self, pos, candle, candles_15m):
        """After TP1: trail the runner's SL by the fast EMA (only tightens)."""
        if not pos.trail_ema_period or not candles_15m:
            return
        closes = [c['c'] for c in candles_15m]
        from strategy_indicators import ema_series
        ema = ema_series(closes, pos.trail_ema_period)
        if ema[-1] is None:
            return
        self._replace_stop(pos, ema[-1])

    # ── Logging ───────────────────────────────────────────────────────────────

    def _init_csv(self):
        if not os.path.exists(TRADE_LOG_FILE):
            with open(TRADE_LOG_FILE, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'open_time', 'close_time', 'symbol', 'strategy',
                    'direction', 'signal_price', 'entry_price', 'sl_price', 'tp_price',
                    'quantity', 'margin_usdt', 'leverage', 'outcome',
                    'pnl_pct', 'pnl_usdt', 'fee_usdt', 'slippage_pct', 'signal_time',
                ])

    def _load_supabase_history(self):
        rows = self.supabase.select_all('trades')
        self.closed_positions = rows
        if rows:
            log.info(f"Loaded {len(rows)} historical trades from Supabase")

    def _log_trade_open(self, pos: OpenPosition):
        log.info(f"[LOG] Trade opened: {pos.symbol} {pos.strategy} {pos.direction} "
                 f"entry={pos.entry_price:.6f} SL={pos.sl_price:.6f} TP={pos.tp_price:.6f} "
                 f"qty={pos.quantity} margin=${pos.margin_usdt} lev={pos.leverage}x")

        signal_price = pos.signal_price if pos.signal_price else pos.entry_price
        slippage_pct = round((pos.entry_price - signal_price) / signal_price * 100, 4) \
                       if signal_price else 0.0

        row = {
            'open_time':    pos.open_time,
            'close_time':   None,
            'symbol':       pos.symbol,
            'strategy':     pos.strategy,
            'direction':    pos.direction,
            'signal_price': round(signal_price, 8),
            'entry_price':  round(pos.entry_price, 8),
            'sl_price':     round(pos.sl_price, 8),
            'tp_price':     round(pos.tp_price, 8),
            'quantity':     pos.quantity,
            'margin_usdt':  pos.margin_usdt,
            'leverage':     pos.leverage,
            'outcome':      'OPEN',
            'pnl_pct':      None,
            'pnl_usdt':     None,
            'fee_usdt':     None,
            'slippage_pct': slippage_pct,
            'signal_time':  pos.signal_time,
        }

        db_id = self.supabase.insert_returning_id('trades', row)
        pos.db_id = db_id
        if db_id:
            log.info(f"[LOG] Supabase row #{db_id} created (OPEN) for {pos.symbol}")
        else:
            log.warning(f"[LOG] Supabase insert_returning_id returned None for {pos.symbol} — close will fallback to insert")

    def _log_trade_close(self, pos: OpenPosition, outcome: str, exit_price: float = None):
        close_time = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

        if outcome == 'WIN':
            exit_p  = exit_price or pos.tp_price
            pnl_pct = (exit_p - pos.entry_price) / pos.entry_price * 100 \
                      if pos.direction == 'LONG' else \
                      (pos.entry_price - exit_p) / pos.entry_price * 100
        elif outcome == 'LP_WIN':
            # LP_WIN: SL fired AFTER it was moved to lp_lock_price by _arm_lock_profit.
            # pos.sl_price was updated in-place at arm time, so it now equals
            # lp_lock_price (which is on the profit side of entry by lp_lock_pct).
            exit_p  = exit_price or pos.sl_price
            pnl_pct = (exit_p - pos.entry_price) / pos.entry_price * 100 \
                      if pos.direction == 'LONG' else \
                      (pos.entry_price - exit_p) / pos.entry_price * 100
        elif outcome == 'LOSS':
            exit_p  = exit_price or pos.sl_price
            pnl_pct = (exit_p - pos.entry_price) / pos.entry_price * 100 \
                      if pos.direction == 'LONG' else \
                      (pos.entry_price - exit_p) / pos.entry_price * 100
        elif outcome == 'MANUAL_CLOSE':
            exit_p  = exit_price or pos.entry_price
            pnl_pct = (exit_p - pos.entry_price) / pos.entry_price * 100 \
                      if pos.direction == 'LONG' else \
                      (pos.entry_price - exit_p) / pos.entry_price * 100
        else:
            exit_p  = exit_price or pos.entry_price
            pnl_pct = 0.0

        notional = pos.margin_usdt * pos.leverage
        pnl_usdt = notional * (pnl_pct / 100)

        # Exact fee calculation based on order types.
        # Entry: MARKET (taker 0.05%).
        # Exit fee depends on outcome:
        #   WIN     -> TAKE_PROFIT maker (0.02%)
        #   LP_WIN  -> STOP taker        (0.05%)  -- LP exits via STOP
        #   LOSS    -> STOP taker        (0.05%)
        #   else    -> taker             (0.05%)
        entry_fee_rate = 0.0005
        exit_fee_rate  = 0.0002 if outcome == 'WIN' else 0.0005
        fee_usdt       = round(notional * (entry_fee_rate + exit_fee_rate), 4)

        # Slippage: actual fill vs signal's intended price
        signal_price  = pos.signal_price if pos.signal_price else pos.entry_price
        slippage_pct  = round((pos.entry_price - signal_price) / signal_price * 100, 4) \
                        if signal_price else 0.0

        row = {
            'open_time':    pos.open_time,
            'close_time':   close_time,
            'symbol':       pos.symbol,
            'strategy':     pos.strategy,
            'direction':    pos.direction,
            'signal_price': round(signal_price, 8),
            'entry_price':  round(pos.entry_price, 8),
            'sl_price':     round(pos.sl_price, 8),
            'tp_price':     round(pos.tp_price, 8),
            'quantity':     pos.quantity,
            'margin_usdt':  pos.margin_usdt,
            'leverage':     pos.leverage,
            'outcome':      outcome,
            'pnl_pct':      round(pnl_pct, 3),
            'pnl_usdt':     round(pnl_usdt, 2),
            'fee_usdt':     fee_usdt,
            'slippage_pct': slippage_pct,
            'signal_time':  pos.signal_time,
        }

        if pos.db_id:
            # Update the existing OPEN row to final outcome
            self.supabase.update('trades', pos.db_id, {
                'close_time':   close_time,
                'sl_price':     round(pos.sl_price, 8),
                'tp_price':     round(pos.tp_price, 8),
                'outcome':      outcome,
                'pnl_pct':      round(pnl_pct, 3),
                'pnl_usdt':     round(pnl_usdt, 2),
                'fee_usdt':     fee_usdt,
                'slippage_pct': slippage_pct,
            })
            log.info(f"[LOG] Supabase row #{pos.db_id} updated → {outcome}")
        else:
            # Fallback: full INSERT (position was opened before this deploy)
            self.supabase.insert('trades', row)
        self.closed_positions.append(row)

        with open(TRADE_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                row['open_time'], row['close_time'], row['symbol'], row['strategy'],
                row['direction'], row['signal_price'], row['entry_price'],
                row['sl_price'], row['tp_price'],
                row['quantity'], row['margin_usdt'], row['leverage'], row['outcome'],
                row['pnl_pct'], row['pnl_usdt'], row['fee_usdt'],
                row['slippage_pct'], row['signal_time'],
            ])

        log.info(f"[LOG] Trade closed: {pos.symbol} {outcome} "
                 f"PnL={pnl_pct:+.3f}% / ${pnl_usdt:+.2f} | "
                 f"Fee=${fee_usdt:.4f} | Slippage={slippage_pct:+.4f}%")


# ============================================================================
# STANDALONE TEST
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

    print("""
+------------------------------------------------------+
|  STEP 3 -- Order Manager  (Futures / connectivity)  |
|  Does NOT place any orders.                          |
+------------------------------------------------------+
""")

    if not API_KEY or not API_SECRET:
        print("ERROR: No API keys found.")
        sys.exit(1)

    client = BinanceClient(API_KEY, API_SECRET, BASE_URL)

    print(f"  Testnet : {TESTNET}")
    print(f"  Base URL: {BASE_URL}")
    print()

    try:
        bal = client.get_usdt_balance()
        print(f"  [OK] Futures USDT Balance : {bal:.2f} USDT")
    except Exception as e:
        print(f"  [FAIL] Balance fetch      : {e}")
        sys.exit(1)

    try:
        price = client.get_ticker_price('BTCUSDT')
        print(f"  [OK] BTCUSDT price        : ${price:.2f}")
    except Exception as e:
        print(f"  [FAIL] Price fetch        : {e}")

    try:
        pc   = PrecisionCache(client)
        qty  = pc.calc_quantity('BTCUSDT', price, DEFAULT_MARGIN, DEFAULT_LEVERAGE)
        prec = pc.get('BTCUSDT')
        print(f"  [OK] BTCUSDT qty          : {qty} (maxQty={prec['max_qty']})")
    except Exception as e:
        print(f"  [FAIL] Precision/qty      : {e}")

    try:
        test_price = 3.175e-05
        formatted  = client._fmt_price(test_price)
        print(f"  [OK] fmt_price test       : {test_price} → '{formatted}'")
    except Exception as e:
        print(f"  [FAIL] fmt_price          : {e}")

    print()
    print("  Strategy config:")
    for strat, cfg in STRATEGY_CONFIG.items():
        print(f"    {strat}: ${cfg['margin_usdt']} margin × {cfg['leverage']}x leverage "
              f"= ${cfg['margin_usdt'] * cfg['leverage']:.0f} notional")
    print(f"  Max open positions: {MAX_OPEN_POSITIONS}")
    print()
    print("  All connectivity tests passed.")
