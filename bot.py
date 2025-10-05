# === TELEGRAM OTC BOT (AUTO SIGNAL + POOL + TRACK) ===
# Plug and play for Render (no .env needed)
# Stops after 3 losses — no win limit

import logging, asyncio, aiohttp, time, re
from datetime import datetime, timezone, timedelta
from collections import deque
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ======= CONFIG =======
BOT_TOKEN = "8471181182:AAFEhPc59AvzNsnuPbj-N2PatGbvgZnnd_0"
ADMIN_ID = 7814662315

TWELVE_KEY = "9aa4ea677d00474aa0c3223d0c812425"
ALPHA_KEY = "BM22MZEIOLL68RI6"

# ======= GLOBAL STATE =======
STATS = {"wins": 0, "losses": 0}
DAILY_LIMIT_LOSSES = 3
AUTO_RUNNING = False
POOL_RUNNING = False

# ======= HELP =======
HELP_MSG = (
    "📈 Commands:\n"
    "/start — Ready check\n"
    "/mode strict|active|ultra\n"
    "/autosignal SYMBOL AMOUNT DURATION\n"
    "/stopsignal — stop autosignal\n"
    "/autopool AMOUNT DURATION\n"
    "/stoppool — stop pool\n"
    "/result win|loss — record result\n"
    "/stats — show stats\n"
    "/reset — reset stats"
)

# ======= SIMPLE EMA & RSI =======
def ema(values, period):
    if len(values) < period: return None
    k = 2/(period+1)
    ev = sum(values[:period])/period
    for v in values[period:]: ev = v*k + ev*(1-k)
    return ev

def rsi(values, period=14):
    if len(values) <= period: return None
    gains = [max(values[i]-values[i-1],0) for i in range(1,len(values))]
    losses = [max(values[i-1]-values[i],0) for i in range(1,len(values))]
    avg_g, avg_l = sum(gains[:period])/period, sum(losses[:period])/period
    for i in range(period,len(values)-1):
        avg_g = (avg_g*(period-1)+gains[i])/period
        avg_l = (avg_l*(period-1)+losses[i])/period
    if avg_l==0: return 100
    rs = avg_g/avg_l
    return 100 - (100/(1+rs))

# ======= FETCH DATA =======
async def fetch_closes(symbol, key, source="twelve"):
    url = ""
    if source=="twelve":
        url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval=5min&apikey={key}"
    else:
        base, quote = symbol[:3], symbol[3:]
        url = f"https://www.alphavantage.co/query?function=FX_INTRADAY&from_symbol={base}&to_symbol={quote}&interval=5min&apikey={key}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=10) as r:
                js = await r.json()
                if source=="twelve": vals = js.get("values", [])
                else:
                    ts = next((v for k,v in js.items() if "Time Series" in k), None)
                    vals = [{"close": float(v["4. close"])} for v in ts.values()] if ts else []
                return [float(v["close"]) for v in vals][::-1]
    except:
        return []

# ======= SIGNAL DECISION =======
def decide(closes):
    e50 = ema(closes, 50)
    r = rsi(closes, 14)
    if not e50 or not r: return None
    if r <= 30 and closes[-1] > e50: return "call"
    if r >= 70 and closes[-1] < e50: return "put"
    return None

# ======= COMMANDS =======
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot online.\n" + HELP_MSG)

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_MSG)

async def result_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global STATS
    if not ctx.args: return await update.message.reply_text("Usage: /result win|loss")
    res = ctx.args[0].lower()
    if res=="win": STATS["wins"]+=1
    elif res=="loss": STATS["losses"]+=1
    else: return await update.message.reply_text("Use win|loss only.")
    if STATS["losses"] >= DAILY_LIMIT_LOSSES:
        await update.message.reply_text("🛑 3 losses reached. Auto stopped.")
    await update.message.reply_text(f"📊 W: {STATS['wins']} | L: {STATS['losses']}")

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📈 Stats:\nWins: {STATS['wins']}\nLosses: {STATS['losses']}")

async def reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATS.update({"wins":0,"losses":0})
    await update.message.reply_text("✅ Stats reset.")

# ======= AUTO SIGNAL =======
async def autosignal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global AUTO_RUNNING
    if AUTO_RUNNING: return await update.message.reply_text("Already running.")
    if len(ctx.args) < 3: return await update.message.reply_text("Usage: /autosignal SYMBOL AMOUNT DURATION")
    symbol, amount, duration = ctx.args[0].upper(), float(ctx.args[1]), int(ctx.args[2])
    AUTO_RUNNING = True
    await update.message.reply_text(f"▶️ Auto signal ON for {symbol}")
    while AUTO_RUNNING and STATS["losses"] < DAILY_LIMIT_LOSSES:
        closes = await fetch_closes(symbol, TWELVE_KEY)
        if not closes: closes = await fetch_closes(symbol, ALPHA_KEY, "alpha")
        decision = decide(closes)
        if decision:
            await update.message.reply_text(f"📣 {symbol} → {decision.upper()} | ${amount} | {duration}s")
        await asyncio.sleep(300)
    AUTO_RUNNING = False
    await update.message.reply_text("⏹ Auto stopped.")

async def stopsignal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global AUTO_RUNNING
    AUTO_RUNNING = False
    await update.message.reply_text("🛑 Stopped auto signal.")

# ======= MAIN =======
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("autosignal", autosignal))
    app.add_handler(CommandHandler("stopsignal", stopsignal))
    app.add_handler(CommandHandler("result", result_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.run_polling()

if __name__=="__main__":
    main()