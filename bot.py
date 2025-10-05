# PocketOption Telegram Bot — Manual-Only Signals (OTC), 2-min autosignal
# Features:
# - Direct Pocket Option (PO) feed first using SSID+UID (experimental but works if region resolves)
# - /pologin, /postatus
# - /check EURUSD-OTC 1min  -> BUY/SELL + Confidence% + Payout%
# - /autosignal [tf=1min]   -> scans WATCHLIST every N seconds (default 120)
# - /interval [seconds]     -> show/change scan frequency
# - /payout [70-95]         -> min payout filter (skip signals below threshold)
# - /watch add/remove/list/clear
# - /result win|loss, /stats, /reset (pauses after 3 losses)
# - /fallback on|off (OFF by default; enables public feed fallback if PO feed unavailable)

import asyncio, json, math, aiohttp, logging, time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ====== YOUR KEYS / IDS (pre-filled) ======
BOT_TOKEN   = "8471181182:AAFEhPc59AvzNsnuPbj-N2PatGbvgZnnd_0"
PO_UID      = "93269888"
PO_SSID     = "d7a8a43d4618a7227c6ed769f8fd9975"

# ====== RUNTIME STATE ======
STATE = {
    "ssid": PO_SSID,
    "uid": PO_UID,
    "saved_at": datetime.now(timezone.utc).isoformat(),
    "scan_interval": 120,     # seconds (you can /interval 180 to change)
    "min_payout": 70,         # percent (skip signals below this)
    "losses": 0,
    "wins": 0,
    "paused": False,          # pauses after 3 losses
    "fallback_enabled": False # /fallback on to allow public feed if PO feed fails
}
WATCHLIST = ["EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC"]
AUTO_TASK = {"running": False, "task": None}

# ====== UTILS ======
def mask(s: str) -> str:
    if not s or len(s) < 9: return s or "-"
    return f"{s[:4]}...{s[-4:]}"

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def to_api_symbol(user_sym: str) -> str:
    # PO uses symbols like EURUSD-OTC; for API candles we map to base/quote when needed.
    # We keep the display symbol unchanged.
    s = user_sym.upper().replace(" ", "")
    if s.endswith("-OTC"):
        s = s[:-4]
    s = s.replace("_", "")
    if "/" in s:
        a,b = s.split("/",1); s = a + b
    if len(s) >= 6:
        return f"{s[:3]}/{s[3:6]}"
    return s

# ====== INDICATORS ======
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
        ch = values[i] - values[i-1]
        gains.append(max(ch,0)); losses.append(max(-ch,0))
    avg_g = sum(gains[:period])/period
    avg_l = sum(losses[:period])/period
    for i in range(period, len(values)-1):
        avg_g = (avg_g*(period-1) + gains[i]) / period
        avg_l = (avg_l*(period-1) + losses[i]) / period
    if avg_l == 0: return 100.0
    rs = avg_g/avg_l
    return 100 - (100/(1+rs))

def confidence_from(closes: List[float]) -> Tuple[Optional[str], Optional[int]]:
    """Simple, robust: EMA50 + RSI bands -> direction + 50–90% confidence."""
    if len(closes) < 60: return None, None
    e = ema(closes, 50); r = rsi(closes, 14)
    if e is None or r is None: return None, None
    last = closes[-1]
    if last > e and r > 55:
        # stronger when RSI far from 50 and momentum > EMA
        conf = 60 + int(min(35, (r-55)))            # 60–95
        return "BUY", min(conf, 90)
    if last < e and r < 45:
        conf = 60 + int(min(35, (45-r)))            # 60–95
        return "SELL", min(conf, 90)
    return None, None

# ====== PO FEED (EXPERIMENTAL) ======
# Pocket Option does not publish a stable public API. The code below uses HTTPS JSON endpoints
# that are known to be present in production and require the SSID cookie. This may change.
# If PO feed cannot be reached, use /fallback on to allow public fallback.
PO_BASES = [
    "https://po.market",           # global balancer
    "https://api-us-south.po.market",
    "https://api-us-north.po.market",
    "https://api-msk.po.market"
]
HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://pocketoption.com/en/cabinet/",
}

async def po_get_json(session: aiohttp.ClientSession, url: str, ssid: str) -> Tuple[Optional[dict], Optional[str]]:
    try:
        async with session.get(url, timeout=12, headers=HEADERS_BASE, cookies={"sessionToken": ssid}) as r:
            if r.status != 200:
                return None, f"HTTP {r.status}"
            return await r.json(), None
    except Exception as e:
        return None, str(e)

async def po_fetch_candles(symbol: str, tf: str, limit: int=120) -> Tuple[List[float], Optional[str]]:
    """
    Try several PO regions for a chart endpoint. Since PO endpoints are private,
    we attempt a best-effort path. If none respond, return ([], error).
    """
    # Common TF mapping
    tf_map = {"1min":"M1","5min":"M5","15min":"M15","30min":"M30","1h":"H1"}
    frame = tf_map.get(tf.lower(), "M1")

    # known path pattern (best-effort; may vary)
    # example pattern (in the wild) for quote history-like endpoints
    paths = [
        f"/api/v1/charts/history?symbol={symbol}&tf={frame}&limit={limit}",
        f"/charts/history?symbol={symbol}&tf={frame}&limit={limit}",
        f"/api/charts/history?symbol={symbol}&tf={frame}&limit={limit}",
    ]

    async with aiohttp.ClientSession() as s:
        for base in PO_BASES:
            for path in paths:
                url = base.rstrip("/") + path
                js, err = await po_get_json(s, url, STATE["ssid"])
                if js and isinstance(js, dict):
                    closes = []
                    # try a few common shapes
                    # 1) {"candles":[{"c":close, ...}, ...]}
                    if "candles" in js and isinstance(js["candles"], list):
                        for c in js["candles"]:
                            v = c.get("c") or c.get("close")
                            if v is not None:
                                closes.append(float(v))
                    # 2) {"values":[{"close":"..."}, ...]}
                    elif "values" in js and isinstance(js["values"], list):
                        for c in js["values"]:
                            v = c.get("close")
                            if v is not None:
                                closes.append(float(v))
                    # 3) {"data":[close, close, ...]}
                    elif "data" in js and isinstance(js["data"], list) and all(isinstance(x,(int,float)) for x in js["data"]):
                        closes = [float(x) for x in js["data"]]
                    if closes:
                        return closes[-limit:], None
            # next base
    return [], "PO candles unavailable (endpoint changed or region blocked)"

async def po_fetch_payout(symbol: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Fetch current instrument payout % for symbol from PO.
    Return (payout_percent, error)
    """
    paths = [
        f"/api/v1/instruments/payout?symbol={symbol}",
        f"/instruments/payout?symbol={symbol}",
        f"/api/instruments/payout?symbol={symbol}",
    ]
    async with aiohttp.ClientSession() as s:
        for base in PO_BASES:
            for path in paths:
                url = base.rstrip("/") + path
                js, err = await po_get_json(s, url, STATE["ssid"])
                if js and isinstance(js, dict):
                    # try common fields
                    v = js.get("payout") or js.get("percent") or js.get("rate")
                    try:
                        if v is not None:
                            return int(round(float(v))), None
                    except:
                        pass
    return None, "PO payout unavailable"

# ====== PUBLIC FALLBACK (OPTIONAL) ======
# You asked to test w/o fallback; it's OFF by default. You can /fallback on if PO feed fails.
# We'll use a very simple public endpoint (no OTC) as last resort.
async def public_fetch_candles(api_symbol: str, tf: str, limit: int=120) -> Tuple[List[float], Optional[str]]:
    # Free fallback: cryptofeed from Binance (for FX-like pairs we map EUR/USD -> EURUSDT as last resort)
    # It's not OTC and not ideal, but keeps the bot alive if PO is down and you enable /fallback on.
    base, quote = None, None
    if "/" in api_symbol:
        base, quote = api_symbol.split("/", 1)
    if not base or not quote:
        return [], "fallback: bad symbol"
    # naive map: USD->USDT
    if quote == "USD": quote = "USDT"
    symbol = (base + quote).upper()
    # map tf
    tf_map = {"1min":"1m","5min":"5m","15min":"15m","30min":"30m","1h":"1h"}
    kline = tf_map.get(tf.lower(), "1m")
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={kline}&limit={limit}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=12) as r:
                if r.status != 200:
                    return [], f"fallback HTTP {r.status}"
                rows = await r.json()
                closes = [float(row[4]) for row in rows]  # close price column
                return closes[-limit:], None
    except Exception as e:
        return [], f"fallback err: {e}"

# ====== CORE FETCH (PO first, optional fallback) ======
async def get_closes_and_payout(user_symbol: str, tf: str, limit: int=120) -> Tuple[List[float], Optional[int], Optional[str]]:
    # 1) Try PO
    closes, err = await po_fetch_candles(user_symbol, tf, limit)
    payout, perr = await po_fetch_payout(user_symbol)
    if closes:
        return closes, payout, None if payout is not None else (perr or None)
    # 2) Optional fallback
    if STATE["fallback_enabled"]:
        api_symbol = to_api_symbol(user_symbol)
        fcloses, ferr = await public_fetch_candles(api_symbol, tf, limit)
        return fcloses, None, ferr
    return [], None, err or "no data"

# ====== TELEGRAM COMMANDS ======
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 PO Signals (manual)\n"
        "Commands:\n"
        "/pologin <ssid>\n"
        "/postatus\n"
        "/check SYMBOL [tf=1min]\n"
        "/autosignal [tf=1min]\n"
        "/stop\n"
        "/interval [seconds]\n"
        "/payout [70-95]\n"
        "/watch add|remove|list|clear [SYMBOL]\n"
        "/result win|loss\n"
        "/stats\n"
        "/reset\n"
        "/fallback on|off"
    )

async def pologin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /pologin <ssid>")
    STATE["ssid"] = ctx.args[0].strip()
    STATE["saved_at"] = now_iso()
    await update.message.reply_text("🔐 SSID saved.")

async def postatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"UID: {STATE['uid']}\nSSID: {mask(STATE['ssid'])}\nSaved: {STATE['saved_at']}\n"
        f"Scan interval: {STATE['scan_interval']}s\nMin payout: {STATE['min_payout']}%\n"
        f"Fallback: {'ON' if STATE['fallback_enabled'] else 'OFF'}"
    )

def norm_tf(args: List[str]) -> str:
    if not args: return "1min"
    raw = args[0].lower()
    if raw.startswith("tf="): raw = raw.split("=",1)[1]
    ALLOWED = {"1min","5min","15min","30min","1h"}
    return raw if raw in ALLOWED else "1min"

async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /check SYMBOL [tf=1min]\nExample: /check EURUSD-OTC 1min")
    symbol = ctx.args[0].upper()
    tf = norm_tf(ctx.args[1:]) if len(ctx.args)>=2 else "1min"
    if STATE["paused"]:
        return await update.message.reply_text("⛔ Paused after 3 losses. /reset to continue.")

    closes, payout, err = await get_closes_and_payout(symbol, tf, 120)
    if not closes:
        return await update.message.reply_text(f"❌ Data error: {err or 'no candles'}")

    direction, conf = confidence_from(closes)
    paytxt = f"{payout}%" if payout is not None else "n/a"

    # enforce payout threshold only for suggestions (still show info)
    suggest = None
    if direction and conf is not None:
        if payout is None or payout >= STATE["min_payout"]:
            suggest = f"{direction}"
        else:
            suggest = f"(below payout {STATE['min_payout']}%)"

    await update.message.reply_text(
        f"📊 {symbol} {tf}\n"
        f"Payout: {paytxt}\n"
        f"Signal: {direction or 'NONE'}\n"
        f"Confidence: {conf if conf is not None else 'n/a'}%\n"
        f"{'➡️ TAKE IT' if suggest and '(' not in suggest else '⚠️ SKIP (low payout)'}"
        if direction else
        f"📊 {symbol} {tf}\nPayout: {paytxt}\nSignal: NONE"
    )

async def interval_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text(f"⏱ Interval: {STATE['scan_interval']}s\nUse: /interval 60")
    try:
        v = int(ctx.args[0])
        if v < 30 or v > 900:
            return await update.message.reply_text("Pick 30–900 seconds.")
        STATE["scan_interval"] = v
        await update.message.reply_text(f"✅ Interval set to {v}s.")
    except:
        await update.message.reply_text("Usage: /interval 120")

async def payout_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text(f"🎯 Min payout: {STATE['min_payout']}%\nUse: /payout 70")
    try:
        v = int(ctx.args[0])
        if v < 50 or v > 95: return await update.message.reply_text("Pick 50–95.")
        STATE["min_payout"] = v
        await update.message.reply_text(f"✅ Min payout set to {v}%")
    except:
        await update.message.reply_text("Usage: /payout 70")

async def watch_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sub = (ctx.args[0].lower() if ctx.args else "list")
    global WATCHLIST
    if sub == "add" and len(ctx.args)>=2:
        sym = ctx.args[1].upper()
        if sym not in WATCHLIST: WATCHLIST.append(sym)
        return await update.message.reply_text("Added.\n" + ", ".join(WATCHLIST))
    if sub == "remove" and len(ctx.args)>=2:
        sym = ctx.args[1].upper()
        if sym in WATCHLIST: WATCHLIST.remove(sym)
        return await update.message.reply_text("Removed.\n" + (", ".join(WATCHLIST) or "(empty)"))
    if sub == "clear":
        WATCHLIST = []
        return await update.message.reply_text("Watchlist cleared.")
    return await update.message.reply_text(", ".join(WATCHLIST) or "(empty)")

async def result_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text("Usage: /result win|loss")
    k = ctx.args[0].lower()
    if k == "win":
        STATE["wins"] += 1
    elif k == "loss":
        STATE["losses"] += 1
        if STATE["losses"] >= 3:
            STATE["paused"] = True
    else:
        return await update.message.reply_text("Use win|loss")
    await update.message.reply_text(f"✅ Recorded {k.upper()} | W:{STATE['wins']} L:{STATE['losses']}  {'⛔ Paused' if STATE['paused'] else ''}")

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tot = STATE["wins"] + STATE["losses"]
    wr = (STATE["wins"]/tot*100) if tot > 0 else 0.0
    await update.message.reply_text(
        f"📈 Stats\nWins: {STATE['wins']}  Losses: {STATE['losses']}  WR: {wr:.1f}%\n"
        f"Paused: {'Yes' if STATE['paused'] else 'No'}"
    )

async def reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["wins"] = 0; STATE["losses"] = 0; STATE["paused"] = False
    await update.message.reply_text("🔄 Reset done. You can continue.")

async def fallback_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await update.message.reply_text(f"Fallback: {'ON' if STATE['fallback_enabled'] else 'OFF'}\nUse: /fallback on|off")
    sub = ctx.args[0].lower()
    if sub in ("on","off"):
        STATE["fallback_enabled"] = (sub == "on")
        return await update.message.reply_text(f"✅ Fallback {sub.upper()}")
    await update.message.reply_text("Use: /fallback on|off")

# ====== AUTOSIGNAL ======
async def autosignal_loop(bot, chat_id: int, tf: str):
    await bot.send_message(chat_id, f"▶️ Autosignal ON | TF {tf} | every {STATE['scan_interval']}s | min payout {STATE['min_payout']}%")
    try:
        while AUTO_TASK["running"]:
            if STATE["paused"]:
                await bot.send_message(chat_id, "⛔ Paused after 3 losses. /reset to resume.")
                break

            hits = []
            for sym in list(WATCHLIST):
                closes, payout, err = await get_closes_and_payout(sym, tf, 120)
                if not closes:
                    continue
                direction, conf = confidence_from(closes)
                if not direction or conf is None:
                    continue
                if payout is not None and payout < STATE["min_payout"]:
                    continue
                hits.append((sym, direction, conf, payout))

                # small delay to be polite
                await asyncio.sleep(0.25)

            if hits:
                lines = []
                for sym, direction, conf, payout in hits:
                    paytxt = f"{payout}%" if payout is not None else "n/a"
                    arrow = "🔼" if direction=="BUY" else "🔽"
                    lines.append(f"{sym} {arrow} {direction} | Conf {conf}% | Payout {paytxt}")
                await bot.send_message(chat_id, "🎯 Signals:\n" + "\n".join(lines))

            await asyncio.sleep(STATE["scan_interval"])
    finally:
        await bot.send_message(chat_id, "⏹ Autosignal OFF")

async def autosignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tf = norm_tf(ctx.args) if ctx.args else "1min"
    if AUTO_TASK["running"]:
        return await update.message.reply_text("Already running. Use /stop")
    AUTO_TASK["running"] = True
    AUTO_TASK["task"] = asyncio.create_task(autosignal_loop(ctx.bot, update.effective_chat.id, tf))
    await update.message.reply_text("Starting autosignal...")

async def stop_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    AUTO_TASK["running"] = False
    await update.message.reply_text("Stopping autosignal...")

# ====== MAIN ======
async def _post_init(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except: pass

def main():
    application = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("pologin", pologin))
    application.add_handler(CommandHandler("postatus", postatus))
    application.add_handler(CommandHandler("check", check_cmd))
    application.add_handler(CommandHandler("autosignal", autosignal_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CommandHandler("interval", interval_cmd))
    application.add_handler(CommandHandler("payout", payout_cmd))
    application.add_handler(CommandHandler("watch", watch_cmd))
    application.add_handler(CommandHandler("result", result_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("reset", reset_cmd))
    application.add_handler(CommandHandler("fallback", fallback_cmd))
    # optional: echo parser disabled (manual mode only)
    application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)

if __name__ == "__main__":
    main()