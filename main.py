#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# PumpHunter v4.1 - Early-detection, improved notification format

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
        "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
        "ADAUSDT","DOGEUSDT","DOTUSDT","AVAXUSDT","LINKUSDT",
        "LTCUSDT","MATICUSDT","TRXUSDT","UNIUSDT","ALGOUSDT",
        "XLMUSDT","FTTUSDT","NEARUSDT","ATOMUSDT","APTUSDT",
        "SUIUSDT","ARBUSDT","HBARUSDT","ICPUSDT","FILUSDT",
        "THETAUSDT","EOSUSDT","XTZUSDT","CHZUSDT","MANAUSDT"
    ]

KLIMIT = int(os.environ.get("KLIMIT", 120))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", 30))
THREADS = int(os.environ.get("THREADS", 15))
MIN_CANDLES = int(os.environ.get("MIN_CANDLES", 30))
BINANCE_CACHE_TTL = int(os.environ.get("BINANCE_CACHE_TTL", 3))

# thresholds
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
    # Preview log
    log.info("TG PREVIEW: %s", msg.replace("\n"," | ")[:400])
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

# ... [الكود كامل كما في نسختك الأصلية، ماعدا إصلاحات السلاسل النصية]

# ---------- publishing helpers ----------
CIRCLED = ['①','②','③','④','⑤','⑥','⑦']

def publish_trade(sym, features, reasons, price, score):
    if not can_publish(sym):
        log.info("Publish blocked by rate limit: %s", sym)
        return False
    leverage = get_leverage_for_score(score)
    if leverage is None:
        log.info("No leverage allocated for score %s", score)
        return False
    side = 'LONG' if features['price_accel'] >= 0 else 'SHORT'
    tps, stop = compose_targets_and_stop(price, side=side, score=score, leverage=leverage)
    lines = []
    lines.append(f"${sym}")
    lines.append(f"{ 'Long' if side=='LONG' else 'Short' } Cross {leverage}x")
    lines.append("🟢Entry: " + format_price(price))
    lines.append("")
    lines.append("Targets:")
    for i,tp in enumerate(tps[:7]):
        lines.append(f"{CIRCLED[i]} {format_price(tp)}")
    lines.append("")
    lines.append("⛔Stop: " + format_price(stop))
    msg = "\n".join(lines)   # ✅ تم إصلاحها
    if tg_send(msg):
        register_publish(sym)
        active = {"symbol":sym, "entry":price, "side":side, "tps":tps, "stop":stop, "hit":[False]*len(tps), "opened_at":nowts(), "leverage":leverage, "tp_hit_any":False}
        state.setdefault('active_trades', []).append(active); save_state(state); return True
    return False

def publish_alert(sym, features, reasons, level='ALERT'):
    if not can_publish(sym): return False
    msg = f"{'⚡ SIGNAL' if level=='SIGNAL' else '⚠️ ALERT'} {sym}\nScore: {features.get('score','?')}\nPrice: {format_price(features.get('last'))}\nReasons: {', '.join(reasons)}\n{nowstr()}"
    ok = tg_send(msg)
    if ok: register_publish(sym)
    return ok

# ... [الباقي بدون تغيير، فقط عدلت كل الرسائل بنفس الأسلوب: "\n".join بدل كسر السطر الغلط]
def main_loop():
    log.info("Bot starting (PumpHunter V4) ...")
    while True:
        try:
            results = scan_symbols()
            if results:
                for sym, features, reasons in results:
                    score = features.get("score", 0)
                    price = features.get("last")
                    if score >= SCORE_SIGNAL:
                        publish_trade(sym, features, reasons, price, score)
                    elif score >= SCORE_ALERT:
                        publish_alert(sym, features, reasons, "ALERT")
                    elif score >= PRE_SIGNAL:
                        publish_alert(sym, features, reasons, "PRE")
            # save state
            save_state(state)
            log.info("Cycle complete. Sleeping %.1f s", POLL_SECONDS)
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            log.info("CTRL-C received, exiting...")
            break
        except Exception as e:
            log.exception("Loop error")
            time.sleep(5)


if __name__ == '__main__':
    main_loop()

