#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PumpHunter v3.1 - جاهز للنسخ/اللصق
# - ذكي، يعمل 24/7 على سيرفر (Railway / VPS). يرسل إشعارات للتلغرام.
# - الوضع الآمن: DRY_RUN=False لإرسال رسائل حقيقية. *ضع توكن التلغرام في متغيرات البيئة* أو ضعها مباشرة (تحذير أدناه).
# - يتضمن: فلتر دفتر الأوامر (pulse), حساب score, رافعة ديناميكية حسب الجودة، 7 TP + SL، تتبع الصفقات.
# - تعليمات سريعة: ضع هذا الملف main.py في repo، ضبط متغيرات البيئة TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID في Railway/Host،
#   ثم شغّل: python main.py
#
# ⚠️ أمان: لا تترك مفاتيح سرية في الريبو. استخدم متغيرات بيئة. إن أردت وضع المفاتيح مباشرة مؤقتاً - افعل ذلك محلياً ثم حذفه فوراً.
# ======================================================================

import os, sys, time, math, json, logging, warnings
from datetime import datetime, timezone
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
log = logging.getLogger("PumpHunter")
warnings.filterwarnings("ignore", category=DeprecationWarning)

# ---------------- CONFIG (عدل هنا فقط إن أردت) ----------------
# الوضع: إذا True => يطبع معاينات فقط ولا يرسل. إذا False => يرسل للتلغرام (تأكد من مفاتيح التلغرام تحت).
DRY_RUN = False  # ضع False لترسل إشعارات حقيقية

# ⚠️ ضع مفاتيح التلغرام كمُتغيرات بيئة في Railway/Host لتجنب فضح مفاتيحك:
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
#
# إذا تفضّل وضعها مباشرة (غير مستحسن للريبو العام)، ازل التعليق وأدخل: "BOT:TOKEN" و "CHAT_ID"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", None)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", None)
# مثال لتجربة محلية مؤقتة (غير موصى به لهذا الريبو):
# TELEGRAM_BOT_TOKEN = "PUT_YOUR_TOKEN_HERE"
# TELEGRAM_CHAT_ID = "PUT_YOUR_CHAT_ID_HERE"

# قائمة العملات التي يتم مسحها (يمكن تغييرها كسلسلة مفصولة بفواصل في متغيرات البيئة)
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "SOLUSDT",
    "XRPUSDT", "DOGEUSDT", "TRXUSDT", "LTCUSDT", "MATICUSDT",
    "AVAXUSDT", "DOTUSDT", "SHIBUSDT", "UNIUSDT", "LINKUSDT",
    "ATOMUSDT", "ALGOUSDT", "MANAUSDT", "SANDUSDT", "NEARUSDT",
    "VETUSDT", "XMRUSDT", "XLMUSDT", "FTMUSDT", "APEUSDT"
]
# إعدادات ضبط السلوك
MIN_CANDLES = int(os.environ.get("MIN_CANDLES", 30))
KLIMIT = int(os.environ.get("KLIMIT", 60))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", 60))

VOL_MULT_STRONG = float(os.environ.get("VOL_MULT_STRONG", 1.8))
VOL_MULT_WEAK = float(os.environ.get("VOL_MULT_WEAK", 1.2))
PRICE_ACCEL_THRESHOLD = float(os.environ.get("PRICE_ACCEL_THRESHOLD", 0.007))
SCORE_ALERT = int(os.environ.get("SCORE_ALERT", 45))
SCORE_SIGNAL = int(os.environ.get("SCORE_SIGNAL", 50))

MAX_ALERTS_PER_SYMBOL_PER_DAY = int(os.environ.get("MAX_ALERTS_PER_SYMBOL_PER_DAY", 1))
MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", 7))

ENABLE_PULSE_FILTER = os.environ.get("ENABLE_PULSE_FILTER", "1") != "0"
PULSE_OB_IMB_THRESHOLD = float(os.environ.get("PULSE_OB_IMB_THRESHOLD", 0.30))
PULSE_TAKER_THRESHOLD = float(os.environ.get("PULSE_TAKER_THRESHOLD", 0.30))

PRICE_DECIMALS = int(os.environ.get("PRICE_DECIMALS", 6))
MIN_TP_PCT = float(os.environ.get("MIN_TP_PCT", 0.001))

STATE_FILE = os.environ.get("STATE_FILE", "pumphunter_state_v3.json")
BINANCE_CACHE_TTL = int(os.environ.get("BINANCE_CACHE_TTL", 5))

# ---------------- Helpers ----------------
def nowts(): return int(time.time())
def nowstr(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def format_price(x, decimals=PRICE_DECIMALS):
    try:
        v = float(x)
        s = f"{v:.{decimals}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
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
    # always preview in logs
    log.info("TG PREVIEW: %s", msg.replace("\n", " | ")[:500])
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
    return {"alerts": {}, "active_trades": [], "history": [], "binance_cache": {}, "daily_trades": {}}

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

def fetch_klines_binance_with_retry(symbol, interval='1m', limit=KLIMIT, retries=2, backoff=0.8):
    key = f"{symbol}|{interval}|{limit}"
    cinfo = state.get('binance_cache', {}).get(key)
    now = nowts()
    if cinfo and (now - cinfo.get('ts', 0) < BINANCE_CACHE_TTL):
        try:
            df = pd.read_json(cinfo['data'])
            df.index = pd.to_datetime(df.index)
            return df
        except Exception as e:
            if DEBUG: _log_exception_short("cache decode error", e)
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
                state.setdefault('binance_cache', {})[key] = {'ts': nowts(), 'data': df.to_json()}
                save_state(state)
                return df
            except Exception as e:
                if DEBUG: _log_exception_short("kline parse error", e)
        time.sleep(backoff * (attempt + 1))
    return None

def coingecko_ohlcv(symbol, minutes=KLIMIT):
    COINGECKO_SIMPLE_MAP = {
        "BTCUSDT":"bitcoin","ETHUSDT":"ethereum","SOLUSDT":"solana","AVAXUSDT":"avalanche-2",
        "MATICUSDT":"matic-network","DOTUSDT":"polkadot","LINKUSDT":"chainlink","DOGEUSDT":"dogecoin",
        "ADAUSDT":"cardano","BNBUSDT":"binancecoin"
    }
    cg_id = COINGECKO_SIMPLE_MAP.get(symbol)
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

# ---------- Pulse (orderbook + trades) ----------
def fetch_orderbook(symbol, limit=10):
    try:
        r = requests.get('https://api.binance.com/api/v3/depth', params={'symbol':symbol,'limit':limit}, timeout=6)
        if r.ok: return r.json()
    except Exception as e:
        if DEBUG: _log_exception_short("orderbook fetch", e)
    return None

def fetch_recent_trades(symbol, limit=100):
    try:
        r = requests.get('https://api.binance.com/api/v3/trades', params={'symbol':symbol,'limit':limit}, timeout=6)
        if r.ok: return r.json()
    except Exception as e:
        if DEBUG: _log_exception_short("recent trades fetch", e)
    return None

def compute_pulse(ob, trades):
    try:
        if not ob or not trades: return 0.0, 0.0
        top_n = min(10, len(ob.get('bids',[])), len(ob.get('asks',[])))
        bid_vol = sum(float(q) for p,q in ob['bids'][:top_n])
        ask_vol = sum(float(q) for p,q in ob['asks'][:top_n])
        ob_imb = (bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-9)
        buy_taker = sell_taker = 0.0
        for t in trades:
            q = float(t.get('qty', t.get('quantity', 0)))
            if t.get('isBuyerMaker'):
                sell_taker += q
            else:
                buy_taker += q
        taker_ratio = (buy_taker - sell_taker) / (buy_taker + sell_taker + 1e-9)
        return ob_imb, taker_ratio
    except Exception as e:
        if DEBUG: _log_exception_short("compute_pulse", e)
        return 0.0, 0.0

# ---------- Features & scoring ----------
def compute_pump_features(df):
    closes = df['close'].astype(float)
    vols = df['volume'].astype(float)
    returns = closes.pct_change().fillna(0)
    last_price = float(closes.iloc[-1])
    vol_mean = vols.tail(30).mean() if len(vols) >= 30 else (vols.mean() if len(vols) > 0 else 0.0)
    vol_last = float(vols.iloc[-1]) if len(vols) > 0 else 0.0
    vol_mult = (vol_last / (vol_mean + 1e-9)) if vol_mean > 0 else 0.0
    price_accel = float(returns.tail(3).sum()) if len(returns) >= 3 else float(returns.sum())
    recent_return_5 = float(returns.tail(5).sum()) if len(returns) >= 5 else float(returns.sum())
    rsi = None
    try:
        if ta: rsi = float(ta.momentum.rsi(closes, window=14).iloc[-1])
    except Exception:
        rsi = None
    vol_std = float(returns.tail(30).std()) if len(returns) >= 30 else 0.0
    return {"last": last_price, "vol_last": vol_last, "vol_mean": float(vol_mean), "vol_mult": vol_mult,
            "price_accel": price_accel, "recent_return_5": recent_return_5, "rsi": rsi, "vol_std": vol_std}

def compute_pump_score(features):
    score = 0
    reasons = []
    v = features.get('vol_mult', 0)
    if v >= VOL_MULT_STRONG:
        score += 40; reasons.append("vol_strong")
    elif v >= VOL_MULT_WEAK:
        score += 20; reasons.append("vol_med")
    if features.get('price_accel', 0) >= PRICE_ACCEL_THRESHOLD:
        score += 30; reasons.append("accel_high")
    elif features.get('price_accel', 0) >= PRICE_ACCEL_THRESHOLD / 2:
        score += 10; reasons.append("accel_mild")
    if features.get('recent_return_5', 0) > 0.01:
        score += 10; reasons.append("recent5>1%")
    r = features.get('rsi')
    if r is not None:
        if r < 40:
            score += 5; reasons.append("RSI_low")
        elif r > 80:
            score -= 20; reasons.append("RSI_extreme")
    if features.get('vol_std', 0) > 0.001:
        score += min(10, int(features['vol_std'] * 1000)); reasons.append("volatility")
    return max(0, min(100, int(score))), reasons

# ---------- Leverage & targets ----------
def get_leverage_for_score(score):
    if score >= 90: return 100
    if score >= 80: return 50
    if score >= 70: return 25
    if score >= 60: return 20
    if score >= 40: return 10   # <--- أضف هذا
    return None

def compose_targets_and_stop(entry, df_for_atr, side='LONG', score=0, leverage=20):
    if score >= 80:
        percents = [0.02, 0.05, 0.08, 0.12, 0.18, 0.30, 0.50]
    elif score >= 70:
        percents = [0.01, 0.02, 0.035, 0.06, 0.09, 0.14, 0.22]
    else:
        percents = [0.003, 0.006, 0.01, 0.015, 0.02, 0.03, 0.05]

    atr = None
    try:
        if ta and df_for_atr is not None and len(df_for_atr) >= 14:
            closes = df_for_atr['close'].astype(float)
            high = df_for_atr['high'].astype(float)
            low = df_for_atr['low'].astype(float)
            atr = float(ta.volatility.average_true_range(high, low, closes, window=14).iloc[-1])
            if atr and atr > 0:
                price = float(closes.iloc[-1])
                atr_pct = atr / (price + 1e-9)
                for i in range(len(percents)):
                    min_pct = atr_pct * max(0.8, (i+1)*0.6)
                    if percents[i] < min_pct:
                        percents[i] = min_pct
    except Exception as e:
        if DEBUG: _log_exception_short("ATR compute error", e)
        atr = None

    tps = []
    last = entry
    for p in percents:
        if side == 'LONG':
            tp = last * (1 + p)
        else:
            tp = last * (1 - p)
        pct = abs((tp - last) / (entry + 1e-9))
        if pct < MIN_TP_PCT:
            if side == 'LONG':
                tp = last * (1 + MIN_TP_PCT)
            else:
                tp = last * (1 - MIN_TP_PCT)
        tps.append(round(tp, PRICE_DECIMALS))
        last = tps[-1]

    if atr and atr > 0:
        stop = (entry - atr * 1.5) if side == 'LONG' else (entry + atr * 1.5)
    else:
        stop = (entry * (1 - 0.01)) if side == 'LONG' else (entry * (1 + 0.01))
    stop = round(stop, PRICE_DECIMALS)
    return [float(f"{tp:.{PRICE_DECIMALS}f}") for tp in tps], float(stop)

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

def daily_trades_count():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return state.get('daily_trades', {}).get(today, 0)

def register_daily_trade():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d = state.setdefault('daily_trades', {})
    d[today] = d.get(today, 0) + 1
    save_state(state)

def publish_trade(sym, features, reasons, df_for_atr, score):
    for t in state.get('active_trades', []):
        if t.get('symbol') == sym:
            log.info("Skip: active trade exists for %s", sym)
            return False
    if daily_trades_count() >= MAX_TRADES_PER_DAY:
        log.info("Skip: daily trades limit reached (%s)", MAX_TRADES_PER_DAY)
        return False

    leverage = get_leverage_for_score(score)
    if leverage is None:
        log.info("Skip: score %s too low for leverage", score)
        return False

    entry = float(features['last'])
    side = 'LONG' if features['price_accel'] >= 0 else 'SHORT'
    tps, stop = compose_targets_and_stop(entry, df_for_atr, side=side, score=score, leverage=leverage)

    # compose message
    msg_lines = [f"${sym}", f"{side} Cross {leverage}x", f"🟢Entry: {format_price(entry)}", "", "Targets:"]
    for idx, tp in enumerate(tps, start=1):
        msg_lines.append(f"{idx}. {format_price(tp)}")
    msg_lines.append("")
    msg_lines.append(f"⛔Stop: {format_price(stop)}")
    msg_lines.append("")
    msg_lines.append("Reasons: " + (", ".join(reasons) if reasons else "n/a"))
    msg_lines.append(f"(DRY_RUN={DRY_RUN})")
    msg_lines.append(nowstr())
    msg = "\n".join(msg_lines)

    ok = tg_send(msg)
    if ok:
        active = {"symbol": sym, "entry": entry, "side": side, "tps": tps, "stop": stop, "hit": [False]*len(tps),
                  "opened_at": nowts(), "leverage": leverage, "tp_hit_any": False}
        state.setdefault('active_trades', []).append(active)
        register_daily_trade()
        save_state(state)
        return True
    return False

def publish_alert(sym, features, reasons, level="ALERT"):
    msg = (f"{'🚨 STRONG' if level=='SIGNAL' else '⚡ ALERT'}\n{sym}\nLevel: {level}\nScore: {features.get('score','?')}\n"
           f"Price: {format_price(features.get('last'))}\nvol_mult: {features['vol_mult']:.2f}\naccel: {features['price_accel']:.4f}\nReasons: {', '.join(reasons)}\n"
           f"(DRY_RUN={DRY_RUN})\n{nowstr()}")
    ok = tg_send(msg)
    if ok:
        pub = state.setdefault('alerts', {})
        daily = pub.get(sym, {"count": 0, "last": 0})
        if nowts() - daily.get("last", 0) > 24*3600:
            daily = {"count": 0, "last": 0}
        daily['count'] = daily.get('count', 0) + 1
        daily['last'] = nowts()
        pub[sym] = daily
        save_state(state)
    return ok

# ---------- Monitor ---------- 
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
            tps = trade.get('tps', [])
            stop = trade.get('stop')
            hit = trade.get('hit', [False]*len(tps))

            for idx, tp in enumerate(tps):
                if not hit[idx]:
                    if (side == 'LONG' and price >= tp) or (side == 'SHORT' and price <= tp):
                        hit[idx] = True
                        trade['hit'] = hit
                        trade['tp_hit_any'] = True
                        tg_send(f"✅ TP{idx+1} HIT — {sym}\nTP: {format_price(tp)}\nEntry: {format_price(entry)}\nNow: {format_price(price)}\n{nowstr()}")
                        changed = True

            if (side == 'LONG' and price <= stop) or (side == 'SHORT' and price >= stop):
                if not trade.get('tp_hit_any', False):
                    tg_send(f"⛔ STOP HIT — {sym}\nStop: {format_price(stop)}\nEntry: {format_price(entry)}\nNow: {format_price(price)}\n{nowstr()}")
                state['active_trades'].remove(trade)
                rec = {"symbol": sym, "side": side, "entry": entry, "exit": price, "reason": "STOP",
                       "opened_at": trade.get('opened_at'), "closed_at": nowts(), "hit": hit}
                state.setdefault('history', []).append(rec)
                changed = True
                continue

            if all(hit):
                tg_send(f"🏁 ALL TPs HIT — {sym}\nEntry: {format_price(entry)}\nFinal: {format_price(price)}\n{nowstr()}")
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

# ---------- Execution placeholder ----------
def execute_order(symbol, side, entry_price, size=None, leverage=20):
    if not os.environ.get("ENABLE_EXECUTION", "0") == "1":
        log.info("EXECUTION disabled (env ENABLE_EXECUTION!=1)")
        return {"status": "skipped"}
    raise NotImplementedError("Real execution not implemented here. Implement your exchange client safely.")

# ---------- Main Loop ----------
def main_loop():
    log.info("PumpHunter v3.1 starting. DRY_RUN=%s DEBUG=%s", DRY_RUN, DEBUG)
    if DRY_RUN:
        log.info("DRY_RUN is ON: script will not send real Telegram messages or place orders.")
    else:
        log.info("DRY_RUN is OFF: attempting real Telegram sends (ensure TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID are set).")

    cycle = 0
    try:
        while True:
            cycle += 1
            start = nowts()
            log.info("--- Cycle %s | %s ---", cycle, nowstr())

            for sym in SYMBOLS:
                sym = sym.strip().upper()
                if not sym: continue
                try:
                    df = fetch_klines_best(sym)
                    if df is None or len(df) < MIN_CANDLES:
                        log.debug("%s: no klines (skip)", sym)
                        continue

                    feats = compute_pump_features(df)
                    score, reasons = compute_pump_score(feats)
                    feats['score'] = score

                    # pulse filter
                    pulse_ok = True
                    ob_imb = taker = 0.0
                    if ENABLE_PULSE_FILTER:
                        ob = fetch_orderbook(sym, limit=10)
                        trades = fetch_recent_trades(sym, limit=100)
                        ob_imb, taker = compute_pulse(ob, trades)
                        pulse_ok = (abs(ob_imb) >= PULSE_OB_IMB_THRESHOLD) or (abs(taker) >= PULSE_TAKER_THRESHOLD)

                    log.info("%s | score=%s | vol_mult=%.2f | accel=%.6f | rsi=%s | last=%s | ob_imb=%.3f taker=%.3f",
                             sym, score, feats['vol_mult'], feats['price_accel'],
                             ("%.2f" % feats['rsi']) if feats['rsi'] is not None else "n/a", format_price(feats['last']),
                             ob_imb, taker)

                    if score >= SCORE_SIGNAL and can_publish(sym) and pulse_ok:
                        published = publish_trade(sym, feats, reasons, df_for_atr=df, score=score)
                        if published:
                            log.info("Published TRADE for %s (score=%s reasons=%s)", sym, score, reasons)
                        else:
                            log.info("Publish skipped/failed for %s", sym)
                    elif score >= SCORE_ALERT and can_publish(sym) and pulse_ok:
                        ok = publish_alert(sym, feats, reasons, level="ALERT")
                        if ok:
                            log.info("Published ALERT for %s (score=%s reasons=%s)", sym, score, reasons)

                    time.sleep(0.12)
                except Exception as e:
                    _log_exception_short(f"Scan error for {sym}", e)
                    continue

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
