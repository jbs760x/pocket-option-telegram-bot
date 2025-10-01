import os, time, logging, requests
from datetime import datetime, timezone
from threading import Event, Thread
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ====== CONFIG ======
PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY"]   # pairs to scan
INTERVAL_SECONDS = 120                      # signals every 2 minutes
BET_AMOUNT = 5                              # $5 bet
BET_DURATION_SECONDS = 60                   # 60 seconds
TF = "1min"                                 # timeframe

# ====== YOUR KEYS (inline for easy mobile use) ======
TELEGRAM_TOKEN = "8471181182:AAEKGH1UASa5XvkXscb3jb5d1Yz19B8oJNM"
ALPHA_API_KEY  = "BM22MZEIOLL68RI6"
CHAT_ID        = "7814662315"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("bot")

# ====== Alpha Vantage Data Helper ======
def alpha_timeseries(symbol, interval="1min"):
    # Alpha Vantage uses "EURUSD" instead of "EUR/USD"
    sym = symbol.replace("/", "")
    url = f"https://www.alphavantage.co/query?function=FX_INTRADAY&from_symbol={sym[:3]}&to_symbol={sym[3:]}&interval={interval}&apikey={ALPHA_API_KEY}"
    r = requests.get(url, timeout=15)
    try:
        data = r.json()
    except:
        return None
    if "Time Series FX ("+interval+")" not in data: 
        return None
    ts = data["Time Series FX ("+interval+")"]
    # Convert to TwelveData-like format
    values = []
    for t, v in ts.items():
        values.append({
            "datetime": t,
            "open": v["1. open"],
            "high": v["2. high"],
            "low": v["3. low"],
            "close": v["4. close"],
            "volume": "0"
        })
    return values

# ====== Strategy ======
def calc_signal_from_candles(values):
    if not values or len(values) < 30: return None
    closes = [float(v["close"]) for v in values]
    opens  = [float(v["open"])  for v in values]
    def ema(series, n):
        k = 2/(n+1); e = series[0]; out=[e]
        for x in series[1:]:
            e = x*k + e*(1-k); out.append(e)
        return out
    ema_fast, ema_slow = ema(closes,5), ema(closes,14)

    gains, losses = [], []
    for i in range(1, 15):
        ch = closes[i-1] - closes[i]
        gains.append(max(ch,0)); losses.append(max(-ch,0))
    avg_gain = sum(gains)/14; avg_loss = sum(losses)/14 or 1e-9
    rs = avg_gain/avg_loss; rsi = 100 - (100/(1+rs))

    body = abs(closes[0]-opens[0])
    high, low = float(values[0]["high"]), float(values[0]["low"])
    rng = max(high-low,1e-9); momentum = min(body/rng,1.0)

    trend = 1 if ema_fast[0] > ema_slow[0] else -1
    rsi_bias = 1 if rsi >= 55 else (-1 if rsi <= 45 else 0)
    candle_dir = 1 if closes[0] > opens[0] else -1

    raw_score = trend + rsi_bias + candle_dir*momentum
    confidence = max(0, min(100, int((raw_score+3)/6*100)))
    side = "BUY (CALL)" if raw_score>0 else ("SELL (PUT)" if raw_score<0 else "NO TRADE")

    return {"side":side,"confidence":confidence,"rsi":round(rsi,1),"momentum":round(momentum,2),"trend":"Bull" if trend==1 else "Bear"}

def format_signal_msg(pair,sig):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if sig["side"]=="NO TRADE": return f"⏸️ {pair} | {TF} | {ts}\nNo clear edge (skip)."
    return (f"📈 Signal | {pair} ({TF})\n"
            f"• Recommendation: **{sig['side']}**\n"
            f"• Confidence: **{sig['confidence']}%**\n"
            f"• Trend: {sig['trend']} | RSI: {sig['rsi']} | Mom: {sig['momentum']}\n"
            f"• Suggested bet: ${BET_AMOUNT} | Duration: {BET_DURATION_SECONDS}s\n"
            f"• Time: {ts}")

# ====== Telegram Commands ======
async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot live ✅\nPairs: "+", ".join(PAIRS)+f"\nSignals every {INTERVAL_SECONDS//60} min(s). Use /now.")

async def now(update:Update,context:ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id,"Scanning now…")
    for p in PAIRS:
        vals = alpha_timeseries(p,interval=TF)
        if not vals: await context.bot.send_message(chat_id,f"⚠️ No data for {p}."); continue
        sig = calc_signal_from_candles(vals)
        if not sig: await context.bot.send_message(chat_id,f"⚠️ Not enough data for {p}."); continue
        await context.bot.send_message(chat_id,format_signal_msg(p,sig),parse_mode="Markdown")

# ====== Auto-push every 2 min ======
stop_event = Event()
def background_signaller(app):
    bot = app.bot
    while not stop_event.is_set():
        try:
            for p in PAIRS:
                vals = alpha_timeseries(p,interval=TF)
                if vals:
                    sig = calc_signal_from_candles(vals)
                    if sig:
                        bot.send_message(chat_id=CHAT_ID, text=format_signal_msg(p, sig), parse_mode="Markdown")
            for _ in range(INTERVAL_SECONDS):
                if stop_event.is_set(): break
                time.sleep(1)
        except Exception as e:
            log.exception(e); time.sleep(10)

async def on_start(app): Thread(target=background_signaller,args=(app,),daemon=True).start()

def main():
    app=ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_start).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("now", now))
    app.run_polling(close_loop=False)

if __name__=="__main__": main()
