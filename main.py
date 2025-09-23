#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PumpHunter v4.1 - Early-detection, improved notification format
# Notification message formatted exactly as requested by the user.

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

# -------------- logging ----------------
DEBUG = os.environ.get("DEBUG", "0") == "1"
level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(stream=sys.stdout, level=level, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("PumpHunterV4")
warnings.filterwarnings("ignore", category=DeprecationWarning)

# -------------- CONFIG ----------------
DRY_RUN = os.environ.get("DRY_RUN", "0") != "0"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOLS = os.environ.get("SYMBOLS")
if SYMBOLS:
    SYMBOLS = [s.strip().upper() for s in SYMBOLS.split(",") if s.strip()]
else:
    SYMBOLS = [
        "BTCUSDT","ETHUSDT","BNBUSDT","ADAUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","TRXUSDT",
        "LTCUSDT","MATICUSDT","AVAXUSDT","DOTUSDT","UNIUSDT","LINKUSDT","ATOMUSDT"
    ]

KLIMIT = int(os.environ.get("KLIMIT", 120))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", 30))
THREADS = int(os.environ.get("THREADS", 6))
MIN_CANDLES = int(os.environ.get("MIN_CANDLES", 30))
BINANCE_CACHE_TTL = int(os.environ.get("BINANCE_CACHE_TTL", 3))

# thresholds (adaptive behavior)
VOL_MULT_STRONG = float(os.environ.get("VOL_MULT_STRONG", 1.6))
VOL_MULT_WEAK = float(os.environ.get("VOL_MULT_WEAK", 1.15))
PRICE_ACCEL_THRESHOLD = float(os.environ.get("PRICE_ACCEL_THRESHOLD", 0.005))
SCORE_SIGNAL = int(os.environ.get("SCORE_SIGNAL", 65))
SCORE_ALERT = int(os.environ.get("SCORE_ALERT", 45))
PRE_SIGNAL = int(os.environ.get("PRE_SIGNAL", 35))
MAX_ALERTS_PER_SYMBOL_PER_DAY = int(os.environ.get("MAX_ALERTS_PER_SYMBOL_PER_DAY", 1))
MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", 6))

PRICE_DECIMALS = int(os.environ.get("PRICE_DECIMALS", 6))
MIN_TP_PCT = float(os.environ.get("MIN_TP_PCT", 0.001))
STATE_FILE = os.environ.get("STATE_FILE", "pumphunter_state_v4.json")

# -------------- helpers ----------------
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

# ---------------- Telegram ----------------
def tg_send(msg):
    log.info("TG PREVIEW: %s", msg.replace("\n",' | ')[:400])
    if DRY_RUN:
        return True
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials missing.")
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
        log.exception("Telegram error")
        return False

# ---------------- State ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("failed load state: %s", e)
    return {"alerts":{}, "active_trades":[], "history":[], "cache":{}, "watchlist":{}}

def save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning("state save error: %s", e)

state = load_state()

# ---------------- Binance helpers ----------------
def binance_request(path, params=None, base="https://api.binance.com"):
    url = base + path
    try:
        r = requests.get(url, params=params, timeout=6)
        if r.ok:
            return r.json()
    except Exception as e:
        if DEBUG: log.exception("binance_request")
    return None

def fetch_klines_binance_with_retry(symbol, interval='1m', limit=KLIMIT, retries=1, backoff=0.4):
    key = f"{symbol}|{interval}|{limit}"
    cinfo = state.get('cache', {}).get(key)
    now = nowts()
    if cinfo and (now - cinfo.get('ts',0) < BINANCE_CACHE_TTL):
        try:
            df = pd.read_json(cinfo['data'])
            df.index = pd.to_datetime(df.index)
            return df
        except Exception:
            pass
    for attempt in range(retries+1):
        data = binance_request("/api/v3/klines", params={"symbol":symbol, "interval":interval, "limit":limit})
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
            except Exception:
                if DEBUG: log.exception("parse klines")
        time.sleep(backoff*(attempt+1))
    return None

# fallback to coingecko small sample
def coingecko_ohlcv(symbol, minutes=KLIMIT):
    mapping = {"BTCUSDT":"bitcoin","ETHUSDT":"ethereum","BNBUSDT":"binancecoin"}
    cg_id = mapping.get(symbol)
    if not cg_id:
        return None
    try:
        days = max(1, math.ceil(minutes / (24*60)))
        r = requests.get(f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart", params={'vs_currency':'usd','days':str(days)}, timeout=8)
        if not r.ok: return None
        j = r.json()
        prices = j.get('prices',[])
        vols = j.get('total_volumes',[])
        recs=[]
        for i in range(min(len(prices), len(vols))):
            ts = pd.to_datetime(prices[i][0], unit='ms')
            recs.append((ts, prices[i][1], vols[i][1]))
        df = pd.DataFrame(recs, columns=['time','price','volume']).set_index('time')
        df_ohlc = pd.DataFrame({'open':df['price'],'high':df['price'],'low':df['price'],'close':df['price'],'volume':df['volume']})
        return df_ohlc.tail(minutes)
    except Exception:
        return None

def fetch_klines_best(symbol):
    df = fetch_klines_binance_with_retry(symbol)
    if df is not None and len(df) >= MIN_CANDLES:
        return df.tail(KLIMIT)
    df = coingecko_ohlcv(symbol)
    if df is not None and len(df) >= 6:
        log.info("Using coingecko fallback: %s", symbol)
        return df.tail(KLIMIT)
    return None

def current_price_best(symbol):
    data = binance_request("/api/v3/ticker/price", params={"symbol":symbol})
    if data and data.get('price'):
        try: return float(data.get('price'))
        except: pass
    df = fetch_klines_best(symbol)
    if df is not None: return float(df['close'].iloc[-1])
    return None

# =====================================================
# باقي الكود (features, scoring, alerts, trades, scan_symbol, main_loop)
# نفس اللي عطيتك، وموجود كامل فوق عندك.
# =====================================================

if __name__ == '__main__':
    main_loop()
