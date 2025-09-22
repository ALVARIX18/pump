import time
import requests
import pandas as pd
import numpy as np
import ta

# ==============================
# إعدادات Binance (زيد API Keys هنا إذا حاب)
# ==============================
BINANCE_API_KEY = "ضع_المفتاح_هنا"
BINANCE_API_SECRET = "ضع_السر_هنا"

# ==============================
# إعدادات التلغرام
# ==============================
TELEGRAM_BOT_TOKEN = "ضع_توكن_البوت"
TELEGRAM_CHAT_ID = "ضع_CHAT_ID"

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("خطأ في إرسال التلغرام:", e)

# ==============================
# الدالة الرئيسية
# ==============================
def main():
    while True:
        msg = "🚀 PumpHunter شغال 24/24!"
        print(msg)
        send_telegram(msg)
        time.sleep(60)  # يرسل كل دقيقة (بدلها بالمنطق تاع الصفقات)

if __name__ == "__main__":
    main()
