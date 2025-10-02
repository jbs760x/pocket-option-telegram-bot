import logging
import requests
from datetime import datetime
from typing import Optional, List, Tuple

from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ===== LOGGING =====
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("otc-bot")

# ===== YOUR KEYS =====
TELEGRAM_BOT_TOKEN = "8471181182:AAEKGH1UASa5XvkXscb3jb5d1Yz19B8oJNM"
TWELVE_API_KEY     = "9aa4ea677d00474aa0c3223d0c812425"
ALPHA_VANTAGE_KEY  = "BM22MZEIOLL68RI6"  # fallback for non-OTC only

# ===== STRICT OTC SETTINGS / STATE =====
STATE = {
    "watchlist": ["EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "USDCHF-OTC"],

    "autopoll_running": False,
    "duration_min": 60,          # how long /autopoll runs

    # API-thrifty pacing
    "cooldown_min": 5,           # min per-pair gap (minutes)
    "min_signal_gap_min": 7,     # min global gap (minutes)

    # strict confluence filters
    "require_votes": 4,          # need 4/4: trend(EMA200), RSI50, MACD line>signal, EMA20>EMA50
    "threshold": 0.80,           # min confidence 80%
    "atr_floor": 0.0006,         # skip flat/choppy

    # risk guard
    "loss_streak_limit": 3,
    "loss_streak": 0,

    # internals
    "last_signal_time": None,
    "pair_last_signal_time": {},
    "chat_id": None,
}

# ---------- Helpers ----------
def _now() -> datetime:
    return datetime.utcnow()

def _safe_float(x) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

# ---------- Data fetchers ----------
def fetch_twelvedata(symbol: str, tf: str = "5min") -> Optional[List[dict]]:
    """Primary source (works for many -OTC symbols). Returns last up-to-60 bars."""
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol, "interval": tf, "outputsize": 60,
        "apikey": TWELVE_API_KEY, "order": "ASC", "timezone": "UTC"
    }
    try:
        j = requests.get(url, params=params, timeout=12).json()
        vals = j.get("values")
        if not vals:
            return None
        items = []
        for v in vals:
            t = datetime.fromisoformat(v["datetime"])
            o = _safe_float(v.get("open"))
            h = _safe_float(v.get("high"))
            l = _safe_float(v.get("low"))
            c = _safe_float(v.get("close"))
            if None in (o, h, l, c):
                continue
            items.append({"t": t, "o": o, "h": h, "l": l, "c": c})
        items.sort(key=lambda x: x["t"])
        return items[-60:] if items else None
    except Exception as e:
        log.warning("fetch_twelvedata error for %s: %s", symbol, e)
        return None

def fetch_alpha(symbol: str, tf: str = "5min") -> Optional[List[dict]]:
    """Alpha Vantage fallback for non-OTC only."""
    if "-OTC" in symbol:
        return None
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
        ts = j.get(key)
        if not ts:
            return None
        items = []
        for ts_k, v in ts.items():
            t = datetime.fromisoformat(ts_k)
            o = _safe_float(v.get("1. open"))
            h = _safe_float(v.get("2. high"))
            l = _safe_float(v.get("3. low"))
            c = _safe_float(v.get("4. close"))
            if None in (o, h, l, c):
                continue
            items.append({"t": t, "o": o, "h": h, "l": l, "c": c})
        items.sort(key=lambda x: x["t"])
        return items[-60:] if items else None
    except Exception as e:
        log.warning("fetch_alpha error for %s: %s", symbol, e)
        return None

def fetch_ohlcv(symbol: str, tf: str = "5min") -> Optional[List[dict]]:
    d = fetch_twelvedata(symbol, tf)
    return d if d else fetch_alpha(symbol, tf)

# ---------- Indicators ----------
def ema_last(vals: List[float], n: int) -> Optional[float]:
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = e + k * (v - e)
    return e

def rsi_last(vals: List[float], n: int = 14) -> Optional[float]:
    if len(vals) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(vals)):
        ch = vals[i] - vals[i - 1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
    rs = (avg_g / avg_l) if avg_l != 0 else 999.0
    return 100 - 100 / (1 + rs)

def _ema_series(arr: List[float], n: int) -> List[Optional[float]]:
    if len(arr) < n:
        return []
    k = 2 / (n + 1)
    e = sum(arr[:n]) / n
    out: List[Optional[float]] = [None] * (n - 1) + [e]
    for v in arr[n:]:
        e = e + k * (v - e)
        out.append(e)
    return out

def macd_last(vals: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[Optional[float], Optional[float]]:
    if len(vals) < slow + signal:
        return (None, None)
    ef = _ema_series(vals, fast)
    es = _ema_series(vals, slow)
    macd_line: List[Optional[float]] = [(ef[i] - es[i]) if (i < len(ef) and i < len(es) and ef[i] and es[i]) else None for i in range(len(vals))]
    macd_vals = [x for x in macd_line if x is not None]
    if len(macd_vals) < signal:
        return (None, None)
    sig_series = _ema_series(macd_vals, signal)
    return macd_line[-1], sig_series[-1] if sig_series else (None, None)

def atr_last(highs: List[float], lows: List[float], closes: List[float], n: int = 14) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    trs: List[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    atr = sum(trs[:n]) / n
    for i in range(n, len(trs)):
        atr = (atr * (n - 1) + trs[i]) / n
    return atr

# ---------- Strategy (strict OTC) ----------
def analyze(symbol: str) -> Tuple[bool, Optional[str], float]:
    bars = fetch_ohlcv(symbol, "5min")
    if not bars or len(bars) < 60:
        return (False, None, 0.0)

    closes = [b["c"] for b in bars]
    highs  = [b["h"] for b in bars]
    lows   = [b["l"] for b in bars]

    ema20  = ema_last(closes, 20)
    ema50  = ema_last(closes, 50)
    ema200 = ema_last(closes, 200) if len(closes) >= 200 else sum(closes) / len(closes)
    rsi14  = rsi_last(closes, 14)
    macd_line, macd_sig = macd_last(closes)
    atr14  = atr_last(highs, lows, closes, 14)

    if atr14 is None or atr14 < STATE["atr_floor"]:
        return (False, None, 0.0)

    up = dn = 0
    up += 1 if closes[-1] > (ema200 or closes[-1]) else 0
    dn += 1 if closes[-1] < (ema200 or closes[-1]) else 0
    if rsi14 is not None:
        up += 1 if rsi14 > 50 else 0
        dn += 1 if rsi14 <= 50 else 0
    if macd_line is not None and macd_sig is not None:
        up += 1 if macd_line > macd_sig else 0
        dn += 1 if macd_line <= macd_sig else 0
    if ema20 is not None and ema50 is not None:
        up += 1 if ema20 > ema50 else 0
        dn += 1 if ema20 <= ema50 else 0

    need = STATE["require_votes"]
    if up >= need and up > dn:
        side, votes = "BUY", up
    elif dn >= need and dn > up:
        side, votes = "SELL", dn
    else:
        return (False, None, 0.0)

    conf = max(0.0, min(0.95, 0.70 + 0.05 * (votes - 4)))
    if conf < STATE["threshold"]:
        return (False, None, conf)

    return (True, side, conf)

# ---------- Messaging ----------
async def send_signal(context: ContextTypes.DEFAULT_TYPE, pair: str, side: str, conf: float) -> None:
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
    now = _now()

    for pair in STATE["watchlist"]:
        # per-pair cooldown
        last_pair = STATE["pair_last_signal_time"].get(pair)
        if last_pair and (now - last_pair).total_seconds() < STATE["cooldown_min"] * 60:
            continue
        # global gap
        if STATE["last_signal_time"] and (now - STATE["last_signal_time"]).total_seconds() < STATE["min_signal_gap_min"] * 60:
            continue

        ok, side, conf = analyze(pair)
        if ok:
            if STATE["loss_streak"] >= STATE["loss_streak_limit"]:
                await context.bot.send_message(chat_id=STATE["chat_id"], text="🚫 3 losses in a row. Stopping.")
                STATE["autopoll_running"] = False
                return
            await send_signal(context, pair, side, conf)
            STATE["pair_last_signal_time"][pair] = now
            STATE["last_signal_time"] = now
        # else: silent on no-signal

async def stop_job(context: ContextTypes.DEFAULT_TYPE):
    STATE["autopoll_running"] = False
    await context.bot.send_message(chat_id=STATE["chat_id"], text="⏹️ Autopoll duration ended.")

# ---------- Buttons & Commands ----------
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
        if STATE["loss_streak"] >= STATE["loss_streak_limit"]:
            STATE["autopoll_running"] = False
            await context.bot.send_message(chat_id=q.message.chat_id, text="🚫 Stopped after 3 losses.")
    else:
        await q.edit_message_text(q.message.text + "\n⏭ SKIP")
    await q.answer()

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        "🤖 Strict OTC bot ready.\n"
        "Commands:\n"
        "• /signal – scan now (one message per valid pair)\n"
        "• /multisignal – bundle all valid pairs into ONE message\n"
        "• /watchlist EURUSD-OTC GBPUSD-OTC ... – set up to 5 pairs\n"
        "• /autopoll – auto every 5 min (stops after 3 losses)\n"
        "• /stop – stop autopoll"
    )

async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
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
    # silent on no-signal

async def cmd_multisignal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    signals = []
    for pair in STATE["watchlist"]:
        ok, side, conf = analyze(pair)
        if ok:
            signals.append(f"{pair}: {side} 📊 {int(conf*100)}%")
    if signals:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Win", callback_data="win"),
            InlineKeyboardButton("❌ Loss", callback_data="loss"),
            InlineKeyboardButton("⏭ Skip", callback_data="skip"),
        ]])
        text = "📊 Multi-Signal Prompt\n" + "\n".join(signals)
        await update.message.reply_text(text, reply_markup=kb)
    # else: silent

async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if context.args:
        pairs = [p.upper() for p in context.args][:5]
        if pairs:
            STATE["watchlist"] = pairs
    await update.message.reply_text(f"Watchlist set: {', '.join(STATE['watchlist'])}")

async def cmd_autopoll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if STATE["autopoll_running"]:
        await update.message.reply_text("ℹ️ Already running.")
        return
    STATE["chat_id"] = update.effective_chat.id
    STATE["autopoll_running"] = True
    STATE["last_signal_time"] = None
    STATE["pair_last_signal_time"] = {}
    context.job_queue.run_repeating(poll_job, interval=300, first=1, name="poll")  # every 5 min
    context.job_queue.run_once(stop_job, when=STATE["duration_min"]*60, name="stopper")
    await update.message.reply_text("▶️ Autopoll started. Waiting for high-quality signals…")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    STATE["autopoll_running"] = False
    for job in context.job_queue.jobs():
        if job.name in ("poll", "stopper"):
            job.schedule_removal()
    await update.message.reply_text("⏹️ Autopoll stopped.")

# ---------- Main ----------
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("signal",      cmd_signal))
    app.add_handler(CommandHandler("multisignal", cmd_multisignal))
    app.add_handler(CommandHandler("watchlist",   cmd_watchlist))
    app.add_handler(CommandHandler("autopoll",    cmd_autopoll))
    app.add_handler(CommandHandler("stop",        cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Bot starting…")
    app.run_polling()

if __name__ == "__main__":
    main()