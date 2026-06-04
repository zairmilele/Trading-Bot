"""
MAIN — Full Bot Entry Point  (Futures Edition)
================================================
"""
import sys, io, logging, os, time, hmac, hashlib, requests, csv
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template

load_dotenv()

print("TESTNET:", os.environ.get("TESTNET"))
print("BINANCE_API_KEY loaded:", bool(os.environ.get("BINANCE_API_KEY")))
print("BINANCE_API_SECRET loaded:", bool(os.environ.get("BINANCE_API_SECRET")))

# ── Flask proxy app ──────────────────────────────────────────────────────────
app = Flask(__name__)

TESTNET = os.environ.get('TESTNET', 'true').lower() == 'true'
if TESTNET:
    BINANCE_BASE = 'https://demo-fapi.binance.com/fapi'
else:
    BINANCE_BASE = 'https://fapi.binance.com/fapi'

API_KEY    = os.environ.get('BINANCE_API_KEY', '')
API_SECRET = os.environ.get('BINANCE_API_SECRET', '')


def _sign(params: dict) -> dict:
    params['timestamp'] = int(time.time() * 1000)
    qs  = '&'.join(f'{k}={v}' for k, v in params.items())
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    params['signature'] = sig
    return params


def binance_signed(path, params={}):
    p = _sign(dict(params))
    qs  = '&'.join(f'{k}={v}' for k, v in p.items())
    url = f'{BINANCE_BASE}{path}?{qs}'
    return requests.get(url, headers={'X-MBX-APIKEY': API_KEY}, timeout=10).json()




@app.route('/')
def dashboard():
    return render_template('index.html')

# ── Futures proxy routes ─────────────────────────────────────────────────────

@app.route('/proxy/fapi/v2/account')
def proxy_fapi_account():
    return jsonify(binance_signed('/v2/account'))


@app.route('/proxy/fapi/v1/openOrders')
def proxy_fapi_open_orders():
    sym = request.args.get('symbol', '')
    params = {'symbol': sym} if sym else {}
    return jsonify(binance_signed('/v1/openOrders', params))


@app.route('/proxy/fapi/v1/allOrders')
def proxy_fapi_all_orders():
    sym = request.args.get('symbol', '')
    return jsonify(binance_signed('/v1/allOrders', {'symbol': sym, 'limit': 500}))


@app.route('/proxy/fapi/v1/ticker/price')
def proxy_fapi_ticker_price():
    sym = request.args.get('symbol', '')
    url = f'{BINANCE_BASE}/v1/ticker/price?symbol={sym}'
    return jsonify(requests.get(url, timeout=10).json())


@app.route('/proxy/fapi/v2/positionRisk')
def proxy_fapi_position_risk():
    sym = request.args.get('symbol', '')
    params = {'symbol': sym} if sym else {}
    return jsonify(binance_signed('/v2/positionRisk', params))


# ── Local history helpers ──────────────────────────────────────────────────────

def load_trade_log_csv(path='trade_log.csv'):
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8', newline='') as f:
            rows = list(csv.DictReader(f))
        return rows
    except Exception as e:
        log = logging.getLogger('main')
        log.warning(f"Could not read {path}: {e}")
        return []


# ── Bot-internal data routes ─────────────────────────────────────────────────

@app.route('/proxy/trades')
def proxy_trades():
    # Priority: Supabase -> in-memory session history -> local CSV history
    if manager:
        rows = manager.supabase.select_all('trades')
        if rows:
            return jsonify(rows)

    if manager and manager.closed_positions:
        return jsonify(manager.closed_positions)

    csv_rows = load_trade_log_csv()
    if csv_rows:
        return jsonify(csv_rows)

    return jsonify([])

@app.route('/proxy/open_positions')
def proxy_open_positions():
    """Return live open positions with duration info from the in-memory tracker."""
    if manager:
        return jsonify(manager.get_open_positions_list())
    return jsonify([])


@app.route('/proxy/stats')
def proxy_stats():
    """Return strategy-level stats: consecutive losses, open count."""
    if manager:
        return jsonify(manager.get_stats())
    return jsonify({'consec_losses': {'S2': 0, 'AXISPRO': 0, 'BREAKOUT': 0, 'ICT': 0},
                    'open_count': 0})


@app.after_request
def cors(r):
    r.headers['Access-Control-Allow-Origin'] = '*'
    return r


# ── Windows UTF-8 fix ────────────────────────────────────────────────────────
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level    = logging.INFO,
    format   = '%(asctime)s  %(levelname)-7s  %(name)-16s  %(message)s',
    datefmt  = '%Y-%m-%d %H:%M:%S',
    handlers = [
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('main')
logging.getLogger('werkzeug').setLevel(logging.ERROR)

from step1_candle_engine   import CandleEngine, SYMBOLS, TESTNET
from step2_signal_detector import SignalEvent
from step3_order_manager   import OrderManager
from step4_telegram        import AlertManager
from strategy_registry     import StrategyDispatcher

dispatcher = engine = manager = alerts = None


class _CloseAdapter:
    """Bridges the order manager's on_trade_closed(symbol, strategy, outcome)
    call to the dispatcher's on_trade_closed(strategy, symbol, outcome)."""
    def __init__(self, disp):
        self._disp = disp

    def on_trade_closed(self, symbol, strategy, outcome):
        self._disp.on_trade_closed(strategy, symbol, outcome)


def fetch_valid_symbols():
    """
    Pull the set of tradable symbols from the futures exchangeInfo so the
    dispatcher can drop any coin the venue doesn't list (several ICT tokenized-
    stock-style perps may not exist on Binance Futures). Returns None on failure
    (meaning: don't filter — let the engine try them and skip on order error).
    """
    try:
        url = f'{BINANCE_BASE}/v1/exchangeInfo'
        data = requests.get(url, timeout=20).json()
        syms = {s['symbol'] for s in data.get('symbols', [])
                if s.get('status', 'TRADING') == 'TRADING'}
        log.info(f"exchangeInfo: {len(syms)} tradable futures symbols")
        return syms or None
    except Exception as e:
        log.warning(f"Could not fetch exchangeInfo ({e}); skipping symbol validation")
        return None


def candle_callback(symbol, candle, candles_15m, candles_1h):
    # 1) feed strategy detectors (entry signals)
    dispatcher.on_15m_close(symbol, candle, candles_15m, candles_1h)
    # 2) drive per-bar exit management for any open positions on this symbol
    if manager:
        manager.on_bar_close(symbol, candle, candles_15m, candles_1h)


def on_signal_with_alert(signal):
    if alerts:  alerts.on_signal(signal)
    if manager: manager.on_signal(signal)


def main():
    global dispatcher, engine, manager, alerts

    # Validate symbols against the venue, then build the four-strategy dispatcher
    valid = fetch_valid_symbols()
    dispatcher = StrategyDispatcher(valid_symbols=valid)
    symbols_15m = dispatcher.symbols_15m
    symbols_1h  = dispatcher.symbols_1h

    print(f"""
+------------------------------------------------------------+
|  TRADING BOT  --  Futures Edition (multi-strategy)         |
|  Strategies : S2 (FVG) + AxisPro + Breakout + ICT Gap      |
|  15m coins  : {len(symbols_15m):<3} unique   1h bias coins : {len(symbols_1h):<3}            |
|  {dispatcher.summary():<56}|
|  Mode       : {'TESTNET (paper money)' if TESTNET else 'LIVE TRADING'}                          |
|  Started    : {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}                     |
+------------------------------------------------------------+
""")

    alerts = AlertManager()

    try:
        manager = OrderManager(detector=_CloseAdapter(dispatcher), alerts=alerts)
    except ValueError as e:
        log.error(str(e)); sys.exit(1)

    _orig_open = manager._log_trade_open
    def _patched_open(pos):
        _orig_open(pos)
        if alerts:
            alerts.on_trade_opened(pos.symbol, pos.strategy, pos.direction,
                                   pos.entry_price, pos.sl_price, pos.tp_price, pos.quantity)
    manager._log_trade_open = _patched_open

    _orig_close = manager._log_trade_close
    def _patched_close(pos, outcome, exit_price=None):
        _orig_close(pos, outcome, exit_price=exit_price)
        if alerts:
            alert_exit = exit_price or (pos.tp_price if outcome == 'WIN' else pos.sl_price)
            alerts.on_trade_closed(pos.symbol, pos.strategy, pos.direction,
                                   pos.entry_price, alert_exit, outcome)
    manager._log_trade_close = _patched_close

    # all four strategies emit through the same alert+order handler
    dispatcher.set_signal_handler(on_signal_with_alert)

    engine = CandleEngine(symbols_15m, callback=candle_callback, symbols_1h=symbols_1h)
    alerts.send_startup(len(symbols_15m), TESTNET)

    log.info(f"All components ready. {dispatcher.summary()}. Starting candle engine...")

    try:
        engine.start()
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
        if alerts: alerts.on_error("Bot stopped by user (KeyboardInterrupt)")


# ── Bot bootstrap ────────────────────────────────────────────────────────────
# IMPORTANT: starting the bot must happen at MODULE LEVEL so it works under both:
#   1) `python main.py`           (local / VPS direct run)
#   2) `gunicorn main:app`        (Railway / Heroku / any WSGI host)
#
# If the bot were only started inside `if __name__ == '__main__':`, gunicorn
# would import this module, serve Flask, and never run the trading thread —
# the dashboard would load but no signals would fire and no trades would open.
# That was the bug: on Railway the bot ran the Flask routes only.
import threading

_bot_thread_lock  = threading.Lock()
_bot_thread       = None

def _start_bot_once():
    """Start the trading thread exactly once per process."""
    global _bot_thread
    # Skip the Werkzeug auto-reloader's parent process so we don't start twice in dev
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'false':
        return
    with _bot_thread_lock:
        if _bot_thread is not None and _bot_thread.is_alive():
            return
        log.info("Starting bot trading thread...")
        _bot_thread = threading.Thread(target=main, daemon=True, name='bot_main')
        _bot_thread.start()

# Start the bot when the module is imported (covers gunicorn) and also when
# run directly. The lock above guarantees a single start per process — if
# gunicorn is configured with multiple workers, each worker will start its
# own bot, which would cause duplicate trades. Keep gunicorn at --workers 1
# (see Procfile) for that reason.
_start_bot_once()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)),
            use_reloader=False)  # reloader would fork and double-start the bot
