#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PumpHunter v4.2 - Enhanced from v4.1
# - realistic % targets (0.5% -> 6%) + fixed 5% stop
# - TP follow-up notifications + no stop message when any TP was hit
# - extra filters: EMA crossover, RSI, simple support/resistance
# - multi-threaded scanning, minimal changes to your original structure

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
DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"  # safe default: DRY_RUN ON
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
VOL_MULT_STRONG = float(os.environ.get("VOL_MULT_STRONG", 1.6))
VOL_MULT_WEAK = float(os.environ.get("VOL_MULT_WEAK", 1.15))
PRICE_ACCEL_THRESHOLD = float(os.environ.get("PRICE_ACCEL_THRESHOLD", 0.005))
SCORE_SIGNAL = int(os.environ.get("SCORE_SIGNAL", 75))
SCORE_ALERT = int(os.environ.get("SCORE_ALERT", 74))
PRE_SIGNAL = int(os.environ.get("PRE_SIGNAL", 73))
MAX_ALERTS_PER_SYMBOL_PER_DAY = int(os.environ.get("MAX_ALERTS_PER_SYMBOL_PER_DAY", 1))
MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", 6))

PRICE_DECIMALS = int(os.environ.get("PRICE_DECIMALS", 6))
STATE_FILE = os.environ.get("STATE_FILE", "pumphunter_state_v4.json")

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

# ---------------- State ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            _log_exception_short("Failed to load state file, starting fresh", e)
    return {"alerts": {}, "active_trades": [], "history": [], "cache": {}, "daily_trades": {}, "watchlist": {}}

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

# CoinGecko fallback (small sample)
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
        ob_imb, taker_ratio, big_taker_vol = pulse_vals
        if abs(ob_imb) > 0.25:
            score += 12; reasons.append('ob_imb')
        if abs(taker_ratio) > 0.25:
            score += 12; reasons.append('taker')
        if big_taker_vol > 0:
            score += min(8, int(big_taker_vol*100)); reasons.append('big_taker')
    score = max(0, min(100, int(score)))
    return score, reasons

# ---------- Watchlist pre-signal flow ----------
PRE_SIGNAL_DURATION = int(os.environ.get('PRE_SIGNAL_DURATION', 12))

def mark_watchlist(sym, score, features, reasons):
    w = state.setdefault('watchlist', {})
    rec = {'first_seen': nowts(), 'score': score, 'features': features, 'reasons': reasons}
    w[sym] = rec
    save_state(state)

def check_watchlist_confirm(sym, newscore, features, reasons):
    w = state.get('watchlist', {})
    rec = w.get(sym)
    if not rec:
        return False
    if nowts() - rec['first_seen'] > PRE_SIGNAL_DURATION:
        state['watchlist'].pop(sym, None)
        save_state(state)
        return False
    if newscore >= SCORE_SIGNAL or newscore >= rec['score'] + 10:
        state['watchlist'].pop(sym, None)
        save_state(state)
        return True
    return False

# ---------- Indicator filter (EMA, RSI, simple SR) ----------
def indicator_filter(df, features):
    """Return True if indicators favour a trade (reduce false positives).
    Rules (conservative):
      - Long: EMA5 > EMA20, RSI < 75, price not far below recent low
      - Short: EMA5 < EMA20, RSI > 25, price not far above recent high
    """
    try:
        closes = df['close'].astype(float)
        ema5 = closes.ewm(span=5, adjust=False).mean().iloc[-1]
        ema20 = closes.ewm(span=20, adjust=False).mean().iloc[-1]
        rsi = None
        try:
            rsi = float(ta.momentum.rsi(closes, window=14).iloc[-1])
        except Exception:
            rsi = features.get('rsi')
        last = float(closes.iloc[-1])
        # simple support/resistance from recent window
        lookback = min(len(df), 60)
        recent_high = float(df['high'].tail(lookback).max())
        recent_low = float(df['low'].tail(lookback).min())
        buffer = 0.01  # 1% buffer
        if features.get('price_accel',0) >= 0:  # long bias
            if not (ema5 > ema20):
                return False
            if rsi is not None and rsi > 75:
                return False
            if last < recent_low * (1 - buffer):
                return False
            return True
        else:  # short bias
            if not (ema5 < ema20):
                return False
            if rsi is not None and rsi < 25:
                return False
            if last > recent_high * (1 + buffer):
                return False
            return True
    except Exception as e:
        if DEBUG: _log_exception_short("indicator_filter", e)
        return True  # fail-open: if indicators fail, allow signal

# ---------- Leverage & targets (percent-based realistic targets) ----------
def get_leverage_for_score(score):
    if score >= 95: return 50
    if score >= 85: return 25
    if score >= 75: return 20
    if score >= 65: return 10
    if score >= 50: return 5
    return None

# realistic percent steps
TARGET_STEPS = [0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.06]  # 0.5% -> 6%

def compose_targets_and_stop_percent(entry, side='LONG'):
    tps = []
    for s in TARGET_STEPS:
        tp = entry * (1 + s) if side == 'LONG' else entry * (1 - s)
        tps.append(round(tp, PRICE_DECIMALS))
    stop = round(entry * 0.95, PRICE_DECIMALS) if side == 'LONG' else round(entry * 1.05, PRICE_DECIMALS)
    return tps, stop

# ---------- Publish + state helpers ----------
def can_publish(sym):
    now = nowts()
    pub = state.get('alerts', {})
    daily = pub.get(sym, {"count": 0, "last": 0})
    if now - daily.get("last", 0) > 24 * 3600:
        return True
    if daily.get("count", 0) < MAX_ALERTS_PER_SYMBOL_PER_DAY:
        return True
    return False

def register_publish(sym):
    pub = state.setdefault('alerts', {})
    daily = pub.get(sym, {"count": 0, "last": 0})
    if nowts() - daily.get("last", 0) > 24*3600:
        daily = {"count": 0, "last": 0}
    daily['count'] = daily.get('count', 0) + 1
    daily['last'] = nowts()
    pub[sym] = daily
    save_state(state)

CIRCLED = ['①','②','③','④','⑤','⑥','⑦']

def publish_trade(sym, features, reasons, price, score):
    if not can_publish(sym):
        log.info("Publish blocked by rate limit: %s", sym)
        return False
    leverage = get_leverage_for_score(score)
    if leverage is None:
        log.info("No leverage allocated for score %s", score)
        return False
    side = 'LONG' if features.get('price_accel', 0) >= 0 else 'SHORT'
    # use percent-based realistic targets
    tps, stop = compose_targets_and_stop_percent(price, side=side)
    lines = []
    lines.append(f"${sym}")
    lines.append(f"{ 'Long' if side=='LONG' else 'Short' } Cross {leverage}x")
    entry_line = ("🟢Entry: " if side == 'LONG' else "🔴Entry: ") + format_price(price)
    lines.append(entry_line)
    lines.append("")
    lines.append("Targets:")
    for i,tp in enumerate(tps[:7]):
        lines.append(f"{CIRCLED[i]} {format_price(tp)}")
    lines.append("")
    lines.append("⛔Stop: " + format_price(stop))
    msg = "\n".join(lines)
    ok = tg_send(msg)
    if ok:
        register_publish(sym)
        active = {"symbol": sym, "entry": price, "side": side, "tps": tps, "stop": stop, "hit": [False]*len(tps),
                  "opened_at": nowts(), "leverage": leverage, "tp_hit_any": False}
        state.setdefault('active_trades', []).append(active)
        save_state(state)
        return True
    return False

def publish_alert(sym, features, reasons, level="ALERT"):
    if not can_publish(sym): return False
    lines = [
        ("⚡ SIGNAL" if level == "SIGNAL" else "⚠️ ALERT") + f" {sym}",
        f"Score: {features.get('score','?')}",
        f"Price: {format_price(features.get('last'))}",
        "Reasons: " + (", ".join(reasons) if reasons else "n/a"),
        nowstr()
    ]
    msg = "\n".join(lines)
    ok = tg_send(msg)
    if ok:
        register_publish(sym)
    return ok

# ---------- Active trades monitor (follow TPs, send notif) ----------
def monitor_active_trades():
    changed = False
    active = state.get('active_trades', [])
    if not active:
        return
    for trade in list(active):
        sym = trade.get('symbol')
        try:
            price = current_price_best(sym)
            if price is None:
                df = fetch_klines_best(sym)
                if df is None:
                    continue
                price = float(df['close'].iloc[-1])
            side = trade.get('side')
            entry = trade.get('entry')
            tps = trade.get('tps', [])
            stop = trade.get('stop')
            hit = trade.get('hit', [False]*len(tps))

            # check TPs
            for idx, tp in enumerate(tps):
                if not hit[idx]:
                    if (side == 'LONG' and price >= tp) or (side == 'SHORT' and price <= tp):
                        hit[idx] = True
                        trade['hit'] = hit
                        trade['tp_hit_any'] = True
                        circ = CIRCLED[idx] if idx < len(CIRCLED) else f"{idx+1}"
                        tg_send(f"${sym}\n🎯 Target {circ} hit → {format_price(tp)}")
                        changed = True

            # STOP: send STOP alert ONLY if no TP was hit yet for this trade
            if (side == 'LONG' and price <= stop) or (side == 'SHORT' and price >= stop):
                if not trade.get('tp_hit_any', False):
                    tg_send(f"${sym}\n⛔ Stop Loss hit → {format_price(stop)}")
                # record and remove
                state['active_trades'].remove(trade)
                rec = {"symbol": sym, "side": side, "entry": entry, "exit": price, "reason": "STOP",
                       "opened_at": trade.get('opened_at'), "closed_at": nowts(), "hit": hit}
                state.setdefault('history', []).append(rec)
                changed = True
                continue

            # all hit
            if all(hit):
                tg_send(f"${sym}\n🏁 ALL TPs HIT\nEntry: {format_price(entry)}\nFinal: {format_price(price)}\n{nowstr()}")
                state['active_trades'].remove(trade)
                rec = {"symbol": sym, "side": side, "entry": entry, "exit": price, "reason": "ALL_TP",
                       "opened_at": trade.get('opened_at'), "closed_at": nowts(), "hit": hit}
                state.setdefault('history', []).append(rec)
                changed = True
        except Exception as e:
            _log_exception_short(f"monitor error for {sym}", e)
            continue
    if changed:
        save_state(state)

# ---------- Scan single symbol ----------
def scan_symbol(sym):
    try:
        df = fetch_klines_best(sym)
        if df is None or len(df) < MIN_CANDLES:
            log.debug("%s: no klines (skip)", sym)
            return None
        features = compute_pump_features(df)
        ob = fetch_orderbook(sym, limit=10)
        trades = fetch_recent_trades(sym, limit=100)
        pulse_vals = compute_pulse(ob, trades)
        score, reasons = compute_pump_score(features, pulse_vals)
        features['score'] = score
        # indicator filter
        features_ok = indicator_filter(df, features)
        return {'symbol': sym, 'features': features, 'score': score, 'reasons': reasons, 'pulse': pulse_vals, 'last': features['last'], 'features_ok': features_ok}
    except Exception as e:
        _log_exception_short(f"scan_symbol error for {sym}", e)
        return None

# ---------- Main Loop (parallel) ----------
def main_loop():
    log.info("PumpHunter v4.2 starting. DRY_RUN=%s DEBUG=%s", DRY_RUN, DEBUG)
    cycle = 0
    executor = ThreadPoolExecutor(max_workers=THREADS)
    try:
        while True:
            cycle += 1
            start = nowts()
            log.info('--- Cycle %s | %s ---', cycle, nowstr())

            futures = {executor.submit(scan_symbol, s): s for s in SYMBOLS}
            results = []
            for fut in as_completed(futures, timeout=25):
                try:
                    r = fut.result()
                except Exception as e:
                    _log_exception_short("future error", e)
                    r = None
                if r:
                    results.append(r)

            results_sorted = sorted(results, key=lambda x: x['score'], reverse=True)
            for r in results_sorted:
                sym = r['symbol']; score = r['score']; features = r['features']; reasons = r['reasons']; last = r['last']; pulse = r['pulse']; ok_ind = r.get('features_ok', True)
                log.info("%s | score=%s | vol_mult=%.2f | accel=%.6f | rsi=%s | last=%s | pulse=%s | ind_ok=%s",
                         sym, score, features['vol_mult'], features['price_accel'],
                         ("%.2f" % features['rsi']) if features['rsi'] is not None else "n/a",
                         format_price(last), (round(pulse[0], 3), round(pulse[1], 3), round(pulse[2], 3)), ok_ind)

                if not ok_ind:
                    # skip signals failing indicator filter
                    continue

                if score >= SCORE_SIGNAL and can_publish(sym):
                    published = publish_trade(sym, features, reasons, price=last, score=score)
                    if published:
                        log.info("Published TRADE for %s (score=%s reasons=%s)", sym, score, reasons)
                elif score >= PRE_SIGNAL and can_publish(sym):
                    if check_watchlist_confirm(sym, score, features, reasons):
                        published = publish_trade(sym, features, reasons, price=last, score=score)
                        if published: log.info('Published CONFIRMED pre-signal TRADE for %s', sym)
                    else:
                        mark_watchlist(sym, score, features, reasons)
                        publish_alert(sym, features, reasons, level='ALERT')
                elif score >= SCORE_ALERT and can_publish(sym):
                    publish_alert(sym, features, reasons, level='ALERT')

                time.sleep(0.04)  # small throttle between publishes

            monitor_active_trades()
            elapsed = nowts() - start
            to_sleep = max(1, POLL_SECONDS - elapsed)
            log.info("Cycle complete. Sleeping %s s", to_sleep)
            time.sleep(to_sleep)
    except KeyboardInterrupt:
        log.info("Stopped by user. Saving state.")
        save_state(state)
    except Exception as e:
        _log_exception_short("Fatal error in main loop", e)
        save_state(state)
        raise

if __name__ == "__main__":
    main_loop()
