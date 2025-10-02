import os
import time
import requests
from datetime import datetime, timedelta

from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ========= YOUR KEYS (as provided) =========
TELEGRAM_BOT_TOKEN = "8471181182:AAEKGH1UASa5XvkXscb3jb5d1Yz19B8oJNM"
TWELVE_API_KEY     = "9aa4ea677d00474aa0c3223d0c812425"
ALPHA_VANTAGE_KEY  = "BM22MZEIOLL68RI6"

# Render public URL (no trailing slash)
PUBLIC_URL = "https://moneymakerjbsbot.onrender.com"
PORT = int(os.environ.get("PORT", "10000"))

# ========= GLOBAL STATE (lean + OTC) =========
STATE = {
    "watchlist": ["EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "USDCHF-OTC"],

    # run control
    "autopoll_running": False,
    "duration_min": 60,          # /autopoll run length

    # cadence
    "cooldown_min": 5,           # per-pair cooldown
    "min_signal_gap_min": 7,     # global gap between any two signals

    # accuracy filters
    "threshold": 0.80,           # min confidence (80%)
    "require_votes": 4,          # EMA200 trend, RSI>50, MACD line>signal, EMA20>EMA50
    "atr_floor": 0.0006,         # skip flat/choppy markets

    # safety
    "loss_streak_limit": 3,
    "loss_streak": 0,

    # (manual) payout filter number you can set with /otc; not blocking
    "min_payout": 80,

    # internals
    "last_signal_time": None,
    "pair_last_signal_time": {},

    # active chat
    "chat_id": None,
}

# ========= DATA FETCHERS (Twelve primary, AV fallback for non-OTC) =========
def fetch_twelve(symbol: str, tf: str = "5min"):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol, "interval": tf, "outputsize": 60,
        "apikey": TWELVE_API_KEY, "order": "ASC", "timezone": "UTC"
    }
    try:
        r = requests.get(url, params=params, timeout=12)
        j = r.json()
        if "values" not in j:
            return None
        out = []
        for v in j["values"]:
            out.append({
                "t": datetime.fromisoformat(v["datetime"]),
                "o": float(v["open"]), "h": float(v["high"]),
                "l": float(v["low"]),  "c": float(v["close"])
            })
        out.sort(key=lambda x: x["t"])
        return out[-60:]
    except Exception:
        return None

def fetch_av(symbol: str, tf: str = "5min"):
    # Alpha Vantage doesn't support OTC; only use for non-OTC if you ever add them
    if "-OTC" in symbol:
        return None
    try:
        base, quote = symbol[:3], symbol[3:]
        url = "https://www.alphavantage.co/query"
        params = {
            "function": "FX_INTRADAY",
            "from_symbol": base,
            "to_symbol": quote,
            "interval": tf,
            "apikey": ALPHA_VANTAGE_KEY,
            "outputsize": "compact",
        }
        j = requests.get(url, params=params, timeout=12).json()
        key = "Time Series FX (5min)"
        if key not in j:
            return None
        items = []
        for ts, v in j[key].items():
            items.append({
                "t": datetime.fromisoformat(ts),
                "o": float(v["1. open"]), "h": float(v["2. high"]),
                "l": float(v["3. low"]),  "c": float(v["4. close"])
            })
        items.sort(key=lambda x: x["t"])
        return items[-60:]
    except Exception:
        return None

def fetch_ohlcv(symbol: str, tf: str = "5min"):
    bars = fetch_twelve(symbol, tf)
    if bars:
        return bars
    return fetch_av(symbol, tf)

# ========= INDICATORS =========
def ema_last(vals, n):
    if len(vals) < n: return None
    k = 2/(n+1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = e + k*(v - e)
    return e

def rsi_last(vals, n=14):
    if len(vals) < n+1: return None
    gains, losses = [], []
    for i in range(1, len(vals)):
        ch = vals[i] - vals[i-1]
        gains.append(max(ch, 0)); losses.append(max(-ch, 0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_g = (avg_g*(n-1) + gains[i]) / n
        avg_l = (avg_l*(n-1) + losses[i]) / n
    rs = (avg_g / avg_l) if avg_l != 0 else 999
    return 100 - 100/(1+rs)

def macd_last(vals, fast=12, slow=26, signal=9):
    if len(vals) < slow + signal: return None, None
    def ema_series(arr, n):
        if len(arr) < n: return []
        k = 2/(n+1)
        e = sum(arr[:n]) / n
        out = [None]*(n-1) + [e]
        for v in arr[n:]:
            e = e + k*(v - e)
            out.append(e)
        return out
    ef = ema_series(vals, fast)
    es = ema_series(vals, slow)
    macd_line = [(ef[i] - es[i]) if ef[i] and es[i] else None for i in range(len(vals))]
    macd_vals = [x for x in macd_line if x is not None]
    if len(macd_vals) < signal: return None, None
    sig_series = ema_series(macd_vals, signal)
    return macd_line[-1], sig_series[-1]

def atr_last(highs, lows, closes, n=14):
    if len(closes) < n+1: return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i]-lows[i],
                       abs(highs[i]-closes[i-1]),
                       abs(lows[i]-closes[i-1])))
    atr = sum(trs[:n]) / n
    for i in range(n, len(trs)):
        atr = (atr*(n-1) + trs[i]) / n
    return atr

# ========= STRATEGY (OTC strict confluence) =========
def analyze(symbol):
    bars = fetch_ohlcv(symbol, "5min")
    if not bars or len(bars) < 60:
        return (False, None, 0.0, "no data")

    closes = [b["c"] for b in bars]
    highs  = [b["h"] for b in bars]
    lows   = [b["l"] for b in bars]

    ema20  = ema_last(closes, 20)
    ema50  = ema_last(closes, 50)
    ema200 = ema_last(closes, 200) if len(closes) >= 200 else sum(closes)/len(closes)
    rsi14  = rsi_last(closes, 14)
    macd_line, macd_sig = macd_last(closes)
    atr14  = atr_last(highs, lows, closes, 14)

    if atr14 is None or atr14 < STATE["atr_floor"]:
        return (False, None, 0.0, "low atr")

    up = dn = 0
    if closes[-1] > ema200: up += 1
    else: dn += 1
    if rsi14 is not None and rsi14 > 50: up += 1
    elif rsi14 is not None: dn += 1
    if macd_line is not None and macd_sig is not None:
        if macd_line > macd_sig: up += 1
        else: dn += 1
    if ema20 is not None and ema50 is not None:
        if ema20 > ema50: up += 1
        else: dn += 1

    need = STATE["require_votes"]
    if up >= need and up > dn:
        side, votes = "BUY", up
    elif dn >= need and dn > up:
        side, votes = "SELL", dn
    else:
        return (False, None, 0.0, f"no side up={up} dn={dn}")

    # confidence from votes (4/4 -> 70%, cap 95%)
    conf = max(0.0, min(0.95, 0.70 + 0.05*(votes-4)))
    if conf < STATE["threshold"]:
        return (False, None, conf, "low conf")

    return (True, side, conf, "ok")

# ========= MESSAGES =========
async def send_signal(context: ContextTypes.DEFAULT_TYPE, pair: str, side: str, conf: float):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Win", callback_data="win"),
        InlineKeyboardButton("❌ Loss", callback_data="loss"),
        InlineKeyboardButton("⏭ Skip", callback_data="skip"),
    ]])
    txt = f"📊 OTC Signal\nPair: {pair}\n👉 {side}\nConfidence: {int(conf*100)}%\nMin payout (manual): {STATE['min_payout']}%"
    await context.bot.send_message(chat_id=STATE["chat_id"], text=txt, reply_markup=kb)

# ========= JOB: AUTO SCAN EVERY 5 MIN =========
async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    if not STATE["autopoll_running"] or STATE["chat_id"] is None:
        return
    now = datetime.now()

    for pair in STATE["watchlist"]:
        last_pair = STATE["pair_last_signal_time"].get(pair)
        if last_pair and (now - last_pair).total_seconds() < STATE["cooldown_min"]*60:
            continue
        if STATE["last_signal_time"] and (now - STATE["last_signal_time"]).total_seconds() < STATE["min_signal_gap_min"]*60:
            continue

        ok, side, conf, _ = analyze(pair)
        if ok:
            if STATE["loss_streak"] >= STATE["loss_streak_limit"]:
                await context.bot.send_message(chat_id=STATE["chat_id"], text="🚫 3 losses in a row. Stopping.")
                STATE["autopoll_running"] = False
                return
            await send_signal(context, pair, side, conf)
            STATE["pair_last_signal_time"][pair] = now
            STATE["last_signal_time"] = now

# ========= STOP JOB AFTER DURATION =========
async def stop_job(context: ContextTypes.DEFAULT_TYPE):
    STATE["autopoll_running"] = False
    await context.bot.send_message(chat_id=STATE["chat_id"], text="⏹️ Autopoll duration ended.")

# ========= BUTTON HANDLER =========
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    if q.data == "win":
        STATE["loss_streak"] = 0
        await q.edit_message_text(q.message.text + "\n✅ WIN")
    elif q.data == "loss":
        STATE["loss_streak"] += 1
        await q.edit_message_text(q.message.text + "\n❌ LOSS")
    else:
        await q.edit_message_text(q.message.text + "\n⏭ SKIP")
    await q.answer()

# ========= COMMANDS =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot ready ✅\n"
        "Use /otc [min_payout] then /autopoll\n"
        "Default OTC pairs: EURUSD-OTC, GBPUSD-OTC, USDJPY-OTC, AUDUSD-OTC, USDCHF-OTC"
    )

async def cmd_otc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if context.args:
            mp = int(context.args[0])
            STATE["min_payout"] = max(50, min(100, mp))
    except Exception:
        pass
    await update.message.reply_text(
        f"⚙️ OTC strict mode ON\n"
        f"- Confidence ≥ {int(STATE['threshold']*100)}%\n"
        f"- Confluence: 4/4 (Trend, RSI50, MACD, EMA20/50)\n"
        f"- ATR floor: {STATE['atr_floor']}\n"
        f"- Min payout (manual): {STATE['min_payout']}%\n"
        f"- Cooldown: {STATE['cooldown_min']}m | Gap: {STATE['min_signal_gap_min']}m"
    )

async def cmd_autopoll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if STATE["autopoll_running"]:
        await update.message.reply_text("ℹ️ Already running.")
        return
    STATE["chat_id"] = update.effective_chat.id
    STATE["autopoll_running"] = True
    STATE["last_signal_time"] = None
    STATE["pair_last_signal_time"] = {}

    # schedule the repeating scan every 5 minutes (on candle close cadence)
    context.job_queue.run_repeating(poll_job, interval=300, first=1, name="poll")

    # schedule a stop after duration
    context.job_queue.run_once(stop_job, when=STATE["duration_min"]*60, name="stopper")

    await update.message.reply_text("▶️ Autopoll started. Waiting for high-quality signals…")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STATE["autopoll_running"] = False
    # cancel jobs for safety
    for job in context.job_queue.jobs():
        if job.name in ("poll", "stopper"):
            job.schedule_removal()
    await update.message.reply_text("⏹️ Autopoll stopped.")

# ========= BOOT (webhook) =========
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("otc",   cmd_otc))
    app.add_handler(CommandHandler("autopoll", cmd_autopoll))
    app.add_handler(CommandHandler("stop",  cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))

    # Webhook only (no polling) – perfect for Render
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_BOT_TOKEN,
        webhook_url=f"{PUBLIC_URL}/{TELEGRAM_BOT_TOKEN}",
    )

if __name__ == "__main__":
    main()