import json, aiohttp, asyncio, logging
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ===== BASIC CONFIG =====
BOT_TOKEN = "8471181182:AAFEhPc59AvzNsnuPbj-N2PatGbvgZnnd_0"
PO_UID = "93269888"
PO_SSID = "d7a8a43d4618a7227c6ed769f8fd9975"
TWELVE_KEY = "9aa4ea677d00474aa0c3223d0c812425"
ALPHA_KEY = "BM22MZEIOLL68RI6"
MAX_LOSSES = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

STATE = {"wins": 0, "losses": 0, "stopped": False}


# ====== INDICATORS ======
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values, period=14):
    if len(values) <= period:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(values) - 1):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


# ====== FETCH CANDLES ======
async def fetch_twelve(symbol, interval="1min", limit=120):
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&outputsize={limit}&apikey={TWELVE_KEY}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=15) as r:
            js = await r.json()
            if js.get("status") == "error":
                return [], js.get("message")
            vals = js.get("values", [])
            vals.reverse()
            return [float(v["close"]) for v in vals], None


# ====== COMMANDS ======
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot Ready\n"
        "/pologin – reconnect your SSID\n"
        "/postatus – show account info\n"
        "/check EURUSD-OTC 1min – get signal\n"
        "/result win or /result loss\n"
        "/stats – show session stats"
    )


async def pologin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /pologin <ssid>")
    global PO_SSID
    PO_SSID = ctx.args[0].strip()
    await update.message.reply_text("🔐 SSID saved successfully.")


async def postatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mask = PO_SSID[:4] + "..." + PO_SSID[-4:]
    await update.message.reply_text(f"📡 UID: {PO_UID}\nSSID: {mask}")


async def check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 1:
        return await update.message.reply_text("Usage: /check SYMBOL [interval]\nExample: /check EURUSD-OTC 1min")

    symbol = ctx.args[0].replace("-OTC", "").upper()
    interval = ctx.args[1] if len(ctx.args) > 1 else "1min"
    closes, err = await fetch_twelve(symbol, interval)
    if err or not closes:
        return await update.message.reply_text(f"❌ Error: {err or 'No data'}")

    e = ema(closes, 50)
    r = rsi(closes, 14)
    if not e or not r:
        return await update.message.reply_text("Not enough data.")

    signal = None
    confidence = 0
    if closes[-1] > e and r > 55:
        signal, confidence = "BUY", round(min(95, r), 2)
    elif closes[-1] < e and r < 45:
        signal, confidence = "SELL", round(100 - r, 2)

    await update.message.reply_text(
        f"📊 {symbol}-{interval}\nRSI: {round(r, 2)}\nEMA50: {round(e, 5)}\n"
        f"➡️ Signal: {signal or 'NONE'}\nConfidence: {confidence}%"
    )


async def result(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /result win|loss")
    if STATE["stopped"]:
        return await update.message.reply_text("⛔ Bot paused after 3 losses. Use /reset to continue.")

    res = ctx.args[0].lower()
    if res == "win":
        STATE["wins"] += 1
    elif res == "loss":
        STATE["losses"] += 1
        if STATE["losses"] >= MAX_LOSSES:
            STATE["stopped"] = True
    else:
        return await update.message.reply_text("Usage: /result win|loss")

    await update.message.reply_text(f"✅ Recorded {res.upper()}. Wins: {STATE['wins']} | Losses: {STATE['losses']}")


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    total = STATE["wins"] + STATE["losses"]
    winrate = (STATE["wins"] / total * 100) if total > 0 else 0
    msg = f"📈 Stats\nWins: {STATE['wins']} | Losses: {STATE['losses']}\nWinrate: {winrate:.1f}%"
    if STATE["stopped"]:
        msg += "\n⚠️ Bot paused (3 losses reached)"
    await update.message.reply_text(msg)


async def reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE.update({"wins": 0, "losses": 0, "stopped": False})
    await update.message.reply_text("🔄 Stats reset. You can trade again.")


# ====== MAIN ======
async def _init(app):  # removes old webhook for Render
    await app.bot.delete_webhook(drop_pending_updates=True)

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pologin", pologin))
    app.add_handler(CommandHandler("postatus", postatus))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("result", result))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset", reset))
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)

if __name__ == "__main__":
    main()