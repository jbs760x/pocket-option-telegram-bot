import requests
from datetime import datetime

from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ===== YOUR KEYS =====
TELEGRAM_BOT_TOKEN = "8471181182:AAEKGH1UASa5XvkXscb3jb5d1Yz19B8oJNM"
TWELVE_API_KEY     = "9aa4ea677d00474aa0c3223d0c812425"
ALPHA_VANTAGE_KEY  = "BM22MZEIOLL68RI6"

# ===== SETTINGS =====
STATE = {
    "watchlist": ["EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "USDCHF-OTC"],
    "autopoll_running": False,
    "duration_min": 60,
    "cooldown_min": 5,
    "min_signal_gap_min": 7,
    "require_votes": 4,
    "threshold": 0.80,
    "atr_floor": 0.0006,
    "loss_streak_limit": 3,
    "loss_streak": 0,
    "last_signal_time": None,
    "pair_last_signal_time": {},
    "chat_id": None
}

# ---------- Data fetchers ----------
def fetch_twelvedata(symbol: str, tf: str = "5min"):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol, "interval": tf, "outputsize": 60,
        "apikey": TWELVE_API_KEY, "order": "ASC", "timezone": "UTC"
    }
    try:
        r = requests.get(url, params=params, timeout=12).json()
        if "values" not in r: return None
        out = []
        for v in r["values"]:
            out.append({
                "t": datetime.fromisoformat(v["datetime"]),
                "o": float(v["open"]), "h": float(v["high"]),
                "l": float(v["low"]),  "c": float(v["close"])
            })
        out.sort(key=lambda x: x["t"])
        return out[-60:]
    except Exception:
        return None

def fetch_alpha(symbol: str, tf: str = "5min"):
    if "-OTC" in symbol: return None
    try:
        base, quote = symbol[:3], symbol[3:]
        url = "https://www.alphavantage.co/query"
        params = {
            "function": "FX_INTRADAY",
            "from_symbol": base, "to_symbol": quote,
            "interval": tf, "apikey": ALPHA_VANTAGE_KEY,
            "outputsize": "compact"
        }
        j = requests.get(url, params=params, timeout=12).json()
        key = "Time Series FX (5min)"
        if key not in j: return None
        out = []
        for ts, v in j[key].items():
            out.append({
                "t": datetime.fromisoformat(ts),
                "o": float(v["1. open"]), "h": float(v["2. high"]),
                "l": float(v["3. low"]),  "c": float(v["4. close"])
            })
        out.sort(key=lambda x: x["t"])
        return out[-60:]
    except Exception:
        return None

def fetch_ohlcv(symbol: str, tf: str = "5min"):
    d = fetch_twelvedata(symbol, tf)
    return d if d else fetch_alpha(symbol, tf)

# ---------- Indicators ----------
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
            e = e + k*(v - e); out.append(e)
        return out
    ef = ema_series(vals, fast)
    es = ema_series(vals, slow)
    macd_line = [(ef[i]-es[i]) if ef[i] and es[i] else None for i in range(len(vals))]
    mvals = [x for x in macd_line if x is not None]
    if len(mvals) < signal: return None, None
    sig = ema_series(mvals, signal)[-1]
    return macd_line[-1], sig

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

# ---------- Strategy ----------
def analyze(symbol):
    bars = fetch_ohlcv(symbol, "5min")
    if not bars or len(bars) < 60:
        return (False, None, 0.0)

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
        return (False, None, 0.0)

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
        return (True, "BUY", 0.85)
    elif dn >= need and dn > up:
        return (True, "SELL", 0.85)
    else:
        return (False, None, 0.0)

# ---------- Messaging ----------
async def send_signal(context: ContextTypes.DEFAULT_TYPE, pair: str, side: str, conf: float):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Win", callback_data="win"),
        InlineKeyboardButton("❌ Loss", callback_data="loss"),
        InlineKeyboardButton("⏭ Skip", callback_data="skip"),
    ]])
    txt = f"📊 OTC Signal\nPair: {pair}\n👉 {side}\nConfidence: {int(conf*100)}%"
    await context.bot.send_message(chat_id=STATE["chat_id"], text=txt, reply_markup=kb)

# ---------- Jobs ----------
async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    if not STATE["autopoll_running"] or STATE["chat_id"] is None:
        return
    now = datetime.now()
    for pair in STATE["watchlist"]:
        ok, side, conf = analyze(pair)
        if ok:
            await send_signal(context, pair, side, conf)
            STATE["pair_last_signal_time"][pair] = now
            STATE["last_signal_time"] = now

async def stop_job(context: ContextTypes.DEFAULT_TYPE):
    STATE["autopoll_running"] = False
    await context.bot.send_message(chat_id=STATE["chat_id"], text="⏹ Autopoll ended.")

# ---------- Buttons & Commands ----------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.data == "win":
        STATE["loss_streak"] = 0
        await q.edit_message_text(q.message.text + "\n✅ WIN")
    elif q.data == "loss":
        STATE["loss_streak"] += 1
        await q.edit_message_text(q.message.text + "\n❌ LOSS")
        if STATE["loss_streak"] >= STATE["loss_streak_limit"]:
            STATE["autopoll_running"] = False
            await context.bot.send_message(chat_id=q.message.chat_id, text="🚫 Stopped after 3 losses.")
    else:
        await q.edit_message_text(q.message.text + "\n⏭ SKIP")
    await q.answer()

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 OTC Bot ready. Use /signal or /autopoll")

async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for pair in STATE["watchlist"]:
        ok, side, conf = analyze(pair)
        if ok:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Win", callback_data="win"),
                InlineKeyboardButton("❌ Loss", callback_data="loss"),
                InlineKeyboardButton("⏭ Skip", callback_data="skip"),
            ]])
            await update.message.reply_text(
                f"📊 OTC Signal\nPair: {pair}\n👉 {side}\nConfidence: {int(conf*100)}%",
                reply_markup=kb
            )

async def cmd_autopoll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if STATE["autopoll_running"]:
        await update.message.reply_text("Already running.")
        return
    STATE["chat_id"] = update.effective_chat.id
    STATE["autopoll_running"] = True
    context.job_queue.run_repeating(poll_job, interval=300, first=1)
    context.job_queue.run_once(stop_job, when=STATE["duration_min"]*60)
    await update.message.reply_text("▶️ Autopoll started (every 5 min).")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STATE["autopoll_running"] = False
    for job in context.job_queue.jobs(): job.schedule_removal()
    await update.message.reply_text("⏹ Autopoll stopped.")

# ---------- Main ----------
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("autopoll", cmd_autopoll))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))
    app.run_polling()

if __name__ == "__main__":
    main()