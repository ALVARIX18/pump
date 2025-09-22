# main.py
# PumpHunter v2.0 - Debuggable, Rail-ready single-file
# DRY_RUN=True => sends Telegram messages only (no real execution)
# Save as main.py and deploy (Railway / VPS). Set TELEGRAM token & CHAT_ID.

import os
import sys
import time
import math
import json
import logging
import traceback
from datetime import datetime, timezone

# Try to import libs; if missing (e.g. in Colab) print instruction.
try:
    import requests
    import pandas as pd
    import numpy as np
    import ta
except Exception as e:
    # If running interactively (Colab), try to install
    if "COLAB_KERNEL" in os.environ or os.environ.get("CI", "") == "":
        print("Missing packages. Attempting pip install (only for interactive runs)...")
        os.system("pip install -q requests pandas numpy ta")
        time.sleep(1)
        try:
            import requests
            import pandas as pd
            import numpy as np
            import ta
        except Exception:
            print("Please install requirements manually: pip install requests pandas numpy ta")
            raise
    else:
        raise

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("PumpHunter")

# ---------------- CONFIG (edit these only) ----------------
DRY_RUN = True                    # True = signals only (safe). Set False only if you implement execute safely.
ENABLE_EXECUTION = False          # Don't enable unless you added safe execution code.

# Put your tokens here or use environment variables (recommended).
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8294324404:AAGyA3W6S3E98TaqOgc9iQpPVOcAqlJ2Ung")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1003089256716")

# Symbols to scan
SYMBOLS = [
 "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","TRXUSDT",
 "AVAXUSDT","DOTUSDT","MATICUSDT","LTCUSDT","LINKUSDT","ATOMUSDT","XLMUSDT",
 "NEARUSDT","FTMUSDT","FILUSDT","APTUSDT","SANDUSDT","AAVEUSDT","THETAUSDT"
]

# thresholds
MIN_CANDLES = 30
KLIMIT = 60
POLL_SECONDS = 60           # main loop sleep (set 60s for nicer logs)
VOL_MULT_STRONG = 5
VOL_MULT_WEAK = 2.1
PRICE_ACCEL_THRESHOLD = 2.510
SCORE_ALERT = 19
SCORE_SIGNAL = 20
MAX_ALERTS_PER_SYMBOL_PER_DAY = 1

# state file (persist alerts / active_trades / cache)
STATE_FILE = "pumphunter_state_v2.json"
BINANCE_CACHE_TTL = 5

# ---------------- Helpers ----------------
def nowts(): return int(time.time())
def nowstr(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def tg_send(msg):
    """Send message to Telegram. returns True on success. In DRY_RUN prints preview."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram disabled (no token/chat). Preview:\n%s", msg)
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
            log.exception("Failed loading state file, starting fresh")
    return {"alerts":{}, "last_run":0, "symbols_info":{}, "active_trades":[], "history":[], "binance_cache":{}}

def save_state(st):
    try:
        json.dump(st, open(STATE_FILE,"w",encoding="utf-8"), indent=2, ensure_ascii=False)
    except Exception:
        log.exception("state save error")

state = load_state()

# ---------------- Binance helpers ----------------
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
            log.debug("cache read error for %s", symbol)
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
                except Exception:
                    log.debug("cache save failed")
                return df
        except Exception:
            log.debug("binance attempt %s failed for %s", attempt, symbol)
        time.sleep(backoff * (attempt+1))
    return None

# ---------------- CoinGecko fallback ----------------
COINGECKO_SIMPLE_MAP = {
    "BTCUSDT":"bitcoin","ETHUSDT":"ethereum","SOLUSDT":"solana","AVAXUSDT":"avalanche-2",
    "MATICUSDT":"matic-network","DOTUSDT":"polkadot","LINKUSDT":"chainlink","DOGEUSDT":"dogecoin",
    "ADAUSDT":"cardano","BNBUSDT":"binancecoin"
}

def coingecko_ohlcv(symbol, minutes=KLIMIT):
    cg_id = state.get('symbols_info', {}).get(symbol, {}).get('cg_id') or COINGECKO_SIMPLE_MAP.get(symbol)
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
    except Exception:
        pass
    df = fetch_klines_best(symbol)
    if df is not None:
        return float(df['close'].iloc[-1])
    return None

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

# ---------------- Targets & stop (ATR or percentages) ----------------
def compose_targets_and_stop(entry, df_for_atr, side='LONG', leverage=20):
    atr = None
    try:
        if df_for_atr is not None and len(df_for_atr) >= 14:
            closes = df_for_atr['close'].astype(float)
            high = df_for_atr['high'].astype(float)
            low = df_for_atr['low'].astype(float)
            atr = float(ta.volatility.average_true_range(high, low, closes, window=14).iloc[-1])
    except Exception:
        atr = None
    if atr and atr>0:
        mults = [0.5,1,1.5,2,3,4,6]
        if side=='LONG':
            tps = [round(entry + atr*m, 8) for m in mults]
            stop = round(entry - atr*1.5, 8)
        else:
            tps = [round(entry - atr*m, 8) for m in mults]
            stop = round(entry + atr*1.5, 8)
    else:
        percents = [0.0025,0.005,0.01,0.015,0.025,0.04,0.06]
        if side=='LONG':
            tps = [round(entry*(1+p),8) for p in percents]
            stop = round(entry*(1-0.01),8)
        else:
            tps = [round(entry*(1-p),8) for p in percents]
            stop = round(entry*(1+0.01),8)
    return tps, stop

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

def publish_alert(sym, features, reasons, level="ALERT"):
    msg = f"{'🚨 STRONG SIGNAL 🚨' if level=='SIGNAL' else '⚡ ALERT ⚡'}\n{sym}\nLevel: {level}\nScore: {features.get('score','?')}\nPrice: {features['last']}\nvol_mult: {features['vol_mult']:.2f}\naccel: {features['price_accel']:.4f}\nReasons: {', '.join(reasons)}\n(DRY_RUN={DRY_RUN})\n{nowstr()}"
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

def publish_trade(sym, features, reasons, df_for_atr, leverage=20):
    entry = features['last']
    side = 'LONG' if features['price_accel'] >= 0 else 'SHORT'
    tps, stop = compose_targets_and_stop(entry, df_for_atr, side=side, leverage=leverage)
    msg = f"💥 NEW TRADE 💥\n{sym}\nSide: {side}\nEntry: {entry}\nLeverage: {leverage}x\n\nTake Profits:\n"
    for i,tp in enumerate(tps, start=1):
        pct = ((tp-entry)/entry*100) if side=='LONG' else ((entry-tp)/entry*100)
        msg += f"• TP{i}: {tp} ({pct:.2f}%)\n"
    stop_pct = ((stop-entry)/entry*100) if side=='LONG' else ((entry-stop)/entry*100)
    msg += f"\n⛔ STOP: {stop} ({stop_pct:.2f}%)\n\nReasons: {', '.join(reasons)}\n(DRY_RUN={DRY_RUN})\n{nowstr()}"
    ok = tg_send(msg)
    if ok:
        active = {"symbol":sym,"entry":entry,"side":side,"tps":tps,"stop":stop,"hit":[False]*len(tps),
                  "opened_at":nowts(),"leverage":leverage}
        state.setdefault('active_trades',[]).append(active)
        save_state(state)
        if not DRY_RUN and ENABLE_EXECUTION:
            try:
                execute_order(sym, side, entry, leverage=leverage)
            except Exception as e:
                log.exception("Execution attempt failed")
        return True
    return False

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
            for idx, tp in enumerate(tps):
                if not hit[idx]:
                    if (side=='LONG' and price >= tp) or (side=='SHORT' and price <= tp):
                        hit[idx] = True
                        trade['hit'] = hit
                        tg_send(f"✅ TP{idx+1} HIT — {sym}\nTP: {tp}\nEntry: {entry}\nNow: {price}\n{nowstr()}")
                        changed = True
            if (side=='LONG' and price <= stop) or (side=='SHORT' and price >= stop):
                tg_send(f"⛔ STOP HIT — {sym}\nStop: {stop}\nEntry: {entry}\nNow: {price}\n{nowstr()}")
                state['active_trades'].remove(trade)
                changed = True
                continue
            if all(hit):
                tg_send(f"🏁 ALL TPs HIT — {sym}\nEntry: {entry}\nFinal: {price}\n{nowstr()}")
                state['active_trades'].remove(trade)
                changed = True
        except Exception:
            log.exception("monitor error for %s", sym)
            continue
    if changed:
        save_state(state)

# ---------------- Optional execution placeholder ----------------
def execute_order(symbol, side, entry_price, size=None, leverage=20):
    if not ENABLE_EXECUTION:
        log.info("EXECUTION disabled — not placing real order.")
        return {"status":"skipped"}
    raise NotImplementedError("Real execution not implemented.")

# ---------------- Main scanning loop ----------------
def main_loop():
    log.info("PumpHunter شغال 24/24! DRY_RUN=%s", DRY_RUN)
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

                    # Debug print (detailed)
                    log.info("%s | score=%s | vol_mult=%.2f | accel=%.6f | rsi=%s | last=%.8f",
                             sym, score, feats['vol_mult'], feats['price_accel'],
                             ("%.2f" % feats['rsi']) if feats['rsi'] is not None else "n/a", feats['last'])

                    # Explain why not publishing (debug)
                    publish_reason = []
                    if score < SCORE_ALERT:
                        publish_reason.append("score<alert")
                    if feats['vol_mult'] < VOL_MULT_WEAK:
                        publish_reason.append("vol low")
                    if feats['price_accel'] < PRICE_ACCEL_THRESHOLD/2:
                        publish_reason.append("accel low")
                    if not can_publish(sym):
                        publish_reason.append("daily limit reached")

                    # Publish logic (safe)
                    if score >= SCORE_SIGNAL and can_publish(sym):
                        published = publish_trade(sym, feats, reasons, df_for_atr=df, leverage=20)
                        if published:
                            log.info("Published TRADE for %s (score=%s reasons=%s)", sym, score, reasons)
                        else:
                            log.warning("Failed to publish TRADE for %s", sym)
                    elif score >= SCORE_ALERT and can_publish(sym):
                        ok = publish_alert(sym, feats, reasons, level="ALERT")
                        if ok:
                            log.info("Published ALERT for %s (score=%s reasons=%s)", sym, score, reasons)
                    else:
                        # debug message (no publish)
                        log.debug("No publish for %s -> %s", sym, "; ".join(publish_reason) or "no_publish_branch")

                    time.sleep(0.15)  # small throttle
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
