# === PocketOption OTC Telegram Bot (signals-only, webhook) ===
# - TwelveData primary, AlphaVantage fallback
# - /mode, /check, /autosignal, /stopsignal, /result win|loss, /stats, /reset
# - Confidence % from EMA50 + RSI14 (+ momentum in "ultra")
# - Stops after 3 losses; NO win limit
# - Webhook mode (works on Render; no Updater/polling)

import os, asyncio, logging, aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ==== CONFIG (filled with your values) ====
BOT_TOKEN  = os.getenv("BOT_TOKEN",  "8471181182:AAFEhPc59AvzNsnuPbj-N2PatGbvgZnnd_0")
ADMIN_ID   = int(os.getenv("ADMIN_ID", "7814662315"))
TWELVE_KEY = os.getenv("TWELVE_KEY",  "9aa4ea677d00474aa0c3223d0c812425")
ALPHA_KEY  = os.getenv("ALPHA_KEY",   "BM22MZEIOLL68RI6")

# Render provides these:
PORT = int(os.getenv("PORT", "10000"))
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

DEFAULT_EVERY = 300
DEFAULT_TF = "5min"
LOSS_LIMIT = 3

STATE = {"auto": False, "task": None, "wins": 0, "losses": 0, "mode": "both"}

HELP = (
    "🤖 Signals (manual only — I never place trades)\n"
    "/start, /help\n"
    "/mode strict|active|both|ultra\n"
    "/check SYMBOL [tf=5min]\n"
    "/autosignal SYMBOL AMOUNT DURATION [every=300] [tf=5min]\n"
    "/stopsignal\n\n"
    "📊 Stats\n"
    "/result win|loss\n"
    "/stats, /reset\n"
)

# ===== TA =====
def ema(v, p):
    if len(v) < p: return None
    k = 2/(p+1); e = sum(v[:p])/p
    for x in v[p:]: e = x*k + e*(1-k)
    return e

def rsi(v, p=14):
    if len(v) <= p: return None
    gains = [max(v[i]-v[i-1],0) for i in range(1,len(v))]
    losses= [max(v[i-1]-v[i],0) for i in range(1,len(v))]
    ag, al = sum(gains[:p])/p, sum(losses[:p])/p
    for i in range(p,len(v)-1):
        ag = (ag*(p-1)+gains[i])/p
        al = (al*(p-1)+losses[i])/p
    if al == 0: return 100.0
    rs = ag/al
    return 100 - 100/(1+rs)

def decide(closes, mode="both"):
    if len(closes) < 60: return None
    e50 = ema(closes,50); r = rsi(closes,14)
    if e50 is None or r is None: return None
    last = closes[-1]

    def strict():
        rp = rsi(closes[:-1],14)
        if rp is None: return None
        if last > e50 and rp < 30 <= r: return "call"
        if last < e50 and rp > 70 >= r: return "put"
        return None

    def active():
        if last > e50 and r > 55: return "call"
        if last < e50 and r < 45: return "put"
        return None

    if mode=="strict": return strict()
    if mode=="active": return active()
    if mode=="both":   return strict() or active()
    if mode=="ultra":
        d = strict() or active()
        if not d: return None
        body = abs(closes[-1]-closes[-2])
        avg  = sum(abs(closes[i]-closes[i-1]) for i in range(-11,-1))/10
        return d if body >= avg else None
    return None

def confidence_pct(closes):
    if len(closes) < 60: return 55
    e50 = ema(closes,50); r = rsi(closes,14)
    if e50 is None or r is None: return 55
    trend = 60 if closes[-1] > e50 else 40
    if r <= 30: base = 60 + (trend-50)//2
    elif r >= 70: base = 60 + ((50-trend)//2)
    else: base = 55
    return max(50, min(80, int(base)))

# ===== symbols =====
def td_symbol(sym: str) -> str:
    raw = sym.upper().replace("_","")
    raw = raw[:-4] if raw.endswith("-OTC") else raw
    if "/" in raw: return raw
    return f"{raw[:3]}/{raw[3:6]}" if len(raw)>=6 else raw

def alpha_from_to(sym: str):
    raw = sym.upper().replace("/","")
    raw = raw[:-4] if raw.endswith("-OTC") else raw
    return raw[:3], (raw[3:6] if len(raw)>=6 else "USD")

# ===== data =====
async def fetch_closes_twelve(symbol, interval=DEFAULT_TF, limit=120):
    if not TWELVE_KEY: return []
    url = f"https://api.twelvedata.com/time_series?symbol={td_symbol(symbol)}&interval={interval}&outputsize={limit}&apikey={TWELVE_KEY}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=20) as r:
                js = await r.json()
                vals = js.get("values") or []
                return [float(v["close"]) for v in reversed(vals)]
    except Exception:
        return []

async def fetch_closes_alpha(symbol, interval=DEFAULT_TF, limit=120):
    if not ALPHA_KEY: return []
    base, quote = alpha_from_to(symbol)
    if interval not in {"1min","5min","15min","30min","60min"}: interval = "5min"
    url = ("https://www.alphavantage.co/query?"
           f"function=FX_INTRADAY&from_symbol={base}&to_symbol={quote}"
           f"&interval={interval}&apikey={ALPHA_KEY}&outputsize=compact")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=20) as r:
                js = await r.json()
                ts = next((v for k,v in js.items() if 'Time Series' in k), None)
                if not ts: return []
                items = sorted(ts.items())[-limit:]
                return [float(v["4. close"]) for _,v in items]
    except Exception:
        return []

async def fetch_closes(symbol, interval=DEFAULT_TF, limit=120):
    data = await fetch_closes_twelve(symbol, interval, limit)
    if data: return data
    return await fetch_closes_alpha(symbol, interval, limit)

# ===== commands =====
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot online (signals only).\n" + HELP)

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)

async def mode_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or ctx.args[0].lower() not in ("strict","active","both","ultra"):
        return await update.message.reply_text("Usage: /mode strict|active|both|ultra")
    STATE["mode"] = ctx.args[0].lower()
    await update.message.reply_text(f"Mode set to {STATE['mode']}")

async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) not in (1,2):
        return await update.message.reply_text("Usage: /check SYMBOL [tf=5min]")
    sym = ctx.args[0].upper()
    tf = ctx.args[1] if len(ctx.args)==2 else DEFAULT_TF
    closes = await fetch_closes(sym, tf, 120)
    if not closes:
        return await update.message.reply_text("❌ No data right now.")
    dec = (decide(closes, STATE["mode"]) or "none").upper()
    conf = confidence_pct(closes)
    await update.message.reply_text(f"📊 {sym} {tf}\nMode: {STATE['mode']}\nDecision: {dec}\nConfidence: {conf}%\n\n➡️ Manual entry only")

async def autosignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if STATE["auto"]:
        return await update.message.reply_text("Already running. /stopsignal first.")
    if len(ctx.args) not in (3,4,5):
        return await update.message.reply_text(
            "Usage: /autosignal SYMBOL AMOUNT DURATION [every=300] [tf=5min]\n"
            "Example: /autosignal EURUSD-OTC 5 60 300 1min"
        )
    sym = ctx.args[0].upper()
    amount = float(ctx.args[1]); duration = int(ctx.args[2])
    every = int(ctx.args[3]) if len(ctx.args)>=4 else DEFAULT_EVERY
    tf = ctx.args[4] if len(ctx.args)==5 else DEFAULT_TF

    STATE["auto"] = True

    async def loop(chat_id):
        await ctx.bot.send_message(chat_id,
            f"▶️ Auto-signal {sym} | ${amount} | {duration}s | every {every}s | TF {tf} | mode {STATE['mode']}")
        while STATE["auto"] and STATE["losses"] < LOSS_LIMIT:
            closes = await fetch_closes(sym, tf, 120)
            if closes:
                dec = decide(closes, STATE["mode"])
                if dec:
                    conf = confidence_pct(closes)
                    arrow = "UP" if dec=="call" else "DOWN"
                    await ctx.bot.send_message(chat_id,
                        f"📣 SIGNAL\nPair: {sym}\nDirection: {arrow}\nAmount: ${amount}\nDuration: {duration}s\nConfidence: {conf}%\n\n➡️ Place manually")
            await asyncio.sleep(every)
        STATE["auto"] = False
        await ctx.bot.send_message(chat_id, "⏹ Auto-signal stopped.")
    STATE["task"] = asyncio.create_task(loop(update.effective_chat.id))
    await update.message.reply_text("Auto started.")

async def stopsignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"] = False
    await update.message.reply_text("Stopping auto-signal...")

async def result_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or ctx.args[0].lower() not in ("win","loss"):
        return await update.message.reply_text("Usage: /result win|loss")
    if ctx.args[0].lower()=="win":
        STATE["wins"] += 1
    else:
        STATE["losses"] += 1
        if STATE["losses"] >= LOSS_LIMIT:
            STATE["auto"] = False
            await update.message.reply_text("🛑 Loss limit reached (3). Auto stopped.")
    await stats_cmd(update, ctx)

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tot = STATE["wins"] + STATE["losses"]
    wr = (STATE["wins"]/tot*100) if tot else 0.0
    await update.message.reply_text(
        f"📈 Stats\nWins: {STATE['wins']}  Losses: {STATE['losses']}  WR: {wr:.1f}%\nLoss limit: {LOSS_LIMIT}"
    )

async def reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE.update({"wins":0,"losses":0})
    await update.message.reply_text("✅ Stats reset.")

# ===== main (WEBHOOK) =====
async def post_init(app: Application):
    if not PUBLIC_URL:
        logging.warning("RENDER_EXTERNAL_URL missing — running local webhook.")
        return
    await app.bot.set_webhook(url=f"{PUBLIC_URL}/{BOT_TOKEN}")

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("autosignal", autosignal_cmd))
    app.add_handler(CommandHandler("stopsignal", stopsignal_cmd))
    app.add_handler(CommandHandler("result", result_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))

    # Run webhook server (no Updater/polling involved)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{PUBLIC_URL}/{BOT_TOKEN}" if PUBLIC_URL else None,
    )

if __name__ == "__main__":
    main()