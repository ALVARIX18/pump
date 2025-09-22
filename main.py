import time
import random

# إعدادات البوت
SCORE_SIGNAL = 12
SCORE_ALERT = 8
VOL_MULT_STRONG = 1.8
VOL_MULT_WEAK = 1.2
MAX_ALERTS_PER_SYMBOL_PER_DAY = 1
MAX_TRADES_PER_DAY = 7

def generate_trade(symbol):
    entry = round(random.uniform(0.01, 1.0), 6)
    targets = [round(entry * (1 + 0.01 * i), 6) for i in range(1, 8)]
    stop = round(entry * 0.98, 6)
    trade = f"""💥 NEW TRADE 💥
{symbol}USDT
Side: LONG
Entry: {entry}
Leverage: 20x

Take Profits:
{chr(10).join([f'• TP{i+1}: {t}' for i, t in enumerate(targets)])}

⛔ STOP: {stop}
"""
    return trade

if __name__ == "__main__":
    for i in range(3):
        print(generate_trade("TEST"))
        time.sleep(2)
