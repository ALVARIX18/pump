import time, threading, json, websocket, requests
import pandas as pd
import numpy as np
from datetime import datetime

# ================= CONFIG =================
API_URL = "https://api.binance.com/api/v3/klines"
TELEGRAM_BOT_TOKEN = "8294324404:AAGyA3W6S3E98TaqOgc9iQpPVOcAqlJ2Ung"
TELEGRAM_CHAT_ID = "-1003089256716"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","ADAUSDT","SOLUSDT","AVAXUSDT","MATICUSDT","DOGEUSDT","LTCUSDT",
    "DOTUSDT","LINKUSDT","TRXUSDT","ATOMUSDT","NEARUSDT","SUIUSDT","APTUSDT","FTMUSDT","GALAUSDT","APEUSDT",
    "SANDUSDT","MANAUSDT","ALGOUSDT","LDOUSDT","OPUSDT","IMXUSDT","UNIUSDT","AAVEUSDT","ARBUSDT","INJUSDT",
    "RNDRUSDT","SEIUSDT","TIAUSDT","PEPEUSDT","BONKUSDT","JUPUSDT","PYTHUSDT","STRKUSDT","WUSDT","ENSUSDT",
    "ICXUSDT","ZILUSDT","CELOUSDT","XLMUSDT","VETUSDT","EGLDUSDT","THETAUSDT","FLOWUSDT","HBARUSDT","CHRUSDT",
    "KAVAUSDT","RUNEUSDT","COMPUSDT","CAKEUSDT","1INCHUSDT","OCEANUSDT","CHZUSDT","GRTUSDT","ANKRUSDT","ROSEUSDT",
    "STORJUSDT","SXPUSDT","FLMUSDT","YFIUSDT","BALUSDT","LRCUSDT","RENUSDT","BANDUSDT","DASHUSDT","ZRXUSDT",
    "HOTUSDT","NKNUSDT","CELRUSDT","CTSIUSDT","ARUSDT","GLMRUSDT","DARUSDT","HIGHUSDT","ACHUSDT","IDEXUSDT",
    "MASKUSDT","HOOKUSDT","MAGICUSDT","OPUSDT","RDNTUSDT","LINAUSDT","CFXUSDT","STGUSDT","ILVUSDT","ALPHAUSDT",
    "QTUMUSDT","BLZUSDT","SNTUSDT","MTLUSDT","WAXPUSDT","TRBUSDT","DODOUSDT","OGNUSDT","AKROUSDT","COTIUSDT",
    "FETUSDT","SKLUSDT","TLMUSDT","UMAUSDT","SUNUSDT","ANTUSDT","BICOUSDT","C98USDT","RNXUSDT","KSMUSDT","ZECUSDT",
    "ZENUSDT","OMGUSDT","IOTAUSDT","KLAYUSDT","DENTUSDT","SCUSDT","XEMUSDT","BTGUSDT","ICPUST","CKBUSDT"
]  # أكثر من 140 رمز

SCAN_INTERVAL_SEC = 60
RSI_PERIOD = 14
EMA_FAST = 9
EMA_SLOW = 21

# ================= GLOBAL STATE =================
last_prices = {}
active_trades = {}
state_lock = threading.Lock()

# ================= HELPERS =================
def log(msg):
    print(f"[{datetime.now().isoformat()}] {msg}")

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
        requests.post(url, data=data)
        log(f"[SENT] {msg[:50]}...")
    except Exception as e:
        log(f"[TELEGRAM ERROR] {e}")

def get_klines(symbol, interval="1m", limit=100):
    try:
        url = f"{API_URL}?symbol={symbol}&interval={interval}&limit={limit}"
        data = requests.get(url, timeout=10).json()
        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume","ct","qav","n","tbbav","tbqav","ignore"
        ])
        df["close"] = df["close"].astype(float)
        return df
    except Exception as e:
        log(f"Error fetching {symbol}: {e}")
        return None

def rsi(series, period=14):
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ma_up = up.ewm(com=(period-1), min_periods=period).mean()
    ma_down = down.ewm(com=(period-1), min_periods=period).mean()
    rs = ma_up / ma_down
    return 100 - (100 / (1 + rs))

def analyze(symbol):
    df = get_klines(symbol)
    if df is None or len(df) < RSI_PERIOD:
        return None
    close = df["close"]
    ema_fast = close.ewm(span=EMA_FAST).mean()
    ema_slow = close.ewm(span=EMA_SLOW).mean()
    rsi_val = rsi(close, RSI_PERIOD).iloc[-1]

    signal = None
    if ema_fast.iloc[-1] > ema_slow.iloc[-1] and rsi_val < 70:
        signal = "LONG"
    elif ema_fast.iloc[-1] < ema_slow.iloc[-1] and rsi_val > 30:
        signal = "SHORT"

    if signal:
        entry = close.iloc[-1]
        # حساب الأهداف
        step = entry * 0.01
        if signal == "LONG":
            targets = [entry + step*i for i in range(1, 6)]
            stop = entry - step*2
        else:
            targets = [entry - step*i for i in range(1, 6)]
            stop = entry + step*2

        score = abs(rsi_val - 50)
        leverage = min(max(int(score//2), 20), 75)

        return {
            "symbol": symbol,
            "dir": signal,
            "entry": entry,
            "targets": targets,
            "stop": stop,
            "leverage": leverage
        }
    return None

def build_msg(trade):
    tps = "\n".join([f"{i+1}) {t:.5f}" for i, t in enumerate(trade["targets"])])
    msg = f"""🔥 {trade['symbol']}
{trade['dir']}
Leverage: Cross {trade['leverage']}×

🟢 ENTRY: {trade['entry']:.5f}

🎯 Take profit:
{tps}

⛔ STOP: {trade['stop']:.5f}"""
    return msg

# ================= MONITOR TRADES =================
def monitor_trades():
    global active_trades
    while True:
        try:
            with state_lock:
                items = list(active_trades.items())

            for sym, trade in items:
                price = last_prices.get(sym)
                if not price:
                    continue

                entry = trade["entry"]
                lev = trade["leverage"]

                for i, tgt in enumerate(trade.get("targets", []), start=1):
                    if trade["dir"] == "LONG" and price >= tgt:
                        profit = (tgt - entry) / entry * 100 * lev
                        msg = f"✅ {sym} وصل للهدف {i} 🎯 عند {tgt:.5f}\nالربح: {profit:.2f}%"
                        send_telegram(msg)
                        trade["targets"] = trade["targets"][i:]
                        break
                    elif trade["dir"] == "SHORT" and price <= tgt:
                        profit = (entry - tgt) / entry * 100 * lev
                        msg = f"✅ {sym} وصل للهدف {i} 🎯 عند {tgt:.5f}\nالربح: {profit:.2f}%"
                        send_telegram(msg)
                        trade["targets"] = trade["targets"][i:]
                        break

                stop = trade.get("stop")
                if stop:
                    if trade["dir"] == "LONG" and price <= stop:
                        send_telegram(f"⛔ {sym} ضرب ستوب عند {stop:.5f}")
                        with state_lock: active_trades.pop(sym, None)
                    elif trade["dir"] == "SHORT" and price >= stop:
                        send_telegram(f"⛔ {sym} ضرب ستوب عند {stop:.5f}")
                        with state_lock: active_trades.pop(sym, None)

        except Exception as e:
            log(f"[monitor_trades] error: {e}")
        time.sleep(5)

# ================= WS PRICE UPDATES =================
def on_message(ws, message):
    data = json.loads(message)
    s = data.get("s")
    p = float(data.get("c", 0))
    last_prices[s] = p

def start_ws(symbols):
    streams = "/".join([f"{s.lower()}@ticker" for s in symbols])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"
    ws = websocket.WebSocketApp(url, on_message=on_message)
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ================= MAIN LOOP =================
def scan_loop():
    while True:
        try:
            for sym in SYMBOLS:
                trade = analyze(sym)
                if trade:
                    msg = build_msg(trade)
                    send_telegram(msg)
                    with state_lock:
                        active_trades[sym] = trade
            time.sleep(SCAN_INTERVAL_SEC)
        except Exception as e:
            log(f"[scan_loop] error: {e}")

# ================= START =================
def main():
    log("🚀 Starting pro_signal_bot...")
    threading.Thread(target=monitor_trades, daemon=True).start()
    start_ws(SYMBOLS)
    scan_loop()

if __name__ == "__main__":
    main()
