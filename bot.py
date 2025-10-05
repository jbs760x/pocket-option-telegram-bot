# Po24/7 — minimal, stable, manual-check bot
# - /pologin <ssid> (saves & shows saved time)
# - /postatus      (shows masked SSID + UID)
# - /check SYMBOL [interval or tf=...]  (EURUSD-OTC ok)
# Data: TwelveData primary, AlphaVantage fallback
# Libs: python-telegram-bot==20.7 (polling; no Updater object)

import os, json, aiohttp, asyncio, logging
from datetime import datetime, timezone
from typing import Tuple, List, Optional
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ====== YOUR SETTINGS (baked in) ======
BOT_TOKEN   = "8471181182:AAFEhPc59AvzNsnuPbj-N2PatGbvgZnnd_0"
ADMIN_ID    = 7814662315
PO_UID      = "93269888"
PO_SSID_INIT = "d7a8a43d4618a7227c6ed769f8fd9975"

TWELVE_KEY  = "9aa4ea677d00474aa0c3223d0c812425"
ALPHA_KEY   = "BM22MZEIOLL68RI6"

SAVE_FILE   = "po_state.json"

# ====== Persistence ======
def load_state():
    if os.path.exists(SAVE_FILE):
        try:
            with open(SAVE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"ssid": PO_SSID_INIT, "saved_at": None}

def save_state(ssid: str):
    data = {"ssid": ssid, "saved_at": datetime.now(timezone.utc).isoformat()}
    with open(SAVE_FILE, "w") as f:
        json.dump(data, f)
    return data

STATE = load_state()

# ====== Helpers: normalize symbol/interval ======
VALID_TF = {
    "1": "1min","1m":"1min","1min":"1min","1minute":"1min",
    "5":"5min","5m":"5min","5min":"5min",
    "15":"15min","15m":"15min","15min":"15min",
    "30":"30min","30m":"30min","30min":"30min",
    "45":"45min","45m":"45min","45min":"45min",
    "60":"1h","1h":"1h","1hr":"1h",
    "2h":"2h","4h":"4h","8h":"8h",
    "1d":"1day","1day":"1day",
    "1w":"1week","1week":"1week",
    "1mo":"1month","1month":"1month",
}

def normalize_interval(args: List[str]) -> str:
    if not args: return "1min"
    for t in args:
        t = t.strip().lower()
        if t.startswith("tf="): t = t.split("=",1)[1].strip()
        if t in VALID_TF: return VALID_TF[t]
    return "1min"

def normalize_user_symbol(sym: str) -> Tuple[str,str]:
    """
    Returns (display_symbol, provider_symbol)
    Accepts EURUSD-OTC, EUR/USD, eurusd, etc.
    Maps *-OTC -> standard pair for data providers.
    """
    disp = sym.upper().strip()
    s = disp.replace(" ", "")
    if s.endswith("-OTC"): s = s[:-4]
    s = s.replace("_","")
    if "/" in s:
        base, quote = s.split("/",1)
        s = base + quote
    if len(s) >= 6:
        api = f"{s[:3]}/{s[3:6]}"
    else:
        api = s
    return disp, api

# ====== Indicators ======
def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period: return None
    k = 2/(period+1)
    e = sum(values[:period])/period
    for v in values[period:]:
        e = v*k + e*(1-k)
    return e

def rsi(values: List[float], period: int=14) -> Optional[float]:
    if len(values) <= period: return None
    gains, losses = [], []
    for i in range(1, len(values)):
        ch = values[i]-values[i-1]
        gains.append(max(ch,0)); losses.append(max(-ch,0))
    avg_g = sum(gains[:period])/period
    avg_l = sum(losses[:period])/period
    for i in range(period, len(values)-1):
        avg_g = (avg_g*(period-1) + gains[i]) / period
        avg_l = (avg_l*(period-1) + losses[i]) / period
    if avg_l == 0: return 100.0
    rs = avg_g/avg_l
    return 100 - (100/(1+rs))

# ====== Data fetchers ======
async def fetch_twelve(symbol: str, interval="1min", limit=120):
    if not TWELVE_KEY: return [], "Missing TWELVE_KEY"
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&outputsize={limit}&apikey={TWELVE_KEY}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=20) as r:
                js = await r.json()
                if isinstance(js, dict) and js.get("status") == "error":
                    return [], f"Twelve error: {js.get('message','unknown')}"
                vals = js.get("values")
                if not vals: return [], "Twelve no candles"
                # values newest-first; flip to oldest-first
                vals = list(reversed(vals))[-limit:]
                # ensure floats
                out = [{"open":float(v["open"]), "high":float(v["high"]),
                        "low":float(v["low"]), "close":float(v["close"])} for v in vals]
                return out, None
    except Exception as e:
        return [], f"Twelve fetch failed: {e}"

async def fetch_alpha(symbol: str, interval="1min", limit=120):
    if not ALPHA_KEY: return [], "Missing ALPHA_KEY"
    # symbol is like EUR/USD
    if "/" in symbol:
        base, quote = symbol.split("/",1)
    else:
        base, quote = symbol[:3], symbol[3:6] if len(symbol) >= 6 else ("EUR","USD")
    if interval not in {"1min","5min","15min","30min","60min"}:
        interval = "1min"
    url = ("https://www.alphavantage.co/query?"
           f"function=FX_INTRADAY&from_symbol={base}&to_symbol={quote}"
           f"&interval={interval}&apikey={ALPHA_KEY}&outputsize=compact")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=20) as r:
                js = await r.json()
                ts_key = next((k for k in js.keys() if "Time Series" in k), None)
                if not ts_key:
                    return [], f"Alpha error: {js.get('Note') or js.get('Error Message') or 'unknown'}"
                series = js[ts_key]
                rows = []
                for t,v in sorted(series.items()):
                    rows.append({"open":float(v["1. open"]), "high":float(v["2. high"]),
                                 "low":float(v["3. low"]),  "close":float(v["4. close"])})
                return rows[-limit:], None
    except Exception as e:
        return [], f"Alpha fetch failed: {e}"

async def fetch_candles(symbol: str, interval="1min", limit=120):
    # Try Twelve first, then Alpha
    cd, err = await fetch_twelve(symbol, interval, limit)
    if cd: return cd, None
    if err: logging.info(f"[DATA] Twelve fail: {err}")
    cd, err2 = await fetch_alpha(symbol, interval, limit)
    if cd: return cd, None
    return [], err or err2 or "no data"

# ====== Commands ======
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot ready. Use /help")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/pologin <ssid>\n"
        "/postatus\n"
        "/check SYMBOL [interval]\n"
        "Examples:\n"
        "• /check EURUSD-OTC 1min\n"
        "• /check EUR/USD tf=5m\n"
    )

def mask(ssid: str) -> str:
    if not ssid: return "-"
    if len(ssid) <= 8: return ssid
    return f"{ssid[:4]}...{ssid[-4:]}"

async def pologin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /pologin <ssid>")
    ssid = ctx.args[0].strip()
    if len(ssid) < 16:
        return await update.message.reply_text("That doesn't look like a valid SSID.")
    data = save_state(ssid)
    global STATE; STATE = data
    await update.message.reply_text("✅ SSID saved & looks valid.")

async def postatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    saved = STATE.get("saved_at") or "-"
    await update.message.reply_text(
        "🔐 PO SSID: " + mask(STATE.get("ssid") or "-") +
        f"\nVerified: {'True' if saved!='-' else 'False'}"
        f"\nSaved: {saved}\nUID: {PO_UID}"
    )

async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) == 0:
        return await update.message.reply_text("Usage: /check SYMBOL [interval]\nExample: /check EURUSD-OTC 1min")
    symbol_token = ctx.args[0]
    interval = normalize_interval(ctx.args[1:])
    disp, api_symbol = normalize_user_symbol(symbol_token)

    candles, err = await fetch_candles(api_symbol, interval, 120)
    if err:
        return await update.message.reply_text(f"❌ {err}\nTried: {disp} ({api_symbol}) {interval}")

    closes = [c["close"] for c in candles]
    e = ema(closes,50); r = rsi(closes,14)
    if e is None or r is None:
        return await update.message.reply_text(f"📊 {disp} {interval}\nCandles: {len(closes)}\nNot enough data yet.")
    decision = None
    if closes[-1] > e and r > 55: decision = "call"
    elif closes[-1] < e and r < 45: decision = "put"

    await update.message.reply_text(
        f"📊 {disp} {interval}\nCandles: {len(closes)}"
        f"\nEMA50: {round(e,5)}  RSI14: {round(r,2)}"
        f"\nSignal: {decision or 'none'}"
    )

# ====== Main ======
async def on_start(app: Application):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    logging.info("Webhook cleared; polling will start.")

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(on_start).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pologin", pologin))
    app.add_handler(CommandHandler("postatus", postatus))
    app.add_handler(CommandHandler("check", check_cmd))
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()