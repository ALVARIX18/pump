#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PumpHunter v2.1 - Improved & Pulse filter (ready for Rail/GitHub)
# - DRY_RUN=True => sends Telegram messages only (no real execution)
# - Uses Binance klines + orderbook/trades pulse filter
# - Prevent duplicate trades, improved TP spacing, stop-notify logic
# - Configure via env vars and top constants

import os, sys, time, math, json, logging
from datetime import datetime, timezone
import requests
import pandas as pd
import numpy as np

# optional TA lib; used only if available
try:
    import ta
except Exception:
    ta = None

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("PumpHunter")

# ---------------- CONFIG (عدل هذه فقط) ----------------
DRY_RUN = True
ENABLE_EXECUTION = False

# Put tokens in environment variables (recommended)
os.environ["TG_TOKEN"] = "8294324404:AAGyA3W6S3E98TaqOgc9iQpPVOcAqlJ2Ung"
os.environ["TG_CHAT"] = "-1003089256716"

TELEGRAM_BOT_TOKEN = os.environ.get("TG_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TG_CHAT")

# Symbols to scan
SYMBOLS = [
 "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","TRXUSDT",
 "AVAXUSDT","DOTUSDT","MATICUSDT","LTCUSDT","LINKUSDT","ATOMUSDT","XLMUSDT",
 "NEARUSDT","FTMUSDT","FILUSDT","APTUSDT","SANDUSDT","AAVEUSDT","THETAUSDT"
]

# thresholds & tuning (تقدر تغيّرهم)
MIN_CANDLES = 30
KLIMIT = 60
POLL_SECONDS = 60           # main loop frequency
VOL_MULT_STRONG = 1.8
VOL_MULT_WEAK = 1.2
PRICE_ACCEL_THRESHOLD = 0.007
SCORE_ALERT = 8
SCORE_SIGNAL = 12
MAX_ALERTS_PER_SYMBOL_PER_DAY = 1
MAX_TRADES_PER_DAY = 7

# Pulse filter (الفكرة الجهنمية) - اجعل True لتفعيل فلتر دفتر الأوامر + تريدرز
ENABLE_PULSE_FILTER = True
PULSE_OB_IMB_THRESHOLD = 0.30   # imbalance threshold
PULSE_TAKER_THRESHOLD = 0.30    # taker ratio threshold

# Targets formatting / precision
PRICE_DECIMALS = 6             # عدد الخانات العشرية في الطباعة
MIN_TP_PCT = 0.001             # أقل فرق بين TP و Entry (0.1%)

# state file
STATE_FILE = "pumphunter_state_v2.json"
BINANCE_CACHE_TTL = 5

# ---------------- Helpers ----------------
def nowts(): return int(time.time())
def nowstr(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def format_price(x, decimals=PRICE_DECIMALS):
    # Friendly formatting: round and strip trailing zeros
    try:
        v = float(x)
        s = f"{v:.{decimals}f}"
        # remove trailing zeros but keep at least one decimal if decimals>0
        if "." in s:
            s = s.rstrip("0").rstrip(".")
            # ensure at least decimals places visually (optional) -> we keep up to decimals
        return s
    except:
        return str(x)

def tg_send(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("TG(DISABLED) | %s", msg.replace("\n"," | ")[:300])
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        if DRY_RUN:
            log.info("TG PREVIEW: %s", msg.replace("\n", " | ")[:400])
        r = requests.post(url, data=payload, timeout=8)
        if not r.ok:
            log.error("Telegram send failed: %s %s", r.status_code, r.text)
            return False
        return True
    except Exception as e:
        log.exception("Telegram send error")
        return False

# ---------------- State ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE,"r",encoding="utf-8"))
        except Exception:
            log.exception("Failed to load state, starting fresh")
    return {"alerts":{}, "active_trades":[], "history":[], "binance_cache":{}, "daily_trades":{}}

def save_state(st):
    try:
        json.dump(st, open(STATE_FILE,"w",encoding="utf-8"), indent=2, ensure_ascii=False)
    except Exception:
        log.exception("state save error")

state = load_state()

# ---------------- Binance data helpers ----------------
def binance_request(path, params=None, base="https://api.binance.com"):
    url = base + path
    try:
        r = requests.get(url, params=params, timeout=8)
        if r.ok:
            return r.json()
    except Exception:
        return None
    return None

def fetch_klines_binance_with_retry(symbol, interval='1m', limit=KLIMIT, retries=2, backoff=0.8):
    key = f"{symbol}|{interval}|{limit}"
    cinfo = state.get('binance_cache', {}).get(key)
    now = nowts()
    if cinfo and (now - cinfo.get('ts',0) < BINANCE_CACHE_TTL):
        try:
            df = pd.read_json(cinfo['data'])
            df.index = pd.to_datetime(df.index)
            return df
        except Exception:
            pass
    for attempt in range(retries+1):
        try:
            data = binance_request("/api/v3/klines", params={"symbol":symbol,"interval":interval,"limit":limit})
            if data:
                df = pd.DataFrame(data, columns=["open_time","open","high","low","close","volume",
                                                 "close_time","qav","num_trades","taker_base_vol","taker_quote_vol","ignore"])
                df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
                df.set_index('open_time', inplace=True)
                for c in ['open','high','low','close','volume']:
                    df[c] = pd.to_numeric(df[c], errors='coerce')
                df = df[['open','high','low','close','volume']].dropna()
                try:
                    state.setdefault('binance_cache', {})[key] = {'ts': nowts(), 'data': df.to_json()}
                    save_state(state)
                except:
                    pass
                return df
        except Exception:
            pass
        time.sleep(backoff * (attempt+1))
    return None

def coingecko_ohlcv(symbol, minutes=KLIMIT):
    # simple fallback for some coins
    COINGECKO_SIMPLE_MAP = {
        "BTCUSDT":"bitcoin","ETHUSDT":"ethereum","SOLUSDT":"solana","AVAXUSDT":"avalanche-2",
        "MATICUSDT":"matic-network","DOTUSDT":"polkadot","LINKUSDT":"chainlink","DOGEUSDT":"dogecoin",
        "ADAUSDT":"cardano","BNBUSDT":"binancecoin"
    }
    cg_id = COINGECKO_SIMPLE_MAP.get(symbol)
    if not cg_id:
        return None
    try:
        days = max(1, math.ceil(minutes/24/60))
        url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart"
        r = requests.get(url, params={'vs_currency':'usd','days':str(days)}, timeout=12)
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
        if not recs:
            return None
        df = pd.DataFrame(recs, columns=['time','price','volume']).set_index('time')
        df_ohlc = pd.DataFrame({'open':df['price'],'high':df['price'],'low':df['price'],'close':df['price'],'volume':df['volume']})
        return df_ohlc.tail(minutes)
    except Exception:
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
    try:
        data = binance_request("/api/v3/ticker/price", params={"symbol":symbol})
        if data and data.get("price"):
            return float(data.get("price"))
    except:
        pass
    df = fetch_klines_best(symbol)
    if df is not None:
        return float(df['close'].iloc[-1])
    return None

# ---------------- Pulse filter (orderbook + trades) ----------------
def fetch_orderbook(symbol, limit=10):
    try:
        r = requests.get('https://api.binance.com/api/v3/depth', params={'symbol':symbol,'limit':limit}, timeout=6)
        if r.ok:
            return r.json()
    except:
        pass
    return None

def fetch_recent_trades(symbol, limit=100):
    try:
        r = requests.get('https://api.binance.com/api/v3/trades', params={'symbol':symbol,'limit':limit}, timeout=6)
        if r.ok:
            return r.json()
    except:
        pass
    return None

def compute_pulse(ob, trades):
    try:
        if not ob or not trades:
            return 0.0, 0.0
        top_n = min(10, len(ob.get('bids',[])), len(ob.get('asks',[])))
        bid_vol = sum(float(q) for p,q in ob['bids'][:top_n])
        ask_vol = sum(float(q) for p,q in ob['asks'][:top_n])
        ob_imb = (bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-9)
        buy_taker = 0.0
        sell_taker = 0.0
        for t in trades:
            q = float(t.get('qty', t.get('quantity',1)))
            # isBuyerMaker True -> taker was seller
            if t.get('isBuyerMaker'):
                sell_taker += q
            else:
                buy_taker += q
        taker_ratio = (buy_taker - sell_taker) / (buy_taker + sell_taker + 1e-9)
        return ob_imb, taker_ratio
    except Exception:
        return 0.0, 0.0

# ---------------- Features & scoring ----------------
def compute_pump_features(df):
    closes = df['close'].astype(float)
    vols = df['volume'].astype(float)
    returns = closes.pct_change().fillna(0)
    last_price = float(closes.iloc[-1])
    vol_mean = vols.tail(30).mean() if len(vols)>=30 else (vols.mean() if len(vols)>0 else 0.0)
    vol_last = float(vols.iloc[-1]) if len(vols)>0 else 0.0
    vol_mult = (vol_last / (vol_mean + 1e-9)) if vol_mean>0 else 0.0
    price_accel = float(returns.tail(3).sum()) if len(returns)>=3 else float(returns.sum())
    recent_return_5 = float(returns.tail(5).sum()) if len(returns)>=5 else float(returns.sum())
    rsi = None
    try:
        if ta:
            rsi = float(ta.momentum.rsi(closes, window=14).iloc[-1])
    except Exception:
        rsi = None
    vol_std = float(returns.tail(30).std()) if len(returns)>=30 else 0.0
    return {"last":last_price,"vol_last":vol_last,"vol_mean":float(vol_mean),"vol_mult":vol_mult,
            "price_accel":price_accel,"recent_return_5":recent_return_5,"rsi":rsi,"vol_std":vol_std}

def compute_pump_score(features):
    score = 0
    reasons = []
    v = features.get('vol_mult',0)
    if v >= VOL_MULT_STRONG:
        score += 40; reasons.append("vol_strong")
    elif v >= VOL_MULT_WEAK:
        score += 20; reasons.append("vol_med")
    if features.get('price_accel',0) >= PRICE_ACCEL_THRESHOLD:
        score += 30; reasons.append("accel_high")
    elif features.get('price_accel',0) >= PRICE_ACCEL_THRESHOLD/2:
        score += 10; reasons.append("accel_mild")
    if features.get('recent_return_5',0) > 0.01:
        score += 10; reasons.append("recent5>1%")
    r = features.get('rsi')
    if r is not None:
        if r < 40:
            score += 5; reasons.append("RSI_low")
        elif r > 80:
            score -= 20; reasons.append("RSI_extreme")
    if features.get('vol_std',0) > 0.001:
        score += min(10,int(features['vol_std']*1000)); reasons.append("volatility")
    return max(0, min(100,int(score))), reasons

# ---------------- Targets & stop (ATR-aware + min spacing) ----------------
def compose_targets_and_stop(entry, df_for_atr, side='LONG', leverage=20):
    atr = None
    try:
        if ta and df_for_atr is not None and len(df_for_atr) >= 14:
            closes = df_for_atr['close'].astype(float)
            high = df_for_atr['high'].astype(float)
            low = df_for_atr['low'].astype(float)
            atr = float(ta.volatility.average_true_range(high, low, closes, window=14).iloc[-1])
    except Exception:
        atr = None

    # percent base (fallback)
    percents = [0.004,0.008,0.012,0.02,0.03,0.05,0.08]  # default
    if atr and atr > 0:
        # Use ATR multipliers but ensure minimal percent spacing
        mults = [0.5,1,1.5,2,3,4,6]
        tps_raw = [(entry + atr*m) if side=='LONG' else (entry - atr*m) for m in mults]
        # enforce min spacing in percent terms
        tps = []
        last = entry
        for tp in tps_raw:
            pct = abs((tp - last) / (entry + 1e-9))
            if pct < MIN_TP_PCT:
                # expand to min pct
                if side == 'LONG':
                    tp = last * (1 + MIN_TP_PCT)
                else:
                    tp = last * (1 - MIN_TP_PCT)
            tps.append(round(tp, PRICE_DECIMALS))
            last = tps[-1]
        # stop based on ATR
        stop = round((entry - atr*1.5) if side=='LONG' else (entry + atr*1.5), PRICE_DECIMALS)
    else:
        # percent based
        tps = [round(entry*(1+p) if side=='LONG' else entry*(1-p), PRICE_DECIMALS) for p in percents]
        stop = round(entry*(1-0.01) if side=='LONG' else entry*(1+0.01), PRICE_DECIMALS)

    # format to consistent decimals
    tps_fmt = [float(f"{tp:.{PRICE_DECIMALS}f}") for tp in tps]
    return tps_fmt, float(stop)

# ---------------- Publish / register trade ----------------
def can_publish(sym):
    now = nowts()
    pub = state.get('alerts', {})
    daily = pub.get(sym, {"count":0,"last":0})
    if now - daily.get("last",0) > 24*3600:
        return True
    if daily.get("count",0) < MAX_ALERTS_PER_SYMBOL_PER_DAY:
        return True
    return False

def daily_trades_count():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return state.get('daily_trades', {}).get(today, 0)

def register_daily_trade():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    d = state.setdefault('daily_trades', {})
    d[today] = d.get(today, 0) + 1
    save_state(state)

def publish_trade(sym, features, reasons, df_for_atr, leverage=20):
    # don't double-open same symbol
    for t in state.get('active_trades', []):
        if t.get('symbol') == sym:
            log.info("Skipping publish_trade: active trade exists for %s", sym)
            return False
    if daily_trades_count() >= MAX_TRADES_PER_DAY:
        log.info("Skipping publish_trade: reached daily trade limit (%s)", MAX_TRADES_PER_DAY)
        return False

    entry = features['last']
    side = 'LONG' if features['price_accel'] >= 0 else 'SHORT'
    tps, stop = compose_targets_and_stop(entry, df_for_atr, side=side, leverage=leverage)

    # compose message (friendly)
    msg = f"${sym}\n{side} Cross {leverage}x\n🟢Entry: {format_price(entry)}\n\nTargets:\n"
    for i,tp in enumerate(tps, start=1):
        msg += f"① {format_price(tp)}\n" if i==1 else f"② {format_price(tp)}\n" if i==2 else f"{i}. {format_price(tp)}\n"
    msg += f"\n⛔Stop: {format_price(stop)}\n\nReasons: {', '.join(reasons)}\n(DRY_RUN={DRY_RUN})\n{nowstr()}"

    ok = tg_send(msg)
    if ok:
        active = {"symbol":sym,"entry":entry,"side":side,"tps":tps,"stop":stop,"hit":[False]*len(tps),
                  "opened_at":nowts(),"leverage":leverage,"tp_hit_any":False}
        state.setdefault('active_trades',[]).append(active)
        register_daily_trade()
        save_state(state)
        return True
    return False

def publish_alert(sym, features, reasons, level="ALERT"):
    msg = f"{'🚨 STRONG' if level=='SIGNAL' else '⚡ ALERT'}\n{sym}\nLevel: {level}\nScore: {features.get('score','?')}\nPrice: {format_price(features.get('last'))}\nvol_mult: {features['vol_mult']:.2f}\naccel: {features['price_accel']:.4f}\nReasons: {', '.join(reasons)}\n(DRY_RUN={DRY_RUN})\n{nowstr()}"
    ok = tg_send(msg)
    if ok:
        pub = state.setdefault('alerts', {})
        daily = pub.get(sym, {"count":0,"last":0})
        if nowts() - daily.get("last",0) > 24*3600:
            daily = {"count":0,"last":0}
        daily['count'] = daily.get('count',0) + 1
        daily['last'] = nowts()
        pub[sym] = daily
        save_state(state)
    return ok

# ---------------- Monitor ----------------
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
                if df is None: continue
                price = float(df['close'].iloc[-1])
            side = trade.get('side')
            entry = trade.get('entry')
            tps = trade.get('tps',[])
            stop = trade.get('stop')
            hit = trade.get('hit',[False]*len(tps))
            any_tp_hit = trade.get('tp_hit_any', False)

            # check TPs
            for idx, tp in enumerate(tps):
                if not hit[idx]:
                    if (side=='LONG' and price >= tp) or (side=='SHORT' and price <= tp):
                        hit[idx] = True
                        trade['hit'] = hit
                        trade['tp_hit_any'] = True
                        # send TP message
                        tg_send(f"✅ TP{idx+1} HIT — {sym}\nTP: {format_price(tp)}\nEntry: {format_price(entry)}\nNow: {format_price(price)}\n{nowstr()}")
                        changed = True

            # check STOP: only send STOP alert if no TP was hit yet for this trade
            if (side=='LONG' and price <= stop) or (side=='SHORT' and price >= stop):
                if not trade.get('tp_hit_any', False):
                    tg_send(f"⛔ STOP HIT — {sym}\nStop: {format_price(stop)}\nEntry: {format_price(entry)}\nNow: {format_price(price)}\n{nowstr()}")
                # record and remove
                state['active_trades'].remove(trade)
                # record history
                rec = {"symbol":sym,"side":side,"entry":entry,"exit":price,"reason":"STOP","opened_at":trade.get('opened_at'),"closed_at":nowts(),"hit":hit}
                state.setdefault('history',[]).append(rec)
                changed = True
                continue

            # all hit
            if all(hit):
                tg_send(f"🏁 ALL TPs HIT — {sym}\nEntry: {format_price(entry)}\nFinal: {format_price(price)}\n{nowstr()}")
                state['active_trades'].remove(trade)
                rec = {"symbol":sym,"side":side,"entry":entry,"exit":price,"reason":"ALL_TP","opened_at":trade.get('opened_at'),"closed_at":nowts(),"hit":hit}
                state.setdefault('history',[]).append(rec)
                changed = True
        except Exception:
            log.exception("monitor error for %s", sym)
            continue
    if changed:
        save_state(state)

# ---------------- Execution placeholder ----------------
def execute_order(symbol, side, entry_price, size=None, leverage=20):
    if not ENABLE_EXECUTION:
        log.info("EXECUTION disabled")
        return {"status":"skipped"}
    raise NotImplementedError("Real execution not implemented.")

# ---------------- Main scanning loop ----------------
def main_loop():
    log.info("PumpHunter v2.1 starting. DRY_RUN=%s", DRY_RUN)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        tg_send(f"🤖 PumpHunter started — DRY_RUN={DRY_RUN} — tracking {len(SYMBOLS)} symbols")

    cycle = 0
    try:
        while True:
            cycle += 1
            start = nowts()
            log.info("--- Cycle %s | %s ---", cycle, nowstr())

            for sym in SYMBOLS:
                try:
                    df = fetch_klines_best(sym)
                    if df is None or len(df) < MIN_CANDLES:
                        log.debug("%s: no klines (skip)", sym)
                        continue

                    feats = compute_pump_features(df)
                    score, reasons = compute_pump_score(feats)
                    feats['score'] = score

                    # compute pulse if enabled
                    pulse_ok = True
                    ob_imb = taker = 0.0
                    if ENABLE_PULSE_FILTER:
                        ob = fetch_orderbook(sym, limit=10)
                        trades = fetch_recent_trades(sym, limit=100)
                        ob_imb, taker = compute_pulse(ob, trades)
                        # require at least one threshold
                        pulse_ok = (abs(ob_imb) >= PULSE_OB_IMB_THRESHOLD) or (abs(taker) >= PULSE_TAKER_THRESHOLD)

                    log.info("%s | score=%s | vol_mult=%.2f | accel=%.6f | rsi=%s | last=%s | ob_imb=%.3f taker=%.3f",
                             sym, score, feats['vol_mult'], feats['price_accel'],
                             ("%.2f" % feats['rsi']) if feats['rsi'] is not None else "n/a", format_price(feats['last']),
                             ob_imb, taker)

                    # publish logic with pulse filter & daily limits & active-trade check
                    if score >= SCORE_SIGNAL and can_publish(sym) and pulse_ok:
                        published = publish_trade(sym, feats, reasons, df_for_atr=df, leverage=20)
                        if published:
                            log.info("Published TRADE for %s (score=%s reasons=%s)", sym, score, reasons)
                        else:
                            log.info("Publish skipped/failed for %s", sym)
                    elif score >= SCORE_ALERT and can_publish(sym) and pulse_ok:
                        ok = publish_alert(sym, feats, reasons, level="ALERT")
                        if ok:
                            log.info("Published ALERT for %s (score=%s reasons=%s)", sym, score, reasons)

                    time.sleep(0.12)
                except Exception:
                    log.exception("Scan error for %s", sym)
                    continue

            # monitor active trades a few times during sleep window
            monitor_active_trades()
            elapsed = nowts() - start
            to_sleep = max(1, POLL_SECONDS - elapsed)
            log.info("Cycle complete. Sleeping %s s", to_sleep)
            time.sleep(to_sleep)
    except KeyboardInterrupt:
        log.info("Stopped by user. Saving state.")
        save_state(state)
    except Exception:
        log.exception("Fatal error in main loop. Saving state.")
        save_state(state)
        raise

if __name__ == "__main__":
    main_loop()
