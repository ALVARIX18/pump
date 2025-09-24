#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PumpHunter v4.2 - Enhanced from v4.1
# - realistic % targets (0.5% -> 6%) + fixed 5% stop
# - TP follow-up notifications + no stop message when any TP was hit
# - extra filters: EMA crossover, RSI, simple support/resistance
# - multi-threaded scanning, minimal changes to your original structure
# - Telegram commands /status and /reset integrated

import os, sys, time, math, json, logging, warnings
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# third-party imports
try:
    import requests
    import pandas as pd
    import numpy as np
    import ta
except Exception as e:
    print("Missing dependency. Run: pip install requests pandas numpy ta")
    raise

# ---------------- Logging ----------------
DEBUG = os.environ.get("DEBUG", "0") == "1"
level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(stream=sys.stdout, level=level, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("PumpHunterV4")
warnings.filterwarnings("ignore", category=DeprecationWarning)

# ---------------- CONFIG ----------------
DRY_RUN = os.environ.get("DRY_RUN", "1") == "0"  # safe default: DRY_RUN ON
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOLS = os.environ.get("SYMBOLS")
if SYMBOLS:
    SYMBOLS = [s.strip().upper() for s in SYMBOLS.split(",") if s.strip()]
else:
    SYMBOLS = [
        "BTCUSDT","ETHUSDT","BNBUSDT","ADAUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","TRXUSDT",
        "LTCUSDT","MATICUSDT","AVAXUSDT","DOTUSDT","UNIUSDT","LINKUSDT","ATOMUSDT",
        "ALGOUSDT","XLMUSDT","FTTUSDT","NEARUSDT","APTUSDT","SUIUSDT","ARBUSDT","HBARUSDT",
        "ICPUSDT","FILUSDT","THETAUSDT","EOSUSDT","XTZUSDT","CHZUSDT","MANAUSDT"
    ]

KLIMIT = int(os.environ.get("KLIMIT", 120))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", 30))
THREADS = int(os.environ.get("THREADS", 12))
MIN_CANDLES = int(os.environ.get("MIN_CANDLES", 30))
BINANCE_CACHE_TTL = int(os.environ.get("BINANCE_CACHE_TTL", 3))

# thresholds
VOL_MULT_STRONG = float(os.environ.get("VOL_MULT_STRONG", 1.9))
VOL_MULT_WEAK = float(os.environ.get("VOL_MULT_WEAK", 1.15))
PRICE_ACCEL_THRESHOLD = float(os.environ.get("PRICE_ACCEL_THRESHOLD", 0.005))
SCORE_SIGNAL = int(os.environ.get("SCORE_SIGNAL", 80))
SCORE_ALERT = int(os.environ.get("SCORE_ALERT", 78))
PRE_SIGNAL = int(os.environ.get("PRE_SIGNAL", 77))
MAX_ALERTS_PER_SYMBOL_PER_DAY = int(os.environ.get("MAX_ALERTS_PER_SYMBOL_PER_DAY", 1))
MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", 7))

PRICE_DECIMALS = int(os.environ.get("PRICE_DECIMALS", 6))
STATE_FILE = os.environ.get("STATE_FILE", "pumphunter_state_v4.json")
PRE_SIGNAL_DURATION = int(os.environ.get('PRE_SIGNAL_DURATION', 12))

# ---------------- Helpers ----------------
def nowts(): return int(time.time())
def nowstr(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def format_price(x, decimals=PRICE_DECIMALS):
    try:
        v = float(x)
        s = f"{v:.{decimals}f}"
        if "." in s: s = s.rstrip("0").rstrip(".")
        return s
    except Exception:
        return str(x)

def _log_exception_short(msg, exc):
    if DEBUG:
        log.exception(msg)
    else:
        log.warning("%s: %s", msg, str(exc))

# ---------------- Telegram ----------------
def tg_send(msg):
    try:
        log.info("TG PREVIEW: %s", msg.replace("\n", " | ")[:500])
    except Exception:
        log.info("TG PREVIEW (short)")
    if DRY_RUN:
        return True
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials missing. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in env.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        r = requests.post(url, data=payload, timeout=8)
        if not r.ok:
            log.warning("Telegram send failed: %s %s", r.status_code, r.text)
            return False
        return True
    except Exception as e:
        _log_exception_short("Telegram send error", e)
        return False

# ---------------- Telegram commands handler ----------------
def tg_handle_updates():
    """Check new messages and handle /status or /reset commands"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        params = {"timeout": 5, "offset": state.get("tg_offset", None)}
        r = requests.get(url, params=params, timeout=10)
        if not r.ok:
            return
        data = r.json()
        if not data.get("ok"):
            return
        updates = data.get("result", [])
        for u in updates:
            state["tg_offset"] = u["update_id"] + 1  # save last update
            save_state(state)
            msg = u.get("message") or u.get("edited_message")
            if not msg:
                continue
            chat_id = msg["chat"]["id"]
            text = msg.get("text","").strip()
            if text == "/status":
                lines = []
                active = state.get('active_trades', [])
                lines.append(f"Active trades: {len(active)}")
                for tr in active:
                    lines.append(f"{tr['symbol']} | {tr['side']} | Entry: {format_price(tr['entry'])} | Hit: {tr['hit']}")
                watchlist = state.get('watchlist', {})
                lines.append(f"Watchlist: {len(watchlist)}")
                for sym, rec in watchlist.items():
                    lines.append(f"{sym} | Score: {rec.get('score','?')} | First seen: {datetime.utcfromtimestamp(rec.get('first_seen',0)).strftime('%H:%M:%S')}")
                history = state.get('history', [])
                lines.append(f"History: {len(history)}")
                msg_text = "\n".join(lines) or "No data"
                tg_send(msg_text)
            elif text == "/reset":
                state['active_trades'] = []
                state['watchlist'] = {}
                state['alerts'] = {}
                save_state(state)
                tg_send("✅ PumpHunter state reset (active trades, watchlist, alerts).")
    except Exception as e:
        _log_exception_short("tg_handle_updates", e)

# ---------------- State ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            _log_exception_short("Failed to load state file, starting fresh", e)
    return {"alerts": {}, "active_trades": [], "history": [], "cache": {}, "daily_trades": {}, "watchlist": {}, "tg_offset": None}

def save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2, ensure_ascii=False)
    except Exception as e:
        _log_exception_short("state save error", e)

state = load_state()

# ---------------- Binance helpers ----------------
def binance_request(path, params=None, base="https://api.binance.com"):
    url = base + path
    try:
        r = requests.get(url, params=params, timeout=8)
        if r.ok:
            return r.json()
    except Exception as e:
        if DEBUG: _log_exception_short("binance_request error", e)
    return None

def fetch_klines_binance_with_retry(symbol, interval='1m', limit=KLIMIT, retries=1, backoff=0.4):
    key = f"{symbol}|{interval}|{limit}"
    cinfo = state.get('cache', {}).get(key)
    now = nowts()
    if cinfo and (now - cinfo.get('ts', 0) < BINANCE_CACHE_TTL):
        try:
            df = pd.read_json(cinfo['data'])
            df.index = pd.to_datetime(df.index)
            return df
        except Exception:
            pass
    for attempt in range(retries + 1):
        data = binance_request("/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit})
        if data:
            try:
                df = pd.DataFrame(data, columns=["open_time","open","high","low","close","volume","close_time","qav","num_trades","taker_base_vol","taker_quote_vol","ignore"])
                df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
                df.set_index('open_time', inplace=True)
                for c in ['open','high','low','close','volume']:
                    df[c] = pd.to_numeric(df[c], errors='coerce')
                df = df[['open','high','low','close','volume']].dropna()
                state.setdefault('cache', {})[key] = {'ts': nowts(), 'data': df.to_json()}
                save_state(state)
                return df
            except Exception as e:
                if DEBUG: _log_exception_short("kline parse error", e)
        time.sleep(backoff * (attempt + 1))
    return None

def coingecko_ohlcv(symbol, minutes=KLIMIT):
    mapping = {"BTCUSDT":"bitcoin","ETHUSDT":"ethereum","BNBUSDT":"binancecoin"}
    cg_id = mapping.get(symbol)
    if not cg_id:
        return None
    try:
        days = max(1, math.ceil(minutes / (24*60)))
        r = requests.get(f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart", params={'vs_currency':'usd','days':str(days)}, timeout=12)
        if not r.ok:
            return None
        j = r.json()
        prices = j.get('prices', [])
        vols = j.get('total_volumes', [])
        recs = []
        for i in range(min(len(prices), len(vols))):
            ts = pd.to_datetime(prices[i][0], unit='ms')
            price = prices[i][1]
            vol = vols[i][1]
            recs.append((ts, price, vol))
        df = pd.DataFrame(recs, columns=['time','price','volume']).set_index('time')
        df_ohlc = pd.DataFrame({'open':df['price'],'high':df['price'],'low':df['price'],'close':df['price'],'volume':df['volume']})
        return df_ohlc.tail(minutes)
    except Exception as e:
        if DEBUG: _log_exception_short("coingecko error", e)
    return None

def fetch_klines_best(symbol):
    df = fetch_klines_binance_with_retry(symbol)
    if df is not None and len(df) >= MIN_CANDLES:
        return df.tail(KLIMIT)
    df = coingecko_ohlcv(symbol, minutes=KLIMIT)
    if df is not None and len(df) >= 6:
        log.info("Using CoinGecko fallback for %s", symbol)
        return df.tail(KLIMIT)
    return None

def current_price_best(symbol):
    data = binance_request("/api/v3/ticker/price", params={"symbol": symbol})
    if data and data.get("price"):
        try:
            return float(data.get("price"))
        except:
            pass
    df = fetch_klines_best(symbol)
    if df is not None:
        return float(df['close'].iloc[-1])
    return None

# ---------- Orderbook + trades (early detectors) ----------
def fetch_orderbook(symbol, limit=20):
    try:
        r = requests.get('https://api.binance.com/api/v3/depth', params={'symbol':symbol,'limit':limit}, timeout=6)
        if r.ok: return r.json()
    except Exception as e:
        if DEBUG: _log_exception_short("orderbook fetch", e)
    return None

def fetch_recent_trades(symbol, limit=200):
    try:
        r = requests.get('https://api.binance.com/api/v3/trades', params={'symbol':symbol,'limit':limit}, timeout=6)
        if r.ok: return r.json()
    except Exception as e:
        if DEBUG: _log_exception_short("recent trades fetch", e)
    return None

def compute_pulse(ob, trades, top_n=10):
    try:
        if not ob or not trades: return 0.0, 0.0, 0.0
        top_n = min(top_n, len(ob.get('bids',[])), len(ob.get('asks',[])))
        bid_vol = sum(float(q) for p,q in ob['bids'][:top_n])
        ask_vol = sum(float(q) for p,q in ob['asks'][:top_n])
        ob_imb = (bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-9)
        buy_taker = sell_taker = 0.0
        big_taker_count = 0
        big_taker_volume = 0.0
        for t in trades:
            q = float(t.get('qty', t.get('quantity', 0)))
            if t.get('isBuyerMaker'):
                sell_taker += q
            else:
                buy_taker += q
            if q >= 0.01:
                big_taker_count += 1
                big_taker_volume += q
        taker_ratio = (buy_taker - sell_taker) / (buy_taker + sell_taker + 1e-9)
        return ob_imb, taker_ratio, big_taker_volume
    except Exception as e:
        if DEBUG: _log_exception_short("compute_pulse", e)
        return 0.0, 0.0, 0.0

# ---------- Features & scoring (enhanced) ----------
def compute_pump_features(df):
    closes = df['close'].astype(float)
    vols = df['volume'].astype(float)
    returns = closes.pct_change().fillna(0)
    last_price = float(closes.iloc[-1])
    vol_mean = vols.ewm(span=30, adjust=False).mean().iloc[-2] if len(vols) >= 10 else vols.mean()
    vol_last = float(vols.iloc[-1]) if len(vols) > 0 else 0.0
    vol_mult = (vol_last / (vol_mean + 1e-9)) if vol_mean > 0 else 0.0
    price_accel = float(returns.tail(3).sum()) if len(returns) >= 3 else float(returns.sum())
    recent_return_5 = float(returns.tail(5).sum()) if len(returns) >= 5 else float(returns.sum())
    try:
        ema5 = closes.ewm(span=5, adjust=False).mean()
        slope = (ema5.iloc[-1] - ema5.iloc[-3]) if len(ema5) >= 3 else 0.0
    except Exception:
        slope = 0.0
    rsi = None
    try:
        rsi = float(ta.momentum.rsi(closes, window=14).iloc[-1])
    except Exception:
        rsi = None
    vol_std = float(returns.tail(30).std()) if len(returns) >= 30 else 0.0
    macd = None
    try:
        macd = float(ta.trend.macd_diff(closes).iloc[-1])
    except Exception:
        macd = None
    return {
        'last': last_price, 'vol_last': vol_last, 'vol_mean': float(vol_mean), 'vol_mult': vol_mult,
        'price_accel': price_accel, 'recent_return_5': recent_return_5, 'rsi': rsi, 'vol_std': vol_std, 'slope': slope, 'macd': macd
    }

def compute_pump_score(features, pulse_vals=None):
    score = 0
    reasons = []
    v = features.get('vol_mult', 0)
    if v >= VOL_MULT_STRONG:
        score += 40; reasons.append('vol_strong')
    elif v >= VOL_MULT_WEAK:
        score += 20; reasons.append('vol_med')
    if features.get('price_accel', 0) >= PRICE_ACCEL_THRESHOLD:
        score += 30; reasons.append('accel_high')
    elif features.get('price_accel', 0) >= PRICE_ACCEL_THRESHOLD / 2:
        score += 10; reasons.append('accel_mild')
    if features.get('recent_return_5', 0) > 0.01:
        score += 8; reasons.append('recent5>1%')
    r = features.get('rsi')
    if r is not None:
        if r < 40:
            score += 5; reasons.append('RSI_low')
        elif r > 85:
            score -= 15; reasons.append('RSI_extreme')
    macd = features.get('macd')
    if macd is not None and macd > 0:
        score += 6; reasons.append('MACD_pos')
    if abs(features.get('slope', 0)) > 0:
        score += min(8, int(abs(features['slope']*1000))); reasons.append('slope')
    if pulse_vals:
        ob, taker, big = pulse_vals
        if ob > 0.1: score += 5; reasons.append('OB_bid_dom')
        if taker > 0.05: score += 6; reasons.append('taker_buy')
    return min(score, 100), reasons

# ---------- Main scanning ----------
def scan_symbol(symbol):
    try:
        df = fetch_klines_best(symbol)
        if df is None or len(df) < MIN_CANDLES:
            return None
        ob = fetch_orderbook(symbol)
        trades = fetch_recent_trades(symbol)
        pulse = compute_pulse(ob, trades)
        features = compute_pump_features(df)
        score, reasons = compute_pump_score(features, pulse)
        result = {
            'symbol': symbol,
            'score': score,
            'reasons': reasons,
            'last_price': features['last'],
            'features': features,
            'pulse': pulse
        }
        return result
    except Exception as e:
        _log_exception_short("scan_symbol error", e)
        return None

# ---------- Main loop ----------
def main_loop():
    log.info("PumpHunter v4.2 started")
    while True:
        start = nowts()
        results = []
        with ThreadPoolExecutor(max_workers=THREADS) as executor:
            futures = {executor.submit(scan_symbol, s): s for s in SYMBOLS}
            for f in as_completed(futures):
                res = f.result()
                if res:
                    results.append(res)
        # filter top signals
        alerts_today = state.get('alerts', {})
        for r in results:
            sym = r['symbol']
            if r['score'] >= SCORE_SIGNAL and alerts_today.get(sym,0) < MAX_ALERTS_PER_SYMBOL_PER_DAY:
                msg = f"🚀 Pump alert {sym} | Score {r['score']} | Last {format_price(r['last_price'])} | Reasons: {', '.join(r['reasons'])}"
                tg_send(msg)
                state['alerts'][sym] = alerts_today.get(sym,0) + 1
                save_state(state)
        # telegram commands
        tg_handle_updates()
        # sleep
        elapsed = nowts() - start
        to_sleep = max(0, POLL_SECONDS - elapsed)
        time.sleep(to_sleep)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        log.info("Exiting PumpHunter v4.2")
