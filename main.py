#!/usr/bin/env python3
# PumpHunter v4.2.1 - Enhanced (BTC trend filter + short support + fewer false positives)
# Usage: set environment variables (see README). Default safe: DRY_RUN=1 (no real orders).

import os, sys, time, math, json, logging, warnings
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests, pandas as pd, numpy as np, ta
except Exception as e:
    print("Missing dependency. Run: pip install requests pandas numpy ta")
    raise

# -------- Logging & flags --------
DEBUG = os.environ.get("DEBUG", "0") == "1"
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG if DEBUG else logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("PumpHunter")
warnings.filterwarnings("ignore")

# -------- Configuration (env-friendly) --------
DRY_RUN = os.environ.get("DRY_RUN", "1") == "0"   # default ON (safe)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOLS = os.environ.get("SYMBOLS")
if SYMBOLS:
    SYMBOLS = [s.strip().upper() for s in SYMBOLS.split(",") if s.strip()]
else:
    SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT",
    "AVAXUSDT", "DOTUSDT", "BASUSDT", "LINKUSDT", "TRXUSDT", "ATOMUSDT",
    "NEARUSDT", "APTUSDT", "FILUSDT", "SUIUSDT", "TIAUSDT", "ARBUSDT", "SEIUSDT",
    "TONUSDT", "NOTUSDT", "ARKMUSDT", "OPUSDT", "IMXUSDT", "RNDRUSDT", "INJUSDT",
    "FTMUSDT", "GALAUSDT", "MANAUSDT", "SANDUSDT", "AAVEUSDT", "CRVUSDT",
    "SNXUSDT", "UNIUSDT", "SUSHIUSDT", "DYDXUSDT", "COMPUSDT", "YFIUSDT",
    "EGLDUSDT", "THETAUSDT", "FLOWUSDT", "CHZUSDT", "XLMUSDT", "VETUSDT",
    "LTCUSDT", "BCHUSDT", "ZECUSDT", "ICPUSDT", "KAVAUSDT", "CELOUSDT", "HOTUSDT",
    "ENSUSDT", "LDOUSDT", "STRKUSDT", "PYTHUSDT", "NTRNUSDT", "ORDIUSDT",
    "SOLIDUSDT", "BOMEUSDT", "JUPUSDT"
]


KLIMIT = int(os.environ.get("KLIMIT", 150))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", 20))
THREADS = int(os.environ.get("THREADS", 18))
MIN_CANDLES = int(os.environ.get("MIN_CANDLES", 30))
BINANCE_CACHE_TTL = int(os.environ.get("BINANCE_CACHE_TTL", 3))

# thresholds (tunable)
VOL_MULT_STRONG = float(os.environ.get("VOL_MULT_STRONG", 2.5))   # raised to reduce false positives
VOL_MULT_WEAK = float(os.environ.get("VOL_MULT_WEAK", 1.4))
PRICE_ACCEL_THRESHOLD = float(os.environ.get("PRICE_ACCEL_THRESHOLD", 0.005))
SCORE_SIGNAL = int(os.environ.get("SCORE_SIGNAL", 98))  # stricter
SCORE_ALERT = int(os.environ.get("SCORE_ALERT", 97))
PRE_SIGNAL = int(os.environ.get("PRE_SIGNAL", 96))
MAX_ALERTS_PER_SYMBOL_PER_DAY = int(os.environ.get("MAX_ALERTS_PER_SYMBOL_PER_DAY", 1))
MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", 12))

PRICE_DECIMALS = int(os.environ.get("PRICE_DECIMALS", 6))
STATE_FILE = os.environ.get("STATE_FILE", "pumphunter_state_v4_enhanced.json")
BTC_TREND_WINDOW = int(os.environ.get("BTC_TREND_WINDOW", 60))  # minutes to compute BTC trend

# -------- Helpers --------
def nowts(): return int(time.time())
def nowstr(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
def _log_exception_short(msg, exc):
    if DEBUG:
        log.exception(msg)
    else:
        log.warning("%s: %s", msg, str(exc))

def format_price(x, decimals=PRICE_DECIMALS):
    try:
        v = float(x)
        s = f"{v:.{decimals}f}"
        if "." in s: s = s.rstrip("0").rstrip(".")
        return s
    except:
        return str(x)

# -------- Telegram (preview + send) --------
def tg_send(msg):
    try:
        log.info("TG PREVIEW: %s", msg.replace("\n"," | ")[:400])
    except:
        pass
    if DRY_RUN:
        return True
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Missing Telegram env variables.")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=8)
        if not r.ok:
            log.warning("Telegram send failed: %s %s", r.status_code, r.text)
            return False
        return True
    except Exception as e:
        _log_exception_short("tg_send error", e)
        return False

# -------- State persistence --------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            _log_exception_short("load_state error", e)
    return {"alerts":{}, "active_trades":[], "history":[], "cache":{}, "daily_trades":{}}

def save_state(s):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)
    except Exception as e:
        _log_exception_short("save_state error", e)

state = load_state()

# -------- Binance minimal helpers (REST) --------
def binance_request(path, params=None, base="https://api.binance.com"):
    url = base + path
    try:
        r = requests.get(url, params=params, timeout=8)
        if r.ok:
            return r.json()
    except Exception as e:
        if DEBUG: _log_exception_short("binance_request", e)
    return None

def fetch_klines_binance_with_retry(symbol, interval='1m', limit=KLIMIT, retries=1, backoff=0.4):
    key = f"{symbol}|{interval}|{limit}"
    c = state.get('cache', {}).get(key)
    now = nowts()
    if c and now - c.get('ts',0) < BINANCE_CACHE_TTL:
        try:
            df = pd.read_json(c['data'])
            df.index = pd.to_datetime(df.index)
            return df
        except:
            pass
    for attempt in range(retries+1):
        data = binance_request("/api/v3/klines", params={"symbol":symbol,"interval":interval,"limit":limit})
        if data:
            try:
                df = pd.DataFrame(data, columns=["open_time","open","high","low","close","volume","close_time","qav","num_trades","taker_base_vol","taker_quote_vol","ignore"])
                df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
                df.set_index('open_time', inplace=True)
                for ccol in ['open','high','low','close','volume']:
                    df[ccol] = pd.to_numeric(df[ccol], errors='coerce')
                df = df[['open','high','low','close','volume']].dropna()
                state.setdefault('cache', {})[key] = {'ts': nowts(), 'data': df.to_json()}
                save_state(state)
                return df
            except Exception as e:
                if DEBUG: _log_exception_short("parse klines", e)
        time.sleep(backoff*(attempt+1))
    return None

def fetch_klines_best(symbol):
    df = fetch_klines_binance_with_retry(symbol)
    if df is not None and len(df) >= MIN_CANDLES:
        return df.tail(KLIMIT)
    # CoinGecko fallback for a few symbols
    mapping = {"BTCUSDT":"bitcoin","ETHUSDT":"ethereum","BNBUSDT":"binancecoin"}
    base = mapping.get(symbol)
    if base:
        try:
            days = max(1, math.ceil(KLIMIT/1440))
            r = requests.get(f"https://api.coingecko.com/api/v3/coins/{base}/market_chart", params={'vs_currency':'usd','days':str(days)}, timeout=10)
            if r.ok:
                j = r.json()
                prices = j.get('prices', [])
                vols = j.get('total_volumes', [])
                recs=[]
                for i in range(min(len(prices), len(vols))):
                    ts = pd.to_datetime(prices[i][0], unit='ms')
                    price = prices[i][1]; vol = vols[i][1]
                    recs.append((ts, price, vol))
                df = pd.DataFrame(recs, columns=['time','price','volume']).set_index('time')
                df_ohlc = pd.DataFrame({'open':df['price'],'high':df['price'],'low':df['price'],'close':df['price'],'volume':df['volume']})
                return df_ohlc.tail(KLIMIT)
        except Exception as e:
            if DEBUG: _log_exception_short("coingecko fallback", e)
    return None

def current_price_best(symbol):
    d = binance_request("/api/v3/ticker/price", params={"symbol":symbol})
    if d and d.get("price"):
        try:
            return float(d['price'])
        except: pass
    df = fetch_klines_best(symbol)
    if df is not None:
        return float(df['close'].iloc[-1])
    return None

# -------- Features & scoring (symmetric for up/down) --------
def compute_pump_features(df):
    closes = df['close'].astype(float)
    vols = df['volume'].astype(float)
    returns = closes.pct_change().fillna(0)
    last = float(closes.iloc[-1])
    vol_mean = vols.ewm(span=30, adjust=False).mean().iloc[-2] if len(vols)>=10 else vols.mean()
    vol_last = float(vols.iloc[-1]) if len(vols)>0 else 0.0
    vol_mult = (vol_last / (vol_mean + 1e-9)) if vol_mean>0 else 0.0
    price_accel = float(returns.tail(3).sum()) if len(returns)>=3 else float(returns.sum())
    recent5 = float(returns.tail(5).sum()) if len(returns)>=5 else float(returns.sum())
    try:
        rsi = float(ta.momentum.rsi(closes, window=14).iloc[-1])
    except:
        rsi = None
    try:
        ema5 = closes.ewm(span=5, adjust=False).mean().iloc[-1]
        ema20 = closes.ewm(span=20, adjust=False).mean().iloc[-1]
    except:
        ema5 = ema20 = None
    vol_std = float(returns.tail(30).std()) if len(returns)>=30 else 0.0
    return {'last': last, 'vol_mult': vol_mult, 'price_accel': price_accel, 'recent5': recent5, 'rsi': rsi, 'ema5': ema5, 'ema20': ema20, 'vol_std': vol_std}

def compute_pump_score(features, pulse_vals=None, btc_trend_pct=0.0):
    score = 0; reasons=[]
    v = features.get('vol_mult', 0)
    if v >= VOL_MULT_STRONG:
        score += 40; reasons.append('vol_strong')
    elif v >= VOL_MULT_WEAK:
        score += 20; reasons.append('vol_med')
    accel = features.get('price_accel', 0)
    # bi-directional accel scoring
    if accel >= PRICE_ACCEL_THRESHOLD:
        score += 30; reasons.append('accel_up')
    elif accel <= -PRICE_ACCEL_THRESHOLD:
        score += 30; reasons.append('accel_down')
    if features.get('recent5',0) > 0.01:
        score += 8; reasons.append('recent5_pos')
    if features.get('rsi') is not None:
        r=features['rsi']
        if r<40:
            score += 4; reasons.append('RSI_low')
        elif r>85:
            score -= 12; reasons.append('RSI_extreme')
    if features.get('vol_std',0) > 0.001:
        score += min(8,int(features['vol_std']*1000)); reasons.append('volatility')
    if pulse_vals:
        ob_imb, taker_ratio, big_taker = pulse_vals
        if abs(ob_imb) > 0.25:
            score += 10; reasons.append('ob_imb')
        if abs(taker_ratio) > 0.25:
            score += 10; reasons.append('taker')
        if big_taker > 0:
            score += min(6,int(big_taker*100)); reasons.append('big_taker')
    # adjust by BTC global trend: favor shorts if BTC down, favor longs if BTC up
    if btc_trend_pct <= -0.005:  # BTC down >0.5% => favor shorts
        if accel <= -PRICE_ACCEL_THRESHOLD:
            score += 12; reasons.append('btc_down_boost')
        if accel >= PRICE_ACCEL_THRESHOLD:
            score -= 8; reasons.append('btc_down_penalty_for_long')
    elif btc_trend_pct >= 0.005:  # BTC up
        if accel >= PRICE_ACCEL_THRESHOLD:
            score += 12; reasons.append('btc_up_boost')
        if accel <= -PRICE_ACCEL_THRESHOLD:
            score -= 8; reasons.append('btc_up_penalty_for_short')
    score = max(0, min(100, int(score)))
    return score, reasons

# -------- Orderbook + trades pulse (cheap) --------
def fetch_orderbook(symbol, limit=20):
    try:
        r = requests.get('https://api.binance.com/api/v3/depth', params={'symbol':symbol,'limit':limit}, timeout=6)
        if r.ok: return r.json()
    except Exception as e:
        if DEBUG: _log_exception_short("orderbook", e)
    return None

def fetch_recent_trades(symbol, limit=200):
    try:
        r = requests.get('https://api.binance.com/api/v3/trades', params={'symbol':symbol,'limit':limit}, timeout=6)
        if r.ok: return r.json()
    except Exception as e:
        if DEBUG: _log_exception_short("trades", e)
    return None

def compute_pulse(ob, trades, top_n=10):
    try:
        if not ob or not trades: return (0.0,0.0,0.0)
        top_n = min(top_n, len(ob.get('bids',[])), len(ob.get('asks',[])))
        bid_vol = sum(float(q) for p,q in ob['bids'][:top_n])
        ask_vol = sum(float(q) for p,q in ob['asks'][:top_n])
        ob_imb = (bid_vol - ask_vol)/(bid_vol + ask_vol + 1e-9)
        buy_taker = sell_taker = 0.0
        big_vol = 0.0
        for t in trades:
            q = float(t.get('qty', t.get('quantity',0)))
            if t.get('isBuyerMaker'):
                sell_taker += q
            else:
                buy_taker += q
            if q >= 0.01:
                big_vol += q
        taker_ratio = (buy_taker - sell_taker)/(buy_taker + sell_taker + 1e-9)
        return (ob_imb, taker_ratio, big_vol)
    except Exception as e:
        if DEBUG: _log_exception_short("compute_pulse", e)
        return (0.0,0.0,0.0)

# -------- Watchlist & publish control --------
def can_publish(sym):
    now = nowts()
    pub = state.get('alerts',{})
    daily = pub.get(sym, {"count":0,"last":0})
    if now - daily.get("last",0) > 24*3600:
        return True
    if daily.get("count",0) < MAX_ALERTS_PER_SYMBOL_PER_DAY:
        return True
    return False

def register_publish(sym):
    pub = state.setdefault('alerts',{})
    daily = pub.get(sym, {"count":0,"last":0})
    if nowts() - daily.get("last",0) > 24*3600:
        daily = {"count":0,"last":0}
    daily['count'] = daily.get('count',0) + 1
    daily['last'] = nowts()
    pub[sym] = daily
    save_state(state)

def daily_trade_count_today():
    d = datetime.utcnow().strftime("%Y-%m-%d")
    return state.setdefault('daily_trades', {}).get(d, 0)

def register_daily_trade():
    d = datetime.utcnow().strftime("%Y-%m-%d")
    state.setdefault('daily_trades', {})
    state['daily_trades'][d] = state['daily_trades'].get(d,0) + 1
    save_state(state)

CIRCLED = ['①','②','③','④','⑤','⑥','⑦']

def publish_trade(sym, features, reasons, price, score, btc_trend_pct=0.0):
    if not can_publish(sym):
        log.info("Publish blocked by alerts rate limit: %s", sym); return False
    if daily_trade_count_today() >= MAX_TRADES_PER_DAY:
        log.info("Daily trades limit reached. Skipping publish."); return False
    side = 'LONG' if features.get('price_accel',0) >= 0 else 'SHORT'
    leverage =  (50 if score>=95 else 25 if score>=85 else 10 if score>=65 else 5)
    # Compose percent targets (realistic)
    TARGET_STEPS = [0.005,0.01,0.015,0.02,0.03,0.04,0.06]
    tps=[]; 
    for s in TARGET_STEPS:
        tp = price*(1+s) if side=='LONG' else price*(1-s)
        tps.append(round(tp, PRICE_DECIMALS))
    stop = round(price * (0.95 if side=='LONG' else 1.05), PRICE_DECIMALS)
    lines = [f"${sym}", f"{'Long' if side=='LONG' else 'Short'} x{leverage}", f"Entry: {format_price(price)}", "" , "Targets:"]
    for i,tp in enumerate(tps[:7]): lines.append(f"{CIRCLED[i]} {format_price(tp)}")
    lines.append(""); lines.append("Stop: " + format_price(stop))
    msg = "\n".join(lines)
    ok = tg_send(msg)
    if ok:
        register_publish(sym); register_daily_trade()
        active = {"symbol":sym,"entry":price,"side":side,"tps":tps,"stop":stop,"hit":[False]*len(tps),"opened_at":nowts(),"leverage":leverage,"tp_hit_any":False}
        state.setdefault('active_trades',[]).append(active)
        save_state(state)
        log.info("Published trade for %s score=%s reasons=%s btc_trend=%.3f", sym, score, reasons, btc_trend_pct)
        return True
    return False

# -------- Active trades monitor (TP/STOP notifications) --------
def monitor_active_trades():
    changed=False
    for trade in list(state.get('active_trades',[])):
        sym = trade.get('symbol')
        try:
            price = current_price_best(sym)
            if price is None:
                df = fetch_klines_best(sym)
                if df is None: continue
                price = float(df['close'].iloc[-1])
            side = trade.get('side'); entry = trade.get('entry'); tps = trade.get('tps',[]); stop = trade.get('stop'); hit = trade.get('hit', [False]*len(tps))
            for idx,tp in enumerate(tps):
                if not hit[idx] and ((side=='LONG' and price>=tp) or (side=='SHORT' and price<=tp)):
                    hit[idx]=True; trade['hit']=hit; trade['tp_hit_any']=True
                    tg_send(f"${sym}\n🎯 TP {CIRCLED[idx] if idx<len(CIRCLED) else idx+1} hit → {format_price(tp)}")
                    changed=True
            # stop handling
            if (side=='LONG' and price<=stop) or (side=='SHORT' and price>=stop):
                if not trade.get('tp_hit_any', False):
                    tg_send(f"${sym}\n⛔ STOP hit → {format_price(stop)}")
                # record close
                try:
                    state['active_trades'].remove(trade)
                except: pass
                state.setdefault('history',[]).append({"symbol":sym,"side":side,"entry":entry,"exit":price,"reason":"STOP","opened_at":trade.get('opened_at'), "closed_at":nowts(), "hit":hit})
                changed=True
            if all(hit):
                tg_send(f"${sym}\n🏁 ALL TPs HIT\nEntry: {format_price(entry)}\nFinal: {format_price(price)}")
                try: state['active_trades'].remove(trade)
                except: pass
                state.setdefault('history',[]).append({"symbol":sym,"side":side,"entry":entry,"exit":price,"reason":"ALL_TP","opened_at":trade.get('opened_at'), "closed_at":nowts(), "hit":hit})
                changed=True
        except Exception as e:
            _log_exception_short(f"monitor {sym}", e)
    if changed: save_state(state)

# -------- Scan single symbol --------
def scan_symbol(sym, btc_trend_pct):
    try:
        df = fetch_klines_best(sym)
        if df is None or len(df) < MIN_CANDLES:
            log.debug("%s: no klines", sym); return None
        features = compute_pump_features(df)
        ob = fetch_orderbook(sym, limit=10)
        trades = fetch_recent_trades(sym, limit=100)
        pulse = compute_pulse(ob, trades)
        score, reasons = compute_pump_score(features, pulse_vals=pulse, btc_trend_pct=btc_trend_pct)
        features['score']=score
        # simple indicator filter: bias-aware
        ok_ind = True
        try:
            if features.get('price_accel',0) >= 0:
                if not (features.get('ema5') and features.get('ema20') and features['ema5'] > features['ema20']): ok_ind=False
                if features.get('rsi') and features['rsi'] > 85: ok_ind=False
            else:
                if not (features.get('ema5') and features.get('ema20') and features['ema5'] < features['ema20']): ok_ind=False
                if features.get('rsi') and features['rsi'] < 15: ok_ind=False
        except Exception:
            ok_ind=True
        return {'symbol':sym,'features':features,'score':score,'reasons':reasons,'pulse':pulse,'last':features['last'],'ok_ind':ok_ind}
    except Exception as e:
        _log_exception_short(f"scan {sym}", e); return None

# -------- BTC trend calculation --------
def btc_trend_pct_calc():
    try:
        df = fetch_klines_best("BTCUSDT")
        if df is None or len(df) < 10: return 0.0
        look = min(len(df)-1, BTC_TREND_WINDOW)
        old = float(df['close'].iloc[-1-look])
        nowp = float(df['close'].iloc[-1])
        if old == 0: return 0.0
        return (nowp - old) / old
    except Exception as e:
        if DEBUG: _log_exception_short("btc_trend", e)
        return 0.0

# -------- Main loop (parallel) --------
def main_loop():
    log.info("PumpHunter Enhanced starting. DRY_RUN=%s DEBUG=%s", DRY_RUN, DEBUG)
    executor = ThreadPoolExecutor(max_workers=THREADS)
    cycle=0
    try:
        while True:
            cycle+=1; start=time.time()
            btc_trend = btc_trend_pct_calc()
            log.info("--- Cycle %s | BTC trend pct=%.4f ---", cycle, btc_trend)
            futures = {executor.submit(scan_symbol, s, btc_trend): s for s in SYMBOLS}
            results=[]
            for fut in as_completed(futures, timeout=30):
                try:
                    r = fut.result()
                except Exception as e:
                    _log_exception_short("future", e); r=None
                if r: results.append(r)
            results = sorted(results, key=lambda x: x['score'], reverse=True)
            for r in results:
                sym=r['symbol']; score=r['score']; features=r['features']; reasons=r['reasons']; last=r['last']; ok_ind=r.get('ok_ind', True)
                log.info("%s | score=%s | vol_mult=%.2f | accel=%.5f | rsi=%s | last=%s | ind_ok=%s",
                         sym, score, features.get('vol_mult',0), features.get('price_accel',0),
                         ("%.2f" % features['rsi']) if features.get('rsi') is not None else "n/a", format_price(last), ok_ind)
                if not ok_ind: continue
                if score >= SCORE_SIGNAL and can_publish(sym):
                    publish_trade(sym, features, reasons, price=last, score=score, btc_trend_pct=btc_trend)
                elif score >= PRE_SIGNAL and can_publish(sym):
                    publish_trade(sym, features, reasons, price=last, score=score, btc_trend_pct=btc_trend)
                elif score >= SCORE_ALERT and can_publish(sym):
                    publish_trade(sym, features, reasons, price=last, score=score, btc_trend_pct=btc_trend)
                time.sleep(0.03)
            monitor_active_trades()
            elapsed = time.time() - start
            to_sleep = max(1, POLL_SECONDS - elapsed)
            log.info("Cycle complete. sleeping %.1f s", to_sleep)
            time.sleep(to_sleep)
    except KeyboardInterrupt:
        log.info("Stopped by user. Saving state."); save_state(state)
    except Exception as e:
        _log_exception_short("Fatal", e); save_state(state); raise

if __name__ == "__main__":
    main_loop()
