import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ==== CONFIG ====
BOT_TOKEN = "8471181182:AAFEhPc59AvzNsnuPbj-N2PatGbvgZnnd_0"
ADMIN_ID = 7814662315

TWELVE_KEY = "9aa4ea677d00474aa0c3223d0c812425"
ALPHA_KEY = "BM22MZEIOLL68RI6"
SSID = "your_ssid_here"

# ==== STATE ====
DAILY_LOSSES = 0
MAX_LOSSES = 3

HELP_TEXT = (
    "📊 *PocketOption Signal Bot*\n\n"
    "Commands:\n"
    "/start – Initialize bot\n"
    "/help – Show help\n"
    "/signal EURUSD-OTC call 5 60 – Log manual signal\n"
    "/result win|loss – Record result\n"
    "/stats – Show stats\n"
    "/reset – Reset stats"
)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is live! Type /help to see commands.")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

STATS = {"wins": 0, "losses": 0, "total": 0}

async def signal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if int(update.effective_user.id) != ADMIN_ID:
        return await update.message.reply_text("❌ Not authorized.")
    if len(ctx.args) != 4:
        return await update.message.reply_text("Usage: /signal SYMBOL call|put AMOUNT DURATION")
    symbol, direction, amount, duration = ctx.args
    msg = (
        f"📈 *Manual Signal*\n"
        f"Pair: {symbol}\n"
        f"Direction: {direction.upper()}\n"
        f"Amount: ${amount}\n"
        f"Duration: {duration}s\n"
        f"SSID: {SSID}\n"
        f"TwelveData: {TWELVE_KEY}\n"
        f"Alpha: {ALPHA_KEY}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def result_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global DAILY_LOSSES
    if not ctx.args:
        return await update.message.reply_text("Usage: /result win|loss")
    res = ctx.args[0].lower()
    if res == "win":
        STATS["wins"] += 1
    elif res == "loss":
        STATS["losses"] += 1
        DAILY_LOSSES += 1
        if DAILY_LOSSES >= MAX_LOSSES:
            await update.message.reply_text("🛑 3 losses reached. Stop trading for today!")
            return
    STATS["total"] += 1
    await update.message.reply_text(f"✅ Recorded {res.upper()}.\nWins: {STATS['wins']} | Losses: {STATS['losses']}")

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📊 Stats\nWins: {STATS['wins']}\nLosses: {STATS['losses']}\nTotal: {STATS['total']}\nDaily Losses: {DAILY_LOSSES}/{MAX_LOSSES}"
    )

async def reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global DAILY_LOSSES
    STATS.update({"wins": 0, "losses": 0, "total": 0})
    DAILY_LOSSES = 0
    await update.message.reply_text("🔄 Stats reset.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("result", result_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.run_polling()

if __name__ == "__main__":
    main()