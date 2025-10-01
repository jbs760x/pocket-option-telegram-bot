import time, logging, requests
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ====== YOUR KEYS (inline for mobile simplicity) ======
TELEGRAM_TOKEN = "8471181182:AAEKGH1UASa5XvkXscb3jb5d1Yz19B8oJNM"
TWELVE_API_KEY = "9aa4ea677d00474aa0c3223d0c812425"
ALPHA_API_KEY  = "BM22MZEIOLL68RI6"
CHAT_ID        = "7814662315"  # your Telegram user ID (also used as admin)

# ====== CONFIG (you can change via commands) ======
PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY"]   # pairs to scan
TF = "1min"                                 # timeframe
INTERVAL_SECONDS = 120                      # auto-signal frequency
BET_AMOUNT = 5                              # $5
BET_DURATION_SECONDS = 60                   # 60s
ENTRY_ALIGN_SECONDS = 60                    # align to next minute candle
LEAD_SECONDS = 15                           # warn this many seconds early
CONF_THRESHOLD = 60                         # only send if confidence >= 60%

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("bot")
stop_event = Event()

# ---------- Data helpers ----------
def twelve_timeseries(symbol, interval="1min", outputsize=30):
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&outputsize={outputsize}&apikey={TWELVE_API_KEY}"
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        if "values" in data:
            return data["values"]  # newest first
    except Exception as e:
        log.error(f"TwelveData error: {e}")
    return None

def alpha_timeseries(symbol, interval="1min"):
    sym = symbol.replace("/", "")
    url = f"https://www.alphavantage.co/query?function=FX_INTRADAY&from_symbol={sym[:3]}&to_symbol={sym[3:]}&interval={interval}&apikey={ALPHA_API_KEY}"
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        key = f"Time Series FX ({interval})"
        if key in data:
            ts = data[key]
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
            values.sort(key=lambda x: x["datetime"], reverse=True)
            return values
    except Exception as e:
        log.error(f"Alpha error: {e}")
    return None

def get_data(pair):
    data = twelve_timeseries(pair, interval=TF)
    if not data:
        log.info(f"TwelveData failed for {pair}, trying AlphaVantage…")
        data = alpha_timeseries(pair, interval=TF)
    return data

# ---------- Strategy ----------
def calc_signal_from_candles(values):
    if not values or len(values) < 30:
        return None
    closes = [float(v["close"]) for v in values]
    opens  = [float(v["open"])  for v in values]

    # EMAs
    def ema(series, n):
        k = 2/(n+1); e = series[0]; out = [e]
        for x in series[1:]:
            e = x*k + e*(1-k); out.append(e)
        return out
    ema_fast, ema_slow = ema(closes,5), ema(closes,14)

    # RSI(14)
    gains, losses = [], []
    for i in range(1, 15):
        ch = closes[i-1] - closes[i]
        gains.append(max(ch,0)); losses.append(max(-ch,0))
    avg_gain = sum(gains)/14
    avg_loss = sum(losses)/14 or 1e-9
    rs = avg_gain/avg_loss
    rsi = 100 - (100/(1+rs))

    # Momentum for current candle
    body = abs(closes[0]-opens[0])
    high, low = float(values[0]["high"]), float(values[0]["low"])
    rng = max(high-low,1e-9)
    momentum = min(body/rng,1.0)

    trend = 1 if ema_fast[0] > ema_slow[0] else -1
    rsi_bias = 1 if rsi >= 55 else (-1 if rsi <= 45 else 0)
    candle_dir = 1 if closes[0] > opens[0] else -1

    raw_score = trend + rsi_bias + candle_dir*momentum
    confidence = max(0, min(100, int((raw_score + 3) / 6 * 100)))
    side = "BUY (CALL)" if raw_score > 0 else ("SELL (PUT)" if raw_score < 0 else "NO TRADE")

    return {"side": side, "confidence": confidence, "rsi": round(rsi,1),
            "momentum": round(momentum,2), "trend": "Bull" if trend==1 else "Bear"}

# ---------- Entry timing ----------
def next_aligned_time(now_utc: datetime, align_seconds=60):
    epoch = int(now_utc.timestamp())
    next_epoch = ((epoch // align_seconds) + 1) * align_seconds
    return datetime.fromtimestamp(next_epoch, tz=timezone.utc)

def seconds_until(dt_utc: datetime):
    return max(0, int((dt_utc - datetime.now(timezone.utc)).total_seconds()))

def format_pre_entry(pair, sig, entry_time_utc, seconds_left):
    return (
        f"🕒 Prep Signal | {pair} ({TF})\n"
        f"• **Enter at**: {entry_time_utc.strftime('%Y-%m-%d %H:%M:%S UTC')} (in {seconds_left}s)\n"
        f"• Recommendation: **{sig['side']}**\n"
        f"• Confidence: **{sig['confidence']}%** | Trend: {sig['trend']} | RSI: {sig['rsi']} | Mom: {sig['momentum']}\n"
        f"• Bet: ${BET_AMOUNT} | Duration: {BET_DURATION_SECONDS}s"
    )

def format_enter_now(pair, sig):
    return f"✅ **ENTER NOW** | {pair} ({TF}) — {sig['side']} | {sig['confidence']}% | ${BET_AMOUNT} for {BET_DURATION_SECONDS}s"

# ---------- Guard ----------
def is_admin(update: Update) -> bool:
    try:
        return str(update.effective_user.id) == str(CHAT_ID)
    except Exception:
        return False

# ---------- Telegram handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot live ✅\n"
        f"Pairs: {', '.join(PAIRS)}\n"
        f"Autosignals every {INTERVAL_SECONDS}s | lead {LEAD_SECONDS}s | threshold {CONF_THRESHOLD}%\n"
        "Commands:\n"
        "• /now – scan now\n"
        "• /confidence – show threshold\n"
        "• /setconfidence 70 – set threshold (admin)\n"
        "• /setlead 20 – set warning seconds (admin)\n"
        "• /setfreq 120 – set autosignal seconds (admin)"
    )

async def confidence_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Current confidence threshold: {CONF_THRESHOLD}%")

async def setconfidence_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CONF_THRESHOLD
    if not is_admin(update): 
        await update.message.reply_text("Not authorized.")
        return
    try:
        val = int(context.args[0])
        if not (0 <= val <= 100): raise ValueError
        CONF_THRESHOLD = val
        await update.message.reply_text(f"Set confidence threshold to {CONF_THRESHOLD}%.")
    except Exception:
        await update.message.reply_text("Usage: /setconfidence 60..100")

async def setlead_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LEAD_SECONDS
    if not is_admin(update): 
        await update.message.reply_text("Not authorized.")
        return
    try:
        val = int(context.args[0])
        if not (5 <= val <= 60): raise ValueError
        LEAD_SECONDS = val
        await update.message.reply_text(f"Set lead warning to {LEAD_SECONDS}s.")
    except Exception:
        await update.message.reply_text("Usage: /setlead 5..60")

async def setfreq_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global INTERVAL_SECONDS
    if not is_admin(update): 
        await update.message.reply_text("Not authorized.")
        return
    try:
        val = int(context.args[0])
        if not (30 <= val <= 900): raise ValueError
        INTERVAL_SECONDS = val
        await update.message.reply_text(f"Set autosignal frequency to {INTERVAL_SECONDS}s.")
    except Exception:
        await update.message.reply_text("Usage: /setfreq 30..900")

async def now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, "Scanning now…")
    for p in PAIRS:
        vals = get_data(p)
        if not vals:
            await context.bot.send_message(chat_id, f"⚠️ No data for {p}."); 
            continue
        sig = calc_signal_from_candles(vals)
        if not sig or sig["side"] == "NO TRADE":
            await context.bot.send_message(chat_id, f"⏸️ {p} | No clear edge (skip)."); 
            continue
        if sig["confidence"] < CONF_THRESHOLD:
            await context.bot.send_message(chat_id, f"⚠️ {p} | Low confidence {sig['confidence']}% (skipped)."); 
            continue

        entry_time = next_aligned_time(datetime.now(timezone.utc), ENTRY_ALIGN_SECONDS)
        secs_left = seconds_until(entry_time)
        if secs_left < LEAD_SECONDS:
            entry_time += timedelta(seconds=ENTRY_ALIGN_SECONDS)
            secs_left = seconds_until(entry_time)

        await context.bot.send_message(chat_id, format_pre_entry(p, sig, entry_time, secs_left), parse_mode="Markdown")

        def delayed_enter():
            try:
                time.sleep(secs_left)
                context.application.bot.send_message(chat_id=chat_id, text=format_enter_now(p, sig), parse_mode="Markdown")
            except Exception as e: log.exception(e)
        Thread(target=delayed_enter, daemon=True).start()

# ---------- Background autosignaller ----------
def push_with_timing(bot, pair):
    vals = get_data(pair)
    if not vals:
        bot.send_message(chat_id=CHAT_ID, text=f"⚠️ No data for {pair}."); 
        return
    sig = calc_signal_from_candles(vals)
    if not sig or sig["side"] == "NO TRADE": 
        return
    if sig["confidence"] < CONF_THRESHOLD:
        bot.send_message(chat_id=CHAT_ID, text=f"⚠️ {pair} | Low confidence {sig['confidence']}% (skipped)."); 
        return

    entry_time = next_aligned_time(datetime.now(timezone.utc), ENTRY_ALIGN_SECONDS)
    secs_left = seconds_until(entry_time)
    if secs_left < LEAD_SECONDS:
        entry_time += timedelta(seconds=ENTRY_ALIGN_SECONDS)
        secs_left = seconds_until(entry_time)

    bot.send_message(chat_id=CHAT_ID, text=format_pre_entry(pair, sig, entry_time, secs_left), parse_mode="Markdown")

    def delayed_enter():
        try:
            time.sleep(secs_left)
            bot.send_message(chat_id=CHAT_ID, text=format_enter_now(pair, sig), parse_mode="Markdown")
        except Exception as e: log.exception(e)
    Thread(target=delayed_enter, daemon=True).start()

def background_signaller(app):
    global INTERVAL_SECONDS
    bot = app.bot
    while not stop_event.is_set():
        try:
            for p in PAIRS:
                push_with_timing(bot, p)
            # sleep in 1s chunks so /setfreq applies quickly
            slept = 0
            while slept < INTERVAL_SECONDS and not stop_event.is_set():
                time.sleep(1)
                slept += 1
        except Exception as e:
            log.exception(e)
            time.sleep(10)

async def on_start(app):
    Thread(target=background_signaller, args=(app,), daemon=True).start()

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_start).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("now", now_cmd))
    app.add_handler(CommandHandler("confidence", confidence_cmd))
    app.add_handler(CommandHandler("setconfidence", setconfidence_cmd))
    app.add_handler(CommandHandler("setlead", setlead_cmd))
    app.add_handler(CommandHandler("setfreq", setfreq_cmd))
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()